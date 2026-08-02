"""Conservative validation and metadata removal for user-provided images.

A photo taken on a phone carries the place and time it was taken. Sharing one
in a channel should not hand that to everyone who can see the channel, so
Campfire rebuilds every upload from the parts needed to decode it and discards
the rest: EXIF, XMP, comments, timestamps, and colour profiles. The image is
therefore interpreted as sRGB, which is the deliberate cost of the rule.

Rebuilding cannot be done half-way. If a file does not parse exactly as its
format requires, it is rejected rather than stored with metadata that was not
understood well enough to remove.
"""

from pathlib import Path
from urllib.parse import unquote

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Only what a decoder needs: pixels, palette, transparency, colour hints and
# animation control. Text, timestamp, EXIF and profile chunks are not here.
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM",
            b"sRGB", b"sBIT", b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT"}
WEBP_DROP = {b"EXIF", b"XMP ", b"ICCP"}
VP8X_METADATA_FLAGS = 0x2C  # ICC profile, EXIF and XMP presence bits


def detect_image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png", {".png"}
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif", {".gif"}
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg", {".jpg", ".jpeg", ".jfif"}
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp", {".webp"}
    return None


def strip_metadata(content, mime_type):
    """Return the image rebuilt without metadata, or None if it cannot be parsed."""
    strippers = {"image/png": strip_png, "image/jpeg": strip_jpeg,
                 "image/gif": strip_gif, "image/webp": strip_webp}
    stripper = strippers.get(mime_type)
    return stripper(content) if stripper else None


def strip_png(content):
    """Copy the chunks a decoder needs, in order, dropping everything else."""
    if not content.startswith(PNG_SIGNATURE):
        return None
    rebuilt = bytearray(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    expecting_header = True
    while offset + 12 <= len(content):
        length = int.from_bytes(content[offset:offset + 4], "big")
        chunk_type = content[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(content):
            return None
        if expecting_header and chunk_type != b"IHDR":
            return None
        expecting_header = False
        if chunk_type in PNG_KEEP:
            rebuilt += content[offset:end]
        offset = end
        if chunk_type == b"IEND":
            return bytes(rebuilt)  # anything appended past the end goes too
    return None


def strip_jpeg(content):
    """Copy the frame, dropping every application and comment segment.

    Application segments are where EXIF, XMP, IPTC and Photoshop data live. The
    lone exception is Adobe's short APP14, which tells a decoder how to read the
    colour channels rather than describing the photographer.
    """
    if not content.startswith(b"\xff\xd8\xff"):
        return None
    rebuilt = bytearray(b"\xff\xd8")
    offset = 2
    while offset + 4 <= len(content):
        if content[offset] != 0xFF:
            return None
        marker = content[offset + 1]
        if marker == 0xFF:  # fill byte
            offset += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            rebuilt += content[offset:offset + 2]
            offset += 2
            continue
        if marker == 0xD9:
            return bytes(rebuilt + b"\xff\xd9")
        length = int.from_bytes(content[offset + 2:offset + 4], "big")
        end = offset + 2 + length
        if length < 2 or end > len(content):
            return None
        segment = content[offset:end]
        if marker == 0xDA:  # start of scan; entropy data runs to the end marker
            trailing = content[end:]
            finish = trailing.find(b"\xff\xd9")
            if finish == -1:
                return None
            return bytes(rebuilt + segment + trailing[:finish + 2])
        adobe = marker == 0xEE and segment[4:9] == b"Adobe" and length <= 32
        if (0xE0 <= marker <= 0xEF or marker == 0xFE) and not adobe:
            offset = end
            continue
        rebuilt += segment
        offset = end
    return None


def gif_blocks_end(content, offset):
    """Walk a chain of length-prefixed sub-blocks, returning the offset past it."""
    while offset < len(content):
        size = content[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
    return None


def strip_gif(content):
    """Copy frames and timing, dropping comment, plain-text and foreign extensions."""
    if content[:6] not in (b"GIF87a", b"GIF89a") or len(content) < 13:
        return None
    rebuilt = bytearray(content[:13])
    offset = 13
    descriptor = content[10]
    if descriptor & 0x80:  # global colour table
        offset += 3 * 2 ** ((descriptor & 7) + 1)
        if offset > len(content):
            return None
        rebuilt += content[13:offset]
    while offset < len(content):
        block = content[offset]
        if block == 0x3B:  # trailer
            return bytes(rebuilt + b"\x3b")
        if block == 0x21:  # extension
            if offset + 3 > len(content):
                return None
            label = content[offset + 1]
            end = gif_blocks_end(content, offset + 2)
            if end is None:
                return None
            looping = label == 0xFF and content[offset + 3:offset + 14] == b"NETSCAPE2.0"
            if label == 0xF9 or looping:  # graphic control, or the loop count
                rebuilt += content[offset:end]
            offset = end
            continue
        if block == 0x2C:  # image descriptor
            if offset + 10 > len(content):
                return None
            local = content[offset + 9]
            cursor = offset + 10 + (3 * 2 ** ((local & 7) + 1) if local & 0x80 else 0)
            if cursor + 1 > len(content):
                return None
            end = gif_blocks_end(content, cursor + 1)  # past the LZW code size
            if end is None:
                return None
            rebuilt += content[offset:end]
            offset = end
            continue
        return None
    return None


def strip_webp(content):
    """Copy the RIFF chunks that carry the image, dropping EXIF, XMP and profiles."""
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        return None
    declared = int.from_bytes(content[4:8], "little")
    limit = 8 + declared
    if limit > len(content) or declared < 4:
        return None
    body = bytearray()
    offset = 12
    while offset + 8 <= limit:
        fourcc = content[offset:offset + 4]
        size = int.from_bytes(content[offset + 4:offset + 8], "little")
        end = offset + 8 + size + (size & 1)  # chunks are padded to an even length
        if end > limit:
            return None
        if fourcc not in WEBP_DROP:
            chunk = bytearray(content[offset:end])
            if fourcc == b"VP8X" and size >= 1:
                # Leaving the flags set would describe metadata that is now absent.
                chunk[8] &= ~VP8X_METADATA_FLAGS & 0xFF
            body += chunk
        offset = end
    if not body:
        return None
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + bytes(body)


def safe_original_name(raw):
    name = Path(unquote(raw or "image")).name
    name = "".join(character for character in name if character.isprintable() and character not in "\r\n")
    return name[:120] or "image"
