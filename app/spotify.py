"""Spotify search and metadata extraction."""

import logging

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from app.config import settings

logger = logging.getLogger(__name__)

_client: spotipy.Spotify | None = None


def get_client() -> spotipy.Spotify:
    """Lazy-initialize and return the Spotify client."""
    global _client
    if _client is None:
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            raise RuntimeError("Spotify credentials not configured")
        _client = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=settings.spotify_client_id,
                client_secret=settings.spotify_client_secret,
            )
        )
    return _client


def search_tracks(query: str, limit: int = 10) -> list[dict]:
    """Search Spotify for tracks. Returns list of result dicts."""
    sp = get_client()
    results = sp.search(q=query, type="track", limit=limit)
    tracks = []
    for item in results["tracks"]["items"]:
        artists = ", ".join(a["name"] for a in item["artists"])
        tracks.append({
            "spotify_id": item["id"],
            "title": item["name"],
            "artist": artists,
            "album": item["album"]["name"],
            "duration_ms": item["duration_ms"],
            "album_art_url": (
                item["album"]["images"][0]["url"]
                if item["album"]["images"]
                else None
            ),
        })
    return tracks


def extract_spotify_metadata(spotify_id: str) -> tuple[str, float | None, str | None]:
    """Extract display title, duration, and album art URL from a Spotify track ID.

    Returns (display_title, duration_in_seconds, album_art_url).
    """
    sp = get_client()
    track = sp.track(spotify_id)
    artists = ", ".join(a["name"] for a in track["artists"])
    title = track["name"]
    duration = track["duration_ms"] / 1000.0
    images = track["album"].get("images") or []
    cover = images[0]["url"] if images else None
    return f"{artists} - {title}", duration, cover


def build_youtube_search_query(spotify_id: str) -> tuple[str, str, float | None, str | None]:
    """Given a Spotify track ID, return a YouTube search query and metadata.

    Returns (youtube_search_query, display_title, duration_in_seconds, album_art_url).
    """
    sp = get_client()
    track = sp.track(spotify_id)
    artists = ", ".join(a["name"] for a in track["artists"])
    title = track["name"]
    duration = track["duration_ms"] / 1000.0
    display_title = f"{artists} - {title}"
    search_query = f"{artists} - {title}"
    images = track["album"].get("images") or []
    cover = images[0]["url"] if images else None
    return search_query, display_title, duration, cover
