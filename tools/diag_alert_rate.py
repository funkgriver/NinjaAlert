"""NinjaAlert - diagnostic: how many alerts would the robust engine raise BEFORE the
AlertGovernor, replayed over your stored tick history (read-only)?

Why: the governor (token bucket + cooldowns + digest) hides how noisy the raw
detector is. A MAD-based z-score with a threshold near 3 is tuned for roughly
Gaussian returns; market returns are heavy-tailed, so the raw false-alert rate can
be many times higher than the Gaussian expectation. This tool measures it.

  python tools/diag_alert_rate.py                    # both games, dist/ or $NINJAALERT_DIR
  python tools/diag_alert_rate.py --db path/to/history-poe1.sqlite3 --skip-hours 48

Output: raw alerts per day, alerts per 1000 evaluated ticks, split by category,
league phase, label and |z| bucket. The first --skip-hours of history are used only
to build baselines (not counted).
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from advanced_analyzer import AnomalyAnalyzer      # noqa: E402  (stdlib-only module)
from appconfig import DetectorCfg                   # noqa: E402
from models import ItemTick, Snapshot               # noqa: E402


def snapshots(db: Path):
    """Yield one Snapshot per (ts, category) group, in time order, streamed."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT key, name, category, price, volume, listings, aux, ts "
                          "FROM ticks ORDER BY ts, category")
        group, gk = [], None
        for row in cur:
            k = (row[7], row[2])
            if gk is not None and k != gk:
                yield Snapshot(gk[1].split(":", 1)[0], gk[1], "", gk[0], "documented", tuple(group))
                group = []
            gk = k
            group.append(ItemTick(*row))
        if group:
            yield Snapshot(gk[1].split(":", 1)[0], gk[1], "", gk[0], "documented", tuple(group))
    finally:
        con.close()


def z_bucket(z: float) -> str:
    a = abs(z)
    return "<3.5" if a < 3.5 else "3.5-4.5" if a < 4.5 else "4.5-6" if a < 6 else ">=6"


def report(db: Path, skip_hours: float, league_start: str) -> None:
    cfg = DetectorCfg()
    an = AnomalyAnalyzer(cfg, lambda g: league_start)
    first_ts = last_ts = None
    ticks_counted = 0
    alerts = Counter(), Counter(), Counter(), Counter()   # category, phase, label, z bucket
    n_alerts = 0
    z_stars = []
    for snap in snapshots(db):
        if first_ts is None:
            first_ts = snap.ts
        last_ts = snap.ts
        out = an.process(snap)
        if snap.ts - first_ts < skip_hours * 3600.0:
            continue
        ticks_counted += len(snap.ticks)
        for a in out:
            n_alerts += 1
            alerts[0][a.category] += 1
            alerts[1][a.phase] += 1
            alerts[2][a.label] += 1
            alerts[3][z_bucket(a.z_adj)] += 1
            z_stars.append(a.z_star)
    print(f"\n{db.name}")
    if first_ts is None or last_ts is None or ticks_counted == 0:
        print("  not enough history after the warm-up window")
        return
    days = max((last_ts - first_ts - skip_hours * 3600.0) / 86400.0, 1e-9)
    z_star = sum(z_stars) / len(z_stars) if z_stars else float("nan")
    gauss = math.erfc(z_star / math.sqrt(2.0)) if z_stars else float("nan")
    print(f"  evaluated ticks: {ticks_counted}   span: {days:.1f} days")
    print(f"  raw alerts (pre-governor): {n_alerts}  ->  {n_alerts / days:.1f} per day, "
          f"{1000.0 * n_alerts / ticks_counted:.2f} per 1000 ticks")
    if z_stars:
        print(f"  mean z* = {z_star:.2f}; a Gaussian model would give "
              f"{1000.0 * gauss:.2f} per 1000 ticks (before the min-move gate)")
    for title, c in zip(("categories", "phases", "labels", "|z| buckets"), alerts):
        if c:
            print(f"  {title:<11}: " + ", ".join(f"{k}={v}" for k, v in c.most_common(8)))
    print("  reading: raw rate far above the Gaussian figure = heavy tails; the governor is "
          "then doing the noise filtering, not the detector.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Raw (pre-governor) alert-rate replay, read-only")
    ap.add_argument("--db", default="", help="history sqlite (default: both games)")
    ap.add_argument("--skip-hours", type=float, default=36.0, help="warm-up hours not counted")
    ap.add_argument("--league-start", default="", help="YYYY-MM-DD (empty = default league day)")
    args = ap.parse_args()
    base = Path(os.environ.get("NINJAALERT_DIR") or (ROOT / "dist"))
    dbs = [Path(args.db)] if args.db else [base / f"history-{g}.sqlite3" for g in ("poe1", "poe2")]
    found = False
    for db in dbs:
        if db.exists():
            found = True
            report(db, args.skip_hours, args.league_start)
    if not found:
        print("no history database found; pass --db or set NINJAALERT_DIR")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
