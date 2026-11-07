"""scenarios.py - user-defined alert scenarios (Scenarios tab in Settings).

A scenario is a lightweight rule evaluated against every anomaly produced by
either engine, independent of the governor's token bucket:

    match    : game + category ("*" = any) + optional case-insensitive
               substring on the item name
    trigger  : metric (pct | z_abs | rvol | priority) compared with >= / <=
               against a threshold, optionally restricted to a direction
    cooldown : per (scenario, item key) so one item cannot spam a scenario

Scenario hits are dispatched by the controller as normal alerts annotated
with the scenario name (toast badge, journal label, webhook text).

Standard library only, so it can be unit-tested without Qt or network.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("ninjaalert.scenarios")

METRIC_FIELDS = {"pct": "pct", "z_abs": "z_adj", "rvol": "rvol", "priority": "priority"}
COOLDOWN_PRUNE = 4096          # entries before a stale-entry sweep
PRUNE_HORIZON_S = 7 * 86400.0


@dataclass(slots=True)
class ScenarioHit:
    scenario: str              # scenario name
    key: str                   # item key (cooldown identity = (scenario, key))
    anomaly: Any               # models.Anomaly (untyped: stdlib-only module)
    metric_value: float


class ScenarioEngine:
    """Evaluates ScenarioCfg objects against anomaly batches (worker thread)."""

    def __init__(self) -> None:
        self._last: Dict[Tuple[str, str], float] = {}

    def reset(self) -> None:
        self._last.clear()

    def evaluate(self, scenarios: Iterable[Any], anomalies: List[Any],
                 now: Optional[float] = None) -> List[ScenarioHit]:
        now = time.time() if now is None else now
        if len(self._last) > COOLDOWN_PRUNE:
            horizon = now - PRUNE_HORIZON_S
            self._last = {k: t for k, t in self._last.items() if t >= horizon}

        hits: List[ScenarioHit] = []
        for sc in scenarios:
            if not getattr(sc, "enabled", False):
                continue
            for a in anomalies:
                try:
                    hit = self._match(sc, a, now)
                except Exception:          # one bad anomaly must not stop the sweep
                    log.exception("scenario evaluation failed (%s)", getattr(sc, "name", "?"))
                    continue
                if hit is not None:
                    hits.append(hit)
        return hits

    def _match(self, sc, a, now: float) -> Optional[ScenarioHit]:
        if a.game != sc.game:
            return None
        if sc.category != "*" and str(a.category).rsplit(":", 1)[-1] != sc.category:
            return None
        if sc.name_pattern and sc.name_pattern.lower() not in str(a.name).lower():
            return None
        if sc.direction == "up" and a.direction <= 0:
            return None
        if sc.direction == "down" and a.direction >= 0:
            return None
        field = METRIC_FIELDS.get(sc.metric)
        if field is None:
            return None
        raw = getattr(a, field, None)
        if raw is None:
            return None
        value = abs(float(raw)) if sc.metric == "z_abs" else float(raw)
        if sc.op == ">=" and not value >= sc.value:
            return None
        if sc.op == "<=" and not value <= sc.value:
            return None
        ident = (sc.name, a.key)
        last = self._last.get(ident)
        if last is not None and (now - last) < sc.cooldown_minutes * 60.0:
            return None
        self._last[ident] = now
        return ScenarioHit(scenario=sc.name, key=a.key, anomaly=a, metric_value=value)
