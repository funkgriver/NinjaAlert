"""NinjaAlert - application configuration (defaults, load, migration, atomic save).

Schema v2 (spec-aligned):
  network.contact      -> required by poe.ninja politeness contract (User-Agent contact)
  network.base_url     -> point at a shared cache/proxy when distributing
  games.<g>            -> per-game enabled/league/league_start/interval/watch
  detector.engine      -> "auto" (phase-adjusted robust engine + basic warmup) | "basic"
  webhook              -> optional outbound notifications (generic JSON POST)

Legacy v1 configs (top-level game/league/watch/...) are migrated on load.
The config file lives next to the executable (portable style).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional

from categories import CATEGORIES, DEFAULT_WATCH

log = logging.getLogger("ninjaalert.config")

CONFIG_FILENAME = "config.json"

VALID_GAMES = ("poe1", "poe2")
VALID_CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
VALID_ENGINES = ("auto", "basic")
VALID_PRICE_DISPLAY = ("primary", "secondary", "both")


def app_dir() -> str:
    """Directory of the frozen exe (PyInstaller) or of the project root."""
    if getattr(sys, "frozen", False):  # PyInstaller onefile
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_FILENAME)


def resource_path(rel: str) -> str:
    """Absolute path for a bundled resource (PyInstaller onefile aware)."""
    base = getattr(sys, "_MEIPASS", None)     # frozen one-file extraction dir
    if base:
        cand = os.path.join(base, rel)
        if os.path.exists(cand):
            return cand
    return os.path.join(app_dir(), rel)


def _default_games() -> Dict[str, "GameCfg"]:
    return {
        "poe1": GameCfg(enabled=True, poll_interval_minutes=15.0,
                        watch=list(DEFAULT_WATCH["poe1"])),
        "poe2": GameCfg(enabled=False, poll_interval_minutes=15.0,
                        watch=list(DEFAULT_WATCH["poe2"])),
    }


@dataclass
class GameCfg:
    enabled: bool = True
    league: Optional[str] = None          # None/empty = first entry of /leagues
    league_auto: bool = True
    league_start: str = ""                # YYYY-MM-DD; empty = auto (see leagues_seen)
    poll_interval_minutes: float = 15.0
    watch: List[str] = field(default_factory=list)
    # sections discovered at runtime from poe.ninja's site index (survive restart);
    # each entry: {id, source, label, slug}
    custom_sections: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class NetworkCfg:
    base_url: str = "https://poe.ninja"   # drop in a proxy/cache URL when distributing
    contact: str = ""                     # poe.ninja asks for a contact in the User-Agent
    timeout_s: float = 20.0


@dataclass
class DetectorCfg:
    engine: str = "auto"                  # auto | basic
    # ---- league-phase model (spec section 1) -------------------------------
    z0: float = 5.0
    z_inf: float = 3.0
    tau_z_days: float = 7.0
    gamma: float = 1.0
    decay_start_days: float = 56.0
    decay_width_days: float = 7.0
    tau_h_days: float = 10.0
    fast_h_min: float = 4.0
    fast_h_max: float = 12.0
    slow_h_min: float = 24.0
    slow_h_max: float = 72.0
    default_league_day: float = 30.0      # used when league_start is unknown
    # ---- robust statistics (spec section 2) --------------------------------
    window: int = 192
    min_history: int = 24
    mad_floor: float = 0.0025
    min_log_move: float = 0.02
    # empirical alert-rate cap: pick the category z* so raw (pre-governor)
    # alerts stay near this target; the phase model remains the lower bound
    target_alerts_per_category_per_day: float = 20.0
    liquidity_l0: float = 20.0
    max_gap_hours: float = 72.0
    # ---- confirmation / classification (spec sections 3-4) -----------------
    crossover_confirm: float = 0.75
    rvol_ref: float = 1.5
    no_volume_prior: float = 0.4
    basis_saturation: float = 0.15
    breadth_z: float = 2.0
    breadth_min_peers: int = 8
    weights: List[float] = field(default_factory=lambda: [1.5, 1.2, 1.5, 1.0, 0.8, 0.6])
    bias: float = -3.0
    suspect_at: float = 0.65
    organic_at: float = 0.35
    # ---- weight refit (audit Phase 1; calibration.py) -----------------------
    auto_calibrate: bool = False          # in-app periodic refit (default: off)
    calibrate_interval_hours: float = 24.0
    # ---- storage ------------------------------------------------------------
    history_retention_days: float = 14.0


@dataclass
class Thresholds:
    # Basic (delta) engine + warmup stage of "auto"
    jump_pct: float = 20.0
    min_price: Dict[str, float] = field(default_factory=lambda: {"poe1": 5.0, "poe2": 0.05})
    volume_mult: float = 3.0
    volume_min_count: float = 10.0
    mode: str = "any"
    baseline_window: int = 6
    cooldown_minutes: float = 90.0


@dataclass
class Alerts:
    sound: bool = True
    popup_seconds: float = 15.0
    max_popups: int = 5
    # price rendering in popups: primary (chaos PoE1 / divine PoE2),
    # secondary (divine PoE1 / exalted PoE2) or both with a conversion hint
    price_display: str = "both"
    # one early re-poll after an alert fires, to confirm the move (0 = off)
    confirm_rescan_seconds: float = 120.0
    # ---- alert budget (spec section 5) --------------------------------------
    bucket_capacity: int = 5              # tokens
    refill_seconds: float = 120.0         # 1 token per 2 minutes
    cooldown_base_s: float = 1800.0       # 30 min, doubles per re-alert
    cooldown_max_s: float = 14400.0       # 4 h cap
    escalation_factor: float = 1.5        # re-alert allowed at >= 1.5x last priority
    max_digest_names: int = 4


@dataclass
class Overlay:
    corner: str = "bottom-right"
    margin: int = 24
    opacity: float = 0.92


@dataclass
class WebhookCfg:
    enabled: bool = False
    url: str = ""                         # generic POST endpoint (full anomaly JSON)
    min_priority: float = 0.0             # only send anomalies with priority >= this
    max_per_minute: int = 12


VALID_METRICS = ("pct", "z_abs", "rvol", "priority")
VALID_OPS = (">=", "<=")
VALID_DIRECTIONS = ("any", "up", "down")


@dataclass
class ScenarioCfg:
    """One user-defined automation scenario (Scenarios tab in Settings)."""
    name: str = ""
    game: str = "poe1"                    # poe1 | poe2
    category: str = "*"                   # category id or "*" (any)
    name_pattern: str = ""                # case-insensitive substring on item name
    metric: str = "pct"                   # pct | z_abs | rvol | priority
    op: str = ">="                        # >= | <=
    value: float = 20.0
    direction: str = "any"                # any | up | down
    cooldown_minutes: float = 60.0        # per (scenario, item key)
    enabled: bool = True


@dataclass
class Config:
    network: NetworkCfg = field(default_factory=NetworkCfg)
    games: Dict[str, GameCfg] = field(default_factory=_default_games)
    detector: DetectorCfg = field(default_factory=DetectorCfg)
    thresholds: Thresholds = field(default_factory=Thresholds)
    alerts: Alerts = field(default_factory=Alerts)
    overlay: Overlay = field(default_factory=Overlay)
    webhook: WebhookCfg = field(default_factory=WebhookCfg)
    scenarios: List[ScenarioCfg] = field(default_factory=list)
    # league -> date this install first saw it (drives auto league_start)
    leagues_seen: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._sanitize()

    # ---------------------------------------------------------------- utils
    def game_cfg(self, game: str) -> GameCfg:
        return self.games.setdefault(game, GameCfg())

    def register_custom_section(self, game: str, cat_id: str, source: str,
                                label: str, slug: str = "") -> bool:
        """Persist a runtime-discovered poe.ninja section (discovery.py).

        Registers the id into the in-memory category registry via _sanitize
        and auto-enables it in the watch list. Returns False when the section
        is already known (built-in or previously registered).
        """
        from categories import CATEGORIES
        if cat_id in CATEGORIES.get(game, {}):
            return False
        gc = self.game_cfg(game)
        if any(s.get("id") == cat_id for s in gc.custom_sections):
            return False
        gc.custom_sections.append({"id": cat_id, "source": source,
                                   "label": label or cat_id, "slug": slug or ""})
        if cat_id not in gc.watch:
            gc.watch.append(cat_id)
        self._sanitize()
        return True

    def unregister_custom_section(self, game: str, cat_id: str) -> None:
        """Drop a custom section from the watch list (registry entry stays for
        the session so poller state never dangles)."""
        gc = self.game_cfg(game)
        gc.watch = [c for c in gc.watch if c != cat_id]
        gc.custom_sections = [s for s in gc.custom_sections if s.get("id") != cat_id]

    def watched(self, game: Optional[str] = None) -> List[str]:
        g = game or "poe1"
        return list(self.game_cfg(g).watch)

    def min_price_for(self, game: Optional[str] = None) -> float:
        return float(self.thresholds.min_price.get(game or "poe1", 0.0))

    @property
    def user_agent(self) -> str:
        return f"NinjaAlert/2.0 (contact: {self.network.contact.strip() or 'unset'})"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Optional[str] = None) -> None:
        path = path or config_path()
        data = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json",
                                   dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------- loading
    @staticmethod
    def load(path: Optional[str] = None) -> "Config":
        path = path or config_path()
        cfg = Config()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return cfg
        cfg._merge(data)
        cfg._sanitize()
        return cfg

    @staticmethod
    def load_or_create(path: Optional[str] = None) -> "Config":
        path = path or config_path()
        created = False
        try:
            cfg = Config.load(path)
        except FileNotFoundError:
            cfg = Config()
            created = True
        except Exception as exc:  # malformed JSON etc.
            log.warning("config unreadable (%s) - using defaults", exc)
            cfg = Config()
        if created:
            try:
                cfg.save(path)
                log.info("created default config at %s", path)
            except Exception as exc:
                log.warning("could not write default config: %s", exc)
        return cfg

    # -------------------------------------------------------------- merging
    def _merge(self, data: Dict[str, Any]) -> None:
        # nested sections first
        _apply_fields(self.network, data.get("network"))
        _apply_fields(self.detector, data.get("detector"))
        _apply_fields(self.thresholds, data.get("thresholds"),
                      special={"min_price": self.thresholds.min_price})
        _apply_fields(self.alerts, data.get("alerts"))
        _apply_fields(self.overlay, data.get("overlay"))
        _apply_fields(self.webhook, data.get("webhook"))

        games_raw = data.get("games")
        if isinstance(games_raw, dict):
            for g, raw in games_raw.items():
                if g not in VALID_GAMES or not isinstance(raw, dict):
                    continue
                gc = self.game_cfg(g)
                _apply_fields(gc, raw, special={"watch": gc.watch,
                                                "custom_sections": gc.custom_sections})
        else:
            self._migrate_legacy(data)

        scen = data.get("scenarios")
        if isinstance(scen, list):
            parsed: List[ScenarioCfg] = []
            for raw in scen:
                if isinstance(raw, ScenarioCfg):
                    parsed.append(raw)
                elif isinstance(raw, dict):
                    sc = ScenarioCfg()
                    _apply_fields(sc, raw)
                    parsed.append(sc)
            self.scenarios = parsed

        seen = data.get("leagues_seen")
        if isinstance(seen, dict):
            for g, leagues in seen.items():
                if g in VALID_GAMES and isinstance(leagues, dict):
                    self.leagues_seen[g] = {str(k): str(v) for k, v in leagues.items()
                                            if isinstance(k, str) and isinstance(v, str)}

    def _migrate_legacy(self, data: Dict[str, Any]) -> None:
        """v1 config: top-level game/league/league_auto/poll_interval_minutes/watch."""
        game = data.get("game") if data.get("game") in VALID_GAMES else "poe1"
        gc = self.game_cfg(game)
        if isinstance(data.get("league"), str) and data["league"].strip():
            gc.league = data["league"].strip()
            gc.league_auto = False if data.get("league_auto") is False else False
        elif data.get("league_auto") is False:
            gc.league_auto = False
        if isinstance(data.get("league_auto"), bool):
            gc.league_auto = data["league_auto"]
            if gc.league_auto:
                gc.league = None
        if isinstance(data.get("poll_interval_minutes"), (int, float)):
            gc.poll_interval_minutes = float(data["poll_interval_minutes"])
        watch = data.get("watch")
        if isinstance(watch, dict) and isinstance(watch.get(game), list):
            gc.watch = [str(x) for x in watch[game]]
        log.info("migrated legacy config for game=%s", game)

    def _sanitize(self) -> None:
        from categories import CATEGORIES as _CATS
        for g in VALID_GAMES:
            gc = self.game_cfg(g)
            valid = _CATS[g]
            # runtime-discovered sections join the registry (idempotent) so
            # watch entries can reference them like built-in categories
            known = dict(valid)
            for sec in gc.custom_sections:
                if (isinstance(sec, dict) and isinstance(sec.get("id"), str) and sec["id"]
                        and sec.get("source") in ("exchange", "item")):
                    if sec["id"] not in known:
                        from categories import CatDef
                        valid[sec["id"]] = CatDef(sec["id"], sec["source"],
                                                  str(sec.get("label") or sec["id"]),
                                                  sec.get("slug") or None)
                        known[sec["id"]] = valid[sec["id"]]
            gc.watch = [c for c in gc.watch if c in known] or list(DEFAULT_WATCH[g])
            gc.custom_sections = [
                {"id": s["id"], "source": s["source"],
                 "label": str(s.get("label") or s["id"]), "slug": str(s.get("slug") or "")}
                for s in gc.custom_sections
                if isinstance(s, dict) and s.get("id") in known]
            gc.poll_interval_minutes = min(max(gc.poll_interval_minutes, 5.0), 240.0)
            if gc.league is not None and not str(gc.league).strip():
                gc.league = None
            if gc.league_start:
                try:
                    from datetime import date
                    date.fromisoformat(gc.league_start)
                except ValueError:
                    log.warning("games.%s.league_start %r invalid - ignoring", g, gc.league_start)
                    gc.league_start = ""
        d = self.detector
        if d.engine not in VALID_ENGINES:
            d.engine = "auto"
        d.window = max(48, int(d.window))
        d.min_history = min(max(8, int(d.min_history)), d.window - 1)
        if len(d.weights) != 6 or not all(isinstance(w, (int, float)) for w in d.weights):
            d.weights = [1.5, 1.2, 1.5, 1.0, 0.8, 0.6]
        # audit: clamp magnitudes so one miscalibrated feature cannot dominate
        d.weights = [min(max(float(w), 0.0), 5.0) for w in d.weights]
        d.bias = min(max(float(d.bias), -10.0), 5.0)
        d.calibrate_interval_hours = min(max(float(d.calibrate_interval_hours), 6.0), 168.0)
        self.thresholds.baseline_window = min(max(self.thresholds.baseline_window, 3), 24)
        self.thresholds.cooldown_minutes = min(max(self.thresholds.cooldown_minutes, 5.0), 1440.0)
        self.thresholds.jump_pct = min(max(self.thresholds.jump_pct, 1.0), 2000.0)
        self.alerts.popup_seconds = min(max(self.alerts.popup_seconds, 5.0), 300.0)
        if self.alerts.price_display not in VALID_PRICE_DISPLAY:
            self.alerts.price_display = "both"
        self.alerts.confirm_rescan_seconds = min(
            max(float(self.alerts.confirm_rescan_seconds), 0.0), 600.0)
        self.alerts.max_popups = min(max(self.alerts.max_popups, 1), 12)
        self.alerts.bucket_capacity = max(1, int(self.alerts.bucket_capacity))
        self.alerts.refill_seconds = max(10.0, float(self.alerts.refill_seconds))
        self.alerts.cooldown_base_s = max(60.0, float(self.alerts.cooldown_base_s))
        self.alerts.cooldown_max_s = max(self.alerts.cooldown_base_s,
                                         float(self.alerts.cooldown_max_s))
        self.overlay.margin = min(max(self.overlay.margin, 0), 200)
        self.overlay.opacity = min(max(self.overlay.opacity, 0.3), 1.0)
        if self.overlay.corner not in VALID_CORNERS:
            self.overlay.corner = "bottom-right"
        seen_names: set = set()
        kept: List[ScenarioCfg] = []
        for raw in list(self.scenarios):
            if isinstance(raw, ScenarioCfg):
                sc = raw
            elif isinstance(raw, dict):          # tolerant: dicts coerced in-memory too
                sc = ScenarioCfg()
                _apply_fields(sc, raw)
            else:
                continue
            sc.name = str(sc.name).strip() or "scenario"
            base, n = sc.name, 2
            while sc.name in seen_names:           # names key UI rows and cooldowns
                sc.name = f"{base} #{n}"
                n += 1
            seen_names.add(sc.name)
            if sc.game not in VALID_GAMES:
                sc.game = "poe1"
            if sc.category != "*" and sc.category not in _CATS[sc.game]:
                sc.category = "*"
            if sc.metric not in VALID_METRICS:
                sc.metric = "pct"
            if sc.op not in VALID_OPS:
                sc.op = ">="
            if sc.direction not in VALID_DIRECTIONS:
                sc.direction = "any"
            sc.value = float(sc.value)
            sc.cooldown_minutes = min(max(float(sc.cooldown_minutes), 5.0), 10080.0)
            kept.append(sc)
        self.scenarios = kept
        self.network.timeout_s = min(max(self.network.timeout_s, 3.0), 120.0)
        if self.network.base_url and not str(self.network.base_url).startswith(("http://", "https://")):
            self.network.base_url = "https://poe.ninja"


# ------------------------------------------------------ generic field applier
def _apply_fields(dc: Any, data: Optional[Dict[str, Any]],
                  special: Optional[Dict[str, Any]] = None) -> None:
    """Type-checked merge of a dict into a dataclass; unknown/bad keys are logged
    and ignored (never fatal), per the spec's tolerant-loading rule."""
    if not isinstance(data, dict) or not is_dataclass(dc):
        return
    known = {f.name for f in fields(dc)}
    for key, value in data.items():
        if key in (special or {}):
            target = special[key]
            if isinstance(value, dict):
                target.update({k: v for k, v in value.items()})
            elif isinstance(value, list) and isinstance(target, list):
                if value and isinstance(value[0], dict):
                    target[:] = [v for v in value if isinstance(v, dict)]
                else:
                    target[:] = [str(v) for v in value]
            continue
        if key not in known:
            log.debug("unknown config key %s.%s ignored", type(dc).__name__, key)
            continue
        cur = getattr(dc, key)
        try:
            if isinstance(cur, bool) or cur is None and key.endswith(("_auto", "enabled", "sound")):
                if not isinstance(value, bool):
                    raise ValueError("expected true/false")
                setattr(dc, key, value)
            elif isinstance(cur, bool):
                if not isinstance(value, bool):
                    raise ValueError("expected true/false")
                setattr(dc, key, value)
            elif isinstance(cur, float):
                setattr(dc, key, float(value))
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(dc, key, int(value))
            elif isinstance(cur, str) or cur is None:
                setattr(dc, key, "" if value is None else str(value))
            elif isinstance(cur, list):
                if not isinstance(value, list):
                    raise ValueError("expected a list")
                if cur and isinstance(cur[0], float):
                    setattr(dc, key, [float(v) for v in value])
                else:
                    setattr(dc, key, [str(v) for v in value])
            else:
                setattr(dc, key, value)
        except (TypeError, ValueError) as exc:
            log.warning("config key %s.%s ignored (%s)", type(dc).__name__, key, exc)
