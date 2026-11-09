"""NinjaAlert — poe.ninja price-spike monitor for PoE 1 / PoE 2.

Entry point: system-tray app wiring per-game pollers -> per-game engines ->
toast overlays / webhook / alert journal. No main window: everything lives
in the tray.

Automation model (spec: "PoE Market Anomaly Detector", Nov 2026):
  * one polling QThread per game - both games monitored in parallel
  * detection engine "auto": league-phase-adjusted robust engine + basic
    delta engine as warmup fallback until robust baselines are ready
  * alert budget: token bucket + escalating cooldowns + digest lines
  * league rollover: auto-detected -> history reset, league_start recorded
  * webhook (generic JSON POST) + alerts.jsonl journal

Usage:
    python main.py            # start monitoring
    python main.py --debug    # verbose console logging + settings window
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from datetime import date
from typing import Dict, Optional

from PyQt6.QtCore import QObject, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from analyzer import Alert
from analysis_worker import AnalysisWorker
from api_client import NinjaApiClient
from appconfig import Config, app_dir, config_path, resource_path
from dataclasses import replace as dc_replace
from poller import PollWorker
from ui_overlay import ToastManager
from ui_settings import SettingsWindow
from webhook import (AlertJournal, WebhookSender, attach_confirms, confirm_record,
                     digest_record, format_alert_text)

log = logging.getLogger("ninjaalert.main")

SINGLE_INSTANCE_KEY = "NinjaAlert-LocalInstance"
TRAY_COLORS = {"ok": "#41d98c", "warn": "#e8b93e", "error": "#e5534b", "paused": "#9aa4b2"}


# --------------------------------------------------------------------- setup
def setup_logging(debug: bool = False) -> None:
    log_file = os.path.join(app_dir(), "ninjaalert.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("ninjaalert")
    root.setLevel(logging.DEBUG)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if debug:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(fmt)
        root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_app_icon() -> QIcon:
    """Custom bundled icon (assets/app.ico); drawn fallback when missing."""
    path = resource_path("assets/app.ico")
    if os.path.exists(path):
        return QIcon(path)
    return render_tray_icon("ok")


def _drawn_icon_pixmap(size: int = 64) -> QPixmap:
    """Legacy programmatic icon (fallback when the .ico asset is absent)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64.0
    p.setBrush(QColor("#12151d"))
    pen = QPen(QColor("#3a4354"))
    pen.setWidthF(2.0 * s)
    p.setPen(pen)
    p.drawRoundedRect(int(2 * s), int(2 * s), int(size - 4 * s), int(size - 4 * s),
                      int(14 * s), int(14 * s))
    color = QColor(TRAY_COLORS["ok"])
    pen = QPen(color)
    pen.setWidthF(5.0 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    points = [(12 * s, 46 * s), (22 * s, 46 * s), (28 * s, 32 * s),
              (36 * s, 38 * s), (50 * s, 16 * s)]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        p.drawLine(int(x1), int(y1), int(x2), int(y2))
    p.drawLine(int(50 * s), int(16 * s), int(40 * s), int(17 * s))
    p.drawLine(int(50 * s), int(16 * s), int(49 * s), int(26 * s))
    p.end()
    return pix


def render_tray_icon(state: str = "ok") -> QIcon:
    """Custom app icon with a state badge dot (bottom-left); the badge keeps
    the tray informative while the artwork stays recognizable."""
    icon = QIcon(resource_path("assets/app.ico")) if \
        os.path.exists(resource_path("assets/app.ico")) else None
    base = QPixmap(icon.pixmap(64, 64)) if icon is not None and not icon.pixmap(64, 64).isNull() \
        else _drawn_icon_pixmap(64)
    if base.isNull():
        base = _drawn_icon_pixmap(64)
    pix = QPixmap(base.size())
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.drawPixmap(0, 0, base)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(TRAY_COLORS.get(state, TRAY_COLORS["ok"])))
    p.setPen(QPen(QColor("#12151d"), 2.0))
    d = base.width() * 0.28
    p.drawEllipse(int(base.width() * 0.06), int(base.height() * 0.62), int(d), int(d))
    p.end()
    return QIcon(pix)


