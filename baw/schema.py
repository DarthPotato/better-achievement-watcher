"""Achievement names, descriptions and icons ("schema") for a Steam AppID.

Emulator saves only contain API names like ``ACH_KILL_100``. The display text comes from,
in order of preference:

1. ``IPlayerService/GetGameAchievements`` - Steam's public endpoint. No API key needed,
   and it also returns the global unlock percentage.
2. ``ISteamUserStats/GetSchemaForGame`` - only if you set your own Steam Web API key.
3. ``steam_settings/achievements.json`` shipped next to the game's steam_api dll.
4. Steam's own cache (``Steam/appcache/stats/UserGameStatsSchema_<appid>.bin``).

Results are cached on disk so the app works offline after the first lookup.
"""

import json
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from . import __version__
from .config import Config, data_dir
from .locations import steam_install_dir

ICON_CDN = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{file}"
KEYLESS_URL = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid={appid}&language={lang}"
WEBAPI_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/?key={key}&appid={appid}&l={lang}"
STORE_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
RETRY_AFTER = 600  # seconds before retrying a failed network lookup


@dataclass
class AchievementInfo:
    name: str
    display_name: str = ""
    description: str = ""
    icon: str = ""  # URL or local file path
    icon_gray: str = ""
    hidden: bool = False
    percent: Optional[float] = None

    @property
    def title(self) -> str:
        return self.display_name or self.name


@dataclass
class GameSchema:
    appid: int
    name: str = ""
    achievements: Dict[str, AchievementInfo] = field(default_factory=dict)
    source: str = ""

    @property
    def title(self) -> str:
        return self.name or f"App {self.appid}"

    def info(self, api_name: str) -> AchievementInfo:
        found = self.achievements.get(api_name)
        if found is None:  # some emulators change the case of API names
            lowered = api_name.lower()
            found = next((a for k, a in self.achievements.items() if k.lower() == lowered), None)
        return found or AchievementInfo(api_name)

    def to_json(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "source": self.source,
            "fetched": int(time.time()),
            "achievements": [asdict(a) for a in self.achievements.values()],
        }

    @classmethod
    def from_json(cls, data: dict) -> "GameSchema":
        achievements = {}
        for entry in data.get("achievements", []):
            info = AchievementInfo(**{k: v for k, v in entry.items() if k in AchievementInfo.__dataclass_fields__})
            achievements[info.name] = info
        return cls(int(data["appid"]), data.get("name", ""), achievements, data.get("source", ""))


# ---------------------------------------------------------------- helpers

def _http_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": f"BetterAchievementWatcher/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _localized(value, language: str) -> str:
    if isinstance(value, dict):
        for key in (language, "english", "token"):
            if value.get(key):
                return str(value[key])
        return str(next(iter(value.values()), ""))
    return "" if value is None else str(value)


def _icon_url(appid: int, icon: str) -> str:
    if not icon or icon.startswith("http"):
        return icon or ""
    return ICON_CDN.format(appid=appid, file=icon)


# ---------------------------------------------------------------- Steam binary KeyValues

