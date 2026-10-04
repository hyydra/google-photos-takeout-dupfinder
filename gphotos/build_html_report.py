#!/usr/bin/env python3
"""
Generate an interactive HTML visual grid report showing:
- Duplicate Groups (each with a BLUE BORDER)
- One GREEN Kept Original per group
- RED BORDERED Duplicate cards crossed out with a thin vector red X
- All photos grid with search, filtering, and live 3s auto-refresh
"""
import argparse, html, json, os, sys
from collections import defaultdict
from pathlib import Path
from PIL import Image

REASON_TEXT = {
    'pixels': 'identical pixels, different file',
    'similar': 'near-identical picture (perceptual hash)',
    'exif': 'same size + EXIF, different file',
}


def format_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

def generate_report(log_path, out_path, thumb_dir):
    log_path = Path(log_path)
    out_path = Path(out_path)
    thumb_dir = Path(thumb_dir)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    if not log_path.exists():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return

    items = []
    tp = log_path.parent / 'trashed.txt'
    trashed_ids = set(tp.read_text(encoding='utf-8').split()) if tp.exists() else set()
    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\r\n').split('\t')
            if not parts or not parts[0]:
                continue
            while len(parts) < 13:
                parts.append('')
            pid, action, sha, sz_str, dims, dt, make, model, gp_dup, local_dup, prob = parts[:11]
            if pid in trashed_ids:
                action = 'TRASH'
            try:
                sz = int(sz_str)
            except ValueError:
                sz = 0

            # Check local file thumbnail if matched
            if local_dup and os.path.exists(local_dup):
                loc_thumb = thumb_dir / f"local_{sha[:12]}.jpg"
                if not loc_thumb.exists():
                    try:
                        with Image.open(local_dup) as lim:
                            t = lim.copy()
                            t.thumbnail((360, 360))
                            if t.mode in ("RGBA", "P"):
                                t = t.convert("RGB")
                            t.save(loc_thumb, "JPEG", quality=82)
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
                "reason": parts[11],
                "crc": parts[12],
                "has_thumb": (thumb_dir / f"{pid}.jpg").exists(),
                "has_local_thumb": (thumb_dir / f"local_{sha[:12]}.jpg").exists() if local_dup else False,
            })

    total_scanned = len(items)
    kept_count = sum(1 for x in items if x['action'] == 'KEEP')
    trashed_count = sum(1 for x in items if x['action'] == 'TRASH')
    would_trash_count = sum(1 for x in items if x['action'] == 'WOULD_TRASH')
    prob_count = sum(1 for x in items if x['action'] == 'PROBABLE_REVIEW')
    dups_bytes = sum(x['size'] for x in items if x['action'] in ('TRASH', 'WOULD_TRASH'))

    # Build Duplicate Groups (clustered by SHA-256)
    sha_items = defaultdict(list)
    for it in items:
        sha_items[it['sha']].append(it)

    dup_groups = []
    group_num = 0

    for sha, g_list in sha_items.items():
        has_dup_action = any(x['action'] in ('TRASH', 'WOULD_TRASH') for x in g_list)
        has_local = any(x.get('local_dup') for x in g_list)
        if len(g_list) > 1 or has_dup_action or has_local:
            group_num += 1
            keepers = [x for x in g_list if x['action'] == 'KEEP']
            dups = [x for x in g_list if x['action'] in ('TRASH', 'WOULD_TRASH')]
            if not dups and len(g_list) > 1:
                keepers = g_list[:1]
                dups = g_list[1:]
                for d in dups:
                    d['action'] = 'WOULD_TRASH'

            loc_keeper = None
            for it in g_list:
                if it.get('local_dup') and os.path.exists(it['local_dup']):
                    loc_keeper = it['local_dup']
                    break

            dup_groups.append({
                "num": group_num,
                "kind": "exact",
                "sha": sha,
                "dims": g_list[0]['dims'],
                "camera": g_list[0]['camera'],
                "dt": g_list[0]['dt'],
                "keepers": keepers,
                "local_keeper": loc_keeper,
                "duplicates": dups,
                "reclaimed_bytes": sum(x['size'] for x in dups),
                "reclaimed_fmt": format_size(sum(x['size'] for x in dups)),
            })

    # Suspicious groups: same dimensions + EXIF but different bytes (PROBABLE_REVIEW), clustered with
    # the item they matched. Never auto-trashed; only grouped here so they can be reviewed and marked.
    by_id = {x['id']: x for x in items}
    parent = {}
    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for it in items:
        if it['action'] == 'PROBABLE_REVIEW' and it['probable']:
            ra, rb = find(it['id']), find(it['probable'])
            if ra != rb:
                parent[ra] = rb
    comps = defaultdict(list)
    for node in list(parent):
        comps[find(node)].append(node)
    for nodes in comps.values():
        gp = [by_id[n] for n in nodes if n in by_id]
        local = next((n for n in nodes if n not in by_id and os.path.exists(n)), None)
        if not gp:
            continue
        gp.sort(key=lambda x: -x['size'])  # largest file is the reference
        keepers, dups = ([], gp) if local else (gp[:1], gp[1:] or gp)
        group_num += 1
        dup_groups.append({
            "num": group_num, "kind": "suspicious",
            "reason": next((x['reason'] for x in dups if x.get('reason')), 'exif'),
            "sha": keepers[0]['sha'] if keepers else gp[0]['sha'],
            "dims": gp[0]['dims'], "camera": gp[0]['camera'], "dt": gp[0]['dt'],
            "keepers": keepers, "local_keeper": local, "duplicates": dups,
            "reclaimed_bytes": sum(x['size'] for x in dups),
            "reclaimed_fmt": format_size(sum(x['size'] for x in dups)),
        })
    n_exact = sum(1 for g in dup_groups if g['kind'] == 'exact')
    n_susp = len(dup_groups) - n_exact

    def removable_online(it):
        """True only when ANOTHER online photo stays behind: an online photo that merely matches a local file is
        the only online version and must never be offered for deletion."""
        if it['action'] == 'WOULD_TRASH':
            return bool(it.get('gp_dup_of'))
        if it['action'] == 'PROBABLE_REVIEW':
            return str(it.get('probable', '')).startswith('AF1Q')
        return False

    def mark_box(it):
        if not removable_online(it):
            return ''
        return (f'<label class="mark-box"><input type="checkbox" class="mark-cb" data-id="{html.escape(it["id"])}" '
                f'data-size="{it["size"]}" onchange="toggleMark(this)"> Mark for deletion</label>')

    def del_button(g):
        eligible = [d for d in g['duplicates'] if removable_online(d)]
        if not eligible:
            return ''
        ids = ','.join(html.escape(d['id']) for d in eligible)
        warn = '1' if g['kind'] == 'suspicious' else '0'
        return (f'<button class="del-btn" data-ids="{ids}" data-n="{len(eligible)}" data-warn="{warn}" '
                f'onclick="deleteOnlineGroup(this)">Delete {len(eligible)} duplicate{"s" if len(eligible) != 1 else ""}</button>')

    MAX_GRID = 3000
    items_grid = items[::-1][:MAX_GRID]  # newest first, capped so the page stays usable

    thin_red_x_svg = '''<div class="cross-overlay">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none">
        <line x1="0" y1="0" x2="100" y2="100" stroke="#f43f5e" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round"/>
        <line x1="100" y1="0" x2="0" y2="100" stroke="#f43f5e" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round"/>
      </svg>
    </div>'''

    default_view = 'groups' if dup_groups else 'all'
    grp_active = 'active' if default_view == 'groups' else ''
    all_active = 'active' if default_view == 'all' else ''
    grp_disp = 'block' if default_view == 'groups' else 'none'
    all_disp = 'grid' if default_view == 'all' else 'none'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Google Photos Duplicate Audit Report</title>
