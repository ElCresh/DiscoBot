"""Audio player engine using VLC."""

import asyncio
import atexit
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path

import vlc

from app.models import (
    HistoryEntry, PendingTrack, PlayerState, RepeatMode, Track, TrackRequest, TrackType,
)
from app.youtube import extract_audio_url

logger = logging.getLogger(__name__)

SOUNDFONTS_DIR = Path("soundfonts")
STATE_FILE = Path("state.json")
PLAYLISTS_DIR = Path("playlists")
MAX_HISTORY = 10000  # safety cap on stored entries; UI paginates the display
COVER_PRUNE_INTERVAL_S = 30 * 60  # 30min — orphan cover-cache sweep cadence
# Retry policy for failed playback start: try the same track up to 3 times
# (covers transient network/Spotify hiccups), then advance to next.
PLAY_MAX_ATTEMPTS = 3
PLAY_RETRY_DELAY_S = 1.0
# Anti-abuso interfaccia pubblica
PUBLIC_MAX_PENDING_PER_REQUESTER = 3
PUBLIC_COOLDOWN_SECONDS = 15.0
# Watchdog: if a track set_media+play() doesn't reach Playing state within
# this many seconds, treat it as stuck and trigger the retry policy. Streamed
# sources (Spotify, YouTube) usually need a couple seconds to buffer; 15s is
# generous enough to avoid false positives but tight enough not to hang a set.
PLAY_WATCHDOG_S = 15.0


def _find_soundfont() -> Path | None:
    """Primo .sf2 in soundfonts/ in ordine alfabetico, o None se assente."""
    if not SOUNDFONTS_DIR.is_dir():
        return None
    sfs = sorted(SOUNDFONTS_DIR.glob("*.sf2"))
    return sfs[0] if sfs else None


def _read_local_metadata(filepath: str) -> tuple[str, str | None, str | None]:
    """Read ID3/metadata tags from a local audio file. Returns (title, artist, album)."""
    try:
        from tinytag import TinyTag

        tag = TinyTag.get(filepath)
        title = tag.title or Path(filepath).stem
        artist = tag.artist
        album = tag.album
        return title, artist, album
    except Exception:
        return Path(filepath).stem, None, None


class ConnectionManager:
    """Manages WebSocket connections for broadcasting state updates."""

    def __init__(self):
        self._connections: list[asyncio.Queue] = []
        self._sync_subscribers: list = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._connections.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            try:
                self._connections.remove(q)
            except ValueError:
                pass

    def subscribe_sync(self, callback) -> None:
        """Register an in-process callback invoked synchronously on every broadcast.

        Used by the native presentation window to receive state updates without
        going through a WebSocket. The callback runs on the broadcasting thread,
        so it must be cheap (e.g. emit a Qt signal).
        """
        with self._lock:
            self._sync_subscribers.append(callback)

    def broadcast(self, data: dict):
        with self._lock:
            for q in self._connections:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # drop if consumer is too slow
            subs = list(self._sync_subscribers)
        for cb in subs:
            try:
                cb(data)
            except Exception:
                logger.debug("sync subscriber failed", exc_info=True)


ws_manager = ConnectionManager()


