"""Native fullscreen kiosk presentation window (PySide6).

Renders VLC video into an embedded widget, shows cover art for local files,
and a live "now playing + upcoming queue" panel synced to the player state.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

import vlc
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
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
QFrame#nowPlaying { background-color: #16213e; border-radius: 12px; padding: 18px; }
QLabel#title { color: #e94560; font-size: 28pt; font-weight: 700; }
QLabel#artist { color: #ddd; font-size: 16pt; }
QLabel#progress { color: #aaa; font-size: 14pt; }
QLabel#idle { color: #e94560; font-size: 48pt; font-weight: 700; }
QLabel#clock { color: #aaa; font-size: 24pt; }
QListWidget { background-color: #0f3460; border: none; border-radius: 12px; padding: 8px; font-size: 14pt; }
QListWidget::item { padding: 10px; border-bottom: 1px solid #16213e; }
QListWidget::item:last { border-bottom: none; }
"""


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

        # VLC vout event → video aspect ratio (fired from a VLC thread, signal hops to GUI thread)
        events = player.vlc_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerVout, self._on_vout)
        self.video_size_received.connect(self.video_container.set_aspect)

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
        root = QHBoxLayout(central)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(24)

        # LEFT — stacked: video / cover / idle
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
        self._idle_title = QLabel("DiscoBot")
        self._idle_title.setObjectName("idle")
        self._idle_title.setAlignment(Qt.AlignCenter)
        self._idle_clock = QLabel("--:--")
        self._idle_clock.setObjectName("clock")
        self._idle_clock.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(self._idle_title)
        idle_layout.addSpacing(16)
        idle_layout.addWidget(self._idle_clock)
        self._stack.addWidget(idle_widget)

        root.addWidget(self._stack, stretch=3)

        # RIGHT — now playing + queue
        right = QVBoxLayout()
        right.setSpacing(16)

        now = QFrame()
        now.setObjectName("nowPlaying")
        nl = QVBoxLayout(now)
        self._title_label = QLabel("—")
        self._title_label.setObjectName("title")
        self._title_label.setWordWrap(True)
        self._artist_label = QLabel("")
        self._artist_label.setObjectName("artist")
        self._artist_label.setWordWrap(True)
        self._progress_label = QLabel("0:00 / 0:00")
        self._progress_label.setObjectName("progress")
        nl.addWidget(self._title_label)
        nl.addWidget(self._artist_label)
        nl.addWidget(self._progress_label)
        right.addWidget(now)

        queue_label = QLabel("Prossime tracce")
        queue_label.setStyleSheet("color: #aaa; font-size: 14pt; padding-left: 4px;")
        right.addWidget(queue_label)

        self._queue_list = QListWidget()
        self._queue_list.setSelectionMode(QListWidget.NoSelection)
        self._queue_list.setFocusPolicy(Qt.NoFocus)
        right.addWidget(self._queue_list, stretch=1)

        right_widget = QWidget()
        right_widget.setLayout(right)
        right_widget.setMinimumWidth(420)
        root.addWidget(right_widget, stretch=2)

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
        else:
            self._current_track_id = None
            self._title_label.setText("Nessuna traccia")
            self._artist_label.setText("")
            self._progress_label.setText("0:00 / 0:00")
            self._stack.setCurrentIndex(2)  # idle

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
        if track.type in (TrackType.YOUTUBE, TrackType.SPOTIFY):
            # Default to 16:9 BEFORE VLC starts rendering — covers the vast
            # majority of YouTube content with no visible black bars on first
            # frame. The MediaPlayerVout event corrects the aspect for vertical
            # / 4:3 / square content shortly after.
            self.video_container.set_aspect(16, 9)
            self._stack.setCurrentIndex(0)
            return

        # Audio source — show idle until embedded/remote cover resolves.
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
        """Fired from a VLC thread when video output starts. Reads the source
        dimensions and hops the result to the GUI thread via Signal."""
        try:
            w, h = self._player.vlc_player.video_get_size(0)
        except Exception:
            return
        if w and h:
            self.video_size_received.emit(int(w), int(h))

    # --- Resize handling ---

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-scale current cover on window resize
        if self._stack.currentIndex() == 1 and self._cover_label.pixmap():
            pm = self._cover_label.pixmap()
            self._cover_label.setPixmap(
                pm.scaled(self._cover_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
