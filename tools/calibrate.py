"""Refit the manipulation-classifier weights (w, b) from stored tick history.

Preview (read-only, default):
    python tools/calibrate.py                 # both games
    python tools/calibrate.py --game poe1

Apply a successful refit to config.json (a timestamped backup is written first):
    python tools/calibrate.py --apply

History DB location follows the app layout: NINJAALERT_DIR env override, else
<repo>/dist (where the frozen exe keeps its portable files).

The refit uses pseudo-labels (the model's own confident SUSPECT/ORGANIC
decisions) and is guarded: min-sample gates, weight clamps, L2 pull toward the
current parameters, and holdout log-loss must improve. See calibration.py.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from advanced_analyzer import HistoryStore, _sigmoid   # noqa: E402
from appconfig import Config, app_dir                  # noqa: E402
from calibration import (calibrate_from_ticks,         # noqa: E402
                         labeled_samples, suggest_thresholds)


def history_base() -> str:
    return os.environ.get("NINJAALERT_DIR") or os.path.join(ROOT, "dist")


def league_start_fn_factory(cfg: Config):
    def fn(game: str) -> str:
        return cfg.game_cfg(game).league_start or ""   # neutral default league day
    return fn


def load_ticks(db_dir: str, game: str, retention_days: float):
    db_path = Path(db_dir) / f"history-{game}.sqlite3"
    if not db_path.exists():
        return None
    store = HistoryStore(db_path)
    try:
        return store.load_since(time.time() - retention_days * 86400.0)
    finally:
        store.close()


def calibrate_game(cfg: Config, game: str, epochs: int, db_dir: str):
    ticks = load_ticks(db_dir, game, cfg.detector.history_retention_days)
    print(f"\n=== {game} ({Path(db_dir) / f'history-{game}.sqlite3'}) ===")
    if ticks is None:
        print("  no history DB yet - skipped")
        return None
    result = calibrate_from_ticks(cfg.detector, league_start_fn_factory(cfg), ticks,
                                  cfg.detector.weights, cfg.detector.bias, epochs=epochs)
    if result is None:
        print("  too little history to replay (<50 ticks) - skipped")
        return None

    print(f"  confident samples: pos {result.n_pos} / neg {result.n_neg}")
    print(f"  holdout log-loss:  {result.loss_before:.4f} -> {result.loss_after:.4f}")
    print(f"  current weights:   {cfg.detector.weights}  bias {cfg.detector.bias}")
    if result.improved:
        print(f"  refit weights:     {result.weights}  bias {result.bias}")
        print("  verdict: IMPROVED - adopt with --apply")
    else:
        print(f"  verdict: KEEP CURRENT ({result.reason})")
        print(f"  (rejected candidate was: {result.weights} bias {result.bias})")
    return result


def threshold_suggestions(cfg: Config, game: str, db_dir: str) -> str:
    ticks = load_ticks(db_dir, game, cfg.detector.history_retention_days)
    if ticks is None or len(ticks) < 50:
        return f"  {game}: not enough history"
    xs, ys = labeled_samples(cfg.detector, league_start_fn_factory(cfg), ticks)
    if not xs:
        return f"  {game}: no labelable rows (need forward price data)"
    pairs = [(_sigmoid(cfg.detector.bias + sum(w * fi for w, fi in
                                               zip(cfg.detector.weights, x))), y)
             for x, y in zip(xs, ys)]
    s, o, j = suggest_thresholds(pairs, cfg.detector.suspect_at, cfg.detector.organic_at)
    fmt = lambda v: "-" if v is None else f"{v:.3f}"
    return (f"  {game}: current [suspect={cfg.detector.suspect_at:.3f} "
            f"organic={cfg.detector.organic_at:.3f}]  "
            f"suggested [suspect={fmt(s)} organic={fmt(o)}] J={j:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", choices=("poe1", "poe2", "both"), default="both")
    ap.add_argument("--apply", action="store_true",
                    help="write improved weights into config.json (backup first)")
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--db-dir", default=None, help="history DB directory override")
    args = ap.parse_args()

    cfg = Config.load_or_create()
    db_dir = args.db_dir or history_base()
    games = ("poe1", "poe2") if args.game == "both" else (args.game,)

    results = {g: calibrate_game(cfg, g, args.epochs, db_dir) for g in games}

    print("\n=== threshold suggestions (Youden J sweep) ===")
    for g in games:
        print(threshold_suggestions(cfg, g, db_dir))

    if args.apply:
        changed = [g for g in games if results[g] is not None and results[g].improved]
        if changed:
            for g in changed:
                cfg.detector.weights = list(results[g].weights)
                cfg.detector.bias = float(results[g].bias)
            cfg_path = os.path.join(app_dir(), "config.json")
            if os.path.exists(cfg_path):
                backup = f"{cfg_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(cfg_path, backup)
                print(f"\nconfig backed up -> {backup}")
            cfg.save(cfg_path)
            print(f"applied refit for {', '.join(changed)} -> {cfg_path}")
            print(f"weights={cfg.detector.weights} bias={cfg.detector.bias}")
        else:
            print("\nnothing to apply - no game improved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
