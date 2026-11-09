"""advanced_analyzer.py - phase-aware anomaly detection, classification and
alert budgeting (spec: "PoE Market Anomaly Detector", Nov 2026).

Implements the spec formulas:
  1. PhaseModel        - league-phase scaling of the z threshold + EMA half-lives
  2. AnomalyAnalyzer   - robust z on unit-time log returns (median/MAD), liquidity
                         weight, RVOL, supply ratio, basis stress, EMA crossover
  3. _manipulation     - organic-demand vs synthetic-pressure logistic score
  4. AlertGovernor     - priority ranking, token bucket, escalating cooldowns, digest
  5. HistoryStore      - SQLite (WAL) tick history for warm start and refitting
  6. AnalysisPipeline  - store -> analyze -> govern

Standard library only, so it can be unit-tested and run without Qt or network.
Adapted to NinjaAlert config objects (appconfig.DetectorCfg / Alerts) and adds
`ready_keys` tracking so "auto" mode can fall back to the basic delta engine
while the robust baseline is still seeding.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from models import Anomaly, Digest, DigestItem, ItemTick, Snapshot

log = logging.getLogger("ninjaalert.advanced")

EPS = 1e-9
Z_POOL_MAX = 4096                  # recent |z_adj| samples kept per category
Z_POOL_MIN = 200                   # samples before the empirical threshold is trusted
CAT_RT_MAX = 2048                  # recent |rt| samples kept per category (B4 shrinkage)
MARKET_MIN_ROWS = 5                # min ready items before a market component is separable
MARKET_COHERENCE = 0.6             # share of same-sign returns for a drift to count
TOD_POOL_MAX = 2048                # recent |rt| samples per time-of-day bucket (D14)
TOD_MIN = 200                      # samples before a bucket factor is trusted
TOD_RECALC_EVERY = 256             # recompute bucket factors every N samples
DIGEST_MAX_ITEMS = 12              # rows carried in a digest toast (bounded payload)


# ---------------------------------------------------------------- small helpers
def _median(values: Iterable[float]) -> float:
    data = sorted(values)
    n = len(data)
    if n == 0:
        return 0.0
    mid = n // 2
    return data[mid] if n % 2 else 0.5 * (data[mid - 1] + data[mid])


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def alert_priority(abs_delta: float, rvol: float, z_adj: float) -> float:
    """Plan item D12: rank alerts by the move's absolute size (primary
    currency: chaos/divine) and by volume - NOT by the item's price relative
    to the category median, which made one divine item outrank any chaos move
    regardless of significance. The z score only modulates the rank."""
    return ((1.0 + math.log1p(max(abs_delta, 0.0)))
            * (1.0 + math.log1p(max(rvol, 1.0)))
            * (1.0 + max(z_adj, 0.0) / 4.0))


# --------------------------------------------------------- league phase model
class PhaseModel:
    """z*(t), A(t) and EMA half-lives as functions of league age t (days)."""

    def __init__(self, cfg) -> None:  # cfg: appconfig.DetectorCfg
        self.c = cfg

    def league_day(self, ts: float, start_iso: str) -> float:
        if start_iso:
            try:
                start = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
                return max(0.0, (ts - start.timestamp()) / 86400.0)
            except ValueError:
                log.warning("Bad league_start %r; using default league day.", start_iso)
        return self.c.default_league_day

    def activity(self, t: float) -> float:
        x = _clip((t - self.c.decay_start_days) / self.c.decay_width_days, -50.0, 50.0)
        return 1.0 / (1.0 + math.exp(x))

    def z_threshold(self, t: float) -> float:
        c = self.c
        return (c.z_inf + (c.z0 - c.z_inf) * math.exp(-t / c.tau_z_days)
                + c.gamma * (1.0 - self.activity(t)))

    def half_lives(self, t: float) -> Tuple[float, float]:
        c = self.c
        s = 1.0 - math.exp(-t / c.tau_h_days)
        return (c.fast_h_min + (c.fast_h_max - c.fast_h_min) * s,
                c.slow_h_min + (c.slow_h_max - c.slow_h_min) * s)

    @staticmethod
    def label(t: float) -> str:
        if t < 4:
            return "Launch"
        if t < 28:
            return "Stabilization"
        if t < 56:
            return "Mature"
        return "Decay"


# --------------------------------------------------------------- per-item state
class _ItemState:
    __slots__ = ("last_ts", "last_price", "rt", "lnaux", "ef", "es", "ev", "el",
                 "steps", "lv", "ll")

    def __init__(self, window: int, price: float, ts: float,
                 volume: Optional[float] = None, listings: Optional[float] = None) -> None:
        lp = math.log(price)
        self.last_ts = ts
        self.last_price = price
        self.rt: Deque[float] = deque(maxlen=window)       # unit-time returns
        self.lnaux: Deque[float] = deque(maxlen=window)    # log stress ratios
        self.ef = lp                                       # fast EMA of ln P
        self.es = lp                                       # slow EMA of ln P
        self.ev: Optional[float] = None                    # slow EMA of volume
        self.el: Optional[float] = None                    # slow EMA of listings
        self.steps: Deque[float] = deque(maxlen=32)        # recent nonzero price deltas
        self.lv = volume                                   # last raw volume (C8 skip)
        self.ll = listings                                 # last raw listings (C8 skip)


@dataclass(slots=True)
class _Metrics:
    ready: bool
    r: float
    z_adj: float
    crossover: float
    confirmed: bool
    rvol: Optional[float]
    supply: Optional[float]
    basis: Optional[float]
    w_l: float
    med: float = 0.0                   # item median rt (kept for residual recompute)
    scale: float = 0.0                 # item robust scale (kept for residual recompute)
    redelisted: bool = False           # item reappeared within delist_window_hours (C10)


# ------------------------------------------------------------------- analyzer
class AnomalyAnalyzer:
    def __init__(self, cfg, league_start_fn: Callable[[str], str]) -> None:
        self.phase = PhaseModel(cfg)
        self._cfg = cfg
        self._league_start = league_start_fn
        self._states: Dict[str, _ItemState] = {}
        self.ready_keys: set = set()   # keys with a completed robust baseline (auto-mode glue)
        self._weights: Optional[List[float]] = None   # calibrated override (see calibration.py)
        self._bias: Optional[float] = None
        # heavy-tail guard: recent |z_adj| per category + snapshot cadence, so the
        # empirical distribution can cap the raw alert rate (B3) instead of the
        # governor filtering noise after the fact
        self._z_pool: Dict[str, Deque[float]] = {}     # category -> recent |z_adj|
        self._cat_rt: Dict[str, Deque[float]] = {}     # category -> recent |rt| (nonzero used)
        self._cat_rt_n: Dict[str, int] = {}            # category -> total |rt| appended
        self._cat_mad_cache: Dict[str, Tuple[int, float]] = {}
        self._last_snap: Dict[str, float] = {}         # category -> last snapshot ts
        self._snap_interval: Dict[str, float] = {}     # category -> EWMA poll interval (s)
        self._snap_size: Dict[str, float] = {}         # category -> EWMA snapshot size
        self._disappeared: Dict[str, float] = {}       # key -> last delist ts (C10)
        # plan item D14: learned volatility scale per 4x6h UTC bucket
        self._tod_pool: Tuple[Deque[float], ...] = tuple(
            deque(maxlen=TOD_POOL_MAX) for _ in range(4))
        self._tod_n = [0, 0, 0, 0]
        self._tod_cache: Tuple[int, Tuple[float, float, float, float]] = (-1, (1.0, 1.0, 1.0, 1.0))

    @property
    def cfg(self):
        return self._cfg

    @cfg.setter
    def cfg(self, value) -> None:
        # keep the phase model in sync: applying saved settings must change
        # z*(t) and EMA half-lives too, not only the analyzer's own parameters
        self._cfg = value
        self.phase.c = value

    @property
    def tracked_count(self) -> int:
        return len(self._states)

    def set_weights(self, weights: List[float], bias: float) -> None:
        """Install refit weights (in-app auto-calibration) without touching the config."""
        self._weights = [float(w) for w in weights]
        self._bias = float(bias)

    # -- empirical alert-rate cap (heavy-tail guard, plan item B3) -----------
    def _observe_snapshot_meta(self, category: str, ts: float, n: int) -> None:
        prev = self._last_snap.get(category)
        if prev is not None and ts > prev:
            dt = ts - prev
            self._snap_interval[category] = 0.8 * self._snap_interval.get(category, dt) + 0.2 * dt
        self._last_snap[category] = ts
        self._snap_size[category] = 0.8 * self._snap_size.get(category, float(n)) + 0.2 * float(n)

    def _observe_z(self, category: str, z_abs: float) -> None:
        pool = self._z_pool.get(category)
        if pool is None:
            pool = self._z_pool[category] = deque(maxlen=Z_POOL_MAX)
        pool.append(z_abs)

    def _z_threshold_category(self, category: str) -> float:
        """|z| quantile that caps raw alerts at ~target/day for this category.

        evaluations/day = snapshot size * 86400 / poll interval, so the needed
        tail is target / evaluations_per_day; the phase model stays the lower
        bound (see process). Returns 0.0 until enough evidence exists.
        """
        c = self.cfg
        target = getattr(c, "target_alerts_per_category_per_day", 0.0)
        pool = self._z_pool.get(category)
        interval = self._snap_interval.get(category)
        size = self._snap_size.get(category)
        if target <= 0 or not pool or len(pool) < Z_POOL_MIN or not interval or not size:
            return 0.0
        evals_per_day = size * 86400.0 / max(interval, 1.0)
        tail = _clip(target / max(evals_per_day, 1.0), 1e-4, 0.5)
        data = sorted(pool)
        idx = min(int((1.0 - tail) * len(data)), len(data) - 1)
        return max(0.0, data[idx])

    # -- young-baseline shrinkage (plan item B4) ------------------------------
    def _observe_rt(self, category: str, rt_abs: float) -> None:
        pool = self._cat_rt.get(category)
        if pool is None:
            pool = self._cat_rt[category] = deque(maxlen=CAT_RT_MAX)
        pool.append(rt_abs)
        self._cat_rt_n[category] = self._cat_rt_n.get(category, 0) + 1

    def _observe_disappearance(self, key: str, ts: float) -> None:
        """Plan item C10: the item left the market (listingCount < 1). Kept as
        a possible-buyout signal for delist_window_hours; the stale price of
        the vanished listing never feeds the baseline."""
        prev = self._disappeared.get(key)
        if prev is None or ts > prev:
            self._disappeared[key] = ts
            log.debug("item vanished from market: %s", key)

    # -- time-of-day seasonality (plan item D14) ------------------------------
    def _observe_tod(self, ts_end: float, rt_abs: float) -> None:
        b = int(time.gmtime(ts_end).tm_hour // 6) % 4
        self._tod_pool[b].append(rt_abs)
        self._tod_n[b] += 1

    def _tod_factors(self) -> Tuple[float, float, float, float]:
        """Volatility scale of each 6h UTC bucket relative to their median."""
        key = sum(self._tod_n) // TOD_RECALC_EVERY
        if key == self._tod_cache[0]:
            return self._tod_cache[1]
        meds = []
        for pool in self._tod_pool:
            vals = [x for x in pool if x > 0.0]
            meds.append(_median(vals) if len(vals) >= TOD_MIN else 0.0)
        if any(m <= 0.0 for m in meds):
            factors = (1.0, 1.0, 1.0, 1.0)
        else:
            ref = _median(meds)
            factors = tuple(_clip(m / ref, 0.4, 2.5) for m in meds)
        self._tod_cache = (key, factors)
        return factors

    def _season(self, ts_start: float, ts_end: float) -> float:
        """Multiplier for a return spanning [ts_start, ts_end]; 1.0 without
        enough evidence or when disabled. A quiet-bucket move is relatively
        more significant than the same move during active hours."""
        if not getattr(self.cfg, "tod_seasonality", False):
            return 1.0
        f = self._tod_factors()
        if f == (1.0, 1.0, 1.0, 1.0):
            return 1.0
        b0 = int(time.gmtime(ts_start).tm_hour // 6) % 4
        b1 = int(time.gmtime(ts_end).tm_hour // 6) % 4
        return _clip(0.5 * (f[b0] + f[b1]), 0.4, 2.5)

    def _category_mad(self, category: str) -> Optional[float]:
        """Typical |unit-time return| of the category (median of nonzero |rt|)."""
        pool = self._cat_rt.get(category)
        if not pool:
            return None
        n = self._cat_rt_n.get(category, 0)
        cached = self._cat_mad_cache.get(category)
        if cached is not None and cached[0] == n:
            return cached[1]
        vals = [x for x in pool if x > 0.0]
        val = _median(vals) if vals else 0.0
        self._cat_mad_cache[category] = (n, val)
        return val

    def _blended_mad(self, category: str, mad: float, n: int) -> float:
        """MAD of an item with few stored returns must not trust itself: blend
        with the category's typical return scale, weight w = n/(n+k)."""
        k = getattr(self.cfg, "mad_shrink_k", 0.0)
        if k <= 0 or n <= 0:
            return mad
        cat_mad = self._category_mad(category)
        if not cat_mad or cat_mad <= 0.0:
            return mad
        w = n / (n + k)
        return w * mad + (1.0 - w) * cat_mad

    # -- one update step shared by live processing and warm start
    def _advance(self, st: _ItemState, tick: ItemTick, hf: float, hs: float) -> Optional[_Metrics]:
        c = self.cfg
        dt_h = (tick.ts - st.last_ts) / 3600.0
        if dt_h <= 0:
            return None                    # duplicate or out-of-order snapshot
        # plan item C8 (revised after prod QA): a price-unchanged tick carries
        # no return information - feeding it dilutes the per-item MAD with zero
        # returns (88% of real ticks!) and inflates z on the next real move.
        # Liquidity EMAs still refresh on volume/listings-only updates; dt then
        # spans change-to-change, which the return scaling expects.
        if tick.price == st.last_price:
            dt_h = max(dt_h, 1.0 / 60.0)
            a_s0 = 1.0 - 2.0 ** (-dt_h / hs)
            if tick.volume is not None and tick.volume >= 0:
                st.ev = tick.volume if st.ev is None else st.ev + a_s0 * (tick.volume - st.ev)
            if tick.listings is not None and tick.listings >= 0:
                st.el = tick.listings if st.el is None else st.el + a_s0 * (tick.listings - st.el)
            st.lv, st.ll = tick.volume, tick.listings
            return None
        dt_h = max(dt_h, 1.0 / 60.0)

        lp = math.log(tick.price)
        r = lp - math.log(st.last_price)
        rt_raw = r / math.sqrt(dt_h)       # scaled to a one-hour unit
        if not (math.isfinite(lp) and math.isfinite(rt_raw)):
            return None                    # non-finite input must not poison the baseline
        if getattr(c, "tod_seasonality", False):
            self._observe_tod(tick.ts, abs(rt_raw))
        rt = rt_raw / self._season(st.last_ts, tick.ts)

        ready = len(st.rt) >= c.min_history
        med = 0.0
        # plan item C10: the item reappeared after a delist - a repricing on
        # top of a vanished supply is a strong cornering feature
        redelisted = (tick.ts - self._disappeared.get(tick.key, -1.0e18)
                      <= getattr(c, "delist_window_hours", 6.0) * 3600.0)
        if ready:
            med = _median(st.rt)
            mad = _median(abs(x - med) for x in st.rt)
            mad = self._blended_mad(tick.category, mad, len(st.rt))
            # plan item B5: a single minimum price step is quantization noise,
            # not a signal — the scale floor rises to the one-step return learned
            # from PRIOR steps (the current move must not set its own floor)
            scale_floor = c.mad_floor
            if st.steps:
                r_step = math.log1p(min(st.steps) / tick.price) / math.sqrt(dt_h)
                if r_step > scale_floor:
                    scale_floor = r_step
            scale = max(1.4826 * mad, scale_floor)
            z = (rt - med) / scale
        else:
            scale, z = c.mad_floor, 0.0
        step = abs(tick.price - st.last_price)
        if step > 0:
            st.steps.append(step)

        listings = tick.listings
        if listings is not None and listings >= 0:
            w_l = listings / (listings + c.liquidity_l0)
        elif (tick.volume is not None and tick.volume >= 0
                and getattr(c, "liquidity_v0", 0.0) > 0):
            # plan item B7: exchange quotes have no listings - volume is the
            # liquidity proxy; w_l=1 made every thin market fully confident
            w_l = tick.volume / (tick.volume + c.liquidity_v0)
        else:
            w_l = 1.0
        z_adj = z * math.sqrt(w_l)

        a_f = 1.0 - 2.0 ** (-dt_h / hf)
        a_s = 1.0 - 2.0 ** (-dt_h / hs)
        st.ef += a_f * (lp - st.ef)
        st.es += a_s * (lp - st.es)
        crossover = (st.ef - st.es) / (scale * math.sqrt(hs))
        confirmed = ready and crossover * z > 0 and abs(crossover) >= c.crossover_confirm

        rvol: Optional[float] = None
        if tick.volume is not None and tick.volume >= 0:
            if st.ev is not None:
                rvol = min(tick.volume / (st.ev + EPS), 100.0)
                st.ev += a_s * (tick.volume - st.ev)
            else:
                st.ev = tick.volume
        supply: Optional[float] = None
        if listings is not None and listings >= 0:
            if st.el is not None:
                supply = min(listings / (st.el + EPS), 100.0)
                st.el += a_s * (listings - st.el)
            else:
                st.el = listings
        basis: Optional[float] = None
        if tick.aux is not None and tick.aux > 0:
            la = math.log(tick.aux)
            if len(st.lnaux) >= c.min_history:
                basis = abs(la - _median(st.lnaux))
            st.lnaux.append(la)

        st.rt.append(rt)
        self._observe_rt(tick.category, abs(rt))
        st.last_ts, st.last_price = tick.ts, tick.price
        st.lv, st.ll = tick.volume, tick.listings
        return _Metrics(ready, r, z_adj, crossover, confirmed, rvol, supply, basis, w_l,
                        med, scale, redelisted)

    def _fresh(self, tick: ItemTick) -> _ItemState:
        return _ItemState(self.cfg.window, tick.price, tick.ts,
                          volume=tick.volume, listings=tick.listings)

    def reset(self) -> None:
        self._states.clear()
        self.ready_keys.clear()
        self._z_pool.clear()
        self._cat_rt.clear()
        self._cat_rt_n.clear()
        self._cat_mad_cache.clear()
        self._disappeared.clear()
        self._last_snap.clear()
        self._snap_interval.clear()
        self._snap_size.clear()
        for pool in self._tod_pool:
            pool.clear()
        self._tod_n = [0, 0, 0, 0]
        self._tod_cache = (-1, (1.0, 1.0, 1.0, 1.0))

    def warm_start(self, ticks: Iterable[ItemTick], now: Optional[float] = None) -> int:
        """Replay stored history (any order) to rebuild baselines silently."""
        return self.warm_start_stream(sorted(ticks, key=lambda t: t.ts), now)

    def warm_start_stream(self, ticks_sorted: Iterable[ItemTick],
                          now: Optional[float] = None) -> int:
        """Replay ts-ascending ticks with O(1) memory — pairs with
        HistoryStore.iter_since so a large history streams instead of being
        materialized in RAM first."""
        count = 0
        for tick in ticks_sorted:
            try:
                if tick.price <= 0:        # delisted marker (C10): keep the fact
                    self._observe_disappearance(tick.key, tick.ts)
                    count += 1
                    continue
                t_days = self.phase.league_day(now or tick.ts,
                                               self._league_start(tick.key.split(":", 1)[0]))
                hf, hs = self.phase.half_lives(t_days)
                st = self._states.get(tick.key)
                if st is None or (tick.ts - st.last_ts) / 3600.0 > self.cfg.max_gap_hours:
                    self._states[tick.key] = self._fresh(tick)
                else:
                    m = self._advance(st, tick, hf, hs)
                    if m is not None and m.ready:
                        self.ready_keys.add(tick.key)   # baseline survives a restart
                        self._observe_z(tick.category, abs(m.z_adj))
            except (TypeError, ValueError, OverflowError):
                continue                   # corrupt stored row must not kill warm start
            count += 1
        return count

    def prune_states(self, older_than_ts: float) -> int:
        """Drop baselines for items not seen since older_than_ts — item churn
        (new league variants, removed items) must not grow the state dict
        unboundedly within a league."""
        stale = [k for k, st in self._states.items() if st.last_ts < older_than_ts]
        for k in stale:
            del self._states[k]
            self.ready_keys.discard(k)
        stale_d = [k for k, ts in self._disappeared.items() if ts < older_than_ts]
        for k in stale_d:
            del self._disappeared[k]
        return len(stale)

    def process(self, snap: Snapshot, collect: Optional[list] = None) -> List[Anomaly]:
        c = self.cfg
        t_days = self.phase.league_day(snap.ts, self._league_start(snap.game))
        hf, hs = self.phase.half_lives(t_days)
        phase = self.phase.label(t_days)
        self._observe_snapshot_meta(snap.category, snap.ts, len(snap.ticks))
        # heavy-tail guard: the empirical category cap lifts z* above the phase
        # model whenever the raw |z| distribution demands it (B3)
        z_star = max(self.phase.z_threshold(t_days), self._z_threshold_category(snap.category))

        rows: List[Tuple[ItemTick, float, _Metrics]] = []
        for tick in snap.ticks:
            try:
                if tick.price <= 0:        # delisted marker (C10): keep the fact,
                    self._observe_disappearance(tick.key, tick.ts)
                    continue               # never feed the stale/zero price forward
                st = self._states.get(tick.key)
                if st is None or (tick.ts - st.last_ts) / 3600.0 > c.max_gap_hours:
                    self._states[tick.key] = self._fresh(tick)
                    continue
                prev = st.last_price
                m = self._advance(st, tick, hf, hs)
                if m is not None:
                    if m.ready:
                        self.ready_keys.add(tick.key)
                    if m.ready:
                        rows.append((tick, prev, m))
            except Exception:              # one bad item must not stop the batch
                log.exception("Analyzer failed on %s", tick.key)
        if not rows:
            return []

        # plan item B6: split the market-wide component from item-specific moves.
        # Only a coherent drift (most rows moving the same way) is a market
        # component; in a split category the median can swallow a real item
        # signal. Small cohorts cannot separate a drift at all.
        cat_med_r, cat_scale = 0.0, 0.0
        if len(rows) >= MARKET_MIN_ROWS:
            up = sum(1 for _, _, m in rows if m.r > 0)
            coherent = max(up, len(rows) - up) / len(rows) >= MARKET_COHERENCE
            if coherent:
                cat_med_r = _median(m.r for _, _, m in rows)
                cat_scale = max(_median(m.scale for _, _, m in rows), EPS)
                if (abs(cat_med_r) >= z_star * cat_scale
                        and abs(cat_med_r) >= c.min_log_move):
                    # whole category shifted: one event instead of N alerts
                    return [self._market_anomaly(snap, cat_med_r, z_star, cat_scale,
                                                 t_days, phase, rows)]

        for _, _, m in rows:               # residualize in place (B6)
            m.z_adj = (m.r - cat_med_r - m.med) / m.scale * math.sqrt(m.w_l)

        pos = sum(1 for _, _, m in rows if m.z_adj >= c.breadth_z)
        neg = sum(1 for _, _, m in rows if m.z_adj <= -c.breadth_z)
        peers = len(rows) - 1

        out: List[Anomaly] = []
        for tick, prev, m in rows:
            self._observe_z(snap.category, abs(m.z_adj))   # feed the empirical pool first
            direction = 1 if m.z_adj > 0 else -1
            breadth: Optional[float] = None
            if peers >= c.breadth_min_peers:
                same = (pos if direction > 0 else neg) - 1    # exclude the item itself
                breadth = max(0, same) / peers
            manip = self._manipulation(m, breadth)
            if collect is not None:
                collect.append((self._features(m, breadth), manip,
                                tick.key, tick.ts, tick.price, m.r))
            if abs(m.z_adj) < z_star or abs(m.r - cat_med_r) < c.min_log_move:
                continue
            label = ("SUSPECT" if manip >= c.suspect_at
                     else "ORGANIC" if manip <= c.organic_at else "AMBIGUOUS")
            rv = m.rvol if m.rvol is not None else 1.0
            priority = alert_priority(abs(tick.price - prev), rv, m.z_adj)
            out.append(Anomaly(
                key=tick.key, name=tick.name, game=snap.game,
                category=tick.category, ts=tick.ts,
                price=tick.price, prev_price=prev, pct=(tick.price / prev - 1.0) * 100.0,
                direction=direction, z_adj=m.z_adj, z_star=z_star, rvol=m.rvol,
                crossover=m.crossover, confirmed=m.confirmed, supply_ratio=m.supply,
                basis_dev=m.basis, breadth=breadth, manip=manip, label=label,
                priority=priority, league_day=t_days, phase=phase))
        out.sort(key=lambda a: a.priority, reverse=True)
        return out

    def _market_anomaly(self, snap: Snapshot, cat_med_r: float, z_star: float,
                        cat_scale: float, t_days: float, phase: str, rows) -> Anomaly:
        """One category-wide event instead of N per-item alerts (plan item B6).

        Excluded from the calibration collect path on purpose: a market drift
        says nothing about an item's own manipulation features.
        """
        med_price = _median(t.price for t, _, _ in rows) or 1.0
        z = cat_med_r / cat_scale
        return Anomaly(
            key=f"{snap.category}#market",
            name=f"Market move · {snap.category.rsplit(':', 1)[-1]}",
            game=snap.game, category=snap.category, ts=snap.ts,
            price=med_price, prev_price=med_price / math.exp(cat_med_r),
            pct=(math.exp(cat_med_r) - 1.0) * 100.0,
            direction=1 if cat_med_r > 0 else -1,
            z_adj=z, z_star=z_star, rvol=None, crossover=0.0, confirmed=False,
            supply_ratio=None, basis_dev=None, breadth=None, manip=0.0,
            label="ORGANIC", priority=3.0 + abs(z), league_day=t_days, phase=phase)

    def _features(self, m: _Metrics, breadth: Optional[float]) -> Tuple[float, ...]:
        """Six normalized manipulation features in [0, 1] (spec section 4)."""
        c = self.cfg
        f1 = 1.0 - m.w_l
        f2 = _clip(1.0 - m.rvol / c.rvol_ref, 0.0, 1.0) if m.rvol is not None else c.no_volume_prior
        # plan item D13: supply pressure is symmetric - listings vanishing into
        # a pump AND listings flooding into a dump are both synthetic pressure
        f3 = 0.0
        if m.supply is not None:
            if m.r > 0:
                f3 = _clip((1.0 - m.supply) / 0.5, 0.0, 1.0)
            elif m.r < 0:
                f3 = _clip((m.supply - 1.0) / 0.5, 0.0, 1.0)
        if m.redelisted and m.r > 0:
            f3 = 1.0                   # vanished supply then repriced up: buyout signal
        f4 = _clip(m.basis / c.basis_saturation, 0.0, 1.0) if m.basis is not None else 0.0
        f5 = 0.0 if m.confirmed else 1.0
        f6 = 1.0 - breadth if breadth is not None else 0.5
        return (f1, f2, f3, f4, f5, f6)

    def _manipulation(self, m: _Metrics, breadth: Optional[float]) -> float:
        features = self._features(m, breadth)
        w = self._weights if self._weights is not None else self.cfg.weights
        b = self._bias if self._bias is not None else self.cfg.bias
        return _sigmoid(b + sum(wi * f for wi, f in zip(w, features)))


