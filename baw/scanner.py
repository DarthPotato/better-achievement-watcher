"""Find achievement save files on disk."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import Config
from .locations import SAVE_FILE_NAMES, SaveRoot, common_game_folders, default_roots, steam_install_dir
from .steam_local import STEAM_SOURCE, stats_files
from . import epic, gog, native, xbox

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
    schema: Optional[object] = None  # GameSchema shipped with the game (Xbox emulators)


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
    if config.gog_enabled:
        roots.append(SaveRoot(gog.SOURCE, gog.saves_root()))
    return roots


def scan_save_roots(config: Config) -> List[SaveFile]:
    saves: List[SaveFile] = []
    for root in save_roots(config):
        saves.extend(scan_root(root))
    if config.scan_default_locations:
        saves.extend(SaveFile(game.appid, path, game.source) for game, path in native.find_saves())
        saves.extend(SaveFile(epic.game_id(path), path, epic.SOURCE) for path in epic.find_saves())
    if config.include_steam_library:
        saves.extend(SaveFile(appid, path, STEAM_SOURCE) for appid, path in stats_files(steam_install_dir()))
    return saves


# ---------------------------------------------------------------- game folders

def _read_small(path: Path, limit: int = 65536) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _appid_from_txt(path: Path) -> Optional[int]:
    words = _read_small(path).split()
    return int(words[0]) if words and words[0].isdigit() and int(words[0]) > 0 else None


def detect_appid(dll_dir: Path, game_root: Optional[Path] = None) -> Optional[int]:
    for folder in (dll_dir, dll_dir / "steam_settings", dll_dir.parent, dll_dir.parent.parent):
        appid = _appid_from_txt(folder / "steam_appid.txt")
        if appid:
            return appid
    try:
        inis = [p for p in dll_dir.iterdir() if p.suffix.lower() in (".ini", ".cfg", ".txt")]
    except OSError:
        inis = []
    for pattern in APPID_PATTERNS:
        for ini in inis:
            match = pattern.search(_read_small(ini))
            if match and int(match.group(1)) > 0:
                return int(match.group(1))
    # Unreal games keep steam_api in Engine\Binaries\ThirdParty\... but steam_appid.txt
    # next to the game's exe, so look anywhere in the game's own folder.
    if game_root is not None and game_root != dll_dir:
        for entry in _walk(game_root, 5):
            if entry.name.lower() == "steam_appid.txt":
                appid = _appid_from_txt(Path(entry.path))
                if appid:
                    return appid
    return None


def _local_save_dirs(dll_dir: Path) -> List[Path]:
    """Save folders configured outside the game folder (Goldberg local_save.txt, gbe_fork configs.user.ini)."""
    found = []
    text = _read_small(dll_dir / "local_save.txt").strip()
    if text:
        found.append(text.splitlines()[0].strip())
    for ini in (dll_dir / "steam_settings" / "configs.user.ini", dll_dir / "steam_settings" / "configs.main.ini"):
        match = re.search(r"^\s*local_save_path\s*=\s*(.+?)\s*$", _read_small(ini), re.I | re.M)
        if match:
            found.append(match.group(1))
    # steam_emu.ini & co. sometimes name their own save folder (SavePath=, SaveDirectory=, ...).
    try:
        inis = [p for p in dll_dir.iterdir() if p.suffix.lower() == ".ini"]
    except OSError:
        inis = []
    for ini in inis:
        for match in re.finditer(r"^\s*\w*save\w*\s*=\s*(.*[\\/%].*?)\s*$", _read_small(ini), re.I | re.M):
            found.append(match.group(1))
    result = []
    for value in found:
        path = Path(os.path.expandvars(value.strip('"')))
        path = path if path.is_absolute() else dll_dir / path
        base = dll_dir.resolve()
        if path.is_dir() and base not in path.resolve().parents and path.resolve() != base:
            result.append(path)
    return result


# Uploader / release-group tags often appended to folder names ("Tiny-Glade-AnkerGames").
RELEASE_TAGS = {
    "ankergames", "fitgirl", "dodi", "elamigos", "codex", "rune", "empress", "tenoke", "plaza", "skidrow",
    "reloaded", "razor1911", "flt", "darksiders", "hoodlum", "prophet", "goldberg", "onlinefix", "steamrip",
    "gog", "repack", "multi", "portable", "gamesdrive",
}


def display_name(folder: str) -> str:
    """A folder name made presentable: "Tiny-Glade-AnkerGames" -> "Tiny Glade". Display only."""
    name = folder.strip()
    if " " not in name and any(c in name for c in "-_."):
        name = re.sub(r"[-_]+", " ", name)
        # Dots separate words in scene names (Hades.II), but not in versions (v1.0.2) or
        # acronyms written with dots (R.E.P.O).
        if any(len(part) > 1 and not part.isdigit() for part in re.split(r"[\s.]+", name) if part):
            name = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", name)
    words = name.split()
    while len(words) > 1 and (words[-1].lower().strip("[]()") in RELEASE_TAGS
                              or re.fullmatch(r"(?i)multi\d+|v?\d+(\.\d+)+", words[-1])):
        words.pop()
    while len(words) > 1 and words[-1] in "-_":
        words.pop()
    return " ".join(words) or folder


def _guess_game_name(dll_dir: Path, library: Path) -> str:
    """The top-level folder under the library is usually the game's name."""
    try:
        rel = dll_dir.relative_to(library)
        if rel.parts:
            return rel.parts[0]
    except ValueError:
        pass
    return dll_dir.name


