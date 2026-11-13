"""
Classify two folders' images in a single pass:

  * duplicates   - (reference file, working file) pairs that match
  * unique_ref   - reference files with no match in the working folder
  * unique_work  - working files with no match in the reference folder
  * dups_ref / dups_work - groups of duplicates *within* each folder

Hash mode (criteria["hash"] truthy): perceptual dHash similarity.  Hashes come from the cache
(core/indexer.py).  similarity=100 -> exact matches via the hash B-tree index; below that ->
vectorised Hamming search (core/similarity.py).

Field mode: images match when every selected field (name/size/created/mtime/dimensions) is equal.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .hash_db import HashDB
from .hash_utils import ALGO, DEFAULT_HASH_SIZE, hash_bits, max_hamming_from_similarity
from .scanner import Cancelled, ImageFile
from .similarity import pairs_within, to_matrix

logger = logging.getLogger(__name__)

FIELD_KEYS = ("name", "size", "created", "mtime", "dimensions")


@dataclass
class Match:
    ref: ImageFile
    work: ImageFile
    dist: Optional[int]
    reason: str


@dataclass
class Group:
    files: List[ImageFile]
    max_dist: int


@dataclass
class CompareResult:
    duplicates: List[Match] = field(default_factory=list)
    unique_ref: List[ImageFile] = field(default_factory=list)
    unique_work: List[ImageFile] = field(default_factory=list)
    dups_ref: List[Group] = field(default_factory=list)
    dups_work: List[Group] = field(default_factory=list)
    unreadable_ref: List[ImageFile] = field(default_factory=list)
    unreadable_work: List[ImageFile] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _groups_from_pairs(files: Sequence[ImageFile], pairs: Sequence[Tuple[int, int, int]]) -> List[Group]:
    uf = _UnionFind(len(files))
    for i, j, _ in pairs:
        uf.union(i, j)
    members: Dict[int, List[int]] = defaultdict(list)
    maxd: Dict[int, int] = defaultdict(int)
    touched = set()
    for i, j, d in pairs:
        touched.add(i)
        touched.add(j)
    for idx in touched:
        members[uf.find(idx)].append(idx)
    for i, j, d in pairs:
        r = uf.find(i)
        maxd[r] = max(maxd[r], d)
    groups = [Group(sorted((files[k] for k in ks), key=lambda f: f.path), maxd[r]) for r, ks in members.items()]
    groups.sort(key=lambda g: g.files[0].path)
    return groups


def _groups_from_keys(files: Sequence[ImageFile], keyfn) -> List[Group]:
    buckets: Dict[object, List[ImageFile]] = defaultdict(list)
    for f in files:
        buckets[keyfn(f)].append(f)
    groups = [Group(sorted(v, key=lambda f: f.path), 0) for v in buckets.values() if len(v) > 1]
    groups.sort(key=lambda g: g.files[0].path)
    return groups


def _finish(res: CompareResult, ref: Sequence[ImageFile], work: Sequence[ImageFile],
            matches: List[Match]) -> CompareResult:
    matches.sort(key=lambda m: (m.ref.path, m.work.path))
    res.duplicates = matches
    matched_ref = {m.ref.path for m in matches}
    matched_work = {m.work.path for m in matches}
    res.unique_ref = [f for f in ref if f.path not in matched_ref]
    res.unique_work = [f for f in work if f.path not in matched_work]
    return res


# --------------------------------------------------------------------------- hash mode
def _compare_hash(ref_files, work_files, criteria, db, ref_root, work_root, cancel) -> CompareResult:
    hash_size = int(criteria.get("hash_size") or DEFAULT_HASH_SIZE)
    similarity = float(criteria.get("similarity") or 90.0)
    bits = hash_bits(hash_size)
    k = max_hamming_from_similarity(bits, similarity)
    logger.info("Hash compare: similarity=%s%% hash_size=%d -> max hamming %d/%d", similarity, hash_size, k, bits)

    res = CompareResult()
    res.unreadable_ref = [f for f in ref_files if f.hash is None]
    res.unreadable_work = [f for f in work_files if f.hash is None]
    ref = [f for f in ref_files if f.hash is not None]
    work = [f for f in work_files if f.hash is not None]

    matches: List[Match] = []
    if ref and work:
        if k == 0 and db is not None and ref_root and work_root:
            # exact: join on the hash B-tree index, then keep only files from this scan
            ref_by_path = {f.path: f for f in ref}
            work_by_path = {f.path: f for f in work}
            for rp, wp in db.exact_pairs(ref_root, work_root, ALGO, hash_size):
                r, w = ref_by_path.get(rp), work_by_path.get(wp)
                if r is not None and w is not None and rp != wp:
                    matches.append(Match(r, w, 0, "dhash identical"))
        else:
            ra, wa = to_matrix([f.hash for f in ref]), to_matrix([f.hash for f in work])
            for i, j, d in pairs_within(ra, wa, k, cancel=cancel):
                r, w = ref[i], work[j]
                if r.path == w.path:
                    continue
                matches.append(Match(r, w, d, f"dhash distance {d}/{bits} ({100 * (1 - d / bits):.1f}% similar)"))

    for side, files in (("ref", ref), ("work", work)):
        if len(files) < 2:
            continue
        if k == 0:
            groups = _groups_from_keys(files, lambda f: f.hash)
        else:
            m = to_matrix([f.hash for f in files])
            groups = _groups_from_pairs(files, pairs_within(m, m, k, upper_triangle=True, cancel=cancel))
        if side == "ref":
            res.dups_ref = groups
        else:
            res.dups_work = groups
    return _finish(res, ref, work, matches)


# --------------------------------------------------------------------------- field mode
def _field_key(f: ImageFile, fields: Sequence[str]):
    parts = []
    for name in fields:
        if name == "name":
            parts.append(f.name.casefold())
        elif name == "size":
            parts.append(f.size)
        elif name == "created":
            parts.append(f.ctime_ns // 1_000_000_000)
        elif name == "mtime":
            parts.append(f.mtime_ns // 1_000_000_000)
        elif name == "dimensions":
            parts.append((f.width, f.height))
    return tuple(parts)


def _compare_fields(ref_files, work_files, criteria) -> CompareResult:
    fields = [x for x in (criteria.get("fields") or []) if x in FIELD_KEYS] or ["name"]
    reason = "+".join(fields)
    res = CompareResult()
    if "dimensions" in fields:  # needs a decoded image
        res.unreadable_ref = [f for f in ref_files if f.width is None]
        res.unreadable_work = [f for f in work_files if f.width is None]
        ref = [f for f in ref_files if f.width is not None]
        work = [f for f in work_files if f.width is not None]
    else:
        ref, work = list(ref_files), list(work_files)

    keyfn = lambda f: _field_key(f, fields)
    work_by_key: Dict[object, List[ImageFile]] = defaultdict(list)
    for w in work:
        work_by_key[keyfn(w)].append(w)
    matches = [Match(r, w, None, reason) for r in ref for w in work_by_key.get(keyfn(r), ()) if r.path != w.path]
    res.dups_ref = _groups_from_keys(ref, keyfn)
    res.dups_work = _groups_from_keys(work, keyfn)
    return _finish(res, ref, work, matches)


# --------------------------------------------------------------------------- entry point
def compare(ref_files: Sequence[ImageFile], work_files: Sequence[ImageFile], criteria: dict,
            db: Optional[HashDB] = None, ref_root: Optional[str] = None, work_root: Optional[str] = None,
            cancel: Optional[threading.Event] = None) -> CompareResult:
    if criteria.get("hash"):
        return _compare_hash(ref_files, work_files, criteria, db, ref_root, work_root, cancel)
    return _compare_fields(ref_files, work_files, criteria)
