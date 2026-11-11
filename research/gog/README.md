# GOG achievements without GOG Galaxy: research notes

Goal: track achievements of standalone GOG installs (no Galaxy client, no GOG login) by having
the app stand in for Galaxy locally. Tested on Windows 10, November 2026.

**Status: works, and is built into the app** (`baw/gog.py`, `baw/galaxy_service.py`, `baw/gog_setup.py`;
Settings → Track GOG games). This folder keeps the research and the original prototype.

First result: Alder's Blood Prologue (GOG, SDK 1.139) unlocked `CampaignStartedAchi`
through `galaxy_standin.py` with no account and nothing sent to GOG. The game logged
`stats and achievements retrieved`, `Achievement "CampaignStartedAchi" unlocked` and
`OnUserStatsAndAchievementsStoreSuccess`; the stand-in received `UNLOCK_USER_ACHIEVEMENT_REQUEST`.

## How a GOG game talks to Galaxy

1. The game ships GOG's SDK: `Galaxy.dll` / `Galaxy64.dll` (Unity games add `GalaxyCSharp.dll`).
   Older ports go through a Steam-to-Galaxy wrapper: `GalaxyWrp.dll` (Fallout: New Vegas,
   SDK 1.100.2), or a `steam_api64.dll` that reads `GalaxyConfig.json` (IRON NEST, SDK 1.152.10).
2. The SDK asks Windows to start the **`GalaxyCommunication` service** (`OpenSCManagerA`,
   `OpenServiceA`, `StartServiceA`). If no such service exists, it never connects. Comet's
   dummy service (`dummy-service.zip` in imLinguin/comet releases) is an idle service under that
   name; installing it needs admin once, and it then gives Everyone start/stop rights.
3. The SDK connects to **127.0.0.1:9977**. Frames: 2-byte big-endian header length, protobuf
   `gog.protocols.pb.Header` (sort, type, size, oseq), then the payload. Responses copy `oseq`
   into header field 100 (`rseq`). Definitions: <https://github.com/Yepoleb/gog_protocols>.
4. `LIBRARY_INFO_REQUEST` (sort 1, type 1: bitness, compiler, version) is answered with a folder
   holding GOG's **peer** library (`peer/msvc-<n>/GalaxyPeer.dll`, `GalaxyPeer64.dll`; Comet
   downloads it as the public `desktop-galaxy-peer` component). The SDK loads it into the game.
   (SDK 1.139 skipped this message once the peer was known.)
5. The peer sends `AUTH_INFO_REQUEST` (client id and secret, game pid). Answering with a made-up
   user id `(2 << 56) | id`, a user name and no refresh token gives an offline sign-in
   (`Successfully signed in`, `User logged on: False`).
6. `GET_USER_STATS_REQUEST` / `GET_USER_ACHIEVEMENTS_REQUEST`: answered with the game's stat and
   achievement **keys**. The game then calls `SetAchievement(key)` + `StoreStatsAndAchievements()`,
   and the peer sends `UNLOCK_USER_ACHIEVEMENT_REQUEST` (achievement id, unix time) and
   `UPDATE_USER_STAT_REQUEST`.

## The catch was the keys

The SDK checks keys locally against the list it received. An unknown key fails inside the game
(`InvalidArgumentError: Achievement "X" not found for user ...`) and never reaches the stand-in,
which is why the earlier tests looked like the peer "never confirmed" anything.

- GOG keys aren't always Steam's: Alder's Blood uses `CampaignStartedAchi` on GOG and
  `CampaignStarted` on Steam (Alder's Blood: Definitive Edition, AppID 820020).
- They can be found in the game's own files: in Alder's Blood they're enum member names in
  `Assembly-CSharp.dll` (`CampaignStartedAchi`, `SecondMissionFinishedAchi`, `HexesTraveledStat`...),
  readable as plain ASCII. IL2CPP games keep them in `il2cpp_data/Metadata/global-metadata.dat`
  (IRON NEST: `ACHIEVEMENT_1_CLEAR`...).
- Steam's list for the same game gives display names, descriptions and icons; match by
  "contains the Steam key" (`CampaignStartedAchi` contains `CampaignStarted`).
- Unity games that log (`output_log.txt` / `Player.log` in `LocalLow\<company>\<product>`) print
  every failed key, which is the quickest way to check a game.

Steam-wrapper ports work too: FNV unlocked `A31` (its earlier "failures" were console use, which
Bethesda games treat as cheating). IRON NEST gets "stats received" (Steam callback 1101) but calls
`FindLeaderboard` at startup; GOG's peer rejects leaderboards without an online session (the wrapper
returns handle 0) and the game waits forever on its disclaimer screen. Answering its sign-in with 401
(what GOG does for unowned games) makes it start normally without achievements; the app does that for
games in `Config.gog_untracked` (IRON NEST by default). Making leaderboards work needed the SDK's "not logged on" guard
lifted in the game's memory: see `PEER_FINDINGS.md` and `baw/galaxy_hook.py` (the app does this for
IRON NEST when it is tracked).

With the service installed but the app not running, GOG games wait 15-30 s on their splash screen:
Comet's dummy service never reports RUNNING (it stays START_PENDING), so the SDK waits for Galaxy.

## Prototype

```
python galaxy_standin.py <seconds> <log> <peer dir> <steam appid>
python galaxy_standin.py <seconds> <log> <peer dir> "ach=CampaignStartedAchi;stats=HexesTraveledStat,ShotsFiredStat"
```

Practical notes: Frida 17 fails on Windows 10 19045 ("Unable to locate the ntdll loader TLS
routines"); Frida 16 works but not inside Claude Code's sandbox (it can't create its temp folder).
