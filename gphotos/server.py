#!/usr/bin/env python3
"""
Localhost live dashboard server for Google Photos duplicate finder.
Zero-dependency (Python standard library http.server).

Serves:
  http://localhost:8765/           -> Live interactive dashboard
  http://localhost:8765/api/data   -> Live JSON data for auto-refresh
  http://localhost:8765/thumbnails -> Thumbnail images (saved or generated on the fly)

Usage:
  python server.py [--port 8765]
"""
import argparse, hashlib, http.server, io, json, os, re, shutil, socketserver, sqlite3, subprocess, sys, threading, time, urllib.parse
from pathlib import Path
from PIL import Image, ImageOps
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "run-log.tsv"
THUMB_DIR = ROOT / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

TAKEOUT_DB = Path(os.environ.get("TAKEOUT_DB") or ROOT.parent / "takeout.sqlite")  # local Takeout index (dupfinder.py)
_local_cache = {}

def takeout_conn():
    if not TAKEOUT_DB.exists():
        return None
    return sqlite3.connect(f"file:{TAKEOUT_DB}?mode=ro", uri=True, timeout=30)

def local_groups(kind, offset, limit):
    """Duplicate groups among the local Takeout files: 'exact' (same SHA-256) or 'pixel' (same decoded pixels)."""
    db = takeout_conn()
    empty = {"files_total": 0, "files_hashed": 0, "total": 0, "reclaim_bytes": 0, "groups": []}
    if not db:
        return empty
    key, having = ("sha", "COUNT(*) > 1") if kind == "exact" else ("pix", "COUNT(DISTINCT sha) > 1")
    cached = _local_cache.get(kind)
    if not cached or time.time() - cached[0] > 120:
        rows = db.execute(f"SELECT {key}, COUNT(*), SUM(size) FROM f WHERE {key} IS NOT NULL AND {key} <> '' "
                          f"GROUP BY {key} HAVING {having} ORDER BY COUNT(*) DESC, SUM(size) DESC").fetchall()
        files_total = db.execute("SELECT COUNT(*) FROM f").fetchone()[0]
        hashed = db.execute("SELECT COUNT(*) FROM f WHERE pix IS NOT NULL").fetchone()[0]
        reclaim = int(sum(total - total / n for _, n, total in rows)) if kind == "exact" else 0
        cached = _local_cache[kind] = (time.time(), rows, files_total, hashed, reclaim)
    _, rows, files_total, hashed, reclaim = cached
    cols = ("path", "size", "w", "h", "dt", "make", "model", "mtime")
    groups = []
    for k, n, total in rows[offset:offset + limit]:
        files = db.execute(f"SELECT {','.join(cols)} FROM f WHERE {key}=? ORDER BY mtime, LENGTH(path)", (k,)).fetchall()
        groups.append({"key": k, "count": n, "bytes": total, "files": [dict(zip(cols, r)) for r in files]})
    return {"files_total": files_total, "files_hashed": hashed, "total": len(rows), "reclaim_bytes": reclaim, "groups": groups}

def local_thumb(path):
    """Thumbnail for a local Takeout file; only files listed in the Takeout index are served."""
    db = takeout_conn()
    if not db or not db.execute("SELECT 1 FROM f WHERE path=?", (path,)).fetchone():
        return None
    out = THUMB_DIR / ("lt_" + hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest()[:20] + ".jpg")
    if not out.exists():
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((360, 360))
                im.convert("RGB").save(out, "JPEG", quality=80)
        except Exception:
            return None
    return out

DB_LOCK = threading.Lock()
QUARANTINE_LOG = ROOT / "quarantine-log.tsv"
SHA_RE = re.compile(r'^[0-9a-f]{64}$')

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

def delete_local_group(sha):
    """'Delete' the duplicates of one byte-identical local group: keep the oldest copy and MOVE the others
    into <drive>:/takeout-dupes (same drive, reversible, logged). Every file is re-verified by SHA-256
    right before it is moved; anything that no longer matches the index is left alone."""
    if not SHA_RE.match(sha or ""):
        raise ValueError("bad checksum")
    with DB_LOCK:
        ro = takeout_conn()
        if not ro:
            raise ValueError("takeout index not found")
        rows = ro.execute("SELECT path FROM f WHERE sha=? ORDER BY mtime, LENGTH(path)", (sha,)).fetchall()
        paths = [r[0] for r in rows]
        if len(paths) < 2:
            raise ValueError("this is no longer a duplicate group")
        keeper = paths[0]
        if not os.path.exists(keeper) or file_sha256(keeper) != sha:
            raise ValueError("the copy to keep is missing or changed on disk; nothing was moved")
        moved, skipped = [], []
        for src in paths[1:]:
            try:
                if not os.path.exists(src):
                    skipped.append([src, "missing"])
                    continue
                if file_sha256(src) != sha:
                    skipped.append([src, "content no longer matches the index"])
                    continue
                qdir = Path(os.environ.get("QUARANTINE_DIR") or (os.path.splitdrive(src)[0] + os.sep + "takeout-dupes"))
                qdir.mkdir(parents=True, exist_ok=True)
                dst = qdir / f"{sha[:8]}_{Path(src).name}"
                n = 1
                while dst.exists():
                    dst = qdir / f"{sha[:8]}_{n}_{Path(src).name}"
                    n += 1
                shutil.move(src, str(dst))
                with open(QUARANTINE_LOG, "a", encoding="utf-8") as lf:
                    lf.write(f"{time.strftime('%F %T')}\t{sha}\t{src}\t{dst}\t{keeper}\n")
                moved.append([src, str(dst)])
            except Exception as e:
                skipped.append([src, str(e)])
        if moved:
            rw = sqlite3.connect(str(TAKEOUT_DB), timeout=60)
            rw.executemany("DELETE FROM f WHERE path=?", [(m[0],) for m in moved])
            rw.commit()
            rw.close()
            _local_cache.clear()
    return {"ok": True, "kept": keeper, "moved": moved, "skipped": skipped}

