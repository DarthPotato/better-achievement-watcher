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

from . import APP_NAME, __version__, startup
from .config import Config
from .notify import Notifier, PopupSpec, demo_spec, enable_dpi_awareness, load_icon
from .parsers import AchievementState
from .scanner import save_roots
from .schema import GameSchema, SchemaStore
from .watcher import GameProgress, Watcher

INSTANCE_PORT = 47863
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
        self._schema_requested: set = set()
        self._listener: Optional[socket.socket] = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if background:
            self.root.withdraw()

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        self._start_instance_server()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        self.watcher.start()
        self.root.after(100, self._drain_ui_queue)
        self.root.mainloop()

    def quit(self) -> None:
        self.watcher.stop()
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
                        if conn.recv(16).startswith(b"show"):
                            self.ui_queue.put(("show",))
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
            except Exception as exc:
                print(f"[worker] {exc!r}")

    def _prepare_popups(self, appid: int, states: List[AchievementState]) -> None:
        schema = self.store.get(appid, [s.name for s in states])
        for state in states[:BURST_LIMIT]:
            info = schema.info(state.name)
            icon = self.store.icon_file(appid, info)
            self.ui_queue.put(("popup", PopupSpec(schema.title, info.title, info.description, icon, info.percent)))
        extra = len(states) - BURST_LIMIT
        if extra > 0:
            self.ui_queue.put(("popup", PopupSpec(schema.title, f"+{extra} more achievements",
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

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(panes)
        self.game_tree = ttk.Treeview(left, columns=("progress",), show="tree headings", selectmode="browse")
        self.game_tree.heading("#0", text="Game")
        self.game_tree.heading("progress", text="Progress")
        self.game_tree.column("#0", width=230)
        self.game_tree.column("progress", width=80, anchor="center", stretch=False)
        self.game_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(left, command=self.game_tree.yview)
        scroll.pack(side="right", fill="y")
        self.game_tree.configure(yscrollcommand=scroll.set)
        self.game_tree.bind("<<TreeviewSelect>>", self._on_select_game)
        panes.add(left, weight=1)

        right = ttk.Frame(panes, padding=(8, 0, 0, 0))
        self.game_title = ttk.Label(right, text="No game selected", font=("Segoe UI", 12, "bold"))
        self.game_title.pack(anchor="w")
        self.game_meta = ttk.Label(right, text="", foreground="#666")
        self.game_meta.pack(anchor="w")
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

    def refresh(self) -> None:
        self.games = {g.appid: g for g in self.watcher.snapshot()}
        ordered = sorted(self.games.values(), key=lambda g: (-g.last_unlock, g.appid))
        existing = set(self.game_tree.get_children())
        wanted = set()
        for index, game in enumerate(ordered):
            schema = self._schema(game.appid)
            total = len(schema.achievements) or len(game.achievements)
            iid = str(game.appid)
            wanted.add(iid)
            values = (f"{len(game.unlocked)}/{total}",)
            if iid in existing:
                self.game_tree.item(iid, text=schema.title, values=values)
                self.game_tree.move(iid, "", index)
            else:
                self.game_tree.insert("", index, iid=iid, text=schema.title, values=values)
        for iid in existing - wanted:
            self.game_tree.delete(iid)
        files = sum(len(g.sources) for g in self.games.values())
        self.status.configure(text=f"Watching {files} save file(s) for {len(self.games)} game(s). "
                                   f"Leave this running (or close the window - it keeps watching).")
        if self.selected_appid is None and ordered:
            self.game_tree.selection_set(str(ordered[0].appid))
        elif self.selected_appid in self.games:
            self._show_achievements(self.selected_appid)

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
        path = self.store.cache_dir / "icons" / str(appid) / source.rsplit("/", 1)[-1]
        return path if path.is_file() else None

    def _show_achievements(self, appid: int) -> None:
        game = self.games.get(appid)
        if game is None:
            return
        schema = self._schema(appid)
        names = list(schema.achievements) + [n for n in game.achievements if n not in schema.achievements]
        unlocked = [n for n in names if n in game.achievements and game.achievements[n].unlocked]
        locked = [n for n in names if n not in unlocked]
        unlocked.sort(key=lambda n: -game.achievements[n].unlock_time)
        total = len(names)
        self.game_title.configure(text=schema.title)
        emulators = sorted({s.emulator for s in game.sources})
        self.game_meta.configure(text=f"AppID {appid}  •  {len(unlocked)} of {total} unlocked  •  "
                                      f"{', '.join(emulators)}  •  names from {schema.source or 'save file'}")
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

    def rescan(self) -> None:
        self.store.forget()
        self._schema_requested.clear()
        self.work_queue.put(("rescan",))

    # ------------------------------------------------------------ settings

    def open_settings(self) -> None:
        SettingsDialog(self)


class SettingsDialog:
    def __init__(self, app: App):
        self.app = app
        cfg = app.config
        win = self.win = tk.Toplevel(app.root)
        win.title("Settings")
        win.geometry("640x620")
        win.transient(app.root)
        body = ttk.Frame(win, padding=10)
        body.pack(fill="both", expand=True)

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
        rows = [
            ("Language (Steam name, e.g. english, german, french)", ttk.Entry(grid, textvariable=self.language)),
            ("Steam Web API key (optional, rarely needed)", ttk.Entry(grid, textvariable=self.api_key, show="*")),
            ("Popup position", ttk.Combobox(grid, textvariable=self.position, state="readonly",
                                            values=["bottom-right", "bottom-left", "top-right", "top-left"])),
            ("Popup seconds", ttk.Entry(grid, textvariable=self.seconds, width=8)),
            ("Popup size (1.0 = normal)", ttk.Entry(grid, textvariable=self.scale, width=8)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
        grid.columnconfigure(1, weight=1)
        ttk.Checkbutton(body, text="Play sound", variable=self.sound).pack(anchor="w")
        ttk.Checkbutton(body, text="Scan the standard emulator save folders", variable=self.defaults).pack(anchor="w")
        ttk.Checkbutton(body, text="Keep watching when the window is closed", variable=self.keep).pack(anchor="w")
        if startup.supported():
            ttk.Checkbutton(body, text="Start with Windows (in the background)", variable=self.autostart).pack(anchor="w")

        ttk.Label(body, text="Checked save locations:").pack(anchor="w", pady=(8, 0))
        info = tk.Text(body, height=7, wrap="none", font=("Consolas", 9))
        for root in save_roots(cfg):
            mark = "found  " if root.path.is_dir() else "missing"
            info.insert("end", f"[{mark}] {root.emulator:<16} {root.path}\n")
        info.configure(state="disabled")
        info.pack(fill="both", expand=True)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(buttons, text="Open data folder", command=self._open_data).pack(side="left")

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
        cfg.save()
        if startup.supported():
            try:
                startup.set_enabled(self.autostart.get())
            except OSError as exc:
                messagebox.showwarning("Settings", f"Couldn't change autostart: {exc}", parent=self.win)
        self.app.rescan()
        self.win.destroy()
