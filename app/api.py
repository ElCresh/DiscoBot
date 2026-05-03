"""REST API routes for remote control."""

import asyncio
from pathlib import Path

import uuid as _uuid

from fastapi import (
    FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect,
    Request, Response, Cookie,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from app.auth import COOKIE_NAME as MANAGER_COOKIE, get_auth, MIN_PASSWORD_LEN
from app.config import settings
from app.models import TrackRequest, Track, PlayerState, HistoryEntry, RepeatMode
from app.player import AudioPlayer, ws_manager
from app.runtime_config import get_runtime_config
from app.tunnel import get_tunnel

app = FastAPI(title="DiscoBot", version="2.0.0")

cors_origins = settings.cors_origins.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Manager auth middleware: protegge tutte le route di controllo.
# Quando manager_auth_enabled è False (toggle runtime), il middleware bypassa
# senza fare nulla — la sezione manager torna libera (uso domestico fidato).
MANAGER_PREFIXES = (
    "/m", "/admin", "/pending", "/player", "/queue",
    "/history", "/playlists", "/spotify/zeroconf",
    "/spotify/auth-status", "/coverart_cache",
    "/media/list", "/media/upload",
)
EXEMPT_FROM_AUTH = (
    "/m/login", "/m/auth-status", "/m/setup", "/m/logout",
)


def _is_manager_path(path: str) -> bool:
    for prefix in MANAGER_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _is_exempt(path: str) -> bool:
    for ep in EXEMPT_FROM_AUTH:
        if path == ep or path.startswith(ep + "/"):
            return True
    return False


@app.middleware("http")
async def manager_auth_middleware(request, call_next):
    cfg = get_runtime_config()
    if not cfg.manager_auth_enabled:
        return await call_next(request)
    path = request.url.path
    if not _is_manager_path(path) or _is_exempt(path):
        return await call_next(request)
    token = request.cookies.get(MANAGER_COOKIE)
    ip = request.client.host if request.client else ""
    if get_auth().verify_session_token(token, ip=ip):
        return await call_next(request)
    # Not authenticated. Decide between JSON 401 e redirect HTML.
    accept = request.headers.get("accept", "")
    is_html = "text/html" in accept and "application/json" not in accept
    if is_html and request.method == "GET":
        return Response(status_code=302, headers={"Location": "/m/login"})
    return JSONResponse({"detail": "Authentication required"}, status_code=401)

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
    """WebSocket endpoint for real-time state updates. Auth-protected when
    manager_auth_enabled is True (cookie verificato nell'handshake)."""
    cfg = get_runtime_config()
    if cfg.manager_auth_enabled:
        token = ws.cookies.get(MANAGER_COOKIE)
        ip = ws.client.host if ws.client else ""
        if not get_auth().verify_session_token(token, ip=ip):
            await ws.close(code=4401)  # custom: unauthorized
            return
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
async def unified_search(q: str, limit: int = 10, offset: int = 0, sources: str | None = None):
    """Search across sources in parallel and return grouped results.

    Real pagination per source:
    - Spotify: native offset via the Web API.
    - Local: linear scan, slice by offset.
    - YouTube/SoundCloud: yt-dlp doesn't expose offset; we fetch (offset+limit)
      and discard the leading `offset`. Costlier than native offset but the only
      option with the underlying scraper.

    `sources` is an optional CSV (e.g. "spotify" or "youtube,soundcloud") to
    restrict which providers are queried — used by "load more" so only the
    active tab is re-fetched, sparing the unrelated network calls.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    query = q.strip()
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    page = min(limit, 50)
    fetch_total = min(offset + page, 200)
    all_sources = {"local", "youtube", "spotify", "soundcloud"}
    enabled = (
        {s.strip() for s in sources.split(",") if s.strip() in all_sources}
        if sources else all_sources
    )

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
                if len(matches) >= fetch_total:
                    break
            return matches[offset:]
        except Exception:
            return []

    async def _search_youtube():
        from app.youtube import search_youtube
        try:
            results = await asyncio.to_thread(search_youtube, query, fetch_total)
            return results[offset:]
        except Exception:
            return []

    async def _search_spotify():
        if not (settings.spotify_client_id and settings.spotify_client_secret):
            return []
        from app.spotify import search_tracks
        try:
            return await asyncio.to_thread(search_tracks, query, page, offset)
        except Exception:
            return []

    async def _search_soundcloud():
        from app.soundcloud import search_soundcloud
        try:
            results = await asyncio.to_thread(search_soundcloud, query, fetch_total)
            return results[offset:]
        except Exception:
            return []

    runners = {
        "local": _search_local,
        "youtube": _search_youtube,
        "spotify": _search_spotify,
        "soundcloud": _search_soundcloud,
    }
    keys = [k for k in runners if k in enabled]
    results = await asyncio.gather(*(runners[k]() for k in keys))
    out = {k: [] for k in all_sources}
    for k, v in zip(keys, results):
        out[k] = v
    out["offset"] = offset
    out["limit"] = page
    return out


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


@app.get("/spotify/auth-status")
def spotify_auth_status():
    """Auth + Zeroconf bootstrap state, polled by the Settings panel."""
    from app.spotify_audio import get_audio, get_zeroconf
    audio = get_audio()
    return {
        "authenticated": audio.is_authenticated(),
        "session_status": audio.session_status,  # idle | warming | ready | failed
        "zeroconf": get_zeroconf().status(),
    }


@app.post("/spotify/zeroconf/start")
def spotify_zeroconf_start(device_name: str = "DiscoBot"):
    """Expose DiscoBot as a Spotify Connect device for remote login.

    The operator opens the Spotify app on a LAN-connected phone, picks the
    advertised device, and credentials are captured + persisted automatically.
    Idempotent: calling while already running returns the current status.
    """
    from app.spotify_audio import get_audio, get_zeroconf

    if get_audio().is_authenticated():
        raise HTTPException(
            status_code=409,
            detail="Already authenticated. Delete the credentials file to re-login.",
        )
    try:
        return get_zeroconf().start(device_name=device_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Zeroconf start failed: {e}")


@app.post("/spotify/zeroconf/stop")
def spotify_zeroconf_stop():
    """Manual cancel of the Zeroconf bootstrap."""
    from app.spotify_audio import get_zeroconf
    return get_zeroconf().stop()


@app.get("/spotify/stream/{track_id}")
def spotify_stream(track_id: str):
    """Stream a Spotify track as OGG/Vorbis. Loopback-only consumer (VLC)."""
    from app.spotify_audio import get_audio

    audio = get_audio()
    if not audio.is_authenticated():
        raise HTTPException(
            status_code=401,
            detail="Spotify not authenticated. Run: python main.py --spotify-login",
        )
    try:
        generator = audio.open_track_stream(track_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        # The load failed even after the stale-session retry — surface a clean
        # 502 to VLC; the player layer will then trigger its own retry policy.
        raise HTTPException(status_code=502, detail=f"Spotify stream failed: {e}")
    return StreamingResponse(generator, media_type="audio/ogg")


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


# --- Manager auth ---

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str, max_age: int | None, secure: bool) -> None:
    response.set_cookie(
        MANAGER_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _is_secure_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    # Cloudflare tunnel forwards original scheme via X-Forwarded-Proto / cf-visitor
    if request.headers.get("x-forwarded-proto") == "https":
        return True
    return False


@app.get("/m/auth-status")
def manager_auth_status(request: Request):
    auth = get_auth()
    token = request.cookies.get(MANAGER_COOKIE)
    cfg = get_runtime_config()
    if not cfg.manager_auth_enabled:
        authenticated = True
    else:
        ip = _client_ip(request)
        authenticated = bool(auth.verify_session_token(token, ip=ip))
    return {
        "auth_enabled": cfg.manager_auth_enabled,
        "configured": auth.is_configured(),
        "authenticated": authenticated,
    }


@app.post("/m/setup")
async def manager_setup(request: Request):
    auth = get_auth()
    if auth.is_configured():
        raise HTTPException(status_code=409, detail="Password gia' configurata")
    body = await request.json()
    password = (body or {}).get("password", "")
    try:
        auth.set_password(password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ua = request.headers.get("user-agent", "")
    ip = _client_ip(request)
    token, max_age = auth.make_session(remember=True, user_agent=ua, ip=ip)
    response = JSONResponse({"status": "configured"})
    _set_session_cookie(response, token, max_age, _is_secure_request(request))
    return response


@app.post("/m/login")
async def manager_login(request: Request):
    auth = get_auth()
    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="Password non configurata. Apri /m/login per il setup.")
    ip = _client_ip(request)
    cooldown = auth.check_rate_limit(ip)
    if cooldown is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Troppi tentativi. Riprova tra {cooldown} secondi.",
            headers={"Retry-After": str(cooldown)},
        )
    body = await request.json()
    password = (body or {}).get("password", "")
    remember = bool((body or {}).get("remember", False))
    if not auth.verify_password(password):
        auth.record_login_attempt(ip, ok=False)
        raise HTTPException(status_code=401, detail="Password errata")
    auth.record_login_attempt(ip, ok=True)
    ua = request.headers.get("user-agent", "")
    token, max_age = auth.make_session(remember=remember, user_agent=ua, ip=ip)
    response = JSONResponse({"status": "ok"})
    _set_session_cookie(response, token, max_age, _is_secure_request(request))
    return response


@app.post("/m/logout")
def manager_logout(request: Request):
    """Logout: rimuove la sessione corrente dallo store + clear cookie."""
    token = request.cookies.get(MANAGER_COOKIE)
    if token and "." in token:
        try:
            sid = token.split(".", 1)[0]
            get_auth().revoke_session(sid)
        except Exception:
            pass
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(MANAGER_COOKIE, path="/")
    return response


@app.get("/m/sessions")
def manager_sessions_list(request: Request):
    """Elenco delle sessioni attive con flag `current`."""
    auth = get_auth()
    token = request.cookies.get(MANAGER_COOKIE)
    current_id = None
    if token and "." in token:
        current_id = token.split(".", 1)[0]
    sessions = []
    for s in auth.list_sessions():
        sessions.append({
            "id": s["id"],
            "ua_label": s.get("ua_label", "Sconosciuto"),
            "ip_last": s.get("ip_last", ""),
            "last_seen": s.get("last_seen"),
            "created_at": s.get("created_at"),
            "expires_at": s.get("expires_at"),
            "remember": s.get("remember", False),
            "current": s["id"] == current_id,
        })
    return {"sessions": sessions}


@app.delete("/m/sessions/{sid}")
def manager_session_revoke(sid: str, request: Request):
    """Revoca una sessione specifica. La sessione corrente è bloccata
    (l'utente deve usare Logout)."""
    token = request.cookies.get(MANAGER_COOKIE)
    if token and token.split(".", 1)[0] == sid:
        raise HTTPException(
            status_code=400,
            detail="Per chiudere la tua sessione usa Logout.",
        )
    if not get_auth().revoke_session(sid):
        raise HTTPException(status_code=404, detail="Sessione non trovata")
    return {"status": "revoked", "id": sid}


@app.delete("/m/sessions")
def manager_sessions_revoke_bulk(request: Request, keep_current: bool = False):
    """Bulk revoke. keep_current=true → mantiene la corrente."""
    keep_id = None
    if keep_current:
        token = request.cookies.get(MANAGER_COOKIE)
        if token and "." in token:
            keep_id = token.split(".", 1)[0]
    count = get_auth().revoke_all_except(keep_id)
    return {"status": "revoked", "count": count}


# --- Runtime config (admin-only for now; will be auth-protected) ---

@app.get("/admin/config")
def admin_config_get():
    return get_runtime_config().as_dict()


@app.patch("/admin/config")
async def admin_config_patch(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        return get_runtime_config().patch(body)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Tunnel pubblico (cloudflared) ---

@app.on_event("startup")
async def _tunnel_autostart_hook():
    """Se l'utente ha abilitato l'autostart, avvia il tunnel quando uvicorn
    e' pronto (non prima: cloudflared deve poter contattare il backend)."""
    cfg = get_runtime_config()
    if cfg.tunnel_autostart:
        try:
            get_tunnel().start(local_port=settings.port)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("tunnel autostart failed: %s", e)


@app.get("/admin/tunnel/status")
def tunnel_status():
    return get_tunnel().status()


@app.post("/admin/tunnel/start")
def tunnel_start():
    if not get_runtime_config().manager_auth_enabled:
        raise HTTPException(
            status_code=403,
            detail="Tunnel non disponibile finche' l'autenticazione Manager e' disabilitata.",
        )
    return get_tunnel().start(local_port=settings.port)


@app.post("/admin/tunnel/stop")
def tunnel_stop():
    return get_tunnel().stop()


@app.get("/admin/tunnel/qr.svg")
def tunnel_qr():
    """Generate an SVG QR code for the current public URL."""
    state = get_tunnel().status()
    url = state.get("url")
    if not url:
        raise HTTPException(status_code=404, detail="Tunnel non attivo")
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    import io
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue()
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, no-store"},
    )


# --- Pending requests (manager view + approve/reject) ---

@app.get("/pending")
def pending_list():
    return {"items": [p.model_dump() for p in player.get_pending()]}


@app.post("/pending/{pid}/approve")
def pending_approve(pid: int):
    try:
        track = player.approve_pending(pid)
        return {"approved": track.model_dump()}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/pending/{pid}")
def pending_reject(pid: int):
    try:
        player.reject_pending(pid)
        return {"status": "rejected", "id": pid}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Public interface (subset, governed by runtime_config) ---

PUBLIC_COOKIE = "discobot_pid"


def _public_id(request: Request, response: Response) -> str:
    """Get-or-create the public requester UUID, stored in cookie."""
    pid = request.cookies.get(PUBLIC_COOKIE)
    if not pid:
        pid = _uuid.uuid4().hex
        response.set_cookie(
            PUBLIC_COOKIE, pid,
            max_age=60 * 60 * 24 * 365,  # 1y
            httponly=False,  # frontend reads it for "le mie richieste" filter
            samesite="lax",
        )
    return pid


@app.get("/public/config")
def public_config():
    return get_runtime_config().public_view()


@app.get("/public/state")
def public_state():
    s = player.get_state()
    return {
        "current_track": s.current_track.model_dump() if s.current_track else None,
        "queue": [t.model_dump() for t in s.queue],
        "is_playing": s.is_playing,
        "position": s.position,
        "duration": s.duration,
    }


@app.get("/public/search")
async def public_search(q: str, limit: int = 10, offset: int = 0):
    """Like /search but limited to the sources enabled for the public."""
    cfg = get_runtime_config()
    if not cfg.public_enabled:
        raise HTTPException(status_code=503, detail="Interfaccia pubblica disattivata")
    enabled = [k for k, v in cfg.public_view()["sources"].items() if v]
    if not enabled:
        return {"local": [], "youtube": [], "spotify": [], "soundcloud": [],
                "offset": offset, "limit": limit}
    # Riusa la logica di unified_search via il suo handler
    return await unified_search(q=q, limit=limit, offset=offset, sources=",".join(enabled))


@app.post("/public/queue/add")
async def public_queue_add(request: Request, response: Response):
    cfg = get_runtime_config()
    if not cfg.public_enabled:
        raise HTTPException(status_code=503, detail="Interfaccia pubblica disattivata")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be JSON object")
    path = body.get("path")
    type_ = body.get("type")
    name = (body.get("requester_name") or "").strip()[:30]
    if not path or not type_:
        raise HTTPException(status_code=400, detail="path e type sono richiesti")
    if not cfg.is_source_enabled_for_public(type_):
        raise HTTPException(
            status_code=400,
            detail=f"Sorgente '{type_}' non disponibile per il pubblico",
        )
    pid = _public_id(request, response)
    try:
        req = TrackRequest(path=path, type=type_)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Richiesta non valida: {e}")

    if cfg.public_require_approval:
        try:
            entry = player.add_pending(
                req,
                requester_name=name,
                requester_id=pid,
                preview_title=body.get("preview_title"),
            )
            return {"status": "pending", "pending": entry.model_dump()}
        except ValueError as e:
            raise HTTPException(status_code=429, detail=str(e))
    else:
        try:
            track = player.add_public_direct(req, requester_id=pid)
            return {"status": "queued", "track": track.model_dump()}
        except ValueError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/public/my-pending")
def public_my_pending(request: Request, response: Response):
    pid = _public_id(request, response)
    return {"items": [p.model_dump() for p in player.get_pending_for_requester(pid)]}


# --- Web UI ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_public_ui():
    """Public-facing interface. Limited subset of features, governed by
    runtime_config.public_enabled. Default landing — un attaccante che prova
    la root trova solo questa, non il pannello manager."""
    return FileResponse("static/public.html")


@app.get("/m")
def serve_manager_ui():
    """Manager control panel. Protected by manager_auth_middleware."""
    return FileResponse("static/index.html")


@app.get("/m/login")
def serve_manager_login():
    """Setup wizard + login page. Esente dall'auth middleware."""
    return FileResponse("static/login.html")
