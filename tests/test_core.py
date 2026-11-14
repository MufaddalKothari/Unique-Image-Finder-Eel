import os, shutil, sys, time, threading, tempfile
os.environ.setdefault('UNIQUE_IMAGE_FINDER_DB', os.path.join(tempfile.mkdtemp(), 'api.db'))
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.hash_db import HashDB
from core.hash_utils import ALGO, analyze_image, max_hamming_from_similarity
from core.indexer import index_files
from core.comparator import compare
from core.paths import abs_path, dir_range, unique_destination
from core.scanner import scan_directory
from core.search import Job
from core.similarity import pairs_within, to_matrix
from core import fileops


def make_img(path, seed, size=(200, 150)):
    """Smooth pseudo-random image so different seeds give clearly different dhashes."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (9, 12, 3), dtype=np.uint8)
    im = Image.fromarray(small).resize(size, Image.BICUBIC)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, quality=95)
    return path


@pytest.fixture
def world(tmp_path):
    ref, work = str(tmp_path / "ref"), str(tmp_path / "work")
    make_img(f"{ref}/a.jpg", 1)
    make_img(f"{ref}/sub/b.jpg", 2)
    make_img(f"{ref}/only_ref.jpg", 3)
    shutil.copy(f"{ref}/a.jpg", f"{ref}/a_copy.jpg")                 # intra-ref exact dup
    shutil.copy(f"{ref}/a.jpg", f"{work}/a_renamed.jpg") if os.makedirs(work) is None else None
    # near-duplicate of b: re-encoded smaller
    Image.open(f"{ref}/sub/b.jpg").resize((100, 75)).save(f"{work}/b_small.jpg", quality=70)
    make_img(f"{work}/only_work.jpg", 4)
    shutil.copy(f"{work}/only_work.jpg", f"{work}/only_work2.jpg")   # intra-work dup
    open(f"{work}/notes.txt", "w").write("x")
    open(f"{work}/broken.jpg", "wb").write(b"not an image")
    return ref, work, HashDB(str(tmp_path / "db" / "h.db"))


def run(ref, work, db, similarity=90, force=False):
    rf, wf = scan_directory(ref), scan_directory(work)
    index_files(db, rf + wf, [ref, work], 16, force=force)
    crit = {"hash": True, "similarity": similarity, "hash_size": 16}
    return rf, wf, compare(rf, wf, crit, db=db, ref_root=ref, work_root=work)


def names(files): return sorted(f.name for f in files)


def test_paths_are_absolute(tmp_path):
    assert os.path.isabs(abs_path("."))
    db = HashDB(str(tmp_path / "x.db"))
    with pytest.raises(ValueError):
        db.has_path("relative/path.jpg")
    with pytest.raises(ValueError):
        db.upsert_many([("rel.jpg", ALGO, 16, 1, 1, 1, 1, "RGB", b"\0" * 32, None)])
    lo, hi = dir_range("/a/b")
    assert lo == "/a/b/" and "/a/b/c/d.jpg" >= lo and "/a/b/c/d.jpg" < hi and "/a/bc/x.jpg" >= hi


def test_scan_skips_junk_and_nonimages(world):
    ref, work, _ = world
    assert "notes.txt" not in names(scan_directory(work))
    assert "broken.jpg" in names(scan_directory(work))   # extension ok; failure is found at hash time


def test_cache_roundtrip_and_invalidation(world):
    ref, work, db = world
    rf, wf = scan_directory(ref), scan_directory(work)
    s1 = index_files(db, rf + wf, [ref, work], 16)
    assert s1["cached"] == 0 and s1["failed"] == 1 and s1["computed"] == len(rf) + len(wf) - 1
    st = db.stats()
    assert st["images"] == len(rf) + len(wf) and st["failed"] == 1
    # second run: everything (incl. the broken file) is a cache hit
    rf, wf = scan_directory(ref), scan_directory(work)
    s2 = index_files(db, rf + wf, [ref, work], 16)
    assert s2["computed"] == 0 and s2["failed"] == 0 and s2["cached"] == len(rf) + len(wf)
    assert all(os.path.isabs(p) for p in db.cached_under(ref, ALGO, 16))
    # modify a file -> only that one is rehashed
    time.sleep(0.01)
    make_img(f"{ref}/a.jpg", 99)
    rf, wf = scan_directory(ref), scan_directory(work)
    s3 = index_files(db, rf + wf, [ref, work], 16)
    assert s3["computed"] == 1 and s3["cached"] == len(rf) + len(wf) - 1
    # force rehashes all
    s4 = index_files(db, rf + wf, [ref, work], 16, force=True)
    assert s4["cached"] == 0


def test_range_scan_is_scoped(world):
    ref, work, db = world
    rf, wf = scan_directory(ref), scan_directory(work)
    index_files(db, rf + wf, [ref, work], 16)
    assert set(db.cached_under(ref, ALGO, 16)) == {f.path for f in rf}
    assert set(db.cached_under(os.path.join(ref, "sub"), ALGO, 16)) == {os.path.join(ref, "sub", "b.jpg")}


def test_hash_results_90(world):
    ref, work, db = world
    rf, wf, res = run(ref, work, db, 90)
    pairs = sorted((m.ref.name, m.work.name) for m in res.duplicates)
    assert ("a.jpg", "a_renamed.jpg") in pairs and ("a_copy.jpg", "a_renamed.jpg") in pairs
    assert ("b.jpg", "b_small.jpg") in pairs
    assert names(res.unique_ref) == ["only_ref.jpg"]
    assert names(res.unique_work) == ["only_work.jpg", "only_work2.jpg"]
    assert [names(g.files) for g in res.dups_ref] == [["a.jpg", "a_copy.jpg"]]
    assert [names(g.files) for g in res.dups_work] == [["only_work.jpg", "only_work2.jpg"]]
    assert names(res.unreadable_work) == ["broken.jpg"]


def test_exact_sql_path_matches_numpy_path(world):
    ref, work, db = world
    rf, wf, exact = run(ref, work, db, 100)          # SQL hash-index join
    crit = {"hash": True, "similarity": 100, "hash_size": 16}
    viadict = compare(rf, wf, crit, db=None)         # numpy path (no db given)
    key = lambda r: sorted((m.ref.path, m.work.path) for m in r.duplicates)
    assert key(exact) == key(viadict) and len(key(exact)) == 2
    assert all(m.dist == 0 for m in exact.duplicates)
    # near-dup is NOT an exact match
    assert ("b.jpg", "b_small.jpg") not in {(m.ref.name, m.work.name) for m in exact.duplicates}
    assert db.find_by_hash(rf[0].hash, ALGO, 16)


def test_overlapping_folders_never_match_self(world):
    ref, work, db = world
    rf, rf2 = scan_directory(ref), scan_directory(ref)
    index_files(db, rf + rf2, [ref], 16)
    res = compare(rf, rf2, {"hash": True, "similarity": 90, "hash_size": 16}, db=db, ref_root=ref, work_root=ref)
    assert all(m.ref.path != m.work.path for m in res.duplicates)
    assert names(res.unique_ref) == ["b.jpg", "only_ref.jpg"]


def test_field_mode(world):
    ref, work, db = world
    shutil.copy(f"{ref}/only_ref.jpg", f"{work}/ONLY_REF.JPG")
    rf, wf = scan_directory(ref), scan_directory(work)
    res = compare(rf, wf, {"hash": False, "fields": ["name", "size"]})
    assert [(m.ref.name, m.work.name) for m in res.duplicates] == [("only_ref.jpg", "ONLY_REF.JPG")]
    assert res.duplicates[0].reason == "name+size"


def test_similarity_engine_vs_bruteforce():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 256, (60, 32), dtype=np.uint8)
    noisy = base.copy()
    flip = rng.integers(0, 32, (60,)); noisy[np.arange(60), flip] ^= 1   # 1 bit flipped
    a, b = to_matrix([bytes(r) for r in base]), to_matrix([bytes(r) for r in noisy])
    got = {(i, j): d for i, j, d in pairs_within(a, b, 3)}
    for i in range(60):
        assert got[(i, i)] == 1
    for (i, j), d in got.items():
        assert d == bin(int.from_bytes(bytes(base[i]), "big") ^ int.from_bytes(bytes(noisy[j]), "big")).count("1")
    tri = pairs_within(a, a, 0, upper_triangle=True)
    assert tri == []


def test_move_keeps_cache_and_unique_names(world, tmp_path):
    ref, work, db = world
    rf, wf = scan_directory(ref), scan_directory(work)
    index_files(db, rf + wf, [ref, work], 16)
    dest = str(tmp_path / "dest"); os.makedirs(dest)
    src = f"{work}/only_work.jpg"
    make_img(f"{dest}/only_work.jpg", 7)                      # clash
    res = fileops.move_files([src], dest, db)
    assert res["done"] == [src] and os.path.exists(f"{dest}/only_work (1).jpg")
    assert not db.has_path(src) and db.has_path(f"{dest}/only_work (1).jpg")
    # moved file is a cache hit at its new location
    nf = scan_directory(dest)
    assert index_files(db, nf, [dest], 16)["cached"] >= 1


def test_delete_removes_db_rows(world, monkeypatch):
    ref, work, db = world
    rf = scan_directory(ref); index_files(db, rf, [ref], 16)
    monkeypatch.setattr(fileops, "send2trash", lambda p: os.remove(p))
    p = rf[0].path
    assert fileops.delete_files([p], db)["done"] == [p]
    assert not db.has_path(p) and not os.path.exists(p)
    assert fileops.delete_files([p], db)["errors"]            # already gone


def test_purge_forget_clear(world):
    ref, work, db = world
    rf, wf = scan_directory(ref), scan_directory(work)
    index_files(db, rf + wf, [ref, work], 16); db.record_root(ref, len(rf))
    os.remove(rf[0].path)
    assert db.purge_missing() == 1
    assert db.list_roots()[0]["cached"] == len(rf) - 1
    assert db.forget_root(ref) == len(rf) - 1 and db.list_roots() == []
    db.clear(); assert db.stats()["images"] == 0


def test_job_end_to_end(world):
    ref, work, db = world
    job = Job("search", ref, work, {"hash": True, "similarity": 90, "hash_size": 16, "fields": []})
    job.start(db); job.thread.join(30)
    snap = job.snapshot()
    assert snap["state"] == "done", snap
    assert snap["counts"] == {"duplicates": 3, "unique_ref": 1, "unique_work": 2, "dups_ref": 1, "dups_work": 1}
    assert job.paths("duplicates", "work") == sorted(set(job.paths("duplicates", "work")), key=job.paths("duplicates", "work").index)
    assert len(job.paths("dups_ref", "all_but_first")) == 1
    gone = {job.page("unique_ref", 0, 5)[0]["path"]}
    job.remove_paths(gone)
    assert job.counts()["unique_ref"] == 0
    # second run is pure cache
    job2 = Job("search", ref, work, {"hash": True, "similarity": 90, "hash_size": 16, "fields": []})
    job2.start(db); job2.thread.join(30)
    assert job2.summary["computed"] == 0 and job2.summary["cached"] > 0


def test_cancel(world):
    ref, work, db = world
    job = Job("search", ref, work, {"hash": True, "similarity": 90, "hash_size": 16})
    job.cancel.set(); job.start(db); job.thread.join(10)
    assert job.state == "cancelled"


def test_thumbnails_and_metadata(tmp_path):
    from core import thumbs, metadata
    import io
    d = tmp_path / "t"; d.mkdir()
    Image.new("RGBA", (300, 200), (255, 0, 0, 80)).save(d / "alpha.png")
    Image.new("P", (50, 50)).save(d / "pal.gif")
    Image.fromarray((np.arange(64 * 64, dtype=np.uint16).reshape(64, 64) * 16)).save(d / "grey16.png")
    Image.new("RGB", (400, 100), "blue").save(d / "wide.tif")
    thumbs.THUMB_DIR = str(tmp_path / "cache")
    for name in ("alpha.png", "pal.gif", "grey16.png", "wide.tif"):
        data = thumbs.thumbnail_bytes(str(d / name), 64)
        im = Image.open(io.BytesIO(data)); assert im.format == "JPEG" and max(im.size) <= 64
    assert thumbs.thumbnail_bytes(str(d / "alpha.png"), 64) == thumbs.thumbnail_bytes(str(d / "alpha.png"), 64)  # disk-cache hit
    assert Image.open(io.BytesIO(thumbs.full_jpeg_bytes(str(d / "wide.tif")))).size == (400, 100)
    m = metadata.read_metadata(str(d / "alpha.png"))
    assert m["dimensions"] == "300 × 200" and m["size"] > 0 and m["origin"] is None


def test_exif_rotation_is_hashed_upright(tmp_path):
    im = Image.fromarray(np.random.default_rng(5).integers(0, 255, (9, 12, 3), dtype=np.uint8)).resize((240, 180))
    a = tmp_path / "upright.jpg"; im.save(a)
    rot = im.rotate(90, expand=True); b = tmp_path / "phone.jpg"
    ex = Image.Exif(); ex[274] = 8   # Orientation: rotate 90 CCW to display -> stored sideways, displays upright
    rot.rotate(180).transpose(Image.FLIP_LEFT_RIGHT) if False else None
    stored = im.rotate(-90, expand=True)   # sideways pixels
    stored.save(b, exif=ex)
    ia, ib = analyze_image(str(a), 16), analyze_image(str(b), 16)
    assert (ib.width, ib.height) == (ia.width, ia.height) == (240, 180)
    dist = bin(int.from_bytes(ia.hash, "big") ^ int.from_bytes(ib.hash, "big")).count("1")
    assert dist <= 26


def test_known_path_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    import api
    monkeypatch.setattr(api, "DB", HashDB(str(tmp_path / "g.db")))
    assert not api._is_known("relative.jpg") and not api._is_known("/a/../b.jpg") and not api._is_known(None)
    assert not api._is_known("/etc/passwd")
    api.DB.upsert_many([("/x/y.jpg", ALGO, 16, 1, 1, 1, 1, "RGB", b"\0" * 32, None)])
    assert api._is_known("/x/y.jpg")


def test_work_select_all_never_includes_reference_files(tmp_path):
    base = str(tmp_path / "ov")
    make_img(f"{base}/a.jpg", 1)
    shutil.copy(f"{base}/a.jpg", f"{base}/sub/x.jpg") if os.makedirs(f"{base}/sub") is None else None
    shutil.copy(f"{base}/a.jpg", f"{base}/sub/y.jpg")
    db = HashDB(str(tmp_path / "o.db"))
    job = Job("search", base, f"{base}/sub", {"hash": True, "similarity": 90, "hash_size": 16, "fields": []})
    job.start(db); job.thread.join(20)
    refs = {d["ref"]["path"] for d in job.tabs["duplicates"]}
    work = job.paths("duplicates", "work")
    assert not (set(work) & refs)
    # disjoint folders are unaffected
    ref2, work2 = str(tmp_path / "r2"), str(tmp_path / "w2")
    make_img(f"{ref2}/a.jpg", 1); os.makedirs(work2); shutil.copy(f"{ref2}/a.jpg", f"{work2}/a2.jpg")
    j2 = Job("search", ref2, work2, {"hash": True, "similarity": 90, "hash_size": 16, "fields": []})
    j2.start(db); j2.thread.join(20)
    assert j2.paths("duplicates", "work") == [f"{work2}/a2.jpg"]
