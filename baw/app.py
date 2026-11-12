"""Main window: game list, achievement viewer, settings, and the popup pipeline."""

import datetime
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from . import APP_NAME, __version__, galaxy_hook, gog, gog_setup, install, startup, update
from .winicon import shield_image
from .galaxy_service import GalaxyService
from .config import Config
from .notify import Notifier, PopupSpec, demo_spec, enable_dpi_awareness, load_icon
from .parsers import AchievementState
from .locations import steam_install_dir
from .notify import play_sound, steam_sound
from .scanner import game_folder_list, save_roots
from .schema import GameSchema, SchemaStore
from .steam_local import STEAM_SOURCE
from .watcher import GameProgress, Watcher

# Viewer filter: label -> test on a game's save sources.
FILTERS = {
    "All games": lambda emulators: True,
    "Steam": lambda emulators: STEAM_SOURCE in emulators,
    "Emulators & other": lambda emulators: STEAM_SOURCE not in emulators and "GOG" not in emulators,
    "GOG": lambda emulators: "GOG" in emulators,
}

INSTANCE_PORT = install.INSTANCE_PORT
UPDATE_RECHECK_MS = 6 * 3600 * 1000
# GOG games get a small badge in the game list: purple = the app stands in for Galaxy, grey = not tracked.
BADGE_SIZE = (19, 13)
BADGE_COLOURS = {"on": "#86328a", "fix": "#86328a", "off": "#8c8c8c"}
BADGE_TEXT = ("111" "0" "111" "0" "111",  # "GOG" in a 3x5 pixel font, one letter after the other
              "100" "0" "101" "0" "100",
              "101" "0" "101" "0" "101",
              "101" "0" "101" "0" "101",
              "111" "0" "111" "0" "111")
BURST_LIMIT = 3  # individual popups per batch; the rest are summarised