# ------------------------------------------------------------------ persistence
class HistoryStore:
    """SQLite tick history (WAL + busy_timeout + ts index).

    Lets the detector warm-start and lets you refit parameters (calibration.py).
    The explicit ``idx_ticks_ts`` index serves the ``WHERE ts >= ?`` range scans
    (load_since / prune); warm start itself replays full rows, so aggregation
    is deliberately kept in SQL-free Python where per-item EMA state is built.

    Size contract (files must not bloat over a <=4-month league):
      * ``append`` stores a row when the item's PRICE changed (prod QA: 88% of
        rows repeated the previous price via listing-count jitter); volume and
        listings ride along on the changed row;
      * hourly ``prune`` enforces the retention window, ``maintain`` truncates
        the WAL and hands freed pages back (incremental vacuum);
      * ``clear`` wipes everything on league rollover - history is
        league-scoped and a fresh VACUUM reclaims the file.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_raw: Dict[str, Tuple[float, Optional[float], Optional[float], float]] = {}
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=5000")
        self._con.execute("PRAGMA cache_size=-65536")     # 64 MB page cache
        self._con.execute("PRAGMA mmap_size=134217728")   # 128 MB memory-mapped I/O
        legacy = int(self._con.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0
        if legacy:
            # activates after the one-shot VACUUM below (new DBs get it directly)
            self._con.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS ticks (key TEXT, ts REAL, name TEXT, category TEXT, "
            "price REAL, volume REAL, listings REAL, aux REAL, PRIMARY KEY (key, ts))")
        # the implicit PK index leads with `key`, so time-range queries need their own
        self._con.execute("CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts)")
        self._con.commit()
        if legacy:
            try:
                self._con.execute("VACUUM")            # one-time: reclaim old bloat
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                log.info("history store upgraded to incremental vacuum (%s)", path.name)
            except sqlite3.Error as exc:
                log.info("store vacuum deferred: %s", exc)

    def append(self, ticks: Iterable[ItemTick]) -> None:
        """Store price CHANGES only (prod QA: 88% of stored rows repeated the
        previous price because listings/volume jitter every refresh). Volume
        and listings ride along with the changed row; delist markers (price 0)
        always go in."""
        rows = []
        for t in ticks:
            if t.price <= 0:
                rows.append((t.key, t.ts, t.name, t.category, t.price,
                             t.volume, t.listings, t.aux))
                self._last_raw.pop(t.key, None)
                continue
            last = self._last_raw.get(t.key)
            if last is not None and last[0] == t.price:
                continue
            rows.append((t.key, t.ts, t.name, t.category, t.price,
                         t.volume, t.listings, t.aux))
            self._last_raw[t.key] = (t.price, t.volume, t.listings, t.ts)
        if rows:
            with self._lock:
                self._con.executemany("INSERT OR IGNORE INTO ticks VALUES (?,?,?,?,?,?,?,?)",
                                      rows)
                self._con.commit()

    def load_since(self, since_ts: float, max_rows: Optional[int] = None) -> List[ItemTick]:
        """Materialized variant of iter_since (bounded when max_rows is given)."""
        return list(self.iter_since(since_ts, max_rows=max_rows))

    def iter_since(self, since_ts: float, max_rows: Optional[int] = None):
        """Stream ticks (key, name, category, price, volume, listings, aux, ts)
        ordered by ts with O(1) memory — the idx_ticks_ts index provides the
        order, so a multi-million-row history never has to be fetched whole.

        The DB lock is held while the generator is alive; it is released in
        ``finally`` when the generator is exhausted or closed, so consumers
        must drain it (or let it go out of scope). With ``max_rows`` only the
        most recent rows are kept (bounded buffer, lock held only for fetch).
        """
        if max_rows is not None and max_rows > 0:
            with self._lock:
                cur = self._con.execute(
                    "SELECT key, name, category, price, volume, listings, aux, ts "
                    "FROM ticks WHERE ts >= ? ORDER BY ts", (since_ts,))
                window: Deque = deque((row for row in cur), maxlen=max_rows)
            for row in window:
                yield ItemTick(*row)
            return
        self._lock.acquire()
        try:
            cur = self._con.execute(
                "SELECT key, name, category, price, volume, listings, aux, ts "
                "FROM ticks WHERE ts >= ? ORDER BY ts", (since_ts,))
            for row in cur:
                yield ItemTick(*row)
        finally:
            self._lock.release()

    def count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0])

    def prune(self, older_than_ts: float) -> None:
        with self._lock:
            self._con.execute("DELETE FROM ticks WHERE ts < ?", (older_than_ts,))
            stale = [k for k, v in self._last_raw.items() if v[3] < older_than_ts]
            for k in stale:
                del self._last_raw[k]
            self._con.commit()

    def maintain(self) -> None:
        """Keep sidecar files tight: truncate the WAL and hand freed pages back
        to the OS (incremental vacuum; no-op while auto_vacuum is inactive)."""
        with self._lock:
            try:
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._con.execute("PRAGMA incremental_vacuum(8192)")
            except sqlite3.Error as exc:
                log.debug("store maintain skipped: %s", exc)

    def clear(self) -> int:
        """League-scoped wipe: drop every stored tick and reclaim the file.
        Called on league rollover / manual league switch - history never
        crosses a league, so the store is bounded by the league's lifetime."""
        with self._lock:
            n = int(self._con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0])
            self._con.execute("DELETE FROM ticks")
            self._last_raw.clear()
            self._con.commit()
        try:
            with self._lock:
                self._con.execute("VACUUM")
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log.debug("store vacuum skipped: %s", exc)
        return n

    def close(self) -> None:
        with self._lock:
            self._con.close()


