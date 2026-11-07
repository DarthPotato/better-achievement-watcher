# GalaxyPeer64.dll: what gates leaderboards (work in progress)

Target: `GalaxyPeer64.dll` 1.2.33.1, build `msvc-17` (sha256 `c0dc94e971afb383d0b8f6aea72a97ab585a176af1323df6de992b07ba530c86`),
downloaded with `baw.gog_setup.install_peer()`. Ghidra 12.1.4 through REA. Addresses are image addresses
(`0x180000000` base). Everything under "Observed" was read from decompiled code. Which MSVC build IRON NEST
loads is not confirmed yet; the other builds should share this logic but have different addresses.

## Observed

1. Every online-only API in the peer starts with the same two guards. For leaderboards the messages are
   `<API>: not signed in with Galaxy` and `<API>: not logged on` (strings `0x18097af48`..`0x18097b8b0`: RequestLeaderboards,
   RequestLeaderboardEntries{Global,AroundUser,ForUsers}, FindLeaderboard, FindOrCreateLeaderboard).
   Score setting, display name, sort method and entry reads have the "signed in" guard only.
   Failing a guard throws a C++ exception (`_CxxThrowException`) that the SDK turns into an error.
2. `FUN_1800a40b0` (FindLeaderboard, guards at `0x1800a4107` / `0x1800a413c`) calls two predicates on the user object at `this+0x48`:
   - `FUN_18004a280` "signed in": `FUN_18004a270() != 0 && *(int *)(obj+0xd4) == 3`
   - `FUN_18004a170` "logged on": `(*(uint *)(obj+0xd8) >> 1) & 1`
   If both pass, it queues `FindLeaderboardInternalEvent`, which later builds a
   `GET_LEADERBOARDS_*` request ("Performing a get leaderboards request" `0x1809b0718`).
3. Sign-in with Galaxy (`FUN_180170300`, logs `OnGetAuthInfoSuccess`) is where our `AUTH_INFO_RESPONSE` lands.
   - With no refresh token (response field at `+0x10` empty) and the sub-operation flag at `+0x110` clear, it takes the offline
     path: it stores the user id/name and carries on. That is the state we are in today.
   - With the flag at `+0x110` set it fails with `Online sign in is required`. (Not hit by our offline sign-in.)
4. `FUN_1803e2990` is the refresh-access-token request. It throws `No refresh token` when there is nothing to refresh with,
   so a made-up refresh token would send the peer to GOG's auth server, which is out of scope (nothing leaves the machine).
5. The peer registers a handler for `galaxy.protocols.communication_service.AuthStateChangeNotification` (class
   `AuthStateChangeNotificationHandler`, constructor `FUN_1802979c0`, vtable `0x1809c5c88`).

6. `AuthStateChangeNotificationHandler` (vtable `0x1809c5c88`: sort 1 / type 0x27 = 39 confirmed from its two tiny methods)
   has its logic in `FUN_180297d30`. `AuthStateChangeNotification` has one field, `refresh_token`
   (Yepoleb/gog_protocols). An empty token takes the error branch; a non-empty one starts a token-based re-authentication
   (the branch that builds a sign-in sub-operation and dispatches it). So type 39 does not set "logged on" by itself.
