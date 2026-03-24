from pydantic import BaseModel
from enum import Enum


class TrackType(str, Enum):
    LOCAL = "local"
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"


class TrackRequest(BaseModel):
    path: str  # file path or YouTube URL
    type: TrackType = TrackType.LOCAL


class Track(BaseModel):
    id: int
    path: str
    title: str
    type: TrackType
    duration: float | None = None  # seconds


class HistoryEntry(BaseModel):
    track: Track
    played_at: str  # ISO 8601


class SpotifySearchResult(BaseModel):
    spotify_id: str
    title: str
    artist: str
    album: str
    duration_ms: int
    album_art_url: str | None = None


class PlayerState(BaseModel):
    current_track: Track | None = None
    queue: list[Track] = []
    is_playing: bool = False
    volume: int = 80  # 0-100
    position: float = 0.0  # seconds
    duration: float = 0.0  # seconds
