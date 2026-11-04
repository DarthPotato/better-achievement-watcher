# Brief for the next GOG session (reverse engineering with REA)

Read [README.md](README.md) in this folder first: it explains the protocol and what already works.
This file says what to do next.

## Goal

Make GOG's peer library (`GalaxyPeer.dll` / `GalaxyPeer64.dll`) accept the local stand-in
(`galaxy_standin.py`), so a game gets its "stats and achievements retrieved" confirmation and
then sends `UNLOCK_USER_ACHIEVEMENT_REQUEST` messages. No GOG account, no traffic to GOG's
servers. Nothing in the app (`baw/`) changes until this works.

## Test game and pass/fail check

IRON NEST: Heavy Turret Simulator (GOG build, `Galaxy64.dll` SDK 1.152.10, `GalaxyConfig.json`
with `"stats_on_init": true`). With the stand-in running, the game currently **hangs at loading**,
because its Steam-to-Galaxy wrapper waits for the stats confirmation.

- **Pass:** the game gets past loading while connected to the stand-in.
- **Then:** landing the first calibration shot should produce
  `UNLOCK_USER_ACHIEVEMENT_REQUEST` for `ACHIEVEMENT_1_CLEAR` in the stand-in log.

Run the stand-in with IRON NEST's Steam AppID for the achievement list:

```
python research/gog/galaxy_standin.py 3600 standin.log <peer dir> 2950790
```

## Setup on the machine

1. Python 3.9+, Git, and REA (`npx rea-agents setup`) with Ghidra or another supported engine.
2. The peer DLLs: run Comet once (`comet --access-token x --refresh-token y --user-id 1 --username test`,
   from imLinguin/comet releases). It downloads them to `%LOCALAPPDATA%\comet\redist\peer`; then stop it.
   That folder is the `<peer dir>` argument.
3. The `GalaxyCommunication` dummy service (needs admin once), from Comet's `dummy-service.zip`:
   `install-dummy-service.bat`. Remove later with
   `sc.exe delete GalaxyCommunication; Remove-Item C:\ProgramData\GOG.com\Galaxy\redists\GalaxyCommunication.exe`.
4. Copy the IRON NEST game folder to the machine.

Port 9977 and the dummy service are the only machine-wide things this touches.

## Where to look in GalaxyPeer64.dll

- What runs when `GET_USER_STATS_RESPONSE` (type 16) and `GET_USER_ACHIEVEMENTS_RESPONSE` (type 24)
  arrive, and what decides between `OnUserStatsAndAchievementsRetrieveSuccess` and `...Failure`.
  Strings to start from: `RequestUserStatsAndAchievementsOperation`,
  `"%s: userID=%s, success=true, statCount=%llu, achievementCount=%llu"`,
  `GameplayGogPbClient`, `GetUserAchievementsListener`.
- Whether it needs a signed-in state that our `AUTH_INFO_RESPONSE` doesn't give (no refresh token),
  or an extra message before or after the lists (for example `START_GAME_SESSION`, an overlay or
  peer-to-server message).
- Whether a field our responses leave out is required (compare with the `UserAchievement` and
  `UserStat` messages in `galaxy.protocols.communication_service.proto`).

Fix the stand-in first. Writing our own replacement peer DLL is the fallback, and a much bigger job.

## Rules

- Local only: never send anything to GOG's servers, and don't fake GOG's auth servers (no hosts-file
  or certificate tricks). The stand-in only answers the game on 127.0.0.1.
- Don't modify game files.
- Work on a branch, keep notes in this folder, and open a PR into `main`.
