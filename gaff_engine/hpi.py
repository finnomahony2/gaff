"""UK House Price Index adapter (B1) — time-adjust an old comp to today's money.

The Value Verdict (U3, :mod:`gaff_engine.value`) prices a subject off the median
£/sqft of nearby sold comps. But a comp that sold in 2021 is quoted in 2021 money;
in a rising market that drags the fair estimate DOWN, so a genuinely fair listing
reads as "over" and a real steal is missed. This adapter removes that bias: it
returns ``factor = price(as_of) / price(sale_month)`` for the comp's BOROUGH and
PROPERTY TYPE, so each comp's £/sqft can be nudged to as-of money before the median.

Source: the UK House Price Index on HM Land Registry's linked-data platform —
``http://landregistry.data.gov.uk/data/ukhpi/region/<slug>/month/<YYYY-MM>.json``
(the same host the Price Paid comps come from, ``landreg.py``). Published under the
Open Government Licence. The per-month record exposes an all-types
``averagePrice``/``housePriceIndex`` and per-type ``averagePriceDetached`` /
``…SemiDetached`` / ``…Terraced`` / ``…FlatMaisonette`` — we use the per-type figure
(what Finn asked for: movement by property type in a given borough), falling back to
all-types, then to no adjustment.

**Honest by construction.** When the borough+month isn't available (thin data, a
future-dated comp, an unmapped region) the factor is ``1.0`` — no adjustment, never a
guess. The factor is clamped to a sane band so a data glitch can't wildly move a price.

Offline-friendly like ``landreg``/``epc``: months are cached under ``data/hpi/``. A
cached month is read without network; an uncached month is fetched once (unless
``offline=True``, when it yields no data → factor 1.0). Tests run off the committed
cache and never hit the network.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from gaff_engine import paths

# Writable per-user cache for writes; shipped warm cache for reads that miss it.
CACHE_DIR = paths.cache_dir("hpi")
SHIPPED_CACHE_DIR = paths.shipped_dir("hpi")
BASE = "http://landregistry.data.gov.uk/data/ukhpi/region"
# Overridable so anyone running the package identifies themselves upstream.
# PLACEHOLDER: the gaff-engine GitHub org is not registered, so the default
# carries no contact URL (an unregistered org is claimable by anyone). Point
# GAFF_USER_AGENT — or this default, once a real home exists — at a URL the
# operator actually controls (BACKLOG R3).
USER_AGENT = os.environ.get("GAFF_USER_AGENT", "Gaff/0.1 (open-data client)")
REQUEST_TIMEOUT_SECONDS = 25

# On-disk cache shape version, injected as one extra key into the stored
# record. Mismatch reads as a miss; a missing field is version 1.
CACHE_SCHEMA = 1

# The as-of month we adjust TO — the latest UK HPI month available at build time.
# Pinned (not wall-clock) so the golden fixture + tests stay deterministic; live
# scoring may pass a fresher month explicitly.
AS_OF_MONTH = "2025-06"

# Gaff propertyType -> the UK HPI per-type average-price field.
_TYPE_FIELD = {
    "detached": "averagePriceDetached",
    "semi-detached": "averagePriceSemiDetached", "semi detached": "averagePriceSemiDetached",
    "terraced": "averagePriceTerraced", "terrace": "averagePriceTerraced",
    "end terrace": "averagePriceTerraced", "end-of-terrace": "averagePriceTerraced",
    "flat": "averagePriceFlatMaisonette", "flat-maisonette": "averagePriceFlatMaisonette",
    "maisonette": "averagePriceFlatMaisonette", "apartment": "averagePriceFlatMaisonette",
}

# Sanity band on the factor — real 4-5 year borough moves sit well inside this;
# anything outside signals bad data, so we fall back to no adjustment.
_FACTOR_CLAMP = (0.5, 2.0)

# Minimal outcode/area -> borough-slug map for the boroughs Gaff's data covers.
# Deliberately small + explicit; unmapped areas fall back to a London-wide series.
_AREA_SLUG = {
    "hackney": "hackney", "de beauvoir": "hackney", "dalston": "hackney",
    "london fields": "hackney", "clapton": "hackney", "de beauvoir town": "hackney",
    "islington": "islington", "canonbury": "islington", "barnsbury": "islington",
    "tower hamlets": "tower-hamlets", "bethnal green": "tower-hamlets", "bow": "tower-hamlets",
    "hackney downs": "hackney", "stoke newington": "hackney", "victoria park": "tower-hamlets",
    "newham": "newham", "waltham forest": "waltham-forest", "walthamstow": "waltham-forest",
    "haringey": "haringey", "camden": "camden", "westminster": "city-of-westminster",
    # The second warm-cache city (L2C): its verdicts must not be adjusted in
    # London money. Leamington Spa sits in Warwick district, whose UK HPI
    # series is warmed alongside the comps.
    "leamington spa": "warwick", "leamington": "warwick", "warwick": "warwick",
}
_DEFAULT_REGION = "london"


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

def _g(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def month_of(date_str: Optional[str]) -> Optional[str]:
    """A sale date (``YYYY-MM-DD`` or ``YYYY-MM``) -> its ``YYYY-MM`` bucket."""
    if not date_str:
        return None
    m = re.match(r"(\d{4})-(\d{2})", str(date_str))
    return "%s-%s" % (m.group(1), m.group(2)) if m else None


def _cache_name(region: str, month: str) -> str:
    return "%s_%s.json" % (region, month)


def _cache_path(region: str, month: str) -> str:
    """Where a fetched record is written: always the user cache."""
    return os.path.join(CACHE_DIR, _cache_name(region, month))


def _read_cache_json(region: str, month: str) -> Optional[Dict[str, Any]]:
    """First VALID cached record: user cache, then shipped warm cache.

    Validated per candidate (parseable, a dict, current cacheSchema) so a
    corrupt or stale-shaped user-tier file cannot mask a valid shipped one.
    """
    name = _cache_name(region, month)
    for root in (CACHE_DIR, SHIPPED_CACHE_DIR):
        candidate = os.path.join(root, name)
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate) as f:
                doc = json.load(f)
        except (ValueError, OSError):
            continue
        if isinstance(doc, dict) and doc.get("cacheSchema", 1) == CACHE_SCHEMA:
            return doc
    return None


def normalise_type(property_type: Optional[str]) -> str:
    """Gaff propertyType -> HPI field, defaulting to flats (the dominant stock)."""
    return _TYPE_FIELD.get(str(property_type or "").strip().lower(), "averagePriceFlatMaisonette")


def region_for(subject: Any) -> str:
    """Best-effort borough slug for a subject. Prefers an explicit borough/localAuthority
    field, then an address/area string match, else the London-wide series."""
    for k in ("borough", "localAuthority", "laName", "district"):
        v = _g(subject, k)
        if v:
            slug = _AREA_SLUG.get(str(v).strip().lower())
            if slug:
                return slug
            return re.sub(r"[^a-z0-9]+", "-", str(v).strip().lower()).strip("-") or _DEFAULT_REGION
    # Fall back to scanning an address / display string for a known area.
    addr = ""
    for k in ("address", "displayAddress", "area", "street"):
        v = _g(subject, k)
        if isinstance(v, str):
            addr += " " + v.lower()
        elif v is not None:
            addr += " " + str(_g(v, "display", "") or "").lower()
    for name, slug in _AREA_SLUG.items():
        if name in addr:
            return slug
    return _DEFAULT_REGION


# ---------------------------------------------------------------------------
# Fetch (cache-first) + the factor.
# ---------------------------------------------------------------------------

def fetch_month(region: str, month: str, *, offline: bool = False) -> Optional[Dict[str, Any]]:
    """The UK HPI record (``primaryTopic``) for a borough+month. Cache-first; fetches
    once on a miss unless ``offline``. Returns ``None`` when unavailable."""
    if not region or not month:
        return None
    path = _cache_path(region, month)
    doc = _read_cache_json(region, month)
    if doc is not None:
        return doc
    # no valid cached record in either tier: fall through to fetch
    if offline:
        return None
    url = "%s/%s/month/%s.json" % (BASE, region, month)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            doc = json.load(resp)
        topic = (doc.get("result") or {}).get("primaryTopic")
    except (urllib.error.URLError, http.client.HTTPException, ValueError, OSError):
        return None
    if not isinstance(topic, dict):
        return None
    topic["cacheSchema"] = CACHE_SCHEMA
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(topic, f)
    return topic


def avg_price(region: str, property_type: str, month: str, *, offline: bool = False) -> Optional[float]:
    """Per-type average price for a borough+month (falls back to the all-types average)."""
    topic = fetch_month(region, month, offline=offline)
    if not topic:
        return None
    field = normalise_type(property_type)
    v = topic.get(field)
    if v is None:
        v = topic.get("averagePrice")           # all-types fallback
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def hpi_factor(region: str, property_type: str, from_date: Optional[str],
               to_date: Optional[str] = None, *, offline: bool = False) -> float:
    """``price(as_of) / price(sale_month)`` for this borough + property type, clamped to a
    sane band. Returns ``1.0`` (no adjustment, never a guess) when either month is
    unavailable — so a future-dated or unmapped comp simply isn't adjusted."""
    from_m = month_of(from_date)
    to_m = month_of(to_date) or AS_OF_MONTH
    if not from_m or from_m >= to_m:
        return 1.0                                # already at/after as-of: nothing to lift
    a = avg_price(region, property_type, from_m, offline=offline)
    b = avg_price(region, property_type, to_m, offline=offline)
    if not a or not b or a <= 0:
        return 1.0
    factor = b / a
    lo, hi = _FACTOR_CLAMP
    return min(hi, max(lo, factor))


__all__ = [
    "AS_OF_MONTH", "CACHE_DIR", "SHIPPED_CACHE_DIR", "month_of", "normalise_type", "region_for",
    "fetch_month", "avg_price", "hpi_factor",
]
