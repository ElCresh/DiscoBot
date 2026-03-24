"""REST API routes for remote control."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.models import TrackRequest, Track, PlayerState, HistoryEntry
from app.player import AudioPlayer

app = FastAPI(title="DiscoBot", version="1.0.0")

cors_origins = settings.cors_origins.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

player = AudioPlayer()

MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)
MEDIA_EXTENSIONS = {
    # Audio
    ".mp3", ".wav", ".flac", ".ogg", ".aac", ".wma", ".midi", ".mid",
    # Video (riprodotti solo audio)
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".wmv", ".flv",
}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


# --- Player controls ---

@app.post("/player/play")
def play():
    """Resume playback."""
    player.play()
    return {"status": "playing"}


@app.post("/player/pause")
def pause():
    """Pause playback."""
    player.pause()
    return {"status": "paused"}


@app.post("/player/stop")
def stop():
    """Stop playback."""
    player.stop()
    return {"status": "stopped"}


@app.post("/player/skip")
def skip():
    """Skip to next track."""
    player.skip()
    return {"status": "skipped"}


@app.post("/player/previous")
def previous():
    """Restart current track."""
    player.previous()
    return {"status": "restarted"}


@app.post("/player/volume")
def set_volume(volume: int):
    """Set volume (0-100)."""
    if not 0 <= volume <= 100:
        raise HTTPException(status_code=400, detail="Volume must be between 0 and 100")
    player.set_volume(volume)
    return {"volume": volume}


@app.post("/player/seek")
def seek(position: float):
    """Seek to position in seconds."""
    if position < 0:
        raise HTTPException(status_code=400, detail="Position must be >= 0")
    player.seek(position)
    return {"position": position}


@app.get("/player/state", response_model=PlayerState)
def get_state():
    """Get current player state."""
    return player.get_state()


# --- Queue management ---

@app.post("/queue/add", response_model=Track)
def add_track(request: TrackRequest):
    """Add a track to the queue."""
    try:
        return player.add_track(request.path, request.type)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/queue/{track_id}")
def remove_track(track_id: int):
    """Remove a track from the queue."""
    if not player.remove_track(track_id):
        raise HTTPException(status_code=404, detail="Track not found in queue")
    return {"status": "removed"}


@app.post("/queue/{track_id}/move")
def move_track(track_id: int, position: int):
    """Move a track to a new position in the queue."""
    if not player.move_track(track_id, position):
        raise HTTPException(status_code=404, detail="Track not found in queue")
    return {"status": "moved"}


@app.delete("/queue")
def clear_queue():
    """Clear the entire queue."""
    player.clear_queue()
    return {"status": "cleared"}


# --- History ---

@app.get("/history")
def get_history():
    """Get playback history."""
    return {"history": player.get_history()}


@app.post("/history/{index}/requeue", response_model=Track)
def requeue_history(index: int):
    """Re-add a track from history to the queue."""
    try:
        return player.requeue_from_history(index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Invalid history index")


@app.delete("/history")
def clear_history():
    """Clear playback history."""
    player.clear_history()
    return {"status": "cleared"}


# --- Media library ---

@app.get("/media/list")
def list_media():
    """List audio files in the media directory."""
    files = sorted(
        f.name for f in MEDIA_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS
    )
    return {"files": files}


@app.post("/media/upload")
async def upload_media(file: UploadFile = File(...)):
    """Upload an audio file to the media directory."""
    filename = Path(file.filename).name  # strip any directory components
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not any(filename.lower().endswith(ext) for ext in MEDIA_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Unsupported audio format")
    dest = MEDIA_DIR / filename
    total_size = 0
    chunks = []
    while chunk := await file.read(1024 * 1024):  # 1 MB chunks
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 100 MB)")
        chunks.append(chunk)
    dest.write_bytes(b"".join(chunks))
    return {"filename": filename}


@app.post("/queue/add-media/{filename}")
def add_media_to_queue(filename: str):
    """Add a file from the media library to the queue."""
    # Sanitize: only allow a plain filename, no path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = (MEDIA_DIR / filename).resolve()
    if not filepath.is_relative_to(MEDIA_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found in media library")
    try:
        return player.add_track(str(filepath), "local")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Spotify ---

@app.get("/spotify/status")
def spotify_status():
    """Check if Spotify integration is configured."""
    configured = bool(settings.spotify_client_id and settings.spotify_client_secret)
    return {"configured": configured}


@app.get("/spotify/search")
def spotify_search(q: str, limit: int = 10):
    """Search Spotify for tracks."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    from app.spotify import search_tracks

    try:
        results = search_tracks(q, min(limit, 50))
        return {"results": results}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- Web UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_ui():
    """Serve the web UI."""
    return FileResponse("static/index.html")
