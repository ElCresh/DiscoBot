"""Pre-flight dependency check, eseguito prima dei moduli pesanti.

Verifica che ogni pacchetto in requirements.txt sia installato (solo presenza,
non versione) e che libvlc nativo risponda. Su errore stampa un messaggio
specifico per OS e fa exit(1) prima che Qt/uvicorn vengano importati.

Opt-out: env DISCOBOT_SKIP_DEPCHECK=1.
"""

from __future__ import annotations

import glob
import importlib.metadata
import platform
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS_FILE = _PROJECT_ROOT / "requirements.txt"
_SOUNDFONTS_DIR = _PROJECT_ROOT / "soundfonts"
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Linux: fluidsynth è un plugin separato (vlc-plugin-fluidsynth). Su macOS e
# Windows è incluso nei build ufficiali di VLC, quindi il check è Linux-only.
_FLUIDSYNTH_PLUGIN_GLOBS = (
    "/usr/lib/*/vlc/plugins/**/*fluidsynth*",
    "/usr/lib/vlc/plugins/**/*fluidsynth*",
    "/usr/lib64/vlc/plugins/**/*fluidsynth*",
    "/usr/local/lib/vlc/plugins/**/*fluidsynth*",
)


def _parse_requirement_names(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _REQ_NAME_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


def _check_python_packages() -> list[str]:
    missing: list[str] = []
    for name in _parse_requirement_names(_REQUIREMENTS_FILE):
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return missing


def _check_libvlc() -> str | None:
    try:
        import vlc  # type: ignore[import-not-found]
        vlc.Instance()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _has_soundfont() -> bool:
    return _SOUNDFONTS_DIR.is_dir() and any(_SOUNDFONTS_DIR.glob("*.sf2"))


def _has_fluidsynth_plugin() -> bool:
    if platform.system() != "Linux":
        return True
    for pattern in _FLUIDSYNTH_PLUGIN_GLOBS:
        if glob.glob(pattern, recursive=True):
            return True
    return False


def _fluidsynth_install_hint() -> str:
    if platform.system() == "Linux":
        return (
            "sudo apt install vlc-plugin-fluidsynth   # Debian/Ubuntu\n"
            "           sudo dnf install vlc-plugin-fluidsynth   # Fedora"
        )
    return "Reinstalla VLC dal sito ufficiale (videolan.org)"


def _hints_for_os() -> tuple[str, str]:
    system = platform.system()
    if system == "Linux":
        pip_hint = "./setup.sh  (oppure: pip install -r requirements.txt)"
        vlc_hint = "sudo apt install vlc libvlc-dev   # Debian/Ubuntu\n           sudo dnf install vlc-devel          # Fedora"
    elif system == "Darwin":
        pip_hint = "./setup.sh  (oppure: pip install -r requirements.txt)"
        vlc_hint = "brew install --cask vlc"
    elif system == "Windows":
        pip_hint = "setup.bat  (oppure: pip install -r requirements.txt)"
        vlc_hint = "Scarica e installa VLC 64-bit da https://www.videolan.org/vlc/"
    else:
        pip_hint = "pip install -r requirements.txt"
        vlc_hint = "Installa VLC media player dal sito ufficiale (videolan.org)"
    return pip_hint, vlc_hint


def check_dependencies() -> None:
    missing_pkgs = _check_python_packages()

    libvlc_error: str | None = None
    if "python-vlc" not in missing_pkgs:
        libvlc_error = _check_libvlc()

    if missing_pkgs or libvlc_error is not None:
        pip_hint, vlc_hint = _hints_for_os()
        print("\nDiscoBot non puo' avviarsi: dipendenze mancanti.\n", file=sys.stderr)

        if missing_pkgs:
            print("Pacchetti Python non installati:", file=sys.stderr)
            for name in missing_pkgs:
                print(f"  - {name}", file=sys.stderr)
            print(f"\nPer installarli:\n    {pip_hint}\n", file=sys.stderr)

        if libvlc_error is not None:
            print(f"libvlc nativo non utilizzabile ({libvlc_error}).", file=sys.stderr)
            print(f"Per installarlo:\n    {vlc_hint}\n", file=sys.stderr)

        print("Per saltare questo controllo: DISCOBOT_SKIP_DEPCHECK=1", file=sys.stderr)
        sys.exit(1)

    if _has_soundfont() and not _has_fluidsynth_plugin():
        print(
            "\nAttenzione: trovato SoundFont in soundfonts/ ma il plugin "
            "fluidsynth di VLC non e' installato — la riproduzione MIDI "
            f"sara' muta.\nPer abilitarla:\n    {_fluidsynth_install_hint()}\n",
            file=sys.stderr,
        )
