import importlib.util
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="baw-test-")
os.environ["BAW_DATA_DIR"] = os.path.join(TMP, "data")

from baw.config import Config  # noqa: E402
from baw.parsers import parse_bytes  # noqa: E402
from baw.scanner import scan_game_folder, scan_save_roots  # noqa: E402
from baw.schema import GameSchema, SchemaStore, parse_binary_kv, schema_from_steam_cache  # noqa: E402
from baw.watcher import Watcher  # noqa: E402


def unlocked(states):
    return {n for n, s in states.items() if s.unlocked}


class ParserTests(unittest.TestCase):
    def test_goldberg_json(self):
        data = {"ACH_A": {"earned": True, "earned_time": 1700000000},
                "ACH_B": {"earned": False, "earned_time": 0}}
        states = parse_bytes(json.dumps(data).encode())
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1700000000)

    def test_gbe_progress(self):
        data = {"ACH_P": {"earned": False, "earned_time": 0, "progress": 3, "max_progress": 10}}
        state = parse_bytes(json.dumps(data).encode())["ACH_P"]
        self.assertFalse(state.unlocked)
        self.assertEqual((state.progress, state.max_progress), (3, 10))

    def test_schema_list_is_not_a_save(self):
        schema = [{"name": "ACH_A", "displayName": "A", "description": "x", "hidden": "0", "icon": "a.jpg"}]
        self.assertEqual(parse_bytes(json.dumps(schema).encode()), {})

    def test_codex_ini(self):
        text = (
            "[SteamAchievements]\nCount=2\n00000=ACH_A\n00001=ACH_B\n\n"
            "[ACH_A]\nAchieved=1\nCurProgress=0\nMaxProgress=0\nUnlockTime=1600000000\n\n"
            "[ACH_B]\nAchieved=0\nCurProgress=2\nMaxProgress=5\nUnlockTime=0\n"
        )
        states = parse_bytes(text.encode())
        self.assertEqual(set(states), {"ACH_A", "ACH_B"})
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1600000000)
        self.assertEqual(states["ACH_B"].max_progress, 5)

    def test_progress_complete_counts_as_unlocked(self):
        states = parse_bytes(b"[ACH_C]\nAchieved=0\nCurProgress=5\nMaxProgress=5\n")
        self.assertTrue(states["ACH_C"].unlocked)

    def test_onlinefix_style(self):
        states = parse_bytes(b"[ACH_A]\nachieved=true\ntimestamp=1650000000\n[ACH_B]\nachieved=false\n")
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1650000000)

    def test_creamapi_style_milliseconds(self):
        states = parse_bytes(b"[ACH_A]\nachieved=1\nunlocktime=1650000000123\n")
        self.assertEqual(states["ACH_A"].unlock_time, 1650000000)

    def test_darksiders_flat(self):
        text = b"[Achievements]\nACH_A=1\nACH_B=0\n[AchievementsUnlockTimes]\nACH_A=1500000000\n"
        states = parse_bytes(text)
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1500000000)

    def test_3dm_hex(self):
        t = struct.pack("<I", 1600000000).hex()
        states = parse_bytes(f"[State]\nACH_A=0101\nACH_B=0000\n[Time]\nACH_A={t}\n".encode())
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1600000000)

    def test_rld_hex_state(self):
        t = struct.pack("<I", 1600000000).hex()
        states = parse_bytes(f"[ACH_A]\nState=01000000\nTime={t}\n[ACH_B]\nState=00000000\n".encode())
        self.assertEqual(unlocked(states), {"ACH_A"})
        self.assertEqual(states["ACH_A"].unlock_time, 1600000000)

    def test_utf16_ini(self):
        data = "﻿[ACH_A]\r\nAchieved=1\r\n".encode("utf-16-le")
        self.assertEqual(unlocked(parse_bytes(data)), {"ACH_A"})

    def test_partial_json_returns_none(self):
        self.assertIsNone(parse_bytes(b'{"ACH_A": {"earned": tr'))

    def test_binary_returns_none(self):
        self.assertIsNone(parse_bytes(bytes(range(256)) * 4))


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    path.write_text(content, encoding="utf-8")
    return path


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP))
        self.env = {
            "APPDATA": str(self.base / "Roaming"),
            "LOCALAPPDATA": str(self.base / "Local"),
            "PUBLIC": str(self.base / "Public"),
            "PROGRAMDATA": str(self.base / "ProgramData"),
            "USERPROFILE": str(self.base / "User"),
        }

    def test_default_roots(self):
        write(self.base / "Roaming/Goldberg SteamEmu Saves/111/achievements.json", {"A": {"earned": True}})
        write(self.base / "Roaming/GSE Saves/222/achievements.json", {"A": {"earned": True}})
        write(self.base / "Public/Documents/Steam/CODEX/333/achievements.ini", "[A]\nAchieved=1\n")
        write(self.base / "Public/Documents/Steam/RUNE/444/achievements.ini", "[A]\nAchieved=1\n")
        write(self.base / "Roaming/EMPRESS/555/remote/555/achievements.json", {"A": {"earned": True}})
        write(self.base / "Public/Documents/OnlineFix/666/Stats/Achievements.ini", "[A]\nachieved=true\n")
        write(self.base / "ProgramData/Steam/Player/777/stats/achievements.ini", "[A]\nState=01000000\n")
        write(self.base / "Roaming/CreamAPI/888/stats/CreamAPI.Achievements.cfg", "[A]\nachieved=1\n")
        write(self.base / "Roaming/Goldberg SteamEmu Saves/settings/achievements.json", {})  # not an appid
        with mock.patch.dict(os.environ, self.env):
            saves = scan_save_roots(Config(include_steam_library=False))
        found = {(s.appid, s.emulator) for s in saves}
        self.assertEqual(found, {
            (111, "Goldberg"), (222, "GSE (gbe_fork)"), (333, "CODEX"), (444, "RUNE"), (555, "EMPRESS"),
            (666, "OnlineFix"), (777, "Reloaded / 3DM"), (888, "CreamAPI"),
        })

    def test_game_folder_local_save(self):
        lib = self.base / "Games"
        game = lib / "Cool Game" / "Binaries" / "Win64"
        write(game / "steam_api64.dll", "")
        write(game / "steam_appid.txt", "999\n")
        write(game / "steam_settings" / "achievements.json", [{"name": "A", "displayName": "Alpha"}])
        write(game / "SAVE" / "999" / "achievements.json", {"A": {"earned": True}})
        games = scan_game_folder(lib)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].appid, 999)
        self.assertEqual(games[0].name, "Cool Game")
        self.assertIsNotNone(games[0].schema_file)
        self.assertEqual([s.path.name for s in games[0].saves], ["achievements.json"])
        self.assertEqual(games[0].saves[0].appid, 999)

    def test_appid_from_ini(self):
        lib = self.base / "Games2"
        write(lib / "G" / "steam_api.dll", "")
        write(lib / "G" / "cream_api.ini", "[config]\nappid = 4242\n")
        self.assertEqual(scan_game_folder(lib)[0].appid, 4242)


