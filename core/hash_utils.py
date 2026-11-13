"""Perceptual hashing (dHash) and compact byte encoding of hashes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import imagehash
import numpy as np
from PIL import Image, ImageOps

try:  # optional HEIC/HEIF support
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

# Very large images are legitimate here; the user chooses what to scan.
Image.MAX_IMAGE_PIXELS = None

ALGO = "dhash"
DEFAULT_HASH_SIZE = 16  # 16x16 = 256 bits = 32 bytes


@dataclass
class ImageInfo:
    width: int
    height: int
    mode: str
    hash: bytes


def hash_to_bytes(h: imagehash.ImageHash) -> bytes:
    """Pack an ImageHash into ceil(bits/8) bytes (big-endian bit order)."""
    return np.packbits(h.hash.flatten().astype(np.uint8)).tobytes()


def analyze_image(path: str, hash_size: int = DEFAULT_HASH_SIZE) -> ImageInfo:
    """Open the image once; return its (EXIF-corrected) dimensions and dHash.

    Raises on unreadable/corrupt images - the caller records the failure.
    """
    with Image.open(path) as im:
        mode = im.mode
        im = ImageOps.exif_transpose(im)  # phone photos: hash the upright image
        width, height = im.size
        h = imagehash.dhash(im, hash_size=hash_size)
    return ImageInfo(width, height, mode, hash_to_bytes(h))


def hash_bits(hash_size: int) -> int:
    return hash_size * hash_size


def max_hamming_from_similarity(bits: int, similarity_percent: float) -> int:
    """90% similar on a 256-bit hash -> at most 26 differing bits."""
    if similarity_percent >= 100:
        return 0
    if similarity_percent <= 0:
        return bits
    return int(round((1.0 - similarity_percent / 100.0) * bits))
