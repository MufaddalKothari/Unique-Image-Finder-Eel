"""
Functions exposed to the browser UI through Eel (`eel.<name>(...)()` in JavaScript) plus the
`/img` route used for thumbnails and previews.

Security model: Eel only listens on localhost, and every file operation / image request is
restricted to paths this app itself has seen (current search results or the hash cache), so a
stray web page cannot use the websocket to delete or read arbitrary files.
"""
from __future__ import annotations

import logging
import os
import string
import sys
from typing import Any, Dict, List, Optional

import bottle as btl
import eel

from core import fileops, settings, thumbs
from core.comparator import FIELD_KEYS
from core.hash_db import HashDB
from core.hash_utils import ALGO, DEFAULT_HASH_SIZE
from core.metadata import read_metadata
from core.paths import abs_path
from core.scanner import is_probable_image
from core.search import JOBS, TABS, Job

logger = logging.getLogger(__name__)

DB = HashDB()


# ------------------------------------------------------------------ helpers
def _is_known(path: Any) -> bool:
    """Absolute, normalised path that this app has indexed or listed in a result."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        return False
    return JOBS.is_known_path(path) or DB.has_path(path)


def _clean_criteria(c: dict) -> dict:
    sim = int(c.get("similarity") or 90)
    return {
        "hash": bool(c.get("hash")),
        "fields": [f for f in (c.get("fields") or []) if f in FIELD_KEYS],
        "similarity": max(50, min(100, sim)),
        "hash_size": DEFAULT_HASH_SIZE,
    }


def _result(job: Optional[Job]) -> Job:
    if job is None:
        raise ValueError("unknown or expired job")
    return job


def _dir_image_count(path: str) -> int:
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if is_probable_image(e.name) and e.is_file():
                    n += 1
    except OSError:
        pass
    return n


# ------------------------------------------------------------------ startup / settings
@eel.expose
def get_bootstrap() -> dict:
    s = settings.load()
    return {"home": os.path.expanduser("~"), "platform": sys.platform, "settings": s,
            "cache": DB.stats(), "hash_size": DEFAULT_HASH_SIZE, "fields": list(FIELD_KEYS)}


@eel.expose
def save_settings(values: dict) -> bool:
    allowed = {"last_ref", "last_work", "similarity", "theme", "use_hash", "fields", "last_dest", "picker_start"}
    settings.update({k: v for k, v in (values or {}).items() if k in allowed})
    return True


# ------------------------------------------------------------------ folder browsing
@eel.expose
def list_dirs(path: str = "", show_hidden: bool = False) -> dict:
    """Sub-folders of `path` (absolute). Empty path lists drives / the filesystem root."""
    if not path:
        if os.name == "nt":
            roots = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        else:
            roots = ["/"]
        home = os.path.expanduser("~")
        dirs = [{"name": "Home", "path": home, "images": _dir_image_count(home)}]
        dirs += [{"name": r, "path": r, "images": 0} for r in roots]
        return {"path": "", "parent": None, "dirs": dirs, "error": None}
    try:
        p = abs_path(path)
        out = []
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and (show_hidden or not e.name.startswith(".")):
                        out.append({"name": e.name, "path": e.path, "images": _dir_image_count(e.path)})
                except OSError:
                    continue
        out.sort(key=lambda d: d["name"].casefold())
        parent = os.path.dirname(p)
        return {"path": p, "parent": parent if parent != p else "", "dirs": out, "error": None}
    except Exception as e:
        return {"path": path, "parent": "", "dirs": [], "error": str(e)}


# ------------------------------------------------------------------ search jobs
@eel.expose
def start_search(ref: str, work: str, criteria: dict) -> dict:
    try:
        ref_p, work_p = abs_path(ref), abs_path(work)
    except ValueError:
        return {"ok": False, "error": "Please choose both a reference and a working folder."}
    for label, p in (("Reference", ref_p), ("Working", work_p)):
        if not os.path.isdir(p):
            return {"ok": False, "error": f"{label} folder does not exist: {p}"}
    job = JOBS.start(Job("search", ref_p, work_p, _clean_criteria(criteria or {})), DB)
    settings.update({"last_ref": ref_p, "last_work": work_p})
    return {"ok": True, "job_id": job.id, "ref": ref_p, "work": work_p}


@eel.expose
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    return job.snapshot() if job else {"state": "error", "error": "unknown job", "counts": {}}


@eel.expose
def cancel_job(job_id: str) -> bool:
    job = JOBS.get(job_id)
    if job:
        job.cancel.set()
    return job is not None


@eel.expose
def get_page(job_id: str, tab: str, offset: int = 0, limit: int = 40) -> dict:
    job = _result(JOBS.get(job_id))
    if tab not in TABS:
        raise ValueError("bad tab")
    return {"items": job.page(tab, max(0, int(offset)), max(1, min(200, int(limit)))), "counts": job.counts()}


@eel.expose
def get_tab_paths(job_id: str, tab: str, which: str = "all") -> List[str]:
    if tab not in TABS:
        raise ValueError("bad tab")
    return _result(JOBS.get(job_id)).paths(tab, which)


@eel.expose
def dismiss_tab(job_id: str, tab: str) -> dict:
    """'Keep': hide a tab's rows from the results without touching any file."""
    if tab not in TABS:
        raise ValueError("bad tab")
    job = _result(JOBS.get(job_id))
    job.clear_tab(tab)
    return job.counts()


