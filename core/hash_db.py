"""
Persistent image-hash cache backed by SQLite.

Why SQLite: every SQLite table and index *is* a B-tree on disk (tables are
B+trees, indexes are B-trees), so it gives ordered, O(log n) lookups with no
extra dependency.  Two B-trees do the work here:

  * the PRIMARY KEY (path, algo, hash_size) of a WITHOUT ROWID table - the table
    itself is the tree keyed by absolute path, so "every cached image under
    /some/folder" is one range scan, and "is this file cached?" is one descent;
  * idx_images_hash (algo, hash_size, hash) - exact-hash lookups and joins
    ("which files have *exactly* this hash") without scanning anything.

Hamming-radius (near-duplicate) search is not something a B-tree can answer
directly; that lives in core/similarity.py and is fed from here.

Only absolute paths are ever stored (enforced by require_abs).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .paths import abs_path, dir_range, require_abs

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = os.environ.get("UNIQUE_IMAGE_FINDER_DB") or os.path.join(
    os.path.expanduser("~"), ".unique_image_finder", "image_hashes.db"
)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS images (
        path      TEXT    NOT NULL,   -- absolute path
        algo      TEXT    NOT NULL,   -- 'dhash'
        hash_size INTEGER NOT NULL,   -- dhash edge length (16 -> 256 bits)
        size      INTEGER NOT NULL,   -- file size in bytes      } cache is valid only
        mtime_ns  INTEGER NOT NULL,   -- st_mtime_ns             } while both still match
        width     INTEGER,
        height    INTEGER,
        mode      TEXT,
        hash      BLOB,               -- NULL when the file could not be decoded
        error     TEXT,
        last_seen INTEGER NOT NULL,   -- unix seconds
        PRIMARY KEY (path, algo, hash_size)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_images_hash ON images(algo, hash_size, hash)",
    """
    CREATE TABLE IF NOT EXISTS scanned_roots (
        path       TEXT PRIMARY KEY,  -- absolute folder path
        last_scan  INTEGER NOT NULL,
        file_count INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
]

# (path, size, mtime_ns, width, height, mode, hash, error)
CachedRow = Tuple[int, int, Optional[int], Optional[int], Optional[str], Optional[bytes], Optional[str]]


