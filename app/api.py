"""REST API routes for remote control."""

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.models import TrackRequest, Track, PlayerState, HistoryEntry, RepeatMode
from app.player import AudioPlayer, ws_manager

app = FastAPI(title="DiscoBot", version="2.0.0")

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


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time state updates."""
    await ws.accept()
    queue = ws_manager.subscribe()
    try:
        # Send initial state
        state = player.get_state()
        await ws.send_json(state.model_dump())
        while True:
            data = await queue.get()
            await ws.send_json(data)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ws_manager.unsubscribe(queue)


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


@app.post("/player/shuffle")
def set_shuffle(enabled: bool):
    """Toggle shuffle mode."""
    player.set_shuffle(enabled)
    return {"shuffle": enabled}


@app.post("/player/repeat")
def set_repeat(mode: RepeatMode):
    """Set repeat mode (off, one, all)."""
    player.set_repeat(mode)
    return {"repeat": mode}


@app.post("/player/normalize")
def set_normalize(enabled: bool):
    """Toggle audio auto-leveling between sources. Applies from the next track."""
    player.set_normalize(enabled)
    return {"normalize": enabled}


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


# --- Cover art cache ---

@app.delete("/coverart_cache")
def clear_coverart_cache():
    """Clear all cached cover images. They will be re-fetched on next play."""
    from app.coverart import clear_cache
    removed = clear_cache()
    return {"status": "cleared", "removed": removed}


# --- History ---

@app.get("/history")
def get_history(offset: int = 0, limit: int = 15):
    """Get a paginated slice of playback history.

    Returns {items, total, offset, limit}. Indices in items are relative to
    the slice; for requeue/remove use offset + local index as the global index.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    items, total = player.get_history(offset=offset, limit=limit)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@app.post("/history/{index}/requeue", response_model=Track)
def requeue_history(index: int):
    """Re-add a track from history to the queue."""
    try:
        return player.requeue_from_history(index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Invalid history index")


@app.delete("/history/{index}")
def remove_history_entry(index: int):
    """Remove a single entry from playback history."""
    try:
        player.remove_history_entry(index)
        return {"status": "removed"}
    except IndexError:
        raise HTTPException(status_code=404, detail="Invalid history index")


@app.delete("/history")
def clear_history():
    """Clear playback history."""
    player.clear_history()
    return {"status": "cleared"}


# --- Media library ---

def _resolve_media_path(rel: str, must_be_dir: bool = False) -> Path:
    """Resolve a relative path inside MEDIA_DIR safely. Raises HTTP 400/404."""
    base = MEDIA_DIR.resolve()
    rel = (rel or "").strip().lstrip("/").lstrip("\\")
    target = (base / rel).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if must_be_dir and not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    return target


def _parent_path(rel: str) -> str | None:
    """Parent of a relative media path. None if already at root."""
    rel = (rel or "").strip().strip("/").strip("\\")
    if not rel:
        return None
    parent = Path(rel).parent.as_posix()
    return "" if parent == "." else parent


@app.get("/media/list")
def list_media(path: str = ""):
    """Browse a directory inside the media library.

    Returns the directory's `dirs` and supported `files` (extension-filtered),
    plus the current `path` and the `parent` path (None at root).
    """
    target = _resolve_media_path(path, must_be_dir=True)
    dirs: list[str] = []
    files: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file() and entry.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(entry.name)
    return {
        "path": path or "",
        "parent": _parent_path(path),
        "dirs": dirs,
        "files": files,
    }


@app.post("/media/upload")
async def upload_media(file: UploadFile = File(...), path: str = ""):
    """Upload an audio file into a (sub)directory of the media library."""
    filename = Path(file.filename).name  # strip any directory components
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not any(filename.lower().endswith(ext) for ext in MEDIA_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Unsupported audio format")
    target_dir = _resolve_media_path(path, must_be_dir=True)
    dest = target_dir / filename
    total_size = 0
    chunks = []
    while chunk := await file.read(1024 * 1024):  # 1 MB chunks
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 100 MB)")
        chunks.append(chunk)
    dest.write_bytes(b"".join(chunks))
    return {"filename": filename, "path": path or ""}


@app.post("/queue/add-media")
def add_media_to_queue(path: str):
    """Add a file from the media library to the queue. `path` is relative to MEDIA_DIR."""
    target = _resolve_media_path(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in media library")
    try:
        return player.add_track(str(target), "local")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Playlists ---

@app.get("/playlists")
def list_playlists():
    """List all saved playlists."""
    return {"playlists": player.list_playlists()}


@app.post("/playlists/save")
def save_playlist(name: str):
    """Save current queue as a named playlist."""
    try:
        count = player.save_playlist(name)
        return {"name": name, "track_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/playlists/{name}/load")
def load_playlist(name: str, replace: bool = False):
    """Load a playlist into the queue."""
    try:
        count = player.load_playlist(name, replace=replace)
        return {"name": name, "tracks_added": count}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Playlist not found")


@app.get("/playlists/{name}")
def get_playlist(name: str):
    """Get playlist details."""
    try:
        return player.get_playlist(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Playlist not found")


@app.delete("/playlists/{name}")
def delete_playlist(name: str):
    """Delete a saved playlist."""
    try:
        player.delete_playlist(name)
        return {"status": "deleted"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Playlist not found")


# --- YouTube ---

@app.get("/youtube/search")
def youtube_search(q: str, limit: int = 10):
    """Search YouTube for tracks."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    from app.youtube import search_youtube

    try:
        results = search_youtube(q, min(limit, 20))
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- Unified search ---

@app.get("/search")
async def unified_search(q: str, limit: int = 5):
    """Search all sources in parallel and return grouped results."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    query = q.strip()
    cap = min(limit, 20)

    async def _search_local():
        lowq = query.lower()
        try:
            matches = []
            for f in MEDIA_DIR.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                if lowq not in f.name.lower():
                    continue
                rel = f.relative_to(MEDIA_DIR).as_posix()
                matches.append({"filename": rel, "title": f.stem})
                if len(matches) >= cap:
                    break
            return matches
        except Exception:
            return []

    async def _search_youtube():
        from app.youtube import search_youtube
        try:
            return await asyncio.to_thread(search_youtube, query, cap)
        except Exception:
            return []

    async def _search_spotify():
        if not (settings.spotify_client_id and settings.spotify_client_secret):
            return []
        from app.spotify import search_tracks
        try:
            return await asyncio.to_thread(search_tracks, query, cap)
        except Exception:
            return []

    async def _search_soundcloud():
        from app.soundcloud import search_soundcloud
        try:
            return await asyncio.to_thread(search_soundcloud, query, cap)
        except Exception:
            return []

    local, youtube, spotify, soundcloud = await asyncio.gather(
        _search_local(), _search_youtube(), _search_spotify(), _search_soundcloud()
    )

    return {
        "local": local,
        "youtube": youtube,
        "spotify": spotify,
        "soundcloud": soundcloud,
    }


# --- SoundCloud ---

@app.get("/soundcloud/search")
def soundcloud_search(q: str, limit: int = 10):
    """Search SoundCloud for tracks."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    from app.soundcloud import search_soundcloud

    try:
        results = search_soundcloud(q, min(limit, 50))
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


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