class WatcherTests(unittest.TestCase):
    def test_detects_new_unlocks_once(self):
        base = Path(tempfile.mkdtemp(dir=TMP))
        save = write(base / "123" / "achievements.json", {"A": {"earned": True, "earned_time": 1}, "B": {"earned": False}})
        write(base / "999" / "achievements.json", {"Z": {"earned": True}})
        config = Config(scan_default_locations=False, extra_save_roots=[str(base)],
                        scan_common_game_folders=False, include_steam_library=False)
        events = []
        watcher = Watcher(config, SchemaStore(config, offline=True), on_unlock=lambda a, s: events.append((a, [x.name for x in s])))
        watcher.initial_scan()
        self.assertEqual(events, [])  # existing unlocks are not re-announced at startup

        write(save, {"A": {"earned": True, "earned_time": 1}, "B": {"earned": True, "earned_time": 2}})
        os.utime(save, (time.time() + 5, time.time() + 5))
        watcher.poll()
        self.assertEqual(events, [(123, ["B"])])
        watcher.poll()
        self.assertEqual(len(events), 1)

        # A brand new game appearing while watching is announced.
        watcher._last_rediscover = 0
        write(base / "456" / "achievements.ini", "[X]\nAchieved=1\n")
        watcher.poll()
        self.assertEqual(events[-1], (456, ["X"]))

        progress = {g.appid: len(g.unlocked) for g in watcher.snapshot()}
        self.assertEqual(progress, {123: 2, 999: 1, 456: 1})


def kv_map(entries) -> bytes:
    out = b""
    for key, value in entries.items():
        if isinstance(value, dict):
            out += b"\x00" + key.encode() + b"\x00" + kv_map(value)
        elif isinstance(value, int):
            out += b"\x02" + key.encode() + b"\x00" + struct.pack("<i", value)
        else:
            out += b"\x01" + key.encode() + b"\x00" + str(value).encode() + b"\x00"
    return out + b"\x08"


class SchemaTests(unittest.TestCase):
    def test_steam_cache_schema(self):
        steam = Path(tempfile.mkdtemp(dir=TMP))
        root = {"620": {"gamename": "Portal 2", "version": "5", "stats": {"1": {"type": 4, "id": 1, "bits": {
            "0": {"name": "ACH_A", "bit": 0, "display": {
                "name": {"english": "Alpha", "german": "Alfa"}, "desc": {"english": "Do it"},
                "hidden": "0", "icon": "abc.jpg", "icon_gray": "abc_g.jpg"}},
            "1": {"name": "ACH_B", "bit": 1, "display": {"name": {"english": "Beta"}, "desc": {"english": ""}, "hidden": "1"}},
        }}}}}
        data = kv_map(root) + b"\x08"
        self.assertEqual(parse_binary_kv(data)["620"]["gamename"], "Portal 2")
        write(steam / "appcache/stats/UserGameStatsSchema_620.bin", "")
        (steam / "appcache/stats/UserGameStatsSchema_620.bin").write_bytes(data)
        schema = schema_from_steam_cache(620, "german", steam)
        self.assertEqual(schema.name, "Portal 2")
        self.assertEqual(schema.info("ACH_A").display_name, "Alfa")
        self.assertTrue(schema.info("ACH_A").icon.endswith("/apps/620/abc.jpg"))
        self.assertTrue(schema.info("ACH_B").hidden)

    def test_local_steam_settings_and_cache_roundtrip(self):
        game = Path(tempfile.mkdtemp(dir=TMP))
        schema_file = write(game / "steam_settings" / "achievements.json", [
            {"name": "A", "displayName": {"english": "Alpha"}, "description": "first", "hidden": "0",
             "icon": "achievement_images/a.jpg"}])
        write(game / "steam_settings" / "achievement_images" / "a.jpg", "x")
        config = Config()
        store = SchemaStore(config, offline=True)
        store.register_game_dir(31337, "Local Game", schema_file)
        schema = store.get(31337, ["A"])
        self.assertEqual(schema.info("A").title, "Alpha")
        self.assertEqual(schema.title, "Local Game")
        self.assertTrue(Path(schema.info("A").icon).is_file())
        again = GameSchema.from_json(schema.to_json())
        self.assertEqual(again.info("A").description, "first")
        self.assertEqual(SchemaStore(config, offline=True).cached(31337).info("A").title, "Alpha")


if __name__ == "__main__":
    unittest.main()


