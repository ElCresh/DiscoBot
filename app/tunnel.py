"""Quick-tunnel pubblico via cloudflared.

DiscoBot gestisce il binario `vendor/cloudflared` come subprocess. Quando
l'utente clicca "Avvia tunnel" nella UI, partiamo cloudflared in modalita'
quick-tunnel (no account, URL random `*.trycloudflare.com`), parsiamo lo
stderr per estrarre l'URL e lo esponiamo al frontend.

Lifecycle: mai automatico, sempre on-demand. Cleanup automatico allo
shutdown di DiscoBot via atexit + SIGTERM.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CLOUDFLARED_PATH = Path("vendor/cloudflared")
URL_REGEX = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
START_TIMEOUT_S = 30.0


class TunnelManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._url: str | None = None
        self._error: str | None = None
        self._started_at: str | None = None
        atexit.register(self._atexit_cleanup)

    @property
    def binary_present(self) -> bool:
        return CLOUDFLARED_PATH.is_file() and os.access(CLOUDFLARED_PATH, os.X_OK)

    def status(self) -> dict:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            # Auto-detect crashed process
            if self._process is not None and not running and self._url is not None:
                self._error = self._error or "Subprocess terminato"
                self._url = None
                self._started_at = None
            return {
                "running": running,
                "url": self._url,
                "error": self._error,
                "started_at": self._started_at,
                "binary_present": self.binary_present,
            }

    def start(self, local_port: int) -> dict:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._status_unlocked()
            if not self.binary_present:
                self._error = (
                    "cloudflared non trovato in vendor/. Esegui ./setup.sh per scaricarlo."
                )
                return self._status_unlocked()

            self._url = None
            self._error = None
            cmd = [
                str(CLOUDFLARED_PATH),
                "tunnel",
                "--url", f"http://127.0.0.1:{local_port}",
                "--no-autoupdate",
                "--metrics", "127.0.0.1:0",  # disabilita metrics su porta fissa
            ]
            try:
                # cloudflared logga su stderr; uniamo stdout+stderr per semplicita'.
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._started_at = datetime.now(timezone.utc).isoformat()
                logger.info("cloudflared started, pid=%s", self._process.pid)
            except Exception as e:
                self._error = f"Avvio fallito: {e}"
                self._process = None
                return self._status_unlocked()

            # Reader thread: parse stdout per trovare l'URL pubblico
            self._reader_thread = threading.Thread(
                target=self._read_output, daemon=True, name="tunnel-reader"
            )
            self._reader_thread.start()
            return self._status_unlocked()

    def stop(self) -> dict:
        with self._lock:
            proc = self._process
            self._process = None
            self._url = None
            self._started_at = None

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                logger.info("cloudflared stopped")
            except Exception:
                logger.exception("Error stopping cloudflared")
        return self.status()

    def wait_for_url(self, timeout: float = START_TIMEOUT_S) -> str | None:
        """Wait until URL is available, or timeout. Used by tests/sync clients."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._url:
                return self._url
            if self._process is None or self._process.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    # --- internals ---

    def _status_unlocked(self) -> dict:
        running = self._process is not None and self._process.poll() is None
        return {
            "running": running,
            "url": self._url,
            "error": self._error,
            "started_at": self._started_at,
            "binary_present": self.binary_present,
        }

    def _read_output(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        deadline = time.monotonic() + START_TIMEOUT_S
        try:
            for line in proc.stdout:
                if not self._url:
                    m = URL_REGEX.search(line)
                    if m:
                        with self._lock:
                            self._url = m.group(0)
                        logger.info("Tunnel URL: %s", self._url)
                # Continua a drainare stdout per evitare backpressure
                if proc.poll() is not None:
                    break
                # Se l'URL non arriva entro START_TIMEOUT_S, segna errore
                if not self._url and time.monotonic() > deadline:
                    with self._lock:
                        self._error = "Timeout: URL pubblico non disponibile entro 30s"
                    self.stop()
                    return
        except Exception:
            logger.exception("tunnel reader thread crashed")

        # Subprocess terminato senza errori espliciti
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                if not self._error:
                    self._error = "Subprocess terminato"
                self._url = None

    def _atexit_cleanup(self) -> None:
        try:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except Exception:
                    self._process.kill()
        except Exception:
            pass


_singleton: TunnelManager | None = None


def get_tunnel() -> TunnelManager:
    global _singleton
    if _singleton is None:
        _singleton = TunnelManager()
    return _singleton
