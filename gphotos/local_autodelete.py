#!/usr/bin/env python3
"""
Send the duplicates of every byte-identical local Takeout group to the Windows Recycle Bin (oldest copy kept).

  python local_autodelete.py --takeout ../takeout.sqlite                       # dry run: prints the plan only
  python local_autodelete.py --takeout ../takeout.sqlite --go --max-groups 20  # pilot on 20 groups
  python local_autodelete.py --takeout ../takeout.sqlite --go                  # everything

All safety logic lives in localtrash.py: Recycle Bin must be enabled and have room (no purging of older items),
long paths are skipped, each file is checked (size + mtime against the index) before it is touched, and the run
aborts the moment the Recycle Bin does not receive exactly what was removed. Every file is logged to
recycled-log.tsv and listed in the dashboard's Local Takeout > Deleted view.
"""
import argparse, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localtrash as lt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--takeout", default=str(Path(__file__).resolve().parent.parent / "takeout.sqlite"))
    ap.add_argument("--go", action="store_true", help="really send files to the Recycle Bin (default: dry run)")
    ap.add_argument("--max-groups", type=int, default=0, help="stop after this many groups (pilot run)")
    ap.add_argument("--batch", type=int, default=100, help="groups per Recycle Bin batch")
    ap.add_argument("--verify", default="stat+crc", choices=["stat", "stat+crc", "hash"],
                    help="check before a file is touched: stat+crc (default) = size/mtime match the index AND its "
                         "CRC32 equals the kept copy's; stat = size/mtime only; hash = full SHA-256")
    a = ap.parse_args()

    if a.go:
        n = lt.sync_index(a.takeout)
        print(f"index synced: {n:,} already-recycled path(s) dropped from the index")
    db = sqlite3.connect(f"file:{a.takeout}?mode=ro", uri=True, timeout=120)
    groups = db.execute("SELECT sha, COUNT(*), SUM(size) FROM f WHERE sha IS NOT NULL AND sha <> '' "
                        "GROUP BY sha HAVING COUNT(*) > 1 ORDER BY SUM(size) DESC").fetchall()
    if a.max_groups:
        groups = groups[:a.max_groups]
    files = sum(n - 1 for _, n, _ in groups)
    need = int(sum(total - total / n for _, n, total in groups))
    root = lt._drive_root(str(db.execute("SELECT path FROM f LIMIT 1").fetchone()[0]))
    enabled, cap = lt.bin_settings(root)
    _, used = lt.bin_state(root)
    room = max(0, cap * lt.BIN_HEADROOM - used)
    print(f"plan: {len(groups):,} groups, ~{files:,} files, ~{need / 1e9:.1f} GB to the Recycle Bin of {root}")
    print(f"      Recycle Bin: enabled={enabled}, capacity {cap / 1e9:.1f} GB, used {used / 1e9:.1f} GB, "
          f"safe room {room / 1e9:.1f} GB -> {'fits' if enabled and need <= room else 'DOES NOT FIT: will refuse'}")
    if not a.go:
        print("dry run: nothing was changed. Add --go to do it.")
        return 0
    db.close()

    budgets, moved, moved_bytes, skipped, errors, t0 = {}, 0, 0, 0, 0, time.time()
    for i in range(0, len(groups), a.batch):
        shas = [g[0] for g in groups[i:i + a.batch]]
        try:
            r = lt.trash_groups(a.takeout, shas, verify=a.verify, budgets=budgets)
        except lt.BinBreaker as e:
            print(f"\nSTOPPED by a Recycle Bin safety check: {e}")
            print(f"so far: {moved:,} files / {moved_bytes / 1e9:.2f} GB moved. Nothing further was touched.")
            return 2
        moved += len(r["moved"]); moved_bytes += r["bytes"]; skipped += len(r["skipped"]); errors += len(r["errors"])
        print(f"  {min(i + a.batch, len(groups)):,}/{len(groups):,} groups | {moved:,} files, "
              f"{moved_bytes / 1e9:.2f} GB in the bin | skipped {skipped} | stale groups {errors} | {time.time() - t0:.0f}s", flush=True)
    print(f"\ndone: {moved:,} files ({moved_bytes / 1e9:.2f} GB) sent to the Recycle Bin, {skipped} skipped, "
          f"{errors} groups no longer valid. Log: {lt.RECYCLED_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