class MoreDetectionTests(unittest.TestCase):
    def test_unreal_layout_appid_next_to_exe(self):
        lib = Path(tempfile.mkdtemp(dir=TMP))
        root = lib / "Big Game"
        dll = root / "Engine/Binaries/ThirdParty/Steamworks/Steamv157/Win64"
        write(dll / "steam_api64.dll", "")
        write(root / "BigGame/Binaries/Win64/steam_appid.txt", "1234\n")
        write(dll / "steam_settings/achievements.json", [{"name": "A"}])
        games = scan_game_folder(lib)
        self.assertEqual([(g.appid, g.name) for g in games], [(1234, "Big Game")])

    def test_local_save_path_outside_game(self):
        lib = Path(tempfile.mkdtemp(dir=TMP))
        elsewhere = Path(tempfile.mkdtemp(dir=TMP))
        write(lib / "G" / "steam_api64.dll", "")
        write(lib / "G" / "steam_appid.txt", "77")
        write(lib / "G" / "steam_settings" / "configs.user.ini", f"[user::saves]\nlocal_save_path={elsewhere}\n")
        write(elsewhere / "77" / "achievements.json", {"A": {"earned": True}})
        saves = scan_game_folder(lib)[0].saves
        self.assertEqual([(s.appid, s.path.parent.name) for s in saves], [(77, "77")])

    def test_owned_steam_game_stats(self):
        from baw.steam_local import parse_steam_stats, stats_files

        steam = Path(tempfile.mkdtemp(dir=TMP))
        stats = steam / "appcache" / "stats"
        stats.mkdir(parents=True)
        schema = {"620": {"gamename": "Portal 2", "stats": {"1": {"type": 4, "bits": {
            "0": {"name": "ACH_A", "bit": 0, "display": {"name": {"english": "A"}}},
            "1": {"name": "ACH_B", "bit": 1, "display": {"name": {"english": "B"}}},
            "2": {"name": "ACH_C", "bit": 2, "display": {"name": {"english": "C"}}},
        }}}}}
        (stats / "UserGameStatsSchema_620.bin").write_bytes(kv_map(schema) + b"\x08")
        user = {"cache": {"crc": 1, "PendingChanges": 0, "1": {"data": 0b101, "AchievementTimes": {"0": 1700000000, "2": 1700000500}}}}
        (stats / "UserGameStats_11111_620.bin").write_bytes(kv_map(user) + b"\x08")
        (stats / "UserGameStats_22222_620.bin").write_bytes(b"\x08")
        os.utime(stats / "UserGameStats_22222_620.bin", (1, 1))  # older account
        files = stats_files(steam)
        self.assertEqual([(a, p.name) for a, p in files], [(620, "UserGameStats_11111_620.bin")])
        states = parse_steam_stats(files[0][1])
        self.assertEqual(unlocked(states), {"ACH_A", "ACH_C"})
        self.assertEqual(states["ACH_C"].unlock_time, 1700000500)

    def test_owned_steam_games_never_pop_up(self):
        from baw import watcher as watcher_module
        from baw.scanner import SaveFile

        base = Path(tempfile.mkdtemp(dir=TMP))
        path = write(base / "UserGameStats_1_5.bin", "")
        config = Config(scan_default_locations=False, include_steam_library=False, scan_common_game_folders=False)
        events = []
        w = Watcher(config, SchemaStore(config, offline=True), on_unlock=lambda a, s: events.append(a))
        states = [{}, {"X": watcher_module.AchievementState("X", True)}]
        with mock.patch.object(watcher_module, "parse_steam_stats", side_effect=lambda p: states[0]):
            w._steam_installed = {5}
            w._files[path] = watcher_module.TrackedFile(SaveFile(5, path, "Steam"))
            w.poll()
            states.pop(0)
            os.utime(path, (time.time() + 9, time.time() + 9))
            w.poll()
        self.assertEqual(events, [])
        self.assertEqual(len(w.snapshot()[0].unlocked), 1)

    @unittest.skipUnless(importlib.util.find_spec("tkinter"), "needs Tk")
    def test_sound_falls_back_to_chime_without_steam(self):
        from baw import notify

        with mock.patch.object(notify, "steam_install_dir", return_value=None):
            self.assertEqual(notify.sound_path(Config()).name, "chime.wav")
        steam = Path(tempfile.mkdtemp(dir=TMP))
        write(steam / "steamui/sounds/deck_ui_achievement_toast.wav", "RIFF")
        with mock.patch.object(notify, "steam_install_dir", return_value=steam):
            # only the Deck sound there: the default falls back to it
            self.assertEqual(notify.sound_path(Config()).name, "deck_ui_achievement_toast.wav")
            write(steam / "steamui/sounds/desktop_toast_default.wav", "RIFF")
            self.assertEqual(notify.sound_path(Config()).name, "desktop_toast_default.wav")  # desktop client
            self.assertEqual(notify.sound_path(Config(sound_style="steam_deck")).name, "deck_ui_achievement_toast.wav")
            self.assertEqual(notify.sound_path(Config(sound_style="chime")).name, "chime.wav")


class EmulatorGroupTests(unittest.TestCase):
    def test_any_group_under_public_documents_steam(self):
        base = Path(tempfile.mkdtemp(dir=TMP))
        write(base / "Public/Documents/Steam/PLAZA/239140/achievements.ini", "[ACH_A]\nAchieved=1\n")
        write(base / "Roaming/Steam/ANKER/2198150/achievements.ini", "[ACH_B]\nAchieved=1\n")
        env = {"APPDATA": str(base / "Roaming"), "PUBLIC": str(base / "Public"),
               "LOCALAPPDATA": str(base / "Local"), "PROGRAMDATA": str(base / "PD"), "USERPROFILE": str(base / "U")}
        with mock.patch.dict(os.environ, env):
            saves = scan_save_roots(Config(include_steam_library=False))
        self.assertEqual(sorted((s.appid, s.emulator) for s in saves), [(239140, "PLAZA"), (2198150, "ANKER")])

    def test_steam_emu_ini_save_path(self):
        lib = Path(tempfile.mkdtemp(dir=TMP))
        saves_dir = Path(tempfile.mkdtemp(dir=TMP))
        write(lib / "G" / "steam_api64.dll", "")
        write(lib / "G" / "steam_emu.ini", f"[Settings]\nAppId=239140\nSavePath={saves_dir}\n")
        write(saves_dir / "239140" / "achievements.ini", "[ACH_A]\nAchieved=1\n")
        game = scan_game_folder(lib)[0]
        self.assertEqual(game.appid, 239140)
        self.assertEqual([s.appid for s in game.saves], [239140])