<style>
  :root {{
    --bg-main: #0c0d10;
    --bg-surface: #15171e;
    --bg-card: #1c1e27;
    --border: #2b2e3b;
    --text-primary: #f3f4f6;
    --text-secondary: #9ca3af;
    --text-muted: #6b7280;
    --accent-keep: #10b981;
    --accent-trash: #f43f5e;
    --accent-would: #f59e0b;
    --accent-prob: #a855f7;
    --accent-blue: #3b82f6;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg-main);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5;
    padding: 24px;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 1720px; margin: 0 auto; }}

  /* Header */
  header {{
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 26px 32px;
    margin-bottom: 24px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
  }}
  .header-top {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; margin-bottom: 20px; }}
  h1 {{ font-size: 24px; font-weight: 700; letter-spacing: -0.5px; display: flex; align-items: center; gap: 10px; }}
  .tag-badge {{ background: #222634; color: #94a3b8; font-size: 13px; font-weight: 500; padding: 3px 10px; border-radius: 6px; }}

  /* Metric cards */
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }}
  .metric-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
  }}
  .metric-val {{ font-size: 26px; font-weight: 700; line-height: 1.2; margin-top: 4px; }}
  .metric-lbl {{ font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }}

  /* Controls */
  .controls-bar {{
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 24px;
    margin-bottom: 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
  }}
  .filter-tabs {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .tab-btn {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    padding: 8px 16px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .tab-btn:hover {{ background: #262936; color: var(--text-primary); }}
  .tab-btn.active {{ background: #2563eb; border-color: #3b82f6; color: #fff; font-weight: 600; }}
  .tab-count {{
    background: rgba(255,255,255,0.15);
    padding: 2px 7px;
    border-radius: 10px;
    font-size: 12px;
  }}

  .search-box {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-primary);
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 14px;
    width: 280px;
    outline: none;
  }}
  .search-box:focus {{ border-color: var(--accent-blue); }}

  /* ========================================================
     DUPLICATE GROUP STYLING (BLUE BORDER BOX)
     ======================================================== */
  .dup-group {{
    background: var(--bg-surface);
    border: 2px solid #3b82f6; /* Blue border as requested */
    border-radius: 14px;
    padding: 20px 24px;
    margin-bottom: 24px;
    box-shadow: 0 4px 24px rgba(59, 130, 246, 0.12);
  }}
  .dup-group-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 18px;
  }}
  .group-badge-title {{
    background: #1d4ed8;
    color: #fff;
    font-size: 13px;
    font-weight: 700;
    padding: 4px 12px;
    border-radius: 6px;
    letter-spacing: 0.5px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .group-meta {{
    font-size: 13px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }}
  .reclaim-pill {{
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
    border: 1px solid rgba(59, 130, 246, 0.3);
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
  }}

  /* Grid of cards inside group */
  .group-cards-row {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
    gap: 12px;
  }}

  /* Photo Card Base */
  .card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }}
  .card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }}

  /* Keeper Card: GREEN */
  .card.card-keeper {{
    border: 2px solid #10b981; /* Green border for the single keeper */
    box-shadow: 0 0 16px rgba(16, 185, 129, 0.12);
  }}
  /* Duplicate Card: RED */
  .card.card-duplicate {{
    border: 2px solid #f43f5e; /* Red border for duplicate */
    box-shadow: 0 0 16px rgba(244, 63, 94, 0.12);
  }}

  .thumb-wrap {{
    position: relative;
    width: 100%;
    aspect-ratio: 4 / 3;
    background: #000;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .thumb-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.2s ease;
  }}
  .thumb-wrap:hover img {{ transform: scale(1.03); }}
  .card.card-duplicate .thumb-wrap img {{
    filter: brightness(0.82) contrast(0.95);
  }}

  .thumb-placeholder {{
    color: var(--text-muted);
    font-size: 13px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }}

  /* Thin Red X overlay for duplicates */
  .cross-overlay {{
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 5;
  }}
  .cross-overlay svg {{
    width: 100%;
    height: 100%;
    display: block;
    filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.9));
  }}

  .badge {{
    position: absolute;
    top: 10px;
    left: 10px;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    box-shadow: 0 2px 8px rgba(0,0,0,0.6);
    z-index: 6;
  }}
  .badge-keeper {{ background: var(--accent-keep); color: #fff; }}
  .badge-duplicate {{ background: var(--accent-trash); color: #fff; }}
  .badge-would {{ background: var(--accent-would); color: #000; }}

  .card-body {{ padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; flex: 1; }}
  .meta-row {{ display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: var(--text-secondary); }}
  .meta-pill {{
    background: #15171f;
    border: 1px solid var(--border);
    padding: 2px 8px;
    border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 12px;
  }}
  .camera-row {{ font-size: 13px; color: var(--text-primary); font-weight: 500; display: flex; align-items: center; gap: 6px; }}
  .date-row {{ font-size: 12px; color: var(--text-muted); }}

  .card-footer {{
    margin-top: auto;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .btn-link {{
    color: var(--accent-blue);
    text-decoration: none;
    font-size: 13px;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }}
  .btn-link:hover {{ text-decoration: underline; }}
  .sha-tag {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 11px;
    color: var(--text-muted);
    cursor: pointer;
  }}
  .sha-tag:hover {{ color: var(--text-primary); }}

  /* Standard Grid */
  .photo-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
    gap: 12px;
  }}
  /* compact previews */
  .card .card-body {{ padding: 8px 10px; }}
  .card .camera-row, .card .date-row {{ font-size: 11px; }}
  .card .meta-pill {{ font-size: 10px; padding: 1px 6px; }}
  .card .badge {{ font-size: 9px; padding: 2px 6px; }}
  .card .btn-link, .card .sha-tag {{ font-size: 10px; }}
  .dup-group.suspicious {{ border: 2px solid #f59e0b; }}
  .dup-group.suspicious .group-badge-title {{ background: #f59e0b; color: #000; }}
  .mark-box {{ display: flex; align-items: center; gap: 8px; margin-top: 8px; padding: 6px 8px;
    border: 1px solid #f43f5e; border-radius: 8px; color: #fda4af; font-size: 12px; font-weight: 600; cursor: pointer; }}
  .mark-box input {{ accent-color: #f43f5e; width: 16px; height: 16px; }}
  .card.marked {{ outline: 3px solid #f43f5e; outline-offset: 2px; }}
  .card.marked .thumb-wrap img {{ filter: grayscale(0.7) brightness(0.55); }}
  #markBar {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; display: flex; gap: 12px; align-items: center;
    justify-content: center; flex-wrap: wrap; padding: 10px 16px; background: rgba(21,23,30,0.96);
    border-top: 1px solid #2b2e3b; font-size: 13px; color: var(--text-secondary); }}
  #markBar button {{ background: #1c1e27; color: var(--text-primary); border: 1px solid #2b2e3b; border-radius: 8px;
    padding: 6px 12px; font-size: 13px; cursor: pointer; }}
  #markBar button:hover {{ border-color: #f43f5e; }}
  #markBar b {{ color: #f43f5e; font-size: 15px; }}
  .dup-group, #standardGrid .card {{ content-visibility: auto; contain-intrinsic-size: auto 520px; }}
  .del-btn {{ background: #3a1219; color: #fda4af; border: 1px solid #f43f5e; border-radius: 8px; padding: 5px 12px;
    font-size: 12px; font-weight: 700; cursor: pointer; margin-left: 10px; }}
  .del-btn:hover {{ background: #f43f5e; color: #fff; }}
  .del-btn:disabled {{ opacity: 0.5; cursor: default; }}
  #toast {{ position: fixed; left: 50%; bottom: 84px; transform: translateX(-50%); background: #15171e; border: 1px solid #10b981;
    color: #f3f4f6; padding: 10px 16px; border-radius: 10px; font-size: 13px; max-width: 90vw; z-index: 60;
    opacity: 0; pointer-events: none; transition: opacity 0.2s; }}
  #toast.show {{ opacity: 1; }}
  #toast.bad {{ border-color: #f59e0b; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="header-top">
      <div>
        <h1>Google Photos Duplicate Audit Report <span class="tag-badge">Clustered Duplicate Groups</span></h1>
        <p style="color: var(--text-secondary); font-size: 14px; margin-top: 4px;">
          Synchronized inspection with blue-bordered duplicate groups, green keepers, and red crossed duplicates.
        </p>
      </div>
    </div>
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-lbl">Total Scanned</div>
        <div class="metric-val">{total_scanned}</div>
      </div>
      <div class="metric-card">
        <div class="metric-lbl">Duplicate Groups</div>
        <div class="metric-val" style="color: #60a5fa;">{len(dup_groups)}</div>
      </div>
      <div class="metric-card">
        <div class="metric-lbl">Originals Kept</div>
        <div class="metric-val" style="color: var(--accent-keep);">{kept_count}</div>
      </div>
      <div class="metric-card">
        <div class="metric-lbl">Duplicates Trashed</div>
        <div class="metric-val" style="color: var(--accent-trash);">{trashed_count + would_trash_count}</div>
      </div>
      <div class="metric-card">
        <div class="metric-lbl">Duplicate Storage</div>
        <div class="metric-val" style="color: #60a5fa;">{format_size(dups_bytes)}</div>
      </div>
    </div>
  </header>

  <div class="controls-bar">
    <div class="filter-tabs">
      <button class="tab-btn {grp_active}" onclick="switchView('groups', this)">Duplicate Groups <span class="tab-count">{len(dup_groups)}</span></button>
      <button class="tab-btn" onclick="switchView('groups', this, 'exact')">Exact <span class="tab-count">{n_exact}</span></button>
      <button class="tab-btn" onclick="switchView('groups', this, 'suspicious')">Suspicious <span class="tab-count">{n_susp}</span></button>
      <button class="tab-btn" onclick="showLocal(this)">Local Takeout <span class="tab-count" id="localCount">&hellip;</span></button>
      <button class="tab-btn {all_active}" onclick="switchView('all', this)">All Items Grid <span class="tab-count">{total_scanned}</span></button>
      <button class="tab-btn" onclick="filterStandard('trash', this)">Deleted <span class="tab-count">{trashed_count}</span></button>
      <button class="tab-btn" onclick="filterStandard('would', this)">Would Trash <span class="tab-count">{would_trash_count}</span></button>
      <button class="tab-btn" onclick="filterStandard('keep', this)">Kept Originals <span class="tab-count">{kept_count}</span></button>
    </div>
    <div style="display: flex; align-items: center; gap: 14px; flex-wrap: wrap;">
      <div id="liveStatusBadge" style="display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--accent-keep); background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); padding: 4px 10px; border-radius: 20px;">
        <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--accent-keep); box-shadow: 0 0 6px var(--accent-keep);"></span>
        <span style="font-weight:700;">LIVE</span>
        <span id="refreshCountdown" style="color:var(--text-muted); font-size:12px;">3s</span>
      </div>
      <span id="localLive" style="font-size: 13px; color: var(--text-secondary);" title="Local Takeout clean-up, refreshed every 10 seconds"></span>
      <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-secondary); cursor: pointer;">
        <input type="checkbox" id="autoRefreshToggle" checked onchange="toggleAutoRefresh()"> Auto-refresh
      </label>
      <input type="text" id="searchBox" class="search-box" placeholder="Filter by camera, date, SHA..." oninput="handleSearch()">
    </div>
  </div>

  <!-- VIEW 1: DUPLICATE GROUPS (DEFAULT VIEW) -->
  <div id="duplicateGroupsView" style="display: {grp_disp};">"""

    if not dup_groups:
        html_content += """
    <div style="text-align: center; padding: 60px 20px; background: var(--bg-surface); border: 1px dashed var(--border); border-radius: 14px; color: var(--text-muted);">
      <div style="font-size: 40px; margin-bottom: 12px;">&#10004;</div>
      <h3 style="color: var(--text-primary); font-size: 18px;">No duplicate groups identified yet in this batch.</h3>
      <p style="margin-top: 8px; font-size: 14px;">All photos inspected so far have distinct SHA-256 hashes. As the scan advances into duplicates, groups with blue borders will appear here automatically.</p>
    </div>"""

    for g in dup_groups:
        html_content += f"""
    <div class="dup-group {g['kind']}" data-kind="{g['kind']}" data-search="{html.escape(f"{g['sha']} {g['camera']} {g['dt']}".lower())}">
      <div class="dup-group-header">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
          <span class="group-badge-title">{'SUSPICIOUS' if g['kind'] == 'suspicious' else 'DUPLICATE'} GROUP #{g['num']}</span>
          <span class="sha-tag" title="Click to copy full SHA" onclick="navigator.clipboard.writeText('{g['sha']}')">{REASON_TEXT.get(g.get('reason'), 'same size + EXIF, different file') if g['kind'] == 'suspicious' else 'SHA-256: ' + g['sha'][:16] + '...'}</span>
        </div>
        <div class="group-meta">
          <span>{html.escape(g['dims'])}</span> &bull; <span>{html.escape(g['camera'])}</span> &bull; <span>{html.escape(g['dt'])}</span>
          <span class="reclaim-pill">+{g['reclaimed_fmt']} Reclaimed</span>
          {del_button(g)}
        </div>
      </div>
      <div class="group-cards-row">"""

        # RENDER THE ONE GREEN KEEPER CARD
        for k in g['keepers']:
            k_pid = k['id']
            k_thumb = f"thumbnails/{k_pid}.jpg" if k['has_thumb'] else ""
            html_content += f"""
        <!-- GREEN KEEPER CARD -->
        <div class="card card-keeper">
          <div class="thumb-wrap">
            {f'<img src="{k_thumb}" alt="Keeper" loading="lazy" decoding="async">' if k_thumb else '<div class="thumb-placeholder"><span>&#128247;</span><span>No Preview</span></div>'}
            <span class="badge badge-keeper">&#10004; ORIGINAL KEEPER</span>
          </div>
          <div class="card-body">
            <div class="meta-row">
              <span class="meta-pill">{html.escape(k['dims'])}</span>
              <span class="meta-pill">{html.escape(k['size_fmt'])}</span>
            </div>
            <div class="camera-row">&#128247; {html.escape(k['camera'])}</div>
            <div class="date-row">{html.escape(k['dt'])}</div>
            <div class="card-footer">
              <a class="btn-link" href="https://photos.google.com/photo/{k_pid}" target="_blank">&#8599; View Original</a>
              <span class="sha-tag" title="SHA-256 / CRC32">{k['sha'][:8]}...{' crc ' + k['crc'] if k.get('crc') else ''}</span>
            </div>
          </div>
        </div>"""

        # If local keeper exists and no GP keeper
        if not g['keepers'] and g['local_keeper']:
            loc_path = g['local_keeper']
            loc_thumb = f"thumbnails/local_{g['sha'][:12]}.jpg" if (thumb_dir / f"local_{g['sha'][:12]}.jpg").exists() else ""
            html_content += f"""
        <!-- GREEN LOCAL KEEPER CARD -->
        <div class="card card-keeper">
          <div class="thumb-wrap">
            {f'<img src="{loc_thumb}" alt="Local Keeper" loading="lazy" decoding="async">' if loc_thumb else '<div class="thumb-placeholder"><span>&#128190;</span><span>Local File</span></div>'}
            <span class="badge badge-keeper">&#10004; LOCAL KEEPER (TAKEOUT)</span>
          </div>
          <div class="card-body">
            <div class="meta-row">
              <span class="meta-pill">{html.escape(g['dims'])}</span>
              <span class="meta-pill">{html.escape(g['duplicates'][0]['size_fmt'])}</span>
            </div>
            <div class="camera-row" title="{html.escape(loc_path)}">&#128190; {html.escape(os.path.basename(loc_path))}</div>
            <div class="date-row">{html.escape(g['dt'])}</div>
            <div class="card-footer">
              <span style="font-size:12px; color:var(--accent-keep); font-weight:600;">Verified Local Copy</span>
              <span class="sha-tag">{g['sha'][:8]}...</span>
            </div>
          </div>
        </div>"""

        # RENDER THE RED CROSSED DUPLICATE CARDS
        for d in g['duplicates']:
            d_pid = d['id']
            d_thumb = f"thumbnails/{d_pid}.jpg" if d['has_thumb'] else ""
            badge_text = {"TRASH": "DELETED (TRASH)", "PROBABLE_REVIEW": "SUSPECT (REVIEW)"}.get(d['action'], "WOULD TRASH")
            if d['action'] == 'WOULD_TRASH' and d.get('local_dup') and not d.get('gp_dup_of'):
                badge_text = "ALSO IN TAKEOUT"   # byte-identical to a local file, not a duplicate within Google Photos
            html_content += f"""
        <!-- RED CROSSED DUPLICATE CARD -->
        <div class="card card-duplicate">
          <div class="thumb-wrap">
            {f'<img src="{d_thumb}" alt="Duplicate" loading="lazy" decoding="async">' if d_thumb else '<div class="thumb-placeholder"><span>&#128247;</span><span>No Preview</span></div>'}
            {thin_red_x_svg}
            <span class="badge badge-duplicate">&#10006; {badge_text}</span>
          </div>
          <div class="card-body">
            <div class="meta-row">
              <span class="meta-pill">{html.escape(d['dims'])}</span>
              <span class="meta-pill">{html.escape(d['size_fmt'])}</span>
            </div>
            <div class="camera-row">&#128247; {html.escape(d['camera'])}</div>
            <div class="date-row">{html.escape(d['dt'])}</div>
            {mark_box(d)}
            <div class="card-footer">
              <a class="btn-link" href="https://photos.google.com/photo/{d_pid}" target="_blank">&#8599; View in Photos</a>
              <span class="sha-tag" title="SHA-256 / CRC32">{d['sha'][:8]}...{' crc ' + d['crc'] if d.get('crc') else ''}</span>
            </div>
          </div>
        </div>"""

        html_content += """
      </div>
    </div>"""

    html_content += """
  </div>

  <!-- LOCAL TAKEOUT VIEW (loaded on demand from /api/local) -->
  <div id="localView" style="display: none;">
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px;">
      <button class="tab-btn active" id="localKindExact" onclick="setLocalKind('exact')">Exact (same SHA-256)</button>
      <button class="tab-btn" id="localKindPixel" onclick="setLocalKind('pixel')">Same pixels, different file</button>
      <button class="tab-btn" id="localKindRecycled" onclick="setLocalKind('recycled')">Deleted (Recycle Bin)</button>
      <span id="localStats" style="color:var(--text-secondary); font-size:13px;"></span>
    </div>
    <div id="localGroups"></div>
    <div style="text-align:center; margin:18px 0;"><button class="tab-btn" id="localMore" onclick="loadLocal(false)" style="display:none;">Load more groups</button></div>
  </div>

  <!-- VIEW 2: FLAT ALL ITEMS GRID -->
  <div id="standardGrid" class="photo-grid" style="display: {all_disp};">"""

    if len(items) > MAX_GRID:
        html_content += f'<div style="grid-column:1/-1;color:var(--text-muted);font-size:13px;">Showing the newest {MAX_GRID} of {len(items)} scanned photos. Every duplicate and suspicious group is still listed under Duplicate Groups.</div>'
    for item in items_grid:
        action = item['action']
        is_dup = action in ('TRASH', 'WOULD_TRASH', 'PROBABLE_REVIEW')
        card_cls = 'card-duplicate' if is_dup else 'card-keeper'
        badge_cls = 'badge-duplicate' if is_dup else 'badge-keeper'
        also_local = action == 'WOULD_TRASH' and item.get('local_dup') and not item.get('gp_dup_of')
        badge_txt = '&#10006; DELETED' if action == 'TRASH' else ('&#128190; ALSO IN TAKEOUT' if also_local else '&#10006; WOULD TRASH' if action == 'WOULD_TRASH' else '&#9888; SUSPECT (REVIEW)' if action == 'PROBABLE_REVIEW' else '&#10004; ORIGINAL KEPT')

        pid = item['id']
        thumb_file = f"thumbnails/{pid}.jpg"
        img_html = f'<img src="{thumb_file}" alt="{html.escape(pid)}" loading="lazy" decoding="async">' if item['has_thumb'] else f'<div class="thumb-placeholder"><span>&#128247;</span><span>{html.escape(item["dims"] or "No Preview")}</span></div>'
        cross_html = thin_red_x_svg if is_dup else ""

        html_content += f"""
    <div class="card {card_cls}" data-category="{action.lower()}" data-search="{html.escape(f"{pid} {item['camera']} {item['dt']} {item['sha']}".lower())}">
      <div class="thumb-wrap">
        {img_html}
        {cross_html}
        <span class="badge {badge_cls}">{badge_txt}</span>
      </div>
      <div class="card-body">
        <div class="meta-row">
          <span class="meta-pill">{html.escape(item['dims'] or 'N/A')}</span>
          <span class="meta-pill">{html.escape(item['size_fmt'])}</span>
        </div>
        <div class="camera-row">&#128247; {html.escape(item['camera'] or 'Unknown')}</div>
        <div class="date-row">{html.escape(item['dt'] or 'No EXIF Date')}</div>
        {mark_box(item)}
        <div class="card-footer">
          <a class="btn-link" href="https://photos.google.com/photo/{pid}" target="_blank">&#8599; View</a>
          <span class="sha-tag" title="SHA-256 / CRC32">{item['sha'][:8]}...{' crc ' + item['crc'] if item.get('crc') else ''}</span>
        </div>
      </div>
    </div>"""

    html_content += """
  </div>
</div>

<div style="height: 80px;"></div>
<div id="markBar">
  <span><b id="markCount">0</b> marked for deletion &middot; <span id="markSize">0 B</span> &middot; saved to marked.txt, nothing is deleted until the trash step is run</span>
  <button onclick="markVisible(true)">Mark all duplicates in view</button>
  <button onclick="markVisible(false)">Unmark in view</button>
  <button onclick="clearMarks()">Clear all</button>
</div>

<script>
let currentView = sessionStorage.getItem('gp_view') || '""" + default_view + """';
let standardFilter = 'all';
let autoRefreshTimer = null;
let countdown = 3;
let isPolling = true;

window.addEventListener('DOMContentLoaded', () => {
  const savedSearch = sessionStorage.getItem('gp_search') || '';
  if (savedSearch) document.getElementById('searchBox').value = savedSearch;

  const btn = document.querySelector(`.tab-btn[onclick*="'${currentView}'"]`);
  if (btn) switchView(currentView, btn);

  const savedScroll = sessionStorage.getItem('gp_scroll');
  if (savedScroll) window.scrollTo(0, parseInt(savedScroll, 10));

  startAutoRefresh();
});

window.addEventListener('beforeunload', () => {
  sessionStorage.setItem('gp_scroll', window.scrollY);
  sessionStorage.setItem('gp_search', document.getElementById('searchBox').value);
  sessionStorage.setItem('gp_view', currentView);
});

function switchView(view, btn, kind) {
  if (view === 'local') { showLocal(btn); return; }
  groupKind = kind || 'all';
  currentView = view;
  sessionStorage.setItem('gp_view', view);
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const groupsView = document.getElementById('duplicateGroupsView');
  const gridView = document.getElementById('standardGrid');
  document.getElementById('localView').style.display = 'none';

  if (view === 'groups') {
    groupsView.style.display = 'block';
    gridView.style.display = 'none';
    applyGroupFilter();
  } else {
    groupsView.style.display = 'none';
    gridView.style.display = 'grid';
    standardFilter = 'all';
    filterCards();
  }
}

function filterStandard(filter, btn) {
  currentView = 'grid';
  sessionStorage.setItem('gp_view', 'grid');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  document.getElementById('duplicateGroupsView').style.display = 'none';
  document.getElementById('localView').style.display = 'none';
  document.getElementById('standardGrid').style.display = 'grid';
  standardFilter = filter.toLowerCase();
  filterCards();
}

function handleSearch() {
  sessionStorage.setItem('gp_search', document.getElementById('searchBox').value);
  const q = document.getElementById('searchBox').value.trim().toLowerCase();

  if (currentView === 'groups') {
    applyGroupFilter();
  } else {
    filterCards();
  }
}

function filterCards() {
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  document.querySelectorAll('#standardGrid .card').forEach(card => {
    const cat = card.getAttribute('data-category') || '';
    const s = card.getAttribute('data-search') || '';
    const matchesCat = (standardFilter === 'all') || (cat === standardFilter);
    const matchesQ = !q || s.includes(q);
    card.style.display = (matchesCat && matchesQ) ? 'flex' : 'none';
  });
}

function toggleAutoRefresh() {
  isPolling = document.getElementById('autoRefreshToggle').checked;
  const badge = document.getElementById('liveStatusBadge');
  if (isPolling) {
    badge.style.display = 'inline-flex';
    startAutoRefresh();
  } else {
    badge.style.display = 'none';
    if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  countdown = 3;
  autoRefreshTimer = setInterval(async () => {
    if (!isPolling) return;
    countdown--;
    const cdEl = document.getElementById('refreshCountdown');
    if (cdEl) cdEl.innerText = countdown + 's';

    if (countdown <= 0) {
      countdown = 3;
      await checkUpdates();
    }
  }, 1000);
}

async function checkUpdates() {
  try {
    if (location.protocol.startsWith('http')) {
      const res = await fetch('/api/data?t=' + Date.now(), { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        const currentCount = parseInt(document.querySelector('.metric-val').innerText, 10) || 0;
        if (data.stats.total !== currentCount && currentView !== 'local') {
          sessionStorage.setItem('gp_scroll', window.scrollY);
          sessionStorage.setItem('gp_search', document.getElementById('searchBox').value);
          sessionStorage.setItem('gp_view', currentView);
          location.reload();
        }
      }
    } else {
      sessionStorage.setItem('gp_scroll', window.scrollY);
      sessionStorage.setItem('gp_search', document.getElementById('searchBox').value);
      sessionStorage.setItem('gp_view', currentView);
      location.reload();
    }
  } catch (e) {}
}

let groupKind = 'all';
let marks = new Set();
function applyGroupFilter() {
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  document.querySelectorAll('.dup-group').forEach(grp => {
    const okKind = groupKind === 'all' || grp.dataset.kind === groupKind;
    const s = grp.getAttribute('data-search') || '';
    grp.style.display = (okKind && (!q || s.includes(q))) ? 'block' : 'none';
  });
}
function fmtBytes(b) { const u = ['B','KB','MB','GB','TB']; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + ' ' + u[i]; }
function syncMarks() {
  const seen = new Set(); let bytes = 0;
  document.querySelectorAll('.mark-cb').forEach(cb => {
    const on = marks.has(cb.dataset.id);
    cb.checked = on;
    const card = cb.closest('.card'); if (card) card.classList.toggle('marked', on);
    if (on && !seen.has(cb.dataset.id)) { seen.add(cb.dataset.id); bytes += parseInt(cb.dataset.size, 10) || 0; }
  });
  document.getElementById('markCount').textContent = marks.size;
  document.getElementById('markSize').textContent = fmtBytes(bytes);
}
async function postMarks(body) {
  try { await fetch('/api/mark', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); } catch (e) {}
}
function toggleMark(cb) {
  const id = cb.dataset.id;
  if (cb.checked) marks.add(id); else marks.delete(id);
  syncMarks(); postMarks({ ids: [id], marked: cb.checked });
}
function markVisible(on) {
  const ids = [];
  document.querySelectorAll('.mark-cb').forEach(cb => { if (cb.offsetParent !== null) ids.push(cb.dataset.id); });
  ids.forEach(id => on ? marks.add(id) : marks.delete(id));
  syncMarks(); postMarks({ ids, marked: on });
}
function clearMarks() { marks.clear(); syncMarks(); postMarks({ clear: true }); }
async function loadMarks() {
  try { const r = await fetch('/api/marks?t=' + Date.now(), { cache: 'no-store' }); if (r.ok) marks = new Set((await r.json()).ids); } catch (e) {}
  syncMarks();
}
window.addEventListener('DOMContentLoaded', loadMarks);

// ---- Local Takeout view -------------------------------------------------
let localKind = 'exact', localOffset = 0, localTotal = 0, localBusy = false;
const LOCAL_PAGE = 10;
const CROSS = '<div class="cross-overlay"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><line x1="0" y1="0" x2="100" y2="100" stroke="#f43f5e" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round"/><line x1="100" y1="0" x2="0" y2="100" stroke="#f43f5e" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round"/></svg></div>';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function showLocal(btn) {
  currentView = 'local';
  sessionStorage.setItem('gp_view', 'local');
  document.querySelectorAll('.tab-btn').forEach(b => { if (b.id !== 'localKindExact' && b.id !== 'localKindPixel' && b.id !== 'localMore') b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  document.getElementById('duplicateGroupsView').style.display = 'none';
  document.getElementById('standardGrid').style.display = 'none';
  document.getElementById('localView').style.display = 'block';
  if (!document.getElementById('localGroups').children.length) loadLocal(true);
}
function setLocalKind(kind) {
  localKind = kind;
  document.getElementById('localKindExact').classList.toggle('active', kind === 'exact');
  document.getElementById('localKindPixel').classList.toggle('active', kind === 'pixel');
  document.getElementById('localKindRecycled').classList.toggle('active', kind === 'recycled');
  loadLocal(true);
}
function imgFail(img) {
  const d = document.createElement('div');
  d.className = 'thumb-placeholder';
  d.innerHTML = '<span>&#128247;</span><span>No preview</span>';
  img.replaceWith(d);
}
function recycledCard(f, keeper, keptPath) {
  const name = f.path.split(String.fromCharCode(92)).pop().split('/').pop();
  const thumb = '/local-thumb?path=' + encodeURIComponent(keptPath); // identical bytes: the kept copy looks the same
  return '<div class="card ' + (keeper ? 'card-keeper' : 'card-duplicate') + '"><div class="thumb-wrap">'
    + '<img loading="lazy" src="' + thumb + '" onerror="imgFail(this)">' + (keeper ? '' : CROSS)
    + '<span class="badge ' + (keeper ? 'badge-keeper' : 'badge-duplicate') + '">' + (keeper ? '&#10004; KEPT' : '&#10006; IN RECYCLE BIN') + '</span></div>'
    + '<div class="card-body"><div class="meta-row">' + (f.size ? '<span class="meta-pill">' + fmtBytes(f.size) + '</span>' : '') + '</div>'
    + '<div class="camera-row" title="' + esc(f.path) + '">&#128190; ' + esc(name) + '</div>'
    + '<div class="date-row" title="' + esc(f.path) + '" style="font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + esc(f.path) + '</div></div></div>';
}
function localCard(f, keeper) {
  const name = f.path.split(String.fromCharCode(92)).pop().split('/').pop();
  const thumb = '/local-thumb?path=' + encodeURIComponent(f.path);
  const dims = (f.w && f.h) ? f.w + 'x' + f.h : 'N/A';
  return '<div class="card ' + (keeper ? 'card-keeper' : 'card-duplicate') + '"><div class="thumb-wrap">'
    + '<img loading="lazy" src="' + thumb + '" onerror="imgFail(this)">'
    + (keeper ? '' : CROSS)
    + '<span class="badge ' + (keeper ? 'badge-keeper' : 'badge-duplicate') + '">' + (keeper ? '&#10004; KEEP (OLDEST)' : '&#10006; DUPLICATE') + '</span></div>'
    + '<div class="card-body"><div class="meta-row"><span class="meta-pill">' + dims + '</span><span class="meta-pill">' + fmtBytes(f.size || 0) + '</span></div>'
    + '<div class="camera-row" title="' + esc(f.path) + '">&#128190; ' + esc(name) + '</div>'
    + '<div class="date-row">' + esc(f.dt || 'No EXIF Date') + (f.crc ? ' &middot; crc32 ' + esc(f.crc) : '') + '</div>'
    + '<div class="date-row" title="' + esc(f.path) + '" style="font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + esc(f.path) + '</div></div></div>';
}
async function loadLocal(reset) {
  if (localBusy) return;
  localBusy = true;
  const box = document.getElementById('localGroups'), more = document.getElementById('localMore'), stats = document.getElementById('localStats');
  if (reset) { localOffset = 0; box.innerHTML = ''; stats.textContent = 'Loading...'; }
  try {
    const url = localKind === 'recycled' ? '/api/recycled?offset=' + localOffset + '&limit=' + LOCAL_PAGE : '/api/local?kind=' + localKind + '&offset=' + localOffset + '&limit=' + LOCAL_PAGE;
    const r = await fetch(url, { cache: 'no-store' });
    const d = await r.json();
    localTotal = d.total || 0;
    document.getElementById('localCount').textContent = localTotal;
    stats.textContent = localKind === 'recycled' ? (d.files || 0).toLocaleString() + ' duplicate files in ' + localTotal.toLocaleString() + ' groups sent to the Recycle Bin (' + fmtBytes(d.bytes || 0) + ', restorable from the bin)' : (d.files_total || 0).toLocaleString() + ' local files indexed (' + (d.files_hashed || 0).toLocaleString() + ' content-hashed) · '
      + localTotal.toLocaleString() + ' ' + (localKind === 'exact' ? 'exact' : 'same-pixel') + ' groups'
      + (localKind === 'exact' ? ' · ' + fmtBytes(d.reclaim_bytes || 0) + ' reclaimable' : '');
    if (!localTotal && reset) box.innerHTML = '<div style="text-align:center; padding:50px; color:var(--text-muted);">No ' + (localKind === 'exact' ? 'exact' : 'same-pixel') + ' groups found yet.' + (localKind === 'pixel' ? ' Pixel hashes are still being computed for the local archive.' : '') + '</div>';
    (d.groups || []).forEach((g, i) => {
      const num = localOffset + i + 1;
      const files = g.files || [];
      if (localKind === 'recycled') {
        box.insertAdjacentHTML('beforeend',
          '<div class="dup-group exact"><div class="dup-group-header"><div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">'
          + '<span class="group-badge-title">DELETED GROUP #' + num + '</span><span class="sha-tag">SHA-256: ' + esc(String(g.sha).slice(0, 16)) + '...</span></div>'
          + '<div class="group-meta"><span>' + files.length + ' sent to the Recycle Bin</span> &bull; <span>' + esc(g.time) + '</span></div></div>'
          + '<div class="group-cards-row">' + recycledCard({ path: g.kept, size: 0 }, true, g.kept) + files.map(f => recycledCard(f, false, g.kept)).join('') + '</div></div>');
        return;
      }
      box.insertAdjacentHTML('beforeend',
        '<div class="dup-group ' + (localKind === 'exact' ? 'exact' : 'suspicious') + '">'
        + '<div class="dup-group-header"><div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;"><span class="group-badge-title">LOCAL GROUP #' + num + '</span>'
        + '<span class="sha-tag">' + (localKind === 'exact' ? 'SHA-256: ' : 'pixels: ') + esc(String(g.key).slice(0, 16)) + '...</span></div>'
        + '<div class="group-meta"><span>' + g.count + ' files</span> &bull; <span class="reclaim-pill">' + fmtBytes(g.bytes || 0) + ' total</span>'
        + (localKind === 'exact' ? ' <button class="del-btn" data-key="' + esc(g.key) + '" data-n="' + (g.count - 1) + '" onclick="deleteLocalGroup(this)">Delete ' + (g.count - 1) + ' duplicates</button>' : '')
        + '</div></div>'
        + '<div class="group-cards-row">' + files.map((f, j) => localCard(f, j === 0)).join('') + '</div></div>');
    });
    localOffset += (d.groups || []).length;
    more.style.display = localOffset < localTotal ? 'inline-block' : 'none';
    setTimeout(() => {
      const r = more.getBoundingClientRect();
      if (more.style.display !== 'none' && currentView === 'local' && r.top < window.innerHeight + 600) loadLocal(false);
    }, 400);
  } catch (e) {
    stats.textContent = 'Could not load local Takeout data: ' + e;
  }
  localBusy = false;
}
async function refreshLocalLive() {
  try {
    const [a, b] = await Promise.all([
      fetch('/api/recycled?limit=1', { cache: 'no-store' }).then(r => r.json()),
      fetch('/api/local?kind=exact&offset=0&limit=1', { cache: 'no-store' }).then(r => r.json()),
    ]);
    const el = document.getElementById('localLive');
    if (el) el.textContent = 'Local: ' + (a.files || 0).toLocaleString() + ' duplicates in the Recycle Bin (' + fmtBytes(a.bytes || 0) + ') · ' + (b.total || 0).toLocaleString() + ' exact groups left · ' + (b.files_total || 0).toLocaleString() + ' files indexed';
    const c = document.getElementById('localCount');
    if (c && !localBusy) c.textContent = b.total || 0;
  } catch (e) {}
}
setInterval(refreshLocalLive, 10000);
window.addEventListener('DOMContentLoaded', refreshLocalLive);
const NL = String.fromCharCode(10);
function toast(msg, bad) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = bad ? 'bad show' : 'show';
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 9000);
}
async function postDelete(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'dupfinder' }, body: JSON.stringify(body) });
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}
async function deleteLocalGroup(btn) {
  const n = btn.dataset.n;
  const msg = 'Move ' + n + ' duplicate file' + (n === '1' ? '' : 's') + ' of this group to the takeout-dupes folder on the same drive?' + NL + NL
    + 'The oldest copy is kept. Each file is re-checked by SHA-256 first, nothing is permanently deleted, and every move is logged in quarantine-log.tsv.';
  if (!window.confirm(msg)) return;
  btn.disabled = true;
  btn.textContent = 'Moving...';
  try {
    const d = await postDelete('/api/delete-local', { sha: btn.dataset.key });
    toast('Moved ' + d.moved.length + ' file(s) to takeout-dupes' + (d.skipped.length ? ', skipped ' + d.skipped.length + ' (changed or missing)' : '') + '.', d.skipped.length > 0);
    if (d.skipped.length === 0 && d.moved.length > 0) {
      const grp = btn.closest('.dup-group');
      if (grp) grp.remove();
      localTotal = Math.max(0, localTotal - 1);
      document.getElementById('localCount').textContent = localTotal;
    } else {
      loadLocal(true); // something was skipped: show the group as it really is now
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Delete ' + n + ' duplicates';
    toast('Not deleted: ' + e.message, true);
  }
}
async function deleteOnlineGroup(btn) {
  const ids = btn.dataset.ids.split(',').filter(Boolean);
  let msg = 'Move ' + ids.length + ' photo' + (ids.length === 1 ? '' : 's') + ' of this group to the Google Photos trash (recoverable for 60 days)?';
  if (btn.dataset.warn === '1') msg += NL + NL + 'WARNING: these are suspects, not byte-identical copies. Check them against the reference photo first.';
  if (!window.confirm(msg)) return;
  btn.disabled = true;
  try {
    const d = await postDelete('/api/delete-online', { ids });
    btn.textContent = d.started ? 'Trashing...' : 'Queued';
    toast(d.message, !d.started);
    loadMarks();
  } catch (e) {
    btn.disabled = false;
    toast('Not deleted: ' + e.message, true);
  }
}
const localObserver = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && currentView === 'local') loadLocal(false); }, { rootMargin: '600px' });
window.addEventListener('DOMContentLoaded', () => { const m = document.getElementById('localMore'); if (m) localObserver.observe(m); });
window.addEventListener('DOMContentLoaded', () => { fetch('/api/local?kind=exact&offset=0&limit=1').then(r => r.json()).then(d => { document.getElementById('localCount').textContent = d.total || 0; }).catch(() => {}); });
</script>
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f"HTML report successfully generated: {out_path} ({len(items)} items, {len(dup_groups)} duplicate groups)")

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--log', default='run-log.tsv')
    p.add_argument('--out', default='report.html')
    p.add_argument('--thumbs', default='thumbnails')
    args = p.parse_args()
    generate_report(args.log, args.out, args.thumbs)