class AudioPlayer:
    def __init__(self):
        # MIDI playback: VLC's built-in fluidsynth plugin reads --soundfont and
        # decodes .mid files natively. No external binary or Python wrapper needed.
        # Linux: requires `vlc-plugin-fluidsynth` (or equivalent). Windows/macOS:
        # bundled in the official VLC build.
        vlc_args = ["--quiet", "--no-video-title-show"]
        sf = _find_soundfont()
        if sf is not None:
            vlc_args.append(f"--soundfont={sf.resolve()}")
            logger.info(f"Using SoundFont: {sf}")
        else:
            logger.warning(
                "No .sf2 SoundFont found in soundfonts/ — MIDI playback may be silent. "
                "Drop a GM SoundFont (e.g. GeneralUser-GS.sf2) into soundfonts/."
            )
        self._instance = vlc.Instance(vlc_args)
        self._player: vlc.MediaPlayer = self._instance.media_player_new()
        self._queue: list[Track] = []
        self._current: Track | None = None
        self._track_counter = 0
        self._volume = 80
        self._history: list[dict] = []
        self._shuffle = False
        self._repeat = RepeatMode.OFF
        self._normalize = True
        self._lock = threading.Lock()
        self._video_hwnd: int | None = None
        # Retry bookkeeping for the currently-playing track. Keyed by track.id
        # so a watchdog firing after a skip doesn't retry the wrong track.
        self._play_attempts: dict[int, int] = {}
        self._play_watchdog: threading.Timer | None = None
        # Public-interface pending queue (in-process, persisted in state.json).
        # _pending_counter assigns monotonic IDs distinct from queue track IDs
        # so frontend keys don't collide.
        self._pending: list[PendingTrack] = []
        self._pending_counter = 0
        # Per-requester cooldown tracking for rate limiting.
        self._public_last_request: dict[str, float] = {}

        PLAYLISTS_DIR.mkdir(exist_ok=True)

        self._load_state()

        self._player.audio_set_volume(self._volume)

        # Listen for track end to auto-advance, and for hard errors so the
        # retry policy kicks in instead of leaving the player in limbo.
        events = self._player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_track_end)
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_player_error)
        events.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_player_playing)

        atexit.register(self._atexit_save)

        # One-shot prune at startup to clean cruft accumulated while offline.
        try:
            self._prune_cover_cache()
        except Exception:
            logger.exception("Initial cover cache prune failed")

        save_thread = threading.Thread(target=self._periodic_save, daemon=True)
        save_thread.start()
        prune_thread = threading.Thread(target=self._periodic_prune, daemon=True)
        prune_thread.start()

    def _save_state(self):
        """Serialize player state to JSON. Must be called inside _lock."""
        try:
            current_data = self._current.model_dump() if self._current else None
            state = {
                "version": 2,
                "track_counter": self._track_counter,
                "volume": self._volume,
                "shuffle": self._shuffle,
                "repeat": self._repeat.value,
                "normalize": self._normalize,
                "current_track": current_data,
                "queue": [t.model_dump() for t in self._queue],
                "history": self._history,
                "pending": [p.model_dump() for p in self._pending],
                "pending_counter": self._pending_counter,
            }
            tmp_path = STATE_FILE.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(STATE_FILE))
        except Exception:
            logger.exception("Failed to save player state")

    def _load_state(self):
        """Restore player state from state.json. Called during __init__."""
        try:
            if not STATE_FILE.exists():
                return
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self._track_counter = data.get("track_counter", 0)
            self._volume = data.get("volume", 80)
            self._shuffle = data.get("shuffle", False)
            self._repeat = RepeatMode(data.get("repeat", "off"))
            self._normalize = data.get("normalize", True)
            self._history = data.get("history", [])[:MAX_HISTORY]

            # Restore pending list (public requests awaiting approval)
            self._pending_counter = data.get("pending_counter", 0)
            for p in data.get("pending", []):
                try:
                    self._pending.append(PendingTrack(**p))
                except Exception:
                    logger.warning("Skipping malformed pending entry: %r", p)

            # Restore queue
            for t in data.get("queue", []):
                track = Track(**t)
                if track.type == TrackType.LOCAL and not Path(track.path).exists():
                    logger.warning(f"Skipping missing local file: {track.path}")
                    continue
                self._queue.append(track)

            # Re-insert current track at head of queue (don't auto-play)
            current_data = data.get("current_track")
            if current_data:
                track = Track(**current_data)
                if track.type == TrackType.LOCAL and not Path(track.path).exists():
                    logger.warning(f"Skipping missing local file: {track.path}")
                else:
                    self._queue.insert(0, track)

            logger.info(
                f"Restored state: {len(self._queue)} tracks in queue, volume={self._volume}"
            )
        except Exception:
            logger.exception("Failed to load player state, starting fresh")
            self._queue.clear()
            self._track_counter = 0
            self._volume = 80

    def _periodic_save(self):
        """Background thread: save state every 30s as a safety net."""
        while True:
            time.sleep(30)
            with self._lock:
                self._save_state()

    def _atexit_save(self):
        """Best-effort save at shutdown."""
        try:
            with self._lock:
                self._save_state()
        except Exception:
            pass

    def _prune_cover_cache(self):
        """Delete coverart_cache/ files not referenced by current/queue/history."""
        from app.coverart import cache_filename_for, prune_orphans, video_cache_filename_for

        keep: set[str] = set()
        with self._lock:
            tracks: list[Track] = []
            if self._current is not None:
                tracks.append(self._current)
            tracks.extend(self._queue)
            for h in self._history:
                try:
                    tracks.append(Track(**h["track"]))
                except Exception:
                    continue
        for t in tracks:
            n = cache_filename_for(t)
            if n:
                keep.add(n)
            v = video_cache_filename_for(t)
            if v:
                keep.add(v)
        removed = prune_orphans(keep)
        if removed:
            logger.info("Pruned %d orphan cover(s) from coverart_cache/", removed)

    def _periodic_prune(self):
        """Background thread: orphan-prune the cover cache every 30 minutes."""
        while True:
            time.sleep(COVER_PRUNE_INTERVAL_S)
            try:
                self._prune_cover_cache()
            except Exception:
                logger.exception("Periodic cover cache prune failed")

    def _next_id(self) -> int:
        self._track_counter += 1
        return self._track_counter

    def _broadcast_state(self):
        """Broadcast current state to all WebSocket clients. Must be called inside _lock."""
        try:
            state = self._get_state_unlocked()
            ws_manager.broadcast(state.model_dump())
        except Exception:
            logger.debug("Broadcast failed", exc_info=True)

    def _record_history(self):
        """Record current track in history. Must be called inside _lock."""
        if self._current is None:
            return
        from datetime import datetime, timezone
        entry = {
            "track": self._current.model_dump(),
            "played_at": datetime.now(timezone.utc).isoformat(),
        }
        self._history.insert(0, entry)
        if len(self._history) > MAX_HISTORY:
            self._history.pop()

    def _on_track_end(self, event):
        """Called when a track finishes playing. Advances to next in queue."""
        threading.Thread(target=self._advance_on_end, daemon=True).start()

    def _on_player_error(self, event):
        """VLC failed to load/decode the current media — trigger retry policy."""
        threading.Thread(target=self._handle_play_failure_async, daemon=True).start()

    def _on_player_playing(self, event):
        """VLC reached Playing state — clear watchdog and retry counter."""
        self._cancel_watchdog()
        cur = self._current
        if cur is not None:
            self._play_attempts.pop(cur.id, None)

    def _cancel_watchdog(self) -> None:
        if self._play_watchdog is not None:
            self._play_watchdog.cancel()
            self._play_watchdog = None

    def _arm_watchdog(self, track_id: int) -> None:
        """Schedule a stuck-loading check for a specific track."""
        self._cancel_watchdog()
        t = threading.Timer(PLAY_WATCHDOG_S, self._on_watchdog_fired, args=(track_id,))
        t.daemon = True
        self._play_watchdog = t
        t.start()

    def _on_watchdog_fired(self, track_id: int) -> None:
        """Track set_media+play() didn't reach Playing in time — likely stuck."""
        with self._lock:
            cur = self._current
            if cur is None or cur.id != track_id:
                return  # already moved on
            try:
                state = self._player.get_state()
            except Exception:
                state = None
            if state == vlc.State.Playing:
                return  # got there in time
            logger.warning(
                "Watchdog: track %d stuck in state %s after %.0fs, retrying",
                track_id, state, PLAY_WATCHDOG_S,
            )
            self._handle_play_failure_locked(cur)

    def _handle_play_failure_async(self) -> None:
        """Entry point from VLC error event — acquires the lock then retries."""
        with self._lock:
            cur = self._current
            if cur is None:
                return
            self._handle_play_failure_locked(cur)

    def _handle_play_failure_locked(self, track: Track) -> None:
        """Retry the same track up to PLAY_MAX_ATTEMPTS, then skip to next.

        Must be called with self._lock held.
        """
        attempts = self._play_attempts.get(track.id, 0) + 1
        self._play_attempts[track.id] = attempts
        try:
            self._player.stop()
        except Exception:
            pass
        self._cancel_watchdog()

        if attempts < PLAY_MAX_ATTEMPTS:
            logger.warning(
                "Track %s failed (attempt %d/%d), retrying in %.1fs",
                track.title, attempts, PLAY_MAX_ATTEMPTS, PLAY_RETRY_DELAY_S,
            )
            threading.Timer(
                PLAY_RETRY_DELAY_S, self._retry_play, args=(track,),
            ).start()
            return

        # Out of attempts: mark as failed, advance.
        logger.error(
            "Track %s failed after %d attempts, skipping",
            track.title, PLAY_MAX_ATTEMPTS,
        )
        self._play_attempts.pop(track.id, None)
        self._record_history()
        self._current = None
        if self._queue:
            idx = random.randrange(len(self._queue)) if self._shuffle else 0
            next_track = self._queue.pop(idx)
            self._play_track(next_track)
        self._save_state()
        self._broadcast_state()

    def _retry_play(self, track: Track) -> None:
        """Re-issue _play_track for a failed attempt. Acquires the lock."""
        with self._lock:
            # Only retry if still the current focus (user might have skipped)
            if self._current is None or self._current.id != track.id:
                # User moved on; clear bookkeeping and let the new flow run
                self._play_attempts.pop(track.id, None)
                return
            self._play_track(track)

    def _advance_on_end(self):
        """Handle track end: respect repeat mode, then advance."""
        with self._lock:
            if self._repeat == RepeatMode.ONE and self._current:
                # Replay current track
                self._play_track(self._current)
                self._save_state()
                self._broadcast_state()
                return

            self._record_history()
            self._player.stop()

            if self._repeat == RepeatMode.ALL and self._current:
                # Re-add finished track to end of queue
                recycled = Track(
                    id=self._next_id(),
                    path=self._current.path,
                    title=self._current.title,
                    type=self._current.type,
                    duration=self._current.duration,
                    artist=self._current.artist,
                    album=self._current.album,
                    cover_url=self._current.cover_url,
                )
                self._queue.append(recycled)

            if self._queue:
                idx = random.randrange(len(self._queue)) if self._shuffle else 0
                track = self._queue.pop(idx)
                self._play_track(track)
            else:
                self._current = None
                logger.info("Queue empty, playback stopped")

            self._save_state()
            self._broadcast_state()

    def _resolve_track(self, path: str, track_type: TrackType) -> tuple[str, str, float | None]:
        """Resolve a track path to a playable URL/path, title, and duration."""
        if track_type == TrackType.YOUTUBE:
            return extract_audio_url(path)
        elif track_type == TrackType.SPOTIFY:
            from app.config import settings
            from app.spotify import extract_spotify_metadata

            # Return only the song title (no artist prefix): _play_track will
            # overwrite track.title with this, and we don't want to clobber
            # the separate track.artist field set during add_track.
            title, _artist, _album, duration, _cover = extract_spotify_metadata(path)
            # VLC opens this loopback URL; the FastAPI endpoint streams OGG
            # bytes decrypted by librespot. Same uvicorn process serves it.
            stream_url = f"http://127.0.0.1:{settings.port}/spotify/stream/{path}"
            return stream_url, title, duration
        elif track_type == TrackType.SOUNDCLOUD:
            from app.soundcloud import extract_audio_url as sc_extract

            return sc_extract(path)
        else:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"File not found: {path}")
            return str(p.resolve()), p.stem, None

    def _play_track(self, track: Track):
        """Start playing a specific track. Sync errors and a stuck-loading
        watchdog both feed into the retry policy (`_handle_play_failure_locked`).
        """
        try:
            playable_url, title, duration = self._resolve_track(track.path, track.type)
            track.title = title
            if duration:
                track.duration = duration

            media = self._instance.media_new(playable_url)
            if self._normalize:
                # AGC: sliding-window RMS normalizer to level out volume between sources
                media.add_option(":audio-filter=normvol")
                media.add_option(":norm-buff-size=20")
                media.add_option(":norm-max-level=2.0")
            self._player.set_media(media)
            if self._video_hwnd is not None:
                # Reapply on every track — VLC sometimes drops the binding on media change.
                self._set_video_handle(self._video_hwnd)
            self._player.play()
            self._player.audio_set_volume(self._volume)
            self._current = track
            self._arm_watchdog(track.id)
            logger.info(f"Now playing: {track.title}")
        except Exception as e:
            # Synchronous failure (e.g. _resolve_track raised). Funnel into the
            # same retry policy as VLC's async error event so we don't end up
            # in the half-dead state the user reported.
            logger.error(f"Failed to play track {track.title!r}: {e}")
            self._current = track
            self._handle_play_failure_locked(track)

    def add_track(self, path: str, track_type: TrackType) -> Track:
        """Add a track to the queue. Starts playing if nothing is playing."""
        # Resolve metadata before acquiring lock (network I/O)
        artist = None
        album = None
        cover_url = None
        if track_type == TrackType.YOUTUBE:
            from app.youtube import extract_youtube_metadata

            title, duration, cover_url = extract_youtube_metadata(path)
        elif track_type == TrackType.SPOTIFY:
            from app.spotify import extract_spotify_metadata

            title, artist, album, duration, cover_url = extract_spotify_metadata(path)
        elif track_type == TrackType.SOUNDCLOUD:
            from app.soundcloud import extract_soundcloud_metadata

            title, duration, cover_url = extract_soundcloud_metadata(path)
        else:
            title, artist, album = _read_local_metadata(path)
            duration = None

        with self._lock:
            track = Track(
                id=self._next_id(),
                path=path,
                title=title,
                type=track_type,
                duration=duration,
                artist=artist,
                album=album,
                cover_url=cover_url,
            )
            if self._current is None and not self._queue:
                self._play_track(track)
            else:
                self._queue.append(track)
                logger.info(f"Queued: {track.title}")
            self._save_state()
            self._broadcast_state()
            return track

    # ----- Public interface: pending requests -----------------------------

    def _check_public_rate(self, requester_id: str) -> None:
        """Raise ValueError if requester is over the rate limit.

        Run inside the lock. Counts only pending requests still waiting; once
        approved/rejected they don't count toward the cap (so a regular user
        can keep submitting at the cooldown rate).
        """
        now = time.monotonic()
        last = self._public_last_request.get(requester_id)
        if last is not None:
            elapsed = now - last
            if elapsed < PUBLIC_COOLDOWN_SECONDS:
                wait = int(PUBLIC_COOLDOWN_SECONDS - elapsed) + 1
                raise ValueError(
                    f"Aspetta ancora {wait} secondi prima della prossima richiesta."
                )
        active = sum(1 for p in self._pending if p.requester_id == requester_id)
        if active >= PUBLIC_MAX_PENDING_PER_REQUESTER:
            raise ValueError(
                f"Hai gia' {active} richieste in attesa, aspetta che il DJ le valuti."
            )

    def add_pending(
        self,
        track_request: TrackRequest,
        requester_name: str,
        requester_id: str,
        preview_title: str | None = None,
    ) -> PendingTrack:
        """Queue a public request for the manager to approve."""
        from datetime import datetime, timezone

        with self._lock:
            self._check_public_rate(requester_id)
            self._pending_counter += 1
            entry = PendingTrack(
                id=self._pending_counter,
                track_request=track_request,
                requester_name=requester_name or "Anonimo",
                requester_id=requester_id,
                submitted_at=datetime.now(timezone.utc).isoformat(),
                preview_title=preview_title,
            )
            self._pending.append(entry)
            self._public_last_request[requester_id] = time.monotonic()
            self._save_state()
            self._broadcast_state()
            return entry

    def approve_pending(self, pending_id: int) -> Track:
        """Move a pending request into the live queue, resolving its metadata."""
        with self._lock:
            idx = next((i for i, p in enumerate(self._pending) if p.id == pending_id), None)
            if idx is None:
                raise KeyError(f"Pending {pending_id} not found")
            entry = self._pending.pop(idx)
            self._save_state()
        # add_track does its own locking + broadcast
        return self.add_track(entry.track_request.path, entry.track_request.type)

    def reject_pending(self, pending_id: int) -> None:
        with self._lock:
            idx = next((i for i, p in enumerate(self._pending) if p.id == pending_id), None)
            if idx is None:
                raise KeyError(f"Pending {pending_id} not found")
            self._pending.pop(idx)
            self._save_state()
            self._broadcast_state()

    def add_public_direct(
        self,
        track_request: TrackRequest,
        requester_id: str,
    ) -> Track:
        """Direct-mode public add: enforce rate limit, then go through add_track."""
        with self._lock:
            self._check_public_rate(requester_id)
            self._public_last_request[requester_id] = time.monotonic()
        return self.add_track(track_request.path, track_request.type)

    def get_pending(self) -> list[PendingTrack]:
        with self._lock:
            return list(self._pending)

    def get_pending_for_requester(self, requester_id: str) -> list[PendingTrack]:
        with self._lock:
            return [p for p in self._pending if p.requester_id == requester_id]

    def play(self):
        """Resume playback."""
        with self._lock:
            if self._current:
                self._player.play()
            elif self._queue:
                track = self._queue.pop(0)
                self._play_track(track)
            self._save_state()
            self._broadcast_state()

    def pause(self):
        """Pause playback."""
        with self._lock:
            self._player.pause()
            self._broadcast_state()

    def stop(self):
        """Stop playback."""
        with self._lock:
            self._player.stop()
            self._record_history()
            self._current = None
            self._save_state()
            self._broadcast_state()

    def skip(self):
        """Skip to the next track."""
        with self._lock:
            self._record_history()
            self._player.stop()

            if self._repeat == RepeatMode.ALL and self._current:
                recycled = Track(
                    id=self._next_id(),
                    path=self._current.path,
                    title=self._current.title,
                    type=self._current.type,
                    duration=self._current.duration,
                    artist=self._current.artist,
                    album=self._current.album,
                    cover_url=self._current.cover_url,
                )
                self._queue.append(recycled)

            if self._queue:
                idx = random.randrange(len(self._queue)) if self._shuffle else 0
                track = self._queue.pop(idx)
                self._play_track(track)
            else:
                self._current = None
                logger.info("Queue empty, playback stopped")
            self._save_state()
            self._broadcast_state()

    def previous(self):
        """Restart current track (no history tracking)."""
        with self._lock:
            if self._current:
                self._player.set_position(0)
                self._broadcast_state()

    def set_volume(self, volume: int):
        """Set volume (0-100)."""
        with self._lock:
            self._volume = max(0, min(100, volume))
            self._player.audio_set_volume(self._volume)
            self._save_state()
            self._broadcast_state()

    def seek(self, position: float):
        """Seek to a position in seconds."""
        with self._lock:
            if self._current and self._player.get_length() > 0:
                length_sec = self._player.get_length() / 1000.0
                if 0 <= position <= length_sec:
                    self._player.set_position(position / length_sec)
                    self._broadcast_state()

    def set_shuffle(self, enabled: bool):
        """Toggle shuffle mode."""
        with self._lock:
            self._shuffle = enabled
            self._save_state()
            self._broadcast_state()

    def set_repeat(self, mode: RepeatMode):
        """Set repeat mode."""
        with self._lock:
            self._repeat = mode
            self._save_state()
            self._broadcast_state()

    def set_normalize(self, enabled: bool):
        """Toggle audio auto-leveling. Takes effect on the next track."""
        with self._lock:
            self._normalize = enabled
            self._save_state()
            self._broadcast_state()

    def _set_video_handle(self, handle: int) -> None:
        # libvlc espone tre API di binding diverse per OS: HWND su Windows,
        # NSObject su macOS, XID su Linux. Le altre due chiamate non sollevano
        # eccezione su piattaforma sbagliata, ma non bind nulla — il video non
        # appare e la window resta in stato indefinito.
        if sys.platform == "win32":
            self._player.set_hwnd(handle)
        elif sys.platform == "darwin":
            self._player.set_nsobject(handle)
        else:
            self._player.set_xwindow(handle)

    def attach_video_window(self, hwnd: int):
        """Bind VLC video output to a native window handle (HWND/XID/NSView).

        Must be called once at startup, after the target widget is realized.
        The handle is reapplied on every track change inside _play_track.
        """
        self._video_hwnd = hwnd
        try:
            self._set_video_handle(hwnd)
        except Exception:
            logger.exception("Failed to bind VLC video output to handle %s", hwnd)

    @property
    def vlc_player(self):
        """Underlying libvlc MediaPlayer. Exposed so the presentation window
        can attach event listeners (e.g. MediaPlayerVout for video size)."""
        return self._player

    def remove_track(self, track_id: int) -> bool:
        """Remove a track from the queue by ID."""
        with self._lock:
            for i, track in enumerate(self._queue):
                if track.id == track_id:
                    self._queue.pop(i)
                    self._save_state()
                    self._broadcast_state()
                    return True
            return False

    def clear_queue(self):
        """Clear the entire queue."""
        with self._lock:
            self._queue.clear()
            self._save_state()
            self._broadcast_state()

    def move_track(self, track_id: int, new_position: int) -> bool:
        """Move a track to a new position in the queue."""
        with self._lock:
            for i, track in enumerate(self._queue):
                if track.id == track_id:
                    self._queue.pop(i)
                    new_pos = max(0, min(len(self._queue), new_position))
                    self._queue.insert(new_pos, track)
                    self._save_state()
                    self._broadcast_state()
                    return True
            return False

    def get_history(self, offset: int = 0, limit: int | None = None) -> tuple[list[HistoryEntry], int]:
        """Get a slice of playback history. Returns (items, total)."""
        with self._lock:
            total = len(self._history)
            if offset < 0:
                offset = 0
            end = total if limit is None else min(total, offset + limit)
            slice_ = self._history[offset:end]
            return [HistoryEntry(**e) for e in slice_], total

    def remove_history_entry(self, index: int):
        """Remove a single entry from playback history."""
        with self._lock:
            if index < 0 or index >= len(self._history):
                raise IndexError("Invalid history index")
            self._history.pop(index)
            self._save_state()
            self._broadcast_state()

    def clear_history(self):
        """Clear playback history."""
        with self._lock:
            self._history.clear()
            self._save_state()
            self._broadcast_state()

    def requeue_from_history(self, index: int) -> Track:
        """Re-add a track from history to the queue."""
        with self._lock:
            if index < 0 or index >= len(self._history):
                raise IndexError("Invalid history index")
            original = Track(**self._history[index]["track"])
            track = Track(
                id=self._next_id(),
                path=original.path,
                title=original.title,
                type=original.type,
                duration=original.duration,
                artist=original.artist,
                album=original.album,
                cover_url=original.cover_url,
            )
            if self._current is None and not self._queue:
                self._play_track(track)
            else:
                self._queue.append(track)
            self._save_state()
            self._broadcast_state()
            return track

    def _get_state_unlocked(self) -> PlayerState:
        """Get player state without acquiring lock."""
        length = self._player.get_length()
        position = self._player.get_time()
        is_playing = self._player.is_playing() == 1

        if self._current and length > 0:
            self._current.duration = length / 1000.0

        # VLC reports length=0 for HTTP streams without Content-Length
        # (Spotify via the librespot loopback). Fall back to the duration
        # already captured in the Track from the source's metadata.
        if length > 0:
            duration = length / 1000.0
        elif self._current and self._current.duration:
            duration = self._current.duration
        else:
            duration = 0.0

        return PlayerState(
            current_track=self._current,
            queue=list(self._queue),
            is_playing=is_playing,
            volume=self._volume,
            position=max(0, position / 1000.0) if position >= 0 else 0.0,
            duration=duration,
            shuffle=self._shuffle,
            repeat=self._repeat,
            normalize=self._normalize,
        )

    def get_state(self) -> PlayerState:
        """Get current player state."""
        with self._lock:
            return self._get_state_unlocked()

    # --- Playlist management ---

    def save_playlist(self, name: str) -> int:
        """Save current queue as a named playlist. Returns track count."""
        safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        if not safe_name:
            raise ValueError("Invalid playlist name")
        with self._lock:
            tracks = []
            if self._current:
                tracks.append(self._current.model_dump())
            tracks.extend(t.model_dump() for t in self._queue)
            data = {"name": name, "tracks": tracks}
            path = PLAYLISTS_DIR / f"{safe_name}.json"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return len(tracks)

    def list_playlists(self) -> list[dict]:
        """List all saved playlists."""
        playlists = []
        for f in sorted(PLAYLISTS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                playlists.append({
                    "name": data.get("name", f.stem),
                    "track_count": len(data.get("tracks", [])),
                })
            except Exception:
                continue
        return playlists

    def load_playlist(self, name: str, replace: bool = False) -> int:
        """Load a playlist into the queue. Returns number of tracks added."""
        safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Playlist not found: {name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        tracks_data = data.get("tracks", [])

        with self._lock:
            if replace:
                self._player.stop()
                self._record_history()
                self._current = None
                self._queue.clear()

            new_tracks = []
            for t in tracks_data:
                track = Track(
                    id=self._next_id(),
                    path=t["path"],
                    title=t.get("title", "Unknown"),
                    type=TrackType(t.get("type", "local")),
                    duration=t.get("duration"),
                    artist=t.get("artist"),
                    album=t.get("album"),
                    cover_url=t.get("cover_url"),
                )
                new_tracks.append(track)

            if replace and new_tracks:
                self._play_track(new_tracks[0])
                self._queue.extend(new_tracks[1:])
            else:
                self._queue.extend(new_tracks)

            self._save_state()
            self._broadcast_state()
            return len(new_tracks)

    def delete_playlist(self, name: str):
        """Delete a saved playlist."""
        safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Playlist not found: {name}")
        path.unlink()

    def get_playlist(self, name: str) -> dict:
        """Get playlist details."""
        safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        path = PLAYLISTS_DIR / f"{safe_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Playlist not found: {name}")
        return json.loads(path.read_text(encoding="utf-8"))
