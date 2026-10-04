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
    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\r\n').split('\t')
            if not parts or not parts[0]:
                continue
            while len(parts) < 12:
                parts.append('')
            pid, action, sha, sz_str, dims, dt, make, model, gp_dup, local_dup, prob = parts[:11]
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

    def mark_box(it):
        if it['action'] not in ('WOULD_TRASH', 'PROBABLE_REVIEW'):
            return ''
        return (f'<label class="mark-box"><input type="checkbox" class="mark-cb" data-id="{html.escape(it["id"])}" '
                f'data-size="{it["size"]}" onchange="toggleMark(this)"> Mark for deletion</label>')

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
    grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
    gap: 18px;
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
    grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
    gap: 20px;
  }}
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
            {f'<img src="{k_thumb}" alt="Keeper">' if k_thumb else '<div class="thumb-placeholder"><span>&#128247;</span><span>No Preview</span></div>'}
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
              <span class="sha-tag">{k['sha'][:8]}...</span>
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
            {f'<img src="{loc_thumb}" alt="Local Keeper">' if loc_thumb else '<div class="thumb-placeholder"><span>&#128190;</span><span>Local File</span></div>'}
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
            html_content += f"""
        <!-- RED CROSSED DUPLICATE CARD -->
        <div class="card card-duplicate">
          <div class="thumb-wrap">
            {f'<img src="{d_thumb}" alt="Duplicate">' if d_thumb else '<div class="thumb-placeholder"><span>&#128247;</span><span>No Preview</span></div>'}
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
              <span class="sha-tag">{d['sha'][:8]}...</span>
            </div>
          </div>
        </div>"""

        html_content += """
      </div>
    </div>"""

    html_content += """
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
        badge_txt = '&#10006; DELETED' if action == 'TRASH' else ('&#10006; WOULD TRASH' if action == 'WOULD_TRASH' else '&#9888; SUSPECT (REVIEW)' if action == 'PROBABLE_REVIEW' else '&#10004; ORIGINAL KEPT')

        pid = item['id']
        thumb_file = f"thumbnails/{pid}.jpg"
        img_html = f'<img src="{thumb_file}" alt="{html.escape(pid)}" loading="lazy">' if item['has_thumb'] else f'<div class="thumb-placeholder"><span>&#128247;</span><span>{html.escape(item["dims"] or "No Preview")}</span></div>'
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
          <span class="sha-tag">{item['sha'][:8]}...</span>
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
  groupKind = kind || 'all';
  currentView = view;
  sessionStorage.setItem('gp_view', view);
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const groupsView = document.getElementById('duplicateGroupsView');
  const gridView = document.getElementById('standardGrid');

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
        if (data.stats.total !== currentCount) {
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
