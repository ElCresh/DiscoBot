"""Headless VLC-based thumbnail extractor for local video files.

Uses a dedicated `vlc.Instance` + `MediaPlayer` rendering to an offscreen
HWND so we can grab a single frame without disturbing the main player.
`initialize(hwnd)` must be called once from the GUI thread (Qt allocates
the HWND); after that, `extract_thumbnail()` is safe to call from worker
threads — calls are serialized through a lock since the headless player
is a single shared resource.
"""

import logging
import threading
import time
from pathlib import Path

import vlc

from app.coverart import CACHE_DIR, _cache_key, _enforce_cache_limit

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".m4v", ".flv", ".ts"}

_instance: vlc.Instance | None = None
_player: vlc.MediaPlayer | None = None
_lock = threading.Lock()


def initialize(hwnd: int) -> None:
    """Wire up the headless player. Idempotent; safe to call once at startup."""
    global _instance, _player
    if _instance is not None:
        return
    try:
        _instance = vlc.Instance(["--no-audio", "--quiet", "--no-video-title-show"])
        _player = _instance.media_player_new()
        _player.set_hwnd(hwnd)
    except Exception:
        logger.warning("Failed to initialize headless thumbnail player", exc_info=True)
        _instance = None
        _player = None


def is_video(filepath: str) -> bool:
    return Path(filepath).suffix.lower() in _VIDEO_EXTS


def cache_path_for(filepath: str) -> Path:
    return CACHE_DIR / f"video_{_cache_key(filepath)}.png"


def extract_thumbnail(filepath: str, *, timestamp_pct: float = 0.1) -> str | None:
    """Take a single snapshot ~`timestamp_pct` into the file. Blocks ~1-2s.
    Returns the cached PNG path, or None if extraction failed."""
    if _player is None or _instance is None:
        return None
    if not is_video(filepath):
        return None

    target = cache_path_for(filepath)
    if target.exists():
        target.touch()
        return str(target)
    CACHE_DIR.mkdir(exist_ok=True)

    with _lock:
        try:
            media = _instance.media_new(filepath)
            _player.set_media(media)
            _player.play()

            # Wait for the player to actually start — VLC needs time to
            # parse the container, fire up the decoder, and produce a vout.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                state = _player.get_state()
                if state == vlc.State.Playing:
                    break
                if state in (vlc.State.Error, vlc.State.Ended):
                    _player.stop()
                    return None
                time.sleep(0.05)
            else:
                _player.stop()
                return None

            # Length isn't always known instantly; wait briefly.
            length = 0
            deadline = time.time() + 1.0
            while time.time() < deadline:
                length = _player.get_length()
                if length > 0:
                    break
                time.sleep(0.05)

            if length > 0:
                _player.set_time(int(length * timestamp_pct))
                # Give the decoder time to render the seeked frame.
                time.sleep(0.45)
            else:
                # Unknown length (live/streaming) — just grab whatever's there.
                time.sleep(0.25)

            # width=320, height=0 → preserve aspect, scale to 320 wide.
            result = _player.video_take_snapshot(0, str(target), 320, 0)
            _player.stop()

            if result == 0 and target.exists():
                _enforce_cache_limit()
                return str(target)
            return None
        except Exception:
            logger.warning(
                "Video thumbnail extraction failed for %s", filepath, exc_info=True
            )
            try:
                _player.stop()
            except Exception:
                pass
            return None
