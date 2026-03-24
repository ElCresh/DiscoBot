"""Audio player engine using VLC."""

import atexit
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import vlc

from app.models import HistoryEntry, PlayerState, Track, TrackType
from app.youtube import extract_audio_url

logger = logging.getLogger(__name__)

MIDI_EXTENSIONS = {".mid", ".midi"}
SOUNDFONTS_DIR = Path("soundfonts")
STATE_FILE = Path("state.json")
MAX_HISTORY = 50


class AudioPlayer:
    def __init__(self):
        self._instance = vlc.Instance()
        self._player: vlc.MediaPlayer = self._instance.media_player_new()
        self._queue: list[Track] = []
        self._current: Track | None = None
        self._track_counter = 0
        self._volume = 80
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="discobot_midi_"))
        self._fluid = self._init_fluidsynth()

        self._load_state()

        self._player.audio_set_volume(self._volume)

        # Listen for track end to auto-advance
        events = self._player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_track_end)

        atexit.register(self._cleanup_tmp)
        atexit.register(self._atexit_save)

        save_thread = threading.Thread(target=self._periodic_save, daemon=True)
        save_thread.start()

    @staticmethod
    def _init_fluidsynth():
        """Try to initialize FluidSynth with the first available SoundFont."""
        try:
            from midi2audio import FluidSynth
        except ImportError:
            logger.warning("midi2audio not installed — MIDI playback disabled")
            return None

        sf_files = sorted(SOUNDFONTS_DIR.glob("*.sf2")) if SOUNDFONTS_DIR.is_dir() else []
        if not sf_files:
            logger.warning(
                "No .sf2 SoundFont found in soundfonts/ — MIDI playback disabled. "
                "Download a GM SoundFont (e.g. FluidR3_GM.sf2) and place it there."
            )
            return None

        logger.info(f"Using SoundFont: {sf_files[0]}")
        return FluidSynth(str(sf_files[0]))

    def _convert_midi(self, midi_path: Path) -> str:
        """Convert a MIDI file to WAV via FluidSynth. Returns path to WAV."""
        if self._fluid is None:
            raise RuntimeError(
                "MIDI playback requires midi2audio + a .sf2 SoundFont in soundfonts/"
            )
        wav_path = self._tmp_dir / (midi_path.stem + ".wav")
        self._fluid.midi_to_audio(str(midi_path), str(wav_path))
        logger.info(f"Converted MIDI → WAV: {wav_path}")
        return str(wav_path)

    def _cleanup_tmp(self):
        """Remove temporary converted files."""
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _save_state(self):
        """Serialize player state to JSON. Must be called inside _lock."""
        try:
            current_data = self._current.model_dump() if self._current else None
            state = {
                "version": 1,
                "track_counter": self._track_counter,
                "volume": self._volume,
                "current_track": current_data,
                "queue": [t.model_dump() for t in self._queue],
                "history": self._history,
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
            self._history = data.get("history", [])[:MAX_HISTORY]

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

    def _next_id(self) -> int:
        self._track_counter += 1
        return self._track_counter

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
        threading.Thread(target=self.skip, daemon=True).start()

    def _resolve_track(self, path: str, track_type: TrackType) -> tuple[str, str, float | None]:
        """Resolve a track path to a playable URL/path, title, and duration."""
        if track_type == TrackType.YOUTUBE:
            return extract_audio_url(path)
        elif track_type == TrackType.SPOTIFY:
            from app.spotify import build_youtube_search_query
            from app.youtube import search_youtube_audio

            search_query, display_title, duration = build_youtube_search_query(path)
            audio_url, _, yt_duration = search_youtube_audio(search_query)
            return audio_url, display_title, duration or yt_duration
        else:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"File not found: {path}")
            # MIDI files need conversion to WAV for reliable VLC playback
            if p.suffix.lower() in MIDI_EXTENSIONS:
                wav_path = self._convert_midi(p)
                return wav_path, p.stem, None
            return str(p.resolve()), p.stem, None

    def _play_track(self, track: Track):
        """Start playing a specific track."""
        try:
            playable_url, title, duration = self._resolve_track(track.path, track.type)
            track.title = title
            if duration:
                track.duration = duration

            media = self._instance.media_new(playable_url)
            self._player.set_media(media)
            self._player.play()
            self._player.audio_set_volume(self._volume)
            self._current = track
            logger.info(f"Now playing: {track.title}")
        except Exception as e:
            logger.error(f"Failed to play track: {e}")
            self._current = None
            # Try next track if available
            if self._queue:
                next_track = self._queue.pop(0)
                self._play_track(next_track)

    def add_track(self, path: str, track_type: TrackType) -> Track:
        """Add a track to the queue. Starts playing if nothing is playing."""
        # Resolve metadata before acquiring lock (network I/O)
        if track_type == TrackType.YOUTUBE:
            from app.youtube import extract_youtube_metadata

            title, duration = extract_youtube_metadata(path)
        elif track_type == TrackType.SPOTIFY:
            from app.spotify import extract_spotify_metadata

            title, duration = extract_spotify_metadata(path)
        else:
            title = Path(path).stem
            duration = None

        with self._lock:
            track = Track(
                id=self._next_id(),
                path=path,
                title=title,
                type=track_type,
                duration=duration,
            )
            if self._current is None and not self._queue:
                self._play_track(track)
            else:
                self._queue.append(track)
                logger.info(f"Queued: {track.title}")
            self._save_state()
            return track

    def play(self):
        """Resume playback."""
        with self._lock:
            if self._current:
                self._player.play()
            elif self._queue:
                track = self._queue.pop(0)
                self._play_track(track)
            self._save_state()

    def pause(self):
        """Pause playback."""
        with self._lock:
            self._player.pause()

    def stop(self):
        """Stop playback."""
        with self._lock:
            self._player.stop()
            self._record_history()
            self._current = None
            self._save_state()

    def skip(self):
        """Skip to the next track."""
        with self._lock:
            self._record_history()
            self._player.stop()
            if self._queue:
                track = self._queue.pop(0)
                self._play_track(track)
            else:
                self._current = None
                logger.info("Queue empty, playback stopped")
            self._save_state()

    def previous(self):
        """Restart current track (no history tracking)."""
        with self._lock:
            if self._current:
                self._player.set_position(0)

    def set_volume(self, volume: int):
        """Set volume (0-100)."""
        with self._lock:
            self._volume = max(0, min(100, volume))
            self._player.audio_set_volume(self._volume)
            self._save_state()

    def seek(self, position: float):
        """Seek to a position in seconds."""
        with self._lock:
            if self._current and self._player.get_length() > 0:
                length_sec = self._player.get_length() / 1000.0
                if 0 <= position <= length_sec:
                    self._player.set_position(position / length_sec)

    def remove_track(self, track_id: int) -> bool:
        """Remove a track from the queue by ID."""
        with self._lock:
            for i, track in enumerate(self._queue):
                if track.id == track_id:
                    self._queue.pop(i)
                    self._save_state()
                    return True
            return False

    def clear_queue(self):
        """Clear the entire queue."""
        with self._lock:
            self._queue.clear()
            self._save_state()

    def move_track(self, track_id: int, new_position: int) -> bool:
        """Move a track to a new position in the queue."""
        with self._lock:
            for i, track in enumerate(self._queue):
                if track.id == track_id:
                    self._queue.pop(i)
                    new_pos = max(0, min(len(self._queue), new_position))
                    self._queue.insert(new_pos, track)
                    self._save_state()
                    return True
            return False

    def get_history(self) -> list[HistoryEntry]:
        """Get playback history."""
        with self._lock:
            return [HistoryEntry(**e) for e in self._history]

    def clear_history(self):
        """Clear playback history."""
        with self._lock:
            self._history.clear()
            self._save_state()

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
            )
            if self._current is None and not self._queue:
                self._play_track(track)
            else:
                self._queue.append(track)
            self._save_state()
            return track

    def get_state(self) -> PlayerState:
        """Get current player state."""
        with self._lock:
            length = self._player.get_length()
            position = self._player.get_time()
            is_playing = self._player.is_playing() == 1

            if self._current and length > 0:
                self._current.duration = length / 1000.0

            return PlayerState(
                current_track=self._current,
                queue=list(self._queue),
                is_playing=is_playing,
                volume=self._volume,
                position=max(0, position / 1000.0) if position >= 0 else 0.0,
                duration=length / 1000.0 if length > 0 else 0.0,
            )
