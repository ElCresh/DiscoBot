"""System tray icon for DiscoBot.

Espone un'icona nella system tray con menu di accesso rapido:
- Apri pannello Manager / interfaccia Pubblica nel browser
- Mostra/copia indirizzo LAN e URL tunnel (se attivo)
- Mostra/Nascondi finestra Presenter
- Esci pulitamente

Note di compatibilita':
- Su GNOME default (post-2018) il tray non e' visibile senza l'estensione
  "AppIndicator and KStatusNotifierItem Support". L'icona viene creata
  comunque; se la system tray non c'e', `QSystemTrayIcon.isSystemTrayAvailable`
  ritorna False e il TrayManager segnala un warning ma non blocca l'app.
"""

from __future__ import annotations

import logging
import socket
import webbrowser

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)


def _local_ip() -> str:
    """IP della scheda usata per uscire verso Internet (best-effort)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class TrayManager:
    """Gestisce l'icona di system tray + menu dinamico."""

    def __init__(self, qt_app: QApplication, presentation_window, host_port: int, icon) -> None:
        self.qt_app = qt_app
        self.presenter = presentation_window
        self.port = host_port
        self.lan_ip = _local_ip()
        self.base_url = f"http://{self.lan_ip}:{self.port}"

        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip(f"DiscoBot · {self.base_url}")

        # Menu rebuild ogni 3s per riflettere stato tunnel / presenter.
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._rebuild_menu)
        self._refresh_timer.start(3000)

        self._rebuild_menu()
        self.tray.activated.connect(self._on_tray_activated)

    # -------- public API --------

    def show(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning(
                "System tray non disponibile (GNOME senza estensione "
                "AppIndicator? Wayland senza supporto?). DiscoBot continua."
            )
            return
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    # -------- menu construction --------

    def _rebuild_menu(self) -> None:
        # Lazy import per evitare circolari all'avvio (tunnel.py importa
        # qrcode etc., niente bisogno di forzarlo qui se non e' usato).
        try:
            from app.tunnel import get_tunnel
            tstatus = get_tunnel().status()
        except Exception:
            tstatus = {}
        tunnel_url = tstatus.get("url") if tstatus.get("running") else None

        menu = QMenu()

        # Header informativo (disabilitato, solo branding)
        header = QAction(f"DiscoBot · {self.base_url}", menu)
        header.setEnabled(False)
        menu.addAction(header)
        menu.addSeparator()

        act_manager = QAction("Apri pannello Manager", menu)
        act_manager.triggered.connect(lambda: webbrowser.open(f"{self.base_url}/m"))
        menu.addAction(act_manager)

        act_public = QAction("Apri interfaccia pubblica", menu)
        act_public.triggered.connect(lambda: webbrowser.open(f"{self.base_url}/"))
        menu.addAction(act_public)
        menu.addSeparator()

        # IP LAN (click = copia URL completo)
        act_lan = QAction(f"IP LAN: {self.lan_ip}", menu)
        act_lan.triggered.connect(lambda: self._copy(self.base_url))
        act_lan.setToolTip("Click per copiare l'URL LAN")
        menu.addAction(act_lan)

        # Tunnel URL (visibile solo se attivo)
        if tunnel_url:
            short = tunnel_url.replace("https://", "")
            act_tun = QAction(f"Tunnel: {short}", menu)
            act_tun.triggered.connect(lambda u=tunnel_url: self._copy(u))
            act_tun.setToolTip("Click per copiare l'URL del tunnel")
            menu.addAction(act_tun)

        menu.addSeparator()

        # Toggle Presenter
        if self.presenter is not None:
            visible = self.presenter.isVisible()
            label = "Nascondi Presenter" if visible else "Mostra Presenter"
            act_pres = QAction(label, menu)
            act_pres.triggered.connect(self._toggle_presenter)
            menu.addAction(act_pres)
            menu.addSeparator()

        act_quit = QAction("Esci", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        # Aggiorna tooltip
        tooltip = f"DiscoBot · {self.base_url}"
        if tunnel_url:
            tooltip += f"\nTunnel: {tunnel_url}"
        self.tray.setToolTip(tooltip)
        self.tray.setContextMenu(menu)

    # -------- actions --------

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            webbrowser.open(f"{self.base_url}/m")

    def _toggle_presenter(self) -> None:
        if self.presenter is None:
            return
        if self.presenter.isVisible():
            self.presenter.hide()
        else:
            self.presenter.show()
            try:
                self.presenter.show_on_target_monitor()
            except Exception:
                pass

    def _copy(self, text: str) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        if self.tray.supportsMessages():
            self.tray.showMessage(
                "DiscoBot", "URL copiato negli appunti",
                QSystemTrayIcon.Information, 1500,
            )

    def _quit(self) -> None:
        try:
            from app.tunnel import get_tunnel
            if get_tunnel().status().get("running"):
                get_tunnel().stop()
        except Exception:
            logger.exception("Tunnel stop on quit failed")
        # Safety belt: rilascia subito l'inhibitor (singleton condiviso con
        # main.py). Idempotente — se aboutToQuit lo richiama dopo, no-op.
        try:
            from app.power import get_inhibitor
            get_inhibitor().stop()
        except Exception:
            logger.exception("Inhibitor stop on quit failed")
        self.qt_app.quit()