class SingleInstanceGuard(QObject):
    """Second launch just brings up the settings window of the first app."""

    def __init__(self, on_activate, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._on_activate = on_activate
        self._server: Optional[QLocalServer] = None

    def acquire(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(SINGLE_INSTANCE_KEY)
        if probe.waitForConnected(300):
            probe.write(b"show\n")
            probe.flush()
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
        self._server = QLocalServer()
        self._server.newConnection.connect(self._on_connection)
        if not self._server.listen(SINGLE_INSTANCE_KEY):
            log.warning("single-instance server failed: %s", self._server.errorString())
        return True

    def _on_connection(self) -> None:
        sock = self._server.nextPendingConnection()
        if sock:
            sock.readAll()
            sock.deleteLater()
        self._on_activate()


def os_startfile(url: str) -> None:
    try:
        os.startfile(url)  # type: ignore[attr-defined]  # Windows
    except AttributeError:
        import webbrowser
        webbrowser.open(url)


# ------------------------------------------------------------------- control
class NinjaAlertController(QObject):
    """Owns config, per-game pollers + engines, overlays, tray and settings."""

    def __init__(self) -> None:
        super().__init__()
        self._cfg = Config.load_or_create()
        if not self._cfg.network.contact.strip():
            log.warning("network.contact is empty - poe.ninja asks every client "
                        "to identify itself; set it in Settings or config.json")

        base = app_dir()
        self._journal = AlertJournal(os.path.join(base, "alerts.jsonl"))
        self._journal.rotate_if_large()            # keep the journal size-bounded
        self._webhook = WebhookSender(lambda: self._cfg)
        self._toasts = ToastManager(lambda: self._cfg, base)
        self._toasts.item_muted.connect(self._on_item_muted)

        self._pollers: Dict[str, PollWorker] = {}
        self._worker: Optional[AnalysisWorker] = None
        self._icon_map: Dict[str, str] = {}
        self._meta_league: Dict[str, str] = {}       # league seen in latest meta per game
        self._pending_confirms: Dict[str, dict] = {} # key -> alert ref awaiting re-scan (C9)
        self._current_league: Dict[str, str] = {}    # league from leagues endpoint
        self._leagues_cache: Dict[str, List[str]] = {}   # last ids per game (UI fill)
        self._runtime_new_leagues: set = set()       # rollovers detected while running
        self._poller_states: Dict[str, str] = {}
        self._scan_done: Dict[str, int] = {}           # per-game cycle progress
        self._last_detail = ""
        self._greeted = False

        self._build_worker()
        self._build_pollers()
        self._build_tray()

    # ------------------------------------------------------------- factories
    def _league_start_fn(self, game: str) -> str:
        gc = self._cfg.game_cfg(game)
        if gc.league_start:
            return gc.league_start
        current = self._current_league.get(game, "")
        first = self._cfg.leagues_seen.get(game, {}).get(current, "")
        # a rollover detected while running (or launch on the league's first day)
        # is a trustworthy day 0; anything else falls back to the neutral default
        if first and first == date.today().isoformat() and current in self._runtime_new_leagues:
            return first
        return ""

    def _build_worker(self) -> None:
        """All engine state (pipelines + basic detectors) lives on the worker
        thread - warm-start replay and per-snapshot analysis never block the GUI."""
        self._worker = AnalysisWorker(self._cfg, self._league_start_fn)
        self._worker.analyzed.connect(self._on_analyzed)
        self._worker.note.connect(self._on_worker_note)
        # watchdog: a dead worker means silently dead monitoring - restart it
        self._worker_watchdog = QTimer(self)
        self._worker_watchdog.setInterval(60_000)
        self._worker_watchdog.timeout.connect(self._check_worker_alive)

    def _check_worker_alive(self) -> None:
        if self._worker is not None and not self._worker.isRunning():
            log.critical("analysis worker died - restarting it")
            self._worker.start()

    def _build_pollers(self) -> None:
        for game in ("poe1", "poe2"):
            client = NinjaApiClient(
                user_agent_fn=lambda: self._cfg.user_agent,
                base_url_fn=lambda: self._cfg.network.base_url,
                timeout_fn=lambda: self._cfg.network.timeout_s,
            )
            worker = PollWorker(client, self._cfg, game)
            worker.snapshot.connect(lambda q, m, g=game: self._on_snapshot(g, q, m))
            worker.leagues_found.connect(self._on_leagues)
            worker.cycle_begin.connect(lambda t: self._on_cycle_begin(t))
            worker.category_done.connect(
                lambda g, lbl, n: self._on_category_done(g, lbl, n))
            worker.note.connect(self._on_note)
            worker.cycle_error.connect(self._on_cycle_error)
            worker.state_changed.connect(lambda s, g=game: self._on_state_changed(g, s))
            worker.section_discovered.connect(self._on_section_discovered)
            worker.section_cleared.connect(self._on_section_cleared)
            self._pollers[game] = worker

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(render_tray_icon("ok"), self)
        self._tray.setToolTip("NinjaAlert — starting…")
        menu = QMenu()
        self._act_open = QAction("Open NinjaAlert", menu)
        self._act_open.triggered.connect(self.show_settings)
        self._act_pause = QAction("Pause monitoring", menu)
        self._act_pause.setCheckable(True)
        self._act_pause.toggled.connect(self._on_pause_toggled)
        self._act_check = QAction("Check now", menu)
        self._act_check.triggered.connect(self.check_now)
        act_site = QAction("Open poe.ninja", menu)
        act_site.triggered.connect(lambda: os_startfile("https://poe.ninja/poe1/economy"))
        self._act_quit = QAction("Quit", menu)
        self._act_quit.triggered.connect(self.quit)

        for a in (self._act_open, self._act_pause, self._act_check, act_site):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self._act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._worker is not None and not self._worker.isRunning():
            self._worker.start()
        self._worker_watchdog.start()
        for worker in self._pollers.values():
            worker.start()
        if not self._greeted:
            self._greeted = True
            enabled = [g for g in ("poe1", "poe2") if self._cfg.game_cfg(g).enabled]
            self._tray.showMessage(
                "NinjaAlert is running",
                f"Watching {', '.join(enabled) or 'no games'} with the "
                f"'{self._cfg.detector.engine}' engine. Use the tray icon for settings.",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    def show_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsWindow(self._cfg)
            self._settings.setWindowIcon(load_app_icon())
            self._settings.set_status_provider(self._status_snapshot)
            self._settings.set_history_source(self._history_snapshot,
                                              lambda: self._journal.revision())
            self._settings.set_mute_state(
                lambda: self._worker.muted_map() if self._worker is not None else {})
            self._settings.settings_saved.connect(self._on_settings_saved)
            self._settings.check_now_requested.connect(self.check_now)
            self._settings.pause_toggled.connect(self._on_pause_toggled)
            self._settings.refresh_leagues_requested.connect(self._refresh_leagues)
            self._settings.mute_requested.connect(self._on_item_muted)
            # the poller may have fetched leagues before the window existed
            for g, ids in self._leagues_cache.items():
                self._settings.set_leagues(g, ids)
        self._settings.set_state(self._aggregate_state(), self._last_detail)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    _settings: Optional[SettingsWindow] = None

    def _history_snapshot(self, limit: int = 200) -> list:
        """Alert history for the History tab: journal tail with confirmation
        outcomes paired onto their alerts, icons enriched, confirm markers
        dropped (they render as badges on the alert cards, not as cards)."""
        try:
            recs = self._journal.read_recent(limit * 2)   # confirms share the tail window
        except Exception:
            log.exception("history read failed")
            return []
        recs = attach_confirms(recs)[-limit:]
        for r in recs:
            key = r.get("key")
            if not r.get("icon") and key and key in self._icon_map:
                r["icon"] = self._icon_map[key]
        return recs

    def _status_snapshot(self) -> dict:
        """Thread-safe snapshot for the Status & Diagnostics tab."""
        stats = self._worker.stats() if self._worker is not None else {}
        snap = {
            "engine": self._cfg.detector.engine,
            "auto_calibrate": self._cfg.detector.auto_calibrate,
            "scenarios": len(self._cfg.scenarios),
            "poller_states": dict(self._poller_states),
            "worker_stats": stats,
            "enabled": {g: self._cfg.game_cfg(g).enabled for g in ("poe1", "poe2")},
            "last_detail": self._last_detail,
            "leagues": dict(self._current_league),
        }
        for game in ("poe1", "poe2"):
            worker = self._pollers.get(game)
            snap[f"{game}:paused"] = bool(worker is not None and worker.is_paused)
        return snap

    def check_now(self) -> None:
        for worker in self._pollers.values():
            worker.check_now()

    def quit(self) -> None:
        log.info("shutting down")
        try:
            for worker in self._pollers.values():
                worker.stop()
            if self._worker is not None:
                self._worker.stop()
                self._worker.wait(8000)    # drains the queue, closes the stores
            self._webhook.stop()           # joins the sender thread
        except Exception:
            log.exception("shutdown failed")
        if self._settings is not None:
            self._settings.hide()
        self._tray.hide()
        QApplication.quit()

    # ------------------------------------------------------------- callbacks
    def _on_pause_toggled(self, paused: bool) -> None:
        for worker in self._pollers.values():
            worker.pause(paused)
        self._act_pause.blockSignals(True)
        self._act_pause.setChecked(paused)
        self._act_pause.blockSignals(False)
        self._act_pause.setText("Resume monitoring" if paused else "Pause monitoring")
        self._tray.setIcon(render_tray_icon("paused" if paused else "ok"))

    def _refresh_leagues(self, game: str) -> None:
        self.check_now()
        try:
            client = self._pollers[game]._client
            leagues = client.get_leagues(game, force=True)
            if self._settings is not None:
                self._settings.set_leagues(game, [l.id for l in leagues])
        except Exception as exc:
            log.warning("manual league refresh failed: %s", exc)

    def _on_settings_saved(self, cfg: Config) -> None:
        self._cfg = cfg
        try:
            cfg.save(config_path())
        except Exception as exc:
            log.warning("could not persist config: %s", exc)
        if self._worker is not None:
            self._worker.apply_config(cfg)   # engine swap/reset handled on the worker
        for worker in self._pollers.values():
            worker.apply_config(cfg)
        log.info("settings applied (engine=%s)", cfg.detector.engine)

    # ---------------------------------------------------------- engine feed
    def _on_snapshot(self, game: str, quotes, meta) -> None:
        """Light GUI-thread hand-off: icon cache + league tracking only; the
        heavy analysis runs on AnalysisWorker (queued, FIFO with resets)."""
        try:
            for q in quotes:
                if q.icon:
                    self._icon_map[q.key] = q.icon

            # league switch (rollover or manual) -> reseed engines before feeding
            if self._meta_league.get(game) and self._meta_league[game] != meta.league:
                log.info("league changed for %s: %s -> %s; resetting engines",
                         game, self._meta_league[game], meta.league)
                self._worker.reset_game(game)
                self._journal.rotate()             # journal is league-scoped too
            self._meta_league[game] = meta.league

            self._resolve_confirms(meta, quotes)
            self._worker.submit_snapshot(game, quotes, meta)
        except Exception:
            log.exception("snapshot hand-off failed (%s)", game)

    def _on_analyzed(self, result: dict) -> None:
        """Worker -> GUI dispatch: toasts, journal, webhook, scenario hits."""
        try:
            meta = result["meta"]
            shown = result["shown"]
            for a in shown:
                self._dispatch_alert(a, meta)
            dispatched = {a.key for a in shown}
            for hit in result.get("scenario_hits") or []:
                if hit.anomaly.key in dispatched:
                    continue    # already alerted via the governor budget
                annotated = dc_replace(hit.anomaly,
                                       label=f"SCENARIO·{hit.scenario}")
                self._dispatch_alert(annotated, meta)
            if shown and float(self._cfg.alerts.confirm_rescan_seconds) > 0:
                # confirmation re-scan: wake this game's poller shortly to
                # re-check the moved items only - the cycle fetches just the
                # alerted categories, not the whole game (plan item C9)
                worker = self._pollers.get(result["game"])
                if worker is not None:
                    cats = {a.key.split(":")[2] for a in shown
                            if len(a.key.split(":")) > 2}
                    worker.wake_soon(float(self._cfg.alerts.confirm_rescan_seconds),
                                     categories=cats)
            digest = result.get("digest")
            if digest is not None:
                # folded moves land in the journal too - the History tab must
                # not lose them just because the budget kept the popup quiet
                for it in digest.items:
                    self._journal.append(digest_record(
                        it, result["game"], meta.league,
                        icon=self._icon_map.get(it.key)))
                self._toasts.show_digest(digest, result["game"], meta.league)
        except Exception:
            log.exception("alert dispatch failed")

    def _resolve_confirms(self, meta, quotes) -> None:
        """A re-scanned category settles its pending confirmations: one
        confirmation record per alerted item goes to alerts.jsonl (C9)."""
        if not self._pending_confirms:
            return
        now = time.time()
        by_key = {q.key: float(q.price) for q in quotes if q.price > 0}
        scoped = [k for k, p in self._pending_confirms.items()
                  if p["game"] == meta.game and p["cat_id"] == meta.category]
        for key in scoped:
            ref = self._pending_confirms.pop(key)
            new_price = by_key.get(key)              # None: gone from the market
            rec = confirm_record(key, ref, new_price, now)
            self._journal.append(rec)
            log.info("confirm %s: %s (share %s)", key,
                     "held" if rec["held"] else
                     ("gone" if rec["share"] is None else "reverted"),
                     rec["share"])
        stale = [k for k, p in self._pending_confirms.items() if p["due"] < now]
        for k in stale:                              # category was never re-fetched
            del self._pending_confirms[k]
            log.info("confirm %s: expired unresolved", k)

    def _on_item_muted(self, key: str, minutes: float) -> None:
        log.info("item muted for %.0f min: %s", minutes, key)
        if self._worker is not None:
            self._worker.submit_mute(key, minutes)

    def _on_worker_note(self, text: str) -> None:
        log.info("engine: %s", text)
        self._last_detail = text

    # --------------------------------------------------- section discovery
    def _on_section_discovered(self, game: str, section: dict) -> None:
        try:
            cat_id = str(section.get("id") or "")
            if not cat_id:
                return
            if self._cfg.register_custom_section(
                    game, cat_id, str(section.get("source") or "item"),
                    str(section.get("label") or cat_id),
                    str(section.get("slug") or "")):
                try:
                    self._cfg.save(config_path())
                except Exception as exc:
                    log.warning("could not persist discovered section: %s", exc)
                if self._worker is not None:
                    self._worker.apply_config(self._cfg)
                for worker in self._pollers.values():
                    worker.apply_config(self._cfg)
                self._tray.showMessage(
                    "New poe.ninja section",
                    f"{game}: '{cat_id}' discovered and now monitored.",
                    QSystemTrayIcon.MessageIcon.Information, 4000)
                log.info("section discovered (%s): %s", game, section)
        except Exception:
            log.exception("section discovery handling failed")

    def _on_section_cleared(self, game: str, cat_id: str) -> None:
        try:
            gc = self._cfg.game_cfg(game)
            if cat_id not in gc.watch:
                return
            gc.watch = [c for c in gc.watch if c != cat_id]
            try:
                self._cfg.save(config_path())
            except Exception as exc:
                log.warning("could not persist section removal: %s", exc)
            if self._worker is not None:
                self._worker.apply_config(self._cfg)
            for worker in self._pollers.values():
                worker.apply_config(self._cfg)
            self._tray.showMessage(
                "Section removed",
                f"{game}: '{cat_id}' is no longer on poe.ninja — auto-cleared.",
                QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception:
            log.exception("section clear handling failed")

    def _dispatch_alert(self, a, meta) -> None:
        parts = a.key.split(":")
        cat_id = parts[2] if len(parts) > 2 else a.category
        ident = parts[3] if len(parts) > 3 else None
        overlay_alert = Alert(
            game=a.game, league=meta.league, category=cat_id, name=a.name,
            old_price=a.prev_price, new_price=a.price, pct=a.pct,
            list_count=None, vol_ratio=a.rvol, icon=self._icon_map.get(a.key),
            details_id=ident, source="exchange" if a.key.find(":x:") > 0 else "item",
            ts=a.ts, key=a.key, label=a.label, phase=a.phase, z_adj=a.z_adj,
        )
        self._toasts.show_alert(overlay_alert, meta)

        payload = {
            "game": a.game, "league": meta.league, "name": a.name,
            "category": a.category, "price": a.price, "prev_price": a.prev_price,
            "pct": round(a.pct, 2), "direction": a.direction,
            "z_adj": round(a.z_adj, 3), "rvol": a.rvol, "confirmed": a.confirmed,
            "supply_ratio": a.supply_ratio, "label": a.label, "manip": round(a.manip, 3),
            "priority": round(a.priority, 3), "phase": a.phase,
            "league_day": round(a.league_day, 1), "ts": a.ts,
            # History-tab fields: identity + navigation (additive for webhooks)
            "key": a.key, "icon": self._icon_map.get(a.key),
            "details_id": ident, "source": overlay_alert.source,
            "text": format_alert_text(a, meta.league),
        }
        self._journal.append(payload)
        self._webhook.notify(payload)
        if float(self._cfg.alerts.confirm_rescan_seconds) > 0 and len(parts) > 2:
            # remember the alert so the category re-scan can grade the move (C9)
            self._pending_confirms[a.key] = {
                "ref_ts": a.ts, "alert_price": a.price, "prev_price": a.prev_price,
                "game": a.game, "cat_id": cat_id, "league": meta.league,
                "due": time.time() + max(
                    float(self._cfg.alerts.confirm_rescan_seconds) * 4.0, 1800.0),
            }

    # ------------------------------------------------------------ status UI
    def _on_leagues(self, game: str, league_ids) -> None:
        if not league_ids:
            return
        current = str(league_ids[0])
        self._leagues_cache[game] = [str(l) for l in league_ids]
        previous = self._current_league.get(game)
        if previous and previous != current:
            # runtime rollover: record first-seen today and reseed
            self._cfg.leagues_seen.setdefault(game, {})[current] = date.today().isoformat()
            self._runtime_new_leagues.add(current)
            try:
                self._cfg.save(config_path())
            except Exception:
                log.warning("could not persist leagues_seen", exc_info=True)
            self._worker.reset_game(game)
            self._journal.rotate()                 # journal is league-scoped too
            self._tray.showMessage(
                "New league detected",
                f"{game}: {current} — baselines reseeded from scratch.",
                QSystemTrayIcon.MessageIcon.Information, 4000)
        elif not previous and self._cfg.league_is_fresh(game, current):
            # the app was OFFLINE across the rollover (patch-day restart):
            # stored ticks belong to the previous league - wipe and reseed
            # exactly like a live rollover, or stale baselines poison warm start
            self._cfg.leagues_seen.setdefault(game, {})[current] = date.today().isoformat()
            self._runtime_new_leagues.add(current)
            try:
                self._cfg.save(config_path())
            except Exception:
                log.warning("could not persist leagues_seen", exc_info=True)
            if self._worker is not None:
                self._worker.reset_game(game)
                self._journal.rotate()             # journal is league-scoped too
            self._tray.showMessage(
                "New league detected",
                f"{game}: {current} — history wiped, baselines reseeded.",
                QSystemTrayIcon.MessageIcon.Information, 4000)
        elif not previous:
            self._cfg.leagues_seen.setdefault(game, {}).setdefault(current,
                                                                   date.today().isoformat())
        self._current_league[game] = current
        if self._settings is not None:
            self._settings.set_leagues(game, league_ids)

    def _on_cycle_begin(self, text: str) -> None:
        self._tray.setToolTip(f"NinjaAlert — {text}")

    def _on_category_done(self, game: str, label: str, count: int) -> None:
        done = self._scan_done.get(game, 0) + 1
        self._scan_done[game] = done
        total = len(self._cfg.watched(game))
        detail = f"{label}: unchanged" if count < 0 else f"{label}: {count} items"
        self._last_detail = f"[{done}/{total}] {detail}"   # visible scan progress
        if self._worker is not None:
            stats = self._worker.stats().get(game, (0, 0))
            self._tray.setToolTip(
                f"NinjaAlert — scanning {game}: {done}/{total} sections · "
                f"{stats[0]} robust + {stats[1]} basic")

    def _on_note(self, text: str) -> None:
        log.info("note: %s", text)
        if text.startswith("New league"):
            self._tray.showMessage("NinjaAlert", text,
                                   QSystemTrayIcon.MessageIcon.Information, 3000)
        self._last_detail = text

    def _on_cycle_error(self, text: str) -> None:
        self._last_detail = text
        self._tray.setToolTip(f"NinjaAlert — error: {text}")

    def _on_state_changed(self, game: str, state: str) -> None:
        self._poller_states[game] = state
        if state == "fetching":
            self._scan_done[game] = 0                  # new cycle: progress resets
        aggregate = self._aggregate_state()
        self._tray.setIcon(render_tray_icon(
            {"error": "error", "paused": "paused"}.get(aggregate, "ok")))
        if aggregate in ("waiting", "fetching"):
            stats = self._worker.stats() if self._worker is not None else {}
            robust = sum(s[0] for s in stats.values())
            basic = sum(s[1] for s in stats.values())
            self._tray.setToolTip(f"NinjaAlert — monitoring "
                                  f"({robust} robust + {basic} basic items)")
        if self._settings is not None:
            if self._cfg.game_cfg(game).enabled:
                # a disabled game must not write its state into the header —
                # the aggregate describes the games that actually run
                self._settings.set_state(aggregate, f"{game}: {self._last_detail}")
            else:
                self._settings.set_state(aggregate)

    def _aggregate_state(self) -> str:
        states = list(self._poller_states.values()) or ["starting"]
        for bad in ("error", "paused", "starting", "fetching"):
            if bad in states:
                return bad
        for ok in ("waiting", "disabled"):
            if ok in states:
                return ok
        return states[0]

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_settings()


# ---------------------------------------------------------------------- main
def main(argv) -> int:
    debug = "--debug" in argv
    setup_logging(debug=debug)
    log.info("NinjaAlert starting (app dir: %s)", app_dir())

    app = QApplication(argv)
    app.setApplicationName("NinjaAlert")
    app.setWindowIcon(load_app_icon())
    app.setQuitOnLastWindowClosed(False)  # tray app: window close != quit

    sys.excepthook = lambda t, v, tb: log.critical("unhandled exception", exc_info=(t, v, tb))

    controller = NinjaAlertController()

    guard = SingleInstanceGuard(controller.show_settings)
    if not guard.acquire():
        log.info("another NinjaAlert instance is already running - activating it")
        return 0

    controller.start()

    if debug:
        controller.show_settings()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
