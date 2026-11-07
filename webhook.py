"""webhook.py - outbound alert automation (spec WebhookConfig).

A daemon-thread sender: the GUI thread enqueues payloads (never blocks the
overlay), the worker rate-limits (max_per_minute), filters by min_priority
and POSTs the full anomaly JSON to any endpoint (n8n, Discord via proxy, ...).

Failures are logged and dropped; they never block the overlay.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import requests

log = logging.getLogger("ninjaalert.webhook")


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


class AlertJournal:
    """Append-only JSONL of every alert admitted by the governor (analysis input)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, record: Dict[str, Any]) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            log.debug("alert journal append failed", exc_info=True)

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