def _fmt_time(ts: int) -> str:
    if not ts:
        return "Unlocked"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def notify_running_instance(show: bool = True) -> bool:
    """Tell an already running copy we started (and optionally to show its window).

    Returns True when another copy is running."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as conn:
            conn.sendall(b"show\n" if show else b"ping\n")
        return True
    except OSError:
        return False


class App:
    def __init__(self, config: Config, background: bool = False, offline: bool = False):
        self.config = config
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("1100x660")
        self.root.minsize(760, 420)
        self.store = SchemaStore(config, offline=offline)
        self.notifier = Notifier(self.root, config)
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.work_queue: "queue.Queue[tuple]" = queue.Queue()
        self.watcher = Watcher(config, self.store, on_unlock=self._on_unlock,
                               on_change=lambda: self.ui_queue.put(("refresh",)))
        self.games: Dict[int, GameProgress] = {}
        self.selected_appid: Optional[int] = None
        self._images: Dict[str, object] = {}
        self._badges: Dict[str, object] = {}
        self._schema_requested: set = set()
        self._listener: Optional[socket.socket] = None
        self.galaxy: Optional[GalaxyService] = None
        self.gog_hooks: Optional[galaxy_hook.HookManager] = None
        self.gog_port_taken = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if background:
            self.root.withdraw()

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        self._start_instance_server()
        startup.repair()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        self._schedule_update_check()
        if self.config.gog_enabled:
            self.work_queue.put(("gog_check",))
        self.watcher.start()
        self.root.after(100, self._drain_ui_queue)
        self.root.mainloop()

    def quit(self) -> None:
        self.watcher.stop()
        self.stop_gog()
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        self.root.destroy()

    def _on_close(self) -> None:
        if self.config.keep_running_on_close:
            self.root.withdraw()
        else:
            self.quit()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _start_instance_server(self) -> None:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", INSTANCE_PORT))
            listener.listen(4)
        except OSError:
            return
        self._listener = listener

        def serve() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    try:
                        message = conn.recv(16)
                        if message.startswith(b"show"):
                            self.ui_queue.put(("show",))
                        elif message.startswith(b"quit"):
                            self.ui_queue.put(("quit",))
                    except OSError:
                        pass

        threading.Thread(target=serve, name="instance", daemon=True).start()

    # ------------------------------------------------------------ threads

    def _on_unlock(self, appid: int, states: List[AchievementState]) -> None:
        self.work_queue.put(("unlock", appid, states))

    def _worker(self) -> None:
        """Network / disk work that must stay off the Tk thread."""
        while True:
            job = self.work_queue.get()
            try:
                kind = job[0]
                if kind == "unlock":
                    self._prepare_popups(job[1], job[2])
                elif kind == "schema":
                    self.store.get(job[1])
                    self.ui_queue.put(("refresh",))
                elif kind == "icons":
                    self._fetch_icons(job[1])
                elif kind == "rescan":
                    self.watcher.scan_game_folders()
                    self.watcher.discover(notify=False)
                    self.ui_queue.put(("refresh",))
                elif kind == "update_check":
                    release = update.latest_release()
                    if release and update.is_newer(release.version):
                        self.ui_queue.put(("update", release))
                elif kind == "update_download":
                    self._download_update(job[1])
                elif kind == "gog_check":
                    # Peer updates need no admin rights; the service is only set up from Settings.
                    if not gog_setup.peer_installed():
                        gog_setup.install_peer()
                    self.ui_queue.put(("gog_status", gog_setup.status(), False))
                elif kind == "gog_remove":
                    removed = gog_setup.uninstall_service()
                    if removed:
                        gog_setup.remove_peer()
                    self.ui_queue.put(("gog_removed", removed))
                elif kind == "gog_setup":
                    try:
                        self.ui_queue.put(("gog_status", gog_setup.setup(), True))
                    except Exception as exc:
                        self.ui_queue.put(("gog_failed", str(exc)))
            except Exception as exc:
                print(f"[worker] {exc!r}")

    def _prepare_popups(self, appid: int, states: List[AchievementState]) -> None:
        schema = self.store.get(appid, [s.name for s in states])
        game_title = self.store.borrowed_by(appid, [s.name for s in states]) or schema.title
        for state in states[:BURST_LIMIT]:
            info = schema.info(state.name)
            icon = self.store.icon_file(appid, info)
            self.ui_queue.put(("popup", PopupSpec(game_title, info.title, info.description, icon, info.percent)))
        extra = len(states) - BURST_LIMIT
        if extra > 0:
            self.ui_queue.put(("popup", PopupSpec(game_title, f"+{extra} more achievements",
                                                  "Open Better Achievement Watcher to see them all.")))
        self.ui_queue.put(("refresh",))

    def _fetch_icons(self, appid: int) -> None:
        schema = self.store.get(appid)
        progress = self.games.get(appid)
        states = progress.achievements if progress else {}
        for name, info in list(schema.achievements.items()):
            unlocked = states.get(name) is not None and states[name].unlocked
            self.store.icon_file(appid, info, gray=not unlocked)
        self.ui_queue.put(("achievements", appid))

    def _download_update(self, release: "update.Release") -> None:
        """Download the new exe and run it as installer; it quits this copy and replaces it."""
        try:
            target = Path(os.environ.get("TEMP") or Path.home()) / "BetterAchievementWatcher-update" / update.EXE_NAME
            update.download(release.exe_url, target)
            install.launch(target, "--install")
        except Exception as exc:
            self.ui_queue.put(("update_failed", str(exc)))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "popup":
                    self.notifier.show(event[1])
                elif kind == "refresh":
                    self.refresh()
                elif kind == "achievements" and event[1] == self.selected_appid:
                    self._show_achievements(event[1])
                elif kind == "show":
                    self.show_window()
                elif kind == "quit":
                    self.quit()
                    return
                elif kind == "gog_status":
                    self._gog_status(event[1], event[2])
                elif kind == "gog_removed":
                    if event[1]:
                        messagebox.showinfo(APP_NAME, "The GalaxyCommunication service was removed.", parent=self.root)
                    else:
                        messagebox.showwarning(APP_NAME, "The service is still there (the admin prompt was "
                                               "declined?). To remove it by hand, in an admin PowerShell:\n\n"
                                               + gog_setup.uninstall_command(), parent=self.root)
                elif kind == "gog_failed":
                    messagebox.showerror(APP_NAME, f"GOG setup failed: {event[1]}", parent=self.root)
                elif kind == "update":
                    self._offer_update(event[1])
                elif kind == "update_failed":
                    self.update_label.configure(text=f"Update failed: {event[1]}")
                    self.update_button.state(["!disabled"])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    # ------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Ach.Treeview", rowheight=40)

        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text=APP_NAME, font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(bar, text="Quit", command=self.quit).pack(side="right", padx=2)
        ttk.Button(bar, text="Settings", command=self.open_settings).pack(side="right", padx=2)
        ttk.Button(bar, text="Test popup", command=lambda: self.notifier.show(demo_spec())).pack(side="right", padx=2)
        ttk.Button(bar, text="Rescan", command=self.rescan).pack(side="right", padx=2)

        self.update_bar = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        self.update_label = ttk.Label(self.update_bar, text="", foreground="#1a6e2e")
        self.update_label.pack(side="left")
        self.update_button = ttk.Button(self.update_bar, text="Update now")
        self.update_button.pack(side="left", padx=8)
        self.update_notes = ttk.Button(self.update_bar, text="What's new")
        self.update_notes.pack(side="left")
        self._update_anchor = bar

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(panes)
        self.filter = tk.StringVar(value="All games")
        chooser = ttk.Combobox(left, textvariable=self.filter, values=list(FILTERS), state="readonly")
        chooser.pack(side="top", fill="x", pady=(0, 4))
        chooser.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.game_tree = ttk.Treeview(left, columns=("progress",), show="tree headings", selectmode="browse")
        self.game_tree.heading("#0", text="Game")
        self.game_tree.heading("progress", text="Progress")
        self.game_tree.column("#0", width=258)
        self.game_tree.column("progress", width=68, anchor="center", stretch=False)
        self.game_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(left, command=self.game_tree.yview)
        scroll.pack(side="right", fill="y")
        self.game_tree.configure(yscrollcommand=scroll.set)
        self.game_tree.bind("<<TreeviewSelect>>", self._on_select_game)
        self.game_tree.tag_configure("hidden", foreground="#999")
        self.game_tree.tag_configure("untracked", foreground="#999")
        self.show_hidden = tk.BooleanVar(value=False)
        self._track_var = tk.BooleanVar(value=True)  # the GOG switches in the right-click menu
        self._fix_var = tk.BooleanVar(value=False)
        self.game_menu = tk.Menu(self.root, tearoff=0)
        self.game_tree.bind("<Button-3>", self._on_game_menu)
        panes.add(left, weight=1)

        right = ttk.Frame(panes, padding=(8, 0, 0, 0))
        self.game_title = ttk.Label(right, text="No game selected", font=("Segoe UI", 12, "bold"))
        self.game_title.pack(anchor="w")
        self.game_meta = ttk.Label(right, text="", foreground="#666")
        self.game_meta.pack(anchor="w", fill="x")
        self.game_meta.bind("<Configure>", lambda e: self.game_meta.configure(wraplength=max(200, e.width - 4)))
        self.progress = ttk.Progressbar(right, maximum=100)
        self.progress.pack(fill="x", pady=(4, 6))
        holder = ttk.Frame(right)
        holder.pack(fill="both", expand=True)
        self.ach_tree = ttk.Treeview(holder, columns=("desc", "when", "rarity"), show="tree headings",
                                     style="Ach.Treeview", selectmode="none")
        for column, text, width, anchor in (("#0", "Achievement", 210, "w"), ("desc", "Description", 250, "w"),
                                             ("when", "Unlocked", 135, "center"), ("rarity", "Rarity", 60, "center")):
            self.ach_tree.heading(column, text=text)
            self.ach_tree.column(column, width=width, anchor=anchor, stretch=column in ("#0", "desc"))
        self.ach_tree.tag_configure("locked", foreground="#888")
        self.ach_tree.pack(side="left", fill="both", expand=True)
        scroll2 = ttk.Scrollbar(holder, command=self.ach_tree.yview)
        scroll2.pack(side="right", fill="y")
        self.ach_tree.configure(yscrollcommand=scroll2.set)
        panes.add(right, weight=3)

        self.status = ttk.Label(self.root, text="Scanning...", padding=(8, 4))
        self.status.pack(fill="x")

    def _schema(self, appid: int) -> GameSchema:
        schema = self.store.cached(appid)
        if schema is None:
            if appid not in self._schema_requested:
                self._schema_requested.add(appid)
                self.work_queue.put(("schema", appid))
            schema = GameSchema(appid)
        return schema

    def _gog_state(self, game: GameProgress) -> str:
        """"" for games that aren't GOG games, else "on" (tracked), "fix" (tracked + memory fix) or "off"."""
        if not any(s.emulator == gog.SOURCE for s in game.sources):
            return ""
        if game.appid in self.config.gog_untracked:
            return "off"
        return "fix" if game.appid in self.config.gog_fixed else "on"

    def _badge(self, state: str):
        """Small GOG badge for the game list (drawn in code; "" gives an empty spacer so titles stay aligned)."""
        if state not in self._badges:
            width, height = BADGE_SIZE
            image = tk.PhotoImage(width=width, height=height, master=self.root)
            if state:
                colour = BADGE_COLOURS[state]
                hollow = state == "off"  # shape as well as colour: untracked is an outline, tracked is solid
                for y in range(height):
                    for x in range(width):
                        edge = min(x, width - 1 - x) == 0 or min(y, height - 1 - y) == 0
                        corner = min(x, width - 1 - x) + min(y, height - 1 - y) < 2  # rounded corners
                        if not corner and (edge or not hollow):
                            image.put(colour, (x, y))
                left = (width - len(BADGE_TEXT[0])) // 2
                top = (height - len(BADGE_TEXT)) // 2
                for row, bits in enumerate(BADGE_TEXT):
                    for col, bit in enumerate(bits):
                        if bit == "1":
                            image.put(colour if hollow else "white", (left + col, top + row))
            self._badges[state] = image
        return self._badges[state]

    def _is_hidden(self, appid: int) -> bool:
        return appid in self.config.hidden_games

    def refresh(self) -> None:
        self.games = {g.appid: g for g in self.watcher.snapshot()}
        hidden_count = sum(self._is_hidden(a) for a in self.games)
        shown = FILTERS.get(self.filter.get(), FILTERS["All games"])
        ordered = sorted((g for g in self.games.values()
                          if (self.show_hidden.get() or not self._is_hidden(g.appid))
                          and shown({s.emulator for s in g.sources})),
                         key=lambda g: (self._is_hidden(g.appid), -g.last_activity, g.appid))
        existing = set(self.game_tree.get_children())
        wanted = set()
        for index, game in enumerate(ordered):
            schema = self._schema(game.appid)
            total = len(schema.achievements) or len(game.achievements)
            iid = str(game.appid)
            wanted.add(iid)
            values = (f"{len(game.unlocked)}/{total}",)
            borrowed = self.store.borrowed_by(game.appid, game.achievements)
            title = borrowed or schema.title
            if borrowed:
                values = (f"{len(game.unlocked)}/{len(game.achievements)}" if game.achievements else "-",)
            state = self._gog_state(game)
            if state == "fix":
                title += f"  {gog.FIX_MARK}"
            tags = ("hidden",) if self._is_hidden(game.appid) else ()
            if state == "off":
                tags += ("untracked",)
            badge = self._badge(state)
            if iid in existing:
                self.game_tree.item(iid, text=title, values=values, tags=tags, image=badge)
                self.game_tree.move(iid, "", index)
            else:
                self.game_tree.insert("", index, iid=iid, text=title, values=values, tags=tags, image=badge)
        for iid in existing - wanted:
            self.game_tree.delete(iid)
        files = sum(len(g.sources) for g in self.games.values())
        hidden_note = f" ({hidden_count} hidden - right-click the list to show them)" if hidden_count else ""
        self.status.configure(text=f"Watching {files} save file(s) for {len(self.games)} game(s){hidden_note}. "
                                   f"Leave this running (or close the window - it keeps watching).")
        if self.selected_appid is not None and str(self.selected_appid) not in wanted:
            self.selected_appid = None
        if self.selected_appid is None and ordered:
            self.game_tree.selection_set(str(ordered[0].appid))
        elif self.selected_appid is None:
            self._clear_achievements()
        elif self.selected_appid in self.games:
            self._show_achievements(self.selected_appid)

    def _clear_achievements(self) -> None:
        self.game_title.configure(text="No game selected")
        self.game_meta.configure(text="")
        self.progress.configure(value=0)
        self.ach_tree.delete(*self.ach_tree.get_children())

    def _on_game_menu(self, event) -> None:
        iid = self.game_tree.identify_row(event.y)
        menu = self.game_menu
        menu.delete(0, "end")
        if iid:
            appid = int(iid)
            self.game_tree.selection_set(iid)
            if self._is_hidden(appid):
                menu.add_command(label="Unhide game", command=lambda: self.set_hidden(appid, False))
            else:
                menu.add_command(label="Hide game", command=lambda: self.set_hidden(appid, True))
            game = self.games.get(appid)
            if game and any(s.emulator == gog.SOURCE for s in game.sources):
                tracked = appid not in self.config.gog_untracked
                self._track_var.set(tracked)
                self._fix_var.set(appid in self.config.gog_fixed)
                menu.add_separator()
                menu.add_checkbutton(label="Track this game", variable=self._track_var,
                                     command=lambda: self.set_gog_tracked(appid, self._track_var.get()))
                menu.add_checkbutton(label=f"Memory fix {gog.FIX_MARK} (experimental)", variable=self._fix_var,
                                     state="normal" if tracked else "disabled",
                                     command=lambda: self.set_gog_fix(appid, self._fix_var.get()))
                menu.add_command(label="Reset achievements...", command=lambda: self.reset_gog_game(appid))
            menu.add_separator()
        menu.add_checkbutton(label="Show hidden games", variable=self.show_hidden, command=self.refresh)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def set_gog_tracked(self, appid: int, tracked: bool) -> None:
        """Untracked GOG games get no Galaxy sign-in, so they run as if Galaxy wasn't there."""
        ids = [i for i in self.config.gog_untracked if i != appid]
        self.config.gog_untracked = ids if tracked else ids + [appid]
        self.config.save()
        self.refresh()

    def set_gog_fix(self, appid: int, on: bool, confirm: bool = True) -> bool:
        """Switch the experimental memory fix (galaxy_hook) for one GOG game. False when the user said no."""
        if on and confirm and not messagebox.askyesno(
                APP_NAME, "Turn on the experimental memory fix for this game?\n\n"
                "While the game runs, this app attaches to it like a debugger and patches GOG's SDK in the game's "
                "memory, so online-only calls (such as leaderboards) don't block it. Nothing in the game's files "
                "changes and nothing is sent to GOG.\n\nAntivirus tools can be wary of programs that attach to "
                "other programs, and a game with anti-tamper protection may refuse to run while attached. If the "
                "fix can't attach, the game just runs without achievements. It takes effect the next time the game "
                "starts, and you can switch it off again here at any time.", parent=self.root):
            self.refresh()
            return False
        ids = [i for i in self.config.gog_fixed if i != appid]
        self.config.gog_fixed = ids + [appid] if on else ids
        self.config.save()
        self.refresh()
        return True

    def reset_gog_game(self, appid: int) -> None:
        title = self._schema(appid).title
        if messagebox.askyesno(APP_NAME, f"Reset all achievements of {title}?\n\nThe game reports them again "
                               "when you earn them next time. This only affects this app.", parent=self.root):
            gog.reset(appid)

    def set_hidden(self, appid: int, hidden: bool) -> None:
        games = [a for a in self.config.hidden_games if a != appid]
        self.config.hidden_games = games + [appid] if hidden else games
        self.config.save()
        self.refresh()

    def _on_select_game(self, _event=None) -> None:
        selection = self.game_tree.selection()
        if not selection:
            return
        self.selected_appid = int(selection[0])
        self._show_achievements(self.selected_appid)
        self.work_queue.put(("icons", self.selected_appid))

    def _image(self, path: Optional[Path]):
        if path is None:
            return ""
        key = str(path)
        if key not in self._images:
            self._images[key] = load_icon(path, 32) or ""
        return self._images[key]

    def _cached_icon(self, appid: int, source: str) -> Optional[Path]:
        if not source:
            return None
        if not source.startswith("http"):
            return Path(source) if Path(source).is_file() else None
        path = self.store.icon_cache_path(appid, source)
        return path if path.is_file() else None

    def _show_achievements(self, appid: int) -> None:
        game = self.games.get(appid)
        if game is None:
            return
        schema = self._schema(appid)
        borrowed = self.store.borrowed_by(appid, game.achievements)
        borrowed_from = ""
        if borrowed:
            # Only the save's own achievement names are known; the AppID's list belongs to another game.
            borrowed_from = schema.title
            schema = GameSchema(appid, borrowed, source="save file")
        names = list(schema.achievements) + [n for n in game.achievements if n not in schema.achievements]
        unlocked = [n for n in names if n in game.achievements and game.achievements[n].unlocked]
        locked = [n for n in names if n not in unlocked]
        unlocked.sort(key=lambda n: -game.achievements[n].unlock_time)
        total = len(names)
        self.game_title.configure(text=schema.title)
        emulators = sorted({s.emulator for s in game.sources})
        self.game_meta.configure(text=f"AppID {appid}  •  {len(unlocked)} of {total} unlocked  •  "
                                      f"{', '.join(emulators)}  •  names from {schema.source or 'save file'}")
        if any(s.emulator == gog.SOURCE for s in game.sources):
            note = gog.status_note(appid, appid not in self.config.gog_untracked, appid in self.config.gog_fixed)
            if note:
                self.game_meta.configure(text=self.game_meta.cget("text") + "\n" + note)
        if borrowed_from:
            recorded = (f"{len(unlocked)} of {total} achievements recorded in the save are shown by their raw names."
                        if total else "Its save hasn't recorded any achievements.")
            self.game_meta.configure(text=f"Runs under the AppID of {borrowed_from} ({appid}), so names and icons "
                                          f"aren't available. {recorded}")
        self.progress.configure(value=100 * len(unlocked) / total if total else 0)
        self.ach_tree.delete(*self.ach_tree.get_children())
        for name in unlocked + locked:
            info = schema.info(name)
            state = game.achievements.get(name)
            is_unlocked = name in unlocked
            title, desc = info.title, info.description
            if info.hidden and not is_unlocked:
                title, desc = "Hidden achievement", "Details are revealed once it's unlocked"
            if state and state.max_progress and not is_unlocked:
                desc = f"{desc}  [{state.progress:g}/{state.max_progress:g}]"
            when = _fmt_time(state.unlock_time) if is_unlocked else ""
            rarity = f"{info.percent:.1f}%" if info.percent is not None else ""
            icon = self._image(self._cached_icon(appid, info.icon if is_unlocked else info.icon_gray))
            self.ach_tree.insert("", "end", text=title, image=icon, values=(desc, when, rarity),
                                 tags=() if is_unlocked else ("locked",))

    def start_gog(self) -> None:
        if self.galaxy is None:
            self.gog_hooks = galaxy_hook.HookManager(self._hooked_folders, log=gog.log)
            service = GalaxyService(gog_setup.peer_dir(), self.watcher.gog_game, log=gog.log,
                                    tracked=lambda game: game.game_id not in self.config.gog_untracked,
                                    needs_hook=gog.NEEDS_HOOK, hook_ready=self.gog_hooks.ready)
            if service.start():
                self.gog_hooks.start()
                self.galaxy = service
                self.gog_port_taken = False
            else:
                self.gog_port_taken = True  # GOG Galaxy itself runs: games use that
                gog.log("port 9977 is taken (GOG Galaxy running?); GOG games use that instead")

    def gog_state(self) -> "tuple":
        """(text, colour) for the GOG status shown next to the setting."""
        if self.galaxy is not None:
            return "\u25cf Active", "#1a7f37"
        if self.gog_port_taken:
            return "\u25cf GOG Galaxy is handling GOG games", "#1a7f37"
        status = gog_setup.status()
        if not self.config.gog_enabled:
            return ("\u25cb Off (service still installed)" if status.service else "\u25cb Off"), "#777"
        if not status.ready:
            missing = "service" if not status.service else "GOG helper"
            return f"\u25cf Not set up ({missing} missing): turn the option off and on", "#b35900"
        return "\u25cf Set up, starting...", "#b35900"

    def _hooked_folders(self) -> List[Path]:
        """Install folders of tracked games that have the memory fix switched on."""
        return [Path(game.folder) for game in list(self.watcher.gog_games.values())
                if game.game_id in self.config.gog_fixed and game.game_id not in self.config.gog_untracked]

    def stop_gog(self) -> None:
        if self.gog_hooks is not None:
            self.gog_hooks.stop()
            self.gog_hooks = None
        if self.galaxy is not None:
            self.galaxy.stop()
            self.galaxy = None

    def _gog_status(self, status: "gog_setup.Status", after_setup: bool) -> None:
        if status.ready:
            self.start_gog()
            if after_setup:
                messagebox.showinfo(APP_NAME, "GOG games are set up. Start a GOG game while this app is "
                                    "running and its achievements will pop up here.\n\nIf a GOG game gets "
                                    "stuck while loading, right-click it in the list and choose \u201cDon't "
                                    "track this game\u201d.", parent=self.root)
        elif after_setup and not status.service:
            messagebox.showwarning(APP_NAME, "GOG games need a small Windows service, and it wasn't installed "
                                   "(the admin prompt was declined?). Turn the option off and on in Settings "
                                   "to try again.", parent=self.root)
        else:
            self.status.configure(text="GOG games: setup isn't finished - turn the option off and on in Settings.")

    def _schedule_update_check(self) -> None:
        """Check at start, then every few hours: the app often runs for days in the background."""
        if install.running_exe() is None:  # running from source: nothing to update
            return
        if self.config.check_updates:
            self.work_queue.put(("update_check",))
        self.root.after(UPDATE_RECHECK_MS, self._schedule_update_check)

    def _offer_update(self, release: "update.Release") -> None:
        import webbrowser

        self.update_label.configure(text=f"Version {release.version} is available (you have {__version__}).")
        self.update_button.configure(command=lambda: self._start_update(release))
        self.update_notes.configure(command=lambda: webbrowser.open(release.page_url))
        self.update_bar.pack(fill="x", after=self._update_anchor)
        if self.root.state() == "withdrawn" and self.config.update_notified != release.version:
            # Running hidden in the background: say it once with a popup.
            self.config.update_notified = release.version
            self.config.save()
            self.notifier.show(PopupSpec(APP_NAME, f"Version {release.version} is available",
                                         "Open the app to update.", header="Update available"))

    def _start_update(self, release: "update.Release") -> None:
        self.update_label.configure(text=f"Downloading version {release.version}...")
        self.update_button.state(["disabled"])
        self.work_queue.put(("update_download", release))

    def rescan(self) -> None:
        self.store.forget()
        self._schema_requested.clear()
        self.work_queue.put(("rescan",))

    # ------------------------------------------------------------ settings

    def open_settings(self) -> None:
        SettingsDialog(self)


