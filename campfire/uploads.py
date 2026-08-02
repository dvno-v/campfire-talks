"""Conservative validation helpers for user-provided images."""

from pathlib import Path
from urllib.parse import unquote


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


def safe_original_name(raw):
    name = Path(unquote(raw or "image")).name
    name = "".join(character for character in name if character.isprintable() and character not in "\r\n")
    return name[:120] or "image"
