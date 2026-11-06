"""NinjaAlert - sliding-window anomaly detector.

State per item key: a bounded deque of (timestamp, price, list_count) points.
A quote triggers an alert when:

  price jump : pct = (price - median(previous window)) / median * 100 >= jump_pct
               AND price >= min_price (per game, primary currency)
               AND price is a fresh high vs the last recorded point
  volume     : list_count >= volume_mult * mean(previous counts in window)
               AND list_count >= volume_min_count  (items only; exchange has none)

  mode "any" -> either condition fires
  mode "all" -> both must fire (volume becomes mandatory)

Guards:
  * seeding  - the first MIN_HISTORY points never alert (no alert storm on launch)
  * cooldown - same item not re-alerted within cooldown_minutes
  * reset    - switching game/league wipes history
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from api_client import FetchMeta, ItemQuote

log = logging.getLogger("ninjaalert.analyzer")

MAX_POINTS_PER_KEY = 48
MIN_HISTORY = 3          # prior points required before any alert can fire
MIN_VOL_POINTS = 2       # prior listing counts required for volume logic
PRICE_EPSILON = 1.001    # "new high" margin (+0.1%)


@dataclass
class Alert:
    game: str
    league: str
    category: str
    name: str
    old_price: float        # baseline (median of previous window)
    new_price: float
    pct: float              # change vs baseline, percent
    list_count: Optional[int]
    vol_ratio: Optional[float]  # current listings / average listings, or None
    icon: Optional[str]
    details_id: Optional[str]
    source: str
    spark_total_change: Optional[float] = None  # 7d trend % reported by the site
    ts: float = 0.0
    key: str = ""           # stable item key (pipeline glue)
    label: str = ""         # ORGANIC | AMBIGUOUS | SUSPECT (advanced engine only)
    phase: str = ""         # league phase label
    z_adj: float = 0.0      # robust z score (advanced engine only)


class AnomalyDetector:
    def __init__(self, thresholds) -> None:  # thresholds: appconfig.Thresholds
        self._t = thresholds
        self._hist: Dict[str, Deque[Tuple[float, float, Optional[int]]]] = {}
        self._last_alert: Dict[str, float] = {}
        self._target: Tuple[str, str] = ("", "")

    # ------------------------------------------------------------- control
    def update_thresholds(self, thresholds) -> None:
        self._t = thresholds

    def reset(self) -> None:
        self._hist.clear()
        self._last_alert.clear()
        self._target = ("", "")
        log.info("detector state reset")

    @property
    def tracked_count(self) -> int:
        return len(self._hist)

    # -------------------------------------------------------------- feed
    def feed(self, quotes: List[ItemQuote], meta: FetchMeta, now: Optional[float] = None) -> List[Alert]:
        now = time.time() if now is None else now
        target = (meta.game, meta.league)
        if target != self._target:
            log.info("target changed %s -> %s; resetting detector", self._target, target)
            self.reset()
            self._target = target

        alerts: List[Alert] = []
        for quote in quotes:
            alert = self._process(quote, meta, now)
            if alert:
                alerts.append(alert)
        return alerts

    # ------------------------------------------------------------ internals
    def _process(self, q: ItemQuote, meta: FetchMeta, now: float) -> Optional[Alert]:
        window = int(min(max(self._t.baseline_window, MIN_HISTORY), 24))
        hist = self._hist.get(q.key)
        if hist is None:
            hist = deque(maxlen=MAX_POINTS_PER_KEY)
            self._hist[q.key] = hist
        prior = list(hist)[-(window + 40):]  # working slice (points also feed volume avg)
        recent = prior[-window:]

        # always record the new observation
        hist.append((now, q.price, q.list_count))

        if len(recent) < MIN_HISTORY:
            return None  # still seeding

        baseline = statistics.median(p[1] for p in recent)
        if baseline <= 0:
            return None
        pct = (q.price - baseline) / baseline * 100.0

        # --- price condition -------------------------------------------------
        last_price = recent[-1][1]
        price_ok = (
            pct >= self._t.jump_pct
            and q.price >= self._t.min_price.get(q.game, 0.0)
            and q.price > last_price * PRICE_EPSILON
        )

        # --- volume condition ------------------------------------------------
        vol_ratio: Optional[float] = None
        volume_ok = False
        if q.list_count is not None and self._t.volume_mult > 0:
            prior_counts = [p[2] for p in recent if p[2] is not None]
            if len(prior_counts) >= MIN_VOL_POINTS:
                avg = sum(prior_counts) / len(prior_counts)
                if avg > 0:
                    vol_ratio = q.list_count / avg
                    volume_ok = (
                        q.list_count >= self._t.volume_min_count
                        and q.list_count >= avg * self._t.volume_mult
                    )

        if self._t.mode == "all":
            fire = price_ok and volume_ok
        else:  # "any"
            fire = price_ok or volume_ok
        if not fire:
            return None

        # --- cooldown --------------------------------------------------------
        last_alert_ts = self._last_alert.get(q.key)
        if last_alert_ts is not None and (now - last_alert_ts) < self._t.cooldown_minutes * 60.0:
            return None

        self._last_alert[q.key] = now
        alert = Alert(
            game=q.game,
            league=meta.league,
            category=q.category,
            name=q.name,
            old_price=baseline,
            new_price=q.price,
            pct=pct,
            list_count=q.list_count,
            vol_ratio=vol_ratio,
            icon=q.icon,
            details_id=q.details_id,
            source=q.source,
            spark_total_change=q.spark_total_change,
            ts=now,
            key=q.key,
        )
        log.info(
            "ALERT %s %s: %s %.2f -> %.2f (%+.1f%%)%s",
            q.game, q.category, q.name, baseline, q.price, pct,
            f" vol x{vol_ratio:.1f}" if vol_ratio else "",
        )
        return alert
