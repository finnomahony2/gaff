"""U9 — HM Land Registry Price Paid comps adapter (Milestone M1).

The "fuel" behind the Value Verdict: fetches sold-transaction comparables from
HM Land Registry Price Paid (open, free, Open Government Licence), caches them
on disk, and parses them into :class:`~gaff_engine.schemas.Comp` records the
Value scorer (U3, later) blends into a fair estimate.

Public surface
--------------
* :func:`fetch_street`        — GET one street (town-scoped), cache once, idempotent.
* :func:`parse_comp`          — one raw PPD ``result.items[]`` item -> ``Comp``.
* :func:`get_comps`           — fetch many streets, parse, filter by year, dedupe, sort.
* :func:`comps_for_listing`   — pick the target street + a De Beauvoir nearby set.
* :func:`select_like_for_like`— the same-type-family / same-tenure subset to blend.
* :func:`hpi_factor`          — TODO stub returning 1.0 (UK HPI time-adjust, deferred).

Data source facts (recon-verified 2026-07-14)
---------------------------------------------
Endpoint: ``http://landregistry.data.gov.uk/data/ppi/transaction-record.json``
queried by ``propertyAddress.street`` + ``propertyAddress.town``, newest first::

    …/transaction-record.json?propertyAddress.street=NORTHCHURCH+ROAD
        &propertyAddress.town=LONDON&_sort=-transactionDate&_pageSize=40

Each ``result.items[]`` item carries: ``pricePaid`` (int), ``transactionDate``
(RFC-822, e.g. "Tue, 28 Apr 2026"), ``propertyAddress`` {paon, saon?, street,
postcode, district, town, county}, ``propertyType``, ``estateType``,
``newBuild`` (bool), ``transactionId``.

GOTCHA — ``propertyType`` / ``estateType`` are NOT plain strings. In practice
each is a *dict* with an ``_about`` URI plus ``label`` / ``prefLabel`` lists of
langString dicts (``{"_value","_lang","_datatype"}``); some feeds return the
bare langString list instead. :func:`_lang_value` handles every shape and never
``str()``-es the dict — ``prefLabel[0]._value`` gives "flat-maisonette" /
"Leasehold".

£/sqft GAP — Land Registry has NO floor area, so ``Comp.pricePerSqft`` is always
``None`` here. Per-comp £/sqft needs the EPC register (a free API key) — that is
DEFERRED and not attempted in U9. The Value Verdict (U3) runs on price-level
like-for-like for now.

TRANSACTION CATEGORY (verified against the cached envelopes 2026-08-29) — each
item also carries ``transactionCategory``, the same langString-dict shape as
``propertyType``, whose ``_about`` slug is ``standardPricePaidTransaction`` or
``additionalPricePaidTransaction``. "Additional price paid" rows are
repossessions, power-of-sale and transfers to non-private individuals — not
open-market sales — and standard AVM practice excludes them from fair
estimates. The JSON endpoint returns the field by default (no extra query
parameter needed) and :func:`fetch_street` caches items verbatim, so every raw
cache file — including ones written before this capture — carries it;
:func:`parse_comp` normalises it via :func:`_txn_category`. The ENRICHED file
(``comps_enriched.json``) is the trap: the category rides as an instance
attribute on ``Comp`` (the declared field is another workstream's), and
``serialize.to_jsonable`` emits declared dataclass fields only, so a persist
through it silently strips the category — ``enrich_run.comp_payload`` carries
it explicitly for exactly that reason (file regenerated with it 2026-08-29).
An enriched file written without the field reads as "category unknown"
downstream — treated as standard, but counted in provenance.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import warnings
import urllib.request
from typing import Any, Dict, List, Optional, Union

from gaff_engine import paths
from gaff_engine.schemas import Comp, CompAddress, Listing

# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

ENDPOINT = "http://landregistry.data.gov.uk/data/ppi/transaction-record.json"
# Contact string sent to the HMLR endpoint. Overridable so anyone running the
# package identifies themselves, rather than shipping one person's address.
# PLACEHOLDER: the gaff-engine GitHub org is not registered, so the default
# carries no contact URL (an unregistered org is claimable by anyone). Point
# GAFF_USER_AGENT — or this default, once a real home exists — at a URL the
# operator actually controls (BACKLOG R3).
USER_AGENT = os.environ.get("GAFF_USER_AGENT", "Gaff/0.1 (open-data client)")
REQUEST_PACING_SECONDS = 0.5          # be polite; ~0.5s between live requests

# Version of the on-disk envelope shape. Bump when the stored shape changes;
# a mismatched file is treated as a cache miss (refetched live, absent
# offline), never parsed on hope. Files written before versioning carry no
# field and count as version 1 — the current shape — so the shipped warm
# cache stays valid until the shape actually changes.
CACHE_SCHEMA = 1
REQUEST_TIMEOUT_SECONDS = 30

# Cache locations come from gaff_engine.paths: CACHE_DIR is the writable
# per-user cache every write lands in, SHIPPED_CACHE_DIR is the read-only warm
# cache that travels with the package. Reads try the user cache, then shipped.
CACHE_DIR = paths.cache_dir("comps")
SHIPPED_CACHE_DIR = paths.shipped_dir("comps")

# The De Beauvoir (N1) nearby-street set U9 pulls for coverage around the golden
# Northchurch Road maisonette (task-specified).
DE_BEAUVOIR_STREETS = [
    "NORTHCHURCH ROAD", "DE BEAUVOIR ROAD", "CULFORD ROAD", "MORTIMER ROAD",
    "UFTON ROAD", "BUCKINGHAM ROAD", "NORTHCHURCH TERRACE", "ENGLEFIELD ROAD",
]

# PPD propertyType slugs grouped into the two broad families the Value scorer
# treats as like-for-like. PPD lumps flats and maisonettes into one slug.
_FLAT_FAMILY = {"flat-maisonette"}
_HOUSE_FAMILY = {"terraced", "semi-detached", "detached"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Small parse helpers.
# ---------------------------------------------------------------------------

def _slug(street: str) -> str:
    """A filesystem-safe cache slug: ``"NORTHCHURCH ROAD" -> "northchurch-road"``."""
    s = re.sub(r"[^a-z0-9]+", "-", (street or "").strip().lower())
    return s.strip("-") or "unknown"


def _lang_value(node: Any) -> Optional[str]:
    """Extract the human ``_value`` from a Land Registry type node, any shape.

    Handles (a) a langString ``{"_value": ...}``; (b) a *list* of langStrings
    (recon shape) -> element 0; (c) a *dict* with ``prefLabel``/``label`` lists
    (observed shape) -> prefLabel preferred so "flat-maisonette" beats the
    title-cased "Flat-maisonette"; (d) a bare ``_about`` URI -> its last path
    segment; (e) a plain string. Never ``str()``-es a dict.
    """
    if node is None:
        return None
    if isinstance(node, str):
        # A URI like ".../def/common/flat-maisonette" -> its slug.
        return node.rstrip("/").rsplit("/", 1)[-1] if "/" in node else (node or None)
    if isinstance(node, list):
        for el in node:
            v = _lang_value(el)
            if v:
                return v
        return None
    if isinstance(node, dict):
        if "_value" in node:
            return node["_value"]
        for key in ("prefLabel", "label"):      # prefLabel is the lowercase slug
            if key in node:
                v = _lang_value(node[key])
                if v:
                    return v
        if "_about" in node:
            return _lang_value(node["_about"])
    return None


def _txn_category(node: Any) -> Optional[str]:
    """Normalise a PPD ``transactionCategory`` node to ``"standard"`` /
    ``"additional"`` / ``None`` (unknown).

    The node is the same langString-dict shape as ``propertyType``, so
    :func:`_lang_value` extracts either the prefLabel ("Standard price paid
    transaction") or the ``_about`` slug ("standardPricePaidTransaction") —
    both contain the word that matters. ``None`` means the item carried no
    recognisable category (e.g. a record written before the register exposed
    the field): downstream treats unknown as standard BUT counts it in
    provenance, so the limitation stays visible rather than silently vanishing.
    """
    v = _lang_value(node)
    if not v:
        return None
    s = str(v).strip().lower()
    if "standard" in s:
        return "standard"
    if "additional" in s:
        return "additional"
    return None


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Parse PPD ``transactionDate`` to ISO ``YYYY-MM-DD``.

    PPD gives an RFC-822-ish string like ``"Tue, 28 Apr 2026"``. Parsed with a
    hand-rolled month map (NOT ``strptime %b``) so it is locale-independent and
    deterministic. Also tolerates a bare ``"28 Apr 2026"`` and an already-ISO
    ``"2026-04-28"``. Returns ``None`` on empty; raises ``ValueError`` on a
    genuinely unparseable non-empty string.
    """
    if not raw:
        return None
    s = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):       # already ISO (optionally with time)
        return s[:10]
    if "," in s:                                  # drop the "Tue, " weekday prefix
        s = s.split(",", 1)[1].strip()
    parts = s.split()
    if len(parts) >= 3:
        mon = _MONTHS.get(parts[1][:3].lower())
        if mon is not None and parts[0].isdigit() and parts[2].isdigit():
            return "%04d-%02d-%02d" % (int(parts[2]), mon, int(parts[0]))
    raise ValueError("unparseable PPD transactionDate: %r" % raw)