def scan_running():
    """True while a `gphotos.js run` scan is alive (it owns the Chrome profile)."""
    cmd = ("(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object "
           "{ $_.CommandLine -like '*gphotos.js*' -and $_.CommandLine -like '* run*' } | Measure-Object).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30).stdout
    return (out.strip() or "0") != "0"

def delete_online(ids):
    """Queue photos for the Google Photos trash (marked.txt). If no scan is running, start the confirm-gated
    trash step for exactly these ids right away; otherwise they wait for the next `gphotos.js trash`."""
    ids = [i for i in ids if isinstance(i, str) and ID_RE.match(i)]
    status = {}
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            c = line.split("\t")
            if len(c) > 1:
                status[c[0]] = c[1]
    eligible = [i for i in ids if status.get(i) in ("WOULD_TRASH", "PROBABLE_REVIEW")]
    if not eligible:
        raise ValueError("none of these photos is a logged duplicate or suspect")
    with MARKS_LOCK:
        write_marks(read_marks() | set(eligible))
    if scan_running():
        return {"ok": True, "queued": len(eligible), "started": False,
                "message": "A scan is running and holds the Chrome profile, so these photos were queued. "
                           "Stop the scan, then run: node gphotos.js trash --confirm <count>"}
    cmd = ["node", "gphotos.js", "trash", "--ids", ",".join(eligible), "--confirm", str(len(eligible)), "--headless"]
    subprocess.Popen(cmd, cwd=str(ROOT), stdout=open(ROOT / "trash-run.log", "a"), stderr=subprocess.STDOUT)
    return {"ok": True, "queued": len(eligible), "started": True,
            "message": f"Moving {len(eligible)} photo(s) to the Google Photos trash in the background (see trash-run.log)."}

MARKS_FILE = ROOT / "marked.txt"   # photo ids the user marked for deletion in the dashboard
MARKS_LOCK = threading.Lock()
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,200}$')

def read_marks():
    if not MARKS_FILE.exists():
        return set()
    return {l.strip() for l in MARKS_FILE.read_text(encoding='utf-8').splitlines() if l.strip()}

def write_marks(marks):
    tmp = MARKS_FILE.with_suffix('.tmp')
    tmp.write_text("\n".join(sorted(marks)) + ("\n" if marks else ""), encoding='utf-8')
    os.replace(tmp, MARKS_FILE)

def format_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

def get_live_data():
    if not LOG_FILE.exists():
        return {
            "items": [],
            "stats": {"total": 0, "kept": 0, "trashed": 0, "would_trash": 0, "probable": 0, "reclaimed_bytes": 0, "reclaimed_fmt": "0 MB"}
        }

    items = []
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\r\n').split('\t')
            if not parts or not parts[0]:
                continue
            while len(parts) < 11:
                parts.append('')
            pid, action, sha, sz_str, dims, dt, make, model, gp_dup, local_dup, prob = parts[:11]
            try:
                sz = int(sz_str)
            except ValueError:
                sz = 0

            # Generate local thumbnail on-the-fly if needed
            if local_dup and os.path.exists(local_dup):
                loc_thumb = THUMB_DIR / f"local_{sha[:12]}.jpg"
                if not loc_thumb.exists():
                    try:
                        with Image.open(local_dup) as lim:
                            t = lim.copy()
                            t.thumbnail((360, 360))
                            if t.mode in ("RGBA", "P"):
                                t = t.convert("RGB")
                            t.save(loc_thumb, "JPEG", quality=80)
                    except Exception:
                        pass

            items.append({
                "id": pid,
                "action": action,
                "sha": sha,
                "size": sz,
                "size_fmt": format_size(sz),
                "dims": dims,
                "dt": dt,
                "camera": f"{make} {model}".strip(),
                "gp_dup_of": gp_dup,
                "local_dup": local_dup,
                "probable": prob,
                "has_thumb": (THUMB_DIR / f"{pid}.jpg").exists(),
                "local_thumb": f"/thumbnails/local_{sha[:12]}.jpg" if (THUMB_DIR / f"local_{sha[:12]}.jpg").exists() else None,
            })

    total = len(items)
    kept = sum(1 for x in items if x['action'] == 'KEEP')
    trashed = sum(1 for x in items if x['action'] == 'TRASH')
    would_trash = sum(1 for x in items if x['action'] == 'WOULD_TRASH')
    probable = sum(1 for x in items if x['action'] == 'PROBABLE_REVIEW')
    reclaimed = sum(x['size'] for x in items if x['action'] in ('TRASH', 'WOULD_TRASH'))

    return {
        "items": items,
        "stats": {
            "total": total,
            "kept": kept,
            "trashed": trashed,
            "would_trash": would_trash,
            "probable": probable,
            "reclaimed_bytes": reclaimed,
            "reclaimed_fmt": format_size(reclaimed),
        }
    }

class ThreadingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

class LiveHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def address_string(self):
        # Disable slow reverse DNS lookups on localhost in Windows
        return self.client_address[0]

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/', '/index.html'):
            try:
                # Regenerate report.html if run-log.tsv was modified after report.html
                report_file = ROOT / "report.html"
                log_file = ROOT / "run-log.tsv"
                if log_file.exists():
                    if not report_file.exists() or log_file.stat().st_mtime > report_file.stat().st_mtime:
                        try:
                            from build_html_report import generate_report
                            generate_report(log_file, report_file, ROOT / "thumbnails")
                        except Exception as ge:
                            print(f"[WARN] Error regenerating report: {ge}")

                with open(report_file, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return
        elif parsed.path == '/api/local':
            q = urllib.parse.parse_qs(parsed.query)
            kind = 'pixel' if q.get('kind', ['exact'])[0] == 'pixel' else 'exact'
            try:
                offset = max(0, int(q.get('offset', ['0'])[0]))
                limit = min(50, max(1, int(q.get('limit', ['10'])[0])))
                self.send_json(local_groups(kind, offset, limit))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return
        elif parsed.path == '/local-thumb':
            thumb = local_thumb(urllib.parse.parse_qs(parsed.query).get('path', [''])[0])
            if not thumb:
                self.send_error(404)
                return
            data = thumb.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(data)
            return
        elif parsed.path == '/api/marks':
            self.send_json({"ids": sorted(read_marks())})
            return
        elif parsed.path == '/api/data':
            try:
                data = get_live_data()
                payload = json.dumps(data).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                self.send_error(500, str(e))
            return
        super().do_GET()

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(payload)

    def same_origin(self):
        """Only the dashboard page itself may POST: blocks other websites from driving localhost."""
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host', ''):
            return False
        return self.headers.get('X-Requested-With') == 'dupfinder'

    def do_POST(self):
        # POST /api/mark  {"ids": [...], "marked": true|false}  or  {"clear": true}
        # POST /api/delete-local {"sha": ...}   POST /api/delete-online {"ids": [...]}
        path = urllib.parse.urlparse(self.path).path
        if path in ('/api/delete-local', '/api/delete-online'):
            if not self.same_origin():
                self.send_json({"ok": False, "error": "forbidden"}, 403)
                return
            try:
                n = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(min(n, 1_000_000)) or b'{}')
                if path == '/api/delete-local':
                    self.send_json(delete_local_group(body.get('sha', '')))
                else:
                    self.send_json(delete_online(body.get('ids', [])))
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return
        if path != '/api/mark':
            self.send_error(404)
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            if n > 5_000_000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(n) or b'{}')
            with MARKS_LOCK:
                marks = read_marks()
                if body.get('clear'):
                    marks.clear()
                else:
                    ids = {i for i in body.get('ids', []) if isinstance(i, str) and ID_RE.match(i)}
                    marks = (marks | ids) if body.get('marked') else (marks - ids)
                write_marks(marks)
            self.send_json({"ok": True, "count": len(marks)})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 400)

    def log_message(self, format, *args):
        # Suppress routine GET logging to keep terminal clean
        if "/api/data" in str(args[0] if args else ""):
            return
        super().log_message(format, *args)

def kill_port_owner(port):
    import subprocess
    try:
        out = subprocess.check_output(f'netstat -ano -p tcp | findstr :{port}', shell=True, text=True)
        my_pid = os.getpid()
        for line in out.strip().splitlines():
            cols = line.split()
            # cols: Proto, Local Address, Foreign Address, State, PID
            if len(cols) >= 5 and cols[3] == 'LISTENING' and cols[1].endswith(f':{port}'):
                pid = int(cols[-1])
                if pid != my_pid and pid > 0:
                    print(f"[*] Reclaiming port {port}: terminating old process PID {pid}...")
                    subprocess.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True)
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    kill_port_owner(args.port)

    with ThreadingServer(("127.0.0.1", args.port), LiveHandler) as httpd:
        print(f"\n[OK] Live Duplicate Audit Server running at:")
        print(f"     http://localhost:{args.port}/\n")
        print(f"Press Ctrl+C to stop the server.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    main()
