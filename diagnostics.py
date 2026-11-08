"""diagnostics.py - automated checks for the Status & Diagnostics tab.

Each check returns a CheckResult(name, ok, detail); `run_checks` sweeps the
local system only (config, SQLite history, journal, assets, log tail) so it
is safe to run from the GUI thread. The optional live-API probe is opt-in
(`probe_api=True`) because it performs network I/O.

Standard library only, so it can be unit-tested without Qt or network.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

log = logging.getLogger("ninjaalert.diagnostics")


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def run_checks(cfg, base_dir: str, probe_api: bool = False,
               api_fn: Optional[Callable[[], Any]] = None) -> List[CheckResult]:
    """Sweep local system health. `api_fn` overrides the live probe (tests)."""
    results: List[CheckResult] = []
    d = cfg.detector

    rss = process_rss_mb()
    if rss is not None:
        results.append(CheckResult(
            "Process memory", rss < 1024.0,
            f"working set {rss:.0f} MB"
            + (" (unusually high — check ninjaalert.log 'memory:' lines)" if rss >= 1024.0 else "")))

    weights_ok = (len(d.weights) == 6
                  and all(isinstance(w, (int, float)) and 0.0 <= w <= 5.0 for w in d.weights))
    results.append(CheckResult(
        "Detector weights", weights_ok,
        f"weights={[round(float(w), 2) for w in d.weights]}, bias={d.bias}, "
        f"auto_calibrate={'on' if d.auto_calibrate else 'off'}"))

    for game in ("poe1", "poe2"):
        gc = cfg.game_cfg(game)
        if not gc.enabled:
            results.append(CheckResult(f"{game}: monitoring", True, "disabled"))
            continue
        results.append(CheckResult(
            f"{game}: watch list", bool(gc.watch),
            f"{len(gc.watch)} section(s) enabled, interval "
            f"{gc.poll_interval_minutes:.0f} min"))
        results.append(_db_check(base_dir, game))

    results.append(_journal_check(base_dir))
    results.append(_asset_check(base_dir))
    results.append(_alert_sound_check())
    results.append(_log_check(base_dir))
    results.append(_webhook_check(cfg))
    if cfg.scenarios:
        active = sum(1 for s in cfg.scenarios if s.enabled)
        results.append(CheckResult(
            "Scenarios", True, f"{active}/{len(cfg.scenarios)} enabled"))
    if probe_api:
        results.append(_api_check(api_fn))
    return results


def process_rss_mb() -> Optional[float]:
    """Resident memory of this process in MB (stdlib only)."""
    try:
        import sys
        if sys.platform == "win32":
            import ctypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p   # pseudo-handle (-1)
            handle = kernel32.GetCurrentProcess()
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(_PMC),
                                                   ctypes.c_ulong]
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return None
            return pmc.WorkingSetSize / (1024.0 * 1024.0)
        with open("/proc/self/status", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def _db_check(base_dir: str, game: str) -> CheckResult:
    path = Path(base_dir) / f"history-{game}.sqlite3"
    if not path.exists():
        return CheckResult(f"{game}: history DB", True, "no history yet (fresh install)")
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            ticks = int(con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0])
            newest = con.execute("SELECT MAX(ts) FROM ticks").fetchone()[0]
            idx = con.execute("SELECT name FROM sqlite_master WHERE type='index' "
                              "AND name='idx_ticks_ts'").fetchone()
        finally:
            con.close()
        age = (time.time() - float(newest)) / 60.0 if newest else None
        age_txt = f", newest {age:.0f} min ago" if age is not None else ""
        idx_txt = "idx ok" if idx else "idx MISSING"
        ok = mode == "wal" and bool(idx)
        return CheckResult(f"{game}: history DB", ok,
                           f"{ticks} ticks, journal={mode}, {idx_txt}{age_txt}")
    except sqlite3.Error as exc:
        return CheckResult(f"{game}: history DB", False, f"sqlite error: {exc}")


def _journal_check(base_dir: str) -> CheckResult:
    try:
        probe = Path(base_dir) / "alerts.jsonl"
        lines = 0
        if probe.exists():
            with open(probe, "r", encoding="utf-8", errors="replace") as f:
                lines = sum(1 for _ in f)
        if os.access(base_dir, os.W_OK):
            return CheckResult("Alert journal", True,
                               f"alerts.jsonl {lines} record(s), dir writable")
        return CheckResult("Alert journal", False, "app dir is not writable")
    except OSError as exc:
        return CheckResult("Alert journal", False, str(exc))


def _asset_check(base_dir: str) -> CheckResult:
    from appconfig import resource_path
    ico = Path(resource_path("assets/app.ico"))
    if ico.exists() and ico.stat().st_size > 0:
        return CheckResult("App icon asset", True, f"{ico.name} bundled")
    return CheckResult("App icon asset", False,
                       "assets/app.ico missing (run tools/make_icon.py)")


def _alert_sound_check() -> CheckResult:
    from appconfig import resource_path
    wav = Path(resource_path("assets/alert.wav"))
    if wav.exists() and wav.stat().st_size > 0:
        return CheckResult("Alert sound asset", True, f"{wav.name} bundled")
    return CheckResult("Alert sound asset", False,
                       "assets/alert.wav missing (rebuild with bundled assets)")


def _log_check(base_dir: str) -> CheckResult:
    log_path = Path(base_dir) / "ninjaalert.log"
    try:
        if not log_path.exists():
            return CheckResult("Log tail", True, "no log yet")
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-200:]
        crit = [ln for ln in tail if "CRITICAL" in ln or "ERROR" in ln]
        ok = not crit
        return CheckResult("Log tail", ok,
                           "no errors in last 200 lines" if ok
                           else f"{len(crit)} error line(s) recently - "
                                f"last: {crit[-1].strip()[:120]}")
    except OSError as exc:
        return CheckResult("Log tail", False, str(exc))


def _webhook_check(cfg) -> CheckResult:
    w = cfg.webhook
    if not w.enabled:
        return CheckResult("Webhook", True, "disabled")
    if not w.url.lower().startswith(("http://", "https://")):
        return CheckResult("Webhook", False, "URL is not an http(s) endpoint")
    return CheckResult("Webhook", True,
                       f"endpoint set (min priority {w.min_priority})")


def _api_check(api_fn: Optional[Callable[[], Any]]) -> CheckResult:
    try:
        if api_fn is None:
            from api_client import NinjaApiClient
            client = NinjaApiClient(timeout=6.0)
            leagues = client.get_leagues("poe1", force=True)
            api_fn_result = leagues
        else:
            api_fn_result = api_fn()
        n = len(api_fn_result) if isinstance(api_fn_result, list) else 0
        return CheckResult("Live API probe", n > 0, f"poe.ninja reachable, {n} league(s)")
    except Exception as exc:
        return CheckResult("Live API probe", False, f"unreachable: {exc}")


def format_result(r: CheckResult) -> str:
    return f"{'✓' if r.ok else '✗'} {r.name}" + (f" — {r.detail}" if r.detail else "")
