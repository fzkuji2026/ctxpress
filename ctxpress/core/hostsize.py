"""How big an item is for the host: the serialized JSON of a request item, in characters, with each image counted
by its pixel dimensions instead of its base64 length (28x28-px patches, capped; the estimate CliffCompaction
uses, from its images.py). Methods that trigger on request size (chars / 4) use these numbers; the replay
simulator, which has no raw items, falls back to tokens x 4."""
from __future__ import annotations
import base64, json, struct

PATCH_PX = 28
# Providers downscale before counting, so cost is bounded no matter the input.
# Without a ceiling here, one 8000x8000 image would reintroduce a milder
# version of the bug this module exists to fix. The value is the
# high-resolution tier's limit (Claude 4.7+); the older standard tier caps at
# 1568, and the tile-based OpenAI models lower still.
MAX_IMAGE_TOKENS = 4_784
# Dimensions unavailable: an https:// URL rather than inline data, a format
# with no header reader below, a truncated payload. Roughly a 1MP screenshot,
# which is what an unmeasurable image usually turns out to be.
DEFAULT_IMAGE_TOKENS = 1_568
# Enough base64 to cover a JPEG's SOF marker past any EXIF block.
_HEADER_B64_CHARS = 8192


def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        return None
    return struct.unpack(">II", raw[16:24])


def _gif_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw[:6] not in (b"GIF87a", b"GIF89a") or len(raw) < 10:
        return None
    return struct.unpack("<HH", raw[6:10])


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        # Standalone markers carry no length field.
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        # SOF0-SOF15, minus the ones that are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", raw[i + 5 : i + 9])
            return width, height
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP" or len(raw) < 30:
        return None
    fmt = raw[12:16]
    if fmt == b"VP8X":
        w = int.from_bytes(raw[24:27], "little") + 1
        h = int.from_bytes(raw[27:30], "little") + 1
        return w, h
    if fmt == b"VP8 ":
        return (
            int.from_bytes(raw[26:28], "little") & 0x3FFF,
            int.from_bytes(raw[28:30], "little") & 0x3FFF,
        )
    if fmt == b"VP8L" and len(raw) >= 25:
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def dimensions(raw: bytes) -> tuple[int, int] | None:
    """Width and height from a decoded image header, or None if unreadable."""
    for reader in (_png_dimensions, _jpeg_dimensions, _gif_dimensions, _webp_dimensions):
        try:
            dims = reader(raw)
        except (struct.error, IndexError, ValueError):
            continue
        if dims and dims[0] > 0 and dims[1] > 0:
            return dims
    return None


def _header_bytes(payload: str) -> bytes | None:
    """Decode just enough of a base64 payload to read an image header."""
    b64 = payload.split(",", 1)[1] if payload.startswith("data:") else payload
    b64 = b64[:_HEADER_B64_CHARS]
    b64 = b64[: len(b64) - len(b64) % 4]
    if not b64:
        return None
    try:
        return base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        return None


def tokens_for_payload(payload: str) -> int:
    """Estimated token cost of one image, from its dimensions where readable."""
    if not payload.startswith("data:"):
        # A hosted URL: nothing to measure, but it still costs a real image.
        return DEFAULT_IMAGE_TOKENS
    raw = _header_bytes(payload)
    dims = dimensions(raw) if raw else None
    if dims is None:
        return DEFAULT_IMAGE_TOKENS
    width, height = dims
    patches = -(-width // PATCH_PX) * (-(-height // PATCH_PX))
    return max(1, min(patches, MAX_IMAGE_TOKENS))


def image_payloads(obj):
    """Every image payload string in a request body, across all dialects.

    Anthropic  {"type": "image", "source": {"data": ...}}
    Responses  {"type": "input_image", "image_url": "data:..."}
    Chat       {"type": "image_url", "image_url": {"url": "data:..."}}

    Documents are deliberately not included: a PDF's cost tracks its text,
    not its pixels, so a per-image constant would understate it. They keep
    the chars/4 estimate, which errs high rather than low.
    """
    if isinstance(obj, dict):
        kind = obj.get("type")
        if kind == "image":
            data = (obj.get("source") or {}).get("data")
            if isinstance(data, str):
                yield data
                return
        elif kind in ("input_image", "image_url"):
            url = obj.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str):
                yield url
                return
        for value in obj.values():
            yield from image_payloads(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from image_payloads(value)


def billable_chars(obj):
    """Serialized length of `obj` (json.dumps, ensure_ascii=False), images priced by dimensions."""
    try:
        n = len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0
    for payload in image_payloads(obj):
        n += tokens_for_payload(payload) * 4 - len(payload)
    return max(n, 0)


def request_chars(fixed, item_chars):
    """Length of the serialized request body: the body without items (`fixed`, input = []) plus the items and
    the ", " between them."""
    n = len(item_chars)
    return fixed + sum(item_chars) + 2 * max(n - 1, 0)