class XboxEmulatorTests(unittest.TestCase):
    def test_1911_achievements_json(self):
        lib = Path(tempfile.mkdtemp(dir=TMP))
        items = [
            {"id": "1", "name": "Thank you for playing!", "description": "Got all other achievements.",
             "titleAssociations": [{"name": "Taiko no Tatsujin", "id": 1624264477}], "progressState": "InProgress",
             "progression": {"requirements": [{"current": "2", "target": "100"}], "timeUnlocked": "0001-01-01T00:00:00.0000000Z"},
             "mediaAssets": [{"type": "Icon", "url": "http://images-eds.xboxlive.com/image?url=abc"}], "isSecret": False},
            {"id": "2", "name": "First Song", "description": "Play a song.",
             "titleAssociations": [{"name": "Taiko no Tatsujin", "id": 1624264477}], "progressState": "Achieved",
             "progression": {"requirements": [], "timeUnlocked": "2026-10-01T12:00:00.1234567Z"},
             "mediaAssets": [{"type": "Icon", "url": "http://images-eds.xboxlive.com/image?url=def"}], "isSecret": True},
        ]
        path = write(lib / "Taiko no Tatsujin The Drum Master" / "1911" / "Achievements" / "Achievements.json",
                     {"achievements": items})
        states = parse_bytes(path.read_bytes())
        self.assertEqual(unlocked(states), {"2"})
        self.assertEqual(states["2"].unlock_time, 1790856000)  # 2026-10-01 12:00 UTC
        self.assertEqual((states["1"].progress, states["1"].max_progress), (2, 100))

        games = scan_game_folder(lib)
        self.assertEqual([(g.appid, g.name) for g in games], [(1624264477, "Taiko no Tatsujin The Drum Master")])
        schema = games[0].schema
        self.assertEqual(schema.info("2").title, "First Song")
        self.assertTrue(schema.info("2").icon.startswith("https://images-eds-ssl.xboxlive.com/"))

        config = Config(scan_default_locations=False, include_steam_library=False, scan_common_game_folders=False,
                        game_folders=[str(lib)])
        store = SchemaStore(config, offline=True)
        w = Watcher(config, store)
        w.initial_scan()
        game = w.snapshot()[0]
        self.assertEqual(len(game.unlocked), 1)
        self.assertEqual(store.get(1624264477).title, "Taiko no Tatsujin The Drum Master")
        icon_a = store.icon_cache_path(1, schema.info("1").icon)
        icon_b = store.icon_cache_path(1, schema.info("2").icon)
        self.assertNotEqual(icon_a, icon_b)

    def test_empty_emulator_save_still_listed(self):
        base = Path(tempfile.mkdtemp(dir=TMP))
        write(base / "239140" / "achievements.ini", "[SteamAchievements]\nCount=0\n")
        config = Config(scan_default_locations=False, include_steam_library=False, scan_common_game_folders=False,
                        extra_save_roots=[str(base)])
        w = Watcher(config, SchemaStore(config, offline=True))
        w.initial_scan()
        self.assertEqual([(g.appid, len(g.unlocked)) for g in w.snapshot()], [(239140, 0)])


class NativeSaveTests(unittest.TestCase):
    def test_restory_save(self):
        import gzip

        from baw.native import parse_restory

        user = Path(tempfile.mkdtemp(dir=TMP))
        folder = user / "AppData/LocalLow/Mandragora/Restory/SaveData"
        folder.mkdir(parents=True)
        path = folder / "achievements.slot1.release.restory"

        def save(*labels):
            data = {"UnlockedAchievements": [{"AchievementID": label} for label in labels],
                    "$version": "AchievementsManagerSaveDataV02"}
            path.write_bytes(gzip.compress(json.dumps(data).encode()))

        save("Achievement - 1 First repair!", "Achievement - Twist-and-turn")
        (folder / "gameSettings.slot1.release.restory").write_bytes(gzip.compress(b"{}"))
        self.assertEqual(unlocked(parse_restory(path)), {"ACH_FIRST_REPAIR", "ACH_TWIST_AND_TURN"})
        path.with_name("half.restory").write_bytes(gzip.compress(b"{}")[:10])
        self.assertIsNone(parse_restory(path.with_name("half.restory")))

        config = Config(include_steam_library=False, scan_common_game_folders=False)
        events = []
        w = Watcher(config, SchemaStore(config, offline=True), on_unlock=lambda a, s: events.append((a, [x.name for x in s])))
        with mock.patch.dict(os.environ, {"USERPROFILE": str(user), "APPDATA": str(user / "R"),
                                          "LOCALAPPDATA": str(user / "L"), "PUBLIC": str(user / "P"),
                                          "PROGRAMDATA": str(user / "PD")}):
            w.initial_scan()
            self.assertEqual([(g.appid, len(g.unlocked)) for g in w.snapshot()], [(3812600, 2)])
            save("Achievement - 1 First repair!", "Achievement - Twist-and-turn", "Achievement - How did I do it?")
            os.utime(path, (time.time() + 5, time.time() + 5))
            w.poll()
        self.assertEqual(events, [(3812600, ["ACH_HOW_DID_I_DO_IT"])])

    def test_placeholder_steam_names_ignored(self):
        from baw.schema import _real_name

        self.assertEqual(_real_name("ValveTestApp34250"), "")
        self.assertEqual(_real_name("Portal 2"), "Portal 2")


class StartupTests(unittest.TestCase):
    def test_repair_repoints_moved_exe(self):
        from baw import startup

        calls = []
        with mock.patch.object(startup, "supported", return_value=True), \
                mock.patch.object(startup.sys, "frozen", True, create=True), \
                mock.patch.object(startup, "set_enabled", side_effect=calls.append):
            with mock.patch.object(startup, "_registered_command", return_value=r'"Z:\gone\BAW.exe" --background'):
                startup.repair()
            with mock.patch.object(startup, "_registered_command", return_value=f'"{__file__}" --background'):
                startup.repair()
            with mock.patch.object(startup, "_registered_command", return_value=""):
                startup.repair()
        self.assertEqual(calls, [True])


