"""Manager authentication.

Single-user, password-based, **stateful** session-cookie auth.
- Password hash + salt + session signing secret persisted in `manager_auth.json`.
- Active sessions persisted in `manager_sessions.json` as a list of records
  with id, timestamps, expires_at, ua_label, ip_first, ip_last.
- Cookie format: `<session_id_hex>.<HMAC(secret, session_id)>`.
- Brute force protection in-memory (resets on restart).

Stateless cookies of the previous version become invalid (good: forces
re-login after deploy). Migration is implicit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_FILE = Path("manager_auth.json")
SESSIONS_FILE = Path("manager_sessions.json")
COOKIE_NAME = "discobot_manager_session"

# scrypt parameters
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
SECRET_BYTES = 32
SESSION_ID_BYTES = 12  # 24 hex chars

REMEMBER_DURATION_S = 30 * 24 * 3600
SESSION_DURATION_S = 12 * 3600

LAST_SEEN_THROTTLE_S = 60.0
SAVE_THROTTLE_S = 5.0

MIN_PASSWORD_LEN = 8

MAX_FAILED_PER_IP = 5
WINDOW_S = 60.0


def _ua_label(ua: str) -> str:
    """Compatto, leggibile: 'Chrome 120 · macOS' / 'Safari · iPhone'."""
    if not ua:
        return "Sconosciuto"
    if "Edg/" in ua:
        browser = "Edge"
    elif "Chromium/" in ua and "Chrome/" not in ua:
        browser = "Chromium"
    elif "Chrome/" in ua and "Chromium" not in ua and "Edg" not in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua and "Chromium" not in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    m = re.search(r"(Edg|Chrome|Chromium|Firefox|Safari)/(\d+)", ua)
    version = m.group(2) if m else ""
    # Order matters: iPhone/iPad UA contengono "like Mac OS X"; Android UA
    # contengono "Linux". Mobili check per primi.
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua:
        os_ = "iPad"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_ = "macOS"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = ""
    parts = [f"{browser} {version}".strip()]
    if os_:
        parts.append(os_)
    return " · ".join(parts)


class AuthState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict | None = None
        self._sessions: dict[str, dict] = {}
        self._last_save = 0.0
        self._dirty = False
        self._failed_attempts: dict[str, list[float]] = {}
        self._load()
        self._load_sessions()

    # ---- persistence: auth ----

    def _load(self) -> None:
        try:
            if AUTH_FILE.is_file():
                self._data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                logger.info("Manager auth state loaded")
        except Exception:
            logger.exception("Failed to load %s; starting unconfigured", AUTH_FILE)
            self._data = None

    def _save_auth(self) -> None:
        try:
            tmp = AUTH_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(AUTH_FILE))
            try:
                os.chmod(AUTH_FILE, 0o600)
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to save %s", AUTH_FILE)

    # ---- persistence: sessions ----

    def _load_sessions(self) -> None:
        try:
            if SESSIONS_FILE.is_file():
                data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
                for s in data.get("sessions", []):
                    if "id" in s:
                        self._sessions[s["id"]] = s
                logger.info("Loaded %d active manager sessions", len(self._sessions))
        except Exception:
            logger.exception("Failed to load %s; starting with empty session store", SESSIONS_FILE)
            self._sessions = {}

    def _save_sessions(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_save) < SAVE_THROTTLE_S and not self._dirty_critical:
            self._dirty = True
            return
        try:
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            payload = {"sessions": list(self._sessions.values())}
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(SESSIONS_FILE))
            try:
                os.chmod(SESSIONS_FILE, 0o600)
            except Exception:
                pass
            self._last_save = now
            self._dirty = False
        except Exception:
            logger.exception("Failed to save %s", SESSIONS_FILE)

    @property
    def _dirty_critical(self) -> bool:
        # Critical writes (login, logout, revoke) bypass throttle.
        # Throttle si applica solo a last_seen updates.
        return False

    # ---- public API: password ----

    def is_configured(self) -> bool:
        with self._lock:
            return self._data is not None and "password_hash" in self._data

    def set_password(self, plain: str) -> None:
        if not plain or len(plain) < MIN_PASSWORD_LEN:
            raise ValueError(
                f"La password deve essere lunga almeno {MIN_PASSWORD_LEN} caratteri."
            )
        salt = secrets.token_bytes(SALT_BYTES)
        h = hashlib.scrypt(
            plain.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        )
        with self._lock:
            existing_secret = (self._data or {}).get("session_secret")
            self._data = {
                "version": 2,
                "password_hash": h.hex(),
                "salt": salt.hex(),
                "session_secret": existing_secret or secrets.token_hex(SECRET_BYTES),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_auth()
        self._failed_attempts.clear()

    def verify_password(self, plain: str) -> bool:
        with self._lock:
            if not self.is_configured():
                return False
            try:
                salt = bytes.fromhex(self._data["salt"])
                expected = bytes.fromhex(self._data["password_hash"])
            except Exception:
                return False
        h = hashlib.scrypt(
            plain.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        )
        return hmac.compare_digest(h, expected)

    def reset(self) -> None:
        """Cancella password + tutte le sessioni (CLI --reset-password)."""
        with self._lock:
            self._data = None
            self._sessions.clear()
            for f in (AUTH_FILE, SESSIONS_FILE):
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    logger.exception("Failed to delete %s", f)
            self._failed_attempts.clear()

    # ---- sessions ----

    def _secret(self) -> bytes:
        with self._lock:
            if not self._data or "session_secret" not in self._data:
                raise RuntimeError("Auth not configured: no session secret")
            return bytes.fromhex(self._data["session_secret"])

    def _sign(self, session_id: str) -> str:
        return hmac.new(
            self._secret(), session_id.encode("ascii"), hashlib.sha256
        ).hexdigest()

    def _purge_expired(self) -> int:
        """Rimuove sessioni scadute. Ritorna numero rimosse."""
        now = datetime.now(timezone.utc)
        with self._lock:
            removed = []
            for sid, s in list(self._sessions.items()):
                try:
                    exp = datetime.fromisoformat(s["expires_at"])
                except Exception:
                    removed.append(sid)
                    continue
                if exp < now:
                    removed.append(sid)
            for sid in removed:
                del self._sessions[sid]
            if removed:
                self._save_sessions(force=True)
        return len(removed)

    def make_session(
        self, remember: bool, user_agent: str, ip: str
    ) -> tuple[str, int | None]:
        """Crea sessione, persiste, ritorna (cookie_token, max_age_or_None)."""
        ttl = REMEMBER_DURATION_S if remember else SESSION_DURATION_S
        now = datetime.now(timezone.utc)
        sid = secrets.token_hex(SESSION_ID_BYTES)
        record = {
            "id": sid,
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "remember": bool(remember),
            "ua_label": _ua_label(user_agent or ""),
            "ip_first": ip or "",
            "ip_last": ip or "",
        }
        with self._lock:
            self._sessions[sid] = record
            self._save_sessions(force=True)
        token = f"{sid}.{self._sign(sid)}"
        max_age = REMEMBER_DURATION_S if remember else None
        return token, max_age

    def verify_session_token(self, token: str | None, ip: str = "") -> str | None:
        """Verifica firma + lookup store + check expiry. Aggiorna last_seen
        (throttled). Ritorna session_id se valido, None altrimenti."""
        if not token or "." not in token:
            return None
        try:
            sid, sig = token.split(".", 1)
        except Exception:
            return None
        try:
            expected = self._sign(sid)
        except RuntimeError:
            return None
        if not hmac.compare_digest(sig, expected):
            return None
        with self._lock:
            record = self._sessions.get(sid)
            if record is None:
                return None
            try:
                exp = datetime.fromisoformat(record["expires_at"])
            except Exception:
                del self._sessions[sid]
                self._save_sessions(force=True)
                return None
            now = datetime.now(timezone.utc)
            if exp < now:
                del self._sessions[sid]
                self._save_sessions(force=True)
                return None
            # Update last_seen (throttled)
            try:
                last = datetime.fromisoformat(record["last_seen"])
            except Exception:
                last = now - timedelta(days=1)
            if (now - last).total_seconds() > LAST_SEEN_THROTTLE_S:
                record["last_seen"] = now.isoformat()
                if ip and record.get("ip_last") != ip:
                    record["ip_last"] = ip
                self._save_sessions(force=False)  # throttled
            elif ip and record.get("ip_last") != ip:
                # IP change is interesting enough to write through
                record["ip_last"] = ip
                self._save_sessions(force=False)
        return sid

    def revoke_session(self, sid: str) -> bool:
        """Revoca singola. Ritorna True se trovata e rimossa."""
        with self._lock:
            if sid not in self._sessions:
                return False
            del self._sessions[sid]
            self._save_sessions(force=True)
        return True

    def revoke_all_except(self, keep_id: str | None = None) -> int:
        """Bulk revoke. Se keep_id None, revoca tutto. Ritorna numero revocate."""
        with self._lock:
            if keep_id is None:
                count = len(self._sessions)
                self._sessions.clear()
            else:
                to_remove = [sid for sid in self._sessions if sid != keep_id]
                for sid in to_remove:
                    del self._sessions[sid]
                count = len(to_remove)
            self._save_sessions(force=True)
        return count

    def list_sessions(self) -> list[dict]:
        """Snapshot delle sessioni attive ordinate per last_seen desc."""
        self._purge_expired()
        with self._lock:
            entries = list(self._sessions.values())
        entries.sort(key=lambda s: s.get("last_seen", ""), reverse=True)
        return entries

    # ---- brute force ----

    def check_rate_limit(self, ip: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            timestamps = self._failed_attempts.get(ip, [])
            timestamps = [t for t in timestamps if now - t < WINDOW_S]
            self._failed_attempts[ip] = timestamps
            if len(timestamps) >= MAX_FAILED_PER_IP:
                oldest = min(timestamps)
                wait = int(WINDOW_S - (now - oldest)) + 1
                return max(1, wait)
        return None

    def record_login_attempt(self, ip: str, ok: bool) -> None:
        if ok:
            with self._lock:
                self._failed_attempts.pop(ip, None)
            return
        with self._lock:
            self._failed_attempts.setdefault(ip, []).append(time.monotonic())


_singleton: AuthState | None = None


def get_auth() -> AuthState:
    global _singleton
    if _singleton is None:
        _singleton = AuthState()
    return _singleton
