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
        worker.category_done.connect(lambda label, n: got["cats"].append((label, n)))
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
        snap = Snapshot(game="poe1", category="poe1:x:Currency", league="Allflame",
                        ts=t0 + 60 * step, source="documented",
                        ticks=(history[-1],))
        anoms = analyzer2.process(snap)
        check("robust z: warm start replayed 60 ticks", replayed == 60, str(replayed))
        check("robust z: +40% spike flagged", len(anoms) == 1 and anoms[0].z_adj > 3.0,
              f"{[round(a.z_adj, 2) for a in anoms]}")
        check("robust z: phase label present", anoms and anoms[0].phase in
              ("Launch", "Stabilization", "Mature", "Decay"))

        # ---- 4. governor token bucket + digest ---------------------------
        gov_cfg = Alerts()
        gov_cfg.bucket_capacity = 3
        gov = AlertGovernor(gov_cfg)
        anoms8 = []
        for i in range(8):
            a = anoms[0] if i == 0 else anoms[0]
            anoms8.append(a)
        shown, digest = gov.decide(anoms8)
        check("governor: bucket admits 3", len(shown) <= 3, str(len(shown)))
        check("governor: digest folds the rest", digest is not None and digest.count == 8 - len(shown))

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
                           price=100.0, volume=None, listings=50.0, aux=None,
                           ts=t0 + i * 900.0) for i in range(40)]
        samples = collect_training_rows(DetectorCfg(), lambda g: "", replay)
        check("calib: replay collects ready rows", len(samples) >= 10, str(len(samples)))
        feats, manip = samples[-1]
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
        cfgd.webhook.format = "telegram"
        cfgd.webhook.url = ""
        bad = [r for r in run_checks(cfgd, td) if r.name == "Webhook / Telegram"]
        check("diag: telegram without chat id flagged", bad and not bad[0].ok)
        cfgd.webhook.url = "123456:ABC-token"
        cfgd.webhook.chat_id = "42"
        good = [r for r in run_checks(cfgd, td) if r.name == "Webhook / Telegram"]
        check("diag: telegram token accepted", good and good[0].ok, good[0].detail)

        # ---- telegram payload (stubbed transport) ------------------------------
        import webhook as wh
        cfgt = Config()
        cfgt.webhook.enabled = True
        cfgt.webhook.format = "telegram"
        cfgt.webhook.url = "123456:ABC-token"
        cfgt.webhook.chat_id = "42"
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
        check("telegram: bare token -> sendMessage URL",
              captured and captured[0][0] ==
              "https://api.telegram.org/bot123456:ABC-token/sendMessage", str(captured))
        check("telegram: body carries chat_id + text",
              captured and captured[0][1]["data"] and
              '"chat_id": "42"' in captured[0][1]["data"].replace("'", '"'))
        cfgt2 = Config()
        cfgt2.webhook.enabled = True
        cfgt2.webhook.format = "telegram"
        cfgt2.webhook.url = "https://t/x"
        cfgt2.webhook.chat_id = ""
        sender2 = wh.WebhookSender(lambda: cfgt2)
        try:
            sender2.notify({"text": "x", "priority": 1.0})
            check("telegram: empty chat id skipped gracefully",
                  sender2._queue.qsize() == 0)
        finally:
            sender2.stop()

        # ---- alert journal history read ----
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
        from appconfig import ScenarioCfg
        settings._scen_draft.append(ScenarioCfg(name="t", game="poe1",
                                                metric="pct", value=5.0))
        settings._reload_scenarios()
        check("qt: scenario draft listed", settings._scen_list.count() == 1)
        settings.set_status_provider(lambda: {
            "engine": "auto", "auto_calibrate": False, "scenarios": 1,
            "poller_states": {"poe1": "waiting"}, "worker_stats": {"poe1": (7, 21)},
            "last_detail": "ok", "leagues": {"poe1": "Allflame"}})
        settings.show()
        settings._refresh_status()
        check("qt: status tab renders provider snapshot",
              "7 robust + 21 basic" in settings._live_labels["poe1"].text(),
              settings._live_labels["poe1"].text())

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
        settings._history._search.setText("mirror-of-kalandra")
        settings._history._rebuild()
        check("qt: history filter yields empty state",
              settings._history._list.count() == 1
              and settings._history._list.item(0).text().startswith("no alerts"),
              settings._history._list.item(0).text() if settings._history._list.count() else "?")
        settings._history._search.setText("divine")
        settings._history._rebuild()
        check("qt: history search matches name", settings._history._list.count() == 1)
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
