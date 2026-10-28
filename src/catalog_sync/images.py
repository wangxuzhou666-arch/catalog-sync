"""Fit downloaded pictures into a byte budget, and refuse inputs that could exhaust memory.

Decoding is where an untrusted image does damage: a few hundred KB of PNG can declare tens of
thousands of pixels per side and unpack to gigabytes. Three guards, in this order:

1. input size, before Pillow is called at all;
2. input pixel count, read from the file header (Pillow opens lazily: no pixel buffer exists yet);
3. only then decode. Decoding and the alpha flatten to RGB happen at the picture's full size (the
   pixel guard above is what bounds that memory); each size tier is then a downscaled copy of the
   flattened picture, and the quality search runs on those copies.

The output policy trades size tiers against JPEG quality. Fabric swatches are woven textures, and
that texture is the only information in them: below a quality floor, JPEG blocking eats it. So the
fitter walks quality down inside a size tier and, once the floor is reached, steps down to the
next *size* tier instead of lowering quality further. Sizes never go up.
"""

from __future__ import annotations

import io
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from catalog_sync.errors import ConfigError, ItemFailed

MAX_INPUT_BYTES = 30 * 1024 * 1024
# 32 megapixels decode to roughly 330 MB at peak in Pillow; a bigger header is refused unseen.
MAX_INPUT_PIXELS = 32_000_000
# Pillow's other plugins include formats that shell out or parse exotic containers. Pictures
# served by the supported sources are one of these three.
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")

_OPTION_KEYS = frozenset({"max_edge", "edge_fallbacks", "qualities", "max_bytes"})
_MIN_EDGE = 16
_MIN_BYTES = 1024


@dataclass(frozen=True)
class ImagePolicy:
    """Longest-edge tiers (largest first), JPEG qualities (best first) and the byte budget."""

    max_edge: int = 1600
    edge_fallbacks: tuple[int, ...] = (1200, 900)
    qualities: tuple[int, ...] = (88, 82, 76, 70)
    max_bytes: int = 614_400

    def __post_init__(self) -> None:
        edges = self.edges
        if edges[-1] < _MIN_EDGE or any(a <= b for a, b in itertools.pairwise(edges)):
            raise ConfigError(
                f"image edges must decrease strictly and stay >= {_MIN_EDGE}: {list(edges)}"
            )
        qualities = self.qualities
        if not qualities or any(not 1 <= q <= 100 for q in qualities):
            raise ConfigError(f"image qualities must be a non-empty list within 1-100: {qualities}")
        if any(a <= b for a, b in itertools.pairwise(qualities)):
            raise ConfigError(f"image qualities must decrease strictly: {list(qualities)}")
        if self.max_bytes < _MIN_BYTES:
            raise ConfigError(f"image max_bytes must be at least {_MIN_BYTES}: {self.max_bytes}")

    @property
    def edges(self) -> tuple[int, ...]:
        return (self.max_edge, *self.edge_fallbacks)

    @classmethod
    def from_options(cls, options: Mapping[str, object] | None) -> ImagePolicy:
        """Build from a config table; missing keys keep their defaults."""
        if options is None:
            return cls()
        unknown = sorted(set(options) - _OPTION_KEYS)
        if unknown:
            raise ConfigError(f"images: unknown option(s) {unknown}")
        defaults = cls()
        return cls(
            max_edge=_int(options, "max_edge", defaults.max_edge),
            edge_fallbacks=_int_list(options, "edge_fallbacks", defaults.edge_fallbacks),
            qualities=_int_list(options, "qualities", defaults.qualities),
            max_bytes=_int(options, "max_bytes", defaults.max_bytes),
        )

    def tightened(
        self, *, max_edge: int | None = None, max_bytes: int | None = None
    ) -> ImagePolicy:
        """A policy that is at most as generous as this one: a caller may lower the limits, never
        raise them (asking for more than the policy allows is ignored, not an error).

        Lowering the edge caps every tier at the new value, so ``1600,1200,900`` tightened to
        1000 becomes ``1000,900``.
        """
        tiers = list(self.edges)
        if max_edge is not None:
            tiers = [min(tier, max_edge) for tier in tiers]
        distinct = list(dict.fromkeys(tiers))
        return replace(
            self,
            max_edge=distinct[0],
            edge_fallbacks=tuple(distinct[1:]),
            max_bytes=self.max_bytes if max_bytes is None else min(self.max_bytes, max_bytes),
        )


