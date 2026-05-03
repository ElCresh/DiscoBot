"""DiscoBot - Remote-controlled audio player + native presentation kiosk."""

import os
import sys
# Silenzia libvlc e VA-API prima che qualunque import li inizializzi.
os.environ.setdefault("VLC_VERBOSE", "-1")
os.environ.setdefault("LIBVA_MESSAGING_LEVEL", "0")

# Wayland + VLC video embedding non funziona: il binding di VLC al widget Qt
# nativo richiede un XID. Forziamo XWayland (QT_QPA_PLATFORM=xcb) se l'utente
# non l'ha gia' impostata. XWayland e' installato di default su Ubuntu Desktop.
if sys.platform == "linux" and os.environ.get("XDG_SESSION_TYPE") == "wayland":
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

print("Avvio in corso...", flush=True)

if os.environ.get("DISCOBOT_SKIP_DEPCHECK") != "1":
    from app.bootstrap import check_dependencies
    check_dependencies()

import logging
import signal
import socket
import threading

# Flag --debug da CLI: rimosso da argv prima di passarlo a Qt per pulizia.
_DEBUG_CLI = "--debug" in sys.argv
if _DEBUG_CLI:
    sys.argv.remove("--debug")

# One-shot Spotify OAuth: esegue il flow nel browser, persiste le credenziali
# librespot e termina senza avviare GUI/server.
if "--spotify-login" in sys.argv:
    from app.spotify_audio import bootstrap_login
    bootstrap_login()
    sys.exit(0)

# One-shot reset della password Manager.
if "--reset-password" in sys.argv:
    from app.auth import get_auth
    auth = get_auth()
    if auth.is_configured():
        confirm = input("Sicuro di cancellare la password Manager? [s/N] ")
        if confirm.strip().lower() not in ("s", "si", "y", "yes"):
            print("Annullato.")
            sys.exit(0)
    auth.reset()
    print("Password Manager cancellata. Al prossimo accesso a /m verra' chiesta una nuova password.")
    sys.exit(0)

# One-shot install/uninstall del desktop entry (solo Linux).
if "--install-desktop" in sys.argv:
    from app.desktop_install import install
    sys.exit(install())
if "--uninstall-desktop" in sys.argv:
    from app.desktop_install import uninstall
    sys.exit(uninstall())

import uvicorn
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.config import settings

DEBUG = _DEBUG_CLI or settings.debug

# Use uvicorn's DefaultFormatter at the root so our app logs share the look
# of uvicorn's HTTP/lifecycle lines ("LEVEL:   message", with TTY colors).
import uvicorn.logging

_handler = logging.StreamHandler()
_handler.setFormatter(uvicorn.logging.DefaultFormatter(
    fmt="%(levelprefix)s %(name)s: %(message)s",
    use_colors=None,  # auto-detect TTY
))
_root = logging.getLogger()
_root.setLevel(logging.DEBUG if DEBUG else logging.WARNING)
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.addHandler(_handler)

# Silence chatty third-party loggers even at DEBUG level — they don't help
# debug *our* code and would drown the useful traces.
for noisy in ("asyncio", "urllib3", "httpx", "httpcore", "yt_dlp", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _local_ip() -> str:
    """IP della scheda di rete usata per uscire verso Internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _run_server():
    # log_config=None tells uvicorn NOT to install its own logging config,
    # so its loggers (uvicorn, uvicorn.access, uvicorn.error) inherit the
    # root config we set above — same timestamp/level/name format as ours.
    uvicorn.run(
        "app.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info" if DEBUG else "warning",
        log_config=None,
    )


if __name__ == "__main__":
    # Windows: tell the shell this is a distinct app, not just python.exe.
    # Without this, the taskbar groups our window under Python's icon and
    # ignores setWindowIcon. Must run before QApplication.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "DiscoBot.Presenter"
            )
        except Exception:
            logger.debug("Failed to set AppUserModelID", exc_info=True)

    # Identita' applicativa: settata PRIMA di costruire QApplication cosi' la
    # registrazione al portale D-Bus (Wayland/XDG) usa subito l'app id
    # corretto. Settarla dopo causa il warning "Could not register app ID:
    # Connection already associated with an application ID".
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication
    QCoreApplication.setApplicationName("DiscoBot")
    QCoreApplication.setOrganizationName("DiscoBot")
    if sys.platform == "linux":
        QGuiApplication.setDesktopFileName("discobot")

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationDisplayName("DiscoBot")

    # Import after QApplication exists. app.api creates the AudioPlayer at import time;
    # we then attach the video widget once the window is shown.
    from app.api import player
    from app.presentation import PresentationWindow, make_app_icon

    qt_app.setWindowIcon(make_app_icon())

    window = PresentationWindow(player, monitor=settings.presentation_monitor)
    window.show_on_target_monitor()
    qt_app.processEvents()  # ensure HWND is realized before set_hwnd

    player.attach_video_window(int(window.video_frame.winId()))

    qt_app.aboutToQuit.connect(player._atexit_save)

    # Ctrl+C handling: Qt's native event loop on Windows doesn't process Python
    # signals on its own. We install a SIGINT handler that asks Qt to quit, and
    # a no-op heartbeat timer so the interpreter gets cycles to deliver the signal.
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    heartbeat = QTimer()
    heartbeat.start(200)
    heartbeat.timeout.connect(lambda: None)

    threading.Thread(target=_run_server, daemon=True).start()

    # Inibisce sospensione + oscuramento monitor finche' DiscoBot e' vivo.
    # Always-on (no toggle UI). Rilasciato su aboutToQuit.
    from app.power import get_inhibitor
    get_inhibitor().start()
    qt_app.aboutToQuit.connect(get_inhibitor().stop)

    # System tray icon: menu di accesso rapido (Manager / pubblico / IP /
    # tunnel / Esci). Su GNOME senza estensione AppIndicator l'icona non
    # compare ma l'app continua.
    from app.tray import TrayManager
    tray = TrayManager(qt_app, window, settings.port, make_app_icon())
    tray.show()
    qt_app.aboutToQuit.connect(tray.hide)

    # Pre-warm the Spotify librespot session in background so the first play
    # doesn't pay the AP handshake latency. Status is exposed via
    # /spotify/auth-status (session_status: idle|warming|ready|failed) and
    # surfaced in the web UI as a yellow pulsing dot until ready.
    from app.spotify_audio import get_audio as _get_spotify_audio
    threading.Thread(target=_get_spotify_audio().prewarm, daemon=True).start()

    lan_ip = _local_ip()
    print(
        f"DiscoBot pronto — http://{settings.host}:{settings.port} "
        f"(LAN: http://{lan_ip}:{settings.port}), "
        f"presentazione su monitor {settings.presentation_monitor}"
        + (" [DEBUG]" if DEBUG else ""),
        flush=True,
    )

    sys.exit(qt_app.exec())
