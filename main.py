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

logging.basicConfig(
    level=logging.INFO if DEBUG else logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
    uvicorn.run(
        "app.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info" if DEBUG else "warning",
    )


if __name__ == "__main__":
    qt_app = QApplication(sys.argv)

    # Import after QApplication exists. app.api creates the AudioPlayer at import time;
    # we then attach the video widget once the window is shown.
    from app.api import player
    from app.presentation import PresentationWindow

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
