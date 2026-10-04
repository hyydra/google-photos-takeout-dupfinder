#!/usr/bin/env python3
"""
Local image duplicate finder.

Groups:
  EXACT     - identical SHA-256 (byte-identical files)
  PROBABLE  - same pixel dimensions + same EXIF signature
              (DateTimeOriginal[+SubSec] + Make + Model [+ ImageUniqueID]),
              but different SHA (re-saved / metadata-stripped-then-edited copies, etc.)

Never deletes anything. Optional --quarantine moves the non-keeper files of EXACT groups
into a folder. Results cached in SQLite (path, size, mtime) so reruns are fast.

Usage:
  python dupfinder.py scan D:\\Photos E:\\Backup --out report
  python dupfinder.py scan D:\\Photos --out report --quarantine D:\\dupes --keep oldest
"""
import argparse, csv, hashlib, json, os, shutil, sqlite3, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ExifTags

Image.MAX_IMAGE_PIXELS = None
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif",
        ".bmp", ".gif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"}
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

TAGS = {v: k for k, v in ExifTags.TAGS.items()}
DB_SCHEMA = """CREATE TABLE IF NOT EXISTS f(
 path TEXT PRIMARY KEY, size INT, mtime REAL, sha TEXT, w INT, h INT,
 dt TEXT, make TEXT, model TEXT, uid TEXT)"""


def sha256(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def read_meta(path):
    w = h = None
    dt = make = model = uid = ""
    try:
        with Image.open(path) as im:
            w, h = im.size
            ex = im.getexif()
            sub = ex.get_ifd(0x8769) if hasattr(ex, "get_ifd") else {}
            dt = str(sub.get(TAGS["DateTimeOriginal"]) or ex.get(TAGS["DateTime"]) or "")
            ss = sub.get(TAGS.get("SubsecTimeOriginal", 0))
            if dt and ss:
                dt += "." + str(ss).strip()
            make = str(ex.get(TAGS["Make"], "")).strip("\x00 ")
            model = str(ex.get(TAGS["Model"], "")).strip("\x00 ")
            uid = str(sub.get(TAGS.get("ImageUniqueID", 0), "")).strip("\x00 ")
    except Exception:
        pass
    return w, h, dt, make, model, uid


def process(path):
    st = os.stat(path)
    w, h, dt, make, model, uid = read_meta(path)
    return (str(path), st.st_size, st.st_mtime, sha256(path), w, h, dt, make, model, uid)


def walk(roots):
    for r in roots:
        for dp, _, fns in os.walk(r):
            for fn in fns:
                if Path(fn).suffix.lower() in EXTS:
                    yield Path(dp) / fn


def scan(args):
    db = sqlite3.connect(args.db)
    db.execute(DB_SCHEMA)
    cached = {r[0]: r for r in db.execute("SELECT * FROM f")}
    todo, rows = [], []
    for p in walk(args.paths):
        st = os.stat(p)
        c = cached.get(str(p))
        if c and c[1] == st.st_size and c[2] == st.st_mtime:
            rows.append(c)
        else:
            todo.append(p)
    print(f"cached: {len(rows)}  to process: {len(todo)}", file=sys.stderr)
    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        for i, r in enumerate(ex.map(lambda p: safe(process, p), todo), 1):
            if r:
                db.execute("INSERT OR REPLACE INTO f VALUES(?,?,?,?,?,?,?,?,?,?)", r)
                rows.append(r)
            if i % 200 == 0:
                db.commit()
                print(f"  {i}/{len(todo)}  {time.time()-t0:.0f}s", file=sys.stderr)
    db.commit()
    return rows


def safe(fn, p):
    try:
        return fn(p)
    except Exception as e:
        print(f"ERR {p}: {e}", file=sys.stderr)


def group(rows):
    exact = defaultdict(list)
    for r in rows:
        exact[r[3]].append(r)
    exact = [g for g in exact.values() if len(g) > 1]
    in_exact = {r[0] for g in exact for r in g}

    sig = defaultdict(list)
    for r in rows:
        path, size, mt, sha, w, h, dt, make, model, uid = r
        if not (w and h and (dt or uid)):  # need real EXIF evidence
            continue
        sig[(w, h, dt, make, model, uid)].append(r)
    probable = []
    for g in sig.values():
        if len({r[3] for r in g}) > 1:  # >1 distinct sha => not already exact
            probable.append(g)
    return exact, probable


def pick_keeper(g, mode):
    key = {"oldest": lambda r: r[2], "newest": lambda r: -r[2],
           "shortest": lambda r: len(r[0]), "largest": lambda r: -r[1]}[mode]
    return min(g, key=key)


def write_reports(exact, probable, out):
    with open(out + ".csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["type", "group", "path", "size", "mtime", "sha256", "w", "h",
                     "exif_datetime", "make", "model", "unique_id"])
        for kind, groups in (("EXACT", exact), ("PROBABLE", probable)):
            for i, g in enumerate(groups, 1):
                for r in g:
                    wr.writerow([kind, f"{kind[0]}{i}", *r[0:2], time.strftime("%F %T", time.localtime(r[2])), *r[3:]])
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump({"exact": exact, "probable": probable}, fh, ensure_ascii=False, indent=1)
    wasted = sum(sum(r[1] for r in g) - max(r[1] for r in g) for g in exact)
    print(f"EXACT groups: {len(exact)} (reclaimable {wasted/1e6:.1f} MB)  "
          f"PROBABLE groups: {len(probable)}\nreports: {out}.csv / {out}.json")


def quarantine(exact, dest, mode):
    dest = Path(dest)
    moved = 0
    for g in exact:
        keep = pick_keeper(g, mode)
        for r in g:
            if r is keep:
                continue
            src = Path(r[0])
            tgt = dest / (r[3][:8] + "_" + src.name)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            n = 1
            while tgt.exists():  # same sha + same filename in different folders
                tgt = dest / f"{r[3][:8]}_{n}_{src.name}"
                n += 1
            shutil.move(str(src), str(tgt))
            moved += 1
    print(f"moved {moved} files to {dest}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out", default="duplicates")
    s.add_argument("--db", default="dupcache.sqlite")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--quarantine")
    s.add_argument("--keep", default="oldest", choices=["oldest", "newest", "shortest", "largest"])
    a = ap.parse_args()
    rows = scan(a)
    exact, probable = group(rows)
    write_reports(exact, probable, a.out)
    if a.quarantine:
        quarantine(exact, a.quarantine, a.keep)


if __name__ == "__main__":
    main()
