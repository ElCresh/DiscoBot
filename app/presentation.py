"""Native fullscreen kiosk presentation window (PySide6).

Layout: header (title/artist/progress + QR) over a row with the video/cover
on the left and the upcoming queue on the right. The window background
adapts to the current cover (blurred art + dominant accent color); when no
track is playing, an idle pane with brand + clock is shown over a gradient.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import qrcode
import vlc
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.coverart import resolve_cover_for
from app.models import Track, TrackType

logger = logging.getLogger(__name__)


DEFAULT_ACCENT = QColor("#e94560")
_QR_SIZE = 160
_THUMB_SIZE = 56
_DISPLAY_FAMILIES = [
    "Segoe UI Variable Display",
    "SF Pro Display",
    "Inter",
    "Helvetica Neue",
    "Arial",
]
_TEXT_FAMILIES = [
    "Segoe UI Variable Text",
    "Segoe UI",
    "Inter",
    "Helvetica Neue",
    "Arial",
]


def _stylesheet(accent: QColor) -> str:
    """QSS rebuilt on every accent change so all accent-tinted widgets refresh."""
    a = accent.name()
    return f"""
QMainWindow {{ background-color: #0c0c18; }}
QWidget {{ color: #f5f5f7; }}
QLabel {{ background-color: transparent; }}

QFrame#header {{
    background-color: rgba(20, 20, 35, 200);
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: 16px;
}}
QLabel#title {{ color: #ffffff; font-size: 34pt; font-weight: 700; letter-spacing: -0.5px; }}
QLabel#artist {{ color: #d8d8e0; font-size: 17pt; font-weight: 400; }}
QLabel#progress {{ color: rgba(255, 255, 255, 180); font-size: 13pt; font-weight: 500; }}
QLabel#qrcode {{ background-color: white; border-radius: 8px; padding: 4px; }}
QLabel#qrCaption {{ color: rgba(255,255,255,150); font-size: 9pt; font-weight: 500; letter-spacing: 1.5px; }}

QLabel#brand {{ color: #ffffff; font-size: 96pt; font-weight: 800; letter-spacing: -2px; }}
QLabel#brandAccent {{ color: {a}; font-size: 96pt; font-weight: 800; letter-spacing: -2px; }}
QLabel#tagline {{ color: rgba(255,255,255,160); font-size: 16pt; font-weight: 400; letter-spacing: 4px; }}
QLabel#clock {{ color: rgba(255,255,255,200); font-size: 64pt; font-weight: 200; }}

QFrame#queuePanel {{
    background-color: rgba(20, 20, 35, 180);
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: 16px;
}}
QLabel#queueHeader {{ color: rgba(255,255,255,170); font-size: 11pt; font-weight: 600; letter-spacing: 2px; }}

QWidget#queueItem {{
    background-color: rgba(255, 255, 255, 8);
    border-radius: 10px;
}}
QLabel#queueTitle {{ color: #ffffff; font-size: 13pt; font-weight: 600; }}
QLabel#queueArtist {{ color: rgba(255,255,255,150); font-size: 10pt; }}
QLabel#queueIndex {{ color: {a}; font-size: 11pt; font-weight: 700; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,40); border-radius: 3px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# --- Cover analysis ---


@dataclass
class CoverPalette:
    """Produced by the worker thread — only QImage/QColor, never QPixmap.
    QPixmap must be constructed on the GUI thread; the slot handles conversion."""
    accent: QColor
    background_image: QImage  # blurred via downscale, sized for full-window paint


def _display_font(point_size: int, weight: int = QFont.DemiBold) -> QFont:
    f = QFont()
    f.setFamilies(_DISPLAY_FAMILIES)
    f.setPointSize(point_size)
    f.setWeight(weight)
    return f


def _text_font(point_size: int, weight: int = QFont.Normal) -> QFont:
    f = QFont()
    f.setFamilies(_TEXT_FAMILIES)
    f.setPointSize(point_size)
    f.setWeight(weight)
    return f


