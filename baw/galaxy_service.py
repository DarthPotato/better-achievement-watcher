"""Local stand-in for GOG Galaxy's communication service (127.0.0.1:9977).

GOG games that ship GOG's SDK connect here when the ``GalaxyCommunication`` service exists
(see gog_setup). We sign them in as a local offline user, hand them their achievement and
stat keys, and save what they unlock. Nothing is sent to GOG.
"""

import ctypes
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import galaxy_proto as pb
from . import gog

PORT = 9977
SORT = 1  # communication service
LOCAL_USER_ID = (2 << 56) | 1  # Galaxy user ids carry their type (2 = user) in the top byte
LOCAL_USER_NAME = "Player"

LIBRARY_INFO_REQUEST, LIBRARY_INFO_RESPONSE = 1, 2
AUTH_INFO_REQUEST, AUTH_INFO_RESPONSE = 3, 4
GET_USER_STATS_REQUEST, GET_USER_STATS_RESPONSE = 15, 16
UPDATE_USER_STAT_REQUEST, UPDATE_USER_STAT_RESPONSE = 17, 18
DELETE_USER_STATS_REQUEST, DELETE_USER_STATS_RESPONSE = 19, 20
GET_USER_ACHIEVEMENTS_REQUEST, GET_USER_ACHIEVEMENTS_RESPONSE = 23, 24
UNLOCK_USER_ACHIEVEMENT_REQUEST, UNLOCK_USER_ACHIEVEMENT_RESPONSE = 25, 26
CLEAR_USER_ACHIEVEMENT_REQUEST, CLEAR_USER_ACHIEVEMENT_RESPONSE = 27, 28
DELETE_USER_ACHIEVEMENTS_REQUEST, DELETE_USER_ACHIEVEMENTS_RESPONSE = 29, 30
GET_USER_TIME_PLAYED_REQUEST, GET_USER_TIME_PLAYED_RESPONSE = 43, 44
OVERLAY_STATE_CHANGE_NOTIFICATION = 58
NOT_IMPLEMENTED = 501
VALUE_TYPE_INT, VALUE_TYPE_FLOAT = 1, 2

GameResolver = Callable[[str, int], Optional[gog.GogGame]]


def exe_of_pid(pid: int) -> Optional[Path]:
    if sys.platform != "win32" or not pid:
        return None
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value)
    finally:
        k32.CloseHandle(handle)
    return None


