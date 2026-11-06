"""NinjaAlert - alert journal analyzer.

Reads alerts.jsonl (one JSON line per admitted alert) and prints:
  - label / category breakdown
  - top anomalies by priority
  - hourly alert rate (last 24 h by default)
  - SQLite tick-history stats (warm-start size, items tracked)

  python tools/analyze.py                       # summary
  python tools/analyze.py --last 15             # last 15 alerts verbatim
  python tools/analyze.py --suspect             # only SUSPECT labels (cornering watch)
  python tools/analyze.py --json                # machine-readable summary
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze NinjaAlert alert journal")
    ap.add_argument("--last", type=int, default=0, help="show the last N alerts verbatim")
    ap.add_argument("--suspect", action="store_true", help="only SUSPECT labels")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--db", default="", help="history sqlite to stat (default: both games)")
    args = ap.parse_args()

    base = os.environ.get("NINJAALERT_DIR") or (ROOT / "dist")
    journal = Path(base) / "alerts.jsonl"
    rows = load_rows(journal)
    if args.suspect:
        rows = [r for r in rows if r.get("label") == "SUSPECT"]

    if args.last:
        for r in rows[-args.last:]:
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r.get('ts', 0)))} "
                  f"[{r.get('label','?'):9}] {r.get('game','')}/{r.get('league','')} "
                  f"{r.get('name','')}  {r.get('prev_price',''):g} -> {r.get('price',''):g} "
                  f"({r.get('pct',0):+.1f}%)  z={r.get('z_adj',0):.1f} "
                  f"prio={r.get('priority',0):.1f}")
        return 0

    labels = collections.Counter(r.get("label", "?") for r in rows)
    cats = collections.Counter(r.get("category", "?") for r in rows)
    names = collections.Counter(r.get("name", "?") for r in rows)
    by_hour = collections.Counter(
        time.strftime("%m-%d %H:00", time.localtime(r.get("ts", 0))) for r in rows)
    top = sorted(rows, key=lambda r: -r.get("priority", 0))[:10]

    summary = {
        "journal": str(journal),
        "alerts_total": len(rows),
        "labels": dict(labels),
        "top_categories": cats.most_common(10),
        "top_items": names.most_common(10),
        "alerts_last_hour": by_hour.get(time.strftime("%m-%d %H:00", time.localtime()), 0),
        "top_by_priority": [
            {"name": r.get("name"), "game": r.get("game"), "price": r.get("price"),
             "pct": r.get("pct"), "z_adj": r.get("z_adj"), "label": r.get("label"),
             "phase": r.get("phase"), "priority": r.get("priority")} for r in top],
        "history": {},
    }

    for game in ("poe1", "poe2"):
        db = Path(args.db) if args.db else Path(base) / f"history-{game}.sqlite3"
        if db.exists():
            try:
                con = sqlite3.connect(str(db))
                ticks = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
                items = con.execute("SELECT COUNT(DISTINCT key) FROM ticks").fetchone()[0]
                newest = con.execute("SELECT MAX(ts) FROM ticks").fetchone()[0]
                con.close()
                if ticks == 0 or newest is None:
                    summary["history"][game] = {"ticks": 0, "items": 0, "newest_age_min": None}
                else:
                    summary["history"][game] = {
                        "ticks": ticks, "items": items,
                        "newest_age_min": round((time.time() - newest) / 60.0, 1)}
            except sqlite3.Error as exc:
                summary["history"][game] = {"error": str(exc)}

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print(f"NinjaAlert journal: {journal}  ({len(rows)} alerts)")
    if not rows:
        print("  empty so far - alerts appear once baselines exist (~3 polls) and")
        print("  anomalies fire; the 'auto' engine then labels them ORGANIC/SUSPECT.")
    if labels:
        print("labels   :", ", ".join(f"{k}={v}" for k, v in labels.most_common()))
    if cats:
        print("categories:", ", ".join(f"{k}={v}" for k, v in cats.most_common(8)))
    if names:
        print("top items :", ", ".join(f"{k}={v}" for k, v in names.most_common(8)))
    print("last hour :", summary["alerts_last_hour"], "alerts")
    for game, st in summary["history"].items():
        if "error" in st:
            print(f"history {game}: {st['error']}")
        elif st.get("newest_age_min") is None:
            print(f"history {game}: empty (game disabled or not yet polled)")
        else:
            print(f"history {game}: {st['ticks']} ticks / {st['items']} items "
                  f"(newest {st['newest_age_min']} min ago)")
    if top:
        print("\ntop by priority:")
        for r in top:
            print(f"  {r.get('priority',0):6.1f}  [{r.get('label','?'):9}] "
                  f"{r.get('game','')} {r.get('name','')}  -> {r.get('price',''):g} "
                  f"({r.get('pct',0):+.1f}%)  {r.get('phase','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
