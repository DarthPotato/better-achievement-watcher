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
        config = Config(scan_default_locations=False)
        events = []
        w = Watcher(config, SchemaStore(config, offline=True), on_unlock=lambda a, s: events.append(a))
        states = [{}, {"X": watcher_module.AchievementState("X", True)}]
        with mock.patch.object(watcher_module, "parse_steam_stats", side_effect=lambda p: states[0]):
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
            self.assertEqual(notify.sound_path(Config()).name, "deck_ui_achievement_toast.wav")
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
            with mock.patch.object(startup, "_registered_command", return_value='"Z:\gone\BAW.exe" --background'):
                startup.repair()
            with mock.patch.object(startup, "_registered_command", return_value=f'"{__file__}" --background'):
                startup.repair()
            with mock.patch.object(startup, "_registered_command", return_value=""):
                startup.repair()
        self.assertEqual(calls, [True])
