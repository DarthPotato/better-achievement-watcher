# Better Achievement Watcher

Steam-style "Achievement unlocked" popups and an achievement viewer for games running on
Steam emulators (Goldberg, GSE/gbe_fork, CODEX, RUNE, EMPRESS, OnlineFix, CreamAPI, ...).

It watches the files those emulators write when you unlock something. It then shows a
popup over your game with the real name, description, icon, and global rarity from Steam.
The viewer lists every game it found, with progress, unlock dates, and locked achievements.

> This tool is local only. It **cannot** unlock achievements on your actual Steam
> account or add playtime there. See [Why not real Steam achievements?](#why-not-real-steam-achievements)

## Why the original Achievement Watcher "does nothing"

| Problem in the original | What this does instead |
| --- | --- |
| Its achievement-name server was shut down, so it needs your own Steam Web API key. Without one it can't resolve names and fails quietly. | Uses Steam's public `IPlayerService/GetGameAchievements` endpoint, which needs **no key**. Falls back to `steam_settings/achievements.json` and Steam's own schema cache, and caches everything for offline use. |
| Popups are Windows toast notifications. Focus Assist / Do Not Disturb hides them, and Windows turns that on automatically while you're gaming. | Draws its own always-on-top popup. The popup is non-activating (it never steals focus from the game) and clicks pass through it. |
| Doesn't know newer save folders (`GSE Saves`, RUNE, OnlineFix). | Covers them, and accepts any extra folder you add. |
| Setup is unclear when nothing shows up. | `doctor` lists every folder it checks and what it found. The Settings window shows the same list. |

## Getting it

**Windows exe:** every push builds `BetterAchievementWatcher.exe` on GitHub Actions. Open
the latest *build* run under the repo's **Actions** tab, download the
`BetterAchievementWatcher-windows` artifact, and unzip it.

**From source** (Python 3.9+):

```
pip install Pillow          # optional, adds icons
python BetterAchievementWatcher.pyw
```

Click **Test popup** to confirm popups show up, then just play. In **Settings**:

* **Game folders**: add the folder your games are installed in (for example
  `D:\Games`). The app finds each game's `steam_api(64).dll`, reads its AppID, and
  picks up saves stored next to the game ("local save" setups) along with the
  `steam_settings` achievement list.
* **Start with Windows**: runs it hidden in the background at login. Popups still show,
  and launching the app again opens the window.
* Language, popup corner, size, duration, and sound.

Closing the window keeps the app watching. Use **Quit** to stop it.

## Where it looks

| Emulator | Folder |
| --- | --- |
| Goldberg | `%APPDATA%\Goldberg SteamEmu Saves\<AppID>\achievements.json` |
| GSE / gbe_fork | `%APPDATA%\GSE Saves\<AppID>\achievements.json` |
| CODEX | `%PUBLIC%\Documents\Steam\CODEX\<AppID>\achievements.ini`, `%APPDATA%\Steam\CODEX\...` |
| RUNE | `%PUBLIC%\Documents\Steam\RUNE\<AppID>\achievements.ini`, `%APPDATA%\Steam\RUNE\...` |
| EMPRESS | `%APPDATA%\EMPRESS\<AppID>\remote\<AppID>\achievements.json`, `%PUBLIC%\Documents\EMPRESS\...` |
| OnlineFix | `%PUBLIC%\Documents\OnlineFix\<AppID>\Stats\Achievements.ini` |
| CreamAPI | `%APPDATA%\CreamAPI\<AppID>\stats\CreamAPI.Achievements.cfg` |
| SKIDROW | `%LOCALAPPDATA%\SKIDROW\<AppID>\`, `Documents\SKIDROW\<AppID>\` |
| Reloaded / 3DM | `%PROGRAMDATA%\Steam\<user>\<AppID>\stats\achievements.ini` |
| Local saves | Anything under a configured game folder, next to `steam_api(64).dll` |
| Anything else | Add it under **Extra save folders**, laid out as `<folder>\<AppID>\...` |

The parser isn't tied to one crack group. It reads the JSON and INI dialects generically:
`earned`/`Achieved`/`achieved`/`State` flags, decimal or hex little-endian unlock times,
`CurProgress`/`MaxProgress`, and UTF-16 files. A new crack that writes a similar file
usually works once you point an extra folder at it.

Not supported: SmartSteamEmu's binary `stats.bin`, and games whose emulator doesn't save
achievements to disk at all. Some cracks ship without achievement support; there is
nothing to watch for those.

## Troubleshooting

* **No popup in game:** run the game in *borderless windowed* mode. Exclusive fullscreen
  draws over every other window, so only an in-game overlay could appear there.
* **A game isn't listed:** run `python -m baw doctor`. If its save folder isn't
  listed, find where the crack writes `achievements.*` (`steam_emu.ini` / `configs.*.ini`
  next to the game usually say) and add it as a game folder or an extra save folder.
* **Raw names like `ACH_WIN_ONE_GAME`:** the AppID lookup failed (offline, or the game
  was delisted). Names fill in automatically once a lookup succeeds.
* Command line: `python -m baw scan` prints everything it found, and
  `python -m baw test-popup` shows a sample popup.

Settings and caches are stored in `%APPDATA%\BetterAchievementWatcher`.

## Why not real Steam achievements?

Steam achievements and playtime are stored on Valve's servers. They only accept them for
an AppID your account holds a license for. Steam Achievement Manager (SAM) works because
it runs under a game you own. For a game you don't own, the client just gets a failure
back.

When a pirated build talks to the real Steam client, Steam shows the game as running and
creates a "Purchase" library page for it. That's only the client displaying the running
AppID; no license is behind it, so nothing gets recorded.

Family Sharing looks different because the borrower holds a real temporary license while
playing. Playtime and achievements earned during that time count. Afterwards they stay
visible even once the license is revoked.

The only route to real Steam achievements is to own the game. Progress tracked here
doesn't carry over automatically if you buy the game later.