7. **Where "logged on" is decided.** `FUN_18004acc0` is the plain setter for the flags dword at `user+0xd8`. It has exactly two callers:
   - `FUN_18010cfb0`, the processing of `AuthSuccessFacadeEvent`: `flags = event[+0x100] ? old | 3 : (old & ~2) | 1`.
     Bit 0 = authorized/signed in, bit 1 = logged on. So an auth-success event with its boolean at `+0x100` false gives
     "signed in, not logged on", which is our offline state, and with it true gives "logged on".
   - `FUN_18010cd80`, `AuthLostFacadeEvent`: clears bit 1 (and bit 0 when the event's flag at `+0x30` is set).
   The event is built by `FUN_1800fee00(event, session, userInfo, bool online)`, which stores `online` at `+0x100`.
   Its only caller is `FUN_180138e70(this, userInfo, bool online)`. With `online` true it also creates an extra `__session`
   object and another object (0xe0 bytes) before publishing the event; with false it only publishes the event.
   `FUN_180138e70` has no direct callers (it is reached through a vtable/dispatch at `0x1809911b0`; the other two references
   are exception-table data).

8. **The refresh token is what continues to the online session.** `FUN_180138e70` sits in slot 4 of a listener vtable
   (`0x180991190`), so it is called virtually and has no direct callers. In the sign-in sub-operation state (`FUN_180170300`)
   `+0x119` is set only when `AUTH_INFO_RESPONSE` carried a refresh token, and `+0x11b` marks "auth info received".
   The continuation `FUN_18016f7c0` (only reader of `+0x119`) returns an empty result when `+0x119` is 0, and otherwise
   goes on to the token-based steps (`FUN_18016d490` / `FUN_18016f8e0`) that talk to GOG's auth server. So no token means
   the sign-in ends in the offline state, by design.

9. **The wrapper's handle 0 is just the peer's error passed on.** IRON NEST's `steam_api64.dll` (GalaxySteamWrapper64,
   sha256 `5f2283f4...fc7d`), `ISteamUserStats::FindLeaderboard` = `FUN_180086dc0`: it allocates a call handle, registers a listener
   for it, calls `galaxy::api::Stats()->FindLeaderboard(name)` (vtable slot `+0x110`), then asks `galaxy::api::GetError()`.
   If the SDK reports an error, it drops the listener and returns 0 (Steam's "invalid call"); otherwise it returns the handle.
   So the handle 0 is the peer's "not logged on" exception coming back as an error. Nothing in the wrapper is a separate gate,
   and nothing there can be answered from the stand-in.

10. **IRON NEST does not load the external peer at all.** Test run (stand-in on a copy of the peers whose "logged on" predicate
    `FUN_18004a170` was patched to return true, in `msvc-17` and `msvc-18`): the game sent no `LIBRARY_INFO_REQUEST`, and
    `GalaxyPeer64.dll` was not among its loaded modules. Its own `Galaxy64.dll` (SDK 1.152.10, 14 MB) carries the same peer code
    (strings "not logged on", "OnGetAuthInfoSuccess", "Performing a get leaderboards request", "auth.gog.com" are in it).
    It talked to the stand-in directly: AUTH_INFO_REQUEST, GET_USER_STATS (15), GET_USER_ACHIEVEMENTS (23), nothing more.
    So serving a patched peer copy does not help for games with a recent SDK; the patch would have to go into the game's own
    `Galaxy64.dll` (in memory). The external peer is only used by older SDKs (e.g. 1.139).
    The "logged on" predicate in the msvc-17 peer is `8b 81 d8 00 00 00 d1 e8 83 e0 01 c3` (`mov eax,[rcx+0xd8]; shr eax,1; and eax,1; ret`).

## Conclusion so far

"Logged on" is not something the stand-in's answers can set by themselves. It is the `online` argument of the peer's own
sign-in-finished routine, and the true case also builds an online `__session` object, i.e. the peer expects to have really authenticated
with GOG (via a refresh token it can use). Our AUTH_INFO_RESPONSE without a refresh token correctly gives the offline
(signed in, not logged on) state, and nothing the stand-in sends over 127.0.0.1:9977 reaches the true branch without a token the peer would then
try to use against `auth.gog.com`. That conflicts with the "nothing leaves the machine" rule.

## Still open

- Who calls the `online = true` variant, and what exactly it takes (refresh token present is the likely condition). The dispatch
  at `0x1809911b0` is the place to start.
- Whether leaderboards can be answered another way: Steam-wrapper games call `FindLeaderboard` and the wrapper returns handle 0 *before*
  the peer is involved (see README). The "not logged on" throw is the peer-side half; the wrapper-side half is in `steam_api64.dll`
  (GOG's wrapper), which is not a GOG-server dependency and may be the better thing to look at.
- Which other APIs share the "not logged on" guard (so far: the six leaderboard calls above). Still to survey: lobbies, cloud
  storage, user data, friends/presence, matchmaking.
- Confirm on the machine with the stand-in log (Frida only from a normal terminal, see REA_BRIEF.md), and which `msvc-*` build
  IRON NEST loads.

## Result: in-memory patch of the SDK's logon guard (tested on IRON NEST, SDK 1.152.10)

- In `Galaxy64.dll` the guards are two small shared helpers called at the start of each online API: a signed-in one and
  `FUN_18016ae80(object, "<api>")` (throws `"%s: not logged on"`). `FindLeaderboard` (`FUN_180141700`) calls both.
- `logged_on_hook.js` (Frida) finds that helper without a fixed address: it looks for the text `%s: not logged on`, finds the code
  that loads it (native `lea rip` scan in a CModule, a JS loop is too slow and the game calls FindLeaderboard before the patch lands),
  takes the function from the exception table, and if it is small (< 0x200 bytes) replaces it with a no-op.
- Result: `FindLeaderboard("TestHighScore")` no longer errors, the stand-in receives `GET_LEADERBOARDS_BY_KEY_REQUEST` (40), and
  the game gets past its disclaimer to the main menu. Without the patch: `FindLeaderboard: not logged on`, handle 0.
- The stand-in (`galaxy_standin.py`) answers 31/40 with leaderboard definitions, 33-35 with empty entries, 37 with an accepted score,
  41 with an id. Unanswered, the SDK retries the request every second.
- Older peer builds have per-API inline guards (no small helper); the hook leaves those alone and logs it.
