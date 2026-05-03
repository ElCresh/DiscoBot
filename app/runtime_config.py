"""Runtime config persisted in JSON.

Convention: settings that may need to be toggled live (during a DJ session)
without restarting the app go here. Static deploy-time settings (host, port,
secrets) stay in app.config / .env.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILE = Path("runtime_config.json")

_DEFAULTS: dict[str, Any] = {
    "public_enabled": False,
    "public_require_approval": True,
    "public_sources": {
        "local": False,
        "youtube": True,
        "spotify": True,
        "soundcloud": True,
    },
    # Avvia automaticamente il tunnel cloudflared al boot di DiscoBot.
    "tunnel_autostart": False,
    # Autenticazione Manager. Se False:
    # - le route Manager sono accessibili senza login
    # - public_enabled e tunnel_autostart sono FORZATI a False e non
    #   riattivabili (esporre il Manager senza auth = regalare il pannello)
    "manager_auth_enabled": True,
}


class RuntimeConfig:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))
        self._load()

    def _load(self) -> None:
        try:
            if CONFIG_FILE.is_file():
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # Merge so missing keys fall back to defaults (forward-compat).
                self._merge_into(self._data, loaded)
                logger.info("Runtime config loaded from %s", CONFIG_FILE)
            else:
                # First boot: persist the defaults so the file is editable.
                self._save()
        except Exception:
            logger.exception("Failed to load %s, using defaults", CONFIG_FILE)

    @staticmethod
    def _merge_into(base: dict, src: dict) -> None:
        for k, v in src.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                RuntimeConfig._merge_into(base[k], v)
            else:
                base[k] = v

    def _save(self) -> None:
        try:
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(CONFIG_FILE))
        except Exception:
            logger.exception("Failed to save %s", CONFIG_FILE)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def public_view(self) -> dict[str, Any]:
        # Subset esposto a /public/config — niente di sensibile da nascondere
        # qui, ma la divisione tiene ordinata l'API e rende esplicito cosa
        # passa al pubblico.
        with self._lock:
            return {
                "enabled": self._data["public_enabled"],
                "require_approval": self._data["public_require_approval"],
                "sources": dict(self._data["public_sources"]),
            }

    def patch(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            # Pre-validation: alcune transizioni sono vietate per sicurezza.
            auth_enabled_after = self._data["manager_auth_enabled"]
            if "manager_auth_enabled" in updates:
                auth_enabled_after = bool(updates["manager_auth_enabled"])

            if not auth_enabled_after:
                # Disabilitazione auth → cascade off di public e tunnel autostart.
                # Se erano on, li forziamo off in modo silente (effetto collaterale
                # documentato del flag).
                if "public_enabled" in updates and bool(updates["public_enabled"]):
                    raise ValueError(
                        "Abilita prima l'autenticazione Manager per attivare l'interfaccia pubblica."
                    )
                if "tunnel_autostart" in updates and bool(updates["tunnel_autostart"]):
                    raise ValueError(
                        "Abilita prima l'autenticazione Manager per attivare l'avvio automatico del tunnel."
                    )

            for key, value in updates.items():
                if key not in self._data:
                    raise KeyError(f"Unknown config key: {key}")
                if key == "public_sources":
                    if not isinstance(value, dict):
                        raise ValueError("public_sources must be a dict")
                    for src_key, enabled in value.items():
                        if src_key not in self._data["public_sources"]:
                            raise KeyError(f"Unknown source: {src_key}")
                        self._data["public_sources"][src_key] = bool(enabled)
                elif isinstance(self._data[key], bool):
                    self._data[key] = bool(value)
                else:
                    self._data[key] = value

            # Post: se manager_auth_enabled è False, cascade off
            if not self._data["manager_auth_enabled"]:
                self._data["public_enabled"] = False
                self._data["tunnel_autostart"] = False

            self._save()
            # Effetto collaterale runtime: se il tunnel è running, fermalo.
            # Lazy import per evitare cicli con app.tunnel.
            if not self._data["manager_auth_enabled"]:
                try:
                    from app.tunnel import get_tunnel
                    if get_tunnel().status().get("running"):
                        get_tunnel().stop()
                except Exception:
                    pass
            return json.loads(json.dumps(self._data))

    # Convenience accessors
    @property
    def public_enabled(self) -> bool:
        with self._lock:
            return bool(self._data["public_enabled"])

    @property
    def public_require_approval(self) -> bool:
        with self._lock:
            return bool(self._data["public_require_approval"])

    @property
    def tunnel_autostart(self) -> bool:
        with self._lock:
            return bool(self._data.get("tunnel_autostart", False))

    @property
    def manager_auth_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("manager_auth_enabled", True))

    def is_source_enabled_for_public(self, src: str) -> bool:
        with self._lock:
            return bool(self._data["public_sources"].get(src, False))


_singleton: RuntimeConfig | None = None


def get_runtime_config() -> RuntimeConfig:
    global _singleton
    if _singleton is None:
        _singleton = RuntimeConfig()
    return _singleton
