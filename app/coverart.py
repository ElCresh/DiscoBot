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
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    target = CACHE_DIR / f"remote_{_cache_key(url)}.img"
    if target.exists():
        target.touch()
        return str(target)

    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "DiscoBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        target.write_bytes(data)
        _enforce_cache_limit()
        return str(target)
    except Exception:
        logger.warning("Failed to download cover %s", url, exc_info=True)
        return None


def resolve_cover_for(track) -> str | None:
    """Return a local image path for the given track, or None if no cover is available."""
    from app.models import TrackType

    if track is None:
        return None
    if track.type == TrackType.LOCAL:
        return extract_local_cover(track.path)
    return download_remote_cover(track.cover_url) if track.cover_url else None
