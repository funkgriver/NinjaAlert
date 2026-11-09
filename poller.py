"""NinjaAlert - background polling worker (QThread), one instance per game.

Runs one fetch cycle every poll_interval_minutes for its game:
  refresh leagues -> resolve league -> fetch each watched category ->
  emit snapshot batches -> sleep (interruptible).

Every SECTION_DISCOVERY_SECONDS the worker also parses poe.ninja's economy
index for sections missing from the built-in registry (discovery.py), probes
the candidates against the API and reports brand-new ones. Watched categories
that keep answering 404 are reported for auto-clear after AUTO_CLEAR_STRIKES.

Signals are emitted from the worker thread; all Qt widgets must only be
touched on the receiving (GUI) side, which the main module wires up.
"""
from __future__ import annotations

import logging
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

from api_client import (
    UNCHANGED,
    ApiError,
    CategoryUnavailable,
    NinjaApiClient,
    RateLimited,
)
from categories import CATEGORIES
from discovery import parse_site_slugs, slug_to_type, unknown_slugs

log = logging.getLogger("ninjaalert.poller")

RATE_LIMIT_EXTRA_WAIT_SECONDS = 10 * 60   # extra cooldown after HTTP 429
LEAGUE_REFRESH_SECONDS = 6 * 3600
# poe.ninja's economy index gains/loses sections on patch day and mid-league
# balance patches - sweep often enough to catch those within hours
SECTION_DISCOVERY_SECONDS = 6 * 3600      # site-index section sweep cadence
AUTO_CLEAR_STRIKES = 6                    # consecutive 404 cycles -> report for removal
LEAGUE_AUTO_CLEAR_HOLD = 60 * 60          # grace after a league flip: new-league
                                          # sections 404 while poe.ninja builds them