class HashDB:
    """Thread-safe (one connection per thread, WAL mode) hash cache."""

    def __init__(self, path: Optional[str] = None):
        self.path = abs_path(path or DEFAULT_DB_PATH)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._local = threading.local()
        self._conns: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._init_schema()
        self._pk = self._probe_pk_hint()

    # ---- connection handling -------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # autocommit; we BEGIN explicitly
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-65536")  # 64 MB page cache
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    @contextmanager
    def _tx(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def _init_schema(self) -> None:
        with self._tx() as c:
            for stmt in _DDL:
                c.execute(stmt)
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _probe_pk_hint(self) -> str:
        """Force folder range scans onto the table's own B-tree (the primary key): otherwise SQLite
        may pick the covering hash index and scan all of it. Falls back to no hint if unsupported."""
        hint = "INDEXED BY sqlite_autoindex_images_1"
        try:
            self._conn().execute(f"SELECT 1 FROM images {hint} WHERE path >= '/' AND path < '0' LIMIT 1").fetchall()
            return hint
        except sqlite3.Error:
            return ""

    def close(self) -> None:
        with self._conns_lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
        self._local = threading.local()

    # ---- reads ---------------------------------------------------------------
    def cached_under(self, root: str, algo: str, hash_size: int) -> Dict[str, CachedRow]:
        """All cached rows below `root`, in one B-tree range scan."""
        lo, hi = dir_range(require_abs(root))
        cur = self._conn().execute(
            "SELECT path, size, mtime_ns, width, height, mode, hash, error FROM images " + self._pk + " "
            "WHERE path >= ? AND path < ? AND +algo = ? AND +hash_size = ?",  # unary + keeps the planner on the path range
            (lo, hi, algo, hash_size),
        )
        return {r[0]: r[1:] for r in cur}

    def has_path(self, path: str) -> bool:
        require_abs(path)
        return self._conn().execute("SELECT 1 FROM images WHERE path = ? LIMIT 1", (path,)).fetchone() is not None

    def exact_pairs(self, ref_root: str, work_root: str, algo: str, hash_size: int) -> List[Tuple[str, str]]:
        """(ref_path, work_path) with byte-identical hashes - a join on the hash index."""
        rlo, rhi = dir_range(require_abs(ref_root))
        wlo, whi = dir_range(require_abs(work_root))
        cur = self._conn().execute(
            """
            SELECT a.path, b.path
              FROM images a
              JOIN images b ON b.algo = a.algo AND b.hash_size = a.hash_size AND b.hash = a.hash
             WHERE +a.algo = ? AND +a.hash_size = ? AND a.hash IS NOT NULL
               AND a.path >= ? AND a.path < ?
               AND b.path >= ? AND b.path < ?
            """,
            (algo, hash_size, rlo, rhi, wlo, whi),
        )
        return list(cur)

    def find_by_hash(self, hash_bytes: bytes, algo: str, hash_size: int) -> List[str]:
        """Every cached path with exactly this hash (anywhere, not just current folders)."""
        cur = self._conn().execute(
            "SELECT path FROM images WHERE algo = ? AND hash_size = ? AND hash = ? ORDER BY path",
            (algo, hash_size, hash_bytes),
        )
        return [r[0] for r in cur]

    def stats(self) -> dict:
        c = self._conn()
        total, hashed, failed = c.execute("SELECT COUNT(*), COUNT(hash), COUNT(error) FROM images").fetchone()
        roots = c.execute("SELECT COUNT(*) FROM scanned_roots").fetchone()[0]
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {"db_path": self.path, "images": total, "hashed": hashed, "failed": failed,
                "roots": roots, "db_bytes": size}

    def list_roots(self) -> List[dict]:
        c = self._conn()
        out = []
        for path, last_scan, file_count in c.execute(
            "SELECT path, last_scan, file_count FROM scanned_roots ORDER BY path"
        ).fetchall():
            lo, hi = dir_range(path)
            cached = c.execute("SELECT COUNT(*) FROM images " + self._pk + " WHERE path >= ? AND path < ?", (lo, hi)).fetchone()[0]
            out.append({"path": path, "last_scan": last_scan, "file_count": file_count,
                        "cached": cached, "exists": os.path.isdir(path)})
        return out

    # ---- writes --------------------------------------------------------------
    def upsert_many(self, rows: Iterable[Sequence]) -> None:
        """rows: (path, algo, hash_size, size, mtime_ns, width, height, mode, hash, error)"""
        now = int(time.time())
        data = []
        for r in rows:
            require_abs(r[0])
            data.append(tuple(r) + (now,))
        if not data:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO images "
                "(path, algo, hash_size, size, mtime_ns, width, height, mode, hash, error, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                data,
            )

    def touch_many(self, paths: Iterable[str], algo: str, hash_size: int) -> None:
        now = int(time.time())
        data = [(now, require_abs(p), algo, hash_size) for p in paths]
        if not data:
            return
        with self._tx() as c:
            c.executemany("UPDATE images SET last_seen = ? WHERE path = ? AND algo = ? AND hash_size = ?", data)

    def record_root(self, root: str, file_count: int) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO scanned_roots (path, last_scan, file_count) VALUES (?,?,?)",
                      (require_abs(root), int(time.time()), file_count))

    def delete_paths(self, paths: Iterable[str]) -> int:
        data = [(require_abs(p),) for p in paths]
        if not data:
            return 0
        with self._tx() as c:
            before = c.total_changes
            c.executemany("DELETE FROM images WHERE path = ?", data)
            return c.total_changes - before

    def rename_path(self, old: str, new: str) -> None:
        """Keep the cached hash when a file is moved (the tree key changes, the data stays)."""
        require_abs(old), require_abs(new)
        with self._tx() as c:
            c.execute("UPDATE OR REPLACE images SET path = ? WHERE path = ?", (new, old))

    def forget_root(self, root: str) -> int:
        """Drop a folder (and everything cached below it) from the cache."""
        lo, hi = dir_range(require_abs(root))
        with self._tx() as c:
            before = c.total_changes
            c.execute("DELETE FROM images WHERE path >= ? AND path < ?", (lo, hi))
            removed = c.total_changes - before
            c.execute("DELETE FROM scanned_roots WHERE path = ?", (root,))
            return removed

    def purge_missing(self) -> int:
        """Remove cached rows whose file no longer exists on disk."""
        c = self._conn()
        paths = [r[0] for r in c.execute("SELECT DISTINCT path FROM images")]
        missing = [p for p in paths if not os.path.exists(p)]
        removed = 0
        for i in range(0, len(missing), 500):
            removed += self.delete_paths(missing[i:i + 500])
        return removed

    def clear(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM images")
            c.execute("DELETE FROM scanned_roots")
        self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