def parse_binary_kv(data: bytes) -> dict:
    """Parse Valve's binary KeyValues format (used by UserGameStatsSchema_*.bin)."""
    pos = 0

    def read_cstring() -> str:
        nonlocal pos
        end = data.index(b"\x00", pos)
        text = data[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        return text

    def read_map() -> dict:
        nonlocal pos
        result = {}
        while pos < len(data):
            kind = data[pos]
            pos += 1
            if kind in (0x08, 0x0B):
                return result
            key = read_cstring()
            if kind == 0x00:
                result[key] = read_map()
            elif kind == 0x01:
                result[key] = read_cstring()
            elif kind in (0x02, 0x04, 0x06):
                result[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4
            elif kind == 0x03:
                result[key] = struct.unpack_from("<f", data, pos)[0]
                pos += 4
            elif kind == 0x07:
                result[key] = struct.unpack_from("<Q", data, pos)[0]
                pos += 8
            elif kind == 0x0A:
                result[key] = struct.unpack_from("<q", data, pos)[0]
                pos += 8
            else:
                raise ValueError(f"unknown KeyValues type 0x{kind:02x} at {pos - 1}")
        return result

    return read_map()


def schema_from_steam_cache(appid: int, language: str, steam_dir: Optional[Path]) -> Optional[GameSchema]:
    if steam_dir is None:
        return None
    path = steam_dir / "appcache" / "stats" / f"UserGameStatsSchema_{appid}.bin"
    try:
        root = parse_binary_kv(path.read_bytes())
    except (OSError, ValueError, struct.error):
        return None
    game = root.get(str(appid)) or next((v for v in root.values() if isinstance(v, dict)), {})
    schema = GameSchema(appid, str(game.get("gamename", "")), source="Steam cache")
    for stat in (game.get("stats") or {}).values():
        if not isinstance(stat, dict) or not isinstance(stat.get("bits"), dict):
            continue
        for bit in stat["bits"].values():
            if not isinstance(bit, dict) or "name" not in bit:
                continue
            display = bit.get("display") or {}
            info = AchievementInfo(
                name=str(bit["name"]),
                display_name=_localized(display.get("name"), language),
                description=_localized(display.get("desc"), language),
                icon=_icon_url(appid, str(display.get("icon", ""))),
                icon_gray=_icon_url(appid, str(display.get("icon_gray", ""))),
                hidden=str(display.get("hidden", "0")) not in ("0", ""),
            )
            schema.achievements[info.name] = info
    return schema if schema.achievements else None


def schema_from_steam_settings(appid: int, path: Path, language: str) -> Optional[GameSchema]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None
    if isinstance(entries, dict):
        entries = list(entries.values())
    schema = GameSchema(appid, source="steam_settings")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or "name" not in entry:
            continue

        def icon(key: str) -> str:
            value = str(entry.get(key) or "")
            if not value or value.startswith("http"):
                return value
            local = path.parent / value
            return str(local) if local.is_file() else _icon_url(appid, Path(value).name)

        info = AchievementInfo(
            name=str(entry["name"]),
            display_name=_localized(entry.get("displayName") or entry.get("display_name"), language),
            description=_localized(entry.get("description") or entry.get("desc"), language),
            icon=icon("icon"),
            icon_gray=icon("icongray") or icon("icon_gray"),
            hidden=str(entry.get("hidden", "0")).lower() in ("1", "true"),
        )
        schema.achievements[info.name] = info
    return schema if schema.achievements else None


def schema_from_keyless_api(appid: int, language: str) -> Optional[GameSchema]:
    data = _http_json(KEYLESS_URL.format(appid=appid, lang=urllib.parse.quote(language)))
    items = ((data or {}).get("response") or {}).get("achievements") or []
    schema = GameSchema(appid, source="Steam")
    for item in items:
        try:
            percent = float(item.get("player_percent_unlocked"))
        except (TypeError, ValueError):
            percent = None
        info = AchievementInfo(
            name=str(item.get("internal_name", "")),
            display_name=item.get("localized_name", ""),
            description=item.get("localized_desc", ""),
            icon=_icon_url(appid, item.get("icon", "")),
            icon_gray=_icon_url(appid, item.get("icon_gray", "")),
            hidden=bool(item.get("hidden")),
            percent=percent,
        )
        if info.name:
            schema.achievements[info.name] = info
    return schema if schema.achievements else None


def schema_from_web_api(appid: int, language: str, key: str) -> Optional[GameSchema]:
    if not key:
        return None
    data = _http_json(WEBAPI_URL.format(key=urllib.parse.quote(key), appid=appid, lang=urllib.parse.quote(language)))
    game = (data or {}).get("game") or {}
    items = (game.get("availableGameStats") or {}).get("achievements") or []
    schema = GameSchema(appid, game.get("gameName", ""), source="Steam Web API")
    for item in items:
        info = AchievementInfo(
            name=item.get("name", ""),
            display_name=item.get("displayName", ""),
            description=item.get("description", ""),
            icon=item.get("icon", ""),
            icon_gray=item.get("icongray", ""),
            hidden=bool(item.get("hidden")),
        )
        if info.name:
            schema.achievements[info.name] = info
    return schema if schema.achievements else None


def game_name_from_store(appid: int) -> str:
    data = _http_json(STORE_URL.format(appid=appid))
    entry = (data or {}).get(str(appid)) or {}
    if entry.get("success"):
        return str((entry.get("data") or {}).get("name", ""))
    return ""


# ---------------------------------------------------------------- store

class SchemaStore:
    """Thread-safe, disk-cached access to game schemas and achievement icons."""

    def __init__(self, config: Config, offline: bool = False):
        self.config = config
        self.offline = offline
        self.cache_dir = data_dir() / "cache"
        (self.cache_dir / "schema").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "icons").mkdir(parents=True, exist_ok=True)
        self._schemas: Dict[int, GameSchema] = {}
        self._failed: Dict[int, float] = {}
        self._local_schema: Dict[int, Path] = {}
        self._local_names: Dict[int, str] = {}
        self._lock = threading.RLock()
        self._steam_dir = steam_install_dir()

    def register_game_dir(self, appid: int, name: str, schema_file: Optional[Path]) -> None:
        with self._lock:
            self._local_names.setdefault(appid, name)
            if schema_file:
                self._local_schema[appid] = schema_file

    def forget(self) -> None:
        """Drop in-memory schemas (e.g. after the language changed)."""
        with self._lock:
            self._schemas.clear()
            self._failed.clear()

    def _cache_path(self, appid: int) -> Path:
        lang = "".join(c for c in self.config.language if c.isalnum()) or "english"
        return self.cache_dir / "schema" / f"{appid}_{lang}.json"

    def cached(self, appid: int) -> Optional[GameSchema]:
        with self._lock:
            if appid in self._schemas:
                return self._schemas[appid]
            try:
                schema = GameSchema.from_json(json.loads(self._cache_path(appid).read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                return None
            self._schemas[appid] = schema
            return schema

    def get(self, appid: int, needed: Iterable[str] = ()) -> GameSchema:
        """Return the best known schema. Refreshes when achievements in `needed` are missing."""
        needed = set(needed)
        schema = self.cached(appid)
        if schema and schema.achievements and all(n in schema.achievements for n in needed):
            return schema
        with self._lock:
            last_fail = self._failed.get(appid, 0)
        if time.time() - last_fail < RETRY_AFTER:
            return schema or GameSchema(appid, self._local_names.get(appid, ""))
        fresh = self._build(appid)
        with self._lock:
            if fresh.achievements:
                self._schemas[appid] = fresh
                self._failed.pop(appid, None)
                try:
                    self._cache_path(appid).write_text(json.dumps(fresh.to_json(), indent=1), encoding="utf-8")
                except OSError:
                    pass
                if not needed or all(n in fresh.achievements for n in needed):
                    return fresh
            self._failed[appid] = time.time()
            if fresh.achievements:
                return fresh
            if schema is None:
                schema = GameSchema(appid, fresh.name or self._local_names.get(appid, ""))
                self._schemas[appid] = schema
            return schema

    def _build(self, appid: int) -> GameSchema:
        lang = self.config.language or "english"
        local = self._local_schema.get(appid)
        sources = []
        if not self.offline:
            sources += [
                lambda: schema_from_keyless_api(appid, lang),
                lambda: schema_from_web_api(appid, lang, self.config.steam_api_key),
            ]
        sources += [
            lambda: schema_from_steam_settings(appid, local, lang) if local else None,
            lambda: schema_from_steam_cache(appid, lang, self._steam_dir),
        ]
        merged = GameSchema(appid)
        for source in sources:
            found = source()
            if not found:
                continue
            merged.name = merged.name or found.name
            merged.source = merged.source or found.source
            for name, info in found.achievements.items():
                existing = merged.achievements.get(name)
                if existing is None:
                    merged.achievements[name] = info
                elif existing.percent is None and info.percent is not None:
                    existing.percent = info.percent
            if merged.achievements and merged.name:
                break
        if not merged.name and not self.offline:
            merged.name = game_name_from_store(appid)
        if not merged.name:
            merged.name = self._local_names.get(appid, "")
        return merged

    def icon_file(self, appid: int, info: AchievementInfo, gray: bool = False, timeout: float = 5.0) -> Optional[Path]:
        """Local path of an achievement icon, downloading it into the cache if needed."""
        source = info.icon_gray if gray else info.icon
        if not source:
            return None
        if not source.startswith("http"):
            path = Path(source)
            return path if path.is_file() else None
        folder = self.cache_dir / "icons" / str(appid)
        target = folder / Path(urllib.parse.urlparse(source).path).name
        if target.is_file() and target.stat().st_size > 0:
            return target
        if self.offline:
            return None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(source, headers={"User-Agent": f"BetterAchievementWatcher/{__version__}"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            return target
        except Exception:
            return None
