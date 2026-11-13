"""On-demand JPEG thumbnails / browser-safe previews, with a small on-disk cache."""
from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Optional

from PIL import Image, ImageOps

from . import hash_utils  # noqa: F401  (registers HEIF opener, lifts pixel limit)

logger = logging.getLogger(__name__)

THUMB_DIR = os.path.join(os.path.expanduser("~"), ".unique_image_finder", "thumbs")
BROWSER_NATIVE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _to_rgb(im: Image.Image) -> Image.Image:
    if im.mode.startswith("I"):                       # 16/32-bit greyscale
        try:
            im = im.point(lambda i: i * (1 / 256)).convert("L")
        except Exception:
            pass
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def _render(path: str, px: Optional[int]) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if px:
            im.thumbnail((px, px))
        out = io.BytesIO()
        _to_rgb(im).save(out, "JPEG", quality=85 if px else 92)
        return out.getvalue()


def thumbnail_bytes(path: str, px: int) -> bytes:
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{px}".encode("utf-8", "surrogatepass")).hexdigest()
    cache_file = os.path.join(THUMB_DIR, key[:2], key + ".jpg")
    try:
        with open(cache_file, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    data = _render(path, px)
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cache_file)
    except OSError:
        logger.debug("thumb cache write failed", exc_info=True)
    return data


def full_jpeg_bytes(path: str) -> bytes:
    return _render(path, None)