class SteamListingTests(unittest.TestCase):
    def test_only_installed_steam_games_listed(self):
        from baw import watcher as watcher_module
        from baw.scanner import SaveFile
        from baw.steam_local import installed_appids

        steam = Path(tempfile.mkdtemp(dir=TMP))
        other = Path(tempfile.mkdtemp(dir=TMP))
        write(steam / "steamapps/appmanifest_1.acf", "")
        write(steam / "steamapps/libraryfolders.vdf", '"libraryfolders"\n{\n "1"\n {\n  "path"  "%s"\n }\n}\n'
              % str(other).replace("\\", "\\\\"))
        write(other / "steamapps/appmanifest_2.acf", "")
        self.assertEqual(installed_appids(steam), {1, 2})

        config = Config(scan_default_locations=False, include_steam_library=False)
        w = Watcher(config, SchemaStore(config, offline=True))
        w._steam_installed = {1, 2}
        locked = {"A": watcher_module.AchievementState("A", False)}
        played = {"A": watcher_module.AchievementState("A", True)}
        for appid, states in ((1, locked), (2, {}), (3, locked), (4, played)):
            path = Path(f"UserGameStats_9_{appid}.bin")
            w._files[path] = watcher_module.TrackedFile(SaveFile(appid, path, "Steam"), states=states)
        # 1: installed, 2: installed but no achievements, 3: only looked at, 4: played, uninstalled
        self.assertEqual(sorted(g.appid for g in w.snapshot()), [1])
        config.include_uninstalled_steam = True
        self.assertEqual(sorted(g.appid for g in w.snapshot()), [1, 4])


@unittest.skipUnless(importlib.util.find_spec("tkinter"), "needs Tk")
class HideGameTests(unittest.TestCase):
    def test_hide_and_unhide_from_menu(self):
        import tkinter as tk

        try:
            tk.Tk().destroy()
        except tk.TclError:
            self.skipTest("no display")
        from baw.app import App

        base = Path(tempfile.mkdtemp(dir=TMP))
        write(base / "111" / "achievements.json", {"A": {"earned": True, "earned_time": 5}})
        write(base / "222" / "achievements.ini", "[SteamAchievements]\nCount=0\n")
        config = Config(scan_default_locations=False, include_steam_library=False, scan_common_game_folders=False,
                        extra_save_roots=[str(base)])
        app = App(config, offline=True)
        try:
            app.watcher.initial_scan()
            app.refresh()
            app.root.update()
            app.game_menu.tk_popup = lambda *a: None
            event = mock.Mock(x=5, y=app.game_tree.bbox("222")[1] + 2, x_root=0, y_root=0)
            app._on_game_menu(event)
            self.assertEqual(app.game_menu.entrycget(0, "label"), "Hide game")
            app.game_menu.invoke(0)
            self.assertEqual(Config.load().hidden_games, [222])
            self.assertFalse(app.game_tree.exists("222"))
            app.show_hidden.set(True)
            app.refresh()
            self.assertEqual(app.game_tree.item("222", "tags"), ("hidden",))
            app.set_hidden(222, False)
            self.assertEqual(app.config.hidden_games, [])
        finally:
            app.root.destroy()


class BorrowedAppIdTests(unittest.TestCase):
    def test_names_match(self):
        from baw.schema import names_match

        for steam, folder in (("Dying Light", "Dying Light - Platinum Edition"), ("Tiny Glade", "TinyGlade"),
                              ("Kingdom Come: Deliverance II", "KCD2"), ("Wizard with a Gun", "WWAG"),
                              ("Cyberpunk 2077", "CP2077"), ("Total War: SHOGUN 2", "Shogun2"), ("11001", "Whatever")):
            self.assertTrue(names_match(steam, folder), (steam, folder))
        for steam, folder in (("Age of Mythology: Retold", "Tiny-Glade-AnkerGames"), ("Portal 2", "Half-Life 2")):
            self.assertFalse(names_match(steam, folder), (steam, folder))

    def test_borrowed_by(self):
        config = Config()
        store = SchemaStore(config, offline=True)
        schema = GameSchema(1934680, "Age of Mythology: Retold", source="test")
        schema.achievements["ACH_WIN"] = schema.info("ACH_WIN")
        store._schemas[1934680] = schema
        self.assertEqual(store.borrowed_by(1934680, []), "")  # not found in a game folder
        store.register_game_dir(1934680, "Tiny-Glade-AnkerGames", None)
        self.assertEqual(store.borrowed_by(1934680, []), "Tiny-Glade-AnkerGames")
        self.assertEqual(store.borrowed_by(1934680, ["ach_win"]), "")  # saves prove it's the real game
        store.register_game_dir(1934680, "Age of Mythology Retold", None)
        self.assertEqual(store.borrowed_by(1934680, []), "")


class NameAndFilterTests(unittest.TestCase):
    def test_store_name_beats_steam_cache_name(self):
        from baw import schema as schema_module

        with_achievements = GameSchema(1599340, "", source="Steam")
        with_achievements.achievements["A"] = with_achievements.info("A")
        cache = GameSchema(1599340, "Lost Ark Test 1", dict(with_achievements.achievements), source="Steam cache")
        config = Config()
        with mock.patch.object(schema_module, "schema_from_keyless_api", return_value=with_achievements), \
                mock.patch.object(schema_module, "schema_from_steam_cache", return_value=cache), \
                mock.patch.object(schema_module, "game_name_from_store", return_value="Lost Ark"):
            store = SchemaStore(config)
            self.assertEqual(store.get(1599340).title, "Lost Ark")
        # An old cache file holding the cache name gets the store name on the next lookup.
        old = GameSchema(77, "Lost Ark Test 1", dict(with_achievements.achievements), source="Steam")
        store._schemas[77] = GameSchema.from_json(old.to_json())
        with mock.patch.object(schema_module, "game_name_from_store", return_value="Real Name"):
            self.assertEqual(store.get(77).title, "Real Name")

    def test_source_filters(self):
        from baw.app import FILTERS

        self.assertTrue(FILTERS["Steam"]({"Steam"}))
        self.assertFalse(FILTERS["Steam"]({"RUNE"}))
        self.assertTrue(FILTERS["Emulators & other"]({"RUNE", "Local save"}))
        self.assertTrue(all(FILTERS["All games"](e) for e in ({"Steam"}, {"RUNE"})))


