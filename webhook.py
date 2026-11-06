"""webhook.py - outbound alert automation (spec WebhookConfig).

A daemon-thread sender: the GUI thread enqueues payloads (never blocks the
overlay), the worker rate-limits (max_per_minute), filters by min_priority
and POSTs JSON. Two formats:
  generic  - full anomaly JSON to any endpoint (Discord via proxy, n8n, custom)
  telegram - {"chat_id", "text"} body to the Telegram Bot API sendMessage URL

Failures are logged and dropped; they never block the overlay.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

log = logging.getLogger("ninjaalert.webhook")


def format_alert_text(a, league: str) -> str:
    """Human-readable one-liner for Telegram/discord-style sinks."""
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
        if cfg.webhook.format == "telegram":
            body: Dict[str, Any] = {
                "chat_id": cfg.webhook.chat_id,
                "text": payload.get("text") or str(payload),
                "disable_web_page_preview": True,
            }
        else:
            body = payload
        resp = requests.post(cfg.webhook.url, data=json.dumps(body),
                             headers=headers, timeout=timeout)
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
