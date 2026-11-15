"""Achievements from GOG Galaxy's own local database.

When you play through the real Galaxy client, it syncs each game's achievement list and your
unlocks into ``%LOCALAPPDATA%\\GOG.com\\Galaxy\\Applications\\<clientId>\\Gameplay\\<userId>\\gameplay.db``
(SQLite): an ``achievement`` table (key, name, description, visible_while_locked, unlock_time,
image_url_locked, image_url_unlocked, rarity) and a ``database_info`` table whose
``achievements_retrieved`` is non-zero once the list has been fetched. Game names live in
``%PROGRAMDATA%\\GOG.com\\Galaxy\\storage\\galaxy-2.0.db`` (``ProductAuthorizations`` links a
client id to a product id, ``LimitedDetails`` holds the title).

Galaxy shows its own popups, so these games are viewer-only, like owned Steam games. The
schema comes from the same file, so there is no Steam AppID and no network access.
Layout learned from public descriptions of the files; not yet checked against a real
Galaxy install.
"""

import os
import re
import shutil
import sqlite3
import tempfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .parsers import AchievementState
from .schema import AchievementInfo, GameSchema

SOURCE = "GOG Galaxy"
DB_NAME = "gameplay.db"
_RARITY_KEYS = ("rarity", "percent")


def applications_dir() -> Optional[Path]:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "GOG.com" / "Galaxy" / "Applications" if base else None


def catalog_path() -> Optional[Path]:
    base = os.environ.get("PROGRAMDATA")
    return Path(base) / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db" if base else None


def client_id(path: Path) -> str:
    """<clientId>/Gameplay/<userId>/gameplay.db -> <clientId>"""
    return path.parent.parent.parent.name


def game_id(path: Path) -> int:
    """Stable id for the viewer (the list has no Steam AppID)."""
    return 3_000_000_000 + zlib.crc32(("galaxy:" + client_id(path)).encode()) % 1_000_000_000


def _query(path: Path, sql: str) -> Optional[List[tuple]]:
    """Run a query on a copy of the database: Galaxy keeps the original open in WAL mode, and a
    read-only open of it can fail or miss recent rows. None = not readable right now."""
    folder = tempfile.mkdtemp(prefix="baw-galaxy-")
    try:
        copy = Path(folder) / path.name
        try:
            shutil.copy2(path, copy)
        except OSError:
            return None
        for suffix in ("-wal", "-shm"):
            try:
                shutil.copy2(str(path) + suffix, str(copy) + suffix)
            except OSError:
                pass
        try:
            connection = sqlite3.connect(str(copy))
            try:
                return connection.execute(sql).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _unix_time(value) -> int:
    """unlock_time as seconds, milliseconds or an ISO date; 0 for locked or unreadable."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
    else:
        text = str(value).strip()
        if not text:
            return 0
        if re.fullmatch(r"-?\d+", text):
            number = int(text)
        else:
            try:
                return max(0, int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()))
            except ValueError:
                return 0
    if number <= 0:
        return 0
    return number // 1000 if number > 4_102_444_800 else number


def _ready(path: Path) -> Optional[bool]:
    """True once Galaxy has fetched the list; None if the file can't be read."""
    rows = _query(path, "SELECT value FROM database_info WHERE key = 'achievements_retrieved'")
    if rows is None:
        return None
    return bool(rows) and str(rows[0][0]).strip() not in ("", "0")


def parse(path: Path) -> Optional[Dict[str, AchievementState]]:
    ready = _ready(path)
    if ready is None:
        return None
    if not ready:
        return {}
    rows = _query(path, "SELECT key, unlock_time FROM achievement")
    if rows is None:
        return None
    states = {}
    for key, unlock_time in rows:
        if not key:
            continue
        when = _unix_time(unlock_time)
        states[str(key)] = AchievementState(str(key), when > 0, when)
    return states


def _titles() -> Dict[str, str]:
    """client id -> game title, from Galaxy's catalog (best effort)."""
    path = catalog_path()
    if path is None or not path.is_file():
        return {}
    rows = _query(
        path,
        "SELECT a.clientId, d.title FROM ProductAuthorizations a "
        "JOIN LimitedDetails d ON d.productId = a.productId "
        "ORDER BY d.stored_at",
    )
    return {str(c): str(t) for c, t in rows or [] if c and t}  # later (newer) rows win


def build_schema(path: Path, titles: Optional[Dict[str, str]] = None) -> Optional[GameSchema]:
    rows = _query(
        path,
        "SELECT key, name, description, visible_while_locked, image_url_locked, "
        "image_url_unlocked, rarity FROM achievement",
    )
    if not rows:
        return None
    client = client_id(path)
    name = (titles if titles is not None else _titles()).get(client) or f"GOG game {client}"
    schema = GameSchema(game_id(path), name, source=SOURCE, name_checked=True)
    for key, title, description, visible, locked, unlocked, rarity in rows:
        if not key:
            continue
        try:
            percent = round(max(0.0, min(100.0, float(rarity))), 1) if rarity is not None else None
        except (TypeError, ValueError):
            percent = None
        schema.achievements[str(key)] = AchievementInfo(
            name=str(key),
            display_name=str(title or ""),
            description=str(description or ""),
            icon=str(unlocked or locked or ""),
            icon_gray=str(locked or unlocked or ""),
            hidden=visible is not None and not visible,
            percent=percent,
        )
    return schema


def find_saves() -> List[Path]:
    root = applications_dir()
    found: List[Path] = []
    if root is None or not root.is_dir():
        return found
    try:
        clients = list(root.iterdir())
    except OSError:
        return found
    for client in clients:
        try:
            for user in (client / "Gameplay").iterdir():
                path = user / DB_NAME
                if path.is_file():
                    found.append(path)
        except OSError:
            continue
    # Several Galaxy users can have data for one game: the most recently used one wins.
    newest: Dict[str, Tuple[float, Path]] = {}
    for path in found:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        key = client_id(path)
        if key not in newest or stamp > newest[key][0]:
            newest[key] = (stamp, path)
    return sorted(p for _, p in newest.values())
