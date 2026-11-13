"""
Fills in dimensions + hashes for scanned files, using the cache wherever it is still valid.

Per root this costs ONE B-tree range scan (HashDB.cached_under) instead of one query per file.
A cache row is reused only while the file's size and mtime_ns are unchanged; otherwise the
image is re-hashed and the row replaced.  Undecodable files are cached too (hash NULL + error)
so they are not re-opened on every run until they change.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence

from .hash_db import HashDB
from .hash_utils import ALGO, analyze_image
from .paths import is_within
from .scanner import Cancelled, ImageFile

logger = logging.getLogger(__name__)

ProgressCB = Callable[[int, int, str], None]
_BATCH = 256


def _outermost(roots: Sequence[str]) -> List[str]:
    """Drop roots nested inside another root (their rows are covered by the parent's scan)."""
    uniq = sorted(set(roots), key=lambda r: (len(r), r))
    keep: List[str] = []
    for r in uniq:
        if not any(r == k or is_within(r, k) for k in keep):
            keep.append(r)
    return keep


def index_files(db: HashDB, files: Sequence[ImageFile], roots: Sequence[str], hash_size: int,
                force: bool = False, progress: Optional[ProgressCB] = None,
                cancel: Optional[threading.Event] = None) -> dict:
    """Populate .width/.height/.mode/.hash/.error on `files`.  Returns hit/computed/failed counts."""
    cached: Dict[str, tuple] = {}
    if not force:
        for root in _outermost(roots):
            cached.update(db.cached_under(root, ALGO, hash_size))

    hits: List[str] = []
    todo: List[ImageFile] = []
    for f in files:
        row = cached.get(f.path)
        if row is not None and row[0] == f.size and row[1] == f.mtime_ns:
            _, _, f.width, f.height, f.mode, f.hash, f.error = row
            hits.append(f.path)
        else:
            todo.append(f)
    logger.info("Index: %d cache hits, %d to compute (hash_size=%d)", len(hits), len(todo), hash_size)

    if hits:
        db.touch_many(hits, ALGO, hash_size)
    total = len(todo)
    if progress:
        progress(0, total, f"{len(hits)} cached, hashing {total} new/changed")

    def work(f: ImageFile):
        if cancel is not None and cancel.is_set():
            return f, None, "cancelled"
        try:
            return f, analyze_image(f.path, hash_size), None
        except Exception as e:  # corrupt / unsupported / vanished file
            return f, None, f"{type(e).__name__}: {e}"

    done = failed = 0
    workers = min(32, (os.cpu_count() or 2) + 2)
    pending_rows: list = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, total, _BATCH * 2):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = todo[start:start + _BATCH * 2]
            for f, info, err in pool.map(work, chunk):
                if err == "cancelled":
                    raise Cancelled()
                if info is not None:
                    f.width, f.height, f.mode, f.hash, f.error = info.width, info.height, info.mode, info.hash, None
                else:
                    f.hash, f.error = None, err
                    failed += 1
                    logger.debug("Cannot hash %s: %s", f.path, err)
                pending_rows.append((f.path, ALGO, hash_size, f.size, f.mtime_ns,
                                     f.width, f.height, f.mode, f.hash, f.error))
                done += 1
            db.upsert_many(pending_rows)
            pending_rows.clear()
            if progress:
                progress(done, total, f"hashed {done}/{total}")
    return {"cached": len(hits), "computed": total - failed, "failed": failed}
