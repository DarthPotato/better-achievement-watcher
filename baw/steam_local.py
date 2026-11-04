"""Achievements of games you own on Steam, read from the Steam client's local cache.

Steam keeps two binary KeyValues files per game in ``Steam/appcache/stats``:

* ``UserGameStatsSchema_<appid>.bin`` - which stat id / bit is which achievement
* ``UserGameStats_<accountid>_<appid>.bin`` - your stats: for each stat id a ``data``
  bit field (bit set = unlocked) and ``AchievementTimes`` (bit -> unix time)

This is shown in the viewer only. Steam shows its own popup for these games.

Steam writes these files for any game whose library or achievement page you opened, owned
or not, so only games that are installed or have at least one unlock are listed.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from .parsers import AchievementState
from .schema import parse_binary_kv

STEAM_SOURCE = "Steam"
STATS_FILE = re.compile(r"^UserGameStats_(\d+)_(\d+)\.bin$", re.I)


def stats_files(steam_dir: Optional[Path]) -> List[tuple]:
    """(appid, path) for the most recently used Steam account's stats files."""
    if steam_dir is None:
        return []
    folder = steam_dir / "appcache" / "stats"
    by_account: Dict[str, List[tuple]] = {}
    newest: Dict[str, float] = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for path in entries:
        match = STATS_FILE.match(path.name)
        if not match:
            continue
        account, appid = match.groups()
        by_account.setdefault(account, []).append((int(appid), path))
        try:
            newest[account] = max(newest.get(account, 0.0), path.stat().st_mtime)
        except OSError:
            pass
    if not by_account:
        return []
    account = max(by_account, key=lambda a: newest.get(a, 0.0))
    return by_account[account]


def _achievement_bits(schema_root: dict) -> Dict[str, Dict[int, str]]:
    """stat id -> {bit index -> achievement API name}"""
    game = next((v for v in schema_root.values() if isinstance(v, dict)), {})
    result: Dict[str, Dict[int, str]] = {}
    for stat_id, stat in (game.get("stats") or {}).items():
        if not isinstance(stat, dict) or not isinstance(stat.get("bits"), dict):
            continue
        bits = {}
        for bit_key, bit in stat["bits"].items():
            if isinstance(bit, dict) and "name" in bit:
                index = bit.get("bit", bit_key)
                try:
                    bits[int(index)] = str(bit["name"])
                except (TypeError, ValueError):
                    continue
        result[str(stat_id)] = bits
    return result


def parse_steam_stats(path: Path) -> Optional[Dict[str, AchievementState]]:
    match = STATS_FILE.match(path.name)
    if not match:
        return None
    schema_path = path.parent / f"UserGameStatsSchema_{match.group(2)}.bin"
    try:
        stats_root = parse_binary_kv(path.read_bytes())
        bits_by_stat = _achievement_bits(parse_binary_kv(schema_path.read_bytes()))
    except Exception:  # missing, half-written or unexpected file
        return None
    if not bits_by_stat:
        return {}
    cache = stats_root.get("cache") if isinstance(stats_root.get("cache"), dict) else stats_root
    result: Dict[str, AchievementState] = {}
    for stat_id, bits in bits_by_stat.items():
        stat = cache.get(stat_id)
        data = stat.get("data", 0) if isinstance(stat, dict) else 0
        times = (stat.get("AchievementTimes") if isinstance(stat, dict) else None) or {}
        try:
            data = int(data) & 0xFFFFFFFF
        except (TypeError, ValueError):
            data = 0
        for index, name in bits.items():
            unlocked = bool(data >> index & 1)
            try:
                when = int(times.get(str(index), 0)) if unlocked else 0
            except (TypeError, ValueError):
                when = 0
            result[name] = AchievementState(name, unlocked, when)
    return result


def installed_appids(steam_dir: Optional[Path]) -> set:
    """AppIDs installed in any Steam library (appmanifest_<appid>.acf)."""
    if steam_dir is None:
        return set()
    libraries = [steam_dir]
    try:
        vdf = (steam_dir / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    found = set()
    for library in libraries:
        try:
            names = (library / "steamapps").iterdir()
            for path in names:
                match = re.match(r"^appmanifest_(\d+)\.acf$", path.name, re.I)
                if match:
                    found.add(int(match.group(1)))
        except OSError:
            continue
    return found
