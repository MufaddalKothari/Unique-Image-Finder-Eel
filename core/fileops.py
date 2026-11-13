"""File operations on already-validated absolute paths."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

from send2trash import send2trash

from .hash_db import HashDB
from .paths import require_abs, unique_destination

logger = logging.getLogger(__name__)


def delete_files(paths: List[str], db: Optional[HashDB] = None) -> Dict[str, list]:
    """Move files to the Trash / Recycle Bin."""
    deleted, errors = [], []
    for p in paths:
        try:
            require_abs(p)
            if not os.path.isfile(p):
                raise FileNotFoundError("file not found")
            send2trash(p)
            deleted.append(p)
        except Exception as e:
            logger.warning("Delete failed for %s: %s", p, e)
            errors.append({"path": p, "error": str(e)})
    if db is not None and deleted:
        db.delete_paths(deleted)
    return {"done": deleted, "errors": errors}


def move_files(paths: List[str], dest_dir: str, db: Optional[HashDB] = None) -> Dict[str, list]:
    """Move files into dest_dir (name clashes get ' (1)', ' (2)' ...). The cached hash follows the file."""
    require_abs(dest_dir)
    if not os.path.isdir(dest_dir):
        raise NotADirectoryError(dest_dir)
    moved, errors = [], []
    for p in paths:
        try:
            require_abs(p)
            if not os.path.isfile(p):
                raise FileNotFoundError("file not found")
            if os.path.dirname(p) == dest_dir:
                raise ValueError("already in that folder")
            target = os.path.abspath(unique_destination(dest_dir, os.path.basename(p)))
            shutil.move(p, target)
            moved.append(p)
            if db is not None:
                try:
                    db.rename_path(p, target)
                except Exception:
                    logger.exception("cache rename failed for %s", p)
        except Exception as e:
            logger.warning("Move failed for %s: %s", p, e)
            errors.append({"path": p, "error": str(e)})
    return {"done": moved, "errors": errors}


def copy_files(groups: Dict[str, List[str]], dest_dir: str) -> Dict[str, list]:
    """groups: {subfolder_name: [paths]} copied to dest_dir/subfolder_name/ (metadata preserved)."""
    require_abs(dest_dir)
    copied, errors = [], []
    for sub, paths in groups.items():
        if not paths:
            continue
        folder = os.path.join(dest_dir, sub)
        os.makedirs(folder, exist_ok=True)
        for p in paths:
            try:
                require_abs(p)
                if not os.path.isfile(p):
                    raise FileNotFoundError("file not found")
                shutil.copy2(p, unique_destination(folder, os.path.basename(p)))
                copied.append(p)
            except Exception as e:
                errors.append({"path": p, "error": str(e)})
    return {"done": copied, "errors": errors}


def open_path(path: str, reveal: bool = False) -> None:
    """Open a file in the default app, or show it in the file manager."""
    require_abs(path)
    if sys.platform.startswith("win"):
        if reveal:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])
