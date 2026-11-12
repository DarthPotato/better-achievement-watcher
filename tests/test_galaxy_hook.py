import importlib.util
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from baw import galaxy_hook  # noqa: E402
from baw import galaxy_proto as pb  # noqa: E402
from baw.galaxy_service import GalaxyService, Session  # noqa: E402

TEXT_RVA, RDATA_RVA, PDATA_RVA = 0x1000, 0x2000, 0x3000
STRING_RVA = RDATA_RVA + 0x10


def _lea_rdx(at_rva: int) -> bytes:
    """lea rdx, [rip+disp32] pointing at the guard text."""
    return b"\x48\x8d\x15" + struct.pack("<i", STRING_RVA - (at_rva + 7))


def fake_sdk(text: bytes = b"%s: not logged on\x00", big_function: bool = True) -> bytes:
    """A tiny 64-bit PE: .text has a small helper and a big function that both load the text, plus an exception table."""
    code = bytearray(b"\xcc" * 0x200)
    code[0x00:0x07] = _lea_rdx(TEXT_RVA)
    code[0x07] = 0xC3
    code[0x100:0x107] = _lea_rdx(TEXT_RVA + 0x100)
    code[0x107] = 0xC3
    big_end = TEXT_RVA + 0x100 + (0x400 if big_function else 0x20)
    pdata = (struct.pack("<III", TEXT_RVA, TEXT_RVA + 0x20, 0)
             + struct.pack("<III", TEXT_RVA + 0x100, big_end, 0))
    rdata = bytearray(0x200)
    rdata[0x10:0x10 + len(text)] = text
    pdata_block = bytearray(0x200)
    pdata_block[:len(pdata)] = pdata

    headers = bytearray(0x400)
    headers[0x3C:0x40] = struct.pack("<I", 0x80)
    headers[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", headers, 0x84, 0x8664, 3, 0, 0, 0, 240, 0x2022)
    opt = 0x98
    struct.pack_into("<H", headers, opt, 0x20B)
    struct.pack_into("<I", headers, opt + 56, 0x4000)  # SizeOfImage
    struct.pack_into("<II", headers, opt + 112 + 3 * 8, PDATA_RVA, len(pdata))  # exception directory
    sections = [(TEXT_RVA, 0x400, 0x60000020), (RDATA_RVA, 0x600, 0x40000040), (PDATA_RVA, 0x800, 0x40000040)]
    for i, (rva, ptr, flags) in enumerate(sections):
        at = opt + 240 + i * 40
        struct.pack_into("<IIII", headers, at + 8, 0x200, rva, 0x200, ptr)
        struct.pack_into("<I", headers, at + 36, flags)
    return bytes(headers) + bytes(code) + bytes(rdata) + bytes(pdata_block)


class GalaxyHookTests(unittest.TestCase):
    def test_finds_the_small_shared_guard_and_leaves_big_functions_alone(self):
        plan = galaxy_hook.analyze(fake_sdk())
        self.assertEqual(plan.guards, [TEXT_RVA])
        self.assertEqual(plan.image_size, 0x4000)
        self.assertTrue(any("inlined" in note for note in plan.notes))

    def test_a_small_second_function_is_patched_too(self):
        self.assertEqual(galaxy_hook.analyze(fake_sdk(big_function=False)).guards, [TEXT_RVA, TEXT_RVA + 0x100])

    def test_nothing_to_patch_without_the_text_or_for_other_files(self):
        self.assertEqual(galaxy_hook.analyze(fake_sdk(text=b"something else\x00")).guards, [])
        self.assertEqual(galaxy_hook.analyze(b"MZ" + b"\x00" * 100).guards, [])
        self.assertEqual(galaxy_hook.analyze(b"").guards, [])

    def test_32_bit_dlls_are_left_alone(self):
        data = bytearray(fake_sdk())
        struct.pack_into("<H", data, 0x84, 0x14C)
        plan = galaxy_hook.analyze(bytes(data))
        self.assertEqual(plan.guards, [])
        self.assertTrue(plan.notes)

    @unittest.skipUnless(os.environ.get("BAW_TEST_GALAXY64"), "set BAW_TEST_GALAXY64 to a real Galaxy64.dll")
    def test_real_sdk(self):
        plan = galaxy_hook.analyze(Path(os.environ["BAW_TEST_GALAXY64"]).read_bytes())
        self.assertEqual(len(plan.guards), 1)


class LeaderboardAnswerTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(GalaxyService(Path("."), lambda client, pid: None), None)

    def test_leaderboards_by_key_get_empty_boards_with_stable_ids(self):
        kind, body = self.session._answer(40, {1: [b"TestHighScore", b"Other"]})
        self.assertEqual(kind, 32)
        boards = [pb.decode(entry) for entry in pb.decode(body)[1]]
        self.assertEqual([pb.text(b, 2) for b in boards], ["TestHighScore", "Other"])
        self.assertEqual(len({pb.first(b, 1) for b in boards}), 2)
        again = [pb.decode(entry) for entry in pb.decode(self.session._answer(40, {1: [b"TestHighScore"]})[1])[1]]
        self.assertEqual(pb.first(again[0], 1), pb.first(boards[0], 1))

    def test_entries_scores_and_creation(self):
        for type_ in (33, 34, 35):
            self.assertEqual(self.session._answer(type_, {1: [1]})[0], 36)
        self.assertEqual(self.session._answer(37, {2: [5]})[0], 38)
        self.assertEqual(self.session._answer(41, {1: [b"New"]})[0], 42)


class _Conn:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


class HookFallbackTests(unittest.TestCase):
    def _sign_in(self, ready: bool):
        from baw import gog

        game = gog.GogGame(game_id=7, client_id="client", name="Needs Fix", folder=".")
        service = GalaxyService(Path("."), lambda client, pid: game, log=lambda m: None,
                                needs_hook={7}, hook_ready=lambda pid: ready)
        session = Session(service, _Conn())
        return session, session._answer(3, {1: [b"client"], 5: [1234]})

    def test_sign_in_is_refused_while_the_fix_is_not_attached(self):
        session, result = self._sign_in(ready=False)
        self.assertEqual(result, 401)
        self.assertIsNone(session.game)

    def test_sign_in_works_once_the_fix_is_attached(self):
        session, result = self._sign_in(ready=True)
        self.assertEqual(result[0], 4)
        self.assertEqual(session.game.name, "Needs Fix")


@unittest.skipUnless(importlib.util.find_spec("tkinter"), "needs Tk")
class TrackingIndicatorTests(unittest.TestCase):
    def test_state_only_for_gog_games(self):
        from types import SimpleNamespace

        from baw import gog
        from baw.app import App

        app = SimpleNamespace(config=SimpleNamespace(gog_untracked=[2], gog_fixed=[3]))
        game = lambda appid, emulator: SimpleNamespace(appid=appid, sources=[SimpleNamespace(emulator=emulator)])
        self.assertEqual(App._gog_state(app, game(1, gog.SOURCE)), "on")
        self.assertEqual(App._gog_state(app, game(2, gog.SOURCE)), "off")
        self.assertEqual(App._gog_state(app, game(3, gog.SOURCE)), "fix")
        self.assertEqual(App._gog_state(app, game(2, "Goldberg")), "")

    def test_badges_are_drawn_for_every_state(self):
        import tkinter as tk

        from baw.app import BADGE_SIZE, App

        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display")
        try:
            app = type("A", (), {})()
            app.root, app._badges = root, {}
            for state in ("on", "off", "fix", ""):
                image = App._badge(app, state)
                self.assertEqual((image.width(), image.height()), BADGE_SIZE)
            self.assertIsNot(App._badge(app, "on"), App._badge(app, "off"))
            self.assertIs(App._badge(app, "on"), App._badge(app, "on"))  # cached, and kept alive for Tk
        finally:
            root.destroy()


class StatusNoteTests(unittest.TestCase):
    def test_note_depends_on_tracking_and_the_fix(self):
        from baw import gog

        self.assertIn("Not tracked", gog.status_note(5, False, False))
        self.assertEqual(gog.status_note(5, True, False), "")  # an ordinary tracked game needs no explanation
        self.assertIn("experimental", gog.status_note(5, True, True))
        waiting = gog.status_note(1162687982, True, False)  # known to need the fix, fix still off
        self.assertIn("leaderboards", waiting)
        self.assertIn("right-click", waiting)


class FixConfigTests(unittest.TestCase):
    def test_defaults_and_migration_of_old_configs(self):
        import json
        import tempfile

        from baw.config import Config

        self.assertEqual((Config().gog_untracked, Config().gog_fixed), ([], []))
        folder = tempfile.mkdtemp()
        old = {"gog_enabled": True, "gog_untracked": [1162687982, 55]}  # an older config, before gog_fixed existed
        (Path(folder) / "config.json").write_text(json.dumps(old), encoding="utf-8")
        with mock.patch.object(Config, "path", classmethod(lambda cls: Path(folder) / "config.json")):
            cfg = Config.load()
            self.assertEqual(cfg.gog_untracked, [55])  # IRON NEST's old default entry goes, user choices stay
            cfg.gog_fixed = [7]
            cfg.gog_untracked = [1162687982]
            cfg.save()
            again = Config.load()
            self.assertEqual((again.gog_untracked, again.gog_fixed), ([1162687982], [7]))  # explicit choices kept


if __name__ == "__main__":
    unittest.main()
