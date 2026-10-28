"""Pictures for the Mock SaaS: deterministic fabric-like swatches drawn from a key.

A thumbnail is a small PNG and a swatch a larger JPEG, so a pull meets both formats. The colour
and the weave come from a hash of the key, so a material always has the same pictures.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw

PNG = "image/png"
JPEG = "image/jpeg"
_SIZES = {"thumb": 96, "swatch": 192}


def picture(role: str, key: str) -> tuple[bytes, str]:
    """The picture of `key` for `role` ("thumb" or "swatch"), and its content type."""
    size = _SIZES[role]
    digest = hashlib.sha256(f"{role}:{key}".encode()).digest()
    base = (60 + digest[0] % 150, 60 + digest[1] % 150, 60 + digest[2] % 150)
    light = tuple(min(255, channel + 40) for channel in base)
    image = Image.new("RGB", (size, size), base)
    draw = ImageDraw.Draw(image)
    step = 4 + digest[3] % 5
    for offset in range(-size, size, step):  # diagonal ribs, like a twill
        draw.line([(offset, 0), (offset + size, size)], fill=light, width=1 + digest[4] % 2)
    buffer = io.BytesIO()
    if role == "thumb":
        image.save(buffer, format="PNG")
        return buffer.getvalue(), PNG
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue(), JPEG
