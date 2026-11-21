"""GOG games run through UniverseLAN, an open-source stand-in for the Galaxy SDK.

It keeps each game's progress in ``%LOCALAPPDATA%\\UniverseLAN\\<gogGameId>\\UniverseLANData\\Achievements.ini``,
one ``[ACHIEVEMENT_KEY]`` section per achievement with ``Unlocked``, ``UnlockTime``,
``Description`` and ``VisibleWhileLocked``. The generic INI parser already understands the
progress part; this module finds the files and builds names from the same file.

The folder name is a GOG game id, not a Steam AppID, so a stable id is derived from it (a
Steam lookup with a GOG id would show another game). Names are the achievement keys: the
file has nothing nicer. Layout from UniverseLAN's documentation; not yet checked against a
real install.
"""

import os
import re
import zlib
from pathlib import Path
from typing import List, Optional

from .parsers import read_ini
from .schema import AchievementInfo, GameSchema

SOURCE = "UniverseLAN"
FILE_NAME = "achievements.ini"


def save_root() -> Optional[Path]:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "UniverseLAN" if local else None


def gog_id(path: Path) -> str:
    """<gogId>/UniverseLANData/Achievements.ini -> <gogId>"""
    return path.parent.parent.name


def game_id(path: Path) -> int:
    return 3_000_000_000 + zlib.crc32(("universelan:" + gog_id(path)).encode()) % 1_000_000_000


def build_schema(path: Path) -> Optional[GameSchema]:
    try:
        sections = read_ini(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None
    schema = GameSchema(game_id(path), f"GOG game {gog_id(path)}", source=SOURCE, name_checked=True)
    for key, entries in sections.items():
        lowered = {k.lower(): v for k, v in entries.items()}
        if not key or not any(k in lowered for k in ("unlocked", "unlocktime")):
            continue
        visible = str(lowered.get("visiblewhilelocked", "1")).strip()
        schema.achievements[key] = AchievementInfo(
            name=key, display_name=key, description=lowered.get("description", ""),
            hidden=visible in ("0", "false"))
    return schema if schema.achievements else None


def find_saves() -> List[Path]:
    root = save_root()
    found: List[Path] = []
    if root is None:
        return found
    try:
        folders = [p for p in root.iterdir() if re.fullmatch(r"\d+", p.name)]
    except OSError:
        return found
    for folder in folders:
        path = folder / "UniverseLANData" / "Achievements.ini"
        if path.is_file():
            found.append(path)
    return sorted(found)