# ---------------------------------------------------------------------------
# Fetch + cache.
# ---------------------------------------------------------------------------

def _rel(street: str, town: str = "LONDON") -> str:
    """Relative cache location for one street, scoped by town.

    Town scoping is not cosmetic. Street names repeat across the country
    ("High Street", "Church Road", "Station Road"), so a flat street-only
    namespace silently returns another town's sales for the same name. The
    layout is ``<town-slug>/<street-slug>.json``.
    """
    return os.path.join(_slug(town), "%s.json" % _slug(street))


def _legacy_rel(street: str) -> str:
    """The pre-town-scoping flat layout: ``<street-slug>.json``."""
    return "%s.json" % _slug(street)


def _cache_path(street: str, town: str = "LONDON") -> str:
    """Where a fetch for ``street`` is written: always the user cache."""
    return os.path.join(CACHE_DIR, _rel(street, town))


def _read_cache_paths(street: str, town: str = "LONDON") -> List[str]:
    """Every path a read should try, best first.

    User cache before shipped warm cache, and the town-scoped layout before the
    pre-town-scoping flat one, so an existing cache from either tier or either
    layout is still found rather than silently re-fetched.
    """
    rel, legacy = _rel(street, town), _legacy_rel(street)
    return [os.path.join(CACHE_DIR, rel),
            os.path.join(SHIPPED_CACHE_DIR, rel),
            os.path.join(CACHE_DIR, legacy),
            os.path.join(SHIPPED_CACHE_DIR, legacy)]


