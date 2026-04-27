"""DiscoBot - Remote-controlled audio player + native presentation kiosk."""

import logging
import signal
import sys
import threading

import uvicorn
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _run_server():
    uvicorn.run(
        "app.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
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
    logger.info("DiscoBot up — server on http://%s:%s, presentation on monitor %s",
                settings.host, settings.port, settings.presentation_monitor)

    sys.exit(qt_app.exec())
