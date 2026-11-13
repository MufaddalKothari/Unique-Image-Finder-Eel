"""
Recursive directory scanner.  Only stat()s files - nothing is opened or decoded here;
dimensions and hashes come from the cache (or are computed once by the indexer).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from .paths import abs_path

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif", ".webp", ".heic", ".heif"}
_JUNK_NAMES = {"thumbs.db", ".ds_store"}


class Cancelled(Exception):
    """Raised when the user cancels a running job."""


@dataclass
class ImageFile:
    path: str                      # always absolute
    name: str
    size: int
    mtime_ns: int
    ctime_ns: int
    width: Optional[int] = None
    height: Optional[int] = None
    mode: Optional[str] = None
    hash: Optional[bytes] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "size": self.size,
            "width": self.width, "height": self.height,
            "mtime": self.mtime_ns / 1e9, "created": self.ctime_ns / 1e9,
        }


def is_probable_image(name: str) -> bool:
    low = name.lower()
    if name.startswith("._") or low in _JUNK_NAMES:
        return False
    return os.path.splitext(low)[1] in IMAGE_EXTENSIONS


def scan_directory(root: str, cancel: Optional[threading.Event] = None,
                   on_count: Optional[Callable[[int], None]] = None) -> List[ImageFile]:
    """Recursively list image files under `root` (symlinked directories are not followed)."""
    root = abs_path(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    out: List[ImageFile] = []
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as e:
            logger.warning("Cannot read directory %s: %s", current, e)
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not is_probable_image(entry.name) or not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                out.append(ImageFile(path=entry.path, name=entry.name, size=st.st_size,
                                     mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns))
        if on_count is not None:
            on_count(len(out))
    out.sort(key=lambda f: f.path)
    logger.info("Scanned %d image(s) in %s", len(out), root)
    return out
