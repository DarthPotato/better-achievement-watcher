"""GOG games: finding them, working out their achievement keys, and the local save.

GOG's SDK only accepts achievement keys that are in the list the Galaxy stand-in sends it, and
an unknown key fails silently inside the game. The keys aren't published anywhere without a
GOG login, so they're taken from the game's own files (Mono assemblies, IL2CPP metadata, the
exe): every identifier that looks like an achievement key, or that matches a key of the same
game on Steam once prefixes/suffixes are ignored (GOG ``CampaignStartedAchi`` = Steam
``CampaignStarted``). Steam's list also gives names, descriptions and icons.

Unlocks are saved as ``<data dir>/gog/<GOG game id>/achievements.json`` in Goldberg's format,
so the normal watcher shows them like any emulator save.
"""

import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from .config import data_dir
from .schema import AchievementInfo, GameSchema, _http_json, names_match

SOURCE = "GOG"
GALAXY_DLLS = {"galaxy.dll", "galaxy64.dll"}
CACHE_VERSION = 1
STORE_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&cc=us&l=english"

_IDENT = re.compile(rb"[A-Za-z][A-Za-z0-9_]{3,63}")
_IDENT16 = re.compile(rb"(?:[A-Za-z0-9_]\x00){4,64}")
ACH_PATTERN = re.compile(r"^(?:ACH|ACHIEVEMENT|Achievement|ach)_|(?:Achi|Achievement|_ACH|_ACHIEVEMENT)$")
STAT_PATTERN = re.compile(r"(?:Stat|_STAT|_stat)$|^(?:STAT|stat)_")
MAX_SCAN_BYTES = 400 * 1024 * 1024
_lock = threading.RLock()

SteamLookup = Callable[[int], Optional[GameSchema]]


@dataclass
class GogGame:
    game_id: int
    client_id: str
    name: str
    folder: str
    steam_appid: int = 0
    aliases: Dict[str, str] = field(default_factory=dict)  # key the game may use -> canonical key
    shown: List[str] = field(default_factory=list)  # canonical keys listed in the viewer
    stats: List[str] = field(default_factory=list)
    info: Dict[str, dict] = field(default_factory=dict)  # canonical key -> AchievementInfo fields
    version: int = CACHE_VERSION


# ---------------------------------------------------------------- paths

def saves_root() -> Path:
    return data_dir() / "gog"


def game_dir(game_id: int) -> Path:
    return saves_root() / str(game_id)


def save_path(game_id: int) -> Path:
    return game_dir(game_id) / "achievements.json"


def stats_path(game_id: int) -> Path:
    return game_dir(game_id) / "stats.json"


def cache_path(game_id: int) -> Path:
    return game_dir(game_id) / "game.json"


# ---------------------------------------------------------------- finding games

