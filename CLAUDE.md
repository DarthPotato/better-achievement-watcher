# Better Achievement Watcher

Windows desktop app (Python 3.9+, Tk, optional Pillow) that watches the achievement
save files written by Steam / Xbox emulators and shows Steam-style "Achievement unlocked"
popups over the game, plus a viewer of every game's progress. It's a rewrite of xan105's
Achievement Watcher. That app stopped working because its name server shut down (a Steam
Web API key became mandatory), and its Windows toasts get swallowed by Focus Assist.

## Hard limits (don't build these)

- It can't unlock achievements or add playtime on the real Steam account for games the
  user doesn't own. Valve's servers only accept stats for licensed AppIDs, and anything
  that tries to get around that is out of scope.
- Don't bundle Valve's sound files. Steam's achievement sound is played from the user's
  own Steam install (`steamui/sounds/deck_ui_achievement_toast.wav`).

## Layout

| Module | Job |
| --- | --- |
| `baw/__main__.py` | CLI: `gui` (default), `--background`, `scan`, `doctor`, `test-popup`, `--offline` |
| `baw/app.py` | Tk main window (right-click a game to hide it; `Config.hidden_games`), Settings dialog, worker thread, popup pipeline, single instance (localhost:47863) |
| `baw/config.py` | `Config` dataclass ↔ `%APPDATA%\BetterAchievementWatcher\config.json` (`BAW_DATA_DIR` overrides it, used by tests) |
| `baw/locations.py` | Emulator save roots (Goldberg, GSE, every `...\Steam\<group>` folder, EMPRESS, OnlineFix, CreamAPI, SKIDROW, Reloaded/3DM), common game folders on fixed drives, Steam install dir |
| `baw/scanner.py` | Finds save files: AppID subfolders under the roots; in game folders, `steam_api(64).dll` → AppID detection (steam_appid.txt, ini `AppId=`, Unreal layouts), local saves, `local_save_path` / ini `Save*=` paths, Xbox `Achievements.json` |
| `baw/parsers.py` | Generic save parser: JSON (Goldberg/GSE/EMPRESS, Xbox `progressState`), INI per-achievement sections, flat sections, 3DM/RLD hex little-endian values, UTF-16 |
| `baw/steam_local.py` | Owned Steam games from `Steam\appcache\stats\UserGameStats_<account>_<appid>.bin`. Viewer only: never pops up. Steam caches stats for any game page you opened, so only installed (`appmanifest_*.acf` in any library) or played games are listed |
| `baw/native.py` | Games that store achievements in their own save format instead of through an emulator (ReStory: gzip'd JSON in `LocalLow\Mandragora`). One table entry per game: folder, file pattern, parser, AppID |
| `baw/xbox.py` | 1911 Xbox emulator `Achievements.json`: title id as the game id, schema read from the file itself |
| `baw/schema.py` | Achievement names/icons/rarity. Order: keyless `IPlayerService/GetGameAchievements` → Web API (optional key) → `steam_settings/achievements.json` → Steam's `UserGameStatsSchema_*.bin`. Binary KeyValues parser, disk cache, icon cache |
| `baw/watcher.py` | Polls file signatures (mtime, size), merges states per game, reports new unlocks (silent on startup, quiet for owned Steam games) |
| `baw/notify.py` | Popup window (non-activating, click-through, DPI aware), Steam sound or generated chime |
| `baw/startup.py` | "Start with Windows" via the HKCU Run key |

## Working on it

- Tests: `python -m unittest discover -s tests -v` (stdlib only; one test skips without Tk).
- Run from source: `python BetterAchievementWatcher.pyw`, or `python -m baw doctor` / `python -m baw scan`
  to see what's detected.
- Match the existing style: stdlib first, small dataclasses, comments only where the why
  isn't obvious. Parsers should return `None` for "not readable yet" and `{}` for "read, no
  achievements"; the watcher depends on that difference.
- Anything that reads emulator files has to tolerate half-written, odd-encoding and
  unknown files without crashing the watcher thread.
- CI (`.github/workflows/build.yml`) runs the tests on Ubuntu and Windows, builds a
  one-file exe with PyInstaller, and on a published GitHub release attaches the exe to
  that release. The release build uses the workflow from the tagged commit, so tags must
  point at a commit that already has the current workflow.
- Work in branches and open PRs into `main`; the owner reviews and merges.

## Verified vs. not yet verified

Verified with real data: keyless Steam schema lookups, Goldberg/CODEX/RUNE parsing, the
live unlock → popup path (on Linux/Xvfb), the Xbox emulator file format (from a real
sample), the exe build.

Verified on a real Windows 10 PC (2026-11):
- popup over a borderless fullscreen window: the game window gets no WM_ACTIVATE /
  WM_ACTIVATEAPP / WM_KILLFOCUS, the foreground window never changes, and a click on the
  popup lands in the game (tested with a ctypes "fake game" that logs those messages)
- owned-Steam `UserGameStats` reader: same unlocked achievements and identical unlock
  times as the public Steam profile (Muse Dash, Project Redux)
- Steam sound: `deck_ui_achievement_toast.wav` is 16-bit PCM and plays via winsound
- fixed-drive detection (`C:\Games`, `C:\GOG Games`)
- Start with Windows: the Run-key command starts the exe hidden from `System32` as cwd;
  toggling writes/removes the value

Not yet verified: the release-attach step.
