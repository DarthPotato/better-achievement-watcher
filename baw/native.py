"""Games that keep their own achievement list in their save data.

Some releases strip the Steam layer out entirely instead of using an emulator. A game
built on a platform abstraction then falls back to its offline mode and stores unlocks in
its own save folder, in its own format, without timestamps. Each such game needs an entry
here: where the file is, how to read it, and its Steam AppID (used for names and icons).
"""

import gzip
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .parsers import AchievementState

Parser = Callable[[Path], Optional[Dict[str, AchievementState]]]


def _api_name(label: str) -> str:
    """"Achievement - 1 First repair!" -> "ACH_FIRST_REPAIR" (ReStory's Steam API names)."""
    label = re.sub(r"^\s*achievement\s*-\s*", "", label, flags=re.I)
    label = re.sub(r"^\d+\s+", "", label)
    words = re.findall(r"[A-Za-z0-9]+", label)
    return "ACH_" + "_".join(w.upper() for w in words) if words else label


def parse_restory(path: Path) -> Optional[Dict[str, AchievementState]]:
    """gzip'd JSON: {"UnlockedAchievements": [{"AchievementID": "Achievement - Cozy!"}, ...]}"""
    try:
        data = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8-sig"))
    except (OSError, EOFError, ValueError, UnicodeDecodeError):
        return None  # missing or mid-write
    items = data.get("UnlockedAchievements") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    states = {}
    for item in items:
        label = item.get("AchievementID") if isinstance(item, dict) else None
        if isinstance(label, str) and label.strip():
            name = _api_name(label)
            states[name] = AchievementState(name, True)
    return states


@dataclass(frozen=True)
class NativeGame:
    appid: int
    name: str
    source: str  # shown as the save's "emulator"
    base_env: str  # environment variable the folder is relative to
    folder: str
    pattern: str  # file name regex inside folder
    parser: Parser


GAMES: List[NativeGame] = [
    NativeGame(
        3812600, "ReStory: Chill Electronics Repairs", "ReStory save",
        "USERPROFILE", r"AppData\LocalLow\Mandragora\Restory\SaveData",
        r"^achievements\.slot\d+\.[\w.-]+\.restory$", parse_restory,
    ),
]

_BY_SOURCE = {g.source: g for g in GAMES}


def parser_for(source: str) -> Optional[Parser]:
    game = _BY_SOURCE.get(source)
    return game.parser if game else None


def find_saves() -> List[tuple]:
    """(game, path) for every native achievement file present."""
    found = []
    for game in GAMES:
        base = os.environ.get(game.base_env)
        if not base:
            continue
        folder = Path(base) / Path(game.folder.replace("\\", os.sep))
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        pattern = re.compile(game.pattern, re.I)
        found += [(game, folder / n) for n in sorted(names) if pattern.match(n)]
    return found
