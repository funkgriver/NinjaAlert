"""models.py - immutable data contracts shared by every module (standard library only).

Spec: "PoE Market Anomaly Detector" (Nov 2026). Only immutable dataclasses
cross thread boundaries, and only through queued Qt signals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True, slots=True)
class ItemTick:
    """One normalized observation of one item, identical for PoE 1 and PoE 2."""
    key: str                    # globally unique: "<game>:<kind>:<type>:<id>"
    name: str
    category: str               # "<game>:<kind>:<type>", the cohort used for breadth and medians
    price: float                # in whatever unit poe.ninja quotes; only ratios are ever used
    volume: Optional[float]     # traded volume or poe.ninja's sample count; None when absent
    listings: Optional[float]   # current listing count; None for exchange endpoints
    aux: Optional[float]        # stress ratio (maxVolumeRate/price); None when absent
    ts: float                   # unix seconds when the snapshot was fetched


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One successful poll of one category."""
    game: str
    category: str
    league: str
    ts: float
    source: str                 # which route produced it ("documented" or "legacy")
    ticks: Tuple[ItemTick, ...]


@dataclass(frozen=True, slots=True)
class Anomaly:
    key: str
    name: str
    game: str
    category: str
    ts: float
    price: float
    prev_price: float
    pct: float                  # percent change versus the previous snapshot
    direction: int              # +1 or -1
    z_adj: float
    z_star: float
    rvol: Optional[float]
    crossover: float
    confirmed: bool
    supply_ratio: Optional[float]
    basis_dev: Optional[float]
    breadth: Optional[float]
    manip: float                # manipulation score M in [0, 1]
    label: str                  # ORGANIC | AMBIGUOUS | SUSPECT
    priority: float
    league_day: float
    phase: str


@dataclass(frozen=True, slots=True)
class DigestItem:
    """One folded anomaly carried inside a digest (interactive toast row)."""
    key: str
    name: str
    category: str               # raw "<game>:<source>:<type>"
    price: float
    prev_price: float
    pct: float
    direction: int              # +1 up, -1 down
    label: str                  # ORGANIC | AMBIGUOUS | SUSPECT | SCENARIO·x
    priority: float


@dataclass(frozen=True, slots=True)
class Digest:
    """Summary for anomalies that did not get a token."""
    count: int
    names: Tuple[str, ...]
    categories: Tuple[Tuple[str, int], ...]
    items: Tuple[DigestItem, ...] = ()   # rows for the expandable toast (capped)
