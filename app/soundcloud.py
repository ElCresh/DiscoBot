"""SoundCloud audio extraction and search via yt-dlp."""

import logging

logger = logging.getLogger(__name__)


def extract_soundcloud_metadata(url: str) -> tuple[str, float | None]:
    """Extract title and duration from a SoundCloud URL.

    Returns (title, duration_in_seconds).
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            uploader = info.get("uploader")
            if uploader:
                title = f"{uploader} - {title}"
            duration = info.get("duration")
            return title, float(duration) if duration else None
    except Exception:
        logger.warning(f"Failed to extract SoundCloud metadata: {url}")
        return "Unknown", None


def extract_audio_url(url: str) -> tuple[str, str, float | None]:
    """Extract playable audio URL from a SoundCloud link.

    Returns (audio_url, title, duration_in_seconds).
    """
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]
        title = info.get("title", "Unknown")
        uploader = info.get("uploader")
        if uploader:
            title = f"{uploader} - {title}"
        duration = info.get("duration")
        return audio_url, title, float(duration) if duration else None


def search_soundcloud(query: str, limit: int = 10) -> list[dict]:
    """Search SoundCloud for tracks using yt-dlp.

    Returns list of dicts with: url, title, artist, duration_ms.
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
            entries = list(info.get("entries", [])) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get("webpage_url") or entry.get("url", "")
                title = entry.get("title", "Unknown")
                artist = entry.get("uploader", "")
                duration = entry.get("duration")
                results.append({
                    "url": url,
                    "title": title,
                    "artist": artist,
                    "duration_ms": int(duration * 1000) if duration else 0,
                })
    except Exception:
        logger.exception("SoundCloud search failed")
    return results
