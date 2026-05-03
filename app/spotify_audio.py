"""Direct Spotify audio streaming via librespot-python.

Replaces the previous YouTube-proxy hack: a Spotify track ID is fed to
librespot, which decrypts the official OGG/Vorbis stream. The bytes flow
out to the loopback HTTP endpoint, which VLC opens like any other URL.

Also hosts the Zeroconf-based remote login flow: ZeroconfBootstrap exposes
DiscoBot as a Spotify Connect device on the LAN; when the operator picks
it from the Spotify app on their phone, librespot auto-persists the new
credentials and the bootstrap auto-stops.
"""

import logging
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 64 * 1024
# Small grace period before tearing down the Zeroconf server after a
# successful capture — the Spotify Connect handshake sends a couple of
# trailing packets we don't want to interrupt.
_ZEROCONF_AUTOSTOP_DELAY_S = 2.0


def _detect_lan_ip() -> str | None:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _patch_librespot_zeroconf_loopback() -> None:
    # Su Debian/Ubuntu /etc/hosts mappa l'hostname a 127.0.1.1, quindi
    # librespot-python pubblicizza il device Connect con un IP loopback e il
    # telefono non lo vede. Sostituiamo gethostbyname solo nel namespace di
    # librespot.zeroconf con un fallback all'IP LAN reale.
    import librespot.zeroconf as _lzc
    if getattr(_lzc, "_discobot_patched", False):
        return
    _orig = _lzc.socket.gethostbyname

    def _gethostbyname(host):
        addr = _orig(host)
        if addr.startswith("127."):
            lan = _detect_lan_ip()
            if lan and not lan.startswith("127."):
                return lan
        return addr

    _lzc.socket.gethostbyname = _gethostbyname
    _lzc._discobot_patched = True


class SpotifyAudio:
    """Singleton wrapper around a librespot Session.

    A single Session and a process-wide lock guard serial access — playback
    is sequential by design (one VLC track at a time).
    """

    def __init__(self):
        self._session = None  # lazy
        self._lock = threading.Lock()
        # Lifecycle visible to the UI: idle → warming → ready / failed.
        # Reset to idle on invalidate_session(); transitions inside _build_session().
        self._session_status: str = "idle"

    def is_authenticated(self) -> bool:
        return Path(settings.spotify_credentials_file).is_file()

    @property
    def session_status(self) -> str:
        return self._session_status

    def _build_session(self):
        from librespot.core import Session

        creds = Path(settings.spotify_credentials_file).resolve()
        if not creds.is_file():
            raise RuntimeError(
                f"Spotify not authenticated: {creds} missing. "
                f"Run: python main.py --spotify-login"
            )
        conf = (
            Session.Configuration.Builder()
            .set_stored_credential_file(str(creds))
            .build()
        )
        self._session_status = "warming"
        try:
            session = Session.Builder(conf).stored_file(str(creds)).create()
            self._session_status = "ready"
            return session
        except Exception:
            self._session_status = "failed"
            raise

    def _get_session(self):
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def invalidate_session(self) -> None:
        """Drop the cached Session so the next play rebuilds from disk.

        Called after a fresh login (CLI or Zeroconf) writes new credentials.
        Safe lock-free: attribute assignment is atomic; a stream in progress
        keeps using its own local Session reference until it finishes.
        """
        self._session = None
        self._session_status = "idle"

    def prewarm(self) -> None:
        """Establish the librespot session ahead of time.

        librespot's first session.create() does a network handshake with the
        Spotify AP servers (Authentication, AP welcome, key exchange) that
        usually costs 1-3 seconds. Doing it eagerly at boot — instead of on
        first play — removes that latency from the user-visible "press play
        to first audio" path.
        """
        if not self.is_authenticated():
            return
        # Retry breve sugli errori di rete: capita che all'avvio la rete non
        # sia ancora pronta (sleep/wake, DHCP lento, Spotify AP transient).
        # Errori non-network (auth, credenziali corrotte) escono subito.
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with self._lock:
                    if self._session is not None:
                        return
                    self._session = self._build_session()
                    logger.info("Spotify librespot session prewarmed")
                    return
            except (ConnectionError, OSError) as e:
                last_err = e
                logger.warning(
                    "Spotify prewarm: connessione fallita (tentativo %d/3): %s",
                    attempt + 1, e,
                )
                if attempt < 2:
                    time.sleep(2)
            except Exception as e:
                logger.warning(
                    "Spotify prewarm fallito (verra' ritentato al primo play): %s", e
                )
                return
        logger.warning(
            "Spotify prewarm fallito dopo 3 tentativi (verra' ritentato al primo play): %s",
            last_err,
        )

    def open_track_stream(self, track_id: str) -> Iterator[bytes]:
        """Open a Spotify track and return a chunk generator.

        Session lookup and `load()` happen synchronously here so any failure
        raises BEFORE the FastAPI endpoint commits the response — the endpoint
        can then return a clean HTTP 5xx instead of a half-streamed body that
        VLC sees as a mid-flight reset.

        Stale-session recovery: Spotify AP closes idle TCP connections after
        a few minutes. The first request on a stale session fails with
        ConnectionResetError. We catch that, drop the cached session, rebuild
        once, and retry. If the second attempt still fails, we raise.

        The session lock is held for the whole stream lifetime — playback is
        sequential by design (one VLC track at a time) and librespot Sessions
        are not safe for concurrent use.
        """
        from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
        from librespot.metadata import TrackId

        self._lock.acquire()
        chunked = None
        try:
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    session = self._get_session()
                    playable = TrackId.from_uri(f"spotify:track:{track_id}")
                    loaded = session.content_feeder().load(
                        playable,
                        VorbisOnlyAudioQuality(AudioQuality.VERY_HIGH),
                        False,
                        None,
                    )
                    chunked = loaded.input_stream.stream()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "librespot load failed (attempt %d/2): %r", attempt + 1, e
                    )
                    # Most failures here are stale session / connection reset.
                    # Drop the cached session so the next attempt rebuilds.
                    self._session = None
                    self._session_status = "idle"
            if chunked is None:
                self._session_status = "failed"
                assert last_err is not None
                raise last_err
        except BaseException:
            self._lock.release()
            raise

        def _gen() -> Iterator[bytes]:
            try:
                while True:
                    chunk = chunked.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    chunked.close()
                except Exception:
                    pass
                self._lock.release()

        return _gen()


