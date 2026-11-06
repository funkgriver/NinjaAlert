"""NinjaAlert smoke test: run ONE real poll cycle end-to-end, headlessly.

Wires the real PollWorker -> NinjaApiClient -> engines pipeline with no GUI
(no tray, no popups, no sound) and prints what would have alerted, including
the advanced engine's classification fields.

  python tools/smoke_cycle.py          # poe1
  python tools/smoke_cycle.py poe2     # poe2
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


def main() -> int:
    game = sys.argv[1] if len(sys.argv) > 1 else "poe1"
    app = QApplication.instance() or QApplication(sys.argv)

    from advanced_analyzer import AnalysisPipeline, anomaly_from_basic, ticks_from_quotes
    from analyzer import AnomalyDetector
    from api_client import NinjaApiClient
    from appconfig import Config
    from models import Snapshot
    from pathlib import Path
    from poller import PollWorker

    cfg = Config()
    gc = cfg.game_cfg(game)
    gc.enabled = True
    gc.poll_interval_minutes = 5
    gc.watch = ["Currency"]  # single category keeps the smoke test fast
    cfg.alerts.sound = False
    cfg.network.contact = "smoke-test@local"

    client = NinjaApiClient(user_agent_fn=lambda: cfg.user_agent,
                            base_url_fn=lambda: cfg.network.base_url)
    pipeline = AnalysisPipeline(cfg.detector, cfg.alerts, lambda g: "",
                                Path(ROOT) / f".smoke-history-{game}.sqlite3")
    basic = AnomalyDetector(cfg.thresholds)
    worker = PollWorker(client, cfg, game)

    results = {"snapshots": 0, "quotes": 0, "alerts": [], "errors": [],
               "league": None, "shown": 0}
    states: list = []

    def on_snapshot(quotes, meta) -> None:
        results["snapshots"] += 1
        results["quotes"] += len(quotes)
        results["league"] = f"{meta.game} · {meta.league}"
        now = time.time()
        ticks = ticks_from_quotes(quotes, now)
        snap = Snapshot(game=meta.game, category=f"{meta.game}:{meta.source}:{meta.category}",
                        league=meta.league, ts=now, source="documented", ticks=tuple(ticks))
        basic_alerts = basic.feed(quotes, meta, now)
        extra = [anomaly_from_basic(a, meta.league)
                 for a in basic_alerts if a.key not in pipeline.analyzer.ready_keys]
        shown, digest = pipeline.handle(snap, extra_anomalies=extra)
        results["shown"] += len(shown)
        for a in shown:
            results["alerts"].append(
                f"{a.name}: {a.prev_price:.4g} -> {a.price:.4g} ({a.pct:+.1f}%) "
                f"z={a.z_adj:.1f} {a.label} · {a.phase}")

    worker.snapshot.connect(on_snapshot)
    worker.cycle_error.connect(lambda e: results["errors"].append(e))
    worker.state_changed.connect(lambda s: (print("  state:", s), states.append(s)))
    worker.start()

    deadline = time.time() + 120
    while time.time() < deadline and worker.isRunning() and "waiting" not in states:
        app.processEvents()
        time.sleep(0.05)
    worker.stop()
    app.processEvents()

    print(f"league         : {results['league']}")
    print(f"snapshots      : {results['snapshots']}")
    print(f"quotes         : {results['quotes']}")
    print(f"stored ticks   : {pipeline.store.count()}")
    print(f"robust baselines ready: {len(pipeline.analyzer.ready_keys)}")
    print(f"errors         : {results['errors'] or 'none'}")
    print(f"alerts         : {results['alerts'] or 'none (first cycle only seeds baselines)'}")
    ok = (results["snapshots"] > 0 and results["quotes"] > 0
          and pipeline.store.count() > 0 and not results["errors"])
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