class UpdateInstallTests(unittest.TestCase):
    def test_versions(self):
        from baw.update import is_newer, parse_version

        self.assertEqual(parse_version("v1.7.1"), (1, 7, 1))
        self.assertEqual(parse_version("nightly"), ())
        self.assertTrue(is_newer("1.8.0", "1.7.1"))
        self.assertTrue(is_newer("v1.7.10", "1.7.9"))
        self.assertFalse(is_newer("1.7.1", "1.7.1"))
        self.assertFalse(is_newer("1.8.0", "dev"))

    def test_latest_release_needs_the_exe(self):
        from baw import update

        def fake(data):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(data).encode()
            return mock.patch.object(update.urllib.request, "urlopen", return_value=response)

        asset = {"name": "BetterAchievementWatcher.exe", "browser_download_url": "https://x/BAW.exe"}
        with fake({"tag_name": "v1.8.0", "html_url": "https://x/r", "assets": [asset]}):
            self.assertEqual(update.latest_release(), update.Release("1.8.0", "https://x/r", "https://x/BAW.exe"))
        with fake({"tag_name": "v1.8.0", "assets": []}):  # build still running
            self.assertIsNone(update.latest_release())

    def test_install_copies_and_repoints_startup(self):
        from baw import install

        base = Path(tempfile.mkdtemp(dir=TMP))
        source = write(base / "Downloads" / "BetterAchievementWatcher.exe", "new build")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base / "Local"), "APPDATA": str(base / "Roaming")}), \
                mock.patch.object(install, "create_shortcut", return_value=True) as shortcut, \
                mock.patch.object(install, "stop_running_instance") as stop, \
                mock.patch.object(install.startup, "supported", return_value=True), \
                mock.patch.object(install.startup, "is_enabled", return_value=True), \
                mock.patch.object(install.startup, "set_enabled") as set_enabled:
            write(install.installed_exe(), "old build")
            target = install.install(source)
            self.assertEqual(target, base / "Local" / "Programs" / "Better Achievement Watcher" / "BetterAchievementWatcher.exe")
            self.assertEqual(target.read_text(encoding="utf-8"), "new build")
            self.assertTrue(install.is_installed(target))
            self.assertFalse(install.is_installed(source))
            stop.assert_called_once()
            shortcut.assert_called_once()
            set_enabled.assert_called_once_with(True, exe=target)

    def test_display_names(self):
        from baw.scanner import display_name

        for folder, shown in (("Tiny-Glade-AnkerGames", "Tiny Glade"), ("Hades.II.v1.0.2-RUNE", "Hades II"),
                              ("Dying Light - Platinum Edition", "Dying Light - Platinum Edition"),
                              ("R.E.P.O", "R.E.P.O"), ("Half-Life 2", "Half-Life 2"), ("GOG", "GOG")):
            self.assertEqual(display_name(folder), shown)


class GogTests(unittest.TestCase):
    def _fake_game(self, base: Path, keys=("CampaignStartedAchi", "SecretBossAchi", "SetAchi", "HexesTraveledStat")):
        folder = base / "Alder's Blood Prologue"
        write(folder / "goggame-2018305509.info", {"gameId": "2018305509", "clientId": "52573467997220792",
                                                  "name": "Alder's Blood Prologue"})
        write(folder / "Galaxy64.dll", "")
        managed = folder / "Alder's Blood Prologue_Data" / "Managed"
        managed.mkdir(parents=True)
        (managed / "Assembly-CSharp.dll").write_bytes(b"\x00".join(k.encode() for k in keys) + b"\x00")
        return folder

    def test_key_matching(self):
        from baw import gog

        self.assertEqual(gog.norm("CampaignStartedAchi"), gog.norm("CampaignStarted"))
        self.assertEqual(gog.norm("ACHIEVEMENT_1_CLEAR"), "1_clear")
        self.assertNotEqual(gog.norm("A4_4"), gog.norm("A44"))
        self.assertFalse(gog.looks_like_key("SetAchi"))
        self.assertFalse(gog.looks_like_key("Achievement"))
        self.assertTrue(gog.looks_like_key("SecretBossAchi"))
        self.assertEqual(gog.display_from_key("BackstabsMadeAchi"), "Backstabs Made")
        steam = GameSchema(820020, "Alder's Blood")
        for key, title in (("CampaignStarted", "Save us"), ("CampaignFinished", "The last hunt")):
            steam.achievements[key] = gog.AchievementInfo(key, title)
        aliases, shown, info = gog.match_keys({"CampaignStartedAchi", "SecretBossAchi", "Unrelated"}, steam)
        self.assertEqual(aliases["CampaignStartedAchi"], "CampaignStarted")
        self.assertIn("CampaignFinished", aliases)  # Steam's own spelling is offered too
        self.assertEqual(shown, ["CampaignStarted", "SecretBossAchi"])  # only what the game's files mention
        self.assertEqual(info["SecretBossAchi"]["display_name"], "Secret Boss")

    def test_find_and_build_game(self):
        from baw import gog

        base = Path(tempfile.mkdtemp(dir=TMP))
        folder = self._fake_game(base)
        write(base / "Old DOS game" / "goggame-1.info", {"gameId": "1", "clientId": "2", "name": "No SDK"})
        self.assertEqual(gog.find_game_folders(base), [folder])
        game = gog.build_game(folder, offline=True)
        self.assertEqual((game.game_id, game.client_id), (2018305509, "52573467997220792"))
        self.assertEqual(game.shown, ["CampaignStartedAchi", "SecretBossAchi"])
        self.assertEqual(game.stats, ["HexesTraveledStat"])
        self.assertEqual(gog.load_cached(game.game_id), game)
        self.assertEqual(json.loads(gog.save_path(game.game_id).read_text()), {})  # listed before any unlock
        self.assertTrue(gog.record_unlock(game.game_id, "SecretBossAchi", 1700000000))
        self.assertFalse(gog.record_unlock(game.game_id, "SecretBossAchi"))
        self.assertEqual(unlocked(parse_bytes(gog.save_path(game.game_id).read_bytes())), {"SecretBossAchi"})

    def test_galaxy_service_end_to_end(self):
        import socket

        from baw import galaxy_proto as pb
        from baw import gog
        from baw.galaxy_service import GalaxyService

        base = Path(tempfile.mkdtemp(dir=TMP))
        game = gog.build_game(self._fake_game(base, keys=("FirstBloodAchi", "ShotsStat")), offline=True)
        gog.reset(game.game_id)
        service = GalaxyService(base / "peer", lambda client, pid: game if client == game.client_id else None,
                                log=lambda m: None, port=0)
        self.assertTrue(service.start())
        try:
            with socket.create_connection(("127.0.0.1", service.port), timeout=5) as conn:
                def call(type_, payload, seq):
                    header = pb.uint(1, 1) + pb.uint(2, type_) + pb.uint(3, len(payload)) + pb.uint(4, seq)
                    conn.sendall(len(header).to_bytes(2, "big") + header + payload)
                    buffer, replies = b"", []
                    while not any(pb.first(h, 100) == seq for h, _ in replies):
                        buffer += conn.recv(65536)
                        frames, buffer = pb.split_frames(buffer)
                        replies += frames
                    return next((h, p) for h, p in replies if pb.first(h, 100) == seq)

                header, payload = call(3, pb.string(1, game.client_id) + pb.uint(5, 1234), 1)
                self.assertEqual(pb.first(header, 2), 4)  # AUTH_INFO_RESPONSE
                self.assertEqual(pb.text(pb.decode(payload), 4), "Player")
                _, payload = call(15, b"", 2)
                stat = pb.decode(pb.decode(payload)[1][0])
                self.assertEqual(pb.text(stat, 2), "ShotsStat")
                _, payload = call(23, b"", 3)
                entries = [pb.decode(e) for e in pb.decode(payload)[1]]
                ids = {pb.text(e, 2): pb.first(e, 1) for e in entries}
                self.assertIn("FirstBloodAchi", ids)
                call(17, pb.fixed64(1, pb.first(stat, 1)) + pb.uint(2, 1) + pb.uint(4, 7), 4)
                call(25, pb.fixed64(1, ids["FirstBloodAchi"]) + pb.fixed32(2, 1790000000), 5)
                header, _ = call(99, b"", 6)
                self.assertEqual(pb.first(header, 101), 501)  # unknown requests get "not implemented"
        finally:
            service.stop()
        self.assertEqual(gog.unlocks(game.game_id), {"FirstBloodAchi": 1790000000})
        self.assertEqual(gog.stat_values(game.game_id), {"ShotsStat": 7})

    def test_peer_download_checks_hashes(self):
        import hashlib
        import io
        import zipfile

        from baw import gog_setup

        dll = b"MZ fake peer"
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("GalaxyPeer64.dll", dll)
        manifest = {"version": "1.2.3", "baseURI": "https://cdn/x", "files": [
            {"path": "peer/msvc-18/GalaxyPeer64.dll", "resource": "r1", "sha256": hashlib.sha256(dll).hexdigest()}]}
        responses = {gog_setup.PEER_MANIFEST: json.dumps(manifest).encode(), "https://cdn/x/r1": packed.getvalue()}
        with mock.patch.object(gog_setup, "_get", side_effect=lambda url, timeout=60: responses[url]):
            self.assertEqual(gog_setup.install_peer(), "1.2.3")
            self.assertEqual((gog_setup.peer_dir() / "msvc-18" / "GalaxyPeer64.dll").read_bytes(), dll)
            manifest["version"] = "1.2.4"
            manifest["files"][0]["sha256"] = "0" * 64
            responses[gog_setup.PEER_MANIFEST] = json.dumps(manifest).encode()
            with self.assertRaises(ValueError):
                gog_setup.install_peer()


