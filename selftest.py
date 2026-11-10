"""NinjaAlert headless self-test suite.

  python selftest.py          # offline: config, analyzer, parsers (fixtures), poller, Qt widgets
  python selftest.py --live   # additionally fetches live data from poe.ninja (both games)

Exit code 0 = all green, 1 = failures.
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

FAILURES: list = []


def check(name: str, cond: bool, info: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ------------------------------------------------------------------- config
def test_config() -> None:
    print("== config ==")
    from appconfig import Config

    cfg = Config.load_or_create(os.path.join(ROOT, "config.json"))
    check("config: default game", cfg.game_cfg("poe1").enabled and
          not cfg.game_cfg("poe2").enabled)
    check("config: watch populated", len(cfg.watched("poe1")) > 0 and len(cfg.watched("poe2")) > 0)
    check("config: watch ids valid",
          all(c in __import__("categories").CATEGORIES["poe1"] for c in cfg.watched("poe1")))

    cfg2 = Config()
    cfg2._merge({
        "game": "poe9", "poll_interval_minutes": "x",
        "thresholds": {"jump_pct": 55}, "overlay": {"corner": "nowhere"},
    })
    check("config: bad game ignored", cfg2.games["poe1"].league is None)
    check("config: jump_pct merged", cfg2.thresholds.jump_pct == 55.0)
    cfg2._sanitize()
    check("config: bad corner ignored", cfg2.overlay.corner == "bottom-right")
    check("config: games present", cfg2.game_cfg("poe1").enabled and
          not cfg2.game_cfg("poe2").enabled)

    # legacy v1 migration
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "v1.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"game": "poe1", "league": "Standard", "league_auto": False,
                       "poll_interval_minutes": 20,
                       "watch": {"poe1": ["Currency", "Fragment"]},
                       "alerts": {"sound": False}}, f)
        migrated = Config.load(p)
        gc = migrated.game_cfg("poe1")
        check("config: v1 league migrated", gc.league == "Standard" and not gc.league_auto)
        check("config: v1 interval migrated", gc.poll_interval_minutes == 20.0)
        check("config: v1 watch migrated", gc.watch == ["Currency", "Fragment"])
        check("config: v1 sound migrated", migrated.alerts.sound is False)
        check("config: v1 poe2 untouched", not migrated.game_cfg("poe2").enabled)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "config.json")
        c = Config()
        c.game_cfg("poe2").enabled = True
        c.network.contact = "me@example.com"
        c.save(p)
        c2 = Config.load(p)
        check("config: save/load round-trip",
              c2.game_cfg("poe2").enabled and c2.network.contact == "me@example.com")


# ----------------------------------------------------------------- analyzer
def test_analyzer() -> None:
    print("== analyzer ==")
    from analyzer import AnomalyDetector
    from api_client import FetchMeta, ItemQuote
    from appconfig import Config

    cfg = Config()
    cfg.thresholds.jump_pct = 20.0
    cfg.thresholds.min_price = {"poe1": 5.0, "poe2": 0.05}
    cfg.thresholds.volume_mult = 3.0
    cfg.thresholds.volume_min_count = 10.0
    cfg.thresholds.mode = "any"
    cfg.thresholds.cooldown_minutes = 90.0

    meta = FetchMeta(game="poe1", league="Allflame", category="Currency",
                     source="exchange", primary="chaos",
                     secondary="divine", rates={"divine": 0.002353})

    def q(name: str, price: float, lc=None) -> ItemQuote:
        return ItemQuote(key=f"k:{name}", name=name, game="poe1", category="Currency",
                         source="exchange", price=price, list_count=lc, icon=None,
                         details_id=name)

    t0 = 1_000_000.0

    det = AnomalyDetector(cfg.thresholds)
    check("analyzer: seed 1", not det.feed([q("Divine Orb", 100.0)], meta, t0))
    check("analyzer: seed 2", not det.feed([q("Divine Orb", 101.0)], meta, t0 + 900))
    check("analyzer: seed 3", not det.feed([q("Divine Orb", 99.0)], meta, t0 + 1800))

    a4 = det.feed([q("Divine Orb", 150.0)], meta, t0 + 2700)
    check("analyzer: +50% spike alerts", len(a4) == 1 and abs(a4[0].pct - 50.0) < 3.0,
          f"{[round(a.pct, 1) for a in a4]}")
    check("analyzer: baseline is median", a4 and abs(a4[0].old_price - 100.0) < 1.0)

    a5 = det.feed([q("Divine Orb", 220.0)], meta, t0 + 2800)
    check("analyzer: cooldown suppresses re-alert", len(a5) == 0)

    # plateau after cooldown must not re-alert; new high must
    det2 = AnomalyDetector(cfg.thresholds)
    ts = t0
    for p in (100.0, 100.0, 100.0):
        det2.feed([q("Divine Orb", p)], meta, ts)
        ts += 900
    det2.feed([q("Divine Orb", 150.0)], meta, ts)
    ts += cfg.thresholds.cooldown_minutes * 60
    check("analyzer: plateau does not re-alert",
          not det2.feed([q("Divine Orb", 150.0)], meta, ts))
    ts += 900
    check("analyzer: new high after cooldown alerts",
          len(det2.feed([q("Divine Orb", 180.0)], meta, ts)) == 1)

    # min price filter
    det3 = AnomalyDetector(cfg.thresholds)
    ts = t0
    for _ in range(3):
        det3.feed([q("Chalk", 1.0)], meta, ts)
        ts += 900
    check("analyzer: min_price filters dust", not det3.feed([q("Chalk", 2.0)], meta, ts))

    # volume-only alert (item data has listing counts)
    meta_i = FetchMeta(game="poe1", league="Allflame", category="UniqueWeapon",
                       source="item", primary="chaos")

    def qi(name: str, price: float, lc: int) -> ItemQuote:
        return ItemQuote(key=f"i:{name}", name=name, game="poe1", category="UniqueWeapon",
                         source="item", price=price, list_count=lc, icon=None,
                         details_id=name)

    det4 = AnomalyDetector(cfg.thresholds)
    ts = t0
    for lc in (10, 12, 11):
        det4.feed([qi("Headhunter", 2000.0, lc)], meta_i, ts)
        ts += 900
    a_vol = det4.feed([qi("Headhunter", 2050.0, 60)], meta_i, ts)
    check("analyzer: volume surge alerts",
          len(a_vol) == 1 and a_vol[0].vol_ratio is not None and a_vol[0].vol_ratio > 4)

    # mode "all" blocks price-only spikes when no volume data exists
    cfg_all = Config()
    cfg_all.thresholds.mode = "all"
    det5 = AnomalyDetector(cfg_all.thresholds)
    ts = t0
    for p in (100.0, 100.0, 100.0):
        det5.feed([q("Mirror", p)], meta, ts)
        ts += 900
    check("analyzer: mode=all blocks price-only (no volume data)",
          not det5.feed([q("Mirror", 150.0)], meta, ts))

    # game/league switch resets state
    meta2 = FetchMeta(game="poe1", league="Standard", category="Currency",
                      source="exchange", primary="chaos")
    check("analyzer: league switch re-seeds silently",
          not det.feed([q("Divine Orb", 99.0)], meta2, t0 + 99_000))


# ------------------------------------------------------------------ parsers
def test_parsers() -> None:
    print("== api parsers (fixtures) ==")
    from api_client import FetchMeta, NinjaApiClient, format_price

    fx = os.path.join(ROOT, "tests", "fixtures")
    client = NinjaApiClient()

    payload = json.load(open(os.path.join(fx, "poe1_exchange_currency.json"), encoding="utf-8"))
    meta, quotes = client._parse_exchange(
        "poe1", "Allflame", client._cat("poe1", "Currency"), payload)
    check("exchange poe1: primary is chaos", meta.primary == "chaos")
    check("exchange poe1: divine rate present", (meta.rates.get("divine") or 0) > 0)
    check("exchange poe1: quotes parsed", len(quotes) > 10, str(len(quotes)))
    check("exchange poe1: names resolved", quotes[0].name != quotes[0].details_id, quotes[0].name)
    check("exchange poe1: keys unique", len({x.key for x in quotes}) == len(quotes))

    payload = json.load(open(os.path.join(fx, "poe1_item_overview.json"), encoding="utf-8"))
    meta, quotes = client._parse_item(
        "poe1", "Allflame", client._cat("poe1", "UniqueWeapon"), payload)
    check("item poe1: chaosValue parsed", len(quotes) > 10 and quotes[0].price > 0)
    check("item poe1: listingCount captured", any(q.list_count for q in quotes))
    check("item poe1: icons absolute",
          all(q.icon is None or q.icon.startswith("http") for q in quotes))

    payload = json.load(open(os.path.join(fx, "poe2_exchange_currency.json"), encoding="utf-8"))
    meta, quotes = client._parse_exchange(
        "poe2", "Forbidden Rites", client._cat("poe2", "Currency"), payload)
    check("exchange poe2: primary is divine", meta.primary == "divine")
    check("exchange poe2: quotes parsed", len(quotes) > 5)

    payload = json.load(open(os.path.join(fx, "poe2_item_overview.json"), encoding="utf-8"))
    meta, quotes = client._parse_item(
        "poe2", "Forbidden Rites", client._cat("poe2", "UniqueWeapons"), payload)
    check("item poe2: primaryValue parsed", len(quotes) > 5)

    m1 = FetchMeta(game="poe1", league="Allflame", category="Currency", source="exchange",
                   primary="chaos", rates={"divine": 0.002353})
    check("format: chaos label", format_price(18.4, m1).startswith("18.4c"), format_price(18.4, m1))
    check("format: div hint", "div" in format_price(500.0, m1), format_price(500.0, m1))
    m2 = FetchMeta(game="poe2", league="Forbidden Rites", category="Currency", source="exchange",
                   primary="divine", rates={"exalted": 652.3})
    check("format: poe2 divine", format_price(1.0, m2).startswith("1div"), format_price(1.0, m2))
    check("format: poe2 ex hint", "ex" in format_price(0.01, m2), format_price(0.01, m2))

    # price display modes (chaos / divine / both)
    check("price: primary only", format_price(500.0, m1, "primary") == "500c",
          format_price(500.0, m1, "primary"))
    check("price: secondary converts chaos->div",
          format_price(500.0, m1, "secondary").endswith("div"),
          format_price(500.0, m1, "secondary"))
    check("price: both keeps legacy hint",
          "≈" in format_price(500.0, m1, "both")
          and "div" in format_price(500.0, m1, "both"))
    check("price: no hint for sub-1-unit conversions",
          "≈" not in format_price(100.0, m1, "both"), format_price(100.0, m1, "both"))
    check("price: secondary without rate falls back",
          format_price(5.0, FetchMeta(game="poe1", league="L", category="Currency",
                                      source="exchange", primary="chaos"),
                       "secondary").endswith("c"))
    check("price: poe2 secondary uses exalted",
          format_price(1.0, m2, "secondary").endswith("ex"),
          format_price(1.0, m2, "secondary"))

    from appconfig import VALID_PRICE_DISPLAY, Config
    cfgpd = Config()
    cfgpd.alerts.price_display = "bogus"
    cfgpd._sanitize()
    check("price: bad display sanitized to both",
          cfgpd.alerts.price_display == "both"
          and cfgpd.alerts.price_display in VALID_PRICE_DISPLAY)

    from ui_overlay import _league_slug
    check("slug: Allflame", _league_slug("Allflame") == "allflame")
    check("slug: HC league", _league_slug("HC Forbidden Rites") == "forbiddenriteshc")



# ------------------------------------------------------------------- poller
def test_poller() -> None:
    print("== poller (QThread cycle, fake client) ==")
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)

        from api_client import FetchMeta, ItemQuote, League
        from appconfig import Config
        from poller import PollWorker

        class FakeClient:
            def get_leagues(self, game, force=False):
                return [League(id="Allflame", name="Allflame"),
                        League(id="Standard", name="Standard")]

            def fetch_category(self, game, league, cat):
                meta = FetchMeta(game=game, league=league, category=cat, source="exchange",
                                 primary="chaos", secondary="divine", rates={"divine": 0.002})
                quote = ItemQuote(key=f"{game}:x:{cat}:1", name="Divine Orb", game=game,
                                  category=cat, source="exchange", price=100.0,
                                  list_count=None, icon=None, details_id="divine-orb",
                                  volume=42.0, aux=0.9)
                return [quote], meta

        cfg = Config()
        cfg.game_cfg("poe1").poll_interval_minutes = 5
        cfg.game_cfg("poe1").watch = ["Currency"]
        worker = PollWorker(FakeClient(), cfg, "poe1")
        got = {"snapshots": [], "leagues": [], "states": [], "cats": []}
        worker.snapshot.connect(lambda q, m: got["snapshots"].append((q, m)))
        worker.leagues_found.connect(lambda g, ls: got["leagues"].append((g, ls)))
        worker.state_changed.connect(lambda s: got["states"].append(s))
        worker.category_done.connect(lambda g, label, n: got["cats"].append((g, label, n)))
        worker.start()
        QTimer.singleShot(1200, worker.stop)
        deadline = time.time() + 8
        while time.time() < deadline and worker.isRunning():
            app.processEvents()
            time.sleep(0.05)
        check("poller: cycle produced snapshot", len(got["snapshots"]) >= 1)
        check("poller: leagues emitted", bool(got["leagues"]) and got["leagues"][0][1][0] == "Allflame")
        check("poller: category done", bool(got["cats"]))
        check("poller: fetching state seen", "fetching" in got["states"])
    except Exception as exc:
        check("poller: cycle ran", False, f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------- advanced (spec) tests
def test_advanced() -> None:
    print("== advanced analyzer (spec math) ==")
    try:
        from pathlib import Path
        import tempfile

        from advanced_analyzer import (AlertGovernor, AnalysisPipeline, AnomalyAnalyzer,
                                       HistoryStore, PhaseModel, _ItemState, _Metrics,
                                       anomaly_from_basic, ticks_from_quotes)
        from appconfig import Alerts, Config, DetectorCfg
        from models import Digest, ItemTick, Snapshot

        # ---- 1. phase model vs the spec's table --------------------------
        cfg = DetectorCfg()
        pm = PhaseModel(cfg)
        z0 = pm.z_threshold(0.0)
        check("phase: z*(day 0) ~ 5.0", abs(z0 - 5.0) < 0.01, f"{z0:.3f}")
        check("phase: z*(day 3) ~ 4.30", abs(pm.z_threshold(3.0) - 4.30) < 0.02,
              f"{pm.z_threshold(3.0):.3f}")
        check("phase: z*(day 28) ~ 3.05", abs(pm.z_threshold(28.0) - 3.05) < 0.03,
              f"{pm.z_threshold(28.0):.3f}")
        check("phase: z*(day 70) ~ 3.9 (late-league term)",
              3.8 < pm.z_threshold(70.0) < 4.1, f"{pm.z_threshold(70.0):.3f}")
        hf0, hs0 = pm.half_lives(0.0)
        check("phase: half-lives at day 0 == (4, 24)", abs(hf0 - 4.0) < 1e-9 and abs(hs0 - 24.0) < 1e-9)
        hf56, hs56 = pm.half_lives(56.0)
        check("phase: half-lives at day 56 ~ (12, 72)",
              abs(hf56 - 12.0) < 0.1 and abs(hs56 - 72.0) < 0.2, f"({hf56:.2f},{hs56:.2f})")
        check("phase: labels", pm.label(2) == "Launch" and pm.label(10) == "Stabilization"
              and pm.label(40) == "Mature" and pm.label(80) == "Decay")

        # ---- 2. manipulation score calibration (spec section 4) ----------
        def metrics(w_l, rvol, supply, basis, confirmed, r=0.1):
            return _Metrics(ready=True, r=r, z_adj=3.0, crossover=1.0,
                            confirmed=confirmed, rvol=rvol, supply=supply,
                            basis=basis, w_l=w_l)

        an = AnomalyAnalyzer(cfg, lambda g: "")
        m = an._manipulation(metrics(0.9, 1.5, 1.0, 0.015, True), breadth=0.7)
        check("manip: organic case M ~ 0.07", abs(m - 0.07) < 0.02, f"{m:.3f}")
        m = an._manipulation(metrics(0.5, 0.75, 1.0, 0.03, False), breadth=0.2)
        check("manip: mixed case M ~ 0.46", abs(m - 0.46) < 0.03, f"{m:.3f}")
        m = an._manipulation(metrics(0.2, 0.0, 0.5, 0.075, False), breadth=0.1)
        check("manip: synthetic case M ~ 0.94", abs(m - 0.94) < 0.03, f"{m:.3f}")

        # ---- 3. robust z catches a planted spike -------------------------
        def tick(key, price, ts, volume=None, listings=None, aux=None, name="T", cat="poe1:x:Currency"):
            return ItemTick(key=key, name=name, category=cat, price=price,
                            volume=volume, listings=listings, aux=aux, ts=ts)

        t0 = 1_000_000.0
        step = 900.0  # 15-minute spacing
        states = {}
        analyzer2 = AnomalyAnalyzer(cfg, lambda g: "")
        history = []
        for i in range(60):
            price = 100.0 * (1.0 + 0.004 * ((i % 7) - 3) / 3.0)
            history.append(tick("k1", price, t0 + i * step))
        history.append(tick("k1", 140.0, t0 + 60 * step))  # +40% spike
        replayed = analyzer2.warm_start(history[:-1])
        check("warm start: ready_keys published", "k1" in analyzer2.ready_keys,
              str(sorted(analyzer2.ready_keys)))
        snap = Snapshot(game="poe1", category="poe1:x:Currency", league="Allflame",
                        ts=t0 + 60 * step, source="documented",
                        ticks=(history[-1],))
        anoms = analyzer2.process(snap)
        check("robust z: warm start replayed 60 ticks", replayed == 60, str(replayed))
        check("robust z: +40% spike flagged", len(anoms) == 1 and anoms[0].z_adj > 3.0,
              f"{[round(a.z_adj, 2) for a in anoms]}")
        check("robust z: phase label present", anoms and anoms[0].phase in
              ("Launch", "Stabilization", "Mature", "Decay"))

        # ---- 3b. config hot-reload must reach the phase model ------------
        z_before = analyzer2.phase.z_threshold(0.0)
        cfg2 = DetectorCfg()
        cfg2.z0 += 2.0                    # 5.0 -> 7.0
        cfg2.fast_h_min += 6.0            # 4 -> 10
        analyzer2.cfg = cfg2
        check("hot-reload: analyzer.cfg swapped", analyzer2.cfg is cfg2)
        check("hot-reload: phase z* follows z0",
              abs(analyzer2.phase.z_threshold(0.0) - z_before - 2.0) < 1e-9,
              f"{analyzer2.phase.z_threshold(0.0):.3f}")
        check("hot-reload: phase half-lives follow",
              analyzer2.phase.half_lives(0.0) == (cfg2.fast_h_min, cfg2.slow_h_min),
              str(analyzer2.phase.half_lives(0.0)))

        # ---- 4. governor token bucket + digest ---------------------------
        gov_cfg = Alerts()
        gov_cfg.bucket_capacity = 3
        gov = AlertGovernor(gov_cfg)
        import dataclasses
        anoms8 = [dataclasses.replace(anoms[0], key=f"k{i}", name=f"T{i}")
                  for i in range(8)]      # 8 distinct items: bucket + digest path
        shown, digest = gov.decide(anoms8)
        check("governor: bucket admits 3", len(shown) <= 3, str(len(shown)))
        check("governor: digest folds the rest", digest is not None and digest.count == 8 - len(shown))
        check("digest: items carry the folded rows",
              digest is not None and len(digest.items) == digest.count
              and digest.items[0].key.startswith("k")
              and digest.items[0].prev_price > 0,
              f"items={len(digest.items) if digest else 0}")
        gov3 = AlertGovernor(gov_cfg)
        many = [dataclasses.replace(anoms[0], key=f"m{i}", name=f"M{i}")
                for i in range(20)]
        _, dg20 = gov3.decide(many)
        check("digest: payload capped at 12 rows",
              dg20 is not None and dg20.count == 17 and len(dg20.items) == 12,
              f"count={dg20.count if dg20 else 0}, "
              f"items={len(dg20.items) if dg20 else 0}")

        # ---- 4b. per-batch dedup: one alert per item key -----------------
        gov2 = AlertGovernor(gov_cfg)
        dup = dataclasses.replace(anoms[0])   # same key, same priority
        shown2, digest2 = gov2.decide([dup, dup, dup])
        check("governor: per-batch key dedup", len(shown2) == 1 and digest2 is None,
              f"shown={len(shown2)} digest={digest2.count if digest2 else None}")
        weak = dataclasses.replace(anoms[0], key="dup-key", priority=anoms[0].priority - 1.0)
        stronger = dataclasses.replace(anoms[0], key="dup-key")
        shown3, _ = gov2.decide([weak, stronger, weak])
        check("governor: dedup keeps highest priority",
              len(shown3) == 1 and shown3[0] is stronger)

        # unmute round-trip (QA: user asked for removable mutes)
        gov_m = AlertGovernor(gov_cfg)
        gov_m.mute(anoms[0].key, 30.0)
        shown_m, dig_m = gov_m.decide([anoms[0]])
        check("mute: muted key suppressed (no shown, no digest)",
              len(shown_m) == 0 and dig_m is None,
              f"shown={len(shown_m)}, digest={dig_m.count if dig_m else None}")
        gov_m.unmute(anoms[0].key)
        shown_m2, _ = gov_m.decide([anoms[0]])
        check("mute: unmute restores alerts immediately", len(shown_m2) == 1,
              str(len(shown_m2)))
        gov_m.mute(anoms[0].key, 30.0)
        gov_m.reset()
        check("mute: reset clears the mute store", not gov_m._muted)

        # ---- 4g-mute. one-click snooze (QA) --------------------------------
        gov_m = AlertGovernor(gov_cfg)
        gov_m.mute(anoms[0].key, 30.0)
        shown_m, dig_m = gov_m.decide([anoms[0]])
        check("mute: muted key suppressed (no shown, no digest)",
              len(shown_m) == 0 and dig_m is None,
              f"shown={len(shown_m)}, digest={dig_m.count if dig_m else None}")
        gov_m._muted[anoms[0].key] = time.time() - 1.0        # TTL expired
        shown_m2, _ = gov_m.decide([anoms[0]])
        check("mute: expired TTL passes again", len(shown_m2) == 1, str(len(shown_m2)))
        gov_m.reset()
        check("mute: reset clears the mute store", not gov_m._muted)

        # ---- 4c. empirical z* caps raw alerts on heavy tails (B3) --------
        import math
        import random

        rng = random.Random(20261108)

        def t3() -> float:            # Student-t(3), stdlib: Z / sqrt(V/3)
            v = sum(rng.gauss(0.0, 1.0) ** 2 for _ in range(3))
            return rng.gauss(0.0, 1.0) / math.sqrt(v / 3.0)

        cfgt = DetectorCfg()
        cfgt.target_alerts_per_category_per_day = 20.0
        ant = AnomalyAnalyzer(cfgt, lambda g: "")
        snap_ts0 = 5_000_000.0
        items = [f"poe1:x:Currency:itm{i}" for i in range(200)]
        prices = {k: 10.0 for k in items}

        def hourly(price_fn, n_snaps, start_idx):
            alerts = 0
            for i in range(start_idx, start_idx + n_snaps):
                ts = snap_ts0 + i * 3600.0
                ticks = tuple(
                    ItemTick(key=k, name=k, category="poe1:x:Currency",
                             price=(prices.__setitem__(k, max(1e-6, price_fn(prices[k])))
                                    or prices[k]),
                             volume=None, listings=None, aux=None, ts=ts)
                    for k in items)
                alerts += len(ant.process(Snapshot(
                    game="poe1", category="poe1:x:Currency", league="", ts=ts,
                    source="documented", ticks=ticks)))
            return alerts

        hourly(lambda p: p * math.exp(t3() * 0.01), 30, 0)     # warm-up: baselines + pool
        measured = hourly(lambda p: p * math.exp(t3() * 0.01), 96, 30)
        rate = measured / 4.0                                   # 4 days, hourly polls
        check("B3: t(3) raw rate within 2x of target (209/day phase-only -> 20.8/day capped)",
              10.0 <= rate <= 40.0, f"{rate:.1f}/day ({measured} alerts / 4d)")

        # ---- 4d. young baselines shrink to the category scale (B4) -------
        cfgb = DetectorCfg()
        cfgb.target_alerts_per_category_per_day = 0.0    # isolate from B3

        def build_blend(k):
            cfgb.mad_shrink_k = k
            ab = AnomalyAnalyzer(cfgb, lambda g: "")
            ts0 = 6_000_000.0

            def filler_snap(i):      # phase 1: build the category return scale
                ticks = tuple(
                    ItemTick(key=f"poe1:x:Currency:f{j}", name=f"f{j}",
                             category="poe1:x:Currency",
                             price=10.0 * (1 + (1 if (i + j) % 2 else -1)
                                           * (0.01 + 0.01 * ((i + 3 * j) % 4) / 3.0)),
                             volume=None, listings=None, aux=None, ts=ts0 + i * 3600.0)
                    for j in range(20))
                return Snapshot(game="poe1", category="poe1:x:Currency", league="",
                                ts=ts0 + i * 3600.0, source="documented", ticks=ticks)

            def x_snap(i, p):        # phase 2: X alone (no B6 median, no breadth)
                return Snapshot(game="poe1", category="poe1:x:Currency", league="",
                                ts=ts0 + 100 + i * 3600.0, source="documented",
                                ticks=(ItemTick(key="poe1:x:Currency:X", name="X",
                                                category="poe1:x:Currency", price=p,
                                                volume=None, listings=None, aux=None,
                                                ts=ts0 + 100 + i * 3600.0),))

            for i in range(25):
                ab.process(filler_snap(i))
            for i in range(25):      # X wiggles +-0.05%: real changes, tiny own MAD
                ab.process(x_snap(i, 5.0 * (1.0 + 0.0005 * math.sin(i))))
            return len(ab.process(x_snap(25, 5.0 * 1.03)))

        no_blend = build_blend(0.0)
        blended = build_blend(30.0)
        check("B4: flat item's 3% jump alerts without shrinkage, quiet with it",
              no_blend >= 1 and blended == 0, f"k=0: {no_blend}, k=30: {blended}")

        # ---- 4e. minimum-step moves must not cross z* (B5) ----------------
        cfgs = DetectorCfg()
        cfgs.target_alerts_per_category_per_day = 0.0
        asz = AnomalyAnalyzer(cfgs, lambda g: "")
        ts0 = 7_000_000.0

        def cheap_snap(i, p):
            return Snapshot(game="poe1", category="poe1:x:Currency", league="",
                            ts=ts0 + i * 3600.0, source="documented",
                            ticks=(ItemTick(key="poe1:x:Currency:cheap", name="C",
                                            category="poe1:x:Currency", price=p,
                                            volume=None, listings=None, aux=None,
                                            ts=ts0 + i * 3600.0),))

        # learn the min step by alternating hourly, then one step after a
        # 2-minute gap (confirmation re-scan pattern) - the floor must absorb it
        for i in range(25):
            asz.process(cheap_snap(i, 0.0505 if i % 2 else 0.05))
        step_anoms = asz.process(cheap_snap(24 + 2.0 / 60.0, 0.051))
        real_anoms = asz.process(cheap_snap(24 + 2.0 / 60.0 + 1.0, 0.0657))
        check("B5: min-step move over a short gap stays quiet", not step_anoms,
              str([round(a.z_adj, 2) for a in step_anoms]))
        check("B5: genuine move after step-quantized prices still alerts",
              len(real_anoms) == 1, str([round(a.z_adj, 2) for a in real_anoms]))

        # ---- 4f. category-wide shift -> one market event (B6) -------------
        cfgm = DetectorCfg()
        cfgm.target_alerts_per_category_per_day = 0.0
        amk = AnomalyAnalyzer(cfgm, lambda g: "")
        ts0 = 8_000_000.0
        mk_items = [f"poe1:x:Currency:m{i}" for i in range(12)]

        def mk_snap(i, shift=1.0):
            ticks = tuple(
                ItemTick(key=k, name=k, category="poe1:x:Currency",
                         price=10.0 * shift * (1.0 + 0.002 * ((i + n) % 5 - 2)),
                         volume=None, listings=None, aux=None, ts=ts0 + i * 3600.0)
                for n, k in enumerate(mk_items))
            return Snapshot(game="poe1", category="poe1:x:Currency", league="",
                            ts=ts0 + i * 3600.0, source="documented", ticks=ticks)

        for i in range(25):
            amk.process(mk_snap(i))
        market = amk.process(mk_snap(25, shift=1.05))          # whole category +5%
        check("B6: 5% category-wide shift yields exactly one market event",
              len(market) == 1 and market[0].key.endswith("#market"),
              str([(a.key, round(a.pct, 1)) for a in market]))
        revert = amk.process(mk_snap(26))                      # -5% reversal
        check("B6: the reversal is one market event too, not N alerts",
              len(revert) == 1 and revert[0].key.endswith("#market"),
              str([(a.key, round(a.pct, 1)) for a in revert]))
        quiet = amk.process(mk_snap(27))                       # stable again
        check("B6: stable snapshot stays quiet (no #market residue)",
              not quiet, str([a.key for a in quiet]))

        # ---- 4g. exchange liquidity weight follows volume (B7) ------------
        cfgv = DetectorCfg()
        av = AnomalyAnalyzer(cfgv, lambda g: "")
        stv = av._fresh(ItemTick(key="k", name="k", category="c", price=10.0,
                                 volume=1000.0, listings=None, aux=None, ts=0.0))
        mv = av._advance(stv, ItemTick(key="k", name="k", category="c", price=10.5,
                                       volume=100.0, listings=None, aux=None, ts=3600.0),
                         12.0, 72.0)
        thin = av._advance(stv, ItemTick(key="k", name="k", category="c", price=11.0,
                                         volume=5.0, listings=None, aux=None, ts=7200.0),
                           12.0, 72.0)
        check("B7: exchange w_l follows volume (damps thin markets)",
              mv is not None and thin is not None
              and abs(mv.w_l - 100.0 / (100.0 + cfgv.liquidity_v0)) < 1e-9
              and thin.w_l < mv.w_l < 1.0,
              f"w_l(vol=100)={mv.w_l:.3f}, w_l(vol=5)={thin.w_l:.3f}")

        # ---- 4h. unchanged ticks skip the engine, reach the store (C8) ----
        cfgu = DetectorCfg()
        au = AnomalyAnalyzer(cfgu, lambda g: "")

        def utick(p, ts, v=None, l=None):
            return ItemTick(key="u", name="u", category="c", price=p,
                            volume=v, listings=l, aux=None, ts=ts)

        stu = au._fresh(utick(10.0, 0.0, v=100.0, l=50.0))
        r1 = au._advance(stu, utick(10.0, 900.0, v=100.0, l=50.0), 12.0, 72.0)
        r2 = au._advance(stu, utick(10.0, 1800.0, v=120.0, l=50.0), 12.0, 72.0)
        r3 = au._advance(stu, utick(10.5, 2700.0, v=120.0, l=50.0), 12.0, 72.0)
        check("C8: price-unchanged ticks skip returns, liquidity EMAs refresh",
              r1 is None and r2 is None and r3 is not None and len(stu.rt) == 1
              and stu.ev is not None and stu.ev > 100.0
              and abs(r3.r - math.log(1.05)) < 1e-9,
              f"rt={[round(x, 4) for x in stu.rt]}, ev={round(stu.ev or 0, 1)}")

        with tempfile.TemporaryDirectory() as td2:
            pp = AnalysisPipeline(DetectorCfg(), Alerts(), lambda g: "",
                                  Path(td2) / "h.sqlite3")
            tsb = time.time() - 600.0
            ticks = (utick(10.0, tsb, v=100.0, l=50.0),
                     utick(10.0, tsb + 900.0, v=100.0, l=50.0),      # unchanged
                     utick(10.001, tsb + 1800.0, v=100.0, l=50.0))
            pp.handle(Snapshot(game="poe1", category="poe1:x:Currency", league="",
                               ts=tsb + 1800.0, source="documented", ticks=ticks))
            zero_rt = [x for x in pp.analyzer._states["u"].rt if x == 0.0]
            check("C8: store keeps every change, engine window has no zero returns",
                  pp.store.count() == 2 and not zero_rt,
                  f"store={pp.store.count()}, zero_rt={len(zero_rt)}")
            pp.close()

        # ---- 4i. confirmation re-scan plumbing (C9) -----------------------
        from webhook import confirm_record
        ref = {"ref_ts": 100.0, "alert_price": 110.0, "prev_price": 100.0,
               "game": "poe1", "league": "L", "cat_id": "Currency"}
        rec_held = confirm_record("k", ref, 108.0, 200.0)     # 80% of the move kept
        rec_rev = confirm_record("k", ref, 101.0, 200.0)      # 10% kept
        rec_gone = confirm_record("k", ref, None, 200.0)      # vanished
        check("C9: confirm_record grades held/reverted/gone",
              rec_held["held"] and abs(rec_held["share"] - 0.8) < 1e-9
              and not rec_rev["held"] and rec_rev["share"] == 0.1
              and not rec_gone["held"] and rec_gone["share"] is None
              and rec_gone["type"] == "confirm",
              f"{rec_held['share']}/{rec_rev['share']}/{rec_gone['share']}")

        from poller import PollWorker
        pw = PollWorker(object(), Config(), "poe1")
        pw.wake_soon(60.0, categories={"Currency"})
        pw.wake_soon(60.0, categories={"Fragment"})
        check("C9: poller merges re-scan categories across alerts",
              pw._wake_cats == {"Currency", "Fragment"}, str(sorted(pw._wake_cats)))

        # ---- 4j. vanished items become a buyout feature (C10) -------------
        from types import SimpleNamespace
        from api_client import NinjaApiClient
        cl = NinjaApiClient()
        meta_q, quotes = cl._parse_item("poe1", "L", SimpleNamespace(id="UniqueArmour"),
                                        {"lines": [
                                            {"name": "Vanished", "chaosValue": 12.0,
                                             "listingCount": 0},
                                            {"name": "Live", "chaosValue": 5.0,
                                             "listingCount": 7},
                                            {"name": "NoCount", "chaosValue": 3.0},
                                        ]})
        check("C10: parser marks zero-listing lines delisted, keeps live ones",
              len(quotes) == 3 and quotes[0].delisted and quotes[0].price == 0.0
              and not quotes[1].delisted and not quotes[2].delisted,
              str([(q.name, q.price, q.delisted) for q in quotes]))
        del_ticks = ticks_from_quotes(quotes, 1_000.0)
        check("C10: delisted quote travels as a zero-price tick",
              len(del_ticks) == 3 and del_ticks[0].price == 0.0
              and del_ticks[0].listings == 0.0, str(del_ticks[:1]))

        ad = AnomalyAnalyzer(DetectorCfg(), lambda g: "")
        tsk = [ItemTick(key="k", name="k", category="c", price=10.0, volume=None,
                        listings=30.0, aux=None, ts=100.0 + i * 3600.0)
               for i in range(25)]
        ad.warm_start(tsk)
        ad.process(Snapshot(game="poe1", category="c", league="",
                            ts=100.0 + 25 * 3600.0, source="documented",
                            ticks=(ItemTick(key="k", name="k", category="c", price=0.0,
                                            volume=None, listings=0.0, aux=None,
                                            ts=100.0 + 25 * 3600.0),)))
        check("C10: disappearance recorded, baseline untouched",
              "k" in ad._disappeared and ad._states["k"].last_price == 10.0,
              str(ad._disappeared))
        m = ad._advance(ad._states["k"],
                        ItemTick(key="k", name="k", category="c", price=11.0, volume=None,
                                 listings=2.0, aux=None, ts=100.0 + 26 * 3600.0),
                        12.0, 72.0)
        check("C10: reappearance after delist raises the manipulation feature",
              m is not None and m.redelisted and ad._features(m, None)[2] == 1.0,
              f"redelisted={getattr(m, 'redelisted', None)}")

        # ---- 7b. symmetric supply-pressure feature on drops (D13) ---------
        m_drop_flood = metrics(0.9, 1.0, 1.5, 0.0, True, r=-0.1)   # listings flood, price falls
        m_drop_vanish = metrics(0.9, 1.0, 0.5, 0.0, True, r=-0.1)  # listings vanish, price falls
        f_flood = an._features(m_drop_flood, None)[2]
        f_vanish = an._features(m_drop_vanish, None)[2]
        check("D13: f3 fires on supply flooding a drop, not on vanishing supply",
              f_flood == 1.0 and f_vanish == 0.0, f"flood={f_flood}, vanish={f_vanish}")

        # ---- 7c. priority ranks by absolute move and volume (D12) ---------
        from advanced_analyzer import alert_priority
        check("D12: priority ignores price level, grows with move size and volume",
              alert_priority(50.0, 1.0, 0.0) > alert_priority(9.0, 1.0, 0.0)
              and alert_priority(9.0, 5.0, 0.0) > alert_priority(9.0, 1.0, 0.0)
              and alert_priority(9.0, 1.0, 2.0) > alert_priority(9.0, 1.0, 0.0)
              and alert_priority(500.0, 1.0, 0.0) > alert_priority(9.0, 1.0, 0.0) * 2.0,
              f"base={alert_priority(9.0, 1, 0):.2f} big={alert_priority(50, 1, 0):.2f} "
              f"rvol={alert_priority(9, 5, 0):.2f}")

        # ---- 7d. calibration labels from measured outcomes (D11) ----------
        from calibration import collect_training_rows, forward_index, outcome_label
        cfgd = DetectorCfg()
        ts00 = 12_000_000.0

        def tt(key, price, i):
            return ItemTick(key=key, name=key, category="poe1:x:Currency", price=price,
                            volume=None, listings=None, aux=None, ts=ts00 + i * 900.0)

        ticks_d = [tt("poe1:x:Currency:A", 100.0 * (1 + 0.0005 * math.sin(i)), i)
                   for i in range(30)]
        ticks_d.append(tt("poe1:x:Currency:A", 150.0, 30))           # pump
        ticks_d.append(tt("poe1:x:Currency:A", 100.0, 31))           # dump back
        ticks_d += [tt("poe1:x:Currency:A", 100.0 * (1 + 0.0005 * math.sin(i)), i)
                    for i in range(40, 70)]                          # flat beyond +6h
        climb = [100.0 * (1.03 ** i) for i in range(30)]
        ticks_d += [tt("poe1:x:Currency:B", climb[i], i) for i in range(30)]
        ticks_d += [tt("poe1:x:Currency:B", climb[-1] * (1 + 0.0005 * math.sin(i)), i)
                    for i in range(30, 70)]                          # hold beyond +6h

        rows_d = collect_training_rows(cfgd, lambda g: "", ticks_d)
        xs_d, ys_d = outcome_label(rows_d, forward_index(ticks_d), cfgd)
        check("D11: reverted pump labeled fake(1), held moves labeled real(0)",
              ys_d.count(1) == 1 and ys_d.count(0) >= 5,
              f"{ys_d.count(1)} fake / {ys_d.count(0)} held of {len(ys_d)}")

        # ---- 7e. time-of-day volatility seasonality (D14) -----------------
        import time as _time
        cfgw = DetectorCfg()
        aw = AnomalyAnalyzer(cfgw, lambda g: "")
        ts_w0 = 1_760_000_000.0

        def jitter(h):
            return 0.002 if 18 <= h % 24 < 24 else 0.01   # quiet nights, active days

        for i in range(900):                              # learn the buckets
            ts = ts_w0 + i * 3600.0
            h = _time.gmtime(ts).tm_hour
            for k_i, key in enumerate(("w1", "w2")):
                sign = 1 if (i + k_i) % 2 else -1
                aw.process(Snapshot(game="poe1", category="poe1:x:Currency", league="",
                                    ts=ts, source="documented",
                                    ticks=(ItemTick(key=key, name=key,
                                                    category="poe1:x:Currency",
                                                    price=10.0 * (1 + sign * jitter(h)),
                                                    volume=None, listings=None, aux=None,
                                                    ts=ts),)))
        last_ts = ts_w0 + 899 * 3600.0

        def final_tick(key, hour_target, mult):
            base = last_ts + 3600.0
            ts = base + ((hour_target - _time.gmtime(base).tm_hour) % 24) * 3600.0
            return ts, ItemTick(key=key, name=key, category="poe1:x:Currency",
                                price=10.0 * mult, volume=None, listings=None,
                                aux=None, ts=ts)

        ts_n, t_n = final_tick("w1", 20, 1.02)            # quiet night bucket
        ts_d, t_d = final_tick("w2", 2, 1.02)             # active day bucket
        mx = aw._advance(aw._states["w1"], t_n, 12.0, 72.0)
        my = aw._advance(aw._states["w2"], t_d, 12.0, 72.0)
        check("D14: same move scores higher in the quiet bucket",
              mx is not None and my is not None and mx.z_adj > my.z_adj
              and aw._season(ts_n, ts_n) < 1.0 <= aw._season(ts_d, ts_d),
              f"z_night={mx.z_adj:.2f} z_day={my.z_adj:.2f} "
              f"s_night={aw._season(ts_n, ts_n):.2f} s_day={aw._season(ts_d, ts_d):.2f}")

        # ---- 5. history store round trip ---------------------------------
        with tempfile.TemporaryDirectory() as td:
            store = HistoryStore(Path(td) / "h.sqlite3")
            store.append(history[:5])
            loaded = store.load_since(0.0)
            check("history: append/load round trip", len(loaded) == 5 and loaded[0].key == "k1")
            check("history: count()", store.count() == 5)
            store.prune(history[2].ts - 1)
            check("history: prune", store.count() == 3)
            store.close()

        # ---- 6. quote -> tick adapter ------------------------------------
        from api_client import FetchMeta, ItemQuote
        quotes = [ItemQuote(key="poe1:x:Currency:chaos", name="Chaos Orb", game="poe1",
                            category="Currency", source="exchange", price=1.0,
                            list_count=None, icon=None, details_id="chaos",
                            volume=123.0, aux=0.99)]
        ticks = ticks_from_quotes(quotes, 1.0)
        check("bridge: exchange aux = rate/price", ticks and abs(ticks[0].aux - 0.99) < 1e-9)
        check("bridge: category cohort format", ticks[0].category == "poe1:exchange:Currency")

        # ---- 7. basic->anomaly wrapper -----------------------------------
        from analyzer import Alert as BasicAlert
        b = BasicAlert(game="poe1", league="Allflame", category="Currency", name="X",
                       old_price=10.0, new_price=13.0, pct=30.0, list_count=None,
                       vol_ratio=None, icon=None, details_id="x", source="exchange",
                       key="poe1:x:Currency:x")
        wa = anomaly_from_basic(b, "Allflame")
        check("bridge: basic alert wrapped", wa.label == "AMBIGUOUS" and wa.pct == 30.0)
    except Exception:
        import traceback
        check("advanced: suite ran", False, traceback.format_exc(limit=4))


# ------------------------------------------------- hardening (audit) tests
def test_hardening() -> None:
    print("== hardening (finite guards, sqlite, api retry) ==")
    try:
        import random
        from pathlib import Path
        import tempfile

        import api_client as ac
        from advanced_analyzer import (AnalysisPipeline, AnomalyAnalyzer, HistoryStore,
                                       ticks_from_quotes)
        from api_client import ApiError, ItemQuote, NinjaApiClient
        from appconfig import Config, DetectorCfg
        from models import ItemTick

        # ---- finite guards --------------------------------------------------
        def quote(key, price=1.0, volume=5.0, lc=None, aux=None):
            return ItemQuote(key=f"poe1:x:Currency:{key}", name="Q", game="poe1",
                             category="Currency", source="exchange", price=price,
                             list_count=lc, icon=None, details_id=key,
                             volume=volume, aux=aux)

        ticks = ticks_from_quotes([
            quote("q1", price=float("nan")), quote("q2", price=0.0),
            quote("q3", volume=float("inf")), quote("q4", aux=float("nan")),
            quote("q5", price=-3.0), quote("q6")], 1.0)
        got = [t.key.rsplit(":", 1)[-1] for t in ticks]
        check("guards: non-positive/NaN prices dropped", got == ["q3", "q4", "q6"], str(got))
        check("guards: inf volume -> None", ticks[0].volume is None)
        check("guards: NaN aux -> None", ticks[1].aux is None)

        an = AnomalyAnalyzer(DetectorCfg(), lambda g: "")
        t0 = 1_000_000.0
        good = ItemTick(key="k", name="K", category="poe1:x:Currency", price=100.0,
                        volume=None, listings=None, aux=None, ts=t0)
        st = an._fresh(good)
        bad = ItemTick(key="k", name="K", category="poe1:x:Currency", price=float("nan"),
                       volume=None, listings=None, aux=None, ts=t0 + 900.0)
        check("guards: _advance rejects NaN price", an._advance(st, bad, 4.0, 24.0) is None)
        check("guards: baseline state untouched by NaN",
              st.last_price == 100.0 and not st.rt)

        # ---- sqlite hardening -----------------------------------------------
        with tempfile.TemporaryDirectory() as td:
            store = HistoryStore(Path(td) / "h.sqlite3")
            mode = store._con.execute("PRAGMA journal_mode").fetchone()[0]
            check("sqlite: journal_mode=wal", str(mode).lower() == "wal", str(mode))
            idx = store._con.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                     "AND name='idx_ticks_ts'").fetchone()
            check("sqlite: idx_ticks_ts created", idx is not None)
            plan = " ".join(str(r[-1]) for r in store._con.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM ticks WHERE ts >= ?", (0.0,)).fetchall())
            check("sqlite: range scan uses idx_ticks_ts", "idx_ticks_ts" in plan, plan)
            store.close()

            cfgp = Config()
            pl = AnalysisPipeline(cfgp.detector, cfgp.alerts, lambda g: "",
                                  Path(td) / "p.sqlite3")
            pl._last_prune = time.time()       # simulate "just pruned" for determinism
            pl.ingest([ItemTick(key="old", name="O", category="c", price=1.0, volume=None,
                                listings=None, aux=None, ts=1_000_000.0)])
            check("sqlite: ingest stores ticks", pl.store.count() == 1)
            pl._last_prune = 0.0               # force the hourly prune on next ingest
            pl.ingest([ItemTick(key="new", name="N", category="c", price=1.0, volume=None,
                                listings=None, aux=None, ts=time.time())])
            rows = pl.store.load_since(0.0)
            check("sqlite: basic-mode ingest prunes", len(rows) == 1 and rows[0].key == "new",
                  str([r.key for r in rows]))
            pl.close()

        # ---- file size bounds (files must not bloat over a league) --------
        import os
        from webhook import AlertJournal, JOURNAL_MAX_BYTES
        check("files: journal cap is bounded", JOURNAL_MAX_BYTES <= 8 * 1024 * 1024,
              f"{JOURNAL_MAX_BYTES} bytes")
        with tempfile.TemporaryDirectory() as td4:
            jpath = os.path.join(td4, "alerts.jsonl")
            j = AlertJournal(jpath)
            for i in range(5):
                j.append({"i": i, "text": "x" * 60})
            check("files: rotate_if_large no-op below cap",
                  j.rotate_if_large(1024 * 1024) is False and os.path.exists(jpath))
            check("files: rotate folds journal into .old",
                  j.rotate() is True and os.path.exists(jpath + ".old")
                  and not os.path.exists(jpath))
            j.append({"i": 99, "text": "y" * 100})
            check("files: oversized journal rotates on cap",
                  j.rotate_if_large(50) is True and os.path.exists(jpath + ".old"))

        with tempfile.TemporaryDirectory() as td5:
            st2 = HistoryStore(Path(td5) / "h5.sqlite3")
            tk = [ItemTick(key="k", name="k", category="c", price=10.0, volume=None,
                           listings=5.0, aux=None, ts=t0 + i * 900.0) for i in range(3)]
            tk.append(ItemTick(key="k", name="k", category="c", price=10.5, volume=None,
                               listings=5.0, aux=None, ts=t0 + 3 * 900.0))
            tk.append(ItemTick(key="k", name="k", category="c", price=0.0, volume=None,
                               listings=0.0, aux=None, ts=t0 + 4 * 900.0))
            st2.append(tk)
            check("files: store keeps changes and delists, not repeats",
                  st2.count() == 3, str(st2.count()))
            st2.prune(t0 + 2.5 * 900.0)            # retention window drops old rows
            st2.maintain()
            check("files: maintain keeps wal truncated", st2.count() == 2)
            st2.clear()
            pages = int(st2._con.execute("PRAGMA page_count").fetchone()[0])
            check("files: league wipe empties and shrinks the store",
                  st2.count() == 0 and pages < 100, f"pages={pages}")
            st2.close()

        # QA P1: one broken scenario value must not reset the whole config (G4)
        from appconfig import ScenarioCfg
        cfgq = Config()
        cfgq.webhook.url = "https://keep.example/hook"
        cfgq.scenarios = [
            ScenarioCfg(name="bad", game="poe1", category="*", metric="pct",
                        op=">=", value="abc", direction="any", cooldown_minutes=60.0),
            ScenarioCfg(name="good", game="poe1", category="*", metric="pct",
                        op=">=", value=5.0, direction="any", cooldown_minutes=60.0)]
        cfgq._sanitize()
        # QA: patch-day restart detection (offline across the rollover)
        cfgl = Config()
        cfgl.leagues_seen["poe1"] = {"Allflame": "2026-11-01"}
        check("leagues: offline rollover detected as fresh",
              cfgl.league_is_fresh("poe1", "Keeperr") is True
              and cfgl.league_is_fresh("poe1", "Allflame") is False)
        cfgl.leagues_seen["poe2"] = {}
        check("leagues: first-ever run is not fresh (nothing to wipe)",
              cfgl.league_is_fresh("poe2", "Forbidden Rites") is False)
        check("config: broken scenario dropped, config survives",
              len(cfgq.scenarios) == 1 and cfgq.scenarios[0].name == "good"
              and cfgq.webhook.url == "https://keep.example/hook",
              str([(x.name, x.value) for x in cfgq.scenarios]))

        # ---- api transport ----------------------------------------------------
        class FakeResp:
            def __init__(self, status, payload=None, headers=None):
                self.status_code = status
                self._payload = payload
                self.headers = headers or {}
                self.history = []
                self.url = "http://fake"

            def json(self):
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        class FakeSession:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls = 0

            def get(self, url, params=None, headers=None, timeout=None):
                self.calls += 1
                item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
                if isinstance(item, Exception):
                    raise item
                return item

        # 304 on /leagues must serve the cache, never hit resp.json()
        from api_client import League
        client4 = NinjaApiClient()
        client4._leagues["poe1"] = [League(id="Allflame", name="Allflame")]
        client4._etags[client4._cache_key("http://fake/poe1/api/economy/leagues", None)] = "e1"
        client4._sess = FakeSession([FakeResp(304)])
        got = client4.get_leagues("poe1", force=True)
        check("api: 304 leagues served from cache",
              [l.id for l in got] == ["Allflame"], str(got))
        client5 = NinjaApiClient()
        client5._etags[client5._cache_key("http://fake/poe1/api/economy/leagues", None)] = "e1"
        client5._sess = FakeSession([FakeResp(304), FakeResp(200, [{"id": "L1"}, {"id": "L2"}])])
        got2 = client5.get_leagues("poe1", force=True)
        check("api: 304 leagues without cache refetches",
              [l.id for l in got2] == ["L1", "L2"], str(got2))

        # QA P0: NaN/inf prices must be rejected by BOTH parsers (F-04)
        from types import SimpleNamespace as _NS
        pcli = NinjaApiClient()
        meta_i, qi = pcli._parse_item("poe1", "L", _NS(id="UniqueArmour"), {"lines": [
            {"name": "NaN", "chaosValue": float("nan")},
            {"name": "Inf", "chaosValue": float("inf")},
            {"name": "Ok", "chaosValue": 5.0}]})
        check("api: item parser rejects NaN/inf prices",
              len(qi) == 1 and qi[0].price == 5.0, str([(q.name, q.price) for q in qi]))
        meta_e, qe = pcli._parse_exchange("poe1", "L", _NS(id="Currency"),
                                          {"lines": [{"id": "nan-quote",
                                                      "primaryValue": float("nan")},
                                                     {"id": "ok-quote",
                                                      "primaryValue": 1.5}]})
        check("api: exchange parser rejects NaN prices",
              len(qe) == 1 and qe[0].price == 1.5, str([(q.name, q.price) for q in qe]))

        # QA P0: concurrent league refresh on one client (GUI + PollWorker)
        import threading as _thr
        import api_client as _ac
        old_gap = _ac.MIN_REQUEST_GAP_SECONDS
        _ac.MIN_REQUEST_GAP_SECONDS = 0.001   # speed the race up
        client8 = NinjaApiClient()
        client8._sess = FakeSession([FakeResp(200, [{"id": "L1"}])])
        errs = []

        def _hammer():
            for _ in range(40):
                try:
                    client8.get_leagues("poe1", force=True)
                except Exception as exc:               # noqa: BLE001 - test spy
                    errs.append(exc)

        ths = [_thr.Thread(target=_hammer) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        _ac.MIN_REQUEST_GAP_SECONDS = old_gap
        check("api: concurrent league refresh is race-free",
              not errs and client8._leagues["poe1"][0].id == "L1", str(errs[:2]))

        # streaming load: iter_since == load_since, max_rows keeps the newest
        hist = [ItemTick(key="k", name="K", category="poe1:x:Currency",
                         price=100.0 + i, volume=None, listings=None, aux=None,
                         ts=1_000_000.0 + i * 900.0) for i in range(40)]
        with tempfile.TemporaryDirectory() as td:
            store = HistoryStore(Path(td) / "h.sqlite3")
            store.append(hist)
            streamed = list(store.iter_since(0.0))
            check("memory: iter_since == load_since",
                  [t.ts for t in streamed] == [t.ts for t in store.load_since(0.0)])
            tail = list(store.iter_since(0.0, max_rows=5))
            check("memory: max_rows keeps newest in order",
                  [t.ts for t in tail] == sorted(t.ts for t in tail) and len(tail) == 5)
            store.close()

            # bounded calibration replay uses max_rows without error
            from calibration import calibrate_from_ticks
            cfgc = DetectorCfg()
            res = calibrate_from_ticks(cfgc, lambda g: "", hist,
                                       cfgc.weights, cfgc.bias)
            check("memory: calibration replay bounded path runs",
                  res is None or res.improved or "insufficient" in res.reason,
                  str(res and res.reason))

        # analyzer state pruning (item churn must not grow the dict forever)
        anp = AnomalyAnalyzer(DetectorCfg(), lambda g: "")
        anp._states["old:item"] = anp._fresh(hist[0])
        anp._states["new:item"] = anp._fresh(hist[-1])
        anp.ready_keys.add("old:item")
        dropped = anp.prune_states(hist[-1].ts)
        check("memory: stale states pruned",
              dropped == 1 and "old:item" not in anp._states
              and "new:item" in anp._states and "old:item" not in anp.ready_keys)

        from analysis_worker import process_rss_mb
        rss = process_rss_mb()
        check("memory: process_rss_mb usable", rss is None or rss > 0, str(rss))

        slept: list = []
        real_sleep = ac.time.sleep
        ac.time.sleep = lambda s: slept.append(s)
        try:
            client = NinjaApiClient()
            client._sess = FakeSession([FakeResp(500), FakeResp(502), FakeResp(200, {"ok": 1})])
            resp = client._request("http://fake/x")
            check("api: 5xx retried then success",
                  resp.status_code == 200 and client._sess.calls == 3,
                  f"calls={client._sess.calls}")
            check("api: backoff sleeps bounded",
                  2 <= len(slept) and all(0 < s <= ac.MAX_BACKOFF_SECONDS for s in slept),
                  str(slept))

            slept.clear()
            client2 = NinjaApiClient()
            client2._sess = FakeSession([FakeResp(429, headers={"Retry-After": "7"})])
            raised = None
            try:
                client2._request("http://fake/y")
            except Exception as exc:
                raised = exc
            check("api: 429 raises RateLimited after retries",
                  isinstance(raised, ac.RateLimited), repr(raised))
            check("api: Retry-After honoured", any(s >= 7.0 for s in slept), str(slept))

            client3 = NinjaApiClient()
            client3._sess = FakeSession([FakeResp(200, ValueError("bad json"))])
            raised = None
            try:
                client3._json(client3._sess.get("http://fake/z"), "ctx")
            except Exception as exc:
                raised = exc
            check("api: malformed JSON -> ApiError", isinstance(raised, ApiError), repr(raised))
        finally:
            ac.time.sleep = real_sleep

        # ---- calibration ------------------------------------------------------
        from advanced_analyzer import _sigmoid
        from calibration import (collect_training_rows, fit_logistic,
                                 pseudo_label, suggest_thresholds)

        xs, ys = [], []
        rnd = random.Random(11)
        for _ in range(120):
            xs.append([rnd.uniform(0.7, 1.0), rnd.uniform(0.6, 1.0), 0.0, 0.0, 1.0, 0.8])
            ys.append(1)
            xs.append([rnd.uniform(0.0, 0.3), rnd.uniform(0.0, 0.2), 0.0, 0.0, 0.0, 0.2])
            ys.append(0)
        w0, b0 = [1.5, 1.2, 1.5, 1.0, 0.8, 0.6], -3.0
        res = fit_logistic(xs, ys, w0, b0)
        check("calib: separable refit accepted", res.improved, res.reason)
        check("calib: holdout log-loss improved", res.loss_after < res.loss_before,
              f"{res.loss_before:.4f} -> {res.loss_after:.4f}")
        check("calib: clamps respected",
              all(0.0 <= w <= 5.0 for w in res.weights) and -10.0 <= res.bias <= 5.0)

        res_small = fit_logistic(xs[:10], ys[:10], w0, b0)
        check("calib: min-sample gate keeps params",
              not res_small.improved and "insufficient" in res_small.reason,
              res_small.reason)

        lx, ly = pseudo_label([((1.0,) * 6, 0.9), ((0.0,) * 6, 0.1), ((0.5,) * 6, 0.5)],
                              0.65, 0.35)
        check("calib: ambiguous rows dropped", len(lx) == 2 and ly == [1, 0], f"{ly}")

        pairs = [(0.9, 1)] * 40 + [(0.1, 0)] * 40
        s, o, j = suggest_thresholds(pairs, 0.65, 0.35)
        check("calib: Youden J finds perfect separation",
              s is not None and abs(s - 0.9) < 1e-9 and j > 0.99, f"{s} {o} {j:.3f}")

        replay = [ItemTick(key="poe1:x:Currency:z", name="Z", category="poe1:x:Currency",
                           price=100.0 * (1.0 + 0.002 * ((i % 5) - 2)), volume=None,
                           listings=50.0, aux=None, ts=t0 + i * 900.0)
                  for i in range(40)]        # every tick is a real change (C8 skips repeats)
        samples = collect_training_rows(DetectorCfg(), lambda g: "", replay)
        check("calib: replay collects ready rows", len(samples) >= 10, str(len(samples)))
        feats, manip = samples[-1][0], samples[-1][1]   # rows carry key/ts/price/r too (D11)
        check("calib: sample features normalized",
              len(feats) == 6 and all(0.0 <= f <= 1.0 for f in feats) and 0.0 <= manip <= 1.0,
              f"{feats} {manip:.3f}")
        check("calib: stable history stays organic",
              _sigmoid(b0 + sum(w * f for w, f in zip(w0, feats))) <= 0.35,
              f"{manip:.3f}")
    except Exception:
        import traceback
        check("hardening: suite ran", False, traceback.format_exc(limit=4))


# --------------------------------------------------- new-features (v2.1)
def test_features() -> None:
    print("== scenarios / discovery / diagnostics / telegram ==")
    try:
        import tempfile
        from pathlib import Path

        # ---- scenario engine ------------------------------------------------
        from analyzer import Alert as BasicAlert
        from advanced_analyzer import anomaly_from_basic
        from appconfig import Config, ScenarioCfg
        from scenarios import ScenarioEngine

        def basic_anomaly(name="Divine Orb", price=150.0, pct=50.0,
                          key="poe1:x:Currency:div", game="poe1"):
            b = BasicAlert(game=game, league="L", category="Currency", name=name,
                           old_price=100.0, new_price=price, pct=pct, list_count=None,
                           vol_ratio=None, icon=None, details_id="x", source="exchange",
                           key=key)
            return anomaly_from_basic(b, "L")

        eng = ScenarioEngine()
        scs = [
            ScenarioCfg(name="big spike", game="poe1", category="Currency",
                        metric="pct", op=">=", value=20.0, direction="up",
                        cooldown_minutes=60.0),
            ScenarioCfg(name="z watch", game="poe1", category="*",
                        metric="z_abs", op=">=", value=2.0, enabled=False),
            ScenarioCfg(name="gem dumps", game="poe1", category="SkillGem",
                        metric="pct", op="<=", value=-10.0, direction="down"),
            ScenarioCfg(name="named", game="poe1", category="*",
                        name_pattern="divine", metric="pct", op=">=", value=1.0),
        ]
        a_up = basic_anomaly()                                    # +50% Currency
        a_down = basic_anomaly(name="Gem", price=5.0, pct=-20.0,
                               key="poe1:i:SkillGem:gem")         # -20% SkillGem
        hits = eng.evaluate(scs, [a_up, a_down])
        names = sorted({h.scenario for h in hits})
        check("scenarios: up-rule + name-rule fire", names == ["big spike", "named"], str(names))
        check("scenarios: disabled rule skipped", "z watch" not in names)
        check("scenarios: wrong category filtered",
              not any(h.key == a_up.key and h.scenario == "gem dumps" for h in hits))
        hits2 = eng.evaluate(scs, [a_up])
        check("scenarios: cooldown suppresses repeat",
              not any(h.scenario == "big spike" for h in hits2))
        eng.reset()
        scs.append(ScenarioCfg(name="z on", game="poe1", category="*",
                               metric="z_abs", op=">=", value=0.5))
        from dataclasses import replace as dc_replace
        a_z = dc_replace(basic_anomaly(), z_adj=-3.0)
        hits3 = eng.evaluate(scs, [a_z])
        check("scenarios: z_abs uses absolute value",
              any(h.scenario == "z on" and h.metric_value == 3.0 for h in hits3),
              str([(h.scenario, h.metric_value) for h in hits3]))

        # ---- discovery parsing ----------------------------------------------
        from discovery import known_slugs, parse_site_slugs, slug_to_type, unknown_slugs
        html = ('<a href="/poe1/economy/currency/">C</a>'
                '<a href="https://poe.ninja/poe1/economy/unique-weapons">U</a>'
                '<a href="/poe2/economy/currency">C2</a>'
                '<a href="/poe1/economy/leagues">skip</a>'
                '<a href="/poe1/economy/brand-new-thing">N</a>')
        slugs = parse_site_slugs(html, "poe1")
        check("discovery: poe1 slugs parsed, poe2/skip excluded",
              slugs == ["currency", "unique-weapons", "brand-new-thing"], str(slugs))
        check("discovery: slug_to_type camelcase",
              slug_to_type("unique-relics") == "UniqueRelics"
              and slug_to_type("brand-new-thing") == "BrandNewThing")
        known = known_slugs("poe1")
        check("discovery: plural guess maps tattoos",
              known.get("tattoos") == "Tattoo" and known.get("currency") == "Currency")
        check("discovery: unknown candidates",
              unknown_slugs("poe1", slugs) == ["brand-new-thing"])

        # ---- diagnostics ------------------------------------------------------
        from diagnostics import run_checks
        cfgd = Config()
        cfgd.scenarios = [ScenarioCfg(name="s", enabled=True)]
        with tempfile.TemporaryDirectory() as td:
            results = run_checks(cfgd, td)
            by_name = {r.name: r for r in results}
            check("diag: weights ok", by_name["Detector weights"].ok)
            check("diag: fresh db handled", by_name["poe1: history DB"].ok
                  and "no history yet" in by_name["poe1: history DB"].detail)
            check("diag: journal writable", by_name["Alert journal"].ok)
            check("diag: icon asset present", by_name["App icon asset"].ok,
                  by_name["App icon asset"].detail)
            check("diag: log tail ok", by_name["Log tail"].ok)
        cfgd.webhook.enabled = True
        cfgd.webhook.url = ""
        bad = [r for r in run_checks(cfgd, td) if r.name == "Webhook"]
        check("diag: webhook without url flagged", bad and not bad[0].ok)
        cfgd.webhook.url = "https://example.test/hook"
        good = [r for r in run_checks(cfgd, td) if r.name == "Webhook"]
        check("diag: webhook endpoint accepted", good and good[0].ok, good[0].detail)

        # ---- webhook (generic JSON; telegram removed in v2.2) ---------------
        import webhook as wh
        cfgt = Config()
        cfgt.webhook.enabled = True
        cfgt.webhook.url = "https://example.test/hook"
        sender = wh.WebhookSender(lambda: cfgt)
        captured: list = []
        real_post = wh.requests.post
        wh.requests.post = lambda url, **kw: captured.append((url, kw)) or type(
            "R", (), {"status_code": 200})()
        try:
            sender._send({"text": "hello", "name": "X", "priority": 1.0})
        finally:
            wh.requests.post = real_post
            sender.stop()
        check("webhook: generic POST url + payload",
              captured and captured[0][0] == "https://example.test/hook"
              and '"priority"' in captured[0][1]["data"], str(captured)[:120])

        # QA P0: webhook token must never reach the log (SEC-01)
        import logging as _logging
        cfgw3 = Config()
        cfgw3.webhook.enabled = True
        cfgw3.webhook.url = "https://discord.com/api/webhooks/123456/TOKEN-XYZ"
        sw3 = wh.WebhookSender(lambda: cfgw3)
        got_logs: list = []

        class _Spy(_logging.Handler):
            def emit(self, record):
                got_logs.append(record.getMessage())

        _spy = _Spy(level=_logging.WARNING)
        whlog = _logging.getLogger("ninjaalert.webhook")
        whlog.addHandler(_spy)
        try:
            real_post2 = wh.requests.post

            class _R502:
                status_code = 502

            wh.requests.post = lambda *a, **k: _R502()
            sw3._send({"name": "X", "priority": 1.0})
        finally:
            wh.requests.post = real_post2
            whlog.removeHandler(_spy)
            sw3.stop()
        check("webhook: token never hits the log on HTTP error",
              any("HTTP 502" in m for m in got_logs)
              and not any("TOKEN-XYZ" in m for m in got_logs),
              str(got_logs)[:160])
        cfgt2 = Config()
        cfgt2.webhook.enabled = True          # no url configured
        sender2 = wh.WebhookSender(lambda: cfgt2)
        try:
            sender2.notify({"text": "x", "priority": 1.0})
            check("webhook: disabled/empty url skipped gracefully",
                  sender2._queue.qsize() == 0)
        finally:
            sender2.stop()

        # plain_reason: human status lines (QA: user feedback)
        from webhook import plain_reason
        r1 = plain_reason(45.0, 5.0, None, 1, "ORGANIC")
        r2 = plain_reason(-18.0, 1.2, None, -1, "AMBIGUOUS")
        r3 = plain_reason(30.0, 2.0, 0.4, 1, "SUSPECT")
        check("reason: plain lines describe the move in words",
              "up 45%" in r1 and "5x the usual volume" in r1
              and "down 18%" in r2 and "thin volume" in r2
              and "buyout" in r3 and "manipulated" in r3,
              f"{r1} | {r2} | {r3}")
        # ---- click-through: every category must resolve past the fallback ----
        from categories import CATEGORIES
        from ui_history import build_site_url
        bad = []
        for game, cats in CATEGORIES.items():
            for cid, cat in cats.items():
                rec = {"game": game, "league": "Allflame", "category":
                       f"{game}:{cat.source}:{cid}", "details_id": "det-x"}
                url = build_site_url(rec) or ""
                if "/economy/allflame/" not in url:
                    bad.append(f"{game}:{cid}")
        check("click: all registry categories resolve to real pages",
              not bad, str(bad[:6]))
        from webhook import AlertJournal
        with tempfile.TemporaryDirectory() as tdj:
            j = AlertJournal(str(Path(tdj) / "alerts.jsonl"))
            for i in range(5):
                j.append({"name": f"item{i}", "ts": 1000.0 + i, "label": "SUSPECT"})
            tail = j.read_recent(3)
            check("history: journal tail oldest->newest",
                  [r["name"] for r in tail] == ["item2", "item3", "item4"], str(tail))
            j.append("garbage-line-torn")     # written as a JSON string, not a dict
            check("history: non-dict lines tolerated", len(j.read_recent(10)) == 5)
            check("history: missing journal -> empty",
                  AlertJournal(str(Path(tdj) / "none.jsonl")).read_recent() == [])
        # ---- statuses, sections/league, presets, journal sync ---------------
        from ui_overlay import friendly_status
        check("ux: friendly statuses",
              friendly_status("SUSPECT").startswith("⚠")
              and friendly_status("SCENARIO·x").startswith("🎯")
              and friendly_status("ORGANIC").startswith("✅")
              and friendly_status("AMBIGUOUS").startswith("🤔"))

        from poller import PollWorker
        wp = PollWorker(type("Stub", (), {})(), Config(), "poe1")
        wp._cfg.game_cfg("poe1").watch = ["Currency"]
        wp._unavail_strikes["Currency"] = 6
        cleared = []
        wp.section_cleared.connect(lambda g, c: cleared.append((g, c)))
        wp._maybe_autoclear()
        check("sections: 6x404 auto-clears watched section",
              cleared == [("poe1", "Currency")], str(cleared))
        wp.wake_soon(120.0)
        check("sections: wake_soon schedules early re-poll",
              wp._wake_at is not None and 60.0 < wp._wake_at - time.time() <= 121.0)

        cfgcs = Config()
        check("sections: register custom section",
              cfgcs.register_custom_section("poe1", "ProbeCat", "item", "Probe Cat")
              and "ProbeCat" in cfgcs.game_cfg("poe1").watch
              and "ProbeCat" in __import__("categories").CATEGORIES["poe1"])
        cfgcs.unregister_custom_section("poe1", "ProbeCat")
        check("sections: unregister removes from watch",
              "ProbeCat" not in cfgcs.game_cfg("poe1").watch)

        from ui_settings import SCENARIO_PRESETS
        cfgsc = Config()
        cfgsc.scenarios = [dict(SCENARIO_PRESETS[0][2]), dict(SCENARIO_PRESETS[2][2])]
        cfgsc._sanitize()
        check("sections: presets sanitize cleanly",
              [s.name for s in cfgsc.scenarios] == ["Divine moves", "Volume burst"],
              str([s.name for s in cfgsc.scenarios]))

        with tempfile.TemporaryDirectory() as tds:
            js = AlertJournal(str(Path(tds) / "alerts.jsonl"))
            js.append({"name": "X", "ts": 1.0, "key": "poe1:x:Currency:div",
                       "icon": None, "source": "exchange", "details_id": "div",
                       "label": "SUSPECT"})
            rec = js.read_recent(1)[0]
            check("sync: push payload fields land in journal history",
                  rec.get("key") == "poe1:x:Currency:div"
                  and "source" in rec and "details_id" in rec, str(rec))

    except Exception:
        import traceback
        check("features: suite ran", False, traceback.format_exc(limit=4))


# ----------------------------------------------------------------------- qt
def test_qt() -> None:
    print("== qt widgets (offscreen) ==")
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)

        from analyzer import Alert
        from api_client import FetchMeta
        from appconfig import Config
        from ui_overlay import ToastManager
        from PyQt6.QtTest import QTest
        from ui_settings import SettingsWindow

        cfg = Config()
        cfg.alerts.sound = False  # no beeps during tests
        cfg.alerts.popup_seconds = 8.0

        meta = FetchMeta(game="poe1", league="Allflame", category="Currency",
                         source="exchange", primary="chaos", secondary="divine",
                         rates={"divine": 0.002353})
        alert = Alert(game="poe1", league="Allflame", category="Currency", name="Divine Orb",
                      old_price=100.0, new_price=180.0, pct=80.0, list_count=None,
                      vol_ratio=None, icon=None, details_id="divine-orb",
                      source="exchange", ts=time.time())

        mgr = ToastManager(lambda: cfg, ROOT)
        mgr.show_alert(alert, meta)
        check("qt: overlay created", len(mgr._active) == 1)
        overlay = mgr._active[0]
        check("qt: site url built",
              bool(overlay._site_url) and "/poe1/economy/allflame/" in overlay._site_url,
              str(overlay._site_url))
        check("qt: title rendered", "Divine Orb" in overlay._title.text())

        # one-click mute from the toast
        muted_cap: list = []
        overlay.muted.connect(lambda k, m: muted_cap.append((k, m)))
        from PyQt6.QtCore import Qt as _QtM
        QTest.mouseClick(overlay._mute_btn, _QtM.MouseButton.LeftButton)
        check("qt: toast mute button emits 30m",
              len(muted_cap) == 1 and muted_cap[0][0] == alert.key
              and muted_cap[0][1] == 30.0, str(muted_cap))

        # interactive digest toast (human labels, expandable, clickable rows)
        from models import Digest, DigestItem
        ditems = tuple(DigestItem(
            key=f"poe1:i:UniqueArmour:det{i}:", name=f"Artifact {i}",
            category="poe1:item:UniqueArmour", price=150.0, prev_price=100.0,
            pct=50.0, direction=1 if i % 2 == 0 else -1,
            label="SUSPECT" if i % 2 == 0 else "ORGANIC", priority=5.0 - i)
            for i in range(14))
        dgt = Digest(count=31, names=tuple(it.name for it in ditems[:4]),
                     categories=(("poe1:item:BaseType", 31), ("poe1:item:ValdoMap", 1)),
                     items=ditems)
        mgr.show_digest(dgt, "poe1", "Allflame")
        check("qt: digest overlay created", len(mgr._active) == 1,
              f"active={len(mgr._active)} (toast muted+dismissed)")
        dovl = mgr._active[-1]
        check("qt: digest chips use human labels",
              "Base Types" in dovl._chips.text() and "BaseType ×31" not in dovl._chips.text(),
              dovl._chips.text())
        check("qt: digest collapsed by default, all given rows render",
              not dovl._expanded and dovl._rows_count() == 14,
              f"expanded={dovl._expanded}, rows={dovl._rows_count()}")
        h_collapsed = dovl.height()
        dovl.toggle()
        check("qt: digest expands and grows", dovl._expanded and dovl.height() > h_collapsed,
              f"{h_collapsed} -> {dovl.height()}")
        url0 = dovl._item_url(ditems[0])
        check("qt: digest row url opens the item page",
              bool(url0) and "/poe1/economy/allflame/" in url0 and "det0" in url0,
              str(url0))
        dovl.dismiss()
        check("qt: digest dismissed", len(mgr._active) == 0)

        settings = SettingsWindow(cfg)
        saved = []
        settings.settings_saved.connect(lambda c: saved.append(c))
        settings._save()
        check("qt: settings save emits", len(saved) == 1)
        check("qt: settings watch preserved", len(saved[0].game_cfg("poe1").watch) > 0)
        settings.close()  # must hide (close-to-tray), not quit the app
        check("qt: close-to-tray keeps window alive", settings.isHidden())

        # tabs + scenarios draft + live status provider
        check("qt: four tabs present", settings._tabs.count() == 4,
              str(settings._tabs.count()))
        # main window is genuinely resizable (no fixed maximum, sane minimum)
        from PyQt6.QtWidgets import QWidget
        check("qt: settings window resizable",
              settings.minimumSize().height() >= 600
              and settings.minimumSize().width() >= 900
              and settings.maximumSize().width() == QWidget().maximumSize().width(),
              f"min={settings.minimumSize().width()}x{settings.minimumSize().height()}, "
              f"maxW={settings.maximumSize().width()}")
        check("qt: poller discovery sweeps patch day",
              __import__("poller").SECTION_DISCOVERY_SECONDS <= 6 * 3600,
              str(__import__("poller").SECTION_DISCOVERY_SECONDS))
        from ui_settings import DARK_QSS
        from PyQt6.QtCore import Qt as _QtQ
        check("qt: buttons have hover/pressed feedback states",
              "QPushButton:hover" in DARK_QSS and "QPushButton:pressed" in DARK_QSS
              and "QPushButton#primaryButton:pressed" in DARK_QSS
              and "QPushButton:checked" in DARK_QSS
              and settings._save_btn.cursor().shape() == _QtQ.CursorShape.PointingHandCursor)

        # QA P1: invalid league_start is flagged in the UI, not only in the log (G5)
        settings._league_start_edit.setText("2026-13-45")
        settings._save()
        check("ui: invalid league_start flagged in the field",
              settings._league_start_edit.property("error") is True)
        settings._league_start_edit.setText("2026-11-09")
        settings._save()
        check("ui: valid league_start clears the error flag",
              settings._league_start_edit.property("error") is False)
        settings._league_start_edit.setText("")

        # QA: monitor checkbox draft survives game-tab switches (G: "the
        # checkmark keeps coming back" - uncheck poe2, switch, save applied
        # only the ACTIVE tab, poe2 silently reverted)
        settings.set_leagues("poe2", ["Forbidden Rites", "Standard"])
        QTest.mouseClick(settings._btn_poe2, _QtQ.MouseButton.LeftButton)
        app.processEvents()
        settings._monitor_check.setChecked(True)     # user re-enables poe2
        QTest.mouseClick(settings._btn_poe1, _QtQ.MouseButton.LeftButton)
        app.processEvents()
        poe1_state = settings._monitor_check.isChecked()
        QTest.mouseClick(settings._btn_poe2, _QtQ.MouseButton.LeftButton)
        app.processEvents()
        check("ui: monitor draft survives tab switches (poe2 keeps user state)",
              settings._monitor_check.isChecked() is True
              and poe1_state is True,
              f"poe2={settings._monitor_check.isChecked()}, poe1={poe1_state}")
        settings._monitor_check.setChecked(False)    # user disables poe2 again
        QTest.mouseClick(settings._btn_poe1, _QtQ.MouseButton.LeftButton)
        app.processEvents()
        settings._save()
        check("ui: save clears the draft, poe1 state intact",
              settings._monitor_check.isChecked() is True
              and "poe2" not in settings._monitor_draft,
              f"poe1={settings._monitor_check.isChecked()}, draft={settings._monitor_draft}")
        from appconfig import ScenarioCfg
        settings._scen_draft.append(ScenarioCfg(name="t", game="poe1",
                                                metric="pct", value=5.0))
        settings._reload_scenarios()
        check("qt: scenario draft listed", settings._scen_list.count() == 1)
        settings.set_status_provider(lambda: {
            "engine": "auto", "auto_calibrate": False, "scenarios": 1,
            "poller_states": {"poe1": "waiting", "poe2": "disabled"},
            "worker_stats": {"poe1": (7, 21), "poe2": (0, 0)},
            "enabled": {"poe1": True, "poe2": False},
            "last_detail": "ok", "leagues": {"poe1": "Allflame"}})
        settings.show()
        settings._refresh_status()
        check("qt: status tab renders provider snapshot",
              "7 robust + 21 basic" in settings._live_labels["poe1"].text(),
              settings._live_labels["poe1"].text())
        check("qt: disabled game says off, not monitoring",
              settings._live_labels["poe2"].text().startswith("off"),
              settings._live_labels["poe2"].text())
        # bundled default alert sound (PoE hitmarker) resolves via resource_path
        import os as _os
        from appconfig import resource_path as _rp
        wavp = _rp("assets/alert.wav")
        check("sound: hitmarker default bundled and reachable",
              _os.path.isfile(wavp) and _os.path.getsize(wavp) > 40000,
              f"{wavp} ({_os.path.getsize(wavp) if _os.path.exists(wavp) else 0} B)")

        # league picker: current league on top, variants grouped, stale -> default
        from api_client import league_is_variant, is_permanent_league
        check("leagues: variant/permanent classification",
              league_is_variant("Hardcore Allflame") and league_is_variant("HC Runes of Aldur")
              and league_is_variant("Allflame SSF") and is_permanent_league("Standard")
              and is_permanent_league("Hardcore")
              and not league_is_variant("Allflame")
              and not league_is_variant("Runes of Aldur"),
              "poe2 live list classified")
        settings._leagues["poe2"] = ["Forbidden Rites", "Runes of Aldur",
                                     "HC Forbidden Rites", "Standard", "Hardcore"]
        settings._editing_game = "poe2"
        settings._fill_league_combo()
        combo = settings._league_combo
        check("leagues: dropdown defaults to the current challenge league",
              combo.itemText(0).startswith("Forbidden Rites") and combo.itemData(0) == "Forbidden Rites"
              and combo.currentData() == "Forbidden Rites",
              f"item0={combo.itemText(0)!r}, data={combo.currentData()!r}")
        seps = [r for r in range(combo.count()) if not combo.itemData(r)]
        check("leagues: separators present and disabled",
              len(seps) == 2 and all(not combo.model().item(r).isEnabled() for r in seps),
              f"rows={combo.count()}, separators={seps}")
        settings._cfg.game_cfg("poe2").league = "Gone League"
        settings._fill_league_combo()
        check("leagues: stale saved league falls back to the current one",
              combo.currentData() == "Forbidden Rites",
              str(combo.currentData()))
        check("leagues: combo usable while Auto is on",
              combo.isEnabled() is True
              and combo.itemText(0).startswith("Forbidden Rites"))
        combo.setCurrentIndex(1)              # "Runes of Aldur" - manual pin
        check("leagues: manual pick unchecks Auto and pins the league",
              settings._auto_league.isChecked() is False
              and settings._cfg.game_cfg("poe2").league == "Runes of Aldur",
              f"auto={settings._auto_league.isChecked()}, "
              f"league={settings._cfg.game_cfg('poe2').league}")
        settings._auto_league.setChecked(True)   # back to Auto
        check("leagues: combo stays enabled in Auto mode", combo.isEnabled() is True)
        settings._cfg.game_cfg("poe2").league = ""
        settings._editing_game = "poe1"

        # History tab: provider -> cards, filters
        rec = {"key": "poe1:x:Currency:div", "name": "Divine Orb", "game": "poe1",
               "league": "Allflame", "category": "Currency", "price": 148.0,
               "prev_price": 100.0, "pct": 48.0, "direction": 1, "label": "SUSPECT",
               "phase": "Stabilization", "z_adj": 4.2, "rvol": 3.1, "ts": time.time(),
               "priority": 2.5, "icon": None, "source": "exchange",
               "details_id": "divine"}
        settings.set_history_source(lambda limit=200: [rec])
        settings._history.refresh(force=True)
        check("qt: history card rendered", settings._history._list.count() == 1,
              str(settings._history._list.count()))

        hist_card = settings._history._list.itemWidget(settings._history._list.item(0))
        hist_muted: list = []
        hist_card.muted.connect(lambda k, m: hist_muted.append((k, m)))
        QTest.mouseClick(hist_card._mute_btn, _QtQ.MouseButton.LeftButton)
        check("qt: history card mute emits",
              len(hist_muted) == 1 and hist_muted[0][0] == rec["key"],
              str(hist_muted))
        settings._history.set_mute_state(lambda: {rec["key"]: time.time() + 1800})
        settings._history.refresh(force=True)
        mute_card = settings._history._list.itemWidget(settings._history._list.item(0))
        check("history: muted item renders the unmute toggle",
              mute_card is not None and mute_card._mute_btn.text() == "🔔"
              and mute_card._is_muted is True,
              f"btn={mute_card._mute_btn.text() if mute_card else None}")
        settings._history.set_mute_state(lambda: {})
        settings._history.refresh(force=True)
        settings_muted: list = []
        settings.mute_requested.connect(lambda k, m: settings_muted.append((k, m)))
        QTest.mouseClick(hist_card._mute_btn, _QtQ.MouseButton.LeftButton)
        check("mute: two-hop relay card -> history -> settings",
              len(settings_muted) == 1 and settings_muted[0][0] == rec["key"],
              str(settings_muted))
        settings._history._search.setText("mirror-of-kalandra")
        settings._history._rebuild()
        check("qt: history filter yields empty state",
              settings._history._list.count() == 1
              and settings._history._list.item(0).text().startswith("no alerts"),
              settings._history._list.item(0).text() if settings._history._list.count() else "?")
        settings._history._search.setText("divine")
        settings._history._rebuild()
        check("qt: history search matches name", settings._history._list.count() == 1)

        # confirm badges + revision-driven refresh (a full window must not freeze)
        from ui_history import AlertHistoryCard
        from ui_overlay import IconLoader
        from webhook import attach_confirms
        now_t = time.time()
        mixed = [
            {"type": "confirm", "key": "k1", "held": True, "share": 0.82,
             "price": 108.0, "ref_ts": now_t - 300, "ts": now_t - 240},
            {"key": "k1", "name": "Alpha", "game": "poe1",
             "category": "poe1:item:UniqueArmour", "price": 100.0,
             "prev_price": 80.0, "pct": 25.0, "ts": now_t - 300,
             "label": "ORGANIC"},
            {"key": "k2", "name": "Beta", "game": "poe1",
             "category": "poe1:exchange:Currency", "price": 5.0,
             "prev_price": 6.0, "pct": -16.7, "ts": now_t - 600,
             "label": "AMBIGUOUS"},
        ]
        out = attach_confirms(mixed)
        check("history: confirms pair with alerts, markers dropped",
              len(out) == 2 and isinstance(out[0].get("confirm"), dict)
              and out[0]["confirm"]["held"] is True and "confirm" not in out[1],
              str([(r.get("name"), "confirm" in r) for r in out]))
        card = AlertHistoryCard(out[0], IconLoader())
        badge = AlertHistoryCard._confirm_badge(out[0])
        check("history: card badge grades the outcome",
              badge is not None and badge[0].startswith("✓ held") and "82" in badge[0]
              and AlertHistoryCard._confirm_badge(out[1]) is None,
              str(badge))
        card.deleteLater()
        # digest-folded items land in the journal and render with a fold marker
        from webhook import digest_record
        drec = digest_record(ditems[0], "poe1", "Allflame")
        check("history: folded digest item journaled",
              drec["folded"] is True and drec["key"].endswith("det0:")
              and drec["source"] == "item" and drec["details_id"] == "det0"
              and drec["category"] == "poe1:item:UniqueArmour",
              str(drec))
        fcard = AlertHistoryCard(drec, IconLoader())
        check("history: folded card marks the budget fold",
              "folded by budget" in fcard._meta_text(drec),
              fcard._meta_text(drec))
        fcard.deleteLater()

        hist_recs = [rec]
        rev = [0]
        settings.set_history_source(
            lambda limit=200: attach_confirms(list(hist_recs)), lambda: rev[0])
        settings._history.refresh(force=True)
        base_count = settings._history._list.count()
        rev[0] += 1                                   # journal grew behind a full window
        hist_recs.insert(0, {"type": "confirm", "key": rec["key"], "held": False,
                             "share": 0.1, "price": 90.0, "ref_ts": rec["ts"],
                             "ts": rec["ts"] + 1})
        settings._history.refresh()
        check("history: revision bump refreshes a full window with badges",
              settings._history._list.count() == base_count
              and bool(settings._history._records)
              and isinstance(settings._history._records[0].get("confirm"), dict)
              and settings._history._records[0]["confirm"]["held"] is False,
              f"count={settings._history._list.count()}")

        # regression: clicks must survive the list/scroll viewport grab
        from PyQt6.QtCore import QSize as _QSize
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        from ui_history import build_site_url
        from ui_overlay import DigestOverlay
        opened: list = []
        from PyQt6.QtGui import QDesktopServices as _QDS
        _QDS.openUrl = lambda url: opened.append(str(url))
        jrec = {"key": "poe1:i:UniqueArmour:lich:", "name": "Lich's Circle",
                "game": "poe1", "league": "Allflame",
                "category": "poe1:item:UniqueArmour", "price": 19.5,
                "prev_price": 12.0, "pct": 62.5, "ts": time.time(),
                "label": "SUSPECT", "details_id": "lich"}
        check("history: url resolves the 3-part journal category",
              "/poe1/economy/allflame/" in (build_site_url(jrec) or "")
              and "lich" in (build_site_url(jrec) or ""),
              str(build_site_url(jrec)))
        card2 = AlertHistoryCard(jrec, IconLoader())
        lst = QListWidget()
        lst.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        item = QListWidgetItem(lst)
        item.setSizeHint(_QSize(420, 100))
        lst.setItemWidget(item, card2)
        lst.resize(420, 160)
        lst.show()
        app.processEvents()
        QTest.mouseClick(card2, _Qt.MouseButton.LeftButton)
        check("history: card click opens poe.ninja despite the list viewport",
              len(opened) == 1 and "/poe1/economy/allflame/" in opened[0],
              str(opened))
        dovl2 = DigestOverlay(dgt, "poe1", "Allflame", 8.0, "both")
        dovl2.show()
        app.processEvents()
        row0 = dovl2._body.widget().layout().itemAt(0).widget()
        QTest.mouseClick(row0, _Qt.MouseButton.LeftButton)
        check("digest: row click opens the item page",
              len(opened) == 2 and "det0" in opened[1], str(opened))
        card2.deleteLater()
        dovl2.deleteLater()
        lst.deleteLater()

        # the real openUrl must exist and must not raise (Qt6 moved it to
        # QDesktopServices - QGuiApplication.openUrl is gone and clicks were
        # silently dying on AttributeError)
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        opened.clear()
        try:
            QDesktopServices.openUrl(QUrl("https://poe.ninja/poe1/economy"))
            check("click: QDesktopServices.openUrl usable", True)
        except Exception as exc:
            check("click: QDesktopServices.openUrl usable", False, str(exc))
        for f in ("ui_history.py", "ui_overlay.py"):
            src = open(os.path.join(ROOT, f), encoding="utf-8").read()
            check(f"click: {f} no longer calls the removed API",
                  "QDesktopServices.openUrl(" in src
                  and "QGuiApplication.openUrl(" not in src)
        # expanded stacks stay on screen (bottom corner clamps at the top edge)
        geo = QGuiApplication.primaryScreen().availableGeometry()
        d3 = Digest(dgt.count, dgt.names, dgt.categories, dgt.items)
        mgr.show_digest(d3, "poe1", "Allflame")
        mgr.show_digest(d3, "poe1", "Allflame")
        big = mgr._active[-2]
        big.toggle()
        app.processEvents()
        ys = [o.geometry().top() for o in mgr._active]
        check("click: expanded stack clamps inside the screen",
              all(geo.top() <= y for y in ys),
              f"ys={ys}, screen_top={geo.top()}")
        for o in list(mgr._active):
            o.dismiss()
        settings._history.deactivate()
        settings.hide()

        # icon cache LRU cap (bounded pixmap memory)
        from ui_overlay import ICON_CACHE_MAX, IconLoader
        loader = IconLoader()
        for i in range(ICON_CACHE_MAX + 50):
            loader._cache[f"url{i}"] = None
        loader._evict()
        check("qt: icon cache LRU capped", len(loader._cache) == ICON_CACHE_MAX
              and "url0" not in loader._cache
              and f"url{ICON_CACHE_MAX + 49}" in loader._cache,
              str(len(loader._cache)))

        # category type-to-filter (Monitoring tab)
        settings._cat_search.setText("frag")
        visible = [i for i in range(settings._cat_list.count())
                   if not settings._cat_list.item(i).isHidden()]
        check("qt: category filter 'frag'",
              len(visible) == 1
              and settings._cat_list.item(visible[0]).text() == "Fragments",
              str(len(visible)))
        settings._cat_search.setText("s")
        visible_s = sum(1 for i in range(settings._cat_list.count())
                        if not settings._cat_list.item(i).isHidden())
        check("qt: category filter 's' matches many", visible_s > 10, str(visible_s))
        settings._cat_search.setText("")
        check("qt: empty filter restores all",
              all(not settings._cat_list.item(i).isHidden()
                  for i in range(settings._cat_list.count())))

        QTimer.singleShot(400, app.quit)
        app.exec()
    except Exception as exc:
        import traceback
        check("qt: widgets constructed", False, traceback.format_exc(limit=3))


# -------------------------------------------------------- worker hand-off
def test_worker() -> None:
    print("== analysis worker (QThread hand-off) ==")
    try:
        import tempfile

        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)

        import analysis_worker as aw
        from analysis_worker import AnalysisWorker
        from api_client import FetchMeta, ItemQuote
        from appconfig import Config, ScenarioCfg

        cfg = Config()
        cfg.scenarios = [ScenarioCfg(name="any spike", game="poe1", category="*",
                                     metric="pct", op=">=", value=1.0,
                                     cooldown_minutes=60.0)]
        tmp = tempfile.TemporaryDirectory()
        real_app_dir = aw.app_dir
        aw.app_dir = lambda: tmp.name          # isolate history DBs from the app's
        try:
            worker = AnalysisWorker(cfg, lambda g: "")
            results: list = []
            worker.analyzed.connect(lambda r: results.append(r))
            worker.start()

            meta = FetchMeta(game="poe1", league="Allflame", category="Currency",
                             source="exchange", primary="chaos")
            quotes = [ItemQuote(key=f"poe1:x:Currency:div{i}", name=f"Divine {i}",
                                game="poe1", category="Currency", source="exchange",
                                price=100.0 + i, list_count=None, icon=None,
                                details_id="divine", volume=42.0, aux=0.9)
                      for i in range(5)]
            worker.submit_snapshot("poe1", quotes, meta)
            deadline = time.time() + 10
            while time.time() < deadline and not results:
                app.processEvents()
                time.sleep(0.05)
            check("worker: analyzed result emitted", len(results) == 1)
            if results:
                r = results[0]
                check("worker: result carries game/meta/shown",
                      r["game"] == "poe1" and r["meta"] is meta
                      and isinstance(r["shown"], list))
            stats = worker.stats()
            check("worker: stats populated", stats.get("poe1", (0, 0))[1] >= 1, str(stats))

            # scenario hook: the basic engine needs 3 prior points before a
            # jump can alert; the 4th snapshot (100->150) then produces an
            # anomaly that the pct>=1 scenario must catch
            prices = (100.0, 102.0, 104.0, 150.0)
            for i in range(1, 4):
                quotes_i = [ItemQuote(key=f"poe1:x:Currency:div{j}", name=f"Divine {j}",
                                      game="poe1", category="Currency",
                                      source="exchange", price=prices[i],
                                      list_count=None, icon=None, details_id="divine",
                                      volume=42.0, aux=0.9) for j in range(5)]
                time.sleep(1.1)                # distinct tick timestamps
                worker.submit_snapshot("poe1", quotes_i, meta)
                deadline = time.time() + 10
                while time.time() < deadline and len(results) <= i:
                    app.processEvents()
                    time.sleep(0.05)
            check("worker: 4 snapshots analyzed", len(results) == 4, str(len(results)))
            hits = results[-1].get("scenario_hits") if results else []
            check("worker: scenario hit emitted", bool(hits), str(len(hits)))
            if hits:
                check("worker: scenario hit carries anomaly + name",
                      hits[0].scenario == "any spike" and hits[0].anomaly.key)

            # mute route: submit_mute -> worker cmd -> governor.mute applied
            mute_key = "poe1:x:Currency:div0"
            worker.submit_mute(mute_key, 30.0)
            deadline = time.time() + 5
            while time.time() < deadline and mute_key not in worker.muted_map():
                app.processEvents()
                time.sleep(0.05)
            check("worker: mute command reaches the governor",
                  mute_key in worker.muted_map(), str(worker.muted_map()))
            worker.submit_mute(mute_key, 0.0)             # unmute variant
            deadline = time.time() + 5
            while time.time() < deadline and mute_key in worker.muted_map():
                app.processEvents()
                time.sleep(0.05)
            check("worker: unmute command clears the mute",
                  mute_key not in worker.muted_map(), str(worker.muted_map()))

            worker.apply_config(cfg)           # config command path
            worker.reset_game("poe1")          # rollover reset path
            worker.stop()
            worker.wait(8000)
            check("worker: clean stop closes stores", not worker.isRunning())
        finally:
            aw.app_dir = real_app_dir
            tmp.cleanup()
    except Exception:
        import traceback
        check("worker: suite ran", False, traceback.format_exc(limit=3))


# ---------------------------------------------------------------- live api
def test_live() -> None:
    print("== live poe.ninja ==")
    from api_client import UNCHANGED, ApiError, NinjaApiClient

    client = NinjaApiClient()
    for game in ("poe1", "poe2"):
        try:
            leagues = client.get_leagues(game, force=True)
            check(f"live: leagues {game}", len(leagues) > 0, "empty list")
            league = leagues[0].id
            result, meta = client.fetch_category(game, league, "Currency")
            n = 0 if result is UNCHANGED else len(result)
            check(f"live: {game}/{league} currency quotes", n > 5, str(n))
            item_cat = "UniqueWeapon" if game == "poe1" else "UniqueWeapons"
            result, meta = client.fetch_category(game, league, item_cat)
            n = 0 if result is UNCHANGED else len(result)
            check(f"live: {game}/{league} {item_cat} quotes", n > 0, str(n))
        except ApiError as exc:
            check(f"live: {game} fetch", False, str(exc))


def main() -> int:
    test_config()
    test_analyzer()
    test_parsers()
    test_advanced()
    test_hardening()
    test_features()
    test_worker()
    test_poller()
    test_qt()
    if "--live" in sys.argv:
        test_live()
    print()
    if FAILURES:
        print(f"SELFTEST FAILED — {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
