"""REST API routes for remote control."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.models import TrackRequest, Track, PlayerState
from app.player import AudioPlayer

app = FastAPI(title="DiscoBot", version="1.0.0")

cors_origins = os.environ.get(
    "DISCOBOT_CORS_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

player = AudioPlayer()

MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".midi", ".mid", ".aac", ".wma"}
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


# --- Media library ---

@app.get("/media/list")
def list_media():
    """List audio files in the media directory."""
    files = sorted(
        f.name for f in MEDIA_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )
    return {"files": files}


@app.post("/media/upload")
async def upload_media(file: UploadFile = File(...)):
    """Upload an audio file to the media directory."""
    filename = Path(file.filename).name  # strip any directory components
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not any(filename.lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
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


# --- Web UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_ui():
    """Serve the web UI."""
    return FileResponse("static/index.html")
