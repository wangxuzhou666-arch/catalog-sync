from __future__ import annotations

import io
import random
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from catalog_sync import images
from catalog_sync.errors import ConfigError, ItemFailed
from catalog_sync.images import MAX_INPUT_BYTES, ImagePolicy, fit_jpeg, probe_dimensions

DEFAULT = ImagePolicy()


def encode(image: Image.Image, fmt: str = "PNG", **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def noise_png(edge: int, seed: int = 1) -> bytes:
    """Incompressible pixels: the worst case for any size budget."""
    rng = random.Random(seed)  # noqa: S311 — test data, not security
    return encode(Image.frombytes("RGB", (edge, edge), rng.randbytes(edge * edge * 3)))


def jpeg_size(raw: bytes, edge: int, quality: int) -> int:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return len(images._encode(image, quality))


def png_declaring(width: int, height: int) -> bytes:
    """A few dozen bytes of PNG whose header claims width x height pixels."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


# --- the policy ---------------------------------------------------------------------------------


def test_a_small_picture_is_re_encoded_but_never_enlarged() -> None:
    fitted = fit_jpeg(encode(Image.new("RGB", (400, 300), (10, 120, 200))), DEFAULT, source_id="a")
    assert (fitted.width, fitted.height) == (400, 300)
    assert (fitted.edge_tier, fitted.quality) == (1600, 88)
    assert Image.open(io.BytesIO(fitted.jpeg)).format == "JPEG"


def test_a_large_picture_is_scaled_down_to_the_first_tier() -> None:
    fitted = fit_jpeg(encode(Image.new("RGB", (2400, 1800), (200, 30, 30))), DEFAULT, source_id="a")
    assert (fitted.width, fitted.height) == (1600, 1200)


def test_noise_that_cannot_fit_at_1600_lands_in_the_next_size_tier() -> None:
    raw = noise_png(1700)
    # Budget: more than the 1200px picture needs at the best quality, less than 1600px needs at
    # the worst. Measured with the same encoder, so the test does not depend on a Pillow version.
    budget = (jpeg_size(raw, 1200, 88) + jpeg_size(raw, 1600, 70)) // 2
    assert jpeg_size(raw, 1200, 88) < budget < jpeg_size(raw, 1600, 70)

    fitted = fit_jpeg(raw, ImagePolicy(max_bytes=budget), source_id="a")

    assert fitted.edge_tier == 1200
    assert (fitted.width, fitted.height) == (1200, 1200)
    assert fitted.quality == 88, "the new tier starts again at the best quality"
    assert len(fitted.jpeg) <= budget


def test_quality_is_walked_down_inside_a_tier_before_the_tier_is_given_up() -> None:
    raw = noise_png(1700)
    budget = (jpeg_size(raw, 1600, 76) + jpeg_size(raw, 1600, 70)) // 2
    fitted = fit_jpeg(raw, ImagePolicy(max_bytes=budget), source_id="a")
    assert (fitted.edge_tier, fitted.quality) == (1600, 70)


def test_quality_never_drops_below_the_floor_it_steps_to_a_smaller_picture_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[int] = []
    real_encode = images._encode

    def spy(image: Image.Image, quality: int) -> bytes:
        tried.append(quality)
        return real_encode(image, quality)

    monkeypatch.setattr(images, "_encode", spy)
    raw = noise_png(1700)
    budget = jpeg_size(raw, 900, 70) + 1  # fits only at the very last tier and quality
    tried.clear()

    fitted = fit_jpeg(raw, ImagePolicy(max_bytes=budget), source_id="a")

    assert (fitted.edge_tier, fitted.quality) == (900, 70)
    assert min(tried) == 70
    assert tried == [88, 82, 76, 70] * 3, "three tiers, each tried best-to-floor"


def test_a_picture_that_does_not_fit_even_at_the_smallest_tier_fails_the_item() -> None:
    raw = noise_png(1700)
    budget = jpeg_size(raw, 900, 70) - 1
    with pytest.raises(ItemFailed, match=r"item-9: image does not fit .* at 900px, quality 70"):
        fit_jpeg(raw, ImagePolicy(max_bytes=budget), source_id="item-9")


def test_tiers_the_picture_is_already_below_are_not_retried() -> None:
    # 800px noise: every tier resolves to the same pixels, so there is exactly one pass.
    raw = noise_png(800)
    with pytest.raises(ItemFailed):
        fit_jpeg(raw, ImagePolicy(max_bytes=2000), source_id="a")


def test_alpha_is_flattened_onto_white_not_black() -> None:
    clear = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    fitted = fit_jpeg(encode(clear), DEFAULT, source_id="a")
    pixel = Image.open(io.BytesIO(fitted.jpeg)).getpixel((10, 10))
    assert isinstance(pixel, tuple)
    assert min(pixel) > 250


def test_exif_orientation_is_applied_and_the_exif_is_dropped() -> None:
    portrait_on_its_side = Image.new("RGB", (300, 100), (50, 50, 50))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    raw = encode(portrait_on_its_side, "JPEG", exif=exif)

    fitted = fit_jpeg(raw, DEFAULT, source_id="a")

    assert (fitted.width, fitted.height) == (100, 300)
    assert not Image.open(io.BytesIO(fitted.jpeg)).getexif()


# --- decompression-bomb and format guards ---------------------------------------------------------


def test_a_png_header_claiming_too_many_pixels_is_refused_before_any_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def decoding_is_forbidden(self: Image.Image) -> None:
        raise AssertionError("pixels were decoded for an image that is over the limit")

    monkeypatch.setattr(Image.Image, "load", decoding_is_forbidden)
    raw = png_declaring(8000, 5000)  # 40 megapixels, ~70 bytes
    assert len(raw) < 200

    with pytest.raises(ItemFailed, match=r"40 megapixels, limit is 32"):
        fit_jpeg(raw, DEFAULT, source_id="a")


def test_a_header_beyond_pillows_own_bomb_limit_is_also_an_item_failure() -> None:
    with pytest.raises(ItemFailed, match="absurd number of pixels"):
        fit_jpeg(png_declaring(60000, 60000), DEFAULT, source_id="a")


def test_a_picture_just_inside_the_pixel_limit_is_accepted() -> None:
    # 4000 x 8000 = 32 MP exactly, as an all-black PNG a few KB large.
    fitted = fit_jpeg(encode(Image.new("L", (4000, 8000))), DEFAULT, source_id="a")
    assert fitted.edge_tier == 1600


def test_an_oversized_input_is_refused_before_pillow_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pillow_is_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Image.open was called for an oversized input")

    monkeypatch.setattr(Image, "open", pillow_is_forbidden)
    with pytest.raises(ItemFailed, match="limit is 31457280"):
        fit_jpeg(b"\0" * (MAX_INPUT_BYTES + 1), DEFAULT, source_id="a")


def test_bytes_that_are_not_a_picture_fail_the_item() -> None:
    with pytest.raises(ItemFailed, match="not a supported picture"):
        fit_jpeg(b"<html>an error page</html>", DEFAULT, source_id="a")


def test_a_format_outside_the_allow_list_is_refused() -> None:
    with pytest.raises(ItemFailed, match="not a supported picture"):
        fit_jpeg(encode(Image.new("P", (8, 8)), "GIF"), DEFAULT, source_id="a")


def test_a_cut_off_file_fails_the_item_instead_of_crashing_the_run() -> None:
    whole = encode(Image.frombytes("RGB", (64, 64), random.Random(3).randbytes(64 * 64 * 3)))  # noqa: S311
    with pytest.raises(ItemFailed, match="cannot be decoded"):
        fit_jpeg(whole[: len(whole) // 2], DEFAULT, source_id="a")


# --- configuration and "max can only tighten" ----------------------------------------------------


def test_defaults_match_the_documented_tiers() -> None:
    assert DEFAULT.edges == (1600, 1200, 900)
    assert DEFAULT.qualities == (88, 82, 76, 70)
    assert DEFAULT.max_bytes == 600 * 1024


def test_options_override_only_what_they_name() -> None:
    policy = ImagePolicy.from_options({"max_bytes": 100_000, "qualities": [80, 60]})
    assert policy.max_bytes == 100_000
    assert policy.qualities == (80, 60)
    assert policy.edges == DEFAULT.edges
    assert ImagePolicy.from_options(None) == DEFAULT


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_edgee": 1}, "unknown option"),
        ({"max_edge": "big"}, "must be an integer"),
        ({"max_edge": True}, "must be an integer"),
        ({"qualities": "88"}, "list of integers"),
        ({"qualities": [70, 88]}, "decrease strictly"),
        ({"qualities": [101]}, "within 1-100"),
        ({"qualities": []}, "non-empty"),
        ({"edge_fallbacks": [1700]}, "decrease strictly"),
        ({"edge_fallbacks": [900, 1000]}, "decrease strictly"),
        ({"edge_fallbacks": [4]}, "stay >= 16"),
        ({"max_bytes": 10}, "at least 1024"),
    ],
)
def test_bad_options_are_config_errors(options: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ImagePolicy.from_options(options)


def test_tightening_lowers_the_limits() -> None:
    tight = DEFAULT.tightened(max_edge=1000, max_bytes=100_000)
    assert tight.edges == (1000, 900)
    assert tight.max_bytes == 100_000
    assert DEFAULT.tightened(max_edge=1200).edges == (1200, 900)


def test_tightening_can_never_loosen() -> None:
    assert DEFAULT.tightened(max_edge=4000, max_bytes=10**9) == DEFAULT
    assert DEFAULT.tightened() == DEFAULT
    once = DEFAULT.tightened(max_edge=1000)
    assert once.tightened(max_edge=1600).edges == once.edges


def test_a_tightened_policy_is_actually_used() -> None:
    raw = encode(Image.new("RGB", (2400, 1800), (1, 2, 3)))
    fitted = fit_jpeg(raw, DEFAULT.tightened(max_edge=1000), source_id="a")
    assert fitted.width == 1000


# --- measuring a stored picture -------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_the_size_of_each_supported_format_is_read_from_the_file(tmp_path: Path, fmt: str) -> None:
    path = tmp_path / "picture.bin"
    path.write_bytes(encode(Image.new("RGB", (37, 21), (1, 2, 3)), fmt))
    assert probe_dimensions(path) == (37, 21), "width first, then height"


def test_a_picture_is_measured_without_decoding_its_pixels(tmp_path: Path) -> None:
    path = tmp_path / "wide.bin"
    path.write_bytes(png_declaring(9_000, 8_000))  # 72 megapixels by its header, 70 bytes on disk
    assert probe_dimensions(path) == (9_000, 8_000), "nothing near that size is ever allocated"


def test_a_header_declaring_an_absurd_size_has_no_size(tmp_path: Path) -> None:
    path = tmp_path / "bomb.bin"
    path.write_bytes(png_declaring(30_000, 20_000))  # beyond twice Pillow's own bomb limit
    assert probe_dimensions(path) is None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not a picture at all",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 5,
        encode(Image.new("RGB", (9, 9)), "GIF"),
    ],
    ids=["empty", "text", "truncated png", "unsupported format"],
)
def test_a_file_that_is_not_a_supported_picture_has_no_size(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(content)
    assert probe_dimensions(path) is None


def test_a_jpeg_cut_off_after_its_header_is_still_measured(tmp_path: Path) -> None:
    whole = encode(Image.new("RGB", (64, 48), (9, 9, 9)), "JPEG")
    path = tmp_path / "cut.bin"
    path.write_bytes(
        whole[: whole.index(b"\xff\xda") + 40]
    )  # the scan header, a little entropy data
    assert probe_dimensions(path) == (64, 48), "the header is all that is read"
