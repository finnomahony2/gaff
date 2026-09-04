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
guess. That applies to the REGION as much as the month: :func:`region_for` returns
``None`` for a subject it cannot place, rather than a default series, because an
adjustment made in the wrong market is a confident error, not a small one. The factor
is clamped to a sane band so a data glitch can't wildly move a price.

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
# Deliberately small + explicit. An area that is not in here is not silently
# mapped to anything: see region_for, which abstains rather than defaulting.
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
# There is deliberately no _DEFAULT_REGION. A subject this map cannot place has
# no region, and "no region" means no adjustment (region_for -> None).


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
    return os.path.join(_root("CACHE_DIR"), _cache_name(region, month))


def _read_cache_json(region: str, month: str) -> Optional[Dict[str, Any]]:
    """First VALID cached record: user cache, then shipped warm cache.

    Validated per candidate (parseable, a dict, current cacheSchema) so a
    corrupt or stale-shaped user-tier file cannot mask a valid shipped one.
    """
    name = _cache_name(region, month)
    for root in (_root("CACHE_DIR"), _root("SHIPPED_CACHE_DIR")):
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


def region_for(subject: Any) -> Optional[str]:
    """The UK HPI region slug for a subject, or ``None`` when it cannot be placed.

    Three outcomes, and the third is the whole point:

    1. an explicit ``borough``/``localAuthority``/``laName``/``district`` field —
       the subject's OWN region. Mapped through :data:`_AREA_SLUG` when it names a
       known area, otherwise slugified as given ("Stratford-on-Avon" ->
       ``stratford-on-avon``), which is how UK HPI names districts anyway. A slug
       the endpoint does not recognise still fails closed: ``fetch_month`` finds
       nothing and the factor stays 1.0.
    2. a known area name found in the address string -> its borough slug.
    3. nothing recognised -> ``None``. Not ``"london"``.

    Outcome 3 used to be the London-wide series, and that was a real adjustment
    made in the wrong market: a Leeds flat lifted by London's 2021-to-2025 curve
    is not a smaller error than no adjustment, it is a confident one. The fallback
    was invisible until S5 started printing the region on every verdict, where it
    would have read "(london)" under a Yorkshire address.

    Abstaining costs no verdict — :func:`hpi_factor` returns 1.0 for a falsy
    region, so the comps simply stand in the money of their own sale dates, and
    the tag, band and confidence are all still produced. It is also what this
    module promises everywhere else: a missing month is never guessed either.
    ``flips._hpi_region`` reached the same conclusion first and named this
    fallback as the reason it would not reuse this function.
    """
    for k in ("borough", "localAuthority", "laName", "district"):
        v = _g(subject, k)
        if v:
            slug = _AREA_SLUG.get(str(v).strip().lower())
            if slug:
                return slug
            return re.sub(r"[^a-z0-9]+", "-", str(v).strip().lower()).strip("-") or None
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
    return None


# ---------------------------------------------------------------------------
# Fetch (cache-first) + the factor.
# ---------------------------------------------------------------------------

def fetch_month(region: Optional[str], month: Optional[str], *,
                offline: bool = False) -> Optional[Dict[str, Any]]:
    """The UK HPI record (``primaryTopic``) for a borough+month. Cache-first; fetches
    once on a miss unless ``offline``. Returns ``None`` when unavailable — which
    includes a ``None`` region, the honest answer :func:`region_for` gives for a
    subject it cannot place."""
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
    os.makedirs(_root("CACHE_DIR"), exist_ok=True)
    with open(path, "w") as f:
        json.dump(topic, f)
    return topic


def avg_price(region: Optional[str], property_type: Optional[str], month: Optional[str], *,
              offline: bool = False) -> Optional[float]:
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


def hpi_factor(region: Optional[str], property_type: Optional[str], from_date: Optional[str],
               to_date: Optional[str] = None, *, offline: bool = False) -> float:
    """``price(as_of) / price(sale_month)`` for this borough + property type, clamped to a
    sane band. Returns ``1.0`` (no adjustment, never a guess) when the region or either
    month is unavailable — so a future-dated comp, an uncached month, or a subject
    :func:`region_for` could not place simply isn't adjusted."""
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


_LAZY_DIRS = {"CACHE_DIR": lambda: paths.cache_dir("hpi"),
              "SHIPPED_CACHE_DIR": lambda: paths.shipped_dir("hpi")}


# ---------------------------------------------------------------------------
# Cache locations: resolved ON USE, and overridable by assignment.
#
# These were module-level constants (``CACHE_DIR = paths.cache_dir(...)``),
# which snapshotted $GAFF_CACHE_DIR at IMPORT time. Anything that set the
# variable afterwards — a test isolating itself, an embedding pointing Gaff at
# its own cache — moved ``paths.*`` but not these, and got silent PARTIAL
# isolation: reads through ``paths.read_candidates`` followed the new root while
# this module kept using the old one.
#
# So the names are no longer bound at import. ``_root()`` resolves them fresh on
# every use, and the public names are served lazily by :pep:`562`. Assignment
# still wins, because tests/test_epc.py isolates itself precisely that way
# ("monkeypatched globals are read at call time") and that contract is kept: an
# assignment lands in the module globals, which ``_root`` checks first.
# ---------------------------------------------------------------------------

def _root(name):
    """The directory for ``name``, honouring an assignment, else resolved now."""
    override = globals().get(name)
    return override if override is not None else _LAZY_DIRS[name]()


def __getattr__(name):
    if name in _LAZY_DIRS:
        return _LAZY_DIRS[name]()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
