from pydantic import BaseModel
from enum import Enum


class TrackType(str, Enum):
    LOCAL = "local"
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


class TrackRequest(BaseModel):
    path: str  # file path or YouTube URL
    type: TrackType = TrackType.LOCAL


class Track(BaseModel):
    id: int
    path: str
    title: str
    type: TrackType
    duration: float | None = None  # seconds
    artist: str | None = None
    album: str | None = None


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


class PlaylistInfo(BaseModel):
    name: str
    track_count: int


class PlaylistDetail(BaseModel):
    name: str
    tracks: list[Track]


class PlayerState(BaseModel):
    current_track: Track | None = None
    queue: list[Track] = []
    is_playing: bool = False
    volume: int = 80  # 0-100
    position: float = 0.0  # seconds
    duration: float = 0.0  # seconds
    shuffle: bool = False
    repeat: RepeatMode = RepeatMode.OFF
