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

**Windows exe:** download `BetterAchievementWatcher.exe` from the repo's **Releases**.
Builds of unreleased changes are under **Actions** → *build* → artifact
`BetterAchievementWatcher-windows`.

There's no installer: the exe is the whole app. The first time you run it, it offers to
install itself to `%LOCALAPPDATA%\Programs\Better Achievement Watcher` (where per-user apps
such as VS Code and Discord live, no admin rights needed) with a Start menu shortcut. Your
settings live in `%APPDATA%\BetterAchievementWatcher` either way.

**Updates:** the app checks for a newer release when it starts and shows an *Update now* bar
(or a popup if it's running in the background). It downloads the new version and restarts.
You can turn the check off in Settings. Downloading a new exe and running it works too: it
replaces the installed copy.

**Publishing a release:** on GitHub, *Releases → Draft a new release*, create a new tag
(e.g. `v1.7.0`; any name works) on `main`, and publish. The *build* workflow then builds
the exe with that version number and attaches it to the release within a few minutes.
A tag pushed without a release doesn't trigger this.

**From source** (Python 3.9+):

```
pip install Pillow          # optional, adds icons
python BetterAchievementWatcher.pyw
```

Click **Test popup** to confirm popups show up, then just play. In **Settings**:

* **Game folders**: `Games` and `GOG Games` on every local drive (plus GOG Galaxy's
  Games folder) are searched automatically; add any other folder here. In each one, the
  app finds every game's `steam_api(64).dll`, reads its AppID, and picks up saves
  stored next to the game ("local save" setups, including a custom `local_save_path`),
  along with the `steam_settings` achievement list.
* **Show games you own on Steam**: also lists the achievements of your real Steam
  games, read from Steam's local cache. These never pop up, since Steam already shows
  its own popup for them. Steam caches stats for every game page you open, so only
  installed games are listed. Tick *...including uninstalled games* to also list games
  you played and uninstalled.
* **Sound**: Steam's own achievement sound, played straight from your Steam install
  (`steamui\sounds\deck_ui_achievement_toast.wav`; it isn't bundled with this app).
  If Steam isn't installed it uses a built-in chime. You can also pick any `.wav`.
* **Start with Windows**: runs it hidden in the background at login. Popups still show,
  and launching the app again opens the window.
* Language, popup corner, size, duration, and sound.

Right-click a game in the list to **hide** it, e.g. saves left behind by a game you
uninstalled. Right-click → **Show hidden games** brings them back. Hidden games still
get popups.

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
| Local saves | Anything under a game folder, next to `steam_api(64).dll` |
| Games with their own achievement save | ReStory: `%USERPROFILE%\AppData\LocalLow\Mandragora\Restory\SaveData\achievements.slot*.restory` |
| Epic emulator (Nemirtingas) | `%APPDATA%\NemirtingasEpicEmu\...\achievements.json` (names from `achievements_db.json` next to it) |
| GOG games | Saved by the app itself, see [GOG games](#gog-games) |
| Steam (owned games) | `Steam\appcache\stats\UserGameStats_<account>_<AppID>.bin` (viewer only) |
| Anything else | Add it under **Extra save folders**, laid out as `<folder>\<AppID>\...` |

The parser isn't tied to one crack group. It reads the JSON and INI dialects generically:
`earned`/`Achieved`/`achieved`/`State` flags, decimal or hex little-endian unlock times,
`CurProgress`/`MaxProgress`, and UTF-16 files. A new crack that writes a similar file
usually works once you point an extra folder at it.

Some releases remove the Steam layer instead of using an emulator, and the game then
keeps achievements in its own save format. Those need support per game (ReStory so far).

Some releases run a game under another game's AppID (e.g. Tiny Glade as Age of
Mythology: Retold). When the game's folder name doesn't resemble that AppID's Steam name
and none of the saved achievements belong to it, the game is listed under its folder
name with a note that its achievements can't be tracked.

Not supported: SmartSteamEmu's binary `stats.bin`, and games whose emulator doesn't save
achievements to disk at all. Some cracks ship without achievement support; there is
nothing to watch for those.

### GOG games

GOG installers (without GOG Galaxy) can't record achievements on their own: GOG's SDK in the
game wants to talk to Galaxy. With **Settings → Track GOG games** on, the app stands in for
Galaxy on your PC. No GOG account, nothing is sent to GOG; unlocks are saved in
`%APPDATA%\BetterAchievementWatcher\gog` and pop up like any other game.

* Turning it on downloads GOG's helper library (`GalaxyPeer`, from GOG's own servers) and
  registers a small idle Windows service called `GalaxyCommunication` (one admin prompt). The
  game only connects once that service exists. Settings shows whether it's active. To remove
  it again, untick the option: the app offers to remove the service (one admin prompt) and its
  downloaded files. By hand, in an admin PowerShell:
  `sc.exe delete GalaxyCommunication; Remove-Item C:\ProgramData\GOG.com\Galaxy\redists\GalaxyCommunication.exe`
* The app has to be running when you start the game (Start with Windows takes care of that).
  If it isn't, GOG games wait 15-30 seconds on their splash screen looking for Galaxy, then
  start without achievements.
* Only games that ship GOG's SDK (`Galaxy.dll` / `Galaxy64.dll` in the game folder) can report
  achievements. Older GOG releases without it have nothing to report, with or without Galaxy.
* The game's achievement keys are read from its own files and matched to the Steam version for
  names and icons. A key the app can't find can't unlock.
* Tested: Alder's Blood Prologue, Fallout: New Vegas and IRON NEST (with the memory fix). Bethesda games such as Fallout don't
  award achievements once you've used the console (`~`) in that save.
* Every GOG game is tracked by default (a **GOG** column in the game list shows `● On` or `○ Off`).
  Right-click a game to stop tracking it: it then starts normally without achievements.
* Some games wait for online features (leaderboards) that an offline sign-in can't give, and get
  stuck while loading. **IRON NEST** is one. For those, right-click the game and switch on the
  experimental **Memory fix** (marked with a gear in the list). While the game runs, the app attaches
  to it like a small debugger and patches GOG's SDK in the game's memory so the request goes
  through; nothing in the game's files changes, nothing is sent to GOG, and no extra download is
  needed. It's off for every game until you switch it on, and it applies the next time the game
  starts. A game with anti-tamper protection could refuse to run while attached, and antivirus tools
  can be wary of programs that attach to other programs; if the fix can't attach, the game just runs
  without achievements. Tested with IRON NEST only. Details: `research/gog/PEER_FINDINGS.md`.
* If GOG Galaxy itself is installed and running, it handles the game and the app stays out of
  the way.

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
