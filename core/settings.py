"""Tiny JSON settings store (replaces Qt's QSettings)."""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".unique_image_finder", "settings.json")
_lock = threading.Lock()


def load() -> dict:
    with _lock:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def update(values: dict) -> dict:
    with _lock:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(values)
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            tmp = SETTINGS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, SETTINGS_PATH)
        except OSError:
            logger.exception("could not save settings")
        return data
