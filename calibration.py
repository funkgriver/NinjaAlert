"""calibration.py - logistic (w, b) refit over accumulated tick history.

Implements the audit's Phase 1 recommendation: periodically refit the
manipulation-classifier weights (w_k, b) by gradient descent on log-loss,
Platt-scaling style, instead of running on static hand-set values forever.

Ground truth: there are no manual labels. Training pairs are pseudo-labelled
from the model's own confident decisions on replayed history:

    manip >= suspect_at  ->  y = 1   (SUSPECT cohort)
    manip <= organic_at  ->  y = 0   (ORGANIC cohort)
    in between           ->  skipped (AMBIGUOUS carries no signal)

Safeguards (the refit may only sharpen the boundary, never run away):
  * minimum-sample gate (>= MIN_POS positives and >= MIN_NEG negatives)
  * weights clamped to WEIGHT_CLAMP, bias to BIAS_CLAMP
  * L2 pull toward the current parameters (trust region)
  * 80/20 train/holdout split; the refit is only accepted when the holdout
    log-loss improves on the current parameters' holdout log-loss

Standard library only, so it can be unit-tested without Qt or network.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from advanced_analyzer import AnomalyAnalyzer, _sigmoid
from models import ItemTick, Snapshot

log = logging.getLogger("ninjaalert.calibration")

WEIGHT_CLAMP = (0.0, 5.0)
BIAS_CLAMP = (-10.0, 5.0)
MIN_POS = 30                       # confident positives required to refit
MIN_NEG = 30                       # confident negatives required to refit
DEFAULT_LR = 0.1
DEFAULT_EPOCHS = 300
DEFAULT_L2 = 0.01                  # pull toward the current parameters
HOLDOUT_FRACTION = 0.2
IMPROVE_MARGIN = 1e-4              # required holdout log-loss improvement


@dataclass(slots=True)
class FitResult:
    weights: List[float]
    bias: float
    improved: bool
    n_pos: int
    n_neg: int
    loss_before: float             # holdout log-loss of the current parameters
    loss_after: float              # holdout log-loss of the refit parameters
    reason: str = ""               # why a refit was rejected ("" when accepted)


# --------------------------------------------------------------- data path
def collect_training_rows(cfg, league_start_fn: Callable[[str], str],
                          ticks: Sequence[ItemTick]) -> List[Tuple[Tuple[float, ...], float]]:
    """Replay stored ticks through a throwaway analyzer, capturing the
    (features, manip) pair for every ready row *before* the alert gate.

    Ticks are grouped per category so cohort breadth and the price median
    match live per-category snapshots. Each tick advances the per-item state
    exactly once, in timestamp order.
    """
    analyzer = AnomalyAnalyzer(cfg, league_start_fn)
    by_cat: Dict[str, List[ItemTick]] = {}
    for t in sorted(ticks, key=lambda t: t.ts):
        by_cat.setdefault(t.category, []).append(t)

    samples: List[Tuple[Tuple[float, ...], float]] = []
    for cat, cat_ticks in by_cat.items():
        game = cat.split(":", 1)[0]
        snap = Snapshot(game=game, category=cat, league="", ts=cat_ticks[-1].ts,
                        source="documented", ticks=tuple(cat_ticks))
        try:
            analyzer.process(snap, collect=samples)
        except Exception:              # one corrupt category must not stop the sweep
            log.exception("training replay failed for %s", cat)
    return samples


def pseudo_label(samples: Sequence[Tuple[Sequence[float], float]],
                 suspect_at: float, organic_at: float
                 ) -> Tuple[List[List[float]], List[int]]:
    """Split confident model decisions into (xs, ys); AMBIGUOUS rows are dropped."""
    xs: List[List[float]] = []
    ys: List[int] = []
    for feats, manip in samples:
        if manip >= suspect_at:
            ys.append(1)
        elif manip <= organic_at:
            ys.append(0)
        else:
            continue
        xs.append([float(f) for f in feats])
    return xs, ys


# ------------------------------------------------------------------- fitting
def _log_loss(xs: Sequence[Sequence[float]], ys: Sequence[int],
              w: Sequence[float], b: float, eps: float = 1e-9) -> float:
    if not ys:
        return float("inf")
    total = 0.0
    for x, y in zip(xs, ys):
        p = _sigmoid(b + sum(wi * xi for wi, xi in zip(w, x)))
        p = min(max(p, eps), 1.0 - eps)
        total += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return total / len(ys)


def fit_logistic(xs: Sequence[Sequence[float]], ys: Sequence[int],
                 w0: Sequence[float], b0: float,
                 lr: float = DEFAULT_LR, epochs: int = DEFAULT_EPOCHS,
                 l2: float = DEFAULT_L2,
                 seed: int = 7) -> FitResult:
    """Refit (w, b) by GD on log-loss with all safeguards applied.

    Returns a FitResult whose `improved` flag says whether the refit should be
    adopted; rejected refits carry the current parameters back unchanged.
    """
    n = len(ys)
    n_pos = sum(1 for y in ys if y == 1)
    n_neg = n - n_pos
    keep = FitResult(list(w0), float(b0), False, n_pos, n_neg, 0.0, 0.0)
    if n_pos < MIN_POS or n_neg < MIN_NEG:
        keep.reason = (f"insufficient confident samples "
                       f"(pos {n_pos} < {MIN_POS} or neg {n_neg} < {MIN_NEG})")
        return keep

    # deterministic shuffle + stratified-enough holdout split
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    n_hold = max(1, int(n * HOLDOUT_FRACTION))
    hold_idx, train_idx = set(idx[:n_hold]), idx[n_hold:]
    xs_hold = [list(xs[i]) for i in hold_idx]
    ys_hold = [ys[i] for i in hold_idx]
    xs_train = [list(xs[i]) for i in train_idx]
    ys_train = [ys[i] for i in train_idx]

    w = [float(x) for x in w0]
    b = float(b0)
    for _ in range(epochs):
        gw = [0.0] * len(w)
        gb = 0.0
        for x, y in zip(xs_train, ys_train):
            p = _sigmoid(b + sum(wi * xi for wi, xi in zip(w, x)))
            err = p - y
            for i, xi in enumerate(x):
                gw[i] += err * xi
            gb += err
        m = len(ys_train)
        for i in range(len(w)):
            w[i] -= lr * (gw[i] / m + l2 * (w[i] - w0[i]))
            w[i] = min(max(w[i], WEIGHT_CLAMP[0]), WEIGHT_CLAMP[1])
        b -= lr * (gb / m + l2 * (b - b0))
        b = min(max(b, BIAS_CLAMP[0]), BIAS_CLAMP[1])

    loss_before = _log_loss(xs_hold, ys_hold, w0, b0)
    loss_after = _log_loss(xs_hold, ys_hold, w, b)
    if not (loss_after < loss_before - IMPROVE_MARGIN):
        keep.loss_before, keep.loss_after = loss_before, loss_after
        keep.reason = (f"holdout log-loss did not improve "
                       f"({loss_before:.4f} -> {loss_after:.4f})")
        return keep
    return FitResult([round(v, 4) for v in w], round(b, 4), True,
                     n_pos, n_neg, loss_before, loss_after)


def suggest_thresholds(pairs: Sequence[Tuple[float, int]],
                       current_suspect: float, current_organic: float
                       ) -> Tuple[Optional[float], Optional[float], float]:
    """Youden-J sweep over the manip distribution of confident samples.

    Returns (suggested_suspect_at, suggested_organic_at, best_j); either
    suggestion is None when that side has no confident samples or the sweep
    cannot beat the current threshold's J.
    """
    pos = [m for m, y in pairs if y == 1]
    neg = [m for m, y in pairs if y == 0]
    if not pos or not neg:
        return None, None, 0.0

    def j_at(t: float) -> float:
        tpr = sum(1 for m in pos if m >= t) / len(pos)
        fpr = sum(1 for m in neg if m >= t) / len(neg)
        return tpr - fpr

    candidates = sorted({m for m, _ in pairs})
    best_suspect, best_j = None, -1.0
    for t in candidates:
        j = j_at(t)
        if j > best_j + 1e-9:
            best_suspect, best_j = t, j

    best_organic: Optional[float] = None
    if j_at(current_suspect if current_suspect in candidates else
            (best_suspect if best_suspect is not None else current_suspect)) < 0:
        best_suspect = None                        # never worse than keeping current
    if best_suspect is not None and best_suspect <= current_organic:
        best_suspect = None
    # organic side: lowest threshold whose ORGANIC-classification J still beats current
    cur_j_org = _organic_j(pos, neg, current_organic)
    for t in candidates:
        if t >= (best_suspect or current_suspect):
            break
        if _organic_j(pos, neg, t) > cur_j_org + 1e-9:
            best_organic = t
    if best_organic is not None and best_organic >= (best_suspect or current_suspect):
        best_organic = None
    return best_suspect, best_organic, best_j


def _organic_j(pos: Sequence[float], neg: Sequence[float], t: float) -> float:
    """J for the ORGANIC side: specificity (neg below t) minus false-organic rate."""
    if not neg:
        return 0.0
    tnr = sum(1 for m in neg if m <= t) / len(neg)
    fpr_org = sum(1 for m in pos if m <= t) / max(1, len(pos))
    return tnr - fpr_org


# ------------------------------------------------------------- ticks-to-fit
def calibrate_from_ticks(cfg, league_start_fn: Callable[[str], str],
                         ticks: Sequence[ItemTick],
                         current_weights: Sequence[float], current_bias: float,
                         epochs: int = DEFAULT_EPOCHS) -> Optional[FitResult]:
    """One full calibration cycle over one game's stored ticks.

    Returns None when there is too little data to replay at all; otherwise a
    FitResult (`improved=False` with a reason when a safeguard rejected it).
    """
    if len(ticks) < 50:
        return None
    samples = collect_training_rows(cfg, league_start_fn, ticks)
    xs, ys = pseudo_label(samples, cfg.suspect_at, cfg.organic_at)
    result = fit_logistic(xs, ys, current_weights, current_bias, epochs=epochs)
    if not xs:
        result.reason = result.reason or "no confident samples in history"
    return result