class PollWorker(QThread):
    # fetched quote batch + fetch context (analyzer input)
    snapshot = pyqtSignal(object, object)          # list[ItemQuote], FetchMeta
    leagues_found = pyqtSignal(str, list)          # game, [league ids] (index 0 = current)
    cycle_begin = pyqtSignal(str)                  # "poe1 · Allflame"
    category_done = pyqtSignal(str, str, int)      # game, label, quote count
    note = pyqtSignal(str)                         # transient status line
    cycle_error = pyqtSignal(str)                  # human-readable failure
    state_changed = pyqtSignal(str)                # fetching|waiting|paused|disabled|error
    section_discovered = pyqtSignal(str, object)   # game, {id, source, label, slug}
    section_cleared = pyqtSignal(str, str)         # game, cat_id (gone from poe.ninja)

    def __init__(self, client: NinjaApiClient, config, game: str, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._cfg = config
        self.game = game
        self._lock = threading.Lock()
        self._stop_flag = False
        self._paused = False
        self._wake = threading.Event()   # interruptible sleep / check-now / reconfig
        self._last_leagues_refresh = 0.0
        self._unavailable_warned: set = set()
        self._unavail_strikes: dict = {}         # cat_id -> consecutive 404 cycles
        self._auto_cleared: set = set()          # reported for removal, don't re-emit
        self._last_discovery = 0.0
        self._wake_at: Optional[float] = None    # one early re-poll deadline
        self._wake_cats: set = set()             # categories the re-poll scopes to
        self._auto_clear_hold = 0.0              # patch-day grace deadline
        self._last_current_league: str = ""

    # --------------------------------------------------- control (GUI thread)
    def apply_config(self, config) -> None:
        with self._lock:
            self._cfg = config
        self._unavailable_warned.clear()
        self._auto_cleared.clear()
        self._unavail_strikes.clear()
        self._last_discovery = 0.0     # settings changed: sweep the site index soon
        self._wake.set()  # start a fresh cycle soon with the new settings

    def pause(self, on: bool) -> None:
        self._paused = bool(on)
        self.state_changed.emit("paused" if on else "starting")
        self._wake.set()

    def check_now(self) -> None:
        self._wake.set()

    def wake_soon(self, delay_s: float = 120.0, categories=None) -> None:
        """One early re-poll (e.g. right after an alert fired): the next sleep
        ends at `delay_s` instead of the full interval, to confirm the move.
        With `categories`, that confirmation cycle fetches only those category
        ids instead of the whole game (plan item C9)."""
        self._wake_at = time.time() + max(30.0, float(delay_s))
        if categories:
            self._wake_cats.update(categories)

    def stop(self) -> None:
        self._stop_flag = True
        self._wake.set()
        self.wait(8000)

    @property
    def current_config(self):
        with self._lock:
            return self._cfg

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------ worker loop
    def run(self) -> None:
        log.info("poller thread started (%s)", self.game)
        while not self._stop_flag:
            if self._paused:
                self.state_changed.emit("paused")
                self._sleep_until_wake(poll_seconds=3600.0)
                continue
            try:
                self._run_cycle()
            except Exception:  # never let the thread die
                log.exception("unexpected error in poll cycle (%s)", self.game)
                self.cycle_error.emit("unexpected error (see ninjaalert.log)")
                self.state_changed.emit("error")
                self._sleep_interval()
        log.info("poller thread stopped (%s)", self.game)

    def _run_cycle(self) -> None:
        cfg = self.current_config
        gc = cfg.game_cfg(self.game)
        self._wake_at = None                   # one-shot re-poll consumed
        wake_cats = self._wake_cats            # one-shot category scope consumed
        self._wake_cats = set()

        if not gc.enabled:
            self.state_changed.emit("disabled")
            self._sleep_until_wake(poll_seconds=600.0)
            return

        leagues = self._ensure_leagues(self.game)
        if leagues is None:
            self.cycle_error.emit(f"could not fetch league list for {self.game}")
            self.state_changed.emit("error")
            self._sleep_interval()
            return

        league_ids = [l.id for l in leagues]
        self.leagues_found.emit(self.game, league_ids)
        current = league_ids[0]
        if self._last_current_league != current:
            if self._last_current_league:
                # patch/league rollover: the section set may have changed -
                # sweep the site index again on the next cycle instead of
                # waiting out the discovery cadence; and new-league sections
                # 404 while poe.ninja builds them - hold the auto-clear for
                # an hour and reset old-league 404 strikes
                self._last_discovery = 0.0
                self._auto_clear_hold = time.time() + LEAGUE_AUTO_CLEAR_HOLD
                self._unavail_strikes.clear()
                self.note.emit(f"New league detected: {current}")
            self._last_current_league = current

        league = current if gc.league_auto or not gc.league else gc.league
        self.cycle_begin.emit(f"{self.game} · {league}")
        self.state_changed.emit("fetching")

        cats = [c for c in cfg.watched(self.game) if c in CATEGORIES[self.game]]
        if wake_cats:
            cats = [c for c in cats if c in wake_cats]
            if not cats:
                self.note.emit("confirmation re-scan: categories no longer watched")
                self.state_changed.emit("waiting")
                self._sleep_interval()
                return
            log.info("confirmation re-scan (%s): only %s", self.game, ", ".join(cats))
        if not cats:
            self.note.emit("No categories selected - nothing to watch")
            self.state_changed.emit("waiting")
            self._sleep_interval()
            return

        had_error = False
        for cat_id in cats:
            if self._stop_flag or self._paused:
                return
            label = CATEGORIES[self.game][cat_id].label
            self.note.emit(f"Fetching {label}…")
            try:
                result, meta = self._client.fetch_category(self.game, league, cat_id)
            except CategoryUnavailable as exc:
                strikes = self._unavail_strikes.get(cat_id, 0) + 1
                self._unavail_strikes[cat_id] = strikes
                if cat_id not in self._unavailable_warned:
                    self._unavailable_warned.add(cat_id)
                    log.warning("category unavailable: %s (strike %d)", exc, strikes)
                    self.note.emit(f"{label}: not available in this league")
                continue
            except RateLimited as exc:
                log.warning("rate limited: %s", exc)
                self.cycle_error.emit("poe.ninja rate limit hit - backing off")
                self.state_changed.emit("error")
                self._sleep_interval(extra_seconds=RATE_LIMIT_EXTRA_WAIT_SECONDS)
                return
            except ApiError as exc:
                log.error("fetch failed: %s", exc)
                self.cycle_error.emit(str(exc))
                self.state_changed.emit("error")
                had_error = True
                break  # skip the rest of the cycle; retry next interval

            self._unavail_strikes.pop(cat_id, None)   # alive again: reset the strike counter
            if result is UNCHANGED:
                self.category_done.emit(self.game, label, -1)  # -1 = not modified
                continue
            quotes: List = result  # type: ignore[assignment]
            self.snapshot.emit(quotes, meta)
            self.category_done.emit(self.game, label, len(quotes))

        if not had_error:
            if not wake_cats:              # sweeps belong to full cycles only
                self._maybe_autoclear()
                self._maybe_discover(cfg, league)
            self.state_changed.emit("waiting")
            self._sleep_interval()
        else:
            self._sleep_interval()

    def _maybe_autoclear(self) -> None:
        """Report watched categories that keep answering 404 (controller
        removes them from config; the poller skips them from then on)."""
        if time.time() < self._auto_clear_hold:
            return                     # patch-day grace: new sections 404 by nature
        for cat_id, strikes in list(self._unavail_strikes.items()):
            if strikes < AUTO_CLEAR_STRIKES or cat_id in self._auto_cleared:
                continue
            if cat_id not in self._cfg.game_cfg(self.game).watch:
                continue
            self._auto_cleared.add(cat_id)
            self._unavail_strikes.pop(cat_id, None)
            log.warning("section %s gone from poe.ninja after %d cycles - clearing",
                        cat_id, strikes)
            self.section_cleared.emit(self.game, cat_id)
            self.note.emit(f"Section no longer on poe.ninja: {cat_id}")

    def _maybe_discover(self, cfg, league: str) -> None:
        """Daily site-index sweep: probe slugs missing from the registry and
        report any that answer with data (controller persists + enables them)."""
        if self._stop_flag or self._paused:
            return
        if time.time() - self._last_discovery < SECTION_DISCOVERY_SECONDS:
            return
        self._last_discovery = time.time()
        try:
            html = self._client.get_economy_index(self.game)
            slugs = parse_site_slugs(html, self.game)
            candidates = unknown_slugs(self.game, slugs)
        except ApiError as exc:
            log.debug("section discovery skipped: %s", exc)
            return
        except Exception:
            log.exception("section discovery failed")
            return
        if not candidates:
            return
        registered = {s["id"] for s in cfg.game_cfg(self.game).custom_sections}
        log.info("discovery: %d new site section(s) to probe: %s",
                 len(candidates), candidates)
        for slug in candidates:
            type_guess = slug_to_type(slug)
            if type_guess in CATEGORIES[self.game] or type_guess in registered:
                continue
            for source in ("item", "exchange"):
                try:
                    result, _meta = self._client.fetch_category(
                        self.game, league, type_guess)
                except ApiError:
                    continue                     # 404/429/etc: not a live section
                label = slug.replace("-", " ").title()
                log.info("discovery: new poe.ninja section %s (%s)", type_guess, source)
                self.section_discovered.emit(self.game, {
                    "id": type_guess, "source": source,
                    "label": label, "slug": slug})
                registered.add(type_guess)
                break                            # registered: no need to probe the other source

    def _ensure_leagues(self, game: str):
        age_ok = (time.time() - self._last_leagues_refresh) < LEAGUE_REFRESH_SECONDS
        if age_ok:
            try:
                return self._client.get_leagues(game)  # cached inside client
            except ApiError as exc:
                log.warning("league refresh failed (using cache): %s", exc)
                cached = self._client._leagues.get(game)
                if cached:
                    return cached
                return None
        try:
            leagues = self._client.get_leagues(game, force=True)
            self._last_leagues_refresh = time.time()
            return leagues
        except ApiError as exc:
            log.warning("league fetch failed: %s", exc)
            return None

    # ------------------------------------------------------------ sleeping
    def _sleep_interval(self, extra_seconds: float = 0.0) -> None:
        interval = max(self.current_config.game_cfg(self.game).poll_interval_minutes, 1.0) * 60.0
        if self._wake_at is not None:          # confirmation re-scan: wake early
            interval = min(interval, max(30.0, self._wake_at - time.time()))
        self._sleep_until_wake(poll_seconds=interval + extra_seconds)

    def _sleep_until_wake(self, poll_seconds: float) -> None:
        """Sleep in short slices; wake early on stop/pause/check-now/reconfig."""
        deadline = time.monotonic() + max(poll_seconds, 1.0)
        while not self._stop_flag:
            if self._wake.is_set():
                self._wake.clear()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._paused:
                return
            time.sleep(min(1.0, remaining))
