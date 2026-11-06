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

        QTimer.singleShot(400, app.quit)
        app.exec()
    except Exception as exc:
        import traceback
        check("qt: widgets constructed", False, traceback.format_exc(limit=3))


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
