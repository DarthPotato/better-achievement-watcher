"""Command line entry point.

    python -m baw               open the viewer (and watch for unlocks)
    python -m baw --background  watch silently, popups only
    python -m baw scan          print what was found (good for troubleshooting)
    python -m baw doctor        list every folder that is checked
    python -m baw test-popup    show a sample popup
"""

import argparse
import datetime
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .config import Config


def cmd_scan(config: Config, offline: bool) -> int:
    from .schema import SchemaStore
    from .watcher import Watcher

    store = SchemaStore(config, offline=offline)
    watcher = Watcher(config, store)
    watcher.initial_scan()
    games = sorted(watcher.snapshot(), key=lambda g: -g.last_activity)
    if not games:
        print("No achievement saves found. Run `doctor` to see which folders were checked.")
        return 1
    for game in games:
        schema = store.get(game.appid)
        borrowed = store.borrowed_by(game.appid, game.achievements)
        if borrowed:
            print(f"\n{borrowed} (AppID {game.appid}) - runs under the AppID of {schema.title}; "
                  f"{len(game.unlocked)}/{len(game.achievements)} recorded in the save (raw names)")
            for source in game.sources:
                print(f"  file: {source.path}  [{source.emulator}]")
            for state in game.unlocked:
                print(f"  [x] {state.name}")
            continue
        total = len(schema.achievements) or len(game.achievements)
        hidden = "  [hidden in viewer]" if game.appid in config.hidden_games else ""
        print(f"\n{schema.title} (AppID {game.appid}) - {len(game.unlocked)}/{total} unlocked{hidden}")
        for source in game.sources:
            print(f"  file: {source.path}  [{source.emulator}]")
        for state in sorted(game.unlocked, key=lambda s: -s.unlock_time):
            info = schema.info(state.name)
            when = datetime.datetime.fromtimestamp(state.unlock_time).strftime("%Y-%m-%d %H:%M") if state.unlock_time else "?"
            print(f"  [x] {when}  {info.title}" + (f" - {info.description}" if info.description else ""))
    return 0


def cmd_doctor(config: Config) -> int:
    from .locations import steam_install_dir
    from .notify import sound_path
    from .scanner import game_folder_list, save_roots, scan_game_folders, scan_root
    from .steam_local import stats_files

    print(f"{APP_NAME} {__version__}\nConfig: {config.path()}\n\nSave locations:")
    for root in save_roots(config):
        exists = root.path.is_dir()
        count = len(scan_root(root)) if exists else 0
        print(f"  [{'found' if exists else '  -  '}] {root.emulator:<16} {root.path}" + (f"  ({count} save files)" if exists else ""))
    if config.scan_default_locations:
        from . import native

        for game, path in native.find_saves():
            print(f"  [found] {game.source:<16} {path}")
    if config.gog_enabled:
        from . import gog, gog_setup

        state = gog_setup.status()
        print(f"\nGOG games: service {'installed' if state.service else 'MISSING'}, "
              f"GOG peer {'downloaded' if state.peer else 'MISSING'}")
        for library in game_folder_list(config):
            for folder in gog.find_game_folders(library):
                meta = gog.read_info(folder) or {}
                cached = gog.load_cached(int(meta.get("gameId", 0) or 0))
                keys = f"{len(cached.shown)} achievements" if cached else "keys not worked out yet"
                print(f"  {meta.get('name', folder.name)}  ({keys})  {folder}")
    steam = steam_install_dir()
    print(f"\nSteam: {steam or 'not found'}")
    if steam and config.include_steam_library:
        from .steam_local import installed_appids

        print(f"  {len(stats_files(steam))} games with cached achievement stats, "
              f"{len(installed_appids(steam))} games installed (only installed or played ones are listed)")
    print(f"Popup sound: {sound_path(config)}")
    print("\nGame folders:")
    for folder in game_folder_list(config):
        print(f"  {folder}")
    if not game_folder_list(config):
        print("  (none found - add your games folder in Settings to catch local saves)")
    print("\nGames found in game folders:")
    # A game can have steam_api in several sub-folders (tools, fixes); list it once.
    grouped = {}
    for game in scan_game_folders(config):
        grouped.setdefault((game.appid, game.name), []).append(game)
    for (appid, name), copies in grouped.items():
        main_copy = min(copies, key=lambda g: len(g.path.parts))
        extra = f"  (+{len(copies) - 1} more steam_api folders)" if len(copies) > 1 else ""
        print(f"  AppID {appid:<8} {name}  dll: {main_copy.path}{extra}")
        schema = next((g.schema_file for g in copies if g.schema_file), None)
        print(f"      schema: {schema or '-'}  saves: {sum(len(g.saves) for g in copies)}")
    return 0


def cmd_test_popup(config: Config) -> int:
    import tkinter as tk

    from .notify import Notifier, demo_spec, enable_dpi_awareness, wait_until_idle

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    notifier = Notifier(root, config)
    notifier.show(demo_spec())
    wait_until_idle(root, notifier, timeout=config.popup_seconds + 5)
    root.destroy()
    return 0


def offer_install(config: Config) -> bool:
    """Ask once whether to move the exe to its standard place. Returns True to install."""
    import tkinter as tk
    from tkinter import messagebox

    from .install import install_dir

    root = tk.Tk()
    root.withdraw()
    answer = messagebox.askyesno(
        APP_NAME,
        f"Install {APP_NAME}?\n\nIt will move to\n{install_dir()}\nand get a Start menu shortcut. "
        "Your settings are kept.\n\nChoose No to keep running it from here (you won't be asked again).",
        parent=root)
    root.destroy()
    if not answer:
        config.offer_install = False
        config.save()
    return answer


def use_windows_certificates() -> None:
    """Verify HTTPS through Windows (truststore) when available.

    Python's bundled OpenSSL only trusts certificates it can chain on its own; Windows also
    fetches intermediate certificates a server leaves out, which some CDNs (GOG's) need."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="baw", description=APP_NAME)
    parser.add_argument("command", nargs="?", default="gui", choices=["gui", "scan", "doctor", "test-popup"])
    parser.add_argument("--background", action="store_true", help="start hidden, only show popups")
    parser.add_argument("--offline", action="store_true", help="don't contact Steam (use cached/local names)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    # Used by the exe itself: install/update this copy, and delete a replaced copy afterwards.
    parser.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup", metavar="PATH", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Piped output defaults to the ANSI code page, which can't encode e.g. Japanese names.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    use_windows_certificates()
    config = Config.load()

    if args.command == "scan":
        return cmd_scan(config, args.offline)
    if args.command == "doctor":
        return cmd_doctor(config)
    if args.command == "test-popup":
        return cmd_test_popup(config)

    from . import install

    exe = install.running_exe()
    if exe is not None and args.install:
        target = install.install(exe)
        install.launch(target, "--cleanup", str(exe))
        return 0
    if exe is not None and not args.background and not install.is_installed(exe) and config.offer_install:
        if offer_install(config):
            target = install.install(exe)
            install.launch(target, "--cleanup", str(exe))
            return 0
    if args.cleanup:
        import threading

        threading.Thread(target=install.delete_later, args=(Path(args.cleanup),), daemon=True).start()

    from .app import App, notify_running_instance

    if notify_running_instance(show=not args.background):
        # Already watching: bring that copy's window up instead of watching twice.
        return 0
    App(config, background=args.background, offline=args.offline).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
