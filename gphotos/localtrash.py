#!/usr/bin/env python3
"""
Send local duplicate files to the Windows Recycle Bin, with checks so nothing is ever deleted permanently.

Why the checks: the shell silently deletes PERMANENTLY (no error) when the Recycle Bin is disabled for the
drive, when an item does not fit, or when a path is too long; and a full bin purges its oldest items.
So before and during a run this module
  * refuses unless the drive's Recycle Bin is enabled and has room for the whole batch (capacity check),
  * skips paths that are too long for the shell API instead of risking them,
  * verifies after every batch that the Recycle Bin gained one entry per file that disappeared, and aborts
    the whole run (BinBreaker) the moment that is not true,
  * keeps the oldest copy of each byte-identical group, re-verifies files against the index first,
  * saves a preview of each file before it goes, and logs every file to recycled-log.tsv.
Everything sent here can be restored from the Recycle Bin.
"""
import ctypes, hashlib, os, re, sqlite3, sys, time, winreg, zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(os.environ.get("RECYCLE_DATA_DIR") or Path(__file__).resolve().parent)  # where recycled-log.tsv + previews live
RECYCLED_LOG = ROOT / "recycled-log.tsv"
REMOVED_TXT = ROOT / "removed-paths.txt"   # paths already sent to the bin whose index rows could not be deleted yet
THUMB_DIR = ROOT / "thumbnails"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_SAFE_PATH = 240          # the shell API mishandles paths near MAX_PATH (260)
BIN_HEADROOM = 0.85          # use at most this fraction of the bin's free capacity


class BinBreaker(Exception):
    """The Recycle Bin did not receive what was removed: stop everything."""


# ---- Windows shell ------------------------------------------------------------------------------
FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def _shell_recycle(paths):
    """SHFileOperation(FO_DELETE) with FOF_ALLOWUNDO = move to the Recycle Bin. Returns the error code."""
    joined = "\0".join(paths) + "\0\0"
    buf = ctypes.create_unicode_buffer(joined, len(joined) + 1)
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, ctypes.c_void_p), None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


# ---- Recycle Bin inspection ---------------------------------------------------------------------
def _drive_root(path):
    return os.path.splitdrive(os.path.abspath(path))[0] + os.sep


def _volume_guid(root):
    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 64):
        return None
    m = re.search(r"Volume(\{[0-9a-fA-F-]+\})", buf.value)
    return m.group(1) if m else None


def bin_settings(root):
    """(enabled, capacity_bytes) for the drive's Recycle Bin, from the per-volume setting."""
    guid = _volume_guid(root)
    if not guid:
        return False, 0
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\%s" % guid
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            nuke = winreg.QueryValueEx(k, "NukeOnDelete")[0]
            cap_mb = winreg.QueryValueEx(k, "MaxCapacity")[0]
            return nuke == 0, cap_mb * 1024 * 1024
    except OSError:
        return False, 0          # unknown configuration: treat as unsafe


def _bin_dirs(root):
    base = Path(root) / "$Recycle.Bin"
    if not base.exists():
        return []
    out = []
    try:
        for d in os.scandir(base):
            if d.is_dir(follow_symlinks=False):
                out.append(d.path)
    except OSError:
        pass
    return out


def bin_state(root):
    """(number of items, total bytes) currently in this drive's Recycle Bin that we can see."""
    items = size = 0
    for d in _bin_dirs(root):
        try:
            for e in os.scandir(d):
                if e.name.startswith("$I"):
                    items += 1
                elif e.name.startswith("$R"):
                    try:
                        if e.is_dir(follow_symlinks=False):
                            for dp, _, fns in os.walk(e.path):
                                size += sum(os.path.getsize(os.path.join(dp, f)) for f in fns)
                        else:
                            size += e.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return items, size


def preflight(root, bytes_needed):
    """Raise unless the Recycle Bin is enabled and can hold bytes_needed more. Returns the byte budget left."""
    enabled, cap = bin_settings(root)
    if not enabled:
        raise BinBreaker(f"Recycle Bin is disabled or unknown for {root}: files would be deleted permanently")
    _, used = bin_state(root)
    budget = int(max(0, cap * BIN_HEADROOM - used))
    if bytes_needed > budget:
        raise BinBreaker(f"Recycle Bin on {root} has room for ~{budget / 1e9:.1f} GB "
                         f"(cap {cap / 1e9:.1f} GB, used {used / 1e9:.1f} GB), batch needs {bytes_needed / 1e9:.1f} GB")
    return budget


# ---- previews / index helpers -------------------------------------------------------------------
def thumb_path(path):
    return THUMB_DIR / ("lt_" + hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest()[:20] + ".jpg")


