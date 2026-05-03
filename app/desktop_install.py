"""Installa/disinstalla il `.desktop` entry e le icone hicolor di DiscoBot.

Su Linux la dock/taskbar (GNOME, KDE, XFCE, Cinnamon, MATE, Unity) collega
una finestra al suo desktop entry tramite `WM_CLASS`. Senza un `.desktop`
file installato + `StartupWMClass=DiscoBot`, la dock mostra l'icona
generica di Python.

Questo modulo:
- esporta l'icona prodotta da `make_app_icon()` come PNG nelle taglie
  hicolor standard
- crea `~/.local/share/applications/discobot.desktop` con Exec puntato a
  `run.sh` della directory corrente
- chiama `gtk-update-icon-cache` se disponibile (idempotente)

Uso CLI:
    python main.py --install-desktop
    python main.py --uninstall-desktop
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DESKTOP_FILE_NAME = "discobot.desktop"
ICON_NAME = "discobot"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

USER_APPS_DIR = Path.home() / ".local" / "share" / "applications"
USER_ICONS_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor"


def _project_root() -> Path:
    """Directory che contiene run.sh — risale da __file__."""
    return Path(__file__).resolve().parent.parent


def _exec_cmd() -> str:
    """Comando da mettere in Exec= del desktop file."""
    return str(_project_root() / "run.sh")


def _export_icons() -> int:
    """Esporta l'icona Qt come PNG nei size hicolor. Ritorna numero di file scritti."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    from app.presentation import make_app_icon

    icon = make_app_icon()
    written = 0
    for size in ICON_SIZES:
        target_dir = USER_ICONS_DIR / f"{size}x{size}" / "apps"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{ICON_NAME}.png"
        pixmap: QPixmap = icon.pixmap(size, size)
        if pixmap.isNull():
            logger.warning("Icona vuota a size %d, skip", size)
            continue
        ok = pixmap.save(str(target), "PNG")
        if ok:
            written += 1
        else:
            logger.warning("Salvataggio fallito: %s", target)
    return written


def _write_desktop_file(exec_cmd: str) -> Path:
    USER_APPS_DIR.mkdir(parents=True, exist_ok=True)
    target = USER_APPS_DIR / DESKTOP_FILE_NAME
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DiscoBot\n"
        "GenericName=Audio Player\n"
        "Comment=Remote-controlled audio player + presenter kiosk\n"
        f"Exec={exec_cmd}\n"
        f"Icon={ICON_NAME}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
        "StartupWMClass=DiscoBot\n"
        "StartupNotify=true\n"
    )
    target.write_text(contents, encoding="utf-8")
    target.chmod(0o644)
    return target


def _refresh_icon_cache() -> None:
    """Best-effort: aggiorna la icon cache hicolor. Errori non sono fatali."""
    if shutil.which("gtk-update-icon-cache"):
        try:
            subprocess.run(
                ["gtk-update-icon-cache", "-q", "-t", str(USER_ICONS_DIR)],
                check=False, timeout=10,
            )
        except Exception:
            pass


def install() -> int:
    """Installa desktop entry + icone. Ritorna 0 ok, !=0 errore."""
    if sys.platform != "linux":
        print("Install desktop: solo su Linux. Skip.")
        return 0
    try:
        # Per esportare le icone serve un QApplication attivo (Qt richiede
        # un'instance per usare QPixmap.save). La istanziamo se manca.
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            # offscreen platform: non serve display, va bene anche su SSH
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            QApplication([])
        n = _export_icons()
        print(f"Esportate {n} icone in {USER_ICONS_DIR}")
        target = _write_desktop_file(_exec_cmd())
        print(f"Desktop file installato: {target}")
        _refresh_icon_cache()
        print(
            "Installazione completata. La dock dovrebbe mostrare l'icona "
            "DiscoBot al prossimo avvio. Su GNOME, per il tray nella barra "
            "superiore, installa l'estensione 'AppIndicator and "
            "KStatusNotifierItem Support'."
        )
        return 0
    except Exception as e:
        print(f"Errore durante l'installazione: {e}", file=sys.stderr)
        logger.exception("install desktop entry failed")
        return 1


def uninstall() -> int:
    if sys.platform != "linux":
        print("Uninstall desktop: solo su Linux. Skip.")
        return 0
    removed = []
    target = USER_APPS_DIR / DESKTOP_FILE_NAME
    if target.exists():
        target.unlink()
        removed.append(str(target))
    for size in ICON_SIZES:
        png = USER_ICONS_DIR / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        if png.exists():
            png.unlink()
            removed.append(str(png))
    _refresh_icon_cache()
    if removed:
        print("Rimossi:")
        for p in removed:
            print(f"  - {p}")
    else:
        print("Niente da rimuovere.")
    return 0