@unittest.skipUnless(importlib.util.find_spec("tkinter"), "needs Tk")
class UpdateRecheckTests(unittest.TestCase):
    def test_rechecks_while_running(self):
        import tkinter as tk

        try:
            tk.Tk().destroy()
        except tk.TclError:
            self.skipTest("no display")
        from baw import app as app_module

        config = Config(scan_default_locations=False, include_steam_library=False, scan_common_game_folders=False)
        app = app_module.App(config, offline=True)
        try:
            scheduled = []
            with mock.patch.object(app_module.install, "running_exe", return_value=Path("C:/x/BAW.exe")), \
                    mock.patch.object(app.root, "after", side_effect=lambda ms, fn: scheduled.append(ms)):
                app._schedule_update_check()
            self.assertEqual(app.work_queue.get_nowait(), ("update_check",))
            self.assertEqual(scheduled, [app_module.UPDATE_RECHECK_MS])
            scheduled.clear()
            with mock.patch.object(app_module.install, "running_exe", return_value=None), \
                    mock.patch.object(app.root, "after", side_effect=lambda ms, fn: scheduled.append(ms)):
                app._schedule_update_check()  # from source: no checks at all
            self.assertEqual(scheduled, [])
        finally:
            app.root.destroy()


class GogMoreTests(unittest.TestCase):
    def test_client_id_from_galaxy_config_and_reset(self):
        from baw import gog

        folder = Path(tempfile.mkdtemp(dir=TMP)) / "IRON NEST"
        write(folder / "goggame-1162687982.info", {"gameId": "1162687982", "clientId": None, "name": "IRON NEST"})
        write(folder / "Iron Nest_Data" / "Plugins" / "x86_64" / "Galaxy64.dll", "")
        write(folder / "GalaxyConfig.json", {"client_id": "59627447589917714", "steam_appid": 2950790})
        self.assertEqual(gog.read_info(folder)["clientId"], "59627447589917714")
        self.assertEqual(gog._steam_appid_in_folder(folder), 2950790)
        game = gog.build_game(folder, offline=True)
        gog.record_unlock(game.game_id, "X", 5)
        gog.set_stat(game.game_id, "S", 3)
        gog.reset(game.game_id)
        self.assertEqual(json.loads(gog.save_path(game.game_id).read_text()), {})  # still listed, at 0
        self.assertEqual(gog.stat_values(game.game_id), {})
        gog.log("hello")
        self.assertIn("hello", gog.log_path().read_text(encoding="utf-8"))


