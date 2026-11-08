"""analysis_worker.py - engine-owner QThread (audit Phase 2 refactor).

Owns both games' AnalysisPipeline + AnomalyDetector so all heavy per-snapshot
work (tick conversion, robust statistics, SQLite writes, warm-start replay)
runs off the GUI thread. The controller hands over raw (game, quotes, meta)
batches and receives compact result dicts back for alert dispatch.

Command queue (FIFO, all engine mutation happens on this thread):
  ("snapshot", game, quotes, meta)   analyze one fetched category
  ("config", cfg)                    apply saved settings
  ("reset", game)                    league rollover / manual league switch
  ("stop",)                          drain and shut down (closes the stores)

Degradation: if a pipeline cannot be constructed (e.g. locked SQLite file),
the worker keeps running in basic-engine-only mode for that game and retries
pipeline construction every PIPELINE_RETRY_SECONDS.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from advanced_analyzer import (
    AnalysisPipeline,
    anomaly_from_basic,
    ticks_from_quotes,
)
from analyzer import AnomalyDetector
from appconfig import app_dir
from calibration import calibrate_from_ticks
from diagnostics import process_rss_mb
from models import Snapshot
from scenarios import ScenarioEngine

log = logging.getLogger("ninjaalert.worker")

GAMES = ("poe1", "poe2")
PIPELINE_RETRY_SECONDS = 600.0
QUEUE_MAX = 64                     # bound the command queue (dead-worker protection)
CALIBRATION_MAX_ROWS = 200_000     # bound the refit replay regardless of DB size
MEMORY_LOG_SECONDS = 1800.0        # periodic RSS note (leak observability)


class AnalysisWorker(QThread):
    analyzed = pyqtSignal(object)   # dict: game, meta, shown(list[Anomaly]), digest
    note = pyqtSignal(str)          # engine-level status lines

    def __init__(self, cfg, league_start_fn, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._league_start_fn = league_start_fn
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_MAX)
        self._stats_lock = threading.Lock()
        self._stats: Dict[str, Tuple[int, int]] = {}   # game -> (robust, basic)
        self._last_calibrate = time.time()
        self._last_pipeline_try = 0.0
        self._last_memory_log = 0.0
        self._scenarios = ScenarioEngine()

    # --------------------------------------------------- control (GUI thread)
    def submit_snapshot(self, game: str, quotes, meta) -> None:
        # bounded queue: a dead/stuck worker must not turn into a memory leak;
        # the next poll cycle delivers fresh data anyway
        if self._q.qsize() >= QUEUE_MAX:
            try:
                dropped = self._q.get_nowait()
                log.warning("analysis queue full - dropped oldest command (%s)",
                            dropped[0])
            except queue.Empty:
                pass
        self._q.put(("snapshot", game, quotes, meta))

    def apply_config(self, cfg) -> None:
        self._q.put(("config", cfg))

    def reset_game(self, game: str) -> None:
        self._q.put(("reset", game))

    def stop(self) -> None:
        self._q.put(("stop",))

    def stats(self) -> Dict[str, Tuple[int, int]]:
        with self._stats_lock:
            return dict(self._stats)

    # ------------------------------------------------------------ worker loop
    def run(self) -> None:
        try:
            pipelines, basics = self._build_engines()
        except Exception:
            # a dead worker means silently dead monitoring AND an unbounded
            # GUI-side queue - never let this loop die
            log.exception("engine construction failed completely - "
                          "worker runs in degraded no-engine mode")
            pipelines, basics = {g: None for g in GAMES}, {g: None for g in GAMES}
        log.info("analysis worker started (warm start complete)")
        running = True
        while running:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                self._maybe_autocalibrate(pipelines)
                self._maybe_log_memory()
                continue
            cmd = item[0]
            try:
                if cmd == "snapshot":
                    self._handle_snapshot(pipelines, basics, item[1], item[2], item[3])
                elif cmd == "config":
                    self._handle_config(pipelines, basics, item[1])
                elif cmd == "reset":
                    self._handle_reset(pipelines, basics, item[1])
                elif cmd == "stop":
                    running = False
            except Exception:          # never let the thread die
                log.exception("analysis worker command failed (%s)", cmd)
            try:
                self._update_stats(pipelines, basics)
            except Exception:
                log.exception("stats update failed")
        for game in GAMES:
            if pipelines.get(game) is not None:
                try:
                    pipelines[game].close()
                except Exception:
                    log.exception("closing pipeline failed (%s)", game)
        log.info("analysis worker stopped")

    def _maybe_log_memory(self) -> None:
        if time.time() - self._last_memory_log < MEMORY_LOG_SECONDS:
            return
        self._last_memory_log = time.time()
        rss = process_rss_mb()
        if rss is not None:
            log.info("memory: working set %.0f MB, items tracked %s",
                     rss, {g: s for g, s in (self.stats() or {}).items()})

    def _build_engines(self):
        pipelines: Dict[str, Optional[AnalysisPipeline]] = {}
        basics: Dict[str, AnomalyDetector] = {}
        for game in GAMES:
            db_path = Path(app_dir()) / f"history-{game}.sqlite3"
            try:
                pipelines[game] = AnalysisPipeline(
                    self._cfg.detector, self._cfg.alerts, self._league_start_fn, db_path)
            except Exception:
                log.exception("pipeline construction failed (%s) - basic-only mode", game)
                self.note.emit(f"{game}: advanced engine unavailable (see log)")
                pipelines[game] = None
                self._last_pipeline_try = time.time()
            basics[game] = AnomalyDetector(self._cfg.thresholds)
        return pipelines, basics

    # ----------------------------------------------------------- commands
    def _handle_snapshot(self, pipelines, basics, game: str, quotes, meta) -> None:
        cfg = self._cfg
        now = time.time()
        pipeline = self._ensure_pipeline(pipelines, game)

        ticks = ticks_from_quotes(quotes, now)
        snap = Snapshot(game=game,
                        category=f"{game}:{meta.source}:{meta.category}",
                        league=meta.league, ts=now, source="documented",
                        ticks=tuple(ticks))

        engine = cfg.detector.engine
        basic_alerts = basics[game].feed(quotes, meta, now)
        full_anomalies: List = []
        if engine == "basic" or pipeline is None:
            anomalies = [anomaly_from_basic(a, meta.league) for a in basic_alerts]
            full_anomalies.extend(anomalies)
            if pipeline is not None:
                pipeline.ingest(ticks)          # storage + prune, no robust analysis
                shown, digest = pipeline.governor.decide(anomalies)
            else:                               # degraded mode: basic cooldowns still apply
                shown, digest = anomalies, None
        else:  # auto: robust engine + basic warmup fallback
            extra = [anomaly_from_basic(a, meta.league)
                     for a in basic_alerts if a.key not in pipeline.analyzer.ready_keys]
            shown, digest = pipeline.handle(snap, extra_anomalies=extra,
                                            collect_anomalies=full_anomalies)

        scenario_hits = self._scenarios.evaluate(cfg.scenarios, full_anomalies, now)
        self.analyzed.emit({"game": game, "meta": meta, "shown": shown,
                            "digest": digest, "scenario_hits": scenario_hits})

    def _handle_config(self, pipelines, basics, cfg) -> None:
        prev = self._cfg
        prev_engine = prev.detector.engine
        self._cfg = cfg
        for game in GAMES:
            basics[game].update_thresholds(cfg.thresholds)
            pipeline = pipelines.get(game)
            if pipeline is None:
                continue
            pipeline.cfg = cfg.detector            # prune/retention window
            pipeline.governor.cfg = cfg.alerts
            pipeline.analyzer.cfg = cfg.detector   # property: rebinds phase.c too
            if cfg.detector.engine != prev_engine and cfg.detector.engine == "auto":
                # switching back to auto: robust baselines may need reseeding
                pipeline.analyzer.reset()
        changed = [f.name for f in fields(cfg.detector)
                   if getattr(prev.detector, f.name, None) != getattr(cfg.detector, f.name)]
        log.info("config applied (detector: %s)", ", ".join(changed) or "no changes")

    def _handle_reset(self, pipelines, basics, game: str) -> None:
        pipeline = pipelines.get(game)
        if pipeline is not None:
            pipeline.analyzer.reset()
            pipeline.governor.reset()
            wiped = pipeline.store.clear()         # history is league-scoped
            log.info("history wiped for league change (%s): %d ticks", game, wiped)
        basics[game].reset()
        self._scenarios.reset()
        log.info("engines reset (%s)", game)

    def _ensure_pipeline(self, pipelines, game: str) -> Optional[AnalysisPipeline]:
        if pipelines.get(game) is not None:
            return pipelines[game]
        if time.time() - self._last_pipeline_try < PIPELINE_RETRY_SECONDS:
            return None
        self._last_pipeline_try = time.time()
        try:
            db_path = Path(app_dir()) / f"history-{game}.sqlite3"
            pipelines[game] = AnalysisPipeline(
                self._cfg.detector, self._cfg.alerts, self._league_start_fn, db_path)
            log.info("pipeline recovered (%s)", game)
            self.note.emit(f"{game}: advanced engine recovered")
            return pipelines[game]
        except Exception:
            log.exception("pipeline retry failed (%s)", game)
            return None

    # -------------------------------------------------------------- stats
    def _update_stats(self, pipelines, basics) -> None:
        stats = {}
        for game in GAMES:
            pipeline = pipelines.get(game)
            robust = pipeline.analyzer.tracked_count if pipeline is not None else 0
            stats[game] = (robust, basics[game].tracked_count)
        with self._stats_lock:
            self._stats = stats

    # ------------------------------------------------- in-app auto-refit
    def _maybe_autocalibrate(self, pipelines) -> None:
        c = self._cfg.detector
        if not c.auto_calibrate:
            return
        if time.time() - self._last_calibrate < c.calibrate_interval_hours * 3600.0:
            return
        self._last_calibrate = time.time()
        for game in GAMES:
            pipeline = pipelines.get(game)
            if pipeline is None:
                continue
            try:
                ticks = pipeline.store.load_since(
                    time.time() - c.history_retention_days * 86400.0,
                    max_rows=CALIBRATION_MAX_ROWS)
                analyzer = pipeline.analyzer
                current_w = analyzer._weights or c.weights
                current_b = analyzer._bias if analyzer._bias is not None else c.bias
                result = calibrate_from_ticks(c, self._league_start_fn, ticks,
                                              current_w, current_b)
                if result is not None and result.improved:
                    analyzer.set_weights(result.weights, result.bias)
                    msg = (f"{game}: weights recalibrated "
                           f"(log-loss {result.loss_before:.4f} -> {result.loss_after:.4f})")
                    log.info(msg)
                    self.note.emit(msg)
                elif result is not None:
                    log.info("auto-calibration kept current weights (%s): %s",
                             game, result.reason)
            except Exception:
                log.exception("auto-calibration failed (%s)", game)
