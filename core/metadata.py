"""Extended metadata for the side-by-side comparison dialog (read on demand, not cached)."""
from __future__ import annotations

import logging
import os
from typing import Dict

from PIL import Image

from . import hash_utils  # noqa: F401
from .xmp_origin import extract_origin_from_jpeg

logger = logging.getLogger(__name__)

_EXIF_IFD = 0x8769


def read_metadata(path: str) -> Dict[str, object]:
    st = os.stat(path)
    meta: Dict[str, object] = {
        "name": os.path.basename(path), "path": path, "size": st.st_size,
        "created": st.st_ctime, "mtime": st.st_mtime,
        "dimensions": None, "mode": None, "datetime_original": None, "artist": None,
        "copyright": None, "make": None, "model": None, "image_description": None, "origin": None,
    }
    try:
        with Image.open(path) as im:
            meta["dimensions"] = f"{im.size[0]} × {im.size[1]}"
            meta["mode"] = im.mode
            exif = im.getexif()
            sub = exif.get_ifd(_EXIF_IFD) if hasattr(exif, "get_ifd") else {}
            meta["datetime_original"] = sub.get(36867) or exif.get(306)
            meta["artist"] = exif.get(315)
            meta["copyright"] = exif.get(33432)
            meta["make"] = exif.get(271)
            meta["model"] = exif.get(272)
            meta["image_description"] = exif.get(270)
    except Exception as e:
        logger.debug("metadata read failed for %s: %s", path, e)
    if path.lower().endswith((".jpg", ".jpeg")):
        try:
            meta["origin"] = extract_origin_from_jpeg(path)
        except Exception:
            pass
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in meta.items()}
