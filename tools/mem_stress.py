"""Feed the analysis worker a realistic snapshot volume and report RSS growth.

  python tools/mem_stress.py [--cycles 40] [--quotes 5000]

Every cycle emulates one fetched poe.ninja category (same item keys, shifting
prices, like live SkillGem data) pushed through the full AnalysisWorker
pipeline (tick conversion -> basic engine -> robust engine -> SQLite ->
governor -> scenarios). A bounded RSS delta proves there is no per-cycle
retention; an ever-growing delta means a leak.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication          # noqa: E402

import analysis_worker as aw                      # noqa: E402
from analysis_worker import AnalysisWorker, process_rss_mb  # noqa: E402
from api_client import FetchMeta, ItemQuote       # noqa: E402
from appconfig import Config                      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cycles", type=int, default=40)
    ap.add_argument("--quotes", type=int, default=5000)
    args = ap.parse_args()

    app = QApplication([])
    cfg = Config()
    tmp = tempfile.TemporaryDirectory()
    real_app_dir = aw.app_dir
    aw.app_dir = lambda: tmp.name                 # isolate history DBs

    worker = AnalysisWorker(cfg, lambda g: "")
    results: list = []
    worker.analyzed.connect(lambda r: results.append(r))
    worker.start()

    meta = FetchMeta(game="poe1", league="Allflame", category="SkillGem",
                     source="item", primary="chaos")

    def quotes(cycle: int) -> list:
        return [ItemQuote(key=f"poe1:i:SkillGem:gem{j}", name=f"Gem Variant {j}",
                          game="poe1", category="SkillGem", source="item",
                          price=10.0 + ((cycle * 37 + j) % 900) / 100.0,
                          list_count=50 + (j % 30), icon=None,
                          details_id=f"gem{j}", volume=None, aux=None)
                for j in range(args.quotes)]

    def wait_for(n: int, deadline_s: float = 300.0) -> None:
        t0 = time.time()
        while len(results) < n and time.time() - t0 < deadline_s:
            app.processEvents()
            time.sleep(0.02)

    worker.submit_snapshot("poe1", quotes(0), meta)
    wait_for(1)
    base = process_rss_mb() or 0.0
    print(f"baseline RSS: {base:.0f} MB (1 warmup cycle)")

    for i in range(1, args.cycles):
        worker.submit_snapshot("poe1", quotes(i), meta)
        wait_for(i + 1)
        if i % 10 == 0:
            print(f"cycle {i:3d}: RSS {process_rss_mb() or 0:.0f} MB")

    final = process_rss_mb() or 0.0
    worker.stop()
    worker.wait(8000)
    after = process_rss_mb() or 0.0
    aw.app_dir = real_app_dir
    tmp.cleanup()

    print(f"\nRSS {base:.0f} -> {final:.0f} MB (delta {final - base:+.0f} MB), "
          f"after worker stop {after:.0f} MB")
    print(f"processed {len(results)}/{args.cycles} snapshots x {args.quotes} quotes")
    ok = len(results) == args.cycles and (final - base) < 150.0
    print("MEM STRESS " + ("OK" if ok else "FAIL — growth suggests retention"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
