"""
Background jobs: a folder-pair search, or a cache (re)index of one folder.

Jobs run in a real thread; the UI polls `snapshot()` for progress, then pages through the
results.  Results stay server-side so a 50,000-row result set never goes over the wire at once.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Dict, List, Optional, Set

from .comparator import CompareResult, FIELD_KEYS, compare
from .hash_db import HashDB
from .hash_utils import ALGO, DEFAULT_HASH_SIZE
from .indexer import index_files
from .paths import abs_path, is_within
from .scanner import Cancelled, ImageFile, scan_directory

logger = logging.getLogger(__name__)

TABS = ("duplicates", "unique_ref", "unique_work", "dups_ref", "dups_work")


class Job:
    def __init__(self, kind: str, ref: Optional[str], work: Optional[str], criteria: dict):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind                      # "search" | "index"
        self.ref, self.work, self.criteria = ref, work, criteria
        self.state = "running"                # running | done | error | cancelled
        self.progress = 0.0
        self.message = "Starting…"
        self.error: Optional[str] = None
        self.warnings: List[str] = []
        self.cancel = threading.Event()
        self.tabs: Dict[str, list] = {t: [] for t in TABS}
        self.known_paths: Set[str] = set()
        self.summary: dict = {}
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- progress ------------------------------------------------------------
    def _set(self, progress: float, message: Optional[str] = None) -> None:
        self.progress = max(0.0, min(100.0, progress))
        if message is not None:
            self.message = message

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "progress": round(self.progress, 1),
                "message": self.message, "error": self.error, "warnings": self.warnings,
                "counts": self.counts(), "summary": self.summary}

    def counts(self) -> dict:
        with self._lock:
            return {
                "duplicates": len(self.tabs["duplicates"]),
                "unique_ref": len(self.tabs["unique_ref"]),
                "unique_work": len(self.tabs["unique_work"]),
                "dups_ref": len(self.tabs["dups_ref"]),
                "dups_work": len(self.tabs["dups_work"]),
            }

    # ---- running -------------------------------------------------------------
    def start(self, db: HashDB) -> None:
        self.thread = threading.Thread(target=self._run, args=(db,), name=f"job-{self.id}", daemon=True)
        self.thread.start()

    def _run(self, db: HashDB) -> None:
        try:
            if self.kind == "index":
                self._run_index(db)
            else:
                self._run_search(db)
            self.state = "done"
            self._set(100, self.message if self.kind == "index" else "Done")
        except Cancelled:
            self.state, self.message = "cancelled", "Cancelled"
        except Exception as e:
            logger.exception("Job %s failed", self.id)
            self.state, self.error = "error", f"{type(e).__name__}: {e}"

    def _index(self, db: HashDB, files: List[ImageFile], roots: List[str], lo: float, hi: float, force=False) -> dict:
        hash_size = int(self.criteria.get("hash_size") or DEFAULT_HASH_SIZE)
        by_path: Dict[str, ImageFile] = {}
        for f in files:
            by_path.setdefault(f.path, f)       # same file reached from both roots -> hash once

        def cb(done: int, total: int, msg: str) -> None:
            self._set(lo + (hi - lo) * (done / total if total else 1), msg)

        stats = index_files(db, list(by_path.values()), roots, hash_size, force=force, progress=cb, cancel=self.cancel)
        for f in files:                          # copy results onto the twin objects
            c = by_path[f.path]
            if c is not f:
                f.width, f.height, f.mode, f.hash, f.error = c.width, c.height, c.mode, c.hash, c.error
        return stats

    def _run_index(self, db: HashDB) -> None:
        root = abs_path(self.ref)
        self._set(2, "Scanning…")
        files = scan_directory(root, self.cancel, on_count=lambda n: self._set(2 + min(8, n / 5000), f"Scanning… {n} images"))
        stats = self._index(db, files, [root], 10, 98, force=bool(self.criteria.get("force")))
        db.record_root(root, len(files))
        self.summary = {"files": len(files), **stats}
        self.message = f"{len(files)} images: {stats['cached']} cached, {stats['computed']} hashed, {stats['failed']} unreadable"

    def _run_search(self, db: HashDB) -> None:
        crit = self.criteria
        ref, work = abs_path(self.ref), abs_path(self.work)
        if ref == work or is_within(ref, work) or is_within(work, ref):
            self.warnings.append("The two folders overlap. A file is never matched with itself, and “Select all Working copies” / “Delete duplicates from Working” skip any file that is also a Reference file, so you can’t delete every copy of an image.")

        self._set(1, "Scanning reference folder…")
        ref_files = scan_directory(ref, self.cancel, on_count=lambda n: self._set(1 + min(4, n / 5000), f"Scanning reference… {n} images"))
        self._set(5, "Scanning working folder…")
        work_files = scan_directory(work, self.cancel, on_count=lambda n: self._set(5 + min(4, n / 5000), f"Scanning working… {n} images"))
        self.known_paths = {f.path for f in ref_files} | {f.path for f in work_files}

        use_hash = bool(crit.get("hash"))
        fields = [x for x in (crit.get("fields") or []) if x in FIELD_KEYS]
        if use_hash or "dimensions" in fields:
            stats = self._index(db, ref_files + work_files, [ref, work], 10, 88)
            db.record_root(ref, len(ref_files))
            db.record_root(work, len(work_files))
        else:
            stats = {"cached": 0, "computed": 0, "failed": 0}

        self._set(90, "Comparing…")
        res: CompareResult = compare(ref_files, work_files, crit, db=db, ref_root=ref, work_root=work, cancel=self.cancel)
        self._build_tabs(res)
        unreadable = len(res.unreadable_ref) + len(res.unreadable_work)
        if unreadable:
            self.warnings.append(f"{unreadable} file(s) could not be decoded and were skipped.")
        self.summary = {"ref_files": len(ref_files), "work_files": len(work_files), **stats}

    def _build_tabs(self, res: CompareResult) -> None:
        with self._lock:
            self.tabs["duplicates"] = [
                {"ref": m.ref.to_dict(), "work": m.work.to_dict(), "dist": m.dist, "reason": m.reason}
                for m in res.duplicates]
            self.tabs["unique_ref"] = [f.to_dict() for f in res.unique_ref]
            self.tabs["unique_work"] = [f.to_dict() for f in res.unique_work]
            self.tabs["dups_ref"] = [{"files": [f.to_dict() for f in g.files], "max_dist": g.max_dist} for g in res.dups_ref]
            self.tabs["dups_work"] = [{"files": [f.to_dict() for f in g.files], "max_dist": g.max_dist} for g in res.dups_work]

    # ---- results access ------------------------------------------------------
    def page(self, tab: str, offset: int, limit: int) -> List[dict]:
        with self._lock:
            return self.tabs[tab][offset:offset + limit]

    def paths(self, tab: str, which: str = "all") -> List[str]:
        """Paths for 'select all'. duplicates: which=ref|work|both.  dups_*: which=all_but_first|all."""
        out: List[str] = []
        seen: Set[str] = set()

        def add(p: str) -> None:
            if p not in seen:
                seen.add(p)
                out.append(p)

        with self._lock:
            items = list(self.tabs[tab])
        # Overlapping folders: a file can be a Working copy in one pair and the Reference in another.
        # "Working copies" must never include a file that also acts as a Reference, otherwise bulk
        # delete could remove every copy of an image.
        ref_paths = {it["ref"]["path"] for it in items} if tab == "duplicates" and which == "work" else set()
        for it in items:
            if tab == "duplicates":
                if which == "work" and it["work"]["path"] in ref_paths:
                    continue
                if which in ("ref", "both"):
                    add(it["ref"]["path"])
                if which in ("work", "both"):
                    add(it["work"]["path"])
            elif tab.startswith("dups_"):
                files = it["files"][1:] if which == "all_but_first" else it["files"]
                for f in files:
                    add(f["path"])
            else:
                add(it["path"])
        return out

    def clear_tab(self, tab: str) -> None:
        """Hide a tab's results (files stay on disk)."""
        with self._lock:
            self.tabs[tab] = []

    def remove_paths(self, paths: Set[str]) -> None:
        """Drop deleted/moved files from the results."""
        with self._lock:
            t = self.tabs
            t["duplicates"] = [d for d in t["duplicates"] if d["ref"]["path"] not in paths and d["work"]["path"] not in paths]
            for k in ("unique_ref", "unique_work"):
                t[k] = [f for f in t[k] if f["path"] not in paths]
            for k in ("dups_ref", "dups_work"):
                groups = []
                for g in t[k]:
                    files = [f for f in g["files"] if f["path"] not in paths]
                    if len(files) > 1:
                        groups.append({"files": files, "max_dist": g["max_dist"]})
                t[k] = groups
        self.known_paths -= paths


class JobManager:
    def __init__(self, keep: int = 4):
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def start(self, job: Job, db: HashDB) -> Job:
        with self._lock:
            for j in self._jobs.values():          # one running job at a time: cancel the old one
                if j.state == "running":
                    j.cancel.set()
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
        job.start(db)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def is_known_path(self, path: str) -> bool:
        with self._lock:
            return any(path in j.known_paths for j in self._jobs.values())


JOBS = JobManager()
