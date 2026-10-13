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

from . import APP_NAME, __version__
from .config import Config


def cmd_scan(config: Config, offline: bool) -> int:
    from .schema import SchemaStore
    from .watcher import Watcher

    store = SchemaStore(config, offline=offline)
    watcher = Watcher(config, store)
    watcher.initial_scan()
    games = sorted(watcher.snapshot(), key=lambda g: -g.last_unlock)
    if not games:
        print("No achievement saves found. Run `doctor` to see which folders were checked.")
        return 1
    for game in games:
        schema = store.get(game.appid)
        total = len(schema.achievements) or len(game.achievements)
        print(f"\n{schema.title} (AppID {game.appid}) - {len(game.unlocked)}/{total} unlocked")
        for source in game.sources:
            print(f"  file: {source.path}  [{source.emulator}]")
        for state in sorted(game.unlocked, key=lambda s: -s.unlock_time):
            info = schema.info(state.name)
            when = datetime.datetime.fromtimestamp(state.unlock_time).strftime("%Y-%m-%d %H:%M") if state.unlock_time else "?"
            print(f"  [x] {when}  {info.title}" + (f" - {info.description}" if info.description else ""))
    return 0


def cmd_doctor(config: Config) -> int:
    from .scanner import save_roots, scan_game_folders, scan_root

    print(f"{APP_NAME} {__version__}\nConfig: {config.path()}\n\nSave locations:")
    for root in save_roots(config):
        exists = root.path.is_dir()
        count = len(scan_root(root)) if exists else 0
        print(f"  [{'found' if exists else '  -  '}] {root.emulator:<16} {root.path}" + (f"  ({count} save files)" if exists else ""))
    print("\nGame folders:")
    if not config.game_folders:
        print("  (none configured - add your games folder in Settings to catch local saves)")
    for game in scan_game_folders(config):
        print(f"  AppID {game.appid:<8} {game.name}  dll: {game.path}")
        print(f"      schema: {game.schema_file or '-'}  saves: {len(game.saves)}")
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="baw", description=APP_NAME)
    parser.add_argument("command", nargs="?", default="gui", choices=["gui", "scan", "doctor", "test-popup"])
    parser.add_argument("--background", action="store_true", help="start hidden, only show popups")
    parser.add_argument("--offline", action="store_true", help="don't contact Steam (use cached/local names)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)
    config = Config.load()

    if args.command == "scan":
        return cmd_scan(config, args.offline)
    if args.command == "doctor":
        return cmd_doctor(config)
    if args.command == "test-popup":
        return cmd_test_popup(config)

    from .app import App, notify_running_instance

    if notify_running_instance(show=not args.background):
        # Already watching: bring that copy's window up instead of watching twice.
        return 0
    App(config, background=args.background, offline=args.offline).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
