"""discovery.py - auto-parse poe.ninja's site index for new item sections.

poe.ninja's economy page links every section the site currently exposes as
``/poe<N>/economy/<slug>/``. Fetching that page once a day and diffing its
slugs against the built-in registry lets NinjaAlert pick up brand-new
sections without an app update: unknown slugs are camel-cased into an API
``type`` guess ("unique-relics" -> "UniqueRelics") and probed against the two
documented endpoints; anything that answers with data is registered as a
custom section (persisted in config, auto-added to the watch list).

Sections that disappear (e.g. retired league mechanics) are auto-cleared
from the watch list after repeated 404s (see PollWorker._maybe_autoclear).

Pure functions here are unit-testable without network.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List

from categories import CATEGORIES

log = logging.getLogger("ninjaalert.discovery")

# href=".../poe1/economy/<slug>/" links on the site index
_SLUG_RE = re.compile(r"href=\"[^\"]*?/poe([12])/economy/([a-z0-9-]+)/?\"", re.IGNORECASE)

# non-category pages linked from the index layout
_SKIP_SLUGS = {"leagues", "charts", "search", "about", "api"}


def parse_site_slugs(html: str, game: str) -> List[str]:
    """Unique economy-section slugs for the game, in first-seen order."""
    if not html or game not in ("poe1", "poe2"):
        return []
    want = game[-1]                       # "poe1" -> "1"
    seen: Dict[str, None] = {}
    for g, slug in _SLUG_RE.findall(html):
        slug = slug.lower()
        if g == want and slug not in _SKIP_SLUGS:
            seen.setdefault(slug, None)
    return list(seen)


def slug_to_type(slug: str) -> str:
    """'unique-relics' -> 'UniqueRelics' (best-effort API type guess)."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_\s]+", slug.strip()) if p)


def known_slugs(game: str) -> Dict[str, str]:
    """slug -> registry id (ids, explicit slugs, and a plural-form guess)."""
    out: Dict[str, str] = {}
    for cid, cat in CATEGORIES[game].items():
        out.setdefault(cid.lower(), cid)
        out.setdefault(cid.lower() + "s", cid)      # 'tattoos' -> Tattoo
        if cat.slug:
            out.setdefault(cat.slug.lower(), cid)
    return out


def unknown_slugs(game: str, slugs: List[str], limit: int = 6) -> List[str]:
    """Site slugs that map to no known category - probe candidates."""
    known = known_slugs(game)
    return [s for s in slugs if s not in known][:limit]
