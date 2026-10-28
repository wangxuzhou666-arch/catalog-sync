"""Tiny pictures for test data, made with the standard library only.

Level-0 deflate and no imaging library, so the bytes are identical on every machine and a
committed replay snapshot can be regenerated and compared byte for byte.
"""

from __future__ import annotations

import struct
import zlib


def png_bytes(index: int, role: str) -> bytes:
    """A valid 8x8 PNG whose colour depends on the item and role; stored, not compressed.

    Stdlib only and level-0 deflate, so the bytes are identical on every machine and the
    committed example snapshot can be regenerated and compared byte for byte.
    """
    shade = (index * 37 + (60 if role == "swatch" else 0)) % 256
    pixel = bytes((shade, (shade * 3) % 256, 255 - shade))
    rows = b"".join(b"\x00" + pixel * 8 for _ in range(8))  # filter byte 0 + 8 RGB pixels

    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 0))
        + chunk(b"IEND", b"")
    )
