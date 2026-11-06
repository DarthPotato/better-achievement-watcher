"""Polls the save files and reports newly unlocked achievements."""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config import Config
from . import epic, gog, native
from .parsers import AchievementState, parse_file
from .scanner import SaveFile, scan_game_folders, scan_save_roots
from .steam_local import STEAM_SOURCE, installed_appids, parse_steam_stats
from .locations import steam_install_dir
from .schema import SchemaStore

REDISCOVER_EVERY = 10.0  # seconds between looks for new save folders
GAME_FOLDER_RESCAN_EVERY = 600.0


@dataclass
class TrackedFile:
    save: SaveFile
    signature: Tuple[float, int] = (0.0, -1)
    states: Dict[str, AchievementState] = field(default_factory=dict)
    # True until the first successful parse of a file found during the silent startup scan.
    quiet: bool = False


@dataclass
class GameProgress:
    appid: int
    achievements: Dict[str, AchievementState]
    sources: List[SaveFile]

    @property
    def unlocked(self) -> List[AchievementState]:
        return [a for a in self.achievements.values() if a.unlocked]

    @property
    def last_unlock(self) -> int:
        return max((a.unlock_time for a in self.unlocked), default=0)

    @property
    def last_activity(self) -> float:
        """For sorting: the latest unlock time, or the save's modified time when unlocks
        carry no timestamps (e.g. ReStory)."""
        if self.last_unlock or not self.unlocked:
            return float(self.last_unlock)
        times = []
        for source in self.sources:
            signature = _signature(source.path)
            if signature:
                times.append(signature[0])
        return max(times, default=0.0)


UnlockCallback = Callable[[int, List[AchievementState]], None]


