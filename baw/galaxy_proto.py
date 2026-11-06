"""Just enough protobuf to talk to GOG's SDK (message layouts: github.com/Yepoleb/gog_protocols).

A Galaxy frame is a 2-byte big-endian header length, a ``gog.protocols.pb.Header``
(sort=1, type=2, size=3, oseq=4; responses add rseq=100) and then ``size`` bytes of payload.
"""

import struct
from typing import Dict, List, Optional, Tuple

Fields = Dict[int, List[object]]


def _varint(data: bytes, pos: int) -> Tuple[int, int]:
    shift = result = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode(data: bytes) -> Fields:
    """Decode one message into {field number: [values]} (bytes for length-delimited fields)."""
    pos, out = 0, {}
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            length, pos = _varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = struct.unpack_from("<I", data, pos)[0], pos + 4
        elif wire == 1:
            value, pos = struct.unpack_from("<Q", data, pos)[0], pos + 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(number, []).append(value)
    return out


def first(fields: Fields, number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


def text(fields: Fields, number: int) -> str:
    value = first(fields, number, b"")
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _enc_varint(value: int) -> bytes:
    value &= (1 << 64) - 1  # negative int32 values are sent as 64-bit two's complement
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def uint(number: int, value: int) -> bytes:
    return _enc_varint(number << 3) + _enc_varint(int(value))


def string(number: int, value) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _enc_varint(number << 3 | 2) + _enc_varint(len(value)) + value


def fixed64(number: int, value: int) -> bytes:
    return _enc_varint(number << 3 | 1) + struct.pack("<Q", int(value) & ((1 << 64) - 1))


def fixed32(number: int, value: int) -> bytes:
    return _enc_varint(number << 3 | 5) + struct.pack("<I", int(value) & 0xFFFFFFFF)


def float32(number: int, value: float) -> bytes:
    return _enc_varint(number << 3 | 5) + struct.pack("<f", float(value))


def frame(sort: int, type_: int, payload: bytes, rseq: Optional[int] = None, code: Optional[int] = None) -> bytes:
    header = uint(1, sort) + uint(2, type_) + uint(3, len(payload))
    if rseq is not None:
        header += uint(100, rseq)
    if code is not None:
        header += uint(101, code)
    return struct.pack(">H", len(header)) + header + payload


def split_frames(buffer: bytes) -> Tuple[List[Tuple[Fields, bytes]], bytes]:
    """Complete frames in buffer as (header fields, payload), plus the unconsumed rest."""
    frames = []
    while len(buffer) >= 2:
        header_len = struct.unpack_from(">H", buffer)[0]
        if len(buffer) < 2 + header_len:
            break
        header = decode(buffer[2:2 + header_len])
        size = first(header, 3, 0)
        end = 2 + header_len + size
        if len(buffer) < end:
            break
        frames.append((header, buffer[2 + header_len:end]))
        buffer = buffer[end:]
    return frames, buffer
