"""Find achievement save files on disk."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import Config
from .locations import SAVE_FILE_NAMES, SaveRoot, default_roots

STEAM_API_DLLS = {"steam_api.dll", "steam_api64.dll"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "$recycle.bin", "windows", "system volume information"}
APPID_PATTERNS = [
    re.compile(r"^\s*real_?app_?id\s*=\s*(\d+)", re.I | re.M),
    re.compile(r"^\s*app_?id\s*=\s*(\d+)", re.I | re.M),
]


@dataclass(frozen=True)
class SaveFile:
    appid: int
    path: Path
    emulator: str


@dataclass
class GameDir:
    """A game install found by scanning the configured game folders."""

    appid: int
    path: Path
    name: str
    schema_file: Optional[Path] = None
    saves: List[SaveFile] = field(default_factory=list)


def _walk(base: Path, max_depth: int) -> Iterator[os.DirEntry]:
    """Yield file entries under base, at most max_depth directories deep."""
    stack = [(base, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name.lower() not in SKIP_DIRS:
                        stack.append((Path(entry.path), depth + 1))
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def _numeric_subdirs(path: Path) -> Iterator[Path]:
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.isdigit() and entry.is_dir():
                    yield Path(entry.path)
    except OSError:
        return


def _saves_in(folder: Path, appid: int, emulator: str, max_depth: int = 3) -> List[SaveFile]:
    found = []
    for entry in _walk(folder, max_depth):
        if entry.name.lower() in SAVE_FILE_NAMES:
            path = Path(entry.path)
            # steam_settings/achievements.json is the achievement list shipped with the
            # emulator, not a save.
            if path.parent.name.lower() == "steam_settings":
                continue
            found.append(SaveFile(appid, path, emulator))
    return found


def scan_root(root: SaveRoot) -> List[SaveFile]:
    saves: List[SaveFile] = []
    if not root.path.is_dir():
        return saves
    parents = [root.path]
    if root.user_level:
        try:
            parents = [Path(e.path) for e in os.scandir(root.path) if e.is_dir()]
        except OSError:
            parents = []
    for parent in parents:
        for appdir in _numeric_subdirs(parent):
            saves.extend(_saves_in(appdir, int(appdir.name), root.emulator))
    return saves


def save_roots(config: Config) -> List[SaveRoot]:
    roots = default_roots() if config.scan_default_locations else []
    roots += [SaveRoot("Custom", Path(os.path.expandvars(p))) for p in config.extra_save_roots]
    return roots


def scan_save_roots(config: Config) -> List[SaveFile]:
    saves: List[SaveFile] = []
    for root in save_roots(config):
        saves.extend(scan_root(root))
    return saves


# ---------------------------------------------------------------- game folders

def _read_small(path: Path, limit: int = 65536) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_appid(dll_dir: Path) -> Optional[int]:
    for folder in (dll_dir, dll_dir / "steam_settings", dll_dir.parent, dll_dir.parent.parent):
        text = _read_small(folder / "steam_appid.txt").strip()
        if text.split() and text.split()[0].isdigit():
            return int(text.split()[0])
    try:
        inis = [p for p in dll_dir.iterdir() if p.suffix.lower() in (".ini", ".cfg", ".txt")]
    except OSError:
        inis = []
    for pattern in APPID_PATTERNS:
        for ini in inis:
            match = pattern.search(_read_small(ini))
            if match and int(match.group(1)) > 0:
                return int(match.group(1))
    return None


def _guess_game_name(dll_dir: Path, library: Path) -> str:
    """The top-level folder under the library is usually the game's name."""
    try:
        rel = dll_dir.relative_to(library)
        if rel.parts:
            return rel.parts[0]
    except ValueError:
        pass
    return dll_dir.name


def scan_game_folder(library: Path, max_depth: int = 6) -> List[GameDir]:
    games: Dict[Path, GameDir] = {}
    for entry in _walk(library, max_depth):
        if entry.name.lower() not in STEAM_API_DLLS:
            continue
        dll_dir = Path(entry.path).parent
        if dll_dir in games:
            continue
        appid = detect_appid(dll_dir)
        if appid is None:
            continue
        schema = dll_dir / "steam_settings" / "achievements.json"
        game = GameDir(appid, dll_dir, _guess_game_name(dll_dir, library), schema if schema.is_file() else None)
        for save in _saves_in(dll_dir, appid, "Local save", max_depth=4):
            # Saves inside "<local save dir>/<appid>/" belong to that appid.
            numeric = [p for p in save.path.relative_to(dll_dir).parts[:-1] if p.isdigit()]
            owner = int(numeric[-1]) if numeric else appid
            game.saves.append(SaveFile(owner, save.path, save.emulator))
        games[dll_dir] = game
    return list(games.values())


def scan_game_folders(config: Config) -> List[GameDir]:
    games: List[GameDir] = []
    for folder in config.game_folders:
        path = Path(os.path.expandvars(folder))
        if path.is_dir():
            games.extend(scan_game_folder(path))
    return games