@dataclass(frozen=True)
class FittedImage:
    jpeg: bytes
    width: int
    height: int
    quality: int
    edge_tier: int


def fit_jpeg(raw: bytes, policy: ImagePolicy, *, source_id: str) -> FittedImage:
    """Re-encode `raw` as a JPEG within ``policy.max_bytes``, or raise ItemFailed.

    Re-encoding also drops EXIF, GPS and any payload appended to the original file.
    """
    image = _open_guarded(raw, source_id)
    rgb = _flattened_rgb(image, source_id)
    previous_size: tuple[int, int] | None = None
    last_try = "nothing"
    for edge in policy.edges:
        work = rgb.copy()
        work.thumbnail((edge, edge), Image.Resampling.LANCZOS)  # shrinks only, never enlarges
        if work.size == previous_size:
            continue  # the picture is already smaller than this tier: same pixels, same bytes
        previous_size = work.size
        for quality in policy.qualities:
            encoded = _encode(work, quality)
            last_try = f"{len(encoded)} bytes at {edge}px, quality {quality}"
            if len(encoded) <= policy.max_bytes:
                return FittedImage(encoded, work.width, work.height, quality, edge)
    raise ItemFailed(
        source_id,
        f"image does not fit {policy.max_bytes} bytes; the smallest allowed result is {last_try}",
    )


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """Width and height of a stored picture, read from its header: no pixel is decoded.

    None when the file is not one of the supported formats or its header is damaged. Opening is
    lazy, so a picture is never loaded into memory just to be measured; the pixel limit that
    `fit_jpeg` applies belongs to decoding and is not repeated here, because the size of a file
    is a fact about it whether or not it could be processed.
    """
    try:
        with Image.open(path, formats=ALLOWED_FORMATS) as image:
            return image.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        # Pillow reports a damaged PNG as SyntaxError, a cut-off file as OSError and a header that
        # declares an absurd size as DecompressionBombError; none of them is worth stopping for.
        return None


def _open_guarded(raw: bytes, source_id: str) -> Image.Image:
    if len(raw) > MAX_INPUT_BYTES:
        raise ItemFailed(source_id, f"image is {len(raw)} bytes, limit is {MAX_INPUT_BYTES}")
    try:
        image = Image.open(io.BytesIO(raw), formats=ALLOWED_FORMATS)
    except UnidentifiedImageError as exc:
        raise ItemFailed(
            source_id, f"not a supported picture ({'/'.join(ALLOWED_FORMATS)})"
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ItemFailed(source_id, "image header declares an absurd number of pixels") from exc
    pixels = image.width * image.height
    if pixels > MAX_INPUT_PIXELS:
        raise ItemFailed(
            source_id,
            f"image is {pixels / 1e6:.0f} megapixels, limit is {MAX_INPUT_PIXELS // 1_000_000}",
        )
    return image


def _flattened_rgb(image: Image.Image, source_id: str) -> Image.Image:
    try:
        image.load()
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        # Pillow reports a damaged PNG as SyntaxError and a cut-off file as OSError.
        raise ItemFailed(source_id, f"image cannot be decoded ({type(exc).__name__})") from exc
    upright = ImageOps.exif_transpose(image)
    if upright.mode in ("RGBA", "LA") or (upright.mode == "P" and "transparency" in upright.info):
        rgba = upright.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return upright.convert("RGB")


def _encode(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _int(options: Mapping[str, object], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"images.{key} must be an integer, got {value!r}")
    return value


def _int_list(options: Mapping[str, object], key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = options.get(key, list(default))
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ConfigError(f"images.{key} must be a list of integers, got {value!r}")
    return tuple(value)