@eel.expose
def get_meta(a: str, b: str) -> dict:
    for p in (a, b):
        if not _is_known(p):
            raise ValueError("unknown path")
    return {"a": read_metadata(a), "b": read_metadata(b)}


# ------------------------------------------------------------------ file operations
def _known_only(paths: List[str]) -> List[str]:
    return [p for p in paths if _is_known(p)]


@eel.expose
def delete_paths(job_id: Optional[str], paths: List[str]) -> dict:
    res = fileops.delete_files(_known_only(paths), DB)
    job = JOBS.get(job_id) if job_id else None
    if job:
        job.remove_paths(set(res["done"]))
    return {**res, "counts": job.counts() if job else {}}


@eel.expose
def move_paths(job_id: Optional[str], paths: List[str], dest: str) -> dict:
    try:
        dest_p = abs_path(dest)
        res = fileops.move_files(_known_only(paths), dest_p, DB)
    except Exception as e:
        return {"done": [], "errors": [{"path": dest, "error": str(e)}], "counts": {}}
    job = JOBS.get(job_id) if job_id else None
    if job:
        job.remove_paths(set(res["done"]))
    settings.update({"last_dest": dest_p})
    return {**res, "counts": job.counts() if job else {}}


@eel.expose
def copy_uniques(job_id: str, dest: str) -> dict:
    job = _result(JOBS.get(job_id))
    try:
        dest_p = abs_path(dest)
        res = fileops.copy_files({"reference_uniques": _known_only(job.paths("unique_ref")),
                                  "working_uniques": _known_only(job.paths("unique_work"))}, dest_p)
    except Exception as e:
        return {"done": [], "errors": [{"path": dest, "error": str(e)}]}
    settings.update({"last_dest": dest_p})
    return res


@eel.expose
def open_file(path: str, reveal: bool = False) -> bool:
    if not _is_known(path):
        return False
    try:
        fileops.open_path(path, reveal)
        return True
    except Exception:
        logger.exception("open failed")
        return False


# ------------------------------------------------------------------ cache manager
@eel.expose
def cache_info() -> dict:
    return {"stats": DB.stats(), "roots": DB.list_roots()}


@eel.expose
def cache_index(path: str, force: bool = False) -> dict:
    try:
        p = abs_path(path)
    except ValueError:
        return {"ok": False, "error": "Choose a folder."}
    if not os.path.isdir(p):
        return {"ok": False, "error": f"Folder does not exist: {p}"}
    job = JOBS.start(Job("index", p, None, {"force": bool(force), "hash_size": DEFAULT_HASH_SIZE}), DB)
    return {"ok": True, "job_id": job.id}


@eel.expose
def cache_forget(path: str) -> int:
    return DB.forget_root(abs_path(path))


@eel.expose
def cache_purge_missing() -> int:
    return DB.purge_missing()


@eel.expose
def cache_clear() -> bool:
    DB.clear()
    return True


# ------------------------------------------------------------------ /img route
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}


@btl.route("/img")
def serve_image():
    """GET /img?p=<absolute path>&s=<px>   s>0: JPEG thumbnail, s=0: full image (converted if needed)."""
    path = btl.request.query.getunicode("p") or ""
    try:
        px = max(0, min(4096, int(btl.request.query.get("s") or 0)))
    except ValueError:
        px = 0
    if not _is_known(path) or os.path.splitext(path)[1].lower() not in _IMG_EXTS or not os.path.isfile(path):
        btl.abort(404, "not found")
    try:
        if px == 0 and os.path.splitext(path)[1].lower() in thumbs.BROWSER_NATIVE:
            resp = btl.static_file(os.path.basename(path), root=os.path.dirname(path))
            resp.set_header("Cache-Control", "private, max-age=3600")
            return resp
        data = thumbs.thumbnail_bytes(path, px) if px else thumbs.full_jpeg_bytes(path)
    except Exception:
        logger.debug("preview failed for %s", path, exc_info=True)
        btl.abort(415, "cannot decode image")
    btl.response.content_type = "image/jpeg"
    btl.response.set_header("Cache-Control", "private, max-age=86400")
    return data
