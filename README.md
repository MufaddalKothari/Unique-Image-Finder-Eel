# Unique Image Finder (Eel edition)

Scan a **Reference** and a **Working** folder (recursively) and, in a single pass, list

* **Duplicates** – Reference ↔ Working matches
* **Uniques** in each folder – images with no match in the other
* **Duplicates inside each folder** – groups of look-alikes

The UI is a local web page driven by Python through [Eel](https://github.com/python-eel/Eel) (no Qt).

## Run

```
pip install -r requirements.txt
python main.py                 # Chrome/Edge app window if installed, else your default browser
python main.py --no-browser    # just print the URL
python main.py --port 9000
```
`scripts/quick_start.sh` / `quick_start.ps1` create a venv and run it. Python 3.9+.

## The hash cache

Hashes live in a SQLite database, by default `~/.unique_image_finder/image_hashes.db`
(override with `UNIQUE_IMAGE_FINDER_DB`). SQLite tables and indexes are on-disk B-trees:

| Structure | Used for |
|---|---|
| `images` is a `WITHOUT ROWID` table whose primary key is `(absolute path, algo, hash_size)` | "is this file cached?" (one descent), "everything under this folder" (one key-range scan) |
| `idx_images_hash (algo, hash_size, hash)` | exact-hash joins/lookups – 100 % similarity uses a join on this index |

* **Only absolute paths** are stored or accepted (`core/paths.py`; the DB layer rejects relative paths).
* A row is reused only while the file's **size and mtime** are unchanged; otherwise it is re-hashed.
  Undecodable files are cached too (so they aren't re-opened every run) until they change.
* Moving a file from the app keeps its cached hash (the path key is renamed); deleting removes the row.
* Folders that were scanned are listed under **Cache**, where you can refresh, rehash, forget, add a
  folder, purge entries for files that no longer exist, or clear everything.

Near-duplicate search (similarity < 100 %) is a Hamming-radius query, which a B-tree can't answer
directly, so it runs in numpy (`core/similarity.py`) on hashes read from the cache. Cost grows with
(Reference files × Working files); rough numbers on one CPU core: 10k×10k ≈ 1–2 s, 50k×50k ≈ 25–40 s.
The old `.image_hash_cache.db` from the Qt version used a different schema and is not read.

## Layout

```
main.py            entrypoint (logging, starts Eel)
api.py             functions exposed to the browser + /img thumbnail route
core/              paths, hash_db, scanner, indexer, similarity, comparator, search (jobs), fileops, thumbs, metadata
web/               index.html, style.css, app.js
tests/             pytest suite for the core
```

## Notes

* A browser can't reveal absolute paths, so **Browse…** opens a server-side folder picker (or type/paste a path).
* The "Duplicates" tab's *Select all* picks only Working copies, never the Reference.
* Delete = send to Trash/Recycle Bin. File operations and image requests are limited to files the app has
  indexed or listed, because Eel's websocket does not check the page origin.
* Compare-by-fields mode (name/size/created/mtime/dimensions) now really uses the selected fields.
* Logs: `~/.unique_image_finder/unique-image-finder.log`.

Run tests: `python -m pytest tests`