def _signature(path: Path) -> Optional[Tuple[float, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def merge(states: List[Dict[str, AchievementState]]) -> Dict[str, AchievementState]:
    """Combine saves from several emulators for the same game (unlocked wins, earliest time)."""
    merged: Dict[str, AchievementState] = {}
    for file_states in states:
        for name, state in file_states.items():
            current = merged.get(name)
            if current is None or (state.unlocked and not current.unlocked):
                merged[name] = state
            elif state.unlocked and current.unlocked and state.unlock_time and (
                not current.unlock_time or state.unlock_time < current.unlock_time
            ):
                merged[name] = state
    return merged


class Watcher:
    def __init__(self, config: Config, store: SchemaStore, on_unlock: Optional[UnlockCallback] = None,
                 on_change: Optional[Callable[[], None]] = None):
        self.config = config
        self.store = store
        self.on_unlock = on_unlock
        self.on_change = on_change
        self._files: Dict[Path, TrackedFile] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._game_dir_saves: List[SaveFile] = []
        self._last_rediscover = 0.0
        self._last_game_scan = 0.0
        self._steam_installed: Set[int] = set()
        self.gog_games: Dict[str, gog.GogGame] = {}  # GOG client id -> game

    # ------------------------------------------------------------ state

    def _unlocked(self, appid: int) -> Set[str]:
        files = [t.states for t in self._files.values() if t.save.appid == appid]
        return {n for n, s in merge(files).items() if s.unlocked}

    def snapshot(self) -> List[GameProgress]:
        with self._lock:
            by_app: Dict[int, List[TrackedFile]] = {}
            for tracked in self._files.values():
                by_app.setdefault(tracked.save.appid, []).append(tracked)
            games = []
            for appid, tracked in by_app.items():
                tracked = [t for t in tracked if self._listed(t)]
                if not tracked:
                    continue
                merged = merge([t.states for t in tracked])
                # A file found by searching game folders may be an unrelated stats.ini, so only
                # list it once it holds achievements. Emulator save folders always count: a
                # game you haven't unlocked anything in yet still shows as 0/N.
                sources = [t.save for t in tracked if t.states or t.save.emulator != "Local save"]
                if merged or sources:
                    games.append(GameProgress(appid, merged, sources or [t.save for t in tracked]))
            return games

    def _listed(self, tracked: TrackedFile) -> bool:
        if tracked.save.emulator != STEAM_SOURCE:
            return True
        # Steam caches stats for every game page you open, so list installed games (and,
        # if asked for, uninstalled ones you unlocked something in).
        if not tracked.states:
            return False
        if tracked.save.appid in self._steam_installed:
            return True
        return self.config.include_uninstalled_steam and any(s.unlocked for s in tracked.states.values())

    # ------------------------------------------------------------ discovery

    def scan_game_folders(self) -> None:
        saves: List[SaveFile] = []
        for game in scan_game_folders(self.config):
            self.store.register_game_dir(game.appid, game.name, game.schema_file)
            if game.schema is not None:
                self.store.register_fixed_schema(game.schema)
            saves.extend(game.saves)
        if self.config.gog_enabled:
            from .scanner import game_folder_list

            for library in game_folder_list(self.config):
                for folder in gog.find_game_folders(library):
                    self._add_gog_game(folder)
        with self._lock:
            self._game_dir_saves = saves
        self._last_game_scan = time.monotonic()

    def _steam_schema(self, appid: int):
        schema = self.store.get(appid)
        return schema if schema.achievements else None

    def _add_gog_game(self, folder: Path) -> Optional[gog.GogGame]:
        try:
            game = gog.build_game(folder, self._steam_schema, offline=self.store.offline)
        except Exception as exc:  # an odd game folder must not stop the scan
            print(f"[gog] {folder}: {exc!r}")
            return None
        if game is not None:
            self.store.register_fixed_schema(gog.to_schema(game))
            with self._lock:
                self.gog_games[game.client_id] = game
        return game

    def gog_game(self, client_id: str, pid: int) -> Optional[gog.GogGame]:
        """The GOG game behind a Galaxy connection (also games outside the game folders)."""
        with self._lock:
            known = self.gog_games.get(client_id)
        if known is not None:
            return known
        from .galaxy_service import exe_of_pid

        exe = exe_of_pid(pid)
        folder = gog.folder_for_exe(exe) if exe else None
        return self._add_gog_game(folder) if folder else None

    def discover(self, notify: bool) -> None:
        saves = scan_save_roots(self.config) + self._game_dir_saves
        if self.config.include_steam_library:
            installed = installed_appids(steam_install_dir())
            with self._lock:
                self._steam_installed = installed
        for save in saves:
            with self._lock:
                known = save.path in self._files
                if not known:
                    self._files[save.path] = TrackedFile(save, quiet=not notify)
            if not known:
                if save.emulator == epic.SOURCE:  # names and icons come with the save
                    schema = epic.build_schema(save.path)
                    if schema is not None:
                        self.store.register_fixed_schema(schema)
                self._refresh(self._files[save.path], notify)
        # Forget files that were deleted.
        with self._lock:
            for path in [p for p in self._files if not p.exists()]:
                del self._files[path]
        self._last_rediscover = time.monotonic()

    # ------------------------------------------------------------ polling

    def _refresh(self, tracked: TrackedFile, notify: bool) -> bool:
        signature = _signature(tracked.save.path)
        if signature is None or signature == tracked.signature:
            return False
        if tracked.save.emulator == STEAM_SOURCE:
            states = parse_steam_stats(tracked.save.path)
        else:
            parser = epic.parse if tracked.save.emulator == epic.SOURCE else native.parser_for(tracked.save.emulator)
            states = (parser or parse_file)(tracked.save.path)
        if states is None:
            # Probably caught the emulator mid-write: retry on the next poll. A file that
            # stays unreadable (unsupported binary format) is only re-read once it changes.
            if time.time() - signature[0] > 2:
                tracked.signature = signature
            return False
        # Steam already announces achievements of games you own.
        notify = notify and not tracked.quiet and tracked.save.emulator != STEAM_SOURCE
        tracked.quiet = False
        appid = tracked.save.appid
        with self._lock:
            before = self._unlocked(appid)
            tracked.signature = signature
            tracked.states = states
            after = self._unlocked(appid)
            merged = merge([t.states for t in self._files.values() if t.save.appid == appid])
        new = [merged[n] for n in after - before]
        if notify and new and self.on_unlock:
            new.sort(key=lambda s: (s.unlock_time, s.name))
            self.on_unlock(appid, new)
        return True

    def poll(self) -> bool:
        changed = False
        with self._lock:
            tracked = list(self._files.values())
        for item in tracked:
            changed |= self._refresh(item, notify=True)
        now = time.monotonic()
        if now - self._last_rediscover >= REDISCOVER_EVERY:
            count = len(self._files)
            self.discover(notify=True)
            changed |= len(self._files) != count
        return changed

    # ------------------------------------------------------------ thread

    def initial_scan(self) -> None:
        self.scan_game_folders()
        self.discover(notify=False)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.initial_scan()
        if self.on_change:
            self.on_change()
        while not self._stop.wait(max(0.25, float(self.config.poll_interval))):
            try:
                if time.monotonic() - self._last_game_scan >= GAME_FOLDER_RESCAN_EVERY:
                    self.scan_game_folders()
                if self.poll() and self.on_change:
                    self.on_change()
            except Exception as exc:  # keep watching no matter what one bad file does
                print(f"[watcher] {exc!r}")
