#!/usr/bin/env python3
"""
Measure one downloaded Google Photos original, record it, and report duplicates as JSON.
  python gp_ingest.py <gphotos.sqlite> <photo_id> <file_path> [--keep] [--takeout takeout.sqlite]

Prints one JSON line:
  {"sha":..., "size":..., "w":..., "h":..., "dt":..., "make":..., "model":...,
   "gp_dup_of": <other GP id with identical SHA or null>,
   "local_dup": <local path with identical SHA or null>,
   "probable": <other GP id / local path with same dims+EXIF but different SHA or null>}
The first-seen GP item with a given SHA is the keeper; later ones report gp_dup_of.
"""
import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dupfinder import sha256, read_meta

from PIL import Image

SCHEMA = """CREATE TABLE IF NOT EXISTS gp(
 id TEXT PRIMARY KEY, url TEXT, filename TEXT, size INT, sha TEXT, w INT, h INT,
 dt TEXT, make TEXT, model TEXT, uid TEXT, status TEXT)"""


def save_thumb(src_path, dst_path, size=(480, 480)):
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src_path) as im:
            thumb = im.copy()
            thumb.thumbnail(size)
            if thumb.mode in ("RGBA", "P"):
                thumb = thumb.convert("RGB")
            thumb.save(dst_path, "JPEG", quality=85)
    except Exception as e:
        pass


def main():
    db_path, pid, fpath = sys.argv[1:4]
    keep = "--keep" in sys.argv
    takeout = sys.argv[sys.argv.index("--takeout") + 1] if "--takeout" in sys.argv else None
    db = sqlite3.connect(db_path)
    db.execute(SCHEMA)
    w, h, dt, make, model, uid = read_meta(fpath)
    sha, size = sha256(fpath), os.path.getsize(fpath)

    # Save thumbnail for this Google Photos item
    thumb_dir = Path(db_path).resolve().parent / "thumbnails"
    save_thumb(Path(fpath), thumb_dir / f"{pid}.jpg")

    r = db.execute("SELECT id FROM gp WHERE sha=? AND id<>? AND status='ok' LIMIT 1", (sha, pid)).fetchone()
    gp_dup = r[0] if r else None
    local_dup = probable = None
    if takeout and os.path.exists(takeout):
        t = sqlite3.connect(takeout)
        r = t.execute("SELECT path FROM f WHERE sha=? LIMIT 1", (sha,)).fetchone()
        local_dup = r[0] if r else None
        if not local_dup and w and h and (dt or uid):
            r = t.execute("SELECT path FROM f WHERE w=? AND h=? AND dt=? AND make=? AND model=? AND uid=? LIMIT 1",
                          (w, h, dt, make, model, uid)).fetchone()
            probable = r[0] if r else None
        if local_dup and os.path.exists(local_dup):
            save_thumb(Path(local_dup), thumb_dir / f"local_{sha[:12]}.jpg")
        elif probable and os.path.exists(probable):
            save_thumb(Path(probable), thumb_dir / f"local_prob_{sha[:12]}.jpg")

    if not (gp_dup or local_dup) and w and h and (dt or uid):
        r = db.execute("SELECT id FROM gp WHERE w=? AND h=? AND dt=? AND make=? AND model=? AND uid=? AND id<>? LIMIT 1",
                       (w, h, dt, make, model, uid, pid)).fetchone()
        probable = probable or (r[0] if r else None)

    db.execute("INSERT OR REPLACE INTO gp VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (pid, f"https://photos.google.com/photo/{pid}", os.path.basename(fpath),
                size, sha, w, h, dt, make, model, uid, "ok"))
    db.commit()
    if not keep:
        os.remove(fpath)
    print(json.dumps({"sha": sha, "size": size, "w": w, "h": h, "dt": dt, "make": make,
                      "model": model, "gp_dup_of": gp_dup, "local_dup": local_dup, "probable": probable}))


if __name__ == "__main__":
    main()
