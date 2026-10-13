"""Parsers for the achievement save files written by Steam emulators.

Every emulator stores roughly the same thing (achievement API name -> unlocked + time),
just in a slightly different dialect. Instead of one parser per crack group, this module
understands the dialects generically:

* JSON (Goldberg, GSE/gbe_fork, EMPRESS):
      {"ACH_NAME": {"earned": true, "earned_time": 1700000000}}
* One INI section per achievement (CODEX, RUNE, OnlineFix, CreamAPI, Reloaded):
      [ACH_NAME]
      Achieved=1
      UnlockTime=1700000000
* Flat INI sections (DARKSiDERS/Hoodlum, SKIDROW, ACHIEVE_DATA):
      [Achievements]              [AchievementsUnlockTimes]
      ACH_NAME=1                  ACH_NAME=1700000000
* 3DM / RLD! hex encoded little-endian values:
      [State]                     [Time]
      ACH_NAME=0101               ACH_NAME=80b6e765
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

ACHIEVED_KEYS = ("achieved", "earned", "unlocked", "haveachieved", "state", "achieve", "isachieved")
TIME_KEYS = (
    "unlocktime", "unlock_time", "earned_time", "earnedtime", "haveachievedtime",
    "havehaveachievedtime", "achievedtime", "time", "timestamp", "date",
)
CUR_PROGRESS_KEYS = ("curprogress", "progress", "cur_progress", "current")
MAX_PROGRESS_KEYS = ("maxprogress", "max_progress", "max")

# Sections that hold metadata rather than achievements.
META_SECTIONS = {"steamachievements", "steam", "steam64", "settings", "stats", "user", "info"}
TIME_SECTIONS = {"achievementsunlocktimes", "time", "unlocktimes", "times"}
STATE_SECTIONS = {"state"}

TRUE_STRINGS = {"1", "true", "yes", "on"}
FALSE_STRINGS = {"0", "false", "no", "off", ""}


@dataclass
class AchievementState:
    name: str
    unlocked: bool
    unlock_time: int = 0  # unix seconds, 0 when unknown
    progress: Optional[float] = None
    max_progress: Optional[float] = None


def _is_hex(value: str) -> bool:
    return len(value) >= 2 and len(value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", value) is not None


def _hex_le(value: str) -> int:
    """Decode a hex string holding little-endian bytes (3DM / RLD! style)."""
    value = value.strip()
    if value.lower().startswith("0x"):
        return int(value, 16)
    return int.from_bytes(bytes.fromhex(value), "little")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in TRUE_STRINGS:
            return True
        if v in FALSE_STRINGS:
            return False
        try:
            return float(v) != 0
        except ValueError:
            pass
        if _is_hex(v):
            return _hex_le(v) != 0
    return False


def _to_number(value, hex_encoded: bool = False) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip()
    if not v:
        return None
    if hex_encoded and _is_hex(v):
        return float(_hex_le(v))
    try:
        return float(v)
    except ValueError:
        if _is_hex(v):
            return float(_hex_le(v))
    return None


def _normalize_time(value: Optional[float]) -> int:
    if not value or value < 0:
        return 0
    if value > 1e11:  # milliseconds
        value /= 1000
    return int(value)


def _pick(entry: Dict, keys) -> Optional[object]:
    lowered = {str(k).lower(): v for k, v in entry.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def _from_entry(name: str, entry: Dict) -> AchievementState:
    """Build a state from a per-achievement mapping (JSON object or INI section)."""
    raw_state = _pick(entry, ACHIEVED_KEYS)
    # RLD!/3DM write State/Time as little-endian hex; others use plain decimal.
    hex_encoded = isinstance(raw_state, str) and len(raw_state.strip()) > 1 and _is_hex(raw_state.strip())
    unlocked = _truthy(raw_state)
    unlock_time = _normalize_time(_to_number(_pick(entry, TIME_KEYS), hex_encoded))
    cur = _to_number(_pick(entry, CUR_PROGRESS_KEYS), hex_encoded)
    mx = _to_number(_pick(entry, MAX_PROGRESS_KEYS), hex_encoded)
    if not unlocked and mx and cur and cur >= mx:
        unlocked = True
    if not mx:
        cur = mx = None
    return AchievementState(name, unlocked, unlock_time if unlocked else 0, cur, mx)


def _has_achieved_key(entry: Dict) -> bool:
    return _pick(entry, ACHIEVED_KEYS) is not None


# ---------------------------------------------------------------- text decoding

def _decode(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le", errors="replace")
    if b"\x00" in data:
        return None  # binary format we don't understand
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---------------------------------------------------------------- JSON

def parse_json(text: str) -> Optional[Dict[str, AchievementState]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    result: Dict[str, AchievementState] = {}
    if isinstance(data, dict):
        # Some tools wrap the list: {"achievements": {...}} / {"Achievements": [...]}
        for wrapper in ("achievements", "Achievements", "ACHIEVE_DATA"):
            if wrapper in data and isinstance(data[wrapper], (dict, list)) and len(data) <= 3:
                return parse_json(json.dumps(data[wrapper]))
        for name, entry in data.items():
            if isinstance(entry, dict):
                if _has_achieved_key(entry):
                    result[name] = _from_entry(name, entry)
            elif isinstance(entry, (bool, int)) or (isinstance(entry, str) and entry.strip().lower() in TRUE_STRINGS | FALSE_STRINGS):
                unlocked = _truthy(entry)
                result[name] = AchievementState(name, unlocked)
    elif isinstance(data, list):
        # A list without achieved flags is a schema (steam_settings/achievements.json), not a save.
        for entry in data:
            if isinstance(entry, dict) and _has_achieved_key(entry):
                name = _pick(entry, ("name", "apiname", "id", "api_name"))
                if name:
                    result[str(name)] = _from_entry(str(name), entry)
    else:
        return None
    return result


# ---------------------------------------------------------------- INI

def read_ini(text: str) -> Dict[str, Dict[str, str]]:
    """Very tolerant INI reader: duplicate keys/sections, no header, odd comments."""
    sections: Dict[str, Dict[str, str]] = {}
    current = sections.setdefault("", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and "]" in line:
            name = line[1:line.index("]")].strip()
            current = sections.setdefault(name, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        current[key.strip()] = value
    return sections


def parse_ini(text: str) -> Dict[str, AchievementState]:
    sections = read_ini(text)
    result: Dict[str, AchievementState] = {}
    times: Dict[str, str] = {}
    hex_states: Dict[str, str] = {}

    for section, entries in sections.items():
        lname = section.lower()
        if lname in TIME_SECTIONS:
            times.update(entries)
            continue
        if lname in STATE_SECTIONS:
            hex_states.update(entries)
            continue
        if section and _has_achieved_key(entries):
            result[section] = _from_entry(section, entries)
            continue
        if lname in META_SECTIONS and lname != "steamachievements":
            continue
        # Flat section: ACH_NAME=1. Skip index tables like CODEX's "00000=ACH_NAME" and "Count=12".
        for key, value in entries.items():
            if key.isdigit() or key.lower() in ("count", "version"):
                continue
            v = value.strip().lower()
            if v in TRUE_STRINGS or v in FALSE_STRINGS:
                result[key] = AchievementState(key, v in TRUE_STRINGS)

    for name, value in hex_states.items():
        v = value.strip()
        unlocked = v == "0101" or _truthy(v)
        result[name] = AchievementState(name, unlocked)

    for name, value in times.items():
        state = result.get(name)
        if state and state.unlocked:
            hex_encoded = name in hex_states
            state.unlock_time = _normalize_time(_to_number(value, hex_encoded))
    return result


# ---------------------------------------------------------------- entry point

def parse_bytes(data: bytes) -> Optional[Dict[str, AchievementState]]:
    """Return achievement states, or None when the data isn't (yet) parseable."""
    text = _decode(data)
    if text is None:
        return None
    stripped = text.lstrip()
    # "[" starts both a JSON list and an INI section header.
    if stripped.startswith("{") or re.match(r"\[\s*[{\]]", stripped):
        return parse_json(text)
    return parse_ini(text)


def parse_file(path: Path) -> Optional[Dict[str, AchievementState]]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data.strip():
        return None
    return parse_bytes(data)
