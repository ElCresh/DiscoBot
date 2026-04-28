"""Cover-art resolution: embedded ID3 art for local files, URLs for remote sources."""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path("coverart_cache")
CACHE_LIMIT = 200  # max files retained in the local cache


def _cache_key(payload: str) -> str:
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def _enforce_cache_limit() -> None:
    try:
        files = sorted(CACHE_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
    except FileNotFoundError:
        return
    while len(files) > CACHE_LIMIT:
        try:
            files.pop(0).unlink()
        except OSError:
            break


def extract_local_cover(filepath: str) -> str | None:
    """Read embedded album art from a local audio file. Returns cached image path or None."""
    try:
        from tinytag import TinyTag

        tag = TinyTag.get(filepath, image=True)
        images = getattr(tag, "images", None)
        front = None
        if images is not None:
            front = getattr(images, "front_cover", None) or (
                images.any if hasattr(images, "any") else None
            )
        data = getattr(front, "data", None) if front else None
        if not data:
            return None

        CACHE_DIR.mkdir(exist_ok=True)
        target = CACHE_DIR / f"local_{_cache_key(filepath)}.img"
        target.write_bytes(data)
        target.touch()
        _enforce_cache_limit()
        return str(target)
    except Exception:
        logger.debug("No embedded cover for %s", filepath, exc_info=True)
        return None


def download_remote_cover(url: str) -> str | None:
    """Download a remote cover image to the cache. Returns local path or None on failure."""
    if not url:
        logger.debug("download_remote_cover: empty url")
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    target = CACHE_DIR / f"remote_{_cache_key(url)}.img"
    if target.exists():
        target.touch()
        logger.debug("download_remote_cover: cache hit %s -> %s", url, target.name)
        return str(target)

    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "DiscoBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        target.write_bytes(data)
        _enforce_cache_limit()
        logger.debug(
            "download_remote_cover: downloaded %s -> %s (%d bytes)",
            url, target.name, len(data),
        )
        return str(target)
    except Exception:
        logger.warning("Failed to download cover %s", url, exc_info=True)
        return None


def clear_cache() -> int:
    """Wipe all cached cover images. Returns number of files removed."""
    if not CACHE_DIR.is_dir():
        return 0
    removed = 0
    for f in CACHE_DIR.iterdir():
        try:
            f.unlink()
            removed += 1
        except OSError:
            logger.warning("Failed to remove cache file %s", f, exc_info=True)
    return removed


def cache_filename_for(track) -> str | None:
    """Cache filename a track's cover would resolve to, or None if it has none.
    Mirrors the naming scheme of extract_local_cover / download_remote_cover /
    video_thumb.extract_thumbnail. Returns the embedded-art name even for video
    files — `prune_orphans` accepts a set, so callers can add the video name too."""
    from app.models import TrackType

    if track is None:
        return None
    if track.type == TrackType.LOCAL:
        return f"local_{_cache_key(track.path)}.img"
    if track.cover_url:
        return f"remote_{_cache_key(track.cover_url)}.img"
    return None


def video_cache_filename_for(track) -> str | None:
    """Cache filename of a video-frame thumbnail, if applicable."""
    from app.models import TrackType

    if track is None or track.type != TrackType.LOCAL:
        return None
    from app.video_thumb import is_video

    if not is_video(track.path):
        return None
    return f"video_{_cache_key(track.path)}.png"


def prune_orphans(keep: set[str]) -> int:
    """Delete cached files whose basename isn't in `keep`. Returns count removed."""
    if not CACHE_DIR.is_dir():
        return 0
    removed = 0
    for f in CACHE_DIR.iterdir():
        if f.name in keep:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            logger.warning("Failed to remove orphan cache file %s", f, exc_info=True)
    return removed


def resolve_cover_for(track) -> str | None:
    """Return a local image path for the given track, or None if no cover is available.

    For LOCAL tracks: try embedded ID3 art first; if none and the file is a
    video, fall back to a libvlc-extracted frame thumbnail."""
    from app.models import TrackType

    if track is None:
        return None
    if track.type == TrackType.LOCAL:
        embedded = extract_local_cover(track.path)
        if embedded:
            logger.debug("resolve_cover_for: id=%s LOCAL embedded -> %s", track.id, embedded)
            return embedded
        from app.video_thumb import is_video, extract_thumbnail
        if is_video(track.path):
            snap = extract_thumbnail(track.path)
            logger.debug("resolve_cover_for: id=%s LOCAL video snapshot -> %s", track.id, snap)
            return snap
        logger.debug("resolve_cover_for: id=%s LOCAL audio with no embedded art", track.id)
        return None
    if not track.cover_url:
        logger.debug("resolve_cover_for: id=%s type=%s NO cover_url", track.id, track.type)
        return None
    path = download_remote_cover(track.cover_url)
    logger.debug(
        "resolve_cover_for: id=%s type=%s remote -> %s", track.id, track.type, path,
    )
    return path
