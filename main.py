#!/usr/bin/env python3
"""
Unique Image Finder - Eel (browser-UI) entrypoint.

    python main.py                   # opens a Chrome/Edge app window, else your default browser
    python main.py --no-browser      # just start the server and print the URL
    python main.py --port 8765

Env: UNIQUE_IMAGE_FINDER_LOG_LEVEL (default INFO), UNIQUE_IMAGE_FINDER_LOG_DIR, UNIQUE_IMAGE_FINDER_DB
"""
import argparse
import logging
import os
import socket
import sys
import warnings
import webbrowser
from logging.handlers import RotatingFileHandler

LOG_LEVEL = os.environ.get("UNIQUE_IMAGE_FINDER_LOG_LEVEL", "INFO").upper()
LOG_DIR = os.environ.get("UNIQUE_IMAGE_FINDER_LOG_DIR", os.path.join(os.path.expanduser("~"), ".unique_image_finder"))
LOG_FILE = os.path.join(LOG_DIR, "unique-image-finder.log")


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(ch)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    logging.getLogger("PIL").setLevel(logging.INFO)

    def hook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, v, tb)
        else:
            logging.critical("Uncaught exception", exc_info=(t, v, tb))
    sys.excepthook = hook
    logging.captureWarnings(True)
    warnings.simplefilter("default")
    warnings.filterwarnings("ignore", message=r".*(deprecated|Deprecat).*", module=r"eel(\..*)?$")  # eel's pyparsing noise


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("localhost", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def pick_browser() -> "str | None":
    """Chrome/Edge in app mode if installed (eel supports these natively), else None."""
    from eel import browsers
    for mode in ("chrome", "edge"):
        try:
            if browsers._browser_modules[mode].find_path():
                return mode
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Unique Image Finder")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open a window")
    args = ap.parse_args()

    setup_logging()
    import eel
    import api  # noqa: F401  (registers @eel.expose functions and the /img route)

    web = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    eel.init(web, allowed_extensions=[".js", ".html", ".css", ".ico", ".svg"])
    port = free_port(args.port)
    url = f"http://localhost:{port}/index.html"
    mode = None if args.no_browser else pick_browser()
    logging.info("Unique Image Finder at %s  (cache: %s)", url, api.DB.path)
    if not args.no_browser and mode is None:
        webbrowser.open(url)
    try:
        eel.start("index.html", mode=mode, port=port, size=(1320, 880), block=True, shutdown_delay=4.0)
    except (SystemExit, KeyboardInterrupt):
        pass
    logging.info("Exiting")
    api.DB.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