def read_info(folder: Path) -> Optional[dict]:
    """The goggame-<id>.info file every GOG installer puts in the game folder."""
    try:
        infos = sorted(folder.glob("goggame-*.info"))
    except OSError:
        return None
    for info in infos:
        try:
            data = json.loads(info.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("gameId") and data.get("clientId"):
            return data
    return None


def has_galaxy_sdk(folder: Path, max_depth: int = 4) -> bool:
    stack = [(folder, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            name = entry.name.lower()
            try:
                if entry.is_file() and name in GALAXY_DLLS:
                    return True
                if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return False


def find_game_folders(library: Path, max_depth: int = 2) -> List[Path]:
    """Folders under library holding a GOG game that uses GOG's achievement SDK."""
    found = []
    stack = [(library, 0)]
    while stack:
        current, depth = stack.pop()
        if read_info(current) is not None:
            if has_galaxy_sdk(current):
                found.append(current)
            continue
        if depth >= max_depth:
            continue
        try:
            subdirs = [Path(e.path) for e in os.scandir(current) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        stack.extend((d, depth + 1) for d in subdirs)
    return found


def folder_for_exe(exe: Path) -> Optional[Path]:
    """The game folder (the one with goggame-*.info) for a running game's exe."""
    for folder in list(exe.parents)[:5]:
        if read_info(folder) is not None:
            return folder
    return None


# ---------------------------------------------------------------- keys

def _scan_files(folder: Path) -> List[Path]:
    files: List[Path] = []
    try:
        top = list(folder.iterdir())
    except OSError:
        return files
    for path in top:
        if path.is_file() and path.suffix.lower() == ".exe" and not path.name.lower().startswith("unins"):
            files.append(path)
        elif path.is_dir() and path.name.lower().endswith("_data"):  # Unity
            managed = path / "Managed"
            if managed.is_dir():
                files += [p for p in managed.glob("*.dll") if p.name.lower().startswith("assembly-csharp")]
            metadata = path / "il2cpp_data" / "Metadata" / "global-metadata.dat"
            if metadata.is_file():
                files.append(metadata)
    return files


def identifiers(folder: Path) -> Set[str]:
    """Identifier-like strings in the game's code (ASCII and UTF-16)."""
    found: Set[str] = set()
    budget = MAX_SCAN_BYTES
    for path in _scan_files(folder):
        try:
            size = path.stat().st_size
            if size > budget:
                continue
            budget -= size
            data = path.read_bytes()
        except OSError:
            continue
        found.update(m.decode("ascii") for m in _IDENT.findall(data))
        found.update(m.decode("utf-16-le") for m in _IDENT16.findall(data))
    return found


_PREFIX = re.compile(r"^(?:achievement|ach)[_\-](?=.{3})", re.I)
_SUFFIX = re.compile(r"(?<=.{3})[_\-]?(?:achievement|achi|ach)$", re.I)
# Code that handles achievements, not achievements: SetAchi, ClearAchi, GetAchievement...
_NOT_KEYS = {"set", "get", "clear", "reset", "unlock", "lock", "add", "submit", "trigger", "show", "update",
             "store", "request", "on", "is", "has", "remove", "save", "load", "init", "check", "my",
             "give", "grant", "award", "process", "handle", "report", "notify", "send", "test", "debug",
             "cheat", "all", "user", "new", "current", "last", "next", "steam", "gog", "galaxy", "platform",
             "achievement", "achievements", "achi", "ach"}


def norm(key: str) -> str:
    """Compare keys ignoring case and achievement prefixes/suffixes (separators inside count)."""
    k = key.strip("_- ").lower()
    k = _PREFIX.sub("", k, count=1)
    return _SUFFIX.sub("", k, count=1).strip("_-")


def looks_like_key(ident: str) -> bool:
    if not ACH_PATTERN.search(ident):
        return False
    stem = norm(ident)
    return len(stem) >= 4 and stem not in _NOT_KEYS and not any(stem.startswith(w + "_") for w in _NOT_KEYS)


def display_from_key(key: str) -> str:
    """"BackstabsMadeAchi" -> "Backstabs Made", "ACH_WIN_ONE_GAME" -> "Win One Game"."""
    stem = _SUFFIX.sub("", _PREFIX.sub("", key.strip("_- "), count=1), count=1).strip("_- ")
    stem = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).replace("_", " ")
    words = stem.split()
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in words) or key


def match_keys(idents: Iterable[str], steam: Optional[GameSchema]):
    """(aliases, shown, info): which keys to offer the game, and what the viewer lists."""
    idents = set(idents)
    aliases: Dict[str, str] = {}
    info: Dict[str, dict] = {}
    if steam is not None and steam.achievements:
        by_norm = {}
        for key in steam.achievements:
            by_norm.setdefault(norm(key), key)
        evidenced = []
        for ident in sorted(idents):
            canonical = by_norm.get(norm(ident))
            if canonical:
                aliases[ident] = canonical
                evidenced.append(canonical)
        for key, achievement in steam.achievements.items():
            aliases.setdefault(key, key)
            info[key] = asdict(achievement)
        # A demo or prologue has only some of the full game's achievements: list the ones
        # its files mention, or all of them when the files gave nothing away.
        order = list(steam.achievements)
        shown = sorted(set(evidenced), key=order.index) if evidenced else order
    else:
        shown = []
    # Keys that clearly name an achievement but have no Steam twin (GOG-only, or no Steam version).
    for ident in sorted(idents):
        if ident not in aliases and looks_like_key(ident):
            aliases[ident] = ident
            shown.append(ident)
            info[ident] = asdict(AchievementInfo(ident, display_from_key(ident)))
    return aliases, shown, info


def stat_keys(idents: Iterable[str], limit: int = 500) -> List[str]:
    return sorted(i for i in idents if STAT_PATTERN.search(i))[:limit]


# ---------------------------------------------------------------- Steam twin

def _steam_appid_in_folder(folder: Path) -> int:
    for config in [folder / "GalaxyConfig.json", *folder.glob("*_Data/GalaxyConfig.json")]:
        try:
            value = json.loads(config.read_text(encoding="utf-8-sig")).get("steam_appid")
            if str(value).isdigit():
                return int(value)
        except (OSError, ValueError, AttributeError):
            continue
    for txt in (folder / "steam_appid.txt",):
        try:
            words = txt.read_text(encoding="utf-8", errors="replace").split()
            if words and words[0].isdigit():
                return int(words[0])
        except OSError:
            continue
    return 0


def steam_appid_for(name: str, folder: Path, offline: bool = False) -> int:
    appid = _steam_appid_in_folder(folder)
    if appid or offline or not name:
        return appid
    for term in (name, re.sub(r"\s+(prologue|demo)$", "", name, flags=re.I)):
        data = _http_json(STORE_SEARCH.format(term=urllib.parse.quote(term))) or {}
        for item in data.get("items") or []:
            if names_match(str(item.get("name", "")), name) and str(item.get("id", "")).isdigit():
                return int(item["id"])
    return 0


# ---------------------------------------------------------------- building / caching

def load_cached(game_id: int) -> Optional[GogGame]:
    try:
        data = json.loads(cache_path(game_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != CACHE_VERSION:
        return None
    try:
        return GogGame(**{k: v for k, v in data.items() if k in GogGame.__dataclass_fields__})
    except TypeError:
        return None


def build_game(folder: Path, steam_lookup: Optional[SteamLookup] = None, offline: bool = False) -> Optional[GogGame]:
    """Work out a game's keys (cached per game; delete game.json to redo it)."""
    meta = read_info(folder)
    if meta is None:
        return None
    game_id = int(meta["gameId"])
    cached = load_cached(game_id)
    if cached is not None and Path(cached.folder) == folder:
        return cached
    name = str(meta.get("name") or folder.name)
    idents = identifiers(folder)
    appid = steam_appid_for(name, folder, offline)
    steam = steam_lookup(appid) if appid and steam_lookup else None
    aliases, shown, info = match_keys(idents, steam)
    game = GogGame(game_id, str(meta["clientId"]), name, str(folder), appid if steam else 0,
                   aliases, shown, stat_keys(idents), info)
    try:
        game_dir(game_id).mkdir(parents=True, exist_ok=True)
        cache_path(game_id).write_text(json.dumps(asdict(game), indent=1), encoding="utf-8")
        with _lock:
            if not save_path(game_id).exists():  # listed as 0/N before the first unlock
                _write_json(save_path(game_id), {})
    except OSError:
        pass
    return game


def to_schema(game: GogGame) -> GameSchema:
    schema = GameSchema(game.game_id, game.name, source=SOURCE)
    for key in game.shown:
        fields = game.info.get(key)
        if fields:
            schema.achievements[key] = AchievementInfo(**{k: v for k, v in fields.items()
                                                          if k in AchievementInfo.__dataclass_fields__})
        else:
            schema.achievements[key] = AchievementInfo(key)
    return schema


# ---------------------------------------------------------------- save files

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temp, path)


def unlocks(game_id: int) -> Dict[str, int]:
    """canonical key -> unlock time"""
    with _lock:
        data = _read_json(save_path(game_id))
    return {k: int(v.get("earned_time") or 0) for k, v in data.items()
            if isinstance(v, dict) and v.get("earned")}


def record_unlock(game_id: int, key: str, when: Optional[int] = None) -> bool:
    """Save an unlock. Returns False when it was already unlocked."""
    with _lock:
        path = save_path(game_id)
        data = _read_json(path)
        if isinstance(data.get(key), dict) and data[key].get("earned"):
            return False
        data[key] = {"earned": True, "earned_time": int(when or time.time())}
        _write_json(path, data)
        return True


def clear_unlock(game_id: int, key: str) -> None:
    with _lock:
        path = save_path(game_id)
        data = _read_json(path)
        if key in data:
            data[key] = {"earned": False, "earned_time": 0}
            _write_json(path, data)


def stat_values(game_id: int) -> Dict[str, float]:
    with _lock:
        return {k: v for k, v in _read_json(stats_path(game_id)).items() if isinstance(v, (int, float))}


def set_stat(game_id: int, key: str, value: float) -> None:
    with _lock:
        path = stats_path(game_id)
        data = _read_json(path)
        data[key] = value
        _write_json(path, data)


def reset(game_id: int) -> None:
    with _lock:
        for path in (save_path(game_id), stats_path(game_id)):
            try:
                path.unlink()
            except OSError:
                pass
