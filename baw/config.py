"""User settings and the app's data directory."""

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List


def data_dir() -> Path:
    override = os.environ.get("BAW_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "BetterAchievementWatcher"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "better-achievement-watcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    # Steam language name used for achievement text ("english", "german", "schinese", ...).
    language: str = "english"
    # Optional. Only used as a last resort; the keyless Steam endpoint covers almost every game.
    steam_api_key: str = ""
    # Look in the standard emulator save folders (Goldberg, GSE, CODEX, RUNE, EMPRESS, ...).
    scan_default_locations: bool = True
    # Extra folders laid out like "<folder>/<appid>/...achievements file...".
    extra_save_roots: List[str] = field(default_factory=list)
    # Game install / library folders. Searched for steam_api(64).dll so that games which
    # save next to the exe ("local_save") and ship steam_settings/achievements.json are found.
    game_folders: List[str] = field(default_factory=list)
    # Also search the usual places games get installed on every fixed drive
    # (X:\Games, X:\GOG Games, GOG Galaxy's Games folder).
    scan_common_game_folders: bool = True
    # Show achievements of games you own on Steam (read from Steam's local cache).
    # These never pop up - Steam already shows its own notification for them.
    include_steam_library: bool = True
    # Also list Steam games that aren't installed any more but have unlocked achievements.
    include_uninstalled_steam: bool = False
    # AppIDs hidden from the viewer (e.g. saves left behind by uninstalled games).
    # Hiding is cosmetic: hidden games still get popups.
    hidden_games: List[int] = field(default_factory=list)
    # Seconds between checks of the save files.
    poll_interval: float = 1.5
    # Popup settings.
    popup_seconds: float = 6.0
    popup_position: str = "bottom-right"  # bottom-right | bottom-left | top-right | top-left
    popup_scale: float = 1.0
    sound: bool = True
    # "steam": Steam's own achievement sound, taken from your Steam install
    # (falls back to the chime if Steam isn't installed). "chime": the built-in sound.
    sound_style: str = "steam"
    # Optional custom .wav; overrides sound_style when set.
    sound_file: str = ""
    # Closing the window keeps watching in the background (run the app again to reopen it).
    keep_running_on_close: bool = True

    @classmethod
    def path(cls) -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg.save()
            return cfg
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                setattr(cfg, key, value)
        return cfg

    def save(self) -> None:
        self.path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
