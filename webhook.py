"""webhook.py - outbound alert automation (spec WebhookConfig).

A daemon-thread sender: the GUI thread enqueues payloads (never blocks the
overlay), the worker rate-limits (max_per_minute), filters by min_priority
and POSTs the full anomaly JSON to any endpoint (n8n, Discord via proxy, ...).

Failures are logged and dropped; they never block the overlay.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import requests

log = logging.getLogger("ninjaalert.webhook")

JOURNAL_MAX_BYTES = 5 * 1024 * 1024   # alerts.jsonl rotates above this (keeps .old)


def format_alert_text(a, league: str) -> str:
    """Human-readable one-liner for webhook/discord-style sinks."""
    pct = f"{a.pct:+.1f}%" if a.prev_price else ""
    bits = [f"{a.name} ({a.category.split(':', 2)[-1]})"]
    if pct:
        bits.append(f"{a.prev_price:g} → {a.price:g} ({pct})")
    if a.rvol is not None:
        bits.append(f"RVOL ×{a.rvol:.1f}")
    bits.append(f"z={a.z_adj:.1f}" if a.z_adj else "")
    bits.append(f"{a.label} · {a.phase}")
    header = f"⚡ {a.game.upper()} [{league}]"
    return header + "\n" + " · ".join(b for b in bits if b)


class WebhookSender:
    def __init__(self, cfg_fn: Callable[[], Any]) -> None:
        self._cfg_fn = cfg_fn
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._recent: List[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.start()

    # ---------------------------------------------------------------- API
    def notify(self, payload: Dict[str, Any]) -> None:
        """Enqueue one alert (payload must carry 'priority'). Non-blocking."""
        cfg = self._cfg_fn()
        if not cfg.webhook.enabled or not cfg.webhook.url:
            return
        if float(payload.get("priority", 0.0)) < float(cfg.webhook.min_priority):
            return
        try:
            self._queue.put_nowait(payload)
        except Exception:
            log.debug("webhook queue full - dropping alert")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)   # flush pending sends before exit

    # ------------------------------------------------------------- worker
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="ninjaalert-webhook",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._send(item)
            except Exception:
                log.exception("webhook send failed")

    def _rate_ok(self) -> bool:
        cfg = self._cfg_fn()
        now = time.time()
        with self._lock:
            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= max(1, int(cfg.webhook.max_per_minute)):
                return False
            self._recent.append(now)
            return True

    def _send(self, payload: Dict[str, Any]) -> None:
        cfg = self._cfg_fn()
        if not self._rate_ok():
            log.debug("webhook rate limited - dropping %s", payload.get("name"))
            return
        headers = {"Content-Type": "application/json",
                   "User-Agent": cfg.user_agent}
        timeout = min(max(cfg.network.timeout_s, 2.0), 15.0)
        try:
            resp = requests.post(cfg.webhook.url, data=json.dumps(payload),
                                 headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("webhook POST failed: %s", exc)
            return
        if resp.status_code >= 300:
            log.warning("webhook POST %s -> HTTP %d", cfg.webhook.url, resp.status_code)
        else:
            log.info("webhook delivered: %s", payload.get("name"))


def confirm_record(key: str, ref: Dict[str, Any], new_price: Optional[float],
                   now: float) -> Dict[str, Any]:
    """Journal record for one confirmation re-scan outcome (plan item C9).

    ref carries the alert context: ref_ts, alert_price, prev_price, game,
    league, cat_id. new_price None means the item vanished from the re-scanned
    category. held = the move kept >= 50% of its size versus the alert's own
    baseline - same semantics as tools/backtest.py.
    """
    move = ref["alert_price"] - ref["prev_price"]
    share = None if (new_price is None or abs(move) < 1e-12) \
        else (new_price - ref["prev_price"]) / move
    return {
        "type": "confirm", "key": key, "ref_ts": ref["ref_ts"], "ts": now,
        "game": ref["game"], "league": ref["league"], "category": ref["cat_id"],
        "held": bool(share is not None and share >= 0.5),
        "share": None if share is None else round(share, 3),
        "price": new_price, "alert_price": ref["alert_price"],
        "prev_price": ref["prev_price"],
    }


class AlertJournal:
    """Append-only JSONL of every alert admitted by the governor (analysis input).

    Bounded by design: the file rotates into alerts.jsonl.old (one generation)
    once it exceeds JOURNAL_MAX_BYTES, and on every league change - leagues
    live at most a few months, so the journal never grows past that."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._appended = 0          # revision source: change detection for the History tab
        self._rotations = 0

    def append(self, record: Dict[str, Any]) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._appended += 1
        except Exception:
            log.debug("alert journal append failed", exc_info=True)

    def revision(self) -> int:
        """Monotonic change counter (appends + rotations): lets the History
        tab detect journal growth even when its window is full and every
        visible record is unchanged (e.g. only confirm markers arrived)."""
        return self._appended + self._rotations

    def rotate_if_large(self, max_bytes: int = JOURNAL_MAX_BYTES) -> bool:
        """Size cap: fold the journal into alerts.jsonl.old once it exceeds
        max_bytes (no-op below the cap)."""
        try:
            if os.path.getsize(self._path) <= max_bytes:
                return False
        except OSError:
            return False
        return self.rotate()

    def rotate(self) -> bool:
        """Start a fresh journal; the current one becomes alerts.jsonl.old
        (replacing the previous generation). Called on league changes too."""
        try:
            with self._lock:
                if not os.path.exists(self._path):
                    return False
                os.replace(self._path, self._path + ".old")
                self._rotations += 1
            log.info("journal rotated -> %s.old", os.path.basename(self._path))
            return True
        except OSError:
            log.exception("journal rotate failed")
            return False

    def read_recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Last `limit` records (oldest -> newest) for the History tab.
        Tolerant of truncated/foreign lines; the app may be appending while
        this runs — reading a concurrently appended file is safe."""
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                lines: "deque[str]" = deque(f, maxlen=max(1, int(limit)))
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in lines:
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
            except json.JSONDecodeError:
                continue                   # torn tail line, skip
        return out


def attach_confirms(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair confirmation records with their alerts (C9 -> History tab).

    Order-agnostic: the newest confirm per key attaches to the newest alert
    record it references (rec["confirm"] = {held, share, price, ts}); confirm
    markers themselves are dropped - they render as badges on the alert
    cards, not as cards of their own.
    """
    confirms: Dict[str, Dict[str, Any]] = {}
    for rec in recs:
        if rec.get("type") != "confirm":
            continue
        key = str(rec.get("key") or "")
        cur = confirms.get(key)
        if cur is None or float(rec.get("ts") or 0.0) > float(cur.get("ts") or 0.0):
            confirms[key] = rec
    out: List[Dict[str, Any]] = []
    for rec in recs:
        if rec.get("type") == "confirm":
            continue
        conf = confirms.get(str(rec.get("key") or ""))
        if (conf is not None
                and float(conf.get("ref_ts") or 0.0) >= float(rec.get("ts") or 0.0)):
            rec = dict(rec)
            rec["confirm"] = {"held": bool(conf.get("held")),
                              "share": conf.get("share"),
                              "price": conf.get("price"),
                              "ts": conf.get("ts")}
        out.append(rec)
    return out