class Session:
    """One game connection."""

    def __init__(self, service: "GalaxyService", conn: socket.socket):
        self.service = service
        self.conn = conn
        self.game: Optional[gog.GogGame] = None
        self.ids: Dict[int, str] = {}  # achievement id we handed out -> key
        self.stat_ids: Dict[int, str] = {}

    def run(self) -> None:
        buffer = b""
        with self.conn:
            while True:
                try:
                    chunk = self.conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                try:
                    frames, buffer = pb.split_frames(buffer)
                except ValueError:
                    return  # not a Galaxy client
                for header, payload in frames:
                    try:
                        self._handle(header, payload)
                    except OSError:
                        return
                    except Exception as exc:  # keep serving the game whatever one message does
                        self.service.log(f"message failed: {exc!r}")

    def _send(self, type_: int, payload: bytes = b"", rseq=None, code=None) -> None:
        self.conn.sendall(pb.frame(SORT, type_, payload, rseq=rseq, code=code))

    def _handle(self, header: pb.Fields, payload: bytes) -> None:
        sort, type_, oseq = pb.first(header, 1, 0), pb.first(header, 2, 0), pb.first(header, 4)
        fields = pb.decode(payload) if payload else {}
        name = self.game.name if self.game else "?"
        self.service.log(f"{name}: request sort={sort} type={type_}")
        result = self._answer(type_, fields) if sort == SORT else None
        if result is None:
            if oseq is not None:
                self._send(type_ + 1, rseq=oseq, code=NOT_IMPLEMENTED)
            return
        self._send(result[0], result[1], rseq=oseq)

    def _answer(self, type_: int, fields: pb.Fields):
        if type_ == LIBRARY_INFO_REQUEST:
            return LIBRARY_INFO_RESPONSE, pb.string(1, str(self.service.peer_folder(pb.text(fields, 3)))) + pb.uint(2, 3)
        if type_ == AUTH_INFO_REQUEST:
            client_id = pb.text(fields, 1)
            self.game = self.service.resolve(client_id, int(pb.first(fields, 5, 0) or 0))
            if self.game:
                self.service.log(f"{self.game.name} signed in ({client_id}): {len(self.game.aliases)} achievement "
                                 f"keys, {len(self.game.stats)} stat keys")
            else:
                self.service.log(f"unknown game signed in (client {client_id}, pid {pb.first(fields, 5)}): "
                                 "no keys to offer")
            # GOG's own service announces the overlay before answering sign-in.
            self._send(OVERLAY_STATE_CHANGE_NOTIFICATION, pb.uint(1, 3))
            return AUTH_INFO_RESPONSE, (pb.uint(2, 0) + pb.fixed64(3, LOCAL_USER_ID)
                                        + pb.string(4, LOCAL_USER_NAME) + pb.uint(5, 0))
        if type_ == GET_USER_STATS_REQUEST:
            return GET_USER_STATS_RESPONSE, self._stats()
        if type_ == UPDATE_USER_STAT_REQUEST:
            key = self.stat_ids.get(pb.first(fields, 1, 0))
            if self.game and key:
                kind = pb.first(fields, 2, VALUE_TYPE_INT)
                value = pb.first(fields, 3, 0) if kind == VALUE_TYPE_FLOAT else pb.first(fields, 4, 0)
                if kind == VALUE_TYPE_FLOAT:
                    value = struct.unpack("<f", struct.pack("<I", value))[0]  # fixed32 bits
                elif value >= 1 << 31:
                    value -= 1 << 64
                gog.set_stat(self.game.game_id, key, value)
            return UPDATE_USER_STAT_RESPONSE, b""
        if type_ == GET_USER_ACHIEVEMENTS_REQUEST:
            return GET_USER_ACHIEVEMENTS_RESPONSE, self._achievements()
        if type_ == UNLOCK_USER_ACHIEVEMENT_REQUEST:
            key = self.ids.get(pb.first(fields, 1, 0))
            if self.game and key:
                canonical = self.game.aliases.get(key, key)
                if gog.record_unlock(self.game.game_id, canonical, pb.first(fields, 2) or None):
                    self.service.log(f"{self.game.name}: unlocked {canonical}")
            else:
                self.service.log(f"unlock for unknown achievement id {pb.first(fields, 1)}")
            return UNLOCK_USER_ACHIEVEMENT_RESPONSE, b""
        if type_ == CLEAR_USER_ACHIEVEMENT_REQUEST:
            key = self.ids.get(pb.first(fields, 1, 0))
            if self.game and key:
                gog.clear_unlock(self.game.game_id, self.game.aliases.get(key, key))
            return CLEAR_USER_ACHIEVEMENT_RESPONSE, b""
        if type_ in (DELETE_USER_STATS_REQUEST, DELETE_USER_ACHIEVEMENTS_REQUEST):
            if self.game:
                gog.reset(self.game.game_id)
            return type_ + 1, b""
        if type_ == GET_USER_TIME_PLAYED_REQUEST:
            return GET_USER_TIME_PLAYED_RESPONSE, pb.uint(1, 0)
        return None

    def _stats(self) -> bytes:
        if self.game is None:
            return b""
        values = gog.stat_values(self.game.game_id)
        body = b""
        for index, key in enumerate(self.game.stats, 1):
            self.stat_ids[index] = key
            value = values.get(key, 0)
            if isinstance(value, float) and not value.is_integer():
                entry = pb.fixed64(1, index) + pb.string(2, key) + pb.uint(3, VALUE_TYPE_FLOAT) + pb.float32(4, value)
            else:
                entry = (pb.fixed64(1, index) + pb.string(2, key) + pb.uint(3, VALUE_TYPE_INT)
                         + pb.uint(5, int(value)) + pb.uint(8, 0) + pb.uint(10, 0) + pb.uint(12, 2 ** 31 - 1))
            body += pb.string(1, entry)
        return body

    def _achievements(self) -> bytes:
        body = b""
        if self.game is not None:
            done = gog.unlocks(self.game.game_id)
            for index, key in enumerate(sorted(self.game.aliases), 1):
                self.ids[index] = key
                canonical = self.game.aliases[key]
                info = self.game.info.get(canonical, {})
                entry = (pb.fixed64(1, index) + pb.string(2, key)
                         + pb.string(3, info.get("display_name") or key) + pb.string(4, info.get("description") or "")
                         + pb.string(5, info.get("icon_gray") or "") + pb.string(6, info.get("icon") or "")
                         + pb.uint(7, 0 if info.get("hidden") else 1))
                if canonical in done:
                    entry += pb.fixed32(8, done[canonical] or int(time.time()))
                body += pb.string(1, entry)
        return body + pb.string(2, "en-US") + pb.string(3, "all_visible")


class GalaxyService:
    def __init__(self, peer_dir: Path, resolver: GameResolver, log: Callable[[str], None] = print, port: int = PORT):
        self.port = port
        self.peer_dir = peer_dir
        self.resolver = resolver
        self.log = log
        self._server: Optional[socket.socket] = None
        self._games: Dict[str, gog.GogGame] = {}
        self._lock = threading.Lock()

    def peer_folder(self, compiler_version: str) -> Path:
        folder = self.peer_dir / f"msvc-{compiler_version or '18'}"
        return folder if folder.is_dir() else self.peer_dir / "msvc-18"

    def resolve(self, client_id: str, pid: int) -> Optional[gog.GogGame]:
        with self._lock:
            if client_id in self._games:
                return self._games[client_id]
        game = self.resolver(client_id, pid)
        if game is not None:
            with self._lock:
                self._games[client_id] = game
        return game

    def start(self) -> bool:
        """Listen on 127.0.0.1:9977; False when the port is taken (e.g. real GOG Galaxy runs)."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", self.port))
            self.port = server.getsockname()[1]
            server.listen(8)
        except OSError:
            server.close()
            return False
        self._server = server
        threading.Thread(target=self._accept, name="galaxy", daemon=True).start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def _accept(self) -> None:
        while self._server is not None:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=Session(self, conn).run, name="galaxy-client", daemon=True).start()
