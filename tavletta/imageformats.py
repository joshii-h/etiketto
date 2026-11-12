"""The image file formats a label may embed: one list for everything that
takes an image file (the renderer, the print server, shipping labels, the
window's file chooser, its import check and drag and drop).

Only what Pillow decodes itself, quickly and once its size is known: not
EPS or PS (Ghostscript would run it), not ICO/ICNS/CUR (decoded when
opened, before their size can be checked), not the dozens of rarer
formats (PPM, TGA, AVIF, ...), which the window would accept and the
renderer then draw as a placeholder. The desktop uses the same list as the
server: a file in another format is converted once, a label that renders
differently on the desktop and on the server would be worse."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

# Pillow's names of the formats (Image.open(formats=...))
FORMATS = ("PNG", "JPEG", "GIF", "BMP", "WEBP", "TIFF")
# what the user reads (texts listing the formats)
NAMES = "PNG, JPEG, GIF, BMP, WebP, TIFF"
MIME_TYPES = ("image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp", "image/tiff")
# file name suffixes, lower case, without the dot
SUFFIXES = ("png", "jpg", "jpeg", "jpe", "gif", "bmp", "webp", "tif", "tiff")


def has_image_suffix(path: str | Path) -> bool:
    """Whether the file's name says it is an image in one of the formats."""
    return Path(path).suffix.lower().lstrip(".") in SUFFIXES


def is_image(path: str | Path) -> bool:
    """Whether the file is an image in one of the formats (its header,
    nothing is decoded), whatever its name."""
    try:
        with Image.open(path, formats=FORMATS):
            return True
    except Exception:  # noqa: BLE001 - PIL raises many kinds
        return False
