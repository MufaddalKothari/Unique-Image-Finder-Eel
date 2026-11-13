"""
Vectorised Hamming-distance search over packed hashes (numpy).

A B-tree cannot answer "all hashes within k bits of X", so near-duplicate search is done here on
arrays pulled from the cache: hashes are packed into uint64 words, XOR-ed block by block and
popcounted.  Exact (distance 0) matching does not need this - it is a hash-index join in
HashDB.exact_pairs / a dict lookup.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .scanner import Cancelled

_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
_HAS_BITWISE_COUNT = hasattr(np, "bitwise_count")  # numpy >= 2.0


def to_matrix(hashes: Sequence[bytes]) -> np.ndarray:
    """Pack equal-length hash byte strings into an (n, words) uint64 matrix (zero-padded)."""
    if not hashes:
        return np.zeros((0, 1), dtype=np.uint64)
    padded = (len(hashes[0]) + 7) // 8 * 8
    buf = b"".join(h.ljust(padded, b"\x00") for h in hashes)
    return np.frombuffer(buf, dtype=">u8").reshape(len(hashes), padded // 8).astype(np.uint64)


def _block_distances(a_blk: np.ndarray, b_t: np.ndarray) -> np.ndarray:
    """(rows, m) Hamming distances, accumulated one 64-bit word at a time (small temporaries).
    b_t is b transposed to (words, m) and contiguous."""
    dist = None
    for w in range(a_blk.shape[1]):
        x = a_blk[:, w, None] ^ b_t[w][None, :]
        if _HAS_BITWISE_COUNT:
            c = np.bitwise_count(x)
        else:
            c = _POP8[x.view(np.uint8)].reshape(*x.shape, 8).sum(-1, dtype=np.uint8)
        dist = c.astype(np.uint16) if dist is None else dist + c
    return dist


def pairs_within(a: np.ndarray, b: np.ndarray, max_dist: int, upper_triangle: bool = False,
                 cancel: Optional[threading.Event] = None) -> List[Tuple[int, int, int]]:
    """All (i, j, distance) with hamming(a[i], b[j]) <= max_dist.

    upper_triangle=True is for comparing a set with itself (a is b): only j > i is reported.
    Row blocks run in parallel threads (numpy releases the GIL).
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    b_t = np.ascontiguousarray(b.T)
    rows = max(1, min(n, 1_000_000 // m))

    def work(i0: int):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        dist = _block_distances(a[i0:i0 + rows], b_t)
        ii, jj = np.nonzero(dist <= max_dist)
        if upper_triangle:
            keep = jj > (ii + i0)
            ii, jj = ii[keep], jj[keep]
        if not len(ii):
            return []
        return list(zip((ii + i0).tolist(), jj.tolist(), dist[ii, jj].tolist()))

    out: List[Tuple[int, int, int]] = []
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        for part in pool.map(work, range(0, n, rows)):
            out.extend(part)
    return out
