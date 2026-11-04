"""Xbox / Microsoft Store games run through an Xbox emulator (e.g. "1911").

These keep an Xbox Live style ``Achievements.json`` next to the game that holds both the
achievement list and your progress:

    {"achievements": [{"id": "1", "name": "...", "description": "...",
                       "progressState": "Achieved" | "InProgress" | "NotStarted",
                       "progression": {"timeUnlocked": "2025-01-01T12:00:00Z",
                                       "requirements": [{"current": "2", "target": "100"}]},
                       "mediaAssets": [{"type": "Icon", "url": "..."}],
                       "isSecret": false,
                       "titleAssociations": [{"name": "...", "id": 1624264477}]}]}

There is no Steam AppID, so the Xbox title id is used as the game's id and names/icons
come from the file itself.
"""

import json
import zlib
from pathlib import Path
from typing import Optional

from .schema import AchievementInfo, GameSchema

SOURCE = "Xbox emulator"
SNIFF_BYTES = 65536


def looks_like_xbox(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    return b'"progressState"' in head and b'"achievements"' in head


def _load(path: Path) -> Optional[list]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None
    items = data.get("achievements") if isinstance(data, dict) else data
    return items if isinstance(items, list) else None


def title_id(path: Path) -> Optional[int]:
    """The Xbox title id, or a stable id derived from the product id."""
    for item in _load(path) or []:
        for title in item.get("titleAssociations") or []:
            try:
                return int(title["id"])
            except (KeyError, TypeError, ValueError):
                continue
        product = item.get("productId") or item.get("serviceConfigId")
        if product:
            return 2_000_000_000 + zlib.crc32(str(product).encode()) % 1_000_000_000
    return None


def build_schema(appid: int, name: str, path: Path) -> Optional[GameSchema]:
    items = _load(path)
    if not items:
        return None
    schema = GameSchema(appid, name, source=SOURCE)
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            continue
        icon = next((m.get("url", "") for m in item.get("mediaAssets") or []
                     if isinstance(m, dict) and m.get("type", "").lower() == "icon"), "")
        # images-eds.xboxlive.com has no valid certificate; its -ssl twin does.
        if icon.startswith("http://images-eds.xboxlive.com/"):
            icon = "https://images-eds-ssl.xboxlive.com/" + icon[len("http://images-eds.xboxlive.com/"):]
        info = AchievementInfo(
            name=str(item["id"]),
            display_name=str(item.get("name", "")),
            description=str(item.get("description") or item.get("lockedDescription") or ""),
            icon=icon,
            icon_gray=icon,
            hidden=bool(item.get("isSecret")),
        )
        schema.achievements[info.name] = info
    if not name:
        titles = [t.get("name") for i in items for t in (i.get("titleAssociations") or []) if isinstance(t, dict)]
        schema.name = next((t for t in titles if t), "")
    return schema if schema.achievements else None
