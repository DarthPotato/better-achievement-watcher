"""Epic games run through Nemirtingas' Epic emulator (EOS SDK emulator).

Its saves default to ``%APPDATA%\\NemirtingasEpicEmu\\...`` and hold, per game:

* ``achievements.json`` - the player's progress, keyed by achievement id:
  ``{"ACH_X": {"achievement_id": "ACH_X", "progress": 1.0, "unlock_time": 1700000000}}``
  (``unlock_time`` -1 = locked)
* ``achievements_db.json`` - the definitions: ``unlocked_display_name``,
  ``unlocked_description``, ``unlocked_icon_url``, ``locked_icon_url``, ``is_hidden``

There is no Steam AppID, so a stable id is derived from the save folder, and the names come
from the definitions file. Games that set a custom ``savepath`` (next to the game) aren't found.
"""

import json
import os
import re
import zlib
from pathlib import Path
from typing import Dict, List, Optional

from .parsers import AchievementState
from .schema import AchievementInfo, GameSchema

SOURCE = "Epic emulator"
SAVES_FILE = "achievements.json"
DB_FILE = "achievements_db.json"
LOCKED = -1


def save_root() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "NemirtingasEpicEmu" if appdata else None


def _load(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None


def _entries(data) -> Dict[str, dict]:
    """The emulator writes a map keyed by id; accept a list of entries too."""
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    if isinstance(data, list):
        return {str(v.get("achievement_id")): v for v in data if isinstance(v, dict) and v.get("achievement_id")}
    return {}


def _unix_time(value) -> int:
    """The emulator sometimes stores a monotonic clock instead of a date: keep real dates only."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    for divisor in (1, 1000, 1_000_000, 1_000_000_000):  # seconds, ms, us, ns
        seconds = value // divisor
        if 946_684_800 <= seconds <= 4_102_444_800:  # 2000 .. 2100
            return seconds
    return 0


def parse(path: Path) -> Optional[Dict[str, AchievementState]]:
    data = _load(path)
    if data is None:
        return None  # missing or mid-write
    states = {}
    for key, entry in _entries(data).items():
        when = entry.get("unlock_time", LOCKED)
        try:
            unlocked = int(when) != LOCKED
        except (TypeError, ValueError):
            unlocked = bool(when)
        try:
            progress = float(entry.get("progress")) if entry.get("progress") is not None else None
        except (TypeError, ValueError):
            progress = None
        states[key] = AchievementState(key, unlocked, _unix_time(when) if unlocked else 0,
                                       progress if not unlocked else None, 1.0 if progress is not None else None)
    return states


def looks_like_epic(path: Path) -> bool:
    return path.name.lower() == SAVES_FILE and (path.parent / DB_FILE).is_file()


def game_name(path: Path) -> str:
    """The nearest folder name that isn't an epic id or the emulator's own folder."""
    for folder in path.parents:
        name = folder.name
        if not name or name.lower() == "nemirtingasepicemu":
            break
        if not re.fullmatch(r"(0x)?[0-9a-fA-F]{16,}", name) and name.lower() not in ("achievements", "remote", "saves"):
            return name
    return path.parent.name


def game_id(path: Path) -> int:
    """Stable id for the viewer (no Steam AppID exists for these)."""
    return 3_000_000_000 + zlib.crc32(str(path.parent).lower().encode()) % 1_000_000_000


def build_schema(path: Path) -> Optional[GameSchema]:
    db = _entries(_load(path.parent / DB_FILE))
    if not db:
        return None
    schema = GameSchema(game_id(path), game_name(path), source=SOURCE)
    for key, item in db.items():
        schema.achievements[key] = AchievementInfo(
            name=key,
            display_name=str(item.get("unlocked_display_name") or item.get("locked_display_name") or ""),
            description=str(item.get("unlocked_description") or item.get("locked_description") or ""),
            icon=str(item.get("unlocked_icon_url") or ""),
            icon_gray=str(item.get("locked_icon_url") or item.get("unlocked_icon_url") or ""),
            hidden=bool(item.get("is_hidden")),
        )
    return schema


def find_saves(max_depth: int = 5) -> List[Path]:
    root = save_root()
    found: List[Path] = []
    if root is None or not root.is_dir():
        return found
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file() and looks_like_epic(Path(entry.path)):
                    found.append(Path(entry.path))
            except OSError:
                continue
    return found