def fetch_street(
    street: str,
    town: str = "LONDON",
    page_size: int = 40,
    force: bool = False,
    offline: bool = False,
) -> List[Dict[str, Any]]:
    """Return the raw PPD ``result.items[]`` for one street, cached on disk.

    Idempotent: the first call fetches ``ENDPOINT`` (newest first), writes
    ``data/comps/<town-slug>/<street-slug>.json`` and returns its items; later
    calls read the cache. The cache is town-scoped because street names repeat
    across towns and a flat namespace returns the wrong town's sales silently.

    Parameters
    ----------
    force    : re-fetch and overwrite the cache even if it exists.
    offline  : never touch the network. Return the cached items if present, else
               ``[]``. Used by the deterministic test so it can NEVER hit the wire.
    """
    path = _cache_path(street, town)

    if not force:
        for candidate in _read_cache_paths(street, town):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    envelope = json.load(fh)
            except (ValueError, OSError) as exc:
                # Audible, matching the network guard's rationale: a skipped
                # tier that ends in [] must not look like "no sales exist".
                warnings.warn("skipping corrupt cache file %s (%s)"
                              % (candidate, exc), RuntimeWarning, stacklevel=2)
                continue
            if not isinstance(envelope, dict):
                warnings.warn("skipping malformed cache file %s (top level is "
                              "%s, not an envelope)" % (candidate, type(envelope).__name__),
                              RuntimeWarning, stacklevel=2)
                continue
            if envelope.get("cacheSchema", 1) != CACHE_SCHEMA:
                continue        # older/newer shape: a miss, not a parse-on-hope
            # A flat-layout file predates town scoping, so its town has to be
            # confirmed from the envelope before its items can be trusted.
            if os.path.basename(os.path.dirname(candidate)) != _slug(town):
                if (envelope.get("town") or "LONDON").upper() != town.upper():
                    continue
            return envelope.get("items", [])

    if offline:                                   # cache miss and forbidden to fetch
        return []

    params = {
        "propertyAddress.street": street.upper(),
        "propertyAddress.town": town.upper(),
        "_sort": "-transactionDate",
        "_pageSize": page_size,
    }
    url = "%s?%s" % (ENDPOINT, urllib.parse.urlencode(params))
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    time.sleep(REQUEST_PACING_SECONDS)            # pace ~0.5s apart
    # Guarded like the sibling hpi fetch (same exception tuple), but audibly:
    # comps are load-bearing where the HPI adjustment is an optional
    # enhancement, so a failed street degrades to [] WITH a warning naming the
    # upstream, never silently. Callers over many streets keep the rest.
    # (warnings.warn dedupes repeats of an identical message per process — the
    # first failure for a given street/cause prints, repeats stay quiet.)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            payload = json.load(resp)
        if not isinstance(payload, dict):
            raise ValueError("upstream body is %s, not an object"
                             % type(payload).__name__)
    except (urllib.error.URLError, http.client.HTTPException,
            ValueError, OSError) as exc:
        warnings.warn(
            "Land Registry fetch failed for %r in %r (%s: %s) — returning no "
            "comps for this street" % (street, town, type(exc).__name__, exc),
            RuntimeWarning, stacklevel=2)
        return []

    items = payload.get("result", {}).get("items", [])
    if not isinstance(items, list):
        items = []

    envelope = {
        "cacheSchema": CACHE_SCHEMA,
        "street": street.upper(),
        "town": town.upper(),
        "url": url,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(items),
        "items": items,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        # An unwritable cache must not discard a successful fetch.
        warnings.warn("could not cache %r in %r (%s) — returning the fetched "
                      "items uncached" % (street, town, exc),
                      RuntimeWarning, stacklevel=2)
    return items


# ---------------------------------------------------------------------------
# Parse a raw item -> Comp.
# ---------------------------------------------------------------------------

def parse_comp(item: Dict[str, Any], distance_note: str = "",
               source_date: Optional[str] = None) -> Comp:
    """Parse one raw PPD ``result.items[]`` item into a :class:`Comp`.

    ``propertyType`` / ``estateType`` go through :func:`_lang_value` (never
    ``str()``); the date is normalised to ISO; ``pricePerSqft`` stays ``None``
    (the Land Registry £/sqft gap — needs EPC, deferred).

    The PPD ``transactionCategory`` is captured too (normalised by
    :func:`_txn_category`) so the Value scorer can exclude non-open-market
    "additional price paid" rows from fair estimates. It rides as an INSTANCE
    attribute rather than a declared ``Comp`` field: the schema record is
    owned by another workstream, and the codebase already carries per-instance
    extras this way (``ValueVerdict.reasons``). ``epc.enrich_comps`` copies it
    across its ``dataclasses.replace`` so the enriched set keeps it.
    """
    addr = item.get("propertyAddress", {}) or {}
    price_raw = item.get("pricePaid")
    comp = Comp(
        price=int(price_raw) if price_raw is not None else None,
        date=_parse_date(item.get("transactionDate")),
        address=CompAddress(
            paon=addr.get("paon"),
            saon=addr.get("saon"),
            street=addr.get("street"),
            postcode=addr.get("postcode"),
            district=addr.get("district"),
            town=addr.get("town"),
            county=addr.get("county"),
        ),
        propertyType=_lang_value(item.get("propertyType")),
        tenure=_lang_value(item.get("estateType")),
        newBuild=bool(item.get("newBuild", False)),
        distanceNote=distance_note,
        pricePerSqft=None,                        # Land Registry has no floor area
        sourceDate=source_date,
        transactionId=item.get("transactionId"),
    )
    comp.transactionCategory = _txn_category(item.get("transactionCategory"))
    return comp


def _comp_key(comp: Comp) -> str:
    """Dedupe key: the PPD transactionId, else a content fingerprint."""
    if comp.transactionId:
        return comp.transactionId
    a = comp.address or CompAddress()
    return "|".join(str(x) for x in (comp.price, comp.date, a.paon, a.saon, a.street))


# ---------------------------------------------------------------------------
# get_comps / comps_for_listing.
# ---------------------------------------------------------------------------

def _collect(street_notes: List[tuple], town: str, since_year: int,
             force: bool, offline: bool) -> List[Comp]:
    """Shared spine: fetch each (street, note), parse, filter, dedupe, sort."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    comps: List[Comp] = []
    for street, note in street_notes:
        for item in fetch_street(street, town=town, force=force, offline=offline):
            comp = parse_comp(item, distance_note=note, source_date=today)
            if comp.price is None or not comp.date:
                continue                          # skip records we cannot use
            if int(comp.date[:4]) < since_year:   # recency filter (on/after since_year)
                continue
            comps.append(comp)

    seen: Dict[str, Comp] = {}
    for comp in comps:                            # dedupe, first (= same-street) wins
        seen.setdefault(_comp_key(comp), comp)
    out = list(seen.values())
    out.sort(key=lambda c: c.date, reverse=True)  # newest first
    return out


def get_comps(streets: List[str], since_year: int, town: str = "LONDON",
              force: bool = False, offline: bool = False) -> List[Comp]:
    """Fetch each street (cached), parse to :class:`Comp`, keep sales on/after
    ``since_year``, dedupe and sort newest first. Each comp's ``distanceNote`` is
    the street's own name (title-cased)."""
    street_notes = [(s, s.title()) for s in streets]
    return _collect(street_notes, town, since_year, force, offline)


def _listing_street(listing: Union[Listing, Dict[str, Any], str, None]) -> Optional[str]:
    """Pull the target street from a Listing, a dict, or a bare string (UPPER)."""
    if listing is None:
        return None
    if isinstance(listing, str):
        return listing.strip().upper() or None
    if isinstance(listing, dict):
        cand = listing.get("street") or listing.get("line1")
        if not cand:
            addr = listing.get("address") or {}
            if isinstance(addr, dict):
                cand = addr.get("street") or addr.get("line1") or addr.get("display")
        return cand.strip().upper() if cand else None
    # A Listing dataclass.
    addr = getattr(listing, "address", None)
    cand = getattr(addr, "line1", None) if addr is not None else None
    if not cand and addr is not None:
        # Fall back to the leading segment of the display string.
        disp = getattr(addr, "display", None)
        cand = disp.split(",")[0] if disp else None
    return cand.strip().upper() if cand else None


def comps_for_listing(listing: Union[Listing, Dict[str, Any], str],
                      since_year: int = 2021, town: str = "LONDON",
                      nearby: Optional[List[str]] = None,
                      force: bool = False, offline: bool = False) -> List[Comp]:
    """Comps for a Listing: its own street (tagged ``"same street"``) plus a
    sensible De Beauvoir nearby set (tagged by street name), fetched/cached,
    filtered to ``since_year`` (default 2021), deduped and sorted newest first.

    The listing may be a :class:`Listing`, a ``{"street": ..., "area": ...}``
    dict, or a bare street string. ``nearby`` overrides the default N1 set.
    """
    target = _listing_street(listing)
    nearby_streets = list(nearby if nearby is not None else DE_BEAUVOIR_STREETS)

    ordered: List[tuple] = []
    if target:
        ordered.append((target, "same street"))
    for s in nearby_streets:
        if target and s.upper() == target:        # already added as "same street"
            continue
        ordered.append((s, s.title()))
    return _collect(ordered, town, since_year, force, offline)


# ---------------------------------------------------------------------------
# select_like_for_like.
# ---------------------------------------------------------------------------

def _type_family(target_type: Any) -> set:
    """Map a target property type onto its PPD like-for-like slug family."""
    t = getattr(target_type, "value", target_type)
    t = str(t or "").strip().lower().replace("_", "-")
    if t in {"flat", "maisonette", "flat-maisonette", "conversion"}:
        return set(_FLAT_FAMILY)
    if t in {"terraced", "terrace", "end-terrace", "semi-detached", "detached", "house"}:
        return set(_HOUSE_FAMILY)
    return {t}                                     # unknown -> exact-match only


def _tenure_norm(tenure: Any) -> Optional[str]:
    """Normalise a tenure to ``"leasehold"`` / ``"freehold"`` (share-of-freehold
    folds to freehold for PPD matching, which only knows the two). ``None`` when
    unknown."""
    t = getattr(tenure, "value", tenure)
    t = str(t or "").strip().lower()
    if not t or t == "unknown":
        return None
    if "lease" in t:
        return "leasehold"
    if "free" in t:                                # freehold + share_of_freehold
        return "freehold"
    return t


def select_like_for_like(comps: List[Comp], target_type: Any,
                         target_tenure: Any = None) -> List[Comp]:
    """The like-for-like subset the fair estimate will blend.

    Keeps comps in the same broad *type family* as the target (a maisonette / flat
    matches only PPD ``flat-maisonette``; a house matches terraced / semi-detached
    / detached) and, *where both are known*, the same tenure. Comps whose tenure
    is unknown are not disqualified on tenure.
    """
    family = _type_family(target_type)
    want_tenure = _tenure_norm(target_tenure)
    out: List[Comp] = []
    for comp in comps:
        ptype = (comp.propertyType or "").strip().lower()
        if ptype not in family:
            continue
        if want_tenure is not None:
            comp_tenure = _tenure_norm(comp.tenure)
            if comp_tenure is not None and comp_tenure != want_tenure:
                continue
        out.append(comp)
    return out


# ---------------------------------------------------------------------------
# UK HPI time-adjust — DEFERRED stub (task-optional).
# ---------------------------------------------------------------------------

def hpi_factor(region: str, from_date: str, to_date: Optional[str] = None) -> float:
    """DEPRECATED — superseded by :func:`gaff_engine.hpi.hpi_factor` (B1), which does the
    real UK HPI time-adjustment per BOROUGH + PROPERTY TYPE off a committed cache. Kept as
    a thin redirect (assuming flats) so no old caller breaks; new code calls
    ``gaff_engine.hpi`` directly. No longer a ``1.0`` stub."""
    from gaff_engine.hpi import hpi_factor as _hpi_factor
    return _hpi_factor(region, "flat-maisonette", from_date, to_date)


__all__ = [
    "ENDPOINT", "CACHE_DIR", "SHIPPED_CACHE_DIR", "DE_BEAUVOIR_STREETS",
    "fetch_street", "parse_comp", "get_comps", "comps_for_listing",
    "select_like_for_like", "hpi_factor",
]
