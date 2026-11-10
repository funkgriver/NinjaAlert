"""NinjaAlert - poe.ninja category registry (valid API `type` values).

Source of truth: https://poe.ninja/docs/api (as of 2026-11).
Each category is fetched from one of two endpoints:
  exchange -> /{game}/api/economy/exchange/current/overview   (bulk price data)
  item     -> /{game}/api/economy/stash/current/item/overview (listed item data)
"""
from __future__ import annotations

from typing import Dict, NamedTuple, Optional


class CatDef(NamedTuple):
    id: str          # API `type` query value
    source: str      # "exchange" | "item"
    label: str       # human label for UI
    slug: Optional[str]  # best-effort site URL slug for click-through


def _x(id_: str, label: str, slug: Optional[str] = None) -> CatDef:
    return CatDef(id_, "exchange", label, slug)


def _i(id_: str, label: str, slug: Optional[str] = None) -> CatDef:
    return CatDef(id_, "item", label, slug)


CATEGORIES: Dict[str, Dict[str, CatDef]] = {
    "poe1": {
        # ---- exchange (bulk economy) -------------------------------------
        "Currency":            _x("Currency", "Currency", "currency"),
        "Fragment":            _x("Fragment", "Fragments", "fragments"),
        "DivinationCard":      _x("DivinationCard", "Divination Cards", "divination-cards"),
        "Scarab":              _x("Scarab", "Scarabs", "scarabs"),
        "Essence":             _x("Essence", "Essences", "essences"),
        "Oil":                 _x("Oil", "Oils", "oils"),
        "DeliriumOrb":         _x("DeliriumOrb", "Delirium Orbs", "delirium-orbs"),
        "Fossil":              _x("Fossil", "Fossils", "fossils"),
        "Resonator":           _x("Resonator", "Resonators", "resonators"),
        "Artifact":            _x("Artifact", "Artifacts", "artifacts"),
        "Omen":                _x("Omen", "Omens", "omens"),
        "Tattoo":              _x("Tattoo", "Tattoos", "tattoos"),
        "Runegraft":           _x("Runegraft", "Runegrafts", "runegrafts"),
        "AllflameEmber":       _x("AllflameEmber", "Allflame Embers", "allflame-embers"),
        "DjinnCoin":           _x("DjinnCoin", "Djinn Coins", "djinn-coins"),
        "Ducat":               _x("Ducat", "Ducats", "ducats"),
        "EnshroudingCrystal":  _x("EnshroudingCrystal", "Enshrouding Crystals", "enshrouding-crystals"),
        "Astrolabe":           _x("Astrolabe", "Astrolabes", "astrolabes"),
        # ---- stash items -------------------------------------------------
        "UniqueWeapon":        _i("UniqueWeapon", "Unique Weapons", "unique-weapons"),
        "UniqueArmour":        _i("UniqueArmour", "Unique Armour", "unique-armours"),
        "UniqueAccessory":     _i("UniqueAccessory", "Unique Accessories", "unique-accessories"),
        "UniqueFlask":         _i("UniqueFlask", "Unique Flasks", "unique-flasks"),
        "UniqueJewel":         _i("UniqueJewel", "Unique Jewels", "unique-jewels"),
        "ForbiddenJewel":      _i("ForbiddenJewel", "Forbidden Jewels", "forbidden-jewels"),
        "SkillGem":            _i("SkillGem", "Skill Gems", "skill-gems"),
        "ImbuedGem":           _i("ImbuedGem", "Imbued Gems", "imbued-gems"),
        "ClusterJewel":        _i("ClusterJewel", "Cluster Jewels", "cluster-jewels"),
        "Map":                 _i("Map", "Maps", "maps"),
        "BlightedMap":         _i("BlightedMap", "Blighted Maps", "blighted-maps"),
        "BlightRavagedMap":    _i("BlightRavagedMap", "Blight-ravaged Maps", "blight-ravaged-maps"),
        "UniqueMap":           _i("UniqueMap", "Unique Maps", "unique-maps"),
        "ValdoMap":            _i("ValdoMap", "Valdo's Maps", "valdo-maps"),
        "Invitation":          _i("Invitation", "Invitations", "invitations"),
        "Memory":              _i("Memory", "Memories", "memories"),
        "IncursionTemple":     _i("IncursionTemple", "Incursion Temples"),
        "ScryingOrb":          _i("ScryingOrb", "Scrying Orbs", "scrying-orbs"),
        "BaseType":            _i("BaseType", "Base Types", "base-types"),
        "Flask":               _i("Flask", "Flasks", "flasks"),
        "Beast":               _i("Beast", "Beasts", "beasts"),
        "Vial":                _i("Vial", "Vials", "vials"),
        "Wombgift":            _i("Wombgift", "Wombgifts", "wombgifts"),
        "Corpse":              _i("Corpse", "Corpses", "corpses"),
        "Incubator":           _i("Incubator", "Incubators", "incubators"),
        "ShrineBelt":          _i("ShrineBelt", "Shrine Belts", "shrine-belts"),
        "UniqueTincture":      _i("UniqueTincture", "Unique Tinctures", "unique-tinctures"),
        "UniqueRelic":         _i("UniqueRelic", "Unique Relics", "unique-relics"),
    },
    "poe2": {
        # ---- exchange (bulk economy) -------------------------------------
        "Currency":            _x("Currency", "Currency", "currency"),
        "Fragments":           _x("Fragments", "Fragments", "fragments"),
        "Abyss":               _x("Abyss", "Abyss"),
        "UncutGems":           _x("UncutGems", "Uncut Gems", "uncut-gems"),
        "LineageSupportGems":  _x("LineageSupportGems", "Lineage Support Gems", "lineage-support-gems"),
        "Essences":            _x("Essences", "Essences", "essences"),
        "SoulCores":           _x("SoulCores", "Soul Cores", "soul-cores"),
        "Idols":               _x("Idols", "Idols", "idols"),
        "Runes":               _x("Runes", "Runes", "runes"),
        "RitualOmens":         _x("RitualOmens", "Ritual Omens", "omens"),
        "Expedition":          _x("Expedition", "Expedition", "expedition"),
        "Delirium":            _x("Delirium", "Delirium"),
        "Breach":              _x("Breach", "Breach"),
        "Verisium":            _x("Verisium", "Verisium", "verisium"),
        # ---- stash items -------------------------------------------------
        "UniqueWeapons":       _i("UniqueWeapons", "Unique Weapons", "unique-weapons"),
        "UniqueArmours":       _i("UniqueArmours", "Unique Armour", "unique-armours"),
        "UniqueAccessories":   _i("UniqueAccessories", "Unique Accessories", "unique-accessories"),
        "UniqueFlasks":        _i("UniqueFlasks", "Unique Flasks", "unique-flasks"),
        "UniqueCharms":        _i("UniqueCharms", "Unique Charms", "unique-charms"),
        "UniqueJewels":        _i("UniqueJewels", "Unique Jewels", "unique-jewels"),
        "UniqueSanctumRelics": _i("UniqueSanctumRelics", "Unique Sanctum Relics", "unique-relics"),
        "UniqueTablets":       _i("UniqueTablets", "Unique Tablets", "unique-tablets"),
        "PrecursorTablets":    _i("PrecursorTablets", "Precursor Tablets", "precursor-tablets"),
    },
}

# Sensible lean defaults - everything else can be enabled in Settings.
DEFAULT_WATCH: Dict[str, list] = {
    "poe1": [
        "Currency", "Fragment", "DivinationCard", "Scarab", "Essence",
        "UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueJewel",
        "SkillGem", "Map",
    ],
    "poe2": [
        "Currency", "Fragments", "Essences", "Runes", "UncutGems",
        "UniqueWeapons", "UniqueArmours", "UniqueAccessories", "UniqueJewels",
    ],
}

PRIMARY_UNIT_LABEL = {"chaos": "c", "divine": "div", "exalted": "ex"}


def get_cat(game: str, cat_id: str) -> CatDef:
    return CATEGORIES[game][cat_id]


def labels_for(game: str) -> Dict[str, str]:
    """Ordered {id: label} map for UI lists."""
    return {cid: c.label for cid, c in CATEGORIES[game].items()}
