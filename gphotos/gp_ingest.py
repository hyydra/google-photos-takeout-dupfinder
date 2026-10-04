#!/usr/bin/env python3
"""
Measure one downloaded Google Photos original, record it, and report duplicates as JSON.
  python gp_ingest.py <gphotos.sqlite> <photo_id> <file_path> [--keep] [--takeout takeout.sqlite]

Prints one JSON line:
  {"sha":..., "size":..., "w":..., "h":..., "dt":..., "make":..., "model":..., "pix":...,
   "gp_dup_of": <other GP id with identical SHA-256 or null>,
   "local_dup":  <local path with identical SHA-256 or null>,
   "probable":   <other GP id / local path that is NOT byte-identical but matches by content, or null>,
   "reason":     "sha" | "pixels" | "similar" | "exif" | null}

Match tiers, strongest first (only the first, byte-identical SHA-256, may ever be auto-trashed):
  sha      identical file bytes
  pixels   identical decoded pixels (SHA-256 of RGB data), different bytes: same picture, other metadata
  similar  same dimensions and a perceptual dHash within SIMILAR_MAX bits: re-compressed / re-saved copy
  exif     same dimensions + EXIF capture time/camera/unique id
The first-seen GP item with a given SHA is the keeper; later ones report gp_dup_of.
"""
import json, os, sqlite3, sys, zlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dupfinder import sha256, read_meta, content_hashes, hamming

from PIL import Image

SIMILAR_MAX = 4  # of 64 dHash bits

SCHEMA = """CREATE TABLE IF NOT EXISTS gp(
 id TEXT PRIMARY KEY, url TEXT, filename TEXT, size INT, sha TEXT, w INT, h INT,
 dt TEXT, make TEXT, model TEXT, uid TEXT, status TEXT, pix TEXT, dh TEXT, crc TEXT)"""


def save_thumb(src_path, dst_path, size=(480, 480)):
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src_path) as im:
            thumb = im.copy()
            thumb.thumbnail(size)
            if thumb.mode in ("RGBA", "P"):
                thumb = thumb.convert("RGB")
            thumb.save(dst_path, "JPEG", quality=85)
    except Exception:
        pass


def file_crc32(path):
    """CRC32 of the file bytes as 8 hex digits (a second, independent checksum next to SHA-256)."""
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def nearest(rows, dh):
    """First (ref, distance) with distance <= SIMILAR_MAX among (ref, dhash) rows, else None."""
    best = None
    for ref, other in rows:
        if not other:
            continue
        d = hamming(dh, other)
        if d <= SIMILAR_MAX and (best is None or d < best[1]):
            best = (ref, d)
    return best


def main():
    db_path, pid, fpath = sys.argv[1:4]
    keep = "--keep" in sys.argv
    takeout = sys.argv[sys.argv.index("--takeout") + 1] if "--takeout" in sys.argv else None
    db = sqlite3.connect(db_path)
    db.execute(SCHEMA)
    for c in ("pix", "dh", "crc"):
        if c not in columns(db, "gp"):
            db.execute(f"ALTER TABLE gp ADD COLUMN {c} TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gp_sha ON gp(sha)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gp_wh ON gp(w, h)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gp_pix ON gp(pix)")

    w, h, dt, make, model, uid = read_meta(fpath)
    pix, dh = content_hashes(fpath)
    sha, size = sha256(fpath), os.path.getsize(fpath)
    crc = file_crc32(fpath)

    # Save thumbnail for this Google Photos item
    thumb_dir = Path(db_path).resolve().parent / "thumbnails"
    save_thumb(Path(fpath), thumb_dir / f"{pid}.jpg")

    t = None
    if takeout and os.path.exists(takeout):
        t = sqlite3.connect(f"file:{takeout}?mode=ro", uri=True, timeout=60)
        tcols = columns(t, "f")

    # Tier 1: byte-identical
    r = db.execute("SELECT id, crc FROM gp WHERE sha=? AND id<>? AND status='ok' LIMIT 1", (sha, pid)).fetchone()
    gp_dup = r[0] if r else None
    # Both checksums must agree before a copy counts as confirmed (rows scanned before CRC existed have none).
    crc_confirmed = bool(r and r[1] and r[1] == crc)
    local_dup = probable = reason = None
    if t:
        r = t.execute("SELECT path FROM f WHERE sha=? LIMIT 1", (sha,)).fetchone()
        local_dup = r[0] if r else None
    if gp_dup or local_dup:
        reason = "sha"

    # Tier 2: identical pixels, different bytes
    if not reason and pix:
        r = db.execute("SELECT id FROM gp WHERE pix=? AND id<>? AND status='ok' LIMIT 1", (pix, pid)).fetchone()
        if r:
            probable, reason = r[0], "pixels"
        elif t and "pix" in tcols:
            r = t.execute("SELECT path FROM f WHERE pix=? LIMIT 1", (pix,)).fetchone()
            if r:
                probable, reason = r[0], "pixels"

    # Tier 3: same dimensions + near-identical perceptual hash
    if not reason and dh and w and h:
        hit = nearest(db.execute("SELECT id, dh FROM gp WHERE w=? AND h=? AND id<>? AND status='ok'", (w, h, pid)), dh)
        if not hit and t and "dh" in tcols:
            hit = nearest(t.execute("SELECT path, dh FROM f WHERE w=? AND h=?", (w, h)), dh)
        if hit:
            probable, reason = hit[0], "similar"

    # Tier 4: same dimensions + EXIF
    if not reason and w and h and (dt or uid):
        r = None
        if t:
            r = t.execute("SELECT path FROM f WHERE w=? AND h=? AND dt=? AND make=? AND model=? AND uid=? LIMIT 1",
                          (w, h, dt, make, model, uid)).fetchone()
        if not r:
            r = db.execute("SELECT id FROM gp WHERE w=? AND h=? AND dt=? AND make=? AND model=? AND uid=? AND id<>? LIMIT 1",
                           (w, h, dt, make, model, uid, pid)).fetchone()
        if r:
            probable, reason = r[0], "exif"

    # Thumbnail of the local match so the dashboard can show it next to this photo
    local_ref = local_dup or (probable if probable and not str(probable).startswith("AF1Q") else None)
    if local_ref and os.path.exists(local_ref):
        save_thumb(Path(local_ref), thumb_dir / f"local_{sha[:12]}.jpg")

    db.execute("INSERT OR REPLACE INTO gp(id,url,filename,size,sha,w,h,dt,make,model,uid,status,pix,dh,crc) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (pid, f"https://photos.google.com/photo/{pid}", os.path.basename(fpath),
                size, sha, w, h, dt, make, model, uid, "ok", pix, dh, crc))
    db.commit()
    if not keep:
        os.remove(fpath)
    print(json.dumps({"sha": sha, "size": size, "w": w, "h": h, "dt": dt, "make": make, "model": model,
                      "pix": pix, "crc": crc, "crc_confirmed": crc_confirmed, "gp_dup_of": gp_dup, "local_dup": local_dup,
                      "probable": probable, "reason": reason}))


if __name__ == "__main__":
    main()
