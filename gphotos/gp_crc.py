#!/usr/bin/env python3
"""
CRC32 backfill helper for online photos that were scanned before CRC checksums existed.

  python gp_crc.py list <gphotos.sqlite>                  # JSON list of photo ids that still have no CRC
  python gp_crc.py fix  <gphotos.sqlite> <id> <file>      # compute CRC32 + SHA-256 of a freshly downloaded
                                                          # original, store the CRC only if the SHA-256 still
                                                          # matches the recorded one, then delete the file
Prints one JSON line for `fix`: {"crc": "...", "sha_match": true|false}
"""
import json, os, sqlite3, sys, zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dupfinder import sha256


def connect(path):
    db = sqlite3.connect(path, timeout=60)
    if "crc" not in {r[1] for r in db.execute("PRAGMA table_info(gp)")}:
        db.execute("ALTER TABLE gp ADD COLUMN crc TEXT")
        db.commit()
    return db


def crc32_file(path):
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def main():
    cmd, dbp = sys.argv[1], sys.argv[2]
    db = connect(dbp)
    if cmd == "list":
        print(json.dumps([r[0] for r in db.execute(
            "SELECT id FROM gp WHERE status='ok' AND w IS NOT NULL AND (crc IS NULL OR crc='')")]))
    elif cmd == "fix":
        pid, fpath = sys.argv[3], sys.argv[4]
        row = db.execute("SELECT sha FROM gp WHERE id=?", (pid,)).fetchone()
        crc, sha = crc32_file(fpath), sha256(fpath)
        match = bool(row and row[0] == sha)
        if match:
            db.execute("UPDATE gp SET crc=? WHERE id=?", (crc, pid))
            db.commit()
        os.remove(fpath)
        print(json.dumps({"crc": crc, "sha_match": match}))
    else:
        sys.exit("usage: gp_crc.py list|fix ...")


if __name__ == "__main__":
    main()
