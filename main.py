"""DiscoBot - Remote-controlled audio player + native presentation kiosk."""

import os
# Silenzia libvlc prima che qualunque import lo inizializzi.
os.environ.setdefault("VLC_VERBOSE", "-1")

print("Avvio in corso...", flush=True)

import logging
import signal
import socket
import sys
import threading

# Flag --debug da CLI: rimosso da argv prima di passarlo a Qt per pulizia.
_DEBUG_CLI = "--debug" in sys.argv
if _DEBUG_CLI:
    sys.argv.remove("--debug")

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

    qt_app = QApplication(sys.argv)

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
    lan_ip = _local_ip()
    print(
        f"DiscoBot pronto — http://{settings.host}:{settings.port} "
        f"(LAN: http://{lan_ip}:{settings.port}), "
        f"presentazione su monitor {settings.presentation_monitor}"
        + (" [DEBUG]" if DEBUG else ""),
        flush=True,
    )

    sys.exit(qt_app.exec())
