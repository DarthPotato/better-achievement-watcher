"""Where Steam emulators / cracks keep their achievement saves."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# File names (lower case) that hold achievement progress.
SAVE_FILE_NAMES = {
    "achievements.json",
    "achievements.ini",
    "achiev.ini",
    "achieve.dat",
    "stats.ini",
    "user_stats.ini",
    "creamapi.achievements.cfg",
}


@dataclass(frozen=True)
class SaveRoot:
    """A folder containing one sub-folder per Steam AppID."""

    emulator: str
    path: Path
    # True when the folder contains wildcard user sub-folders before the AppID
    # (Reloaded / 3DM: %PROGRAMDATA%/Steam/<user>/<appid>/stats/achievements.ini).
    user_level: bool = False


def _env(name: str, fallback: Optional[str] = None) -> Optional[Path]:
    value = os.environ.get(name) or fallback
    return Path(value) if value else None


def documents_folder() -> Optional[Path]:
    """The real "My Documents" folder (may be redirected to OneDrive or another drive)."""
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            value, _ = winreg.QueryValueEx(key, "Personal")
            return Path(os.path.expandvars(value))
        except OSError:
            pass
    home = _env("USERPROFILE") or Path.home()
    return home / "Documents"


def default_roots() -> List[SaveRoot]:
    appdata = _env("APPDATA")
    local = _env("LOCALAPPDATA")
    public = _env("PUBLIC", "C:\\Users\\Public" if sys.platform == "win32" else None)
    programdata = _env("PROGRAMDATA")
    docs = documents_folder()

    roots: List[SaveRoot] = []

    def add(emulator: str, base: Optional[Path], *parts: str, user_level: bool = False) -> None:
        if base is not None:
            roots.append(SaveRoot(emulator, base.joinpath(*parts), user_level))

    add("Goldberg", appdata, "Goldberg SteamEmu Saves")
    add("GSE (gbe_fork)", appdata, "GSE Saves")
    add("CODEX", public, "Documents", "Steam", "CODEX")
    add("CODEX", appdata, "Steam", "CODEX")
    add("RUNE", public, "Documents", "Steam", "RUNE")
    add("RUNE", appdata, "Steam", "RUNE")
    add("EMPRESS", appdata, "EMPRESS")
    add("EMPRESS", public, "Documents", "EMPRESS")
    add("OnlineFix", public, "Documents", "OnlineFix")
    add("CreamAPI", appdata, "CreamAPI")
    add("SKIDROW", local, "SKIDROW")
    add("SKIDROW", docs, "SKIDROW")
    add("Reloaded / 3DM", programdata, "Steam", user_level=True)
    # De-duplicate while keeping order (e.g. when two env vars point at the same place).
    seen = set()
    unique = []
    for root in roots:
        key = str(root.path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def steam_install_dir() -> Optional[Path]:
    candidates: List[Path] = []
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            candidates.append(Path(value))
        except OSError:
            pass
        candidates.append(Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Steam")
    else:
        candidates += [Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]
    for path in candidates:
        if path.is_dir():
            return path
    return None