class GogUntrackedTests(unittest.TestCase):
    def test_untracked_game_is_refused(self):
        import socket

        from baw import galaxy_proto as pb
        from baw import gog
        from baw.galaxy_service import GalaxyService

        self.assertEqual(Config().gog_untracked, [])  # every GOG game is tracked by default
        folder = Path(tempfile.mkdtemp(dir=TMP)) / "G"
        write(folder / "goggame-77.info", {"gameId": "77", "clientId": "123", "name": "G"})
        write(folder / "Galaxy64.dll", "")
        game = gog.build_game(folder, offline=True)
        service = GalaxyService(folder, lambda c, p: game, log=lambda m: None, port=0, tracked=lambda g: False)
        self.assertTrue(service.start())
        try:
            with socket.create_connection(("127.0.0.1", service.port), timeout=5) as conn:
                payload = pb.string(1, "123")
                header = pb.uint(1, 1) + pb.uint(2, 3) + pb.uint(3, len(payload)) + pb.uint(4, 1)
                conn.sendall(len(header).to_bytes(2, "big") + header + payload)
                buffer, frames = b"", []
                while not frames:
                    buffer += conn.recv(4096)
                    frames, buffer = pb.split_frames(buffer)
        finally:
            service.stop()
        header, _ = frames[0]
        self.assertEqual((pb.first(header, 2), pb.first(header, 101)), (4, 401))  # sign-in refused


class EpicEmulatorTests(unittest.TestCase):
    def test_nemirtingas_saves(self):
        from baw import epic

        base = Path(tempfile.mkdtemp(dir=TMP))
        folder = base / "Roaming" / "NemirtingasEpicEmu" / "Fall Guys" / "0123456789abcdef0123456789abcdef"
        write(folder / "achievements_db.json", {
            "ACH_WIN": {"achievement_id": "ACH_WIN", "unlocked_display_name": "Winner",
                        "unlocked_description": "Win a show", "unlocked_icon_url": "https://x/win.png",
                        "locked_icon_url": "https://x/win_l.png", "is_hidden": False},
            "ACH_JUMP": {"achievement_id": "ACH_JUMP", "unlocked_display_name": "Jumper", "is_hidden": True}})
        save = write(folder / "achievements.json", {
            "ACH_WIN": {"achievement_id": "ACH_WIN", "progress": 1.0, "unlock_time": 1700000000},
            "ACH_JUMP": {"achievement_id": "ACH_JUMP", "progress": 0.5, "unlock_time": -1}})
        states = epic.parse(save)
        self.assertEqual(unlocked(states), {"ACH_WIN"})
        self.assertEqual(states["ACH_WIN"].unlock_time, 1700000000)
        self.assertEqual(epic._unix_time(1700000000123), 1700000000)  # milliseconds
        self.assertEqual(epic._unix_time(123456789), 0)  # a monotonic clock, not a date
        self.assertEqual(epic.game_name(save), "Fall Guys")

        config = Config(include_steam_library=False, scan_common_game_folders=False)
        store = SchemaStore(config, offline=True)
        env = {"APPDATA": str(base / "Roaming"), "LOCALAPPDATA": str(base / "L"), "PUBLIC": str(base / "P"),
               "PROGRAMDATA": str(base / "PD"), "USERPROFILE": str(base / "U")}
        with mock.patch.dict(os.environ, env):
            w = Watcher(config, store)
            w.initial_scan()
        game = w.snapshot()[0]
        self.assertEqual(len(game.unlocked), 1)
        schema = store.get(game.appid)
        self.assertEqual((schema.title, schema.info("ACH_WIN").title), ("Fall Guys", "Winner"))


class BuildTests(unittest.TestCase):
    def test_app_does_not_import_what_the_exe_leaves_out(self):
        import ast
        import importlib.util as iu

        spec = iu.spec_from_file_location("build_exe", Path(__file__).resolve().parent.parent / "tools" / "build_exe.py")
        build = iu.module_from_spec(spec)
        spec.loader.exec_module(build)
        excluded = set(build.EXCLUDE)
        for source in (Path(__file__).resolve().parent.parent / "baw").glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                for name in names:
                    parts = name.split(".")
                    hits = {".".join(parts[:i]) for i in range(1, len(parts) + 1)} & excluded
                    self.assertFalse(hits, f"{source.name} imports {name}, which tools/build_exe.py excludes")


class ReviewFixTests(unittest.TestCase):
    def test_gog_game_without_steam_twin_is_retried_daily(self):
        from baw import gog

        folder = Path(tempfile.mkdtemp(dir=TMP)) / "Some GOG Game"
        write(folder / "goggame-4242.info", {"gameId": "4242", "clientId": "99", "name": "Some GOG Game"})
        write(folder / "Galaxy64.dll", "")
        first = gog.build_game(folder, offline=True)
        self.assertEqual(first.steam_appid, 0)
        lookups = []
        steam = GameSchema(5, "Some GOG Game")
        steam.achievements["WIN"] = gog.AchievementInfo("WIN", "Winner")
        with mock.patch.object(gog, "steam_appid_for", return_value=5):
            gog.build_game(folder, lambda appid: lookups.append(appid) or steam)  # cached, too recent
            self.assertEqual(lookups, [])
            with mock.patch.object(gog.time, "time", return_value=first.built + gog.RETRY_STEAM_AFTER + 1):
                again = gog.build_game(folder, lambda appid: lookups.append(appid) or steam)
        self.assertEqual((lookups, again.steam_appid), ([5], 5))

    def test_update_download_folder_is_removed(self):
        from baw import install

        folder = Path(tempfile.mkdtemp(dir=TMP)) / "BetterAchievementWatcher-update"
        exe = write(folder / "BetterAchievementWatcher.exe", "x")
        install.delete_later(exe)
        self.assertFalse(folder.exists())


class StatsIniTests(unittest.TestCase):
    def test_codex_stats_are_not_achievements(self):
        # Dying Light (CODEX): progress counters must never pop up as "ACH_11_PROGRESS" unlocks.
        text = b"[UserStats]\nACH_10_PROGRESS=3\nACH_11_PROGRESS=1\nACH_21_PROGRESS=20195\n"
        self.assertEqual(parse_bytes(text), {})
        self.assertEqual(parse_bytes(b"[Whatever]\nKILLS=1\nDISTANCE=120\n"), {})  # counters by their values
        self.assertEqual(unlocked(parse_bytes(b"[Achievements]\nACH_A=1\nACH_B=0\n")), {"ACH_A"})  # still a flat list
