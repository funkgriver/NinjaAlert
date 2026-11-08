"""NinjaAlert - diagnostic: how do price returns depend on the time between polls?

The robust engine scales every log return by 1/sqrt(dt_hours) (random-walk
assumption). poe.ninja prices, however, change in discrete steps whenever the
site refreshes its data, so that assumption may not hold. This script checks it
on your own tick history (read-only):

  * share of consecutive tick pairs with an unchanged price (r == 0). A high share
    drags the per-item MAD towards the floor and inflates z on the next real move.
  * per time-gap bucket: median |r| and median |r| / sqrt(dt_h) over non-zero moves.
    If the sqrt(dt) scaling is right, the last column is roughly flat across buckets;
    if it falls strongly with dt, short gaps (e.g. confirmation re-scans) inflate z.

  python tools/diag_returns.py                       # both games, dist/ or $NINJAALERT_DIR
  python tools/diag_returns.py --db path/to/history-poe1.sqlite3
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_GAP_H = 72.0                                   # same as DetectorCfg.max_gap_hours
BUCKETS_MIN = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 180), (180, 1e9)]


def collect(db: Path):
    """Per bucket: list of (|r|, dt_h) for consecutive pairs of the same key."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT key, ts, price FROM ticks ORDER BY key, ts")
        data = [[] for _ in BUCKETS_MIN]
        prev_key, prev_ts, prev_p = None, 0.0, 0.0
        for key, ts, price in cur:
            if key == prev_key and price > 0 and prev_p > 0:
                dt_h = (ts - prev_ts) / 3600.0
                if 0 < dt_h <= MAX_GAP_H:
                    r = abs(math.log(price / prev_p))
                    mins = dt_h * 60.0
                    for i, (lo, hi) in enumerate(BUCKETS_MIN):
                        if lo <= mins < hi:
                            data[i].append((r, max(dt_h, 1.0 / 60.0)))
                            break
            prev_key, prev_ts, prev_p = key, ts, price
        return data
    finally:
        con.close()


def report(db: Path) -> None:
    data = collect(db)
    total = sum(len(b) for b in data)
    print(f"\n{db.name}: {total} consecutive pairs")
    if total == 0:
        return
    zeros = sum(1 for b in data for r, _ in b if r == 0.0)
    print(f"  unchanged-price pairs: {zeros / total:.1%}")
    print(f"  {'gap (min)':>11} {'pairs':>8} {'zero%':>7} {'med|r|':>9} {'med|r| nz':>10} {'med|r|/sqrt(dt)':>16}")
    for (lo, hi), b in zip(BUCKETS_MIN, data):
        if not b:
            continue
        label = f"{lo}-{int(hi)}" if hi < 1e8 else f">{lo}"
        allr = [r for r, _ in b]
        nz = [(r, dt) for r, dt in b if r > 0.0]
        med_all = statistics.median(allr)
        med_nz = statistics.median(r for r, _ in nz) if nz else float("nan")
        scaled = statistics.median(r / math.sqrt(dt) for r, dt in nz) if nz else float("nan")
        print(f"  {label:>11} {len(b):>8} {sum(1 for r in allr if r == 0.0) / len(b):>6.0%} "
              f"{med_all:>9.4f} {med_nz:>10.4f} {scaled:>16.4f}")
    print("  reading: flat last column = sqrt(dt) scaling fits; strongly falling = "
          "short gaps inflate z.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Return vs poll-gap diagnostic (read-only)")
    ap.add_argument("--db", default="", help="history sqlite (default: both games)")
    args = ap.parse_args()
    base = Path(os.environ.get("NINJAALERT_DIR") or (ROOT / "dist"))
    dbs = [Path(args.db)] if args.db else [base / f"history-{g}.sqlite3" for g in ("poe1", "poe2")]
    found = False
    for db in dbs:
        if db.exists():
            found = True
            report(db)
    if not found:
        print("no history database found; pass --db or set NINJAALERT_DIR")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
