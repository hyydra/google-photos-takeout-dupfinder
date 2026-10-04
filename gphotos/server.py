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
import argparse, http.server, io, json, os, socketserver, sys, urllib.parse
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "run-log.tsv"
THUMB_DIR = ROOT / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

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

    def log_message(self, format, *args):
        # Suppress routine GET logging to keep terminal clean
        if "/api/data" in (args[0] if args else ""):
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
