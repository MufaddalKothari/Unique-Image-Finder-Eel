"""
Absolute-path helpers.

Every path that enters the cache, the scanner or the file operations goes through
`abs_path()` first, so the database only ever contains absolute, normalised paths.
"""
from __future__ import annotations

import os


def abs_path(p) -> str:
    """Return an absolute, normalised path string (~ expanded, '..' collapsed).

    Symlinks are intentionally *not* resolved: the path stays what the user sees.
    """
    if p is None or not str(p).strip():
        raise ValueError("empty path")
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p).strip())))


def require_abs(p: str) -> str:
    """Raise unless `p` is already an absolute path (used at the DB boundary)."""
    if not isinstance(p, str) or not os.path.isabs(p):
        raise ValueError(f"absolute path required, got: {p!r}")
    return p


def dir_range(root: str) -> tuple[str, str]:
    """Half-open key range [lo, hi) covering every path *inside* `root`.

    This is what turns "everything under this folder" into a single B-tree range
    scan:  WHERE path >= lo AND path < hi.  No LIKE, no per-file lookups.
    """
    require_abs(root)
    prefix = root if root.endswith(os.sep) else root + os.sep
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def is_within(path: str, root: str) -> bool:
    lo, hi = dir_range(root)
    return lo <= path < hi


def unique_destination(dest_dir: str, filename: str) -> str:
    """dest_dir/filename, or 'name (1).ext', 'name (2).ext' ... if it already exists."""
    candidate = os.path.join(dest_dir, filename)
    if not os.path.lexists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = os.path.join(dest_dir, f"{stem} ({n}){ext}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1
