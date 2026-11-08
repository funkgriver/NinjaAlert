"""NinjaAlert - alert-outcome backtest.

Reads the alert journal (alerts.jsonl) and, for every alert, looks up the
item's price in the tick history (history-*.sqlite3, read-only) after
+2 h, +6 h and +24 h. For each horizon it computes the *retained move share*

    s(H) = (p(H) - prev_price) / (price - prev_price)

i.e. which fraction of the alerted move was still in place H hours later
(0 = fully reverted, 1 = move held, >1 = moved further). Confirmed means
s >= 0.5 (default, --confirm-ratio).

Output: confirmation tables by category, league phase and label, plus the
overall per-horizon picture. Runs without arguments using the app layout
(NINJAALERT_DIR env override, else <repo>/dist).

  python tools/backtest.py                # text tables
  python tools/backtest.py --json         # machine-readable
  python tools/backtest.py --journal path/alerts.jsonl --db-dir path
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

HORIZONS_H = (2.0, 6.0, 24.0)


def load_journal(path: Path) -> Tuple[List[dict], int]:
    rows, skipped = [], 0
    if not path.exists():
        return rows, skipped
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key") and rec.get("prev_price") and rec.get("price"):
                rows.append(rec)
            else:
                skipped += 1
    return rows, skipped


def load_prices(db: Path, keys: set) -> Dict[str, List[Tuple[float, float]]]:
    """key -> [(ts, price), ...] ts-ascending, only for the requested keys."""
    out: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    if not db.exists() or not keys:
        return out
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT key, ts, price FROM ticks WHERE price > 0 ORDER BY key, ts")
        for key, ts, price in cur:
            if key in keys:
                out[key].append((ts, price))
    finally:
        con.close()
    return out


def price_after(series: List[Tuple[float, float]], ts: float) -> Optional[float]:
    """First recorded price at or after ts (None if the item left the history)."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    return series[lo][1] if lo < len(series) else None


def retained_share(rec: dict, p_h: Optional[float]) -> Optional[float]:
    if p_h is None:
        return None
    move = rec["price"] - rec["prev_price"]
    if abs(move) < 1e-12:
        return None
    return (p_h - rec["prev_price"]) / move


def group_stats(rows: List[dict]) -> dict:
    """Per-group confirmation stats: {2.0: [shares], 6.0: [...], 24.0: [...]}."""
    out = {}
    for h in HORIZONS_H:
        shares = [s for s in (r["shares"].get(h) for r in rows) if s is not None]
        confirmed = sum(1 for s in shares if s >= 0.5)
        reverted = sum(1 for s in shares if s <= 0.0)
        out[h] = {
            "n": len(shares),
            "missing": len(rows) - len(shares),
            "confirmed": confirmed,
            "confirm_pct": round(100.0 * confirmed / len(shares), 1) if shares else None,
            "reverted": reverted,
            "median_share": round(statistics.median(shares), 3) if shares else None,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default="",
                    help="alerts.jsonl path (default: <base>/alerts.jsonl)")
    ap.add_argument("--db-dir", default="",
                    help="history DB directory (default: <base>)")
    ap.add_argument("--confirm-ratio", type=float, default=0.5,
                    help="retained share that counts as confirmed (default 0.5)")
    ap.add_argument("--min-group", type=int, default=3,
                    help="min alerts to list a category/phase/label row (default 3)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = Path(os.environ.get("NINJAALERT_DIR") or (ROOT / "dist"))
    journal = Path(args.journal) if args.journal else base / "alerts.jsonl"
    alerts, skipped = load_journal(journal)
    if not alerts:
        print(f"no usable alerts in {journal} (skipped {skipped} records without key)")
        return 1

    prices = {}
    for game in ("poe1", "poe2"):
        db = Path(args.db_dir) / f"history-{game}.sqlite3" if args.db_dir \
            else base / f"history-{game}.sqlite3"
        prices.update(load_prices(db, {a["key"] for a in alerts}))

    for a in alerts:
        series = prices.get(a["key"], [])
        a["shares"] = {h: retained_share(a, price_after(series, a["ts"] + h * 3600.0))
                       for h in HORIZONS_H}

    def rows_by(field: str) -> List[Tuple[str, dict]]:
        groups: Dict[str, List[dict]] = defaultdict(list)
        for a in alerts:
            groups[str(a.get(field) or "?")].append(a)
        return sorted(((k, group_stats(v)) for k, v in groups.items()
                       if len(v) >= args.min_group),
                      key=lambda kv: -(kv[1][HORIZONS_H[0]]["n"]))

    overall = group_stats(alerts)
    result = {
        "journal": str(journal),
        "alerts_total": len(alerts) + skipped,
        "alerts_scored": len(alerts),
        "alerts_without_key_skipped": skipped,
        "confirm_ratio": args.confirm_ratio,
        "overall": overall,
        "by_category": dict(rows_by("category")),
        "by_phase": dict(rows_by("phase")),
        "by_label": dict(rows_by("label")),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    def fmt(hstats: dict) -> str:
        parts = []
        for h in HORIZONS_H:
            s = hstats[h]
            if s["n"] == 0:
                parts.append(f"+{h:g}h: -")
            else:
                parts.append(f"+{h:g}h: {s['confirm_pct']:.0f}% (n={s['n']}, "
                             f"med {s['median_share']:.2f})")
        return "  ".join(parts)

    print(f"NinjaAlert backtest: {journal.name}  "
          f"({len(alerts)} scored, {skipped} skipped without key)")
    print(f"confirmed = retained share >= {args.confirm_ratio:.0%} of the alerted move")
    print(f"overall   : {fmt(overall)}")
    for title, key in (("by category", "by_category"),
                       ("by phase", "by_phase"), ("by label", "by_label")):
        rows = result[key]
        if not rows:
            continue
        print(f"{title} (n >= {args.min_group}):")
        for name, st in rows.items():
            print(f"  {name:32} {fmt(st)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
