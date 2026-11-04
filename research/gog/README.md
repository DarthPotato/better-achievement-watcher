# GOG achievements without GOG Galaxy: research notes (parked)

Goal: track achievements of standalone GOG installs (no Galaxy client, no GOG login) by having
the app stand in for Galaxy locally. Tested on Windows 10, November 2026. **Status: parked**:
the handshake works, but GOG's peer DLL never confirms stats, so games never unlock anything.

## How a GOG game talks to Galaxy

1. The game ships GOG's SDK: `Galaxy.dll` / `Galaxy64.dll`. Older ports go through a
   Steam-to-Galaxy wrapper: `GalaxyWrp.dll` (Fallout: New Vegas, SDK 1.100.2), or a
   `steam_api64.dll` that reads `GalaxyConfig.json` (IRON NEST, SDK 1.152.10).
2. The SDK asks Windows to start the **`GalaxyCommunication` service** (`OpenSCManagerA`,
   `OpenServiceA`, `StartServiceA`). If no such service exists, it never connects. Comet's
   dummy service (`dummy-service.zip` in imLinguin/comet releases) is an idle service under that
   name; installing it needs admin once, and it then gives Everyone start/stop rights.
3. The SDK connects to **127.0.0.1:9977**. Frames: 2-byte big-endian header length, protobuf
   `gog.protocols.pb.Header` (sort, type, size, oseq), then the payload. Responses copy `oseq`
   into header field 100 (`rseq`). Definitions: <https://github.com/Yepoleb/gog_protocols>.
4. `LIBRARY_INFO_REQUEST` (sort 1, type 1: bitness, compiler, version "17") is answered with a
   folder holding GOG's **peer** library (`peer/msvc-<n>/GalaxyPeer.dll`, `GalaxyPeer64.dll`;
   Comet downloads it as the public `desktop-galaxy-peer` component). The SDK loads it into the game.
5. The peer then sends `AUTH_INFO_REQUEST` (client id and secret, game pid), `GET_USER_STATS_REQUEST`
   and `GET_USER_ACHIEVEMENTS_REQUEST`. User ids are `(2 << 56) | id`.

`galaxy_standin.py` here answers all of that with a made-up user and an achievement list taken
from the Steam version of the game (the wrapper ports use Steam's API names as GOG keys: FNV `A01`…,
IRON NEST `ACHIEVEMENT_1_CLEAR`…).

## What happens

- Every step above succeeds: the peer loads, signs in as the fake user, and receives the lists.
- After that the peer sends **nothing**: no `UNLOCK_USER_ACHIEVEMENT_REQUEST`, no stat updates.
- A Frida hook on FNV's Steam-style `ISteamUserStats` showed the game makes one
  `RequestCurrentStats` call and then never touches achievements: it never gets the "stats
  received" callback. IRON NEST (`stats_on_init: true`) hangs at load waiting for the same callback.
- Real Comet in offline mode (dummy tokens, seeded `gameplay.db`) behaves identically.
- Cutting the internet doesn't change anything, so it's not GOG's servers rejecting the session:
  the peer itself doesn't accept or confirm the data.

## Next step if picked up again

Find out inside `GalaxyPeer.dll` (for example with REA + Ghidra) what it needs before it fires
`OnUserStatsAndAchievementsRetrieveSuccess`: a field missing from our responses, a signed-in state
that needs a real token, or an online check. Then fix the stand-in, or replace the peer with our own
DLL (point `LIBRARY_INFO_RESPONSE.location` at it), which means implementing GOG's private
SDK-to-peer interface for each SDK version.

Practical notes: Frida 17 fails on Windows 10 19045 ("Unable to locate the ntdll loader TLS
routines"); Frida 16 works but not inside Claude Code's sandbox (it can't create its temp folder).
