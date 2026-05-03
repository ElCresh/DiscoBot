"""Suspend & screen-blank inhibitor.

Mantiene il sistema sveglio e il monitor acceso finche' DiscoBot e' in
esecuzione. Backend per OS:

- Linux: subprocess `systemd-inhibit` che blocca idle:sleep:idle. Il blocco
  rilascia automaticamente quando il subprocess termina (kill o terminate).
- Windows: SetThreadExecutionState con ES_DISPLAY_REQUIRED + ES_SYSTEM_REQUIRED.
- macOS: IOPMAssertionCreateWithName via IOKit (best-effort).

API minimale e idempotente: start() / stop() / is_active(). Se il backend
non e' disponibile (es. systemd-inhibit mancante), log warning e l'app
continua senza inhibitor.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

REASON = "DiscoBot Presenter attivo"
WHO = "DiscoBot"


class _NullBackend:
    name = "null"

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_active(self) -> bool:
        return False


class _LinuxBackend:
    """Doppia copertura per essere robusti su tutti i DE Linux:

    1) `systemd-inhibit` blocca le azioni di logind (suspend automatico,
       chiusura coperchio, tasto power). Copre il livello sistema.
    2) D-Bus `org.freedesktop.ScreenSaver.Inhibit` blocca il blank/dim
       gestito dal desktop environment (GNOME, KDE, XFCE, Cinnamon, MATE
       espongono questo nome standard). Copre il livello DE.

    Senza (2), GNOME e altri DE possono spegnere lo schermo o triggerare
    suspend tramite il proprio idle timer ignorando il lock di logind.
    """

    name = "systemd-inhibit + DBus ScreenSaver"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._dbus_iface = None
        self._dbus_cookie: int | None = None

    def start(self) -> None:
        # 1) systemd-inhibit (logind level)
        if self._proc is None or self._proc.poll() is not None:
            if not shutil.which("systemd-inhibit"):
                raise RuntimeError("systemd-inhibit non installato")
            cmd = [
                "systemd-inhibit",
                # idle:        blocca l'idle timer di logind
                # sleep:       blocca le chiamate Suspend (anche dal DE)
                # handle-lid-switch: chiusura coperchio = niente suspend
                # handle-power-key:  pulsante power = niente shutdown
                # handle-suspend-key: idem per il tasto sleep
                "--what=idle:sleep:handle-lid-switch:handle-power-key:handle-suspend-key",
                f"--who={WHO}",
                f"--why={REASON}",
                "--mode=block",
                "cat",
            ]
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # 2) D-Bus ScreenSaver Inhibit (DE level)
        if self._dbus_iface is None:
            self._start_dbus_screensaver()

    def _start_dbus_screensaver(self) -> None:
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface
        except Exception as e:
            logger.debug("QtDBus non disponibile: %s", e)
            return
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                logger.debug("Session bus D-Bus non connesso")
                return
            iface = QDBusInterface(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver",
                bus,
            )
            if not iface.isValid():
                logger.debug("org.freedesktop.ScreenSaver non disponibile")
                return
            reply = iface.call("Inhibit", WHO, REASON)
            if reply.errorName():
                logger.debug(
                    "ScreenSaver.Inhibit ha risposto errore: %s",
                    reply.errorName(),
                )
                return
            args = reply.arguments()
            if not args:
                return
            self._dbus_iface = iface
            self._dbus_cookie = int(args[0])
            logger.debug("ScreenSaver inhibit cookie=%s", self._dbus_cookie)
        except Exception:
            logger.exception("Errore D-Bus ScreenSaver inhibit")

    def stop(self) -> None:
        # 2) Rilascia D-Bus inhibit
        if self._dbus_iface is not None and self._dbus_cookie is not None:
            try:
                self._dbus_iface.call("UnInhibit", self._dbus_cookie)
            except Exception:
                logger.exception("Errore UnInhibit D-Bus")
            self._dbus_iface = None
            self._dbus_cookie = None

        # 1) Termina systemd-inhibit
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    try: self._proc.stdin.close()
                    except Exception: pass
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1)
            except Exception:
                logger.exception("Errore terminando systemd-inhibit")
        self._proc = None

    def is_active(self) -> bool:
        si_alive = self._proc is not None and self._proc.poll() is None
        dbus_alive = self._dbus_cookie is not None
        return si_alive or dbus_alive


class _WindowsBackend:
    name = "SetThreadExecutionState"

    # Da winbase.h
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(self) -> None:
        self._active = False

    def start(self) -> None:
        if self._active:
            return
        import ctypes
        flags = self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_DISPLAY_REQUIRED
        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if result == 0:
            raise RuntimeError("SetThreadExecutionState ha ritornato 0")
        self._active = True

    def stop(self) -> None:
        if not self._active:
            return
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        self._active = False

    def is_active(self) -> bool:
        return self._active


class _MacOSBackend:
    name = "IOPMAssertion"

    def __init__(self) -> None:
        self._assertion_id = None

    def start(self) -> None:
        if self._assertion_id is not None:
            return
        import ctypes
        import ctypes.util
        iokit_path = ctypes.util.find_library("IOKit")
        if not iokit_path:
            raise RuntimeError("IOKit framework non trovato")
        iokit = ctypes.cdll.LoadLibrary(iokit_path)
        cf_path = ctypes.util.find_library("CoreFoundation")
        cf = ctypes.cdll.LoadLibrary(cf_path)

        # CFStringCreateWithCString
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        kCFStringEncodingUTF8 = 0x08000100
        type_str = cf.CFStringCreateWithCString(
            None, b"NoDisplaySleepAssertion", kCFStringEncodingUTF8,
        )
        reason_str = cf.CFStringCreateWithCString(
            None, REASON.encode("utf-8"), kCFStringEncodingUTF8,
        )

        iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int
        iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kIOPMAssertionLevelOn = 255
        aid = ctypes.c_uint32(0)
        rc = iokit.IOPMAssertionCreateWithName(
            type_str, kIOPMAssertionLevelOn, reason_str, ctypes.byref(aid),
        )
        if rc != 0:
            raise RuntimeError(f"IOPMAssertionCreateWithName failed: {rc}")
        self._assertion_id = aid.value
        self._iokit = iokit

    def stop(self) -> None:
        if self._assertion_id is None:
            return
        try:
            self._iokit.IOPMAssertionRelease(self._assertion_id)
        except Exception:
            logger.exception("Errore rilasciando IOPMAssertion")
        self._assertion_id = None

    def is_active(self) -> bool:
        return self._assertion_id is not None


class SuspendInhibitor:
    """Facade cross-platform."""

    def __init__(self) -> None:
        if sys.platform == "linux":
            self._backend = _LinuxBackend()
        elif sys.platform == "win32":
            self._backend = _WindowsBackend()
        elif sys.platform == "darwin":
            self._backend = _MacOSBackend()
        else:
            self._backend = _NullBackend()

    def start(self) -> None:
        try:
            self._backend.start()
            if self._backend.is_active():
                logger.info(
                    "Sospensione e oscuramento monitor inibiti via %s",
                    self._backend.name,
                )
        except Exception as e:
            logger.warning(
                "Inhibitor non disponibile (%s): %s. "
                "DiscoBot non blocchera' la sospensione.",
                self._backend.name, e,
            )
            # Fallback a null: chiamate successive a start()/stop() no-op
            self._backend = _NullBackend()

    def stop(self) -> None:
        try:
            was_active = self._backend.is_active()
            self._backend.stop()
            if was_active:
                logger.info("Inhibitor rilasciato")
        except Exception:
            logger.exception("Errore rilasciando inhibitor")

    def is_active(self) -> bool:
        return self._backend.is_active()


_singleton: SuspendInhibitor | None = None


def get_inhibitor() -> SuspendInhibitor:
    """Factory singleton: cosi' tray e main.py condividono la stessa istanza
    e i `stop()` ridondanti su quit sono no-op idempotenti."""
    global _singleton
    if _singleton is None:
        _singleton = SuspendInhibitor()
    return _singleton