_audio: SpotifyAudio | None = None


def get_audio() -> SpotifyAudio:
    global _audio
    if _audio is None:
        _audio = SpotifyAudio()
    return _audio


def bootstrap_login() -> Path:
    """Run the OAuth flow and persist credentials. Returns the file path.

    Opens the user's browser to Spotify's authorization page. On success,
    librespot auto-writes credentials to settings.spotify_credentials_file
    (auto-persistence enabled by default in Configuration). Idempotent: if
    a valid file already exists, librespot reuses it without prompting.
    """
    from librespot.core import Session

    creds = Path(settings.spotify_credentials_file).resolve()
    creds.parent.mkdir(parents=True, exist_ok=True)

    conf = (
        Session.Configuration.Builder()
        .set_stored_credential_file(str(creds))
        .set_store_credentials(True)
        .build()
    )

    def _open(url: str) -> None:
        print(
            f"\nOpen this URL in your browser to authorize DiscoBot:\n{url}\n",
            flush=True,
        )
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass

    print("Starting Spotify OAuth login...", flush=True)
    session = Session.Builder(conf).oauth(_open).create()
    try:
        session.close()
    except Exception:
        logger.debug("session.close() failed", exc_info=True)
    print(f"Spotify login OK. Credentials saved to {creds}", flush=True)
    return creds


# ---- Zeroconf (Spotify Connect) bootstrap for remote login ----------------


class _SessionListener:
    """Adapter for ZeroconfServer.add_session_listener.

    librespot expects an object with `session_changed` and `session_closing`
    methods; we forward only the "new login captured" event upstream.
    """

    def __init__(self, on_login):
        self._on_login = on_login

    def session_changed(self, session) -> None:
        try:
            self._on_login()
        except Exception:
            logger.exception("Zeroconf on_login handler failed")

    def session_closing(self, session) -> None:
        pass


class ZeroconfBootstrap:
    """Run a Spotify Connect Zeroconf endpoint to capture credentials remotely.

    Single instance per process. The server stays up only between `start()`
    and either an automatic teardown after the first successful login or an
    explicit `stop()`. While running, DiscoBot appears in the Spotify app's
    device list on the same LAN; selecting it triggers credential delivery,
    which librespot auto-persists into `settings.spotify_credentials_file`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._server = None
        self._device_name = "DiscoBot"
        self._started_at: str | None = None

    def is_running(self) -> bool:
        return self._server is not None

    def status(self) -> dict:
        return {
            "running": self._server is not None,
            "device_name": self._device_name,
            "started_at": self._started_at,
        }

    def start(self, device_name: str = "DiscoBot") -> dict:
        from librespot.core import Session
        _patch_librespot_zeroconf_loopback()
        from librespot.zeroconf import ZeroconfServer

        with self._lock:
            if self._server is not None:
                return self.status()

            creds = Path(settings.spotify_credentials_file).resolve()
            creds.parent.mkdir(parents=True, exist_ok=True)
            conf = (
                Session.Configuration.Builder()
                .set_stored_credential_file(str(creds))
                .set_store_credentials(True)
                .build()
            )
            server = (
                ZeroconfServer.Builder(conf)
                .set_device_name(device_name)
                .create()
            )
            server.add_session_listener(_SessionListener(self._on_login))
            self._server = server
            self._device_name = device_name
            self._started_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Zeroconf Spotify Connect started as '%s' (waiting for login)",
                device_name,
            )
            lan = _detect_lan_ip()
            if lan:
                logger.info(
                    "Annunciato su IP LAN: %s — assicurati che il telefono sia sulla stessa rete",
                    lan,
                )
            return self.status()

    def _on_login(self) -> None:
        # The internal Session has already auto-persisted credentials via
        # __authenticate_partial(). Drop the cached SpotifyAudio session so
        # the next play picks up the fresh creds, then warm a fresh one in
        # the background so the next play doesn't pay the handshake latency.
        audio = get_audio()
        audio.invalidate_session()
        logger.info("Spotify Connect login captured; auto-stop scheduled")
        threading.Thread(target=self._delayed_stop, daemon=True).start()
        threading.Thread(target=audio.prewarm, daemon=True).start()

    def _delayed_stop(self) -> None:
        time.sleep(_ZEROCONF_AUTOSTOP_DELAY_S)
        self.stop()

    def stop(self) -> dict:
        with self._lock:
            if self._server is None:
                return self.status()
            try:
                self._server.close()
            except Exception:
                logger.debug("ZeroconfServer.close failed", exc_info=True)
            self._server = None
            self._started_at = None
            logger.info("Zeroconf Spotify Connect stopped")
            return self.status()


_zeroconf: ZeroconfBootstrap | None = None


def get_zeroconf() -> ZeroconfBootstrap:
    global _zeroconf
    if _zeroconf is None:
        _zeroconf = ZeroconfBootstrap()
    return _zeroconf
