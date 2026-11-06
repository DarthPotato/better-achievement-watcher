# Brief: why GOG's peer refuses online features offline (reverse engineering with REA)

Read [README.md](README.md) first: it explains how the app stands in for GOG Galaxy and what
already works (achievements and stats for Alder's Blood Prologue and Fallout: New Vegas).

## The problem

Some GOG games get stuck when the app answers for Galaxy. IRON NEST (Unity, GOG's Steam wrapper
`steam_api64.dll`, SDK 1.152.10, peer `GalaxyPeer64.dll` 1.2.33.1) calls `FindLeaderboard` at
startup. The wrapper returns call handle 0 (Steam's "invalid") straight away, the peer never sends
a leaderboard request to our stand-in, and the game waits forever on its disclaimer screen.
The app works around it by refusing that game's sign-in (it then runs without achievements).

Our sign-in is offline: the `AUTH_INFO_RESPONSE` has a user id and name but no refresh token, and
games log `User logged on: False` (Galaxy's `IsLoggedOn()`). Achievements and stats work in that
state; leaderboards don't.

## Goal

Find out, inside `GalaxyPeer64.dll`, what decides that an online feature is unavailable, and make
leaderboard requests reach the stand-in (which can then answer with empty boards). Then list which
other features are gated the same way.

**Can't be promised for every GOG game.** Each online feature (leaderboards, lobbies/multiplayer,
cloud storage, user data, friends, presence) may be gated separately and would need its own
answers from the stand-in. The deliverable is the explanation plus a fix for leaderboards, and an
honest list of what else games could need.

## Questions to answer

1. What does the peer check before sending `GET_LEADERBOARDS_BY_KEY_REQUEST` (sort 1, type 40) or
   `GET_LEADERBOARDS_REQUEST` (31)? Likely candidates: the logged-on state, the presence of a
   refresh/access token, or a connection to GOG's backend (`notifications-pusher.gog.com`,
   `gameplay.gog.com`).
2. Where is `IsLoggedOn` decided, and what makes it true? Is it only about having a token, or does
   the peer contact GOG's servers itself?
3. Is there a message the communication service can send that sets that state, the way Comet's
   online mode does (`AUTH_STATE_CHANGE_NOTIFICATION`, type 39, or a refresh token in
   `AUTH_INFO_RESPONSE`)? What does the peer do with a refresh token it can't validate?
4. Which other request types are gated by the same check? (Message list:
   `galaxy.protocols.communication_service.proto` in github.com/Yepoleb/gog_protocols.)

## Where to start in GalaxyPeer64.dll

Strings and RTTI names seen in the peer (namespace `gog_1_92_2` / `galaxy::peer`):
`RequestUserStatsAndAchievementsOperation`, `GameplayGogPbClient`, `GetUserAchievementsListener`,
`"%s: userID=%s, success=true, statCount=%llu, achievementCount=%llu"`, host names
`auth.gog.com`, `gameplay.gog.com`, `notifications-pusher.gog.com`. Look for the leaderboard
counterparts (find/request leaderboard operations) and follow what they check before building a
`GET_LEADERBOARDS_*` message.

## Test setup on the machine

- Python 3.9+, Git, REA (`npx rea-agents setup`) with Ghidra or another supported engine.
- This repo, and the app with GOG tracking set up (Settings > Track GOG games). For experiments,
  run `research/gog/galaxy_standin.py` instead of the app (quit the app first; only one can use
  port 9977). It logs every request a game makes.
- IRON NEST, with its id removed from `gog_untracked` in `%APPDATA%\BetterAchievementWatcher\config.json`
  (or right-click > Track this game).
- **Pass:** IRON NEST gets past its disclaimer while tracked, the stand-in log shows a leaderboard
  request, and later its first achievement (`ACHIEVEMENT_1_CLEAR`, the calibration shot) unlocks.
- Frida 16.4.10 works for tracing when started from a normal terminal (not from inside Claude
  Code's sandbox). `research/gog/` has no hook scripts yet; IRON NEST's Steam-side calls can be
  watched by hooking `steam_api64.dll` exports such as `SteamAPI_ManualDispatch_GetNextCallback`
  (callback id at offset 4 of `CallbackMsg_t`).

## Rules

- Local only: nothing is sent to GOG's servers, and no faking of GOG's own servers (no hosts-file
  or certificate tricks). Changes go into the stand-in (what we answer on 127.0.0.1) or into how
  we answer the peer, never into game files or GOG's DLLs.
- Work on a branch, keep findings in this folder, open a PR into `main`.