# --------------------------------------------------------------- alert governor
@dataclass(slots=True)
class _Cool:
    k: int
    last_ts: float
    last_priority: float


class AlertGovernor:
    """Token bucket + escalating per-item cooldown + digest of what did not fit."""

    def __init__(self, cfg, now_fn: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._now = now_fn
        self._tokens = float(cfg.bucket_capacity)
        self._t0 = now_fn()
        self._cool: Dict[str, _Cool] = {}
        self._muted: Dict[str, float] = {}     # key -> mute until ts (one-click snooze)

    def reset(self) -> None:
        self._cool.clear()
        self._muted.clear()
        self._tokens = float(self.cfg.bucket_capacity)
        self._t0 = self._now()

    def mute(self, key: str, minutes: float = 30.0) -> None:
        """One-click snooze: suppress alerts for this item until the TTL ends
        (not shown, not digested - the user asked for silence)."""
        self._muted[key] = self._now() + max(1.0, float(minutes)) * 60.0

    def unmute(self, key: str) -> None:
        self._muted.pop(key, None)

    def _cooldown(self, k: int) -> float:
        return min(self.cfg.cooldown_max_s, self.cfg.cooldown_base_s * 2 ** max(0, k - 1))

    def decide(self, anomalies: List[Anomaly]) -> Tuple[List[Anomaly], Optional[Digest]]:
        cfg, now = self.cfg, self._now()
        self._tokens = min(float(cfg.bucket_capacity),
                           self._tokens + (now - self._t0) / cfg.refill_seconds)
        self._t0 = now

        # one candidate per item: the robust and basic engines may both flag the
        # same key in one batch; only the highest-priority one may compete
        best: Dict[str, Anomaly] = {}
        for a in anomalies:
            cur = best.get(a.key)
            if cur is None or a.priority > cur.priority:
                best[a.key] = a

        eligible: List[Anomaly] = []
        for a in sorted(best.values(), key=lambda x: x.priority, reverse=True):
            until = self._muted.get(a.key)
            if until is not None:
                if now < until:
                    continue               # one-click snooze active (no shown, no digest)
                del self._muted[a.key]
            cool = self._cool.get(a.key)
            if cool is not None:
                age = now - cool.last_ts
                if age > 2.0 * self._cooldown(cool.k):
                    del self._cool[a.key]          # quiet long enough: start over
                    cool = None
            if (cool is None or age >= self._cooldown(cool.k)
                    or a.priority >= cfg.escalation_factor * cool.last_priority):
                eligible.append(a)

        shown: List[Anomaly] = []
        overflow: List[Anomaly] = []
        for a in eligible:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                shown.append(a)
                prev = self._cool.get(a.key)
                self._cool[a.key] = _Cool((prev.k + 1) if prev else 1, now, a.priority)
            else:
                overflow.append(a)

        digest: Optional[Digest] = None
        if overflow:
            counts: Dict[str, int] = {}
            for a in overflow:
                counts[a.category] = counts.get(a.category, 0) + 1
            digest = Digest(
                count=len(overflow),
                names=tuple(a.name for a in overflow[: self.cfg.max_digest_names]),
                categories=tuple(sorted(counts.items(), key=lambda kv: -kv[1])),
                items=tuple(DigestItem(a.key, a.name, a.category, a.price,
                                       a.prev_price, a.pct, a.direction,
                                       a.label, a.priority)
                            for a in overflow[:DIGEST_MAX_ITEMS]))
        return shown, digest


# -------------------------------------------------------------------- pipeline
class AnalysisPipeline:
    """store -> analyze -> govern. One instance per game."""

    def __init__(self, detector_cfg, alerts_cfg, league_start_fn: Callable[[str], str],
                 db_path: Path) -> None:
        self.cfg = detector_cfg
        self.analyzer = AnomalyAnalyzer(detector_cfg, league_start_fn)
        self.governor = AlertGovernor(alerts_cfg)
        self.store = HistoryStore(db_path)
        self._last_prune = 0.0
        loaded = self.analyzer.warm_start_stream(self.store.iter_since(
            time.time() - detector_cfg.history_retention_days * 86400.0))
        log.info("Warm start replayed %d stored ticks (%s)", loaded, db_path.name)

    def ingest(self, ticks: Iterable[ItemTick]) -> None:
        """Storage-only path (basic engine): append + the same hourly prune guard
        that `handle` applies, so the DB cannot grow unbounded in basic mode."""
        self.store.append(ticks)
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        if time.time() - self._last_prune > 3600.0:
            cutoff = time.time() - self.cfg.history_retention_days * 86400.0
            self.store.prune(cutoff)
            dropped = self.analyzer.prune_states(cutoff)
            if dropped:
                log.info("pruned %d stale item baselines", dropped)
            self.store.maintain()                  # truncate WAL, release freed pages
            self._last_prune = time.time()

    def handle(self, snap: Snapshot,
               extra_anomalies: Optional[List[Anomaly]] = None,
               collect_anomalies: Optional[List[Anomaly]] = None
               ) -> Tuple[List[Anomaly], Optional[Digest]]:
        self.store.append(snap.ticks)
        self._maybe_prune()
        anomalies = self.analyzer.process(snap)
        if extra_anomalies:
            anomalies = list(anomalies) + list(extra_anomalies)
            anomalies.sort(key=lambda a: a.priority, reverse=True)
        if collect_anomalies is not None:
            # full pre-governor list (scenario engine evaluates everything)
            collect_anomalies.extend(anomalies)
        return self.governor.decide(anomalies)

    def close(self) -> None:
        self.store.close()


# -------------------------------------------------------------- tick adapter
def ticks_from_quotes(quotes, ts: float) -> List[ItemTick]:
    """Convert api_client.ItemQuote batches (duck-typed) into spec ItemTicks.

    Exchange quotes carry volume (volumePrimaryValue) and the stress ratio
    aux = maxVolumeRate / primaryValue; item quotes carry listings and the
    sample count as a volume proxy.
    """
    ticks: List[ItemTick] = []
    for q in quotes:
        try:
            price = float(q.price)
            if not math.isfinite(price) or price <= 0:
                # plan item C10: a delisted marker travels as a zero-price tick
                # (the stale price must never leak into a baseline); the engine
                # records the disappearance and stores the fact in history
                if price == 0.0 and getattr(q, "delisted", False):
                    ticks.append(ItemTick(
                        key=str(q.key), name=str(q.name),
                        category=f"{q.game}:{q.source}:{q.category}",
                        price=0.0, volume=None, listings=0.0, aux=None, ts=ts))
                continue
            # a NaN/inf in any secondary field would poison medians/EMAs downstream
            volume = float(q.volume) if getattr(q, "volume", None) is not None else None
            listings = float(q.list_count) if q.list_count is not None else None
            aux = float(q.aux) if getattr(q, "aux", None) is not None else None
            ticks.append(ItemTick(
                key=str(q.key),
                name=str(q.name),
                category=f"{q.game}:{q.source}:{q.category}",
                price=price,
                volume=volume if volume is None or math.isfinite(volume) else None,
                listings=listings if listings is None or math.isfinite(listings) else None,
                aux=aux if aux is None or math.isfinite(aux) else None,
                ts=ts,
            ))
        except (TypeError, ValueError, AttributeError):
            continue
    return ticks


def anomaly_from_basic(alert, league: str, median_price: float = 1.0) -> Anomaly:
    """Wrap a basic delta-engine Alert as an Anomaly so both engines share one governor."""
    rv = alert.vol_ratio if alert.vol_ratio is not None and alert.vol_ratio > 1.0 else 1.0
    priority = alert_priority(abs(alert.new_price - alert.old_price), rv, 0.0)
    return Anomaly(
        key=alert.key, name=alert.name, game=alert.game,
        category=f"{alert.game}:{alert.source}:{alert.category}",
        ts=alert.ts, price=alert.new_price, prev_price=alert.old_price,
        pct=alert.pct, direction=1 if alert.pct >= 0 else -1,
        z_adj=0.0, z_star=0.0, rvol=alert.vol_ratio, crossover=0.0,
        confirmed=False, supply_ratio=None, basis_dev=None, breadth=None,
        manip=0.5, label="AMBIGUOUS", priority=priority,
        league_day=0.0, phase=f"basic · {league}",
    )
