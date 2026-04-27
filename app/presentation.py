"""Native fullscreen kiosk presentation window (PySide6).

Layout: header (title/artist/progress + QR) over a row with the video/cover
on the left and the upcoming queue on the right.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

import qrcode
import vlc
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.coverart import resolve_cover_for
from app.models import Track, TrackType

logger = logging.getLogger(__name__)


STYLESHEET = """
QMainWindow { background-color: #1a1a2e; }
QWidget { color: #eee; }
QLabel { background-color: transparent; }
QFrame#header { background-color: #16213e; border-radius: 12px; }
QLabel#title { color: #e94560; font-size: 32pt; font-weight: 700; }
QLabel#artist { color: #ddd; font-size: 16pt; }
QLabel#progress { color: #aaa; font-size: 18pt; font-weight: 600; }
QLabel#qrcode { background-color: white; border-radius: 6px; padding: 3px; }
QLabel#clock { color: #aaa; font-size: 72pt; font-weight: 300; }
QListWidget { background-color: #0f3460; border: none; border-radius: 12px; padding: 8px; font-size: 14pt; }
QListWidget::item { padding: 10px; border-bottom: 1px solid #16213e; }
QListWidget::item:last { border-bottom: none; }
"""


_QR_SIZE = 160


def _resolve_track_url(track: Track) -> str | None:
    """Public URL of the current media, or None for local files."""
    if track.type == TrackType.YOUTUBE or track.type == TrackType.SOUNDCLOUD:
        return track.path or None
    if track.type == TrackType.SPOTIFY:
        return f"https://open.spotify.com/track/{track.path}" if track.path else None
    return None


def make_qr_pixmap(text: str, size: int = _QR_SIZE) -> QPixmap:
    """Render a QR code as a black-on-white QPixmap of exactly `size` px,
    using QPainter (no PIL dependency). Uses a fractional cell size with
    rounded rect coordinates so the matrix fills the requested area tightly."""
    qr = qrcode.QRCode(border=2, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    pix = QPixmap(size, size)
    pix.fill(Qt.white)
    painter = QPainter(pix)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("black"))
    cell_f = size / n
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if not dark:
                continue
            px = int(round(x * cell_f))
            py = int(round(y * cell_f))
            w = int(round((x + 1) * cell_f)) - px
            h = int(round((y + 1) * cell_f)) - py
            painter.drawRect(px, py, w, h)
    painter.end()
    return pix


def _format_time(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


class AspectVideoContainer(QWidget):
    """Container that keeps the inner video QFrame at the source aspect ratio,
    centered both vertically and horizontally. The empty space around the frame
    shows the parent's background color (no black letterbox)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_frame = QFrame(self)
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        self.video_frame.setStyleSheet("background-color: black; border-radius: 8px;")
        self._aspect: float | None = None
        self._relayout()

    def set_aspect(self, w: int, h: int):
        if w <= 0 or h <= 0:
            self._aspect = None
        else:
            self._aspect = w / h
        self._relayout()

    def reset_aspect(self):
        self._aspect = None
        self._relayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self):
        cw, ch = max(1, self.width()), max(1, self.height())
        if self._aspect is None:
            self.video_frame.setGeometry(0, 0, cw, ch)
            return
        target_w = cw
        target_h = int(round(cw / self._aspect))
        if target_h > ch:
            target_h = ch
            target_w = int(round(ch * self._aspect))
        x = (cw - target_w) // 2
        y = (ch - target_h) // 2
        self.video_frame.setGeometry(x, y, target_w, target_h)


class PresentationWindow(QMainWindow):
    state_received = Signal(dict)
    video_size_received = Signal(int, int)

    def __init__(self, player, monitor: int = 0):
        super().__init__()
        self._player = player
        self._monitor = monitor
        self._current_track_id: int | None = None
        self._cover_thread: threading.Thread | None = None

        self.setWindowTitle("DiscoBot")
        self.setStyleSheet(STYLESHEET)
        self.setCursor(Qt.BlankCursor)

        self._build_ui()

        # Hook player state updates (sync subscriber emits Qt signal — thread-safe)
        from app.player import ws_manager

        ws_manager.subscribe_sync(lambda data: self.state_received.emit(data))
        self.state_received.connect(self._on_state)

        # VLC vout event → real video dimensions. Until this fires, we keep the
        # cover/idle pane visible — that way the user never sees the black
        # letterbox flash before the first frame is rendered.
        events = player.vlc_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerVout, self._on_vout)
        self.video_size_received.connect(self._on_video_ready)

        # Progress timer (state broadcasts don't include position deltas)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._tick_progress)
        self._progress_timer.start()

        # Idle clock timer
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()

        # Render initial state
        self._on_state(player.get_state().model_dump())

    @property
    def video_frame(self):
        """HWND target for VLC. Stays valid across container resizes."""
        return self.video_container.video_frame

    # --- UI construction ---

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # --- HEADER (full width): title/artist/progress on the left, QR on the right ---
        header = QFrame()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 16, 16, 16)
        hl.setSpacing(20)

        info = QVBoxLayout()
        info.setSpacing(4)
        self._title_label = QLabel("Nessuna traccia")
        self._title_label.setObjectName("title")
        self._title_label.setWordWrap(True)
        self._artist_label = QLabel("")
        self._artist_label.setObjectName("artist")
        self._artist_label.setWordWrap(True)
        self._progress_label = QLabel("0:00 / 0:00")
        self._progress_label.setObjectName("progress")
        info.addWidget(self._title_label)
        info.addWidget(self._artist_label)
        info.addStretch(1)
        info.addWidget(self._progress_label)
        hl.addLayout(info, stretch=1)

        self._qr_label = QLabel()
        self._qr_label.setObjectName("qrcode")
        # 3px padding each side from stylesheet → label = QR + 6
        self._qr_label.setFixedSize(_QR_SIZE + 6, _QR_SIZE + 6)
        self._qr_label.setAlignment(Qt.AlignCenter)
        # Keep the slot reserved when hidden so the header layout doesn't shift
        sp = self._qr_label.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self._qr_label.setSizePolicy(sp)
        self._qr_label.setVisible(False)
        hl.addWidget(self._qr_label, alignment=Qt.AlignVCenter | Qt.AlignRight)

        header.setMinimumHeight(_QR_SIZE + 32)
        root.addWidget(header)

        # --- MAIN row: video stack (left) + queue (right) ---
        main = QHBoxLayout()
        main.setSpacing(16)

        self._stack = QStackedWidget()
        self._stack.setMinimumWidth(800)

        self.video_container = AspectVideoContainer()
        self._stack.addWidget(self.video_container)

        self._cover_label = QLabel()
        self._cover_label.setAlignment(Qt.AlignCenter)
        self._cover_label.setStyleSheet("background-color: black; border-radius: 12px;")
        self._stack.addWidget(self._cover_label)

        idle_widget = QWidget()
        idle_layout = QVBoxLayout(idle_widget)
        idle_layout.setAlignment(Qt.AlignCenter)
        self._idle_clock = QLabel("--:--")
        self._idle_clock.setObjectName("clock")
        self._idle_clock.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(self._idle_clock)
        self._stack.addWidget(idle_widget)

        main.addWidget(self._stack, stretch=3)

        # Queue column (no more "now playing" card — info is in the header)
        queue_col = QVBoxLayout()
        queue_col.setSpacing(8)
        queue_label = QLabel("Prossime tracce")
        queue_label.setStyleSheet("color: #aaa; font-size: 14pt; padding-left: 4px;")
        queue_col.addWidget(queue_label)
        self._queue_list = QListWidget()
        self._queue_list.setSelectionMode(QListWidget.NoSelection)
        self._queue_list.setFocusPolicy(Qt.NoFocus)
        queue_col.addWidget(self._queue_list, stretch=1)

        queue_widget = QWidget()
        queue_widget.setLayout(queue_col)
        queue_widget.setMinimumWidth(420)
        main.addWidget(queue_widget, stretch=2)

        root.addLayout(main, stretch=1)

        self.setCentralWidget(central)

    # --- Display ---

    def show_on_target_monitor(self):
        screens = QApplication.screens()
        if not screens:
            self.showFullScreen()
            return
        target = screens[min(self._monitor, len(screens) - 1)]
        self.setGeometry(target.geometry())
        self.showFullScreen()

    # --- State handling ---

    def _on_state(self, state: dict):
        current = state.get("current_track")
        queue = state.get("queue", [])

        if current:
            track = Track(**current)
            self._title_label.setText(track.title or "—")
            artist_bits = [b for b in (track.artist, track.album) if b]
            self._artist_label.setText(" — ".join(artist_bits))

            if track.id != self._current_track_id:
                self._current_track_id = track.id
                self._switch_media_view(track)
                self._update_qr(track)
        else:
            self._current_track_id = None
            self._title_label.setText("Nessuna traccia")
            self._artist_label.setText("")
            self._progress_label.setText("0:00 / 0:00")
            self._stack.setCurrentIndex(2)  # idle
            self._qr_label.setVisible(False)
            self._qr_label.clear()

        # Queue
        self._queue_list.clear()
        for entry in queue:
            t = Track(**entry)
            label_bits = [t.title or "—"]
            if t.artist:
                label_bits.append(f"· {t.artist}")
            self._queue_list.addItem(QListWidgetItem(" ".join(label_bits)))

    def _switch_media_view(self, track: Track):
        self._cover_label.clear()

        # Show idle by default; cover takes over if available.
        # The video pane is shown only when VLC's vout event reports actual
        # frame dimensions (see _on_video_ready), so any source — local mp4,
        # YouTube, Spotify — gets its real aspect ratio with no flash.
        self._stack.setCurrentIndex(2)

        def worker(track_id: int):
            path = resolve_cover_for(track)
            if track_id != self._current_track_id:
                return
            QTimer.singleShot(0, lambda: self._apply_cover(track_id, path))

        self._cover_thread = threading.Thread(
            target=worker, args=(track.id,), daemon=True
        )
        self._cover_thread.start()

    def _update_qr(self, track: Track):
        url = _resolve_track_url(track)
        if not url:
            self._qr_label.setVisible(False)
            self._qr_label.clear()
            return
        try:
            pix = make_qr_pixmap(url, size=_QR_SIZE)
        except Exception:
            logger.warning("QR generation failed for %s", url, exc_info=True)
            self._qr_label.setVisible(False)
            return
        self._qr_label.setPixmap(pix)
        self._qr_label.setVisible(True)

    def _apply_cover(self, track_id: int, path: str | None):
        if track_id != self._current_track_id:
            return
        if not path or not Path(path).exists():
            return  # leave video frame visible
        pix = QPixmap(path)
        if pix.isNull():
            return
        size = self._cover_label.size()
        self._cover_label.setPixmap(
            pix.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self._stack.setCurrentIndex(1)

    # --- Tickers ---

    def _tick_progress(self):
        try:
            state = self._player.get_state()
        except Exception:
            return
        if not state.current_track:
            return
        self._progress_label.setText(
            f"{_format_time(state.position)} / {_format_time(state.duration)}"
        )

    def _tick_clock(self):
        self._idle_clock.setText(datetime.now().strftime("%H:%M"))

    # --- VLC vout handler ---

    def _on_vout(self, event):
        """Fired from a VLC thread when video output starts/stops. Reads the
        source dimensions and hops to the GUI thread via Signal."""
        try:
            w, h = self._player.vlc_player.video_get_size(0)
        except Exception:
            return
        if w and h:
            self.video_size_received.emit(int(w), int(h))

    def _on_video_ready(self, w: int, h: int):
        """GUI-thread slot: VLC has actual video — set aspect and switch pane."""
        self.video_container.set_aspect(w, h)
        self._stack.setCurrentIndex(0)

    # --- Resize handling ---

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-scale current cover on window resize
        if self._stack.currentIndex() == 1 and self._cover_label.pixmap():
            pm = self._cover_label.pixmap()
            self._cover_label.setPixmap(
                pm.scaled(self._cover_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
