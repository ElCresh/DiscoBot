"""YouTube audio URL extraction with yt-dlp + pytubefix fallback."""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def validate_youtube_url(url: str) -> None:
    """Validate that the URL is a legitimate YouTube URL.

    Raises ValueError if the URL scheme or hostname is not allowed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise ValueError(f"Not a YouTube URL: {parsed.hostname!r}")


def extract_with_ytdlp(url: str) -> tuple[str, str, float | None]:
    """Extract audio URL using yt-dlp. Returns (audio_url, title, duration)."""
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
        duration = info.get("duration")
        return audio_url, title, duration


def extract_with_pytubefix(url: str) -> tuple[str, str, float | None]:
    """Extract audio URL using pytubefix as fallback. Returns (audio_url, title, duration)."""
    from pytubefix import YouTube

    yt = YouTube(url)
    stream = yt.streams.get_audio_only()
    if not stream:
        raise RuntimeError("No audio stream found")
    title = yt.title or "Unknown"
    duration = yt.length
    return stream.url, title, float(duration) if duration else None


def extract_youtube_metadata(url: str) -> tuple[str, float | None]:
    """Extract title and duration from a YouTube URL without resolving the audio stream URL.

    Returns (title, duration).
    """
    validate_youtube_url(url)
    try:
        _, title, duration = extract_with_ytdlp(url)
        return title, duration
    except Exception:
        pass
    try:
        _, title, duration = extract_with_pytubefix(url)
        return title, duration
    except Exception:
        return "Unknown", None


def search_youtube_audio(query: str) -> tuple[str, str, float | None]:
    """Search YouTube by text query and extract audio URL for the top result.

    Returns (audio_url, title, duration_in_seconds).
    """
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "default_search": "ytsearch1",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        audio_url = info["url"]
        title = info.get("title", "Unknown")
        duration = info.get("duration")
        return audio_url, title, duration


def search_youtube(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube for tracks. Returns list of dicts with url, title, artist, duration_ms."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = list(info.get("entries", [])) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get("url") or entry.get("webpage_url", "")
                # Ensure we have a proper YouTube URL
                vid_id = entry.get("id", "")
                if vid_id and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                results.append({
                    "url": url,
                    "title": entry.get("title", "Unknown"),
                    "artist": entry.get("uploader") or entry.get("channel", ""),
                    "duration_ms": int(entry["duration"] * 1000) if entry.get("duration") else 0,
                })
    except Exception:
        logger.exception("YouTube search failed")
    return results


def extract_audio_url(url: str) -> tuple[str, str, float | None]:
    """Extract audio URL from YouTube link. Tries yt-dlp first, falls back to pytubefix.

    Returns:
        tuple of (audio_url, title, duration_in_seconds)
    """
    validate_youtube_url(url)
    try:
        result = extract_with_ytdlp(url)
        logger.info("YouTube URL resolved via yt-dlp")
        return result
    except Exception as e:
        logger.warning(f"yt-dlp failed: {e}, trying pytubefix...")

    try:
        result = extract_with_pytubefix(url)
        logger.info("YouTube URL resolved via pytubefix")
        return result
    except Exception as e:
        logger.error(f"pytubefix also failed: {e}")
        raise RuntimeError(f"Cannot extract audio from YouTube URL: {url}") from e