def _extract_accent(img: QImage) -> QColor:
    """Pick the most saturated, mid-bright pixel from a 16x16 downsample.
    A flat average tends toward muddy grey; this gives us a punchier hue."""
    small = img.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    best: QColor | None = None
    best_score = -1
    for y in range(small.height()):
        for x in range(small.width()):
            c = QColor(small.pixel(x, y))
            h, s, v, _ = c.getHsv()
            if v < 70 or v > 235:
                continue
            # Prefer saturated colors but boost mid-bright ones a little.
            score = s * 2 + (255 - abs(v - 180))
            if score > best_score:
                best_score = score
                best = c
    if best is None:
        return DEFAULT_ACCENT
    # Push toward more vivid: lift saturation a bit but keep hue.
    h, s, v, _ = best.getHsv()
    boosted = QColor()
    boosted.setHsv(h, min(255, int(s * 1.1)), max(180, v))
    return boosted


def _make_blurred_background(img: QImage, target_w: int, target_h: int) -> QImage:
    """Downscale-then-upscale trick gives a free, fast Gaussian-ish blur with
    no extra dependencies. Stays as QImage so it can be built off-thread."""
    if target_w <= 0 or target_h <= 0:
        return QImage()
    tiny = img.scaled(40, 40, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    return tiny.scaled(target_w, target_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)


def analyze_cover(path: str, target_size: tuple[int, int]) -> CoverPalette | None:
    """Worker-thread-safe: returns only QImage + QColor. Caller converts to QPixmap."""
    img = QImage(path)
    if img.isNull():
        logger.debug("analyze_cover: QImage null for path=%s", path)
        return None
    accent = _extract_accent(img)
    bg = _make_blurred_background(img, target_size[0], target_size[1])
    logger.debug(
        "analyze_cover: path=%s src=%dx%d accent=%s bg=%dx%d (null=%s)",
        path, img.width(), img.height(), accent.name(),
        bg.width(), bg.height(), bg.isNull(),
    )
    return CoverPalette(accent=accent, background_image=bg)


# --- QR rendering ---


def _resolve_track_url(track: Track) -> str | None:
    """Public URL of the current media, or None for local files."""
    if track.type == TrackType.YOUTUBE or track.type == TrackType.SOUNDCLOUD:
        return track.path or None
    if track.type == TrackType.SPOTIFY:
        return f"https://open.spotify.com/track/{track.path}" if track.path else None
    return None


def make_qr_pixmap(text: str, size: int = _QR_SIZE) -> QPixmap:
    """Render a QR code as a black-on-white QPixmap of exactly `size` px."""
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


_placeholder_cache: dict[int, QPixmap] = {}
_unavailable_qr_cache: dict[int, QPixmap] = {}


def make_app_icon() -> QIcon:
    """Multi-resolution app icon: rounded brand-red tile with a white music
    note glyph. Provides crisp pixmaps for every taskbar/title-bar size Qt
    might request — 16/24/32/48/64/128/256 covers Windows + most desktops."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(DEFAULT_ACCENT)
        radius = max(2, size // 5)
        p.drawRoundedRect(0, 0, size, size, radius, radius)
        f = QFont()
        f.setFamilies(_DISPLAY_FAMILIES)
        f.setPointSize(max(6, int(size * 0.55)))
        f.setWeight(QFont.Black)
        p.setFont(f)
        p.setPen(Qt.white)
        p.drawText(pix.rect(), Qt.AlignCenter, "♪")
        p.end()
        icon.addPixmap(pix)
    return icon


def _make_unavailable_qr_pixmap(size: int) -> QPixmap:
    """A 'barred QR' placeholder shown when the current track has no shareable
    URL (e.g. a local file). Real QR pattern underneath for an authentic look,
    faded with a white overlay, then a red diagonal strike to communicate
    'not scannable'. Cached per size."""
    cached = _unavailable_qr_cache.get(size)
    if cached is not None:
        return cached
    base = make_qr_pixmap("https://discobot.local/unavailable", size=size)
    pix = QPixmap(size, size)
    pix.fill(Qt.white)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, base)
    # Whitewash to desaturate the underlying QR.
    p.fillRect(0, 0, size, size, QColor(255, 255, 255, 140))
    # Red diagonal strike — thick, with rounded caps for a clean "barred" look.
    pen = QPen(QColor(233, 69, 96, 235))
    pen.setWidth(max(4, size // 22))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    margin = size // 9
    p.drawLine(margin, margin, size - margin, size - margin)
    p.end()
    _unavailable_qr_cache[size] = pix
    return pix


def _placeholder_thumb(size: int) -> QPixmap:
    """Generic music-note thumbnail used when a track has no resolvable cover.
    Built lazily on the GUI thread (QPixmap is GUI-only) and cached per size."""
    cached = _placeholder_cache.get(size)
    if cached is not None:
        return cached
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    # Subtle gradient fill — feels more "designed" than a flat tile.
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor(255, 255, 255, 38))
    grad.setColorAt(1, QColor(255, 255, 255, 14))
    p.setBrush(grad)
    p.drawRoundedRect(0, 0, size, size, 8, 8)
    # Music-note glyph centered. Display fonts render ♪ cleanly at any size.
    glyph_font = QFont()
    glyph_font.setFamilies(_DISPLAY_FAMILIES)
    glyph_font.setPointSize(int(size * 0.55))
    glyph_font.setWeight(QFont.DemiBold)
    p.setFont(glyph_font)
    p.setPen(QColor(255, 255, 255, 150))
    p.drawText(pix.rect(), Qt.AlignCenter, "♪")
    p.end()
    _placeholder_cache[size] = pix
    return pix


# --- Custom widgets ---


class BackgroundFrame(QWidget):
    """Window-wide background: blurred cover with dark scrim, or a vertical
    gradient when there's no cover. Caches the scaled pixmap until it changes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source: QPixmap | None = None
        self._scaled_cache: QPixmap | None = None
        self._fallback_top = QColor("#1a1a2e")
        self._fallback_bottom = QColor("#08080f")
        self.setAttribute(Qt.WA_StyledBackground, False)

    def set_background(self, pix: QPixmap | None):
        self._source = pix if pix and not pix.isNull() else None
        self._scaled_cache = None
        self.update()

    def set_fallback_tint(self, color: QColor):
        # Pull the fallback gradient toward the accent for a unified feel.
        h, s, v, _ = color.getHsv()
        top = QColor()
        top.setHsv(h, max(60, min(180, s)), 50)
        bottom = QColor()
        bottom.setHsv(h, max(40, min(120, s)), 18)
        self._fallback_top = top
        self._fallback_bottom = bottom
        if self._source is None:
            self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scaled_cache = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._source is not None:
            # Baseline fill: avoids any 1px gap from KeepAspectRatioByExpanding
            # rounding, and prevents the QMainWindow stylesheet bg from leaking.
            painter.fillRect(self.rect(), Qt.black)
            if self._scaled_cache is None or self._scaled_cache.size() != self.size():
                self._scaled_cache = self._source.scaled(
                    self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
            scaled = self._scaled_cache
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            # Dark scrim — keeps text readable over any cover.
            painter.fillRect(self.rect(), QColor(0, 0, 0, 170))
        else:
            grad = QLinearGradient(0, 0, 0, self.height())
            grad.setColorAt(0, self._fallback_top)
            grad.setColorAt(1, self._fallback_bottom)
            painter.fillRect(self.rect(), grad)


class ProgressBar(QWidget):
    """Thin rounded progress bar painted by hand for full color control."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._progress = 0.0
        self._color = DEFAULT_ACCENT

    def set_progress(self, value: float):
        v = max(0.0, min(1.0, value))
        if abs(v - self._progress) < 1e-3:
            return
        self._progress = v
        self.update()

    def set_color(self, color: QColor):
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        radius = self.height() / 2
        # Track
        painter.setBrush(QColor(255, 255, 255, 45))
        painter.drawRoundedRect(self.rect(), radius, radius)
        # Fill
        if self._progress > 0:
            fw = max(self.height(), int(self.width() * self._progress))
            painter.setBrush(self._color)
            painter.drawRoundedRect(0, 0, fw, self.height(), radius, radius)


class QueueItemWidget(QWidget):
    """One row of the upcoming-queue panel: index + thumbnail + title/artist."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("queueItem")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 12, 8)
        layout.setSpacing(12)

        self._index = QLabel()
        self._index.setObjectName("queueIndex")
        self._index.setFixedWidth(22)
        self._index.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._index)

        self._thumb = QLabel()
        self._thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignCenter)
        # Show the generic music-note tile until/unless a real cover arrives.
        self._thumb.setPixmap(_placeholder_thumb(_THUMB_SIZE))
        layout.addWidget(self._thumb)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel()
        self._title.setObjectName("queueTitle")
        self._title.setWordWrap(False)
        self._artist = QLabel()
        self._artist.setObjectName("queueArtist")
        self._artist.setWordWrap(False)
        text_col.addWidget(self._title)
        text_col.addWidget(self._artist)
        text_col.addStretch(1)
        layout.addLayout(text_col, stretch=1)

    def set_track(self, index: int, track: Track):
        self._index.setText(f"{index:02d}")
        self._title.setText(self._elide(track.title or "—", 36))
        self._artist.setText(self._elide(track.artist or "", 40))

    def set_thumbnail(self, pix: QPixmap):
        if pix.isNull():
            return
        self._thumb.setPixmap(
            pix.scaled(
                _THUMB_SIZE,
                _THUMB_SIZE,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
        )

    @staticmethod
    def _elide(text: str, max_chars: int) -> str:
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


# --- Aspect-preserving video container (unchanged) ---


class AspectVideoContainer(QWidget):
    """Container that keeps the inner video QFrame at the source aspect ratio,
    centered both vertically and horizontally."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_frame = QFrame(self)
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        self.video_frame.setStyleSheet("background-color: black; border-radius: 12px;")
        self._aspect: float | None = None
        self._relayout()

    def set_aspect(self, w: int, h: int):
        self._aspect = (w / h) if (w > 0 and h > 0) else None
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


# --- Main window ---


class PresentationWindow(QMainWindow):
    state_received = Signal(dict)
    video_size_received = Signal(int, int)
    thumb_received = Signal(int, str)  # track_id, image path (QPixmap built in slot)
    cover_ready = Signal(int, object, object)  # track_id, path|None, CoverPalette|None

    def __init__(self, player, monitor: int = 0):
        super().__init__()
        self._player = player
        self._monitor = monitor
        self._current_track_id: int | None = None
        self._cover_thread: threading.Thread | None = None
        self._accent: QColor = DEFAULT_ACCENT
        self._queue_thumbs: dict[int, QPixmap] = {}
        self._last_queue_ids: list[int] = []

        self.setWindowTitle("DiscoBot")
        self.setCursor(Qt.BlankCursor)
        self._apply_accent(DEFAULT_ACCENT)

        self._build_ui()

        # Headless thumbnailer needs an HWND. We use a top-level frame (no
        # parent) so realizing its HWND doesn't cascade to the QMainWindow —
        # otherwise the main window's HWND would be allocated on monitor 0
        # before show_on_target_monitor() can move it to its target screen.
        self._thumb_hwnd_frame: QFrame | None = None
        self._init_video_thumbnailer()

        from app.player import ws_manager

        ws_manager.subscribe_sync(lambda data: self.state_received.emit(data))
        self.state_received.connect(self._on_state)

        events = player.vlc_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerVout, self._on_vout)
        self.video_size_received.connect(self._on_video_ready)

        self.thumb_received.connect(self._on_thumb_ready)
        self.cover_ready.connect(self._apply_cover)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._tick_progress)
        self._progress_timer.start()

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()

        self._on_state(player.get_state().model_dump())

    @property
    def video_frame(self):
        """HWND target for VLC. Stays valid across container resizes."""
        return self.video_container.video_frame

    # --- UI construction ---

    def _build_ui(self):
        self._background = BackgroundFrame()
        root = QVBoxLayout(self._background)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(18)

        # --- HEADER ---
        header = QFrame()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 20, 20, 20)
        hl.setSpacing(24)

        info = QVBoxLayout()
        info.setSpacing(6)
        self._title_label = QLabel("Nessuna traccia")
        self._title_label.setObjectName("title")
        self._title_label.setFont(_display_font(34, QFont.Bold))
        self._title_label.setWordWrap(True)
        self._artist_label = QLabel("")
        self._artist_label.setObjectName("artist")
        self._artist_label.setFont(_text_font(17, QFont.Normal))
        self._artist_label.setWordWrap(True)
        info.addWidget(self._title_label)
        info.addWidget(self._artist_label)
        info.addStretch(1)

        # Progress: bar + time text aligned right
        self._progress_bar = ProgressBar()
        info.addWidget(self._progress_bar)
        self._progress_label = QLabel("0:00 / 0:00")
        self._progress_label.setObjectName("progress")
        self._progress_label.setFont(_text_font(13, QFont.Medium))
        self._progress_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info.addWidget(self._progress_label)

        hl.addLayout(info, stretch=1)

        # QR + caption stacked vertically; caption hides with the QR
        qr_col = QVBoxLayout()
        qr_col.setSpacing(6)
        qr_col.setAlignment(Qt.AlignCenter)
        self._qr_label = QLabel()
        self._qr_label.setObjectName("qrcode")
        self._qr_label.setFixedSize(_QR_SIZE + 8, _QR_SIZE + 8)
        self._qr_label.setAlignment(Qt.AlignCenter)
        sp = self._qr_label.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self._qr_label.setSizePolicy(sp)
        self._qr_label.setVisible(False)
        self._qr_caption = QLabel("APRI BRANO")
        self._qr_caption.setObjectName("qrCaption")
        self._qr_caption.setFont(_text_font(9, QFont.Medium))
        self._qr_caption.setAlignment(Qt.AlignCenter)
        self._qr_caption.setVisible(False)
        qr_col.addWidget(self._qr_label)
        qr_col.addWidget(self._qr_caption)
        hl.addLayout(qr_col)

        header.setMinimumHeight(_QR_SIZE + 40)
        root.addWidget(header)

        # --- MAIN row ---
        main = QHBoxLayout()
        main.setSpacing(18)

        self._stack = QStackedWidget()
        self._stack.setMinimumWidth(800)

        self.video_container = AspectVideoContainer()
        self._stack.addWidget(self.video_container)

        self._cover_label = QLabel()
        self._cover_label.setAlignment(Qt.AlignCenter)
        self._cover_label.setStyleSheet(
            "background-color: transparent; border-radius: 14px;"
        )
        self._stack.addWidget(self._cover_label)

        # Idle pane
        idle_widget = QWidget()
        idle_layout = QVBoxLayout(idle_widget)
        idle_layout.setAlignment(Qt.AlignCenter)
        idle_layout.setSpacing(6)

        # "DiscoBot" with accent on "Bot" for some flair
        brand_row = QHBoxLayout()
        brand_row.setSpacing(0)
        brand_row.setAlignment(Qt.AlignCenter)
        self._idle_brand_a = QLabel("Disco")
        self._idle_brand_a.setObjectName("brand")
        self._idle_brand_a.setFont(_display_font(96, QFont.Black))
        self._idle_brand_b = QLabel("Bot")
        self._idle_brand_b.setObjectName("brandAccent")
        self._idle_brand_b.setFont(_display_font(96, QFont.Black))
        brand_row.addWidget(self._idle_brand_a)
        brand_row.addWidget(self._idle_brand_b)
        idle_layout.addLayout(brand_row)

        self._idle_tagline = QLabel("IN ATTESA DELLA PROSSIMA TRACCIA")
        self._idle_tagline.setObjectName("tagline")
        self._idle_tagline.setFont(_text_font(14, QFont.Medium))
        self._idle_tagline.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(self._idle_tagline)
        idle_layout.addSpacing(40)

        self._idle_clock = QLabel("--:--")
        self._idle_clock.setObjectName("clock")
        self._idle_clock.setFont(_display_font(64, QFont.Light))
        self._idle_clock.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(self._idle_clock)
        self._stack.addWidget(idle_widget)

        main.addWidget(self._stack, stretch=3)

        # Queue panel
        queue_panel = QFrame()
        queue_panel.setObjectName("queuePanel")
        queue_panel.setMinimumWidth(440)
        qpl = QVBoxLayout(queue_panel)
        qpl.setContentsMargins(18, 18, 12, 18)
        qpl.setSpacing(12)

        queue_header = QLabel("PROSSIME TRACCE")
        queue_header.setObjectName("queueHeader")
        queue_header.setFont(_text_font(11, QFont.DemiBold))
        qpl.addWidget(queue_header)

        self._queue_scroll = QScrollArea()
        self._queue_scroll.setWidgetResizable(True)
        self._queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._queue_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._queue_scroll.setFrameShape(QFrame.NoFrame)
        queue_inner = QWidget()
        self._queue_layout = QVBoxLayout(queue_inner)
        self._queue_layout.setContentsMargins(0, 0, 6, 0)
        self._queue_layout.setSpacing(8)
        self._queue_layout.addStretch(1)
        self._queue_scroll.setWidget(queue_inner)
        self._queue_empty = QLabel("La coda è vuota — aggiungi qualcosa!")
        self._queue_empty.setStyleSheet(
            "color: rgba(255,255,255,120); font-size: 11pt; padding: 12px 4px;"
        )
        self._queue_empty.setWordWrap(True)
        self._queue_empty.setVisible(False)
        qpl.addWidget(self._queue_empty)
        qpl.addWidget(self._queue_scroll, stretch=1)

        main.addWidget(queue_panel, stretch=2)

        root.addLayout(main, stretch=1)

        self.setCentralWidget(self._background)

    # --- Display ---

    def show_on_target_monitor(self):
        screens = QApplication.screens()
        if screens:
            target = screens[min(self._monitor, len(screens) - 1)]
            self.setGeometry(target.geometry())
        self.showFullScreen()

    def _init_video_thumbnailer(self):
        if self._thumb_hwnd_frame is not None:
            return
        try:
            # Top-level (no parent): realizing this HWND must not cascade to
            # the main window, otherwise show_on_target_monitor()'s setGeometry
            # gets ignored and the kiosk opens on monitor 0.
            frame = QFrame(None)
            frame.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
            frame.setAttribute(Qt.WA_NativeWindow, True)
            frame.setAttribute(Qt.WA_DontShowOnScreen, True)
            frame.resize(2, 2)
            from app import video_thumb

            video_thumb.initialize(int(frame.winId()))
            self._thumb_hwnd_frame = frame
        except Exception:
            logger.warning("Headless video thumbnailer init failed", exc_info=True)

    # --- Accent / palette plumbing ---

    def _apply_accent(self, accent: QColor):
        self._accent = accent
        self.setStyleSheet(_stylesheet(accent))
        if hasattr(self, "_progress_bar"):
            self._progress_bar.set_color(accent)
        # Note: we deliberately do NOT tint the background fallback here.
        # _apply_accent is also called with DEFAULT_ACCENT (brand red) on
        # idle/no-cover, which would bleed the brand color onto the gradient.
        # The fallback tint is updated only from a real cover-derived accent
        # in _apply_cover, so idle stays neutral until the first cover loads.

    def _on_thumb_ready(self, track_id: int, path: str):
        # QPixmap must be built on the GUI thread — that's here.
        pix = QPixmap(path)
        if pix.isNull():
            logger.debug("_on_thumb_ready: QPixmap null for path=%s", path)
            return
        self._queue_thumbs[track_id] = pix
        applied = 0
        for i in range(self._queue_layout.count() - 1):  # last is stretch
            item = self._queue_layout.itemAt(i).widget()
            if isinstance(item, QueueItemWidget) and item.property("track_id") == track_id:
                item.set_thumbnail(pix)
                applied += 1
        logger.debug(
            "_on_thumb_ready: id=%s path=%s applied_to_rows=%d",
            track_id, path, applied,
        )

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
            self._progress_bar.set_progress(0)
            self._stack.setCurrentIndex(2)  # idle
            self._qr_label.setVisible(False)
            self._qr_caption.setVisible(False)
            self._qr_label.clear()
            self._background.set_background(None)
            self._apply_accent(DEFAULT_ACCENT)

        self._render_queue(queue)

    def _render_queue(self, queue: list[dict]):
        # Empty-state label
        self._queue_empty.setVisible(len(queue) == 0)

        # Clear existing rows (keep the trailing stretch).
        while self._queue_layout.count() > 1:
            item = self._queue_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for idx, entry in enumerate(queue, start=1):
            t = Track(**entry)
            row = QueueItemWidget()
            row.setProperty("track_id", t.id)
            row.set_track(idx, t)
            cached = self._queue_thumbs.get(t.id)
            if cached:
                row.set_thumbnail(cached)
            else:
                self._dispatch_thumb_load(t)
            # Insert above the stretch.
            self._queue_layout.insertWidget(self._queue_layout.count() - 1, row)

        # Drop thumb cache entries that aren't in queue or current.
        keep_ids = {entry["id"] for entry in queue if "id" in entry}
        if self._current_track_id is not None:
            keep_ids.add(self._current_track_id)
        self._queue_thumbs = {k: v for k, v in self._queue_thumbs.items() if k in keep_ids}

    def _dispatch_thumb_load(self, track: Track):
        track_id = track.id
        track_type = track.type
        cover_url = track.cover_url

        def worker():
            path = resolve_cover_for(track)
            logger.debug(
                "_dispatch_thumb_load: id=%s type=%s cover_url=%s -> path=%s",
                track_id, track_type, cover_url, path,
            )
            if not path:
                return
            # Don't touch QPixmap here — emit the path and let the slot build it.
            self.thumb_received.emit(track_id, path)

        threading.Thread(target=worker, daemon=True).start()

    def _switch_media_view(self, track: Track):
        self._cover_label.clear()
        # Default to idle until cover or video resolves; avoids black flash.
        self._stack.setCurrentIndex(2)

        track_id = track.id
        target_size = (max(800, self.width()), max(600, self.height()))
        logger.debug(
            "_switch_media_view: id=%s type=%s cover_url=%s target_size=%s",
            track_id, track.type, track.cover_url, target_size,
        )

        def worker():
            path = resolve_cover_for(track)
            stale = track_id != self._current_track_id
            logger.debug(
                "_switch_media_view worker: id=%s -> path=%s stale=%s",
                track_id, path, stale,
            )
            if stale:
                return
            palette = analyze_cover(path, target_size) if path else None
            # Cross-thread dispatch: emit a signal so the slot runs on the GUI
            # thread (Qt.AutoConnection auto-queues across threads). QTimer
            # from a worker thread doesn't work — the worker has no event loop.
            self.cover_ready.emit(track_id, path, palette)

        self._cover_thread = threading.Thread(target=worker, daemon=True)
        self._cover_thread.start()

    def _update_qr(self, track: Track):
        url = _resolve_track_url(track)
        if not url:
            # No public URL (e.g. local file) — show a barred QR placeholder
            # so the slot stays filled and the user understands "no link".
            self._qr_label.setPixmap(_make_unavailable_qr_pixmap(_QR_SIZE))
            self._qr_caption.setText("NON DISPONIBILE")
            self._qr_label.setVisible(True)
            self._qr_caption.setVisible(True)
            return
        try:
            pix = make_qr_pixmap(url, size=_QR_SIZE)
        except Exception:
            logger.warning("QR generation failed for %s", url, exc_info=True)
            self._qr_label.setPixmap(_make_unavailable_qr_pixmap(_QR_SIZE))
            self._qr_caption.setText("NON DISPONIBILE")
            self._qr_label.setVisible(True)
            self._qr_caption.setVisible(True)
            return
        self._qr_label.setPixmap(pix)
        self._qr_caption.setText("APRI BRANO")
        self._qr_label.setVisible(True)
        self._qr_caption.setVisible(True)

    def _apply_cover(self, track_id: int, path: str | None, palette):
        # Runs on the GUI thread (dispatched via QTimer.singleShot from worker),
        # so this is the right place to construct any QPixmap.
        if track_id != self._current_track_id:
            return

        if palette is not None:
            bg_pix = QPixmap.fromImage(palette.background_image)
            # Background first, then style. _apply_accent calls setStyleSheet
            # which re-polishes the whole widget tree; doing background work
            # before that avoids any chance the polish triggers a paint while
            # _source is still stale.
            self._background.set_background(bg_pix)
            self._background.set_fallback_tint(palette.accent)
            self._apply_accent(palette.accent)
            logger.debug(
                "_apply_cover: track_id=%s path=%s accent=%s bg_set=%s",
                track_id, path, palette.accent.name(),
                self._background._source is not None,
            )
        else:
            self._background.set_background(None)
            self._apply_accent(DEFAULT_ACCENT)
            logger.debug(
                "_apply_cover: track_id=%s path=%s palette=None (fallback)",
                track_id, path,
            )

        if not path or not Path(path).exists():
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        self._queue_thumbs[track_id] = pix
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
        pos = state.position or 0.0
        dur = state.duration or 0.0
        self._progress_label.setText(f"{_format_time(pos)} / {_format_time(dur)}")
        self._progress_bar.set_progress((pos / dur) if dur > 0 else 0.0)

    def _tick_clock(self):
        self._idle_clock.setText(datetime.now().strftime("%H:%M"))

    # --- VLC vout handler ---

    def _on_vout(self, event):
        try:
            w, h = self._player.vlc_player.video_get_size(0)
        except Exception:
            return
        if w and h:
            self.video_size_received.emit(int(w), int(h))

    def _on_video_ready(self, w: int, h: int):
        self.video_container.set_aspect(w, h)
        self._stack.setCurrentIndex(0)

    # --- Resize handling ---

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._stack.currentIndex() == 1 and self._cover_label.pixmap():
            pm = self._cover_label.pixmap()
            self._cover_label.setPixmap(
                pm.scaled(
                    self._cover_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
