#!/usr/bin/env python3
"""
Compare Google Photos (gphotos.sqlite) with local Takeout scan (takeout.sqlite).
Outputs gp_compare.csv with:
  GP_DUP_EXACT      same SHA inside Google Photos
  GP_DUP_PROBABLE   same dims + EXIF signature, different SHA, inside Google Photos
  LOCAL_MATCH_EXACT GP item has byte-identical local file(s)
  LOCAL_MATCH_PROBABLE  GP item matches local file(s) by dims+EXIF only
  GP_ONLY           in Google Photos, no local match
  LOCAL_ONLY        local file, not in Google Photos (summary count + csv rows)

Usage: python gp_compare.py [gphotos.sqlite] [takeout.sqlite] [out.csv]
"""
import csv, sqlite3, sys
from collections import defaultdict

gp_db, lo_db, out = (sys.argv[1:4] + ["gphotos.sqlite", "../takeout.sqlite", "gp_compare.csv"][len(sys.argv[1:4]):])
gp = sqlite3.connect(gp_db).execute(
    "SELECT id,url,filename,size,sha,w,h,dt,make,model,uid FROM gp WHERE status='ok'").fetchall()
lo = sqlite3.connect(lo_db).execute(
    "SELECT path,size,sha,w,h,dt,make,model,uid FROM f").fetchall()


def sig(w, h, dt, make, model, uid):
    return (w, h, dt, make, model, uid) if (w and h and (dt or uid)) else None


lo_sha = defaultdict(list); lo_sig = defaultdict(list)
for r in lo:
    lo_sha[r[2]].append(r)
    s = sig(*r[3:9])
    if s: lo_sig[s].append(r)
gp_sha = defaultdict(list); gp_sig = defaultdict(list)
for r in gp:
    gp_sha[r[4]].append(r)
    s = sig(*r[5:11])
    if s: gp_sig[s].append(r)

rows = []
for k, g in gp_sha.items():
    if len(g) > 1:
        for r in g: rows.append(("GP_DUP_EXACT", k[:12], r[1], r[2], r[3], r[4], ""))
for k, g in gp_sig.items():
    if len({r[4] for r in g}) > 1:
        for r in g: rows.append(("GP_DUP_PROBABLE", str(k)[:60], r[1], r[2], r[3], r[4], ""))
gp_matched_sha = set(); gp_matched_sig = set()
for r in gp:
    if r[4] in lo_sha:
        gp_matched_sha.add(r[4])
        rows.append(("LOCAL_MATCH_EXACT", r[4][:12], r[1], r[2], r[3], r[4], " | ".join(x[0] for x in lo_sha[r[4]])))
    else:
        s = sig(*r[5:11])
        if s and s in lo_sig:
            gp_matched_sig.add(s)
            rows.append(("LOCAL_MATCH_PROBABLE", str(s)[:60], r[1], r[2], r[3], r[4], " | ".join(x[0] for x in lo_sig[s])))
        else:
            rows.append(("GP_ONLY", "", r[1], r[2], r[3], r[4], ""))
gp_shas = set(gp_sha); gp_sigs = set(gp_sig)
n_lo_only = 0
for r in lo:
    if r[2] in gp_shas: continue
    s = sig(*r[3:9])
    if s and s in gp_sigs: continue
    n_lo_only += 1
    rows.append(("LOCAL_ONLY", "", "", r[0], r[1], r[2], ""))

with open(out, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["type", "key", "gp_url", "name_or_local_path", "size", "sha256", "local_paths"])
    w.writerows(rows)
from collections import Counter
print(Counter(r[0] for r in rows), "->", out)
