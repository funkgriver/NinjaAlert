"""NinjaAlert - poe.ninja API client (PoE 1 + PoE 2).

Implements the 2026 poe.ninja economy API (https://poe.ninja/docs/api):

  leagues   : GET /{game}/api/economy/leagues                 (first = current league)
  exchange  : GET /{game}/api/economy/exchange/current/overview?league=&type=
  items     : GET /{game}/api/economy/stash/current/item/overview?league=&type=

Site guidelines honoured here:
  * descriptive User-Agent with app identity
  * ETag/If-None-Match conditional requests (site HTTP-caches ~5 min)
  * a minimum gap between two consecutive requests
  * conservative retry/backoff; errors never crash the caller

Price semantics (verified against live data 2026-11):
  * exchange lines: `primaryValue` is the unit price in `core.primary`
    (poe1 -> chaos, poe2 -> divine); `core.rates` converts to secondary units.
  * poe1 item lines: `chaosValue` (chaos); poe2 item lines: `primaryValue` (divine).
  * item lines carry `listingCount` (volume signal); exchange lines do not.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from random import uniform
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union
from urllib.parse import urlencode

import requests

from categories import CatDef, CATEGORIES

log = logging.getLogger("ninjaalert.api")

BASE_URL = "https://poe.ninja"
LEAGUES_TTL_SECONDS = 6 * 3600        # league list refresh cadence
MIN_REQUEST_GAP_SECONDS = 1.2         # politeness between consecutive requests
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 30.0            # cap for the jittered exponential backoff
RETRY_AFTER_MAX_SECONDS = 120.0       # never sleep longer than this on Retry-After
RETRY_STATUS_CODES = {500, 502, 503, 504}

# Returned by fetch_category when the ETag says "not modified since last poll".
UNCHANGED = object()


class ApiError(Exception):
    """Generic poe.ninja fetch failure (network/HTTP)."""


class CategoryUnavailable(ApiError):
    """404 - this category/type does not exist for the given game/league."""


class RateLimited(ApiError):
    """429 - poe.ninja asked us to slow down."""


class League(NamedTuple):
    id: str
    name: str


@dataclass
class FetchMeta:
    """Context of a successful fetch cycle - needed to interpret prices."""
    game: str
    league: str
    category: str
    source: str                 # "exchange" | "item"
    primary: str                # currency id prices are expressed in
    secondary: str = ""
    rates: Dict[str, float] = field(default_factory=dict)  # unit -> per-primary rate


@dataclass
class ItemQuote:
    """One normalized price observation."""
    key: str                    # stable across polls, unique per variant
    name: str                   # display name (variant appended when present)
    game: str                   # "poe1" | "poe2"
    category: str               # category id, e.g. "Currency"
    source: str                 # "exchange" | "item"
    price: float                # in the game's primary currency
    list_count: Optional[int]   # None for exchange categories
    icon: Optional[str]
    details_id: Optional[str]
    spark_total_change: Optional[float] = None  # 7d trend % reported by the site
    volume: Optional[float] = None   # exchange: volumePrimaryValue; items: sample count
    aux: Optional[float] = None      # stress ratio maxVolumeRate / primaryValue


def format_number(v: float) -> str:
    """Compact number formatting for UI: 1234 -> '1.2k', 0.0053 -> '0.0053'."""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{v / 1_000:.1f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def format_price(price: float, meta: FetchMeta) -> str:
    """'18.4c  ≈ 1.2 div' style rendering using the fetch context."""
    from categories import PRIMARY_UNIT_LABEL

    unit = PRIMARY_UNIT_LABEL.get(meta.primary, meta.primary)
    text = f"{format_number(price)}{unit}"
    if meta.game == "poe1" and price >= 20 and meta.rates.get("divine"):
        text += f"  ≈ {format_number(price * meta.rates['divine'])} div"
    elif meta.game == "poe2" and price < 0.5 and meta.rates.get("exalted"):
        text += f"  ≈ {format_number(price * meta.rates['exalted'])} ex"
    return text


class NinjaApiClient:
    def __init__(self, timeout: float = REQUEST_TIMEOUT_SECONDS,
                 user_agent_fn: Optional[Callable[[], str]] = None,
                 base_url_fn: Optional[Callable[[], str]] = None,
                 timeout_fn: Optional[Callable[[], float]] = None):
        self.timeout = timeout
        # per-request override (e.g. live config value); falls back to `timeout`
        self._timeout_fn = timeout_fn or (lambda: timeout)
        self._user_agent_fn = user_agent_fn or (lambda: "NinjaAlert/2.0 (contact: unset)")
        self._base_url_fn = base_url_fn or (lambda: BASE_URL)
        self._sess = requests.Session()
        self._etags: Dict[str, str] = {}
        self._last_request_ts = 0.0
        self._leagues: Dict[str, List[League]] = {}
        self._leagues_ts: Dict[str, float] = {}

    # ------------------------------------------------------------ leagues
    def get_leagues(self, game: str, force: bool = False) -> List[League]:
        """League ids for a game; first entry is the current challenge league."""
        if game not in ("poe1", "poe2"):
            raise ValueError(f"unknown game: {game!r}")
        cached = self._leagues.get(game)
        fresh = (time.time() - self._leagues_ts.get(game, 0.0)) < LEAGUES_TTL_SECONDS
        if cached and fresh and not force:
            return cached
        resp = self._request(f"{self._base_url_fn().rstrip('/')}/{game}/api/economy/leagues")
        data = self._json(resp, f"{game}/leagues")
        if not isinstance(data, list):
            raise ApiError(f"unexpected league payload for {game}")
        leagues = []
        for entry in data:
            if isinstance(entry, dict) and entry.get("id"):
                leagues.append(League(id=str(entry["id"]), name=str(entry.get("name") or entry["id"])))
        if not leagues:
            raise ApiError(f"league list for {game} is empty")
        self._leagues[game] = leagues
        self._leagues_ts[game] = time.time()
        log.info("leagues(%s): %s", game, [l.id for l in leagues])
        return leagues

    def current_league(self, game: str) -> str:
        return self.get_leagues(game)[0].id

    # ------------------------------------------------------------- quotes
    def fetch_category(
        self, game: str, league: str, cat_id: str
    ) -> Tuple[Union[List[ItemQuote], object], Optional[FetchMeta]]:
        """Fetch one category. Returns (quotes, meta) or (UNCHANGED, None)
        when the site reports 304 not-modified."""
        cat = self._cat(game, cat_id)
        if cat.source == "exchange":
            path = f"/{game}/api/economy/exchange/current/overview"
        else:
            path = f"/{game}/api/economy/stash/current/item/overview"
        resp = self._request(self._base_url_fn().rstrip("/") + path,
                             params={"league": league, "type": cat.id})
        if resp is UNCHANGED:
            return UNCHANGED, None
        payload = self._json(resp, f"{game}/{cat.id}")
        if cat.source == "exchange":
            meta, quotes = self._parse_exchange(game, league, cat, payload)
        else:
            meta, quotes = self._parse_item(game, league, cat, payload)
        log.info("fetched %s/%s/%s: %d quotes", game, league, cat.id, len(quotes))
        return quotes, meta

    # ------------------------------------------------------------- parsing
    @staticmethod
    def _cat(game: str, cat_id: str) -> CatDef:
        try:
            return CATEGORIES[game][cat_id]
        except KeyError as exc:
            raise ApiError(f"unknown category {cat_id!r} for {game}") from exc

    def _parse_exchange(
        self, game: str, league: str, cat: CatDef, payload: Dict[str, Any]
    ) -> Tuple[FetchMeta, List[ItemQuote]]:
        core = payload.get("core") or {}
        # id -> {name, image, detailsId}; site sends it both at top level and in core
        items_meta: Dict[str, Dict[str, Any]] = {}
        for it in (payload.get("items") or []) + (core.get("items") or []):
            if isinstance(it, dict) and it.get("id"):
                items_meta.setdefault(str(it["id"]), it)

        meta = FetchMeta(
            game=game,
            league=league,
            category=cat.id,
            source="exchange",
            primary=str(core.get("primary") or ("chaos" if game == "poe1" else "divine")),
            secondary=str(core.get("secondary") or ("divine" if game == "poe1" else "chaos")),
            rates={k: float(v) for k, v in (core.get("rates") or {}).items() if isinstance(v, (int, float))},
        )

        quotes: List[ItemQuote] = []
        for ln in payload.get("lines") or []:
            try:
                ident = str(ln.get("id"))
                price = float(ln.get("primaryValue") or 0.0)
                if not ident or price <= 0:
                    continue
                m = items_meta.get(ident) or {}
                icon = self._abs_icon(m.get("image"))
                spark = ln.get("sparkline") or {}
                rate = _f(ln.get("maxVolumeRate"))
                quotes.append(
                    ItemQuote(
                        key=f"{game}:x:{cat.id}:{ident}",
                        name=str(m.get("name") or ident),
                        game=game,
                        category=cat.id,
                        source="exchange",
                        price=price,
                        list_count=None,          # exchange data has no listing counts
                        icon=icon,
                        details_id=str(m.get("detailsId") or ident),
                        spark_total_change=_f(spark.get("totalChange")),
                        volume=_f(ln.get("volumePrimaryValue")),
                        aux=(rate / price) if rate else None,
                    )
                )
            except (TypeError, ValueError) as exc:
                log.debug("skipping malformed exchange line: %s (%s)", ln.get("id"), exc)
        return meta, quotes

    def _parse_item(
        self, game: str, league: str, cat: CatDef, payload: Dict[str, Any]
    ) -> Tuple[FetchMeta, List[ItemQuote]]:
        meta = FetchMeta(
            game=game,
            league=league,
            category=cat.id,
            source="item",
            primary="chaos" if game == "poe1" else "divine",
            secondary="divine" if game == "poe1" else "chaos",
            rates={},
        )

        quotes: List[ItemQuote] = []
        for ln in payload.get("lines") or []:
            try:
                name = str(ln.get("name") or ln.get("itemId") or ln.get("id"))
                details_id = str(ln.get("detailsId") or "")
                variant = ln.get("variant")
                display = f"{name} ({variant})" if variant else name
                if game == "poe1":
                    price = ln.get("chaosValue")
                    if price is None:
                        price = ln.get("primaryValue")
                else:
                    price = ln.get("primaryValue")
                price = float(price or 0.0)
                if price <= 0:
                    continue
                lc = ln.get("listingCount")
                list_count = int(lc) if isinstance(lc, (int, float)) else None
                if list_count is not None and list_count < 1:
                    continue            # no live listings: the price is stale
                spark = ln.get("sparkLine") or ln.get("sparkline") or {}
                quotes.append(
                    ItemQuote(
                        key=f"{game}:i:{cat.id}:{details_id}:{variant or ''}",
                        name=display,
                        game=game,
                        category=cat.id,
                        source="item",
                        price=price,
                        list_count=list_count,
                        icon=self._abs_icon(ln.get("icon")),
                        details_id=details_id or None,
                        spark_total_change=_f(spark.get("totalChange")),
                        volume=_f(ln.get("count")),
                    )
                )
            except (TypeError, ValueError) as exc:
                log.debug("skipping malformed item line: %s (%s)", ln.get("name"), exc)
        return meta, quotes

    @staticmethod
    def _abs_icon(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        url = str(url)
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return BASE_URL + url
        return url

    # ------------------------------------------------------------ transport
    def _request(self, url: str, params: Optional[Dict[str, str]] = None):
        """GET with ETag caching, retries and error mapping.

        Retries network errors and 5xx with jittered exponential backoff;
        429/503 additionally honour the Retry-After header. 404 and other 4xx
        fail immediately. Returns a requests.Response, or the UNCHANGED
        sentinel on HTTP 304.
        """
        cache_key = url + ("?" + urlencode(sorted((params or {}).items())) if params else "")
        headers: Dict[str, str] = {"Accept": "application/json",
                                   "User-Agent": self._user_agent_fn()}
        etag = self._etags.get(cache_key)
        if etag:
            headers["If-None-Match"] = etag

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                resp = self._sess.get(url, params=params, headers=headers,
                                      timeout=self._timeout_fn())
            except requests.RequestException as exc:
                last_err = exc
                log.warning("request failed (attempt %d/%d): %s %s -> %s",
                            attempt + 1, MAX_RETRIES, url, params or {}, exc)
                self._backoff(attempt)
                continue

            if resp.status_code == 304:
                log.debug("304 not modified: %s", cache_key)
                return UNCHANGED
            if resp.history:
                log.info("endpoint moved: %s -> %s", url, resp.url)  # audit: log lost routes
            if resp.status_code == 404:
                raise CategoryUnavailable(f"{cat_label(url, params)}: not available (404)")
            if resp.status_code == 429 or resp.status_code in RETRY_STATUS_CODES:
                wait = self._retry_after(resp)
                if attempt + 1 < MAX_RETRIES:
                    log.warning("HTTP %d (attempt %d/%d)%s: %s %s", resp.status_code,
                                attempt + 1, MAX_RETRIES,
                                f", server asked to wait {wait:.0f}s" if wait else "",
                                url, params or {})
                    self._backoff(attempt, floor=wait)
                    continue
                if resp.status_code == 429:
                    raise RateLimited("rate limited by poe.ninja (HTTP 429)")
                raise ApiError(f"poe.ninja returned HTTP {resp.status_code} for {url}")
            if resp.status_code >= 400:
                raise ApiError(f"poe.ninja returned HTTP {resp.status_code} for {url}")

            et = resp.headers.get("ETag")
            if et:
                self._etags[cache_key] = et
            return resp
        raise ApiError(f"network error after {MAX_RETRIES} attempts: {last_err}")

    def _backoff(self, attempt: int, floor: float = 0.0) -> None:
        """Jittered exponential backoff, never below a server-requested floor."""
        delay = min(MAX_BACKOFF_SECONDS,
                    BACKOFF_BASE_SECONDS * (2 ** attempt) * uniform(0.5, 1.5))
        time.sleep(max(delay, floor))

    @staticmethod
    def _retry_after(resp) -> float:
        """Server-requested delay in seconds, clamped to sane bounds (0 = absent)."""
        raw = resp.headers.get("Retry-After", "")
        try:
            return min(max(float(raw), 1.0), RETRY_AFTER_MAX_SECONDS)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _json(resp, context: str) -> Any:
        """Parse a JSON body, mapping malformed payloads onto ApiError so the
        poller's except-ApiError handler covers truncated/HTML responses."""
        try:
            return resp.json()
        except (ValueError, TypeError) as exc:
            raise ApiError(f"malformed JSON from {context}: {exc}") from exc

    def _throttle(self) -> None:
        gap = time.time() - self._last_request_ts
        if gap < MIN_REQUEST_GAP_SECONDS:
            time.sleep(MIN_REQUEST_GAP_SECONDS - gap)
        self._last_request_ts = time.time()


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cat_label(url: str, params: Optional[Dict[str, str]]) -> str:
    """Human-ish label for error messages."""
    game = "poe1" if "/poe1/" in url else "poe2"
    cat = (params or {}).get("type", "?")
    return f"{game}/{cat}"
