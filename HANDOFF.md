# Project Handoff & Operational Guide

## Overview & State of the Project

This repository contains the complete codebase and automation tools for deduplicating large-scale Google Photos cloud libraries and local Google Takeout archives.

### Completed Milestones
1. **Local Takeout Scan (100% Complete)**:
   * Target: `K:\google takeout`
   * Scanned: **233,806 image files** indexed into SQLite database `takeout.sqlite`.
   * Findings: **42,019 EXACT byte-identical duplicate groups** representing **~137.5 GB** of reclaimable storage.
   * Reports generated: `takeout_report.csv` and `takeout_report.json`.
2. **Live Visual Audit Server (`gphotos/server.py`)**:
   * Running on **`http://localhost:8765/`**.
   * Instant dynamic reload on `run-log.tsv` changes.
   * Renders clustered duplicate groups with blue borders (`2px solid #3b82f6`), single green keeper card (`2px solid #10b981`), and red duplicate cards (`2px solid #f43f5e`) with thin SVG vector red $\times$ overlays.
3. **Google Photos Puppeteer Ingestion Engine (`gphotos/gphotos.js`)**:
   * Uses persistent Chrome context in `gphotos/chrome-profile`.
   * Intercepts `Shift+KeyD` downloads into `gphotos/downloads/tmp`.
   * Passes downloaded originals to `gp_ingest.py` for EXIF extraction, SHA-256 computation, and cross-matching against `takeout.sqlite` and `gphotos.sqlite`.
   * Generates $480 \times 480$ high-quality JPEG thumbnails directly from downloaded originals into `gphotos/thumbnails/`.
   * Fast-forwards through already indexed items using `done.txt`.

---

## File and Component Directory

| File / Folder | Purpose |
| :--- | :--- |
| `dupfinder.py` | Local archive scanner, multi-threaded EXIF & SHA-256 extractor, SQLite indexer, duplicate report generator. |
| `gphotos/gphotos.js` | Main browser runner using Puppeteer to navigate Google Photos, download originals, and trash duplicates. |
| `gphotos/gp_ingest.py` | Python ingestion helper for measuring metadata, computing SHA-256, cross-referencing DBs, and saving thumbnails. |
| `gphotos/server.py` | Zero-dependency HTTP server (`http://localhost:8765/`) serving the live dashboard and `/api/data` polling endpoint. |
| `gphotos/build_html_report.py` | Generates standalone and live `report.html` from `run-log.tsv` with clustered duplicate group cards. |
| `gphotos/check_dashboard.js` | Headless Playwright test script to verify dashboard health and render screenshot. |
| `gphotos/run-log.tsv` | Running TSV log of all processed Google Photos items (`id \t action \t sha \t size \t dims \t dt \t make \t model \t gp_dup \t local_dup \t prob`). |
| `gphotos/done.txt` | Set of photo IDs already processed to enable fast-forwarding and resumption. |
| `gphotos/failed.txt` | Error log for photos that timed out or failed to download. |
| `gphotos/gphotos.sqlite` | SQLite database storing all processed Google Photos items and metadata. |
| `takeout.sqlite` | SQLite database storing all 233,806 local Takeout files and metadata. |

---

## Database Schemas

### 1. `takeout.sqlite` (Table `f`)
```sql
CREATE TABLE f (
  path TEXT PRIMARY KEY,
  size INT,
  mtime REAL,
  sha TEXT,
  w INT,
  h INT,
  dt TEXT,
  make TEXT,
  model TEXT,
  uid TEXT
);
CREATE INDEX idx_f_sha ON f(sha);
CREATE INDEX idx_f_meta ON f(w, h, dt, make, model);
```

### 2. `gphotos.sqlite` (Table `gp`)
```sql
CREATE TABLE gp (
  id TEXT PRIMARY KEY,
  url TEXT,
  filename TEXT,
  size INT,
  sha TEXT,
  w INT,
  h INT,
  dt TEXT,
  make TEXT,
  model TEXT,
  uid TEXT,
  status TEXT
);
CREATE INDEX idx_gp_sha ON gp(sha);
CREATE INDEX idx_gp_meta ON gp(w, h, dt, make, model);
```

---

## Operating Procedures & Command Reference

### Starting the Live Dashboard
```powershell
cd L:\google-photos-takeout-dupfinder\gphotos
python server.py --port 8765
```
Dashboard URL: **`http://localhost:8765/`**

### Scanning a Local Archive
```powershell
cd L:\google-photos-takeout-dupfinder
python dupfinder.py scan "K:\google takeout" --db takeout.sqlite --out takeout_report

# Optional: move non-keeper files of EXACT groups (never deletes). --keep: oldest|newest|shortest|largest
python dupfinder.py scan "K:\google takeout" --db takeout.sqlite --out takeout_report --quarantine D:\dupes --keep oldest
```
Name collisions in the quarantine folder get a numeric suffix (`<sha8>_<n>_<name>`).

### Running the Google Photos Scanner

```powershell
cd L:\google-photos-takeout-dupfinder\gphotos

# 1. Batch Test (Scan next 50 items, dry-run without trashing):
node gphotos.js run --limit 50 --takeout ../takeout.sqlite

# 2. Continuous Scan (Dry-run through entire library):
node gphotos.js run --takeout ../takeout.sqlite

# 3. Automated Online Duplicate Trashing:
node gphotos.js run --takeout ../takeout.sqlite --delete-gp-dups
```

### CLI Flags for `gphotos.js`:
* `--limit <N>`: Stop after processing $N$ new items.
* `--takeout <path>`: Path to `takeout.sqlite` for local cross-matching.
* `--delete-gp-dups`: Automatically click trash/delete on verified exact duplicate Google Photos items.
* `--delete-local-dups`: Automatically trash Google Photos items that already exist in local Takeout archive.
* `--keep`: Keep full-resolution downloaded files in `downloads/keep/` instead of deleting after metadata extraction.

---

## Safety & Invariant Guarantees

1. **Exact Matches**: Only images with **byte-identical SHA-256 hashes** are classified as `EXACT` duplicates eligible for automated deletion.
2. **Probable Matches**: Images sharing identical dimensions and EXIF timestamps but differing hashes (e.g. re-compressed web copies or edited versions) are classified as `PROBABLE_REVIEW` and **NEVER auto-deleted**.
3. **Resumption**: If interrupted, running `gphotos.js` reads `done.txt` and automatically fast-forwards to the next unprocessed photo.
4. **Non-destructive Ingestion**: Original downloads are verified before any action is taken.

---

## Environment & Port Conventions
* **Port 8765**: Live Dashboard (`server.py`).
* **Ports 8080 / 8081**: **DO NOT USE** (reserved for `beepet_local` Docker containers on `HOMEPC`).