def scan_game_folder(library: Path, max_depth: int = 8) -> List[GameDir]:
    games: Dict[Path, GameDir] = {}
    xbox_candidates: List[Path] = []
    for entry in _walk(library, max_depth):
        if entry.name.lower() == "achievements.json":
            xbox_candidates.append(Path(entry.path))
        if entry.name.lower() not in STEAM_API_DLLS:
            continue
        dll_dir = Path(entry.path).parent
        if dll_dir in games:
            continue
        name = _guess_game_name(dll_dir, library)
        appid = detect_appid(dll_dir, library / name)
        if appid is None:
            continue
        schema = dll_dir / "steam_settings" / "achievements.json"
        game = GameDir(appid, dll_dir, display_name(name), schema if schema.is_file() else None)
        for folder in _local_save_dirs(dll_dir):
            for save in _saves_in(folder, appid, "Local save"):
                numeric = [p for p in save.path.relative_to(folder).parts[:-1] if p.isdigit()]
                game.saves.append(SaveFile(int(numeric[-1]) if numeric else appid, save.path, save.emulator))
        for save in _saves_in(dll_dir, appid, "Local save", max_depth=4):
            # Saves inside "<local save dir>/<appid>/" belong to that appid.
            numeric = [p for p in save.path.relative_to(dll_dir).parts[:-1] if p.isdigit()]
            owner = int(numeric[-1]) if numeric else appid
            game.saves.append(SaveFile(owner, save.path, save.emulator))
        games[dll_dir] = game
    for path in xbox_candidates:
        if path.parent.name.lower() == "steam_settings" or not xbox.looks_like_xbox(path):
            continue
        title = xbox.title_id(path)
        if title is None:
            continue
        name = _guess_game_name(path.parent, library)
        name = display_name(name)
        game = GameDir(title, path.parent, name, saves=[SaveFile(title, path, xbox.SOURCE)])
        game.schema = xbox.build_schema(title, name, path)
        games[path] = game
    return list(games.values())


def game_folder_list(config: Config) -> List[Path]:
    folders = [Path(os.path.expandvars(f)) for f in config.game_folders]
    if config.scan_common_game_folders:
        folders += common_game_folders()
    unique, seen = [], set()
    for folder in folders:
        key = str(folder).rstrip("\\/").lower()
        if key not in seen and folder.is_dir():
            seen.add(key)
            unique.append(folder)
    return unique


def scan_game_folders(config: Config) -> List[GameDir]:
    games: List[GameDir] = []
    for folder in game_folder_list(config):
        games.extend(scan_game_folder(folder))
    return games
