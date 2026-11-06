"""Prototype: a local stand-in for GOG Galaxy's communication service (no GOG account).

Usage: python galaxy_standin.py <seconds> <log file> <peer dir> <steam appid for achievement keys>
"""
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

seconds, log_path, peer_dir, source = float(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
# source: a Steam AppID (achievement keys from Steam), or "ach=KEY,KEY;stats=KEY,KEY" (keys from the game's log)
steam_appid = int(source) if source.isdigit() else 0
SPEC = dict(part.split("=", 1) for part in source.split(";") if "=" in part)
STATS = [(i + 1, key) for i, key in enumerate(k for k in SPEC.get("stats", "").split(",") if k)]
STAT_VALUES = {}
log = open(log_path, "w", encoding="utf-8")
USER_ID = (2 << 56) | 1234567  # a made-up local user


def write(msg):
    log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    log.flush()


# ------------------------------------------------------------ protobuf (just what's needed)

def varint(data, pos):
    shift = result = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def decode(data):
    pos, out = 0, {}
    while pos < len(data):
        key, pos = varint(data, pos)
        num, wt = key >> 3, key & 7
        if wt == 0:
            value, pos = varint(data, pos)
        elif wt == 2:
            length, pos = varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wt == 5:
            value, pos = struct.unpack("<I", data[pos:pos + 4])[0], pos + 4
        elif wt == 1:
            value, pos = struct.unpack("<Q", data[pos:pos + 8])[0], pos + 8
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(num, []).append(value)
    return out


def enc_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def f_varint(num, value):
    return enc_varint(num << 3) + enc_varint(value)


def f_bytes(num, value):
    if isinstance(value, str):
        value = value.encode()
    return enc_varint(num << 3 | 2) + enc_varint(len(value)) + value


def f_fixed64(num, value):
    return enc_varint(num << 3 | 1) + struct.pack("<Q", value)


def f_fixed32(num, value):
    return enc_varint(num << 3 | 5) + struct.pack("<I", value)


def f_float(num, value):
    return enc_varint(num << 3 | 5) + struct.pack("<f", value)


# ------------------------------------------------------------ achievements

def load_achievements():
    if not steam_appid:
        keys = [k for k in SPEC.get("ach", "").split(",") if k]
        return [(i + 1, key, key, "") for i, key in enumerate(keys)]
    url = f"https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid={steam_appid}&language=english"
    data = json.loads(urllib.request.urlopen(url, timeout=15).read())
    items = data["response"]["achievements"]
    return [(i + 1, a["internal_name"], a["localized_name"], a["localized_desc"]) for i, a in enumerate(items)]


ACHIEVEMENTS = load_achievements()
BY_ID = {a[0]: a for a in ACHIEVEMENTS}
UNLOCKED = {}
write(f"loaded {len(ACHIEVEMENTS)} achievements, first keys {[a[1] for a in ACHIEVEMENTS[:5]]}")

# ------------------------------------------------------------ handlers (sort 1 = communication service)

def handle(type_, payload):
    """Return (response type, payload) or None for no response."""
    fields = decode(payload) if payload else {}
    if type_ == 1:  # LIBRARY_INFO_REQUEST
        version = fields.get(3, [b"17"])[0].decode()
        location = f"{peer_dir}\\msvc-{version}"
        write(f"  -> library at {location}")
        return 2, f_bytes(1, location) + f_varint(2, 3)  # UPDATE_COMPLETE
    if type_ == 3:  # AUTH_INFO_REQUEST
        write(f"  auth from client_id={fields.get(1)} pid={fields.get(5)}")
        return 4, f_varint(2, 0) + f_fixed64(3, USER_ID) + f_bytes(4, "Player") + f_varint(5, 0)
    if type_ == 15:  # GET_USER_STATS_REQUEST
        body = b""
        for stat_id, key in STATS:
            entry = (f_fixed64(1, stat_id) + f_bytes(2, key) + f_varint(3, 1)  # VALUE_TYPE_INT
                     + f_varint(5, STAT_VALUES.get(stat_id, 0)) + f_varint(8, 0) + f_varint(10, 0)
                     + f_varint(12, 2147483647) + f_varint(15, 0))
            body += f_bytes(1, entry)
        return 16, body
    if type_ == 17:  # UPDATE_USER_STAT_REQUEST
        stat_id, value = fields.get(1, [0])[0], fields.get(4, [0])[0]
        STAT_VALUES[stat_id] = value
        write(f"  stat {dict(STATS).get(stat_id, stat_id)} = {value}")
        return 18, b""
    if type_ == 23:  # GET_USER_ACHIEVEMENTS_REQUEST
        body = b""
        for ach_id, key, name, desc in ACHIEVEMENTS:
            img = "https://images.gog.com/placeholder.png"
            entry = (f_fixed64(1, ach_id) + f_bytes(2, key) + f_bytes(3, name) + f_bytes(4, desc)
                     + f_bytes(5, img) + f_bytes(6, img) + f_varint(7, 1) + f_float(9, 10.0)
                     + f_bytes(10, "Common") + f_bytes(11, "common"))
            if ach_id in UNLOCKED:
                entry += f_fixed32(8, UNLOCKED[ach_id])
            body += f_bytes(1, entry)
        body += f_bytes(2, "en-US") + f_bytes(3, "all_visible")
        return 24, body
    if type_ == 25:  # UNLOCK_USER_ACHIEVEMENT_REQUEST
        ach_id, when = fields.get(1, [0])[0], fields.get(2, [0])[0]
        UNLOCKED[ach_id] = when or int(time.time())
        write(f"  *** UNLOCK id={ach_id} {BY_ID.get(ach_id)} time={when}")
        return 26, b""
    if type_ == 27:  # CLEAR_USER_ACHIEVEMENT_REQUEST
        UNLOCKED.pop(fields.get(1, [0])[0], None)
        return 28, b""
    if type_ == 43:  # GET_USER_TIME_PLAYED_REQUEST
        return 44, f_varint(1, 0)
    if type_ in (49, 59):  # START_GAME_SESSION / CONFIGURE_ENVIRONMENT
        return type_ + 1, b""
    return None


def send(conn, sort, type_, payload, rseq=None, code=None):
    header = f_varint(1, sort) + f_varint(2, type_) + f_varint(3, len(payload))
    if rseq is not None:
        header += f_varint(100, rseq)
    if code is not None:
        header += f_varint(101, code)
    conn.sendall(struct.pack(">H", len(header)) + header + payload)


def client(conn, addr):
    write(f"CONNECT {addr}")
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 2:
                hlen = struct.unpack(">H", buf[:2])[0]
                if len(buf) < 2 + hlen:
                    break
                header = decode(buf[2:2 + hlen])
                size = header.get(3, [0])[0]
                if len(buf) < 2 + hlen + size:
                    break
                payload = buf[2 + hlen:2 + hlen + size]
                buf = buf[2 + hlen + size:]
                sort, type_ = header.get(1, [0])[0], header.get(2, [0])[0]
                oseq = header.get(4, [None])[0]
                write(f"{addr[1]} recv sort={sort} type={type_} oseq={oseq} payload={decode(payload) if payload else {}}"[:600])
                if sort == 1 and type_ == 3:
                    # Comet announces the overlay before answering sign-in (OVERLAY_STATE_CHANGE, INITIALIZED).
                    send(conn, 1, 58, f_varint(1, 3))
                    write("  -> overlay state INITIALIZED")
                result = handle(type_, payload) if sort == 1 else None
                if result is None:
                    if oseq is not None:
                        send(conn, sort, type_ + 1, b"", rseq=oseq, code=501)
                        write("  -> 501 not implemented")
                    continue
                send(conn, sort, result[0], result[1], rseq=oseq)
    except OSError as exc:
        write(f"closed {addr}: {exc}")
    write(f"DISCONNECT {addr}")


server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 9977))
server.listen(8)
server.settimeout(1)
write("listening")
end = time.time() + seconds
while time.time() < end:
    try:
        conn, addr = server.accept()
    except socket.timeout:
        continue
    threading.Thread(target=client, args=(conn, addr), daemon=True).start()
write(f"done, unlocked: {UNLOCKED}")
