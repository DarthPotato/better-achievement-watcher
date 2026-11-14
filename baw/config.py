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
    # Popup sound volume in percent (Steam's sounds are also evened out, see notify.prepared_sound).
    sound_volume: int = 100
    sound_style: str = "steam"  # "steam" (desktop client), "steam_deck" (Big Picture), "chime"
    # Optional custom .wav; overrides sound_style when set.
    sound_file: str = ""
    # Closing the window keeps watching in the background (run the app again to reopen it).
    keep_running_on_close: bool = True
    # GOG games: the app stands in for GOG Galaxy on this PC so GOG games can unlock achievements
    # without a GOG account. Needs a one-time setup (Settings), off by default.
    gog_enabled: bool = False
    # GOG game ids the app doesn't answer for, so they run as without Galaxy (right-click a game to
    # change it). Every GOG game is tracked unless it's listed here.
    gog_untracked: List[int] = field(default_factory=list)
    # GOG game ids that get the experimental memory fix (baw/galaxy_hook.py): the app patches the game's
    # GOG SDK in memory so online-only calls (leaderboards) don't block it. Off unless asked for.
    gog_fixed: List[int] = field(default_factory=list)
    # Look for a newer release on GitHub at startup (exe builds only).
    check_updates: bool = True
    # Last release version a background popup was shown for, so it's announced only once.
    update_notified: str = ""
    # Offer once to move the exe to %LOCALAPPDATA%\Programs and add a Start menu shortcut.
    offer_install: bool = True

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
        if "gog_fixed" not in raw and isinstance(cfg.gog_untracked, list):
            # Older configs listed IRON NEST as untracked by default. Games are tracked by default now; the ones
            # that need the memory fix simply run without achievements until it is switched on.
            cfg.gog_untracked = [g for g in cfg.gog_untracked if g != 1162687982]
        return cfg

    def save(self) -> None:
        self.path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