def make_thumb(path):
    out = thumb_path(path)
    if out.exists():
        return
    try:
        from PIL import Image, ImageOps
        THUMB_DIR.mkdir(exist_ok=True)
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((240, 240))
            im.convert("RGB").save(out, "JPEG", quality=80)
    except Exception:
        pass


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def file_crc32(path):
    """CRC32 of the file bytes (8 hex digits): a second, independent checksum next to SHA-256."""
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def still_matches(path, sha, verify, size, mtime):
    if "stat" in verify:      # 'stat' / 'stat+crc': untouched since the index was built (size + mtime)
        st = os.stat(path)
        return st.st_size == size and abs(st.st_mtime - mtime) < 2
    return file_sha256(path) == sha


# ---- index maintenance --------------------------------------------------------------------------
def _removed_paths():
    return set(REMOVED_TXT.read_text(encoding="utf-8").splitlines()) if REMOVED_TXT.exists() else set()


def db_delete(takeout_db, paths, wait=300):
    """Delete index rows. The hash pass keeps a write transaction open almost all the time, so poll for the
    short gap between its commits instead of relying on SQLite's coarse busy timeout. Never raises: paths that
    could not be deleted are remembered in removed-paths.txt and excluded from planning."""
    paths = list(paths)
    if not paths:
        return True
    rw = sqlite3.connect(str(takeout_db), timeout=0, isolation_level=None)
    deadline = time.time() + wait
    try:
        while True:
            try:
                rw.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.005)
        rw.executemany("DELETE FROM f WHERE path=?", [(p,) for p in paths])
        rw.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        with open(REMOVED_TXT, "a", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
        return False
    finally:
        rw.close()


def sync_index(takeout_db):
    """Drop index rows of everything already sent to the Recycle Bin (recycled-log + removed-paths.txt)."""
    gone = logged_paths() | _removed_paths()
    if gone and db_delete(takeout_db, gone):
        if REMOVED_TXT.exists():
            REMOVED_TXT.unlink()
        return len(gone)
    return 0


# ---- duplicate groups ---------------------------------------------------------------------------
def bin_count(root):
    """Number of items in the Recycle Bin (names only: cheap enough to call once per batch)."""
    n = 0
    for d in _bin_dirs(root):
        try:
            n += sum(1 for e in os.scandir(d) if e.name.startswith("$I"))
        except OSError:
            pass
    return n


class BinBudget:
    """Tracks how many more bytes the drive's Recycle Bin can take without purging anything."""
    def __init__(self, root):
        enabled, cap = bin_settings(root)
        if not enabled:
            raise BinBreaker(f"Recycle Bin is disabled or unknown for {root}: files would be deleted permanently")
        _, used = bin_state(root)
        self.root, self.cap, self.used = root, cap, used
        self.remaining = int(max(0, cap * BIN_HEADROOM - used))

    def take(self, n):
        if n > self.remaining:
            raise BinBreaker(f"Recycle Bin on {self.root} has room for ~{self.remaining / 1e9:.1f} GB more "
                             f"(cap {self.cap / 1e9:.1f} GB, used {self.used / 1e9:.1f} GB), batch needs {n / 1e9:.1f} GB")
        self.remaining -= n
        self.used += n


def _plan_group(ro, sha, verify, removed=frozenset()):
    """(keeper_path, [(src, size)], skipped) for one byte-identical group; ValueError if nothing to do."""
    if not SHA_RE.match(sha or ""):
        raise ValueError("bad checksum")
    rows = [r for r in ro.execute("SELECT path,size,mtime FROM f WHERE sha=? ORDER BY mtime, LENGTH(path)", (sha,)).fetchall()
            if r[0] not in removed]
    if len(rows) < 2:
        raise ValueError("this is no longer a duplicate group")
    keeper = rows[0]
    if not os.path.exists(keeper[0]) or not still_matches(keeper[0], sha, verify, keeper[1], keeper[2]):
        raise ValueError("the copy to keep is missing or changed on disk; nothing was removed")
    keeper_crc = file_crc32(keeper[0]) if "crc" in verify else ""   # every duplicate must match this CRC32 too
    ok, skipped = [], []
    for src, size, mtime in rows[1:]:
        try:
            if len(src) > MAX_SAFE_PATH:
                skipped.append([src, "path too long for the Recycle Bin API (left in place)"])
            elif not os.path.exists(src):
                skipped.append([src, "missing"])
            elif not still_matches(src, sha, verify, size, mtime):
                skipped.append([src, "content no longer matches the index"])
            elif keeper_crc and (crc := file_crc32(src)) != keeper_crc:
                skipped.append([src, f"CRC32 {crc} differs from the kept copy {keeper_crc} (left in place)"])
            else:
                ok.append((src, size, keeper_crc))
        except Exception as e:
            skipped.append([src, str(e)])
    return keeper[0], ok, skipped


def trash_groups(takeout_db, shas, verify="hash", budgets=None, chunk=300):
    """Keep the oldest copy of each byte-identical group, send the others to the Recycle Bin in batches.
    Returns {"groups", "moved": [[src, size]], "skipped": [[src, why]], "errors": [[sha, why]], "bytes"}.
    Raises BinBreaker (and stops) if the Recycle Bin ever fails to receive what was removed."""
    ro = sqlite3.connect(f"file:{takeout_db}?mode=ro", uri=True, timeout=60)
    budgets = budgets if budgets is not None else {}
    work, skipped, errors = [], [], []          # work: (src, size, sha, keeper, crc)
    removed = _removed_paths() | logged_paths()
    for sha in shas:
        try:
            keeper, ok, sk = _plan_group(ro, sha, verify, removed)
        except ValueError as e:
            errors.append([sha, str(e)])
            continue
        skipped += sk
        work += [(src, size, sha, keeper, crc) for src, size, crc in ok]
    ro.close()

    moved, total = [], 0
    for i in range(0, len(work), chunk):
        batch = work[i:i + chunk]
        root = _drive_root(batch[0][0])
        if root not in budgets:
            budgets[root] = BinBudget(root)
        budgets[root].take(sum(b[1] for b in batch))
        # no per-file preview: a deleted copy is byte-identical to the kept one, the dashboard shows that one
        before = bin_count(root)
        _shell_recycle([b[0] for b in batch])
        gone = [b for b in batch if not os.path.exists(b[0])]
        gained = bin_count(root) - before
        if gone and gained < len(gone):
            raise BinBreaker(f"{len(gone)} file(s) disappeared but the Recycle Bin gained only {gained} "
                             f"entr{'y' if gained == 1 else 'ies'}: stopped, check the drive")
        now = time.strftime("%F %T")
        with open(RECYCLED_LOG, "a", encoding="utf-8") as lf:
            for src, size, sha, keeper, crc in gone:
                lf.write("\t".join([now, sha, src, keeper, str(size), crc]) + "\n")
        db_delete(takeout_db, [b[0] for b in gone])
        for src, size, sha, keeper, crc in batch:
            if os.path.exists(src):
                skipped.append([src, "could not be sent to the Recycle Bin (left in place)"])
            else:
                moved.append([src, size])
                total += size
    return {"ok": True, "groups": len(shas) - len(errors), "moved": moved, "skipped": skipped,
            "errors": errors, "bytes": total}


def trash_group(takeout_db, sha, verify="hash"):
    """One group (the dashboard button). Same checks as trash_groups."""
    r = trash_groups(takeout_db, [sha], verify=verify)
    if r["errors"]:
        raise ValueError(r["errors"][0][1])
    return {"ok": True, "kept": None, "moved": r["moved"], "skipped": r["skipped"], "bytes": r["bytes"]}


def read_log(offset=0, limit=10):
    """Newest-first entries of recycled-log.tsv as dicts, plus the total."""
    if not RECYCLED_LOG.exists():
        return {"total": 0, "bytes": 0, "entries": []}
    rows = [l.split("\t") for l in RECYCLED_LOG.read_text(encoding="utf-8").splitlines() if l.count("\t") >= 4]
    rows.reverse()
    entries = [{"time": r[0], "sha": r[1], "path": r[2], "kept": r[3], "size": int(r[4] or 0)}
               for r in rows[offset:offset + limit]]
    return {"total": len(rows), "bytes": sum(int(r[4] or 0) for r in rows), "entries": entries}


def read_log_groups(offset=0, limit=10):
    """recycled-log.tsv grouped by checksum, newest group first: what was deleted and which copy was kept."""
    empty = {"total": 0, "files": 0, "bytes": 0, "groups": []}
    if not RECYCLED_LOG.exists():
        return empty
    groups, order = {}, []
    for line in RECYCLED_LOG.read_text(encoding="utf-8").splitlines():
        c = line.split("\t")
        if len(c) < 5:
            continue
        g = groups.get(c[1])
        if g is None:
            g = groups[c[1]] = {"sha": c[1], "time": c[0], "kept": c[3], "files": []}
            order.append(c[1])
        g["time"] = c[0]
        g["files"].append({"path": c[2], "size": int(c[4] or 0)})
    order.sort(key=lambda k: groups[k]["time"], reverse=True)
    allg = [groups[k] for k in order]
    return {"total": len(allg), "files": sum(len(g["files"]) for g in allg),
            "bytes": sum(f["size"] for g in allg for f in g["files"]), "groups": allg[offset:offset + limit]}


def logged_paths():
    if not RECYCLED_LOG.exists():
        return set()
    return {l.split("\t")[2] for l in RECYCLED_LOG.read_text(encoding="utf-8").splitlines() if l.count("\t") >= 4}


if __name__ == "__main__":
    drive = sys.argv[1] if len(sys.argv) > 1 else "C:\\"
    en, cap = bin_settings(drive)
    items, used = bin_state(drive)
    print(f"{drive} Recycle Bin: enabled={en} capacity={cap / 1e9:.1f} GB items={items} used={used / 1e9:.2f} GB")