GOG_SETUP_TEXT = (
    "To track GOG games, the app stands in for GOG Galaxy on this PC. Windows will ask for "
    "administrator permission once, because one step changes Windows' list of services.\n\n"
    "What happens:\n"
    "  \u2022  GOG's helper library (GalaxyPeer) is downloaded from GOG's servers into this app's "
    "data folder. GOG games load it to talk to Galaxy.\n"
    "  \u2022  A small Windows service called GalaxyCommunication is registered (this is the admin "
    "step). It does nothing by itself: GOG games only look for it before they connect. It's the "
    "dummy service from the open-source Comet project, checked against a known checksum.\n\n"
    "No GOG account is used and nothing is sent to GOG. Turning the option off later offers to "
    "remove the service again.\n\n"
    "Good to know:\n"
    "  \u2022  Keep this app running while you play (Start with Windows does that). Otherwise GOG "
    "games wait 15-30 seconds on their splash screen before they start.\n"
    "  \u2022  A few games wait for online features such as leaderboards, which need a real GOG "
    "login, and get stuck while loading. Right-click such a game in the list and choose "
    "\u201cDon't track this game\u201d: it then starts normally, without achievements."
)


def _shield_dialog(parent, title: str, text: str, action: str) -> bool:
    """A yes/no dialog whose action button carries Windows' admin shield."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    body = ttk.Frame(win, padding=14)
    body.pack(fill="both", expand=True)
    big = shield_image(32)
    if big is not None:
        ttk.Label(body, image=big).grid(row=0, column=0, sticky="n", padx=(0, 12))
    ttk.Label(body, text=text, wraplength=440, justify="left").grid(row=0, column=1, sticky="w")
    buttons = ttk.Frame(body)
    buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(14, 0))
    answer = {"ok": False}

    def choose(value: bool) -> None:
        answer["ok"] = value
        win.destroy()

    small = shield_image(16)
    go = ttk.Button(buttons, text=action, command=lambda: choose(True))
    if small is not None:
        go.configure(image=small, compound="left")
    go.pack(side="right")
    ttk.Button(buttons, text="Cancel", command=lambda: choose(False)).pack(side="right", padx=6)
    win._images = (big, small)  # keep references so Tk doesn't drop them
    win.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    win.grab_set()
    go.focus_set()
    parent.wait_window(win)
    return answer["ok"]


def confirm_gog_setup(parent) -> bool:
    return _shield_dialog(parent, "Set up GOG games", GOG_SETUP_TEXT, "Continue")


def confirm_gog_removal(parent) -> bool:
    return _shield_dialog(parent, "Remove GOG games setup",
                          "Also remove the GalaxyCommunication service from Windows? This needs "
                          "administrator permission again. Your GOG achievements in this app are kept.",
                          "Remove service")


class SettingsDialog:
    def __init__(self, app: App):
        self.app = app
        cfg = app.config
        win = self.win = tk.Toplevel(app.root)
        win.title("Settings")
        win.transient(app.root)
        # The options scroll; the Save/Cancel row below them always stays visible.
        self._canvas = canvas = tk.Canvas(win, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, padding=10)
        canvas.create_window((0, 0), window=body, anchor="nw", tags="body")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("body", width=e.width))
        win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.game_folders = self._folder_list(
            body, "Game folders (searched for steam_api.dll, local saves and steam_settings)", cfg.game_folders)
        self.extra_roots = self._folder_list(
            body, "Extra save folders (laid out as <folder>\\<AppID>\\...)", cfg.extra_save_roots)

        grid = ttk.Frame(body)
        grid.pack(fill="x", pady=8)
        self.language = tk.StringVar(value=cfg.language)
        self.api_key = tk.StringVar(value=cfg.steam_api_key)
        self.position = tk.StringVar(value=cfg.popup_position)
        self.seconds = tk.StringVar(value=str(cfg.popup_seconds))
        self.scale = tk.StringVar(value=str(cfg.popup_scale))
        self.sound = tk.BooleanVar(value=cfg.sound)
        self.defaults = tk.BooleanVar(value=cfg.scan_default_locations)
        self.keep = tk.BooleanVar(value=cfg.keep_running_on_close)
        self.autostart = tk.BooleanVar(value=startup.is_enabled())
        self.check_updates = tk.BooleanVar(value=cfg.check_updates)
        self.gog = tk.BooleanVar(value=cfg.gog_enabled)
        self.common = tk.BooleanVar(value=cfg.scan_common_game_folders)
        self.steam_library = tk.BooleanVar(value=cfg.include_steam_library)
        self.steam_uninstalled = tk.BooleanVar(value=cfg.include_uninstalled_steam)
        self.sound_file = cfg.sound_file
        self.sound_choice = tk.StringVar(value=self._sound_label(cfg.sound_style, cfg.sound_file))
        rows = [
            ("Language (Steam name, e.g. english, german, french)", ttk.Entry(grid, textvariable=self.language)),
            ("Steam Web API key (optional, rarely needed)", ttk.Entry(grid, textvariable=self.api_key, show="*")),
            ("Popup position", ttk.Combobox(grid, textvariable=self.position, state="readonly",
                                            values=["bottom-right", "bottom-left", "top-right", "top-left"])),
            ("Popup seconds", ttk.Entry(grid, textvariable=self.seconds, width=8)),
            ("Popup size (1.0 = normal)", ttk.Entry(grid, textvariable=self.scale, width=8)),
            ("Sound", self._sound_row(grid)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        grid.columnconfigure(1, weight=1)
        ttk.Checkbutton(body, text="Play sound", variable=self.sound).pack(anchor="w")
        ttk.Checkbutton(body, text="Scan the standard emulator save folders", variable=self.defaults).pack(anchor="w")
        ttk.Checkbutton(body, text="Also search Games / GOG Games folders on every drive", variable=self.common).pack(anchor="w")
        ttk.Checkbutton(body, text="Show games you own on Steam (no popups for these - Steam shows its own)",
                        variable=self.steam_library).pack(anchor="w")
        ttk.Checkbutton(body, text="    ...including uninstalled games you unlocked achievements in",
                        variable=self.steam_uninstalled).pack(anchor="w")
        ttk.Checkbutton(body, text="Keep watching when the window is closed", variable=self.keep).pack(anchor="w")
        if startup.supported():
            ttk.Checkbutton(body, text="Start with Windows (in the background)", variable=self.autostart).pack(anchor="w")
        ttk.Checkbutton(body, text="Check for updates", variable=self.check_updates).pack(anchor="w")
        if startup.supported():
            gog_row = ttk.Frame(body)
            gog_row.pack(anchor="w", fill="x")
            ttk.Checkbutton(gog_row, text="Track GOG games (no GOG account needed)",
                            variable=self.gog).pack(side="left")
            state, colour = app.gog_state()
            ttk.Label(gog_row, text=state, foreground=colour).pack(side="left", padx=(8, 0))
            ttk.Label(body, text=gog.SETTINGS_HELP.format(mark=gog.FIX_MARK), wraplength=520, justify="left",
                      foreground="#666").pack(anchor="w", padx=(22, 0), pady=(0, 4))

        ttk.Label(body, text="Checked locations:").pack(anchor="w", pady=(8, 0))
        info = tk.Text(body, height=6, wrap="none", font=("Consolas", 9))
        for root in save_roots(cfg):
            mark = "found  " if root.path.is_dir() else "missing"
            info.insert("end", f"[{mark}] {root.emulator:<16} {root.path}\n")
        for folder in game_folder_list(cfg):
            info.insert("end", f"[found  ] {'Game folder':<16} {folder}\n")
        steam = steam_install_dir()
        info.insert("end", f"[{'found  ' if steam else 'missing'}] {'Steam':<16} {steam or 'not installed'}\n")
        info.configure(state="disabled")
        info.pack(fill="both", expand=True)

        buttons = ttk.Frame(win, padding=(10, 6))
        buttons.pack(side="bottom", fill="x", before=scroll)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(buttons, text="Open data folder", command=self._open_data).pack(side="left")
        # As tall as the options need, but never taller than the screen (minus the taskbar).
        win.update_idletasks()
        wanted = body.winfo_reqheight() + buttons.winfo_reqheight() + 4
        height = min(wanted, win.winfo_screenheight() - int(120 * max(1.0, win.winfo_fpixels("1i") / 96.0)))
        win.geometry(f"{max(680, body.winfo_reqwidth() + scroll.winfo_reqwidth())}x{height}")

    SOUND_LABELS = {"steam": "Steam achievement sound", "chime": "Built-in chime", "custom": "Custom .wav file"}

    def _sound_label(self, style: str, sound_file: str) -> str:
        return self.SOUND_LABELS["custom" if sound_file else style if style in self.SOUND_LABELS else "steam"]

    def _sound_row(self, parent) -> ttk.Frame:
        row = ttk.Frame(parent)
        combo = ttk.Combobox(row, textvariable=self.sound_choice, state="readonly",
                             values=list(self.SOUND_LABELS.values()), width=26)
        combo.pack(side="left")

        def chosen(_event=None) -> None:
            if self.sound_choice.get() == self.SOUND_LABELS["custom"]:
                path = filedialog.askopenfilename(parent=self.win, filetypes=[("WAV audio", "*.wav")])
                if path:
                    self.sound_file = os.path.normpath(path)
                else:
                    self.sound_choice.set(self.SOUND_LABELS["steam"])

        def preview() -> None:
            test = Config(**{**self.app.config.__dict__, "sound": True, **self._sound_settings()})
            play_sound(test)

        combo.bind("<<ComboboxSelected>>", chosen)
        ttk.Button(row, text="Play", command=preview, width=6).pack(side="left", padx=4)
        if steam_sound() is None:
            ttk.Label(row, text="(Steam not found - uses the chime)", foreground="#888").pack(side="left")
        return row

    def _sound_settings(self) -> dict:
        label = self.sound_choice.get()
        style = next((k for k, v in self.SOUND_LABELS.items() if v == label), "steam")
        if style == "custom":
            return {"sound_style": "steam", "sound_file": self.sound_file}
        return {"sound_style": style, "sound_file": ""}

    def _folder_list(self, parent, label: str, values: List[str]) -> tk.Listbox:
        ttk.Label(parent, text=label).pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(2, 6))
        box = tk.Listbox(row, height=4)
        for value in values:
            box.insert("end", value)
        box.pack(side="left", fill="x", expand=True)
        col = ttk.Frame(row)
        col.pack(side="right", padx=(6, 0))

        def add() -> None:
            folder = filedialog.askdirectory(parent=self.win)
            if folder:
                box.insert("end", os.path.normpath(folder))

        ttk.Button(col, text="Add...", command=add).pack(fill="x")
        ttk.Button(col, text="Remove", command=lambda: [box.delete(i) for i in reversed(box.curselection())]).pack(fill="x")
        return box

    def _open_data(self) -> None:
        folder = str(self.app.config.path().parent)
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606 - opens Explorer

    def save(self) -> None:
        cfg = self.app.config
        try:
            cfg.popup_seconds = max(1.0, float(self.seconds.get()))
            cfg.popup_scale = min(3.0, max(0.5, float(self.scale.get())))
        except ValueError:
            messagebox.showerror("Settings", "Popup seconds and size must be numbers.", parent=self.win)
            return
        cfg.game_folders = list(self.game_folders.get(0, "end"))
        cfg.extra_save_roots = list(self.extra_roots.get(0, "end"))
        language = self.language.get().strip() or "english"
        cfg.language = language
        cfg.steam_api_key = self.api_key.get().strip()
        cfg.popup_position = self.position.get()
        cfg.sound = self.sound.get()
        cfg.scan_default_locations = self.defaults.get()
        cfg.keep_running_on_close = self.keep.get()
        cfg.scan_common_game_folders = self.common.get()
        cfg.include_steam_library = self.steam_library.get()
        cfg.check_updates = self.check_updates.get()
        gog_was_on, cfg.gog_enabled = cfg.gog_enabled, self.gog.get()
        if cfg.gog_enabled and not gog_was_on:
            if gog_setup.service_installed() or confirm_gog_setup(self.win):
                self.app.work_queue.put(("gog_setup",))
            else:
                cfg.gog_enabled = False
        elif gog_was_on and not cfg.gog_enabled:
            self.app.stop_gog()
            if gog_setup.service_installed() and confirm_gog_removal(self.win):
                self.app.work_queue.put(("gog_remove",))
        cfg.include_uninstalled_steam = self.steam_uninstalled.get()
        for key, value in self._sound_settings().items():
            setattr(cfg, key, value)
        cfg.save()
        if startup.supported():
            try:
                startup.set_enabled(self.autostart.get())
            except OSError as exc:
                messagebox.showwarning("Settings", f"Couldn't change autostart: {exc}", parent=self.win)
        self.app.rescan()
        self.win.destroy()
