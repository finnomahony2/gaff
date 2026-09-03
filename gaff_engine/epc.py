"""U9 — EPC floor-area adapter (Milestone M1; completes the U9 data layer).

The missing half of the £/sqft story. HM Land Registry Price Paid gives us the
sold *price* of a comp but never its *floor area* (see ``landreg.py`` — every
:class:`~gaff_engine.schemas.Comp` leaves ``pricePerSqft`` ``None``). This module
closes that gap against the free EPC (Energy Performance of Buildings) register:
it looks up a comp's certificate by postcode + house number, reads the certified
``total_floor_area`` (m²), converts to sqft and stamps a real £/sqft — so the
Value Verdict can compare like-for-like on *size-normalised* money instead of raw
price, where a maisonette and a whole house sit £1m+ apart for no useful reason.

Public surface
--------------
* :func:`search_postcode`      — GET the EPC domestic search for one postcode (cached).
* :func:`fetch_certificate`    — GET one certificate incl. ``total_floor_area`` (cached).
* :func:`floor_area_sqft_for`  — postcode + house-number (+ optional ``sale_date``) →
                                 floor area in sqft (or None).
* :func:`enrich_comps`         — fill ``pricePerSqft`` + full EPC provenance on a list
                                 of comps where a certificate matches.

Data integrity (sale-date matched areas)
-----------------------------------------
A comp's £/sqft is only as good as the floor area it divides by. Two traps: a
*stale* area (a 2026 sale over a 2012 EPC) and an *extension* (the property was
enlarged between EPC and sale, so the old area over-states £/sqft). So instead of
blindly taking the newest EPC, we gather the **full EPC history** at an address,
pick the one whose ``registrationDate`` is the latest **on or before the comp's
sale date** (the area *as configured at sale*), and stamp each comp with the EPC
date, the sale↔EPC gap in years, an ``areaChanged`` flag when the address's EPCs
disagree on floor area (>5% or >5 m²), an ``epcAfterSaleOnly`` flag when no EPC
pre-dates the sale, and an ``areaConfidence`` in {high, medium, low} the Value
scorer can gate on.

Data source facts (recon-verified 2026-07-14; do NOT re-explore)
----------------------------------------------------------------
Base ``https://api.get-energy-performance-data.communities.gov.uk/api``. Every
request carries ``Authorization: Bearer <token>`` + ``Accept: application/json``;
the token is resolved at runtime by :func:`gaff_engine.paths.epc_token`
(``$GAFF_EPC_TOKEN``, the macOS keychain, ``~/.gaff/epc_token``, then the
development ``.secrets`` fallback) and is NEVER logged, printed, hard-coded or
written to any artifact.

* SEARCH  ``/domestic/search?postcode=<PC>&page_size=<n>`` →
  ``{"data": <rows|{"rows": rows}>, "pagination": {...}}``. ``data`` is a bare
  list on some postcodes and a ``{"rows": [...]}`` dict on others — both handled.
  Each row: ``addressLine1`` ("104b, Sample Road" — synthetic examples
  throughout; real rows carry real addresses), ``addressLine2``,
  ``postcode``, ``certificateNumber``, ``currentEnergyEfficiencyBand``,
  ``registrationDate``, ``postTown``, ``council``. NO floor area on search.
* CERT    ``/certificate?certificate_number=<CN>`` → ``{"data": {...}}`` with
  ``total_floor_area`` (SQUARE METRES, e.g. 40), ``habitable_room_count``,
  ``address_line_1``, ``postcode``. (Confirmed: 1000-2000-3000-4000-5001 →
  total_floor_area 40, "104b, Sample Road", N1 9ZY.)

Rate limit 6000 req / 5 min — we pace ~0.25s between live calls and cache every
response under ``data/epc/`` (``search_<PC>.json`` / ``cert_<CN>.json``), so the
run is idempotent and a re-run is offline-fast. ``force=True`` re-fetches;
``offline=True`` never touches the wire (cache hit or nothing) — the deterministic
test path.

m² → sqft: multiply by 10.7639.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine import paths
from gaff_engine.schemas import Comp

# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

API_BASE = "https://api.get-energy-performance-data.communities.gov.uk/api"
SEARCH_ENDPOINT = API_BASE + "/domestic/search"
CERT_ENDPOINT = API_BASE + "/certificate"

SQM_TO_SQFT = 10.7639                    # 1 m² = 10.7639 sqft (task-specified)
REQUEST_PACING_SECONDS = 0.25            # ~0.25s between live calls (limit 6000/5min)
REQUEST_TIMEOUT_SECONDS = 30
SEARCH_PAGE_SIZE = 100                   # a postcode unit rarely holds >100 certs

# On-disk cache shape version (search_*.json and cert_*.json). Mismatch reads
# as a miss; a missing field is version 1, the current shape.
CACHE_SCHEMA = 1

# Cache locations come from gaff_engine.paths, mirroring landreg: EPC_CACHE_DIR
# is the writable per-user cache every write lands in. SHIPPED_EPC_DIR exists
# only in the lab — no EPC data ships with the package (docs/data-strategy.md
# §3) — and reads skip it harmlessly when absent.
EPC_CACHE_DIR = paths.cache_dir("epc")
SHIPPED_EPC_DIR = paths.shipped_dir("epc")


# ---------------------------------------------------------------------------
# Token — read at runtime, never printed / logged / written anywhere.
# ---------------------------------------------------------------------------

def _load_token() -> str:
    """Return the EPC Bearer token, resolved by :func:`gaff_engine.paths.epc_token`.

    Order: ``$GAFF_EPC_TOKEN``, the macOS keychain, ``~/.gaff/epc_token``, then
    the development ``.secrets/epc_token``. The value is never echoed — not in
    logs, not in exceptions, not in any file this module writes. It only ever
    travels in the ``Authorization`` request header.

    The not-found error is rewritten here rather than passed through: the
    resolver's own message lists every source it tried, including the
    checkout-only ``.secrets`` path, and pointing an installed user at a lab
    path they do not have is worse than saying nothing. So the re-raise names
    only the three sources a package user can act on (``from None`` so the
    original, ``.secrets``-naming message never rides along in the traceback).
    """
    try:
        return paths.epc_token()
    except RuntimeError:
        raise RuntimeError(
            "EPC API token not found. Sources checked, in order:\n"
            + "\n".join("  - %s" % s for s in paths.epc_token_sources()[:3])
            + "\n\nSet one, for example:\n"
              "    export %s='YOUR_TOKEN'\n"
              "or store it in the keychain (macOS):\n"
              "    security add-generic-password -s %s -a \"$USER\" -w 'YOUR_TOKEN'\n"
              "Request a token at https://epc.opendatacommunities.org/."
            % (paths.ENV_EPC_TOKEN, paths.KEYCHAIN_SERVICE)
        ) from None


def _token_hint() -> str:
    """The token sources a *package user* can act on, comma-joined.

    Derived from :func:`gaff_engine.paths.epc_token_sources` so the story stays
    one list everywhere; the fourth entry (the development ``.secrets`` fallback,
    a checkout-only path) is deliberately dropped — pointing an installed user
    at a lab path they do not have is worse than saying nothing.
    """
    return ", ".join(paths.epc_token_sources()[:3])


# ---------------------------------------------------------------------------
# HTTP.
# ---------------------------------------------------------------------------

def _http_get_json(url: str) -> Any:
    """GET ``url`` with the Bearer auth headers and parse JSON.

    The token lives only in the request header, never in ``url`` — so a raised
    ``HTTPError`` (which echoes the URL) can never leak it. We still re-raise with
    a token-free message to be certain.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer %s" % _load_token(),
            "Accept": "application/json",
        },
    )
    time.sleep(REQUEST_PACING_SECONDS)               # be polite; pace under the limit
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:            # token-free re-raise
        raise RuntimeError(
            "EPC API HTTP %s for %s (check the token: %s)"
            % (exc.code, url, _token_hint())
        ) from None


# ---------------------------------------------------------------------------
# Small parse / normalise helpers (pure, deterministic, network-free).
# ---------------------------------------------------------------------------

def sqm_to_sqft(m2: Optional[float]) -> Optional[float]:
    """Convert square metres to square feet (``m² × 10.7639``); ``None``→``None``."""
    if m2 is None:
        return None
    return float(m2) * SQM_TO_SQFT


def _to_float(v: Any) -> Optional[float]:
    """Coerce an EPC numeric-ish value ("40", "40.0", 40) to float, else None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _pc_canonical(postcode: Optional[str]) -> str:
    """Uppercase, single-spaced postcode for the API param ("n1  3ny" -> "N1 9ZY")."""
    return re.sub(r"\s+", " ", (postcode or "").strip().upper())


def _pc_slug(postcode: Optional[str]) -> str:
    """Space-free, uppercase postcode key for cache filenames / equality
    ("N1 9ZY" -> "N13NY"). Used to compare a comp's postcode to an EPC row's."""
    return re.sub(r"[^A-Z0-9]+", "", (postcode or "").upper())


def _house_token(text: Optional[str]) -> Optional[str]:
    """The leading house token of an address line, lowercased, commas stripped.

    ``"104b, Sample Road" -> "104b"``; ``"137B, ..." -> "137b"``. Per the
    matching spec: strip commas, lowercase, take the first token.
    """
    s = (text or "").strip().lower()
    if not s:
        return None
    first = re.split(r"[,\s]+", s, maxsplit=1)[0]
    return first or None


def _split_num_alpha(token: Optional[str]):
    """Split a house token into ``(number:int|None, suffix:str)``.

    ``"137b" -> (137, "b")``; ``"104" -> (104, "")``; ``"rosemount" -> (None, "")``.
    Only a *leading* number (with optional trailing letters) counts as a house
    number; a purely-named token (a block name) yields ``(None, "")`` and cannot
    match a numeric Land Registry paon.
    """
    if not token:
        return (None, "")
    m = re.match(r"^(\d+)([a-z]*)", token)
    if not m:
        return (None, "")
    return (int(m.group(1)), m.group(2))


def _house_matches(paon: Optional[str], epc_address_line1: Optional[str]) -> bool:
    """Does a Land Registry paon match an EPC ``addressLine1``'s house number?

    Rule (case-insensitive, comma-stripped, first token only): the **numbers must
    be equal**, and the **letter suffix must be equal OR absent on one side**. So
    Land Registry "104" or "104B" both match EPC "104b, Sample Road", while
    "137" does not; and "137B" matches "137B, ..." exactly. When a paon carries no
    letter (the whole-number case) it matches any flat at that number — several
    such matches are later disambiguated by most-recent registrationDate.
    """
    a_num, a_suf = _split_num_alpha(_house_token(paon))
    b_num, b_suf = _split_num_alpha(_house_token(epc_address_line1))
    if a_num is None or b_num is None:
        return False
    if a_num != b_num:
        return False
    if a_suf and b_suf and a_suf != b_suf:
        return False
    return True


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    """Pull the result rows from a search payload, tolerating both shapes.

    ``{"data": {"rows": [...]}}`` (dict-with-rows) AND ``{"data": [...]}`` (bare
    list) are both observed live; a bare top-level list is handled for safety too.
    """
    if isinstance(payload, list):
        return payload
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    if isinstance(data, list):
        return data
    return []


def _norm_search_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a raw EPC search row onto the small, stable shape we cache/return."""
    return {
        "addressLine1": row.get("addressLine1"),
        "addressLine2": row.get("addressLine2"),
        "certificateNumber": row.get("certificateNumber"),
        "postcode": row.get("postcode"),
        "band": row.get("currentEnergyEfficiencyBand"),
        "registrationDate": row.get("registrationDate"),
    }


# ---------------------------------------------------------------------------
# Sale-date EPC selection + area-change integrity (pure, deterministic).
# ---------------------------------------------------------------------------

def _parse_iso(s: Optional[str]) -> Optional[date]:
    """Parse an ISO ``YYYY-MM-DD`` (leading 10 chars) to a ``date``, else None."""
    if not s:
        return None
    try:
        y, m, d = str(s)[:10].split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def _gap_years(epc_date: Optional[str], sale_date: Optional[str]) -> Optional[float]:
    """Absolute gap in years between an EPC's date and a sale date (1 dp), or None."""
    a, b = _parse_iso(epc_date), _parse_iso(sale_date)
    if a is None or b is None:
        return None
    return round(abs((b - a).days) / 365.25, 1)


def _select_by_sale_date(history: List[Dict[str, Any]],
                         sale_date: Optional[str]) -> Tuple[Dict[str, Any], bool]:
    """Pick the EPC whose area reflects the property *as configured at sale*.

    Returns ``(chosen_entry, epc_after_sale_only)``. With a ``sale_date`` we take
    the latest EPC dated **on or before** it; if none pre-dates the sale we fall
    back to the earliest available EPC and flag ``epc_after_sale_only=True`` (a
    post-sale EPC may reflect the buyer's own works, so it is lower trust). With
    no ``sale_date`` we take the newest EPC (the naive "latest EPC" behaviour).
    """
    dated = [h for h in history if h.get("epcDate")]
    if not sale_date or not dated:
        chosen = max(history, key=lambda h: h.get("epcDate") or "")
        return chosen, False
    cutoff = str(sale_date)[:10]
    pre = [h for h in dated if h["epcDate"][:10] <= cutoff]
    if pre:
        return max(pre, key=lambda h: h["epcDate"]), False
    return min(dated, key=lambda h: h["epcDate"]), True


def _detect_area_change(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Flag an address whose EPCs disagree on floor area (extension / re-measure).

    ``areaChanged`` is True when there are ≥2 EPCs and the min/max ``m²`` differ by
    **>5 m² OR >5%** — the min/max values and their EPC dates are recorded as the
    evidence of a probable extension or reconfiguration.
    """
    areas = [h["m2"] for h in history]
    lo, hi = min(areas), max(areas)
    lo_entry = min(history, key=lambda h: h["m2"])
    hi_entry = max(history, key=lambda h: h["m2"])
    changed = (len(history) >= 2
               and ((hi - lo) > 5.0 or (lo > 0 and (hi - lo) / lo > 0.05)))
    return {
        "areaChanged": bool(changed),
        "minM2": lo, "maxM2": hi,
        "minDate": lo_entry.get("epcDate"), "maxDate": hi_entry.get("epcDate"),
    }


# Marketing-vs-EPC tolerance for the SUBJECT's floor area (sqft basis flag).
# Marketing sqft and an EPC total_floor_area are measured to different
# conventions (gross internal vs the EPC surveyor's read), so some daylight is
# normal; 12.5% (mid of the sensible 10-15% window) separates convention noise
# from a real disagreement — an extension the EPC predates, a floorplan that
# counts eaves, or a plain typo. Sits here beside the comp-side >5%/>5m²
# area-change rule so all floor-area-integrity thresholds live in one module.
SQFT_BASIS_TOLERANCE_PCT = 12.5


def sqft_basis_check(stated_sqft: Optional[float], epc_sqft: Optional[float],
                     tolerance_pct: float = SQFT_BASIS_TOLERANCE_PCT
                     ) -> Optional[Dict[str, Any]]:
    """Compare a subject's stated/marketing sqft against its EPC-derived area.

    The subject-side sibling of :func:`_detect_area_change` (which flags comps
    whose EPC history disagrees with itself). The failure mode this guards: a
    £/sqft verdict that looks rigorous while its numerator (comp £/sqft, EPC
    areas) and its subject denominator (marketing sqft) measure different
    things. Returns ``None`` when either figure is missing or non-positive —
    no conflict can be asserted from one number — else
    ``{statedSqft, epcSqft, diffPct, conflict}`` where ``diffPct`` is the
    absolute disagreement relative to the EPC area (the certified reference)
    and ``conflict`` is True beyond ``tolerance_pct``.

    Which figure the engine prices on, and why: the STATED sqft. It reflects
    the property as currently marketed (the floorplan the buyer sees), where
    the EPC area may predate an extension or reconfiguration. The flag exists
    so that choice is visible and dents confidence when the two disagree,
    rather than being silent.
    """
    if stated_sqft is None or epc_sqft is None:
        return None
    try:
        stated, epc_area = float(stated_sqft), float(epc_sqft)
    except (TypeError, ValueError):
        return None
    if stated <= 0 or epc_area <= 0:
        return None
    diff_pct = round(abs(stated - epc_area) / epc_area * 100.0, 1)
    return {
        "statedSqft": stated,
        "epcSqft": epc_area,
        "diffPct": diff_pct,
        "conflict": diff_pct > float(tolerance_pct),
    }


def _area_confidence(gap_years: Optional[float], area_changed: bool,
                     after_sale_only: bool) -> str:
    """Trust tier for a sale-matched area.

    ``low``  — the area is unreliable: gap >8y (stale), or the address was
               re-measured/extended (``areaChanged``), or the only usable EPC
               post-dates the sale (``epcAfterSaleOnly``), or the gap is unknown.
               A lone EPC that is *distant* falls here via the gap / after-sale rules.
    ``high`` — gap ≤3y and no area-change / after-sale red flags.
    ``medium`` — everything else (gap 3–8y, no red flags).
    """
    if area_changed or after_sale_only or gap_years is None or gap_years > 8:
        return "low"
    if gap_years <= 3:
        return "high"
    return "medium"


# ---------------------------------------------------------------------------
# Cache paths.
# ---------------------------------------------------------------------------

def _search_name(postcode: str) -> str:
    return "search_%s.json" % _pc_slug(postcode)


def _cert_name(cert_number: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9-]+", "_", cert_number or "unknown")
    return "cert_%s.json" % safe


def _search_cache_path(postcode: str) -> str:
    """Where a fetched postcode search is written: always the user cache."""
    return os.path.join(EPC_CACHE_DIR, _search_name(postcode))


def _cert_cache_path(cert_number: str) -> str:
    """Where a fetched certificate is written: always the user cache."""
    return os.path.join(EPC_CACHE_DIR, _cert_name(cert_number))


def _read_cache_json(name: str) -> Optional[Dict[str, Any]]:
    """First VALID cache file called ``name``: user cache, then shipped.

    Validated per candidate — corrupt, non-dict or stale-shaped files are
    skipped so a bad user-tier file cannot mask a valid shipped one. Both
    roots are read from the module globals at call time so a test can
    redirect either tier.
    """
    for root in (EPC_CACHE_DIR, SHIPPED_EPC_DIR):
        candidate = os.path.join(root, name)
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        if isinstance(doc, dict) and doc.get("cacheSchema", 1) == CACHE_SCHEMA:
            return doc
    return None


def _write_json(path: str, obj: Any) -> None:
    if not isinstance(obj, dict):
        raise TypeError("EPC cache entries are dict envelopes; got %s"
                        % type(obj).__name__)
    obj.setdefault("cacheSchema", CACHE_SCHEMA)
    os.makedirs(EPC_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# search_postcode.
# ---------------------------------------------------------------------------

def search_postcode(postcode: str, page_size: int = SEARCH_PAGE_SIZE,
                    force: bool = False, offline: bool = False) -> List[Dict[str, Any]]:
    """Return the EPC domestic certificates for one postcode, cached on disk.

    Each element is the small stable shape ``{addressLine1, addressLine2,
    certificateNumber, postcode, band, registrationDate}`` (no floor area — that
    only lives on the certificate). Idempotent: the first live call fetches and
    writes ``data/epc/search_<PC>.json``; later calls read the cache.

    ``force`` re-fetches and overwrites; ``offline`` never hits the wire (returns
    the cache if present, else ``[]``).
    """
    path = _search_cache_path(postcode)
    env = None if force else _read_cache_json(_search_name(postcode))
    if env is not None:
        return env.get("rows", [])
    if offline:
        return []

    url = "%s?%s" % (SEARCH_ENDPOINT, urllib.parse.urlencode(
        {"postcode": _pc_canonical(postcode), "page_size": page_size}))
    payload = _http_get_json(url)
    rows = [_norm_search_row(r) for r in _extract_rows(payload)]
    _write_json(path, {
        "postcode": _pc_canonical(postcode),
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(rows),
        "rows": rows,
    })
    return rows


# ---------------------------------------------------------------------------
# fetch_certificate.
# ---------------------------------------------------------------------------

def fetch_certificate(cert_number: str, registration_date: Optional[str] = None,
                      force: bool = False,
                      offline: bool = False) -> Optional[Dict[str, Any]]:
    """Return one EPC certificate (cached), normalised to the fields we use.

    Shape: ``{certificateNumber, floor_area_m2, habitable_rooms, address,
    postcode, registrationDate}``. ``floor_area_m2`` is the certified
    ``total_floor_area`` in square metres; the certificate endpoint carries the
    area but not the lodgement date, so the date is joined in from the search row
    (``registration_date``) and persisted alongside the cert — a cached cert that
    predates this join is backfilled in place (no network). Idempotent cache at
    ``data/epc/cert_<CN>.json``. ``offline`` returns the cached cert if present,
    else ``None`` (never fetches).
    """
    if not cert_number:
        return None
    path = _cert_cache_path(cert_number)
    cert = None if force else _read_cache_json(_cert_name(cert_number))
    if cert is not None:
        if registration_date and not cert.get("registrationDate"):
            cert["registrationDate"] = registration_date       # backfill, no network
            _write_json(path, cert)        # backfill always lands in the user tier
        return cert
    # No valid cached cert (missing, corrupt, or stale-shaped): fall through.
    if offline:
        return None

    url = "%s?%s" % (CERT_ENDPOINT, urllib.parse.urlencode(
        {"certificate_number": cert_number}))
    payload = _http_get_json(url)
    data = (payload or {}).get("data") or {}
    cert = {
        "certificateNumber": cert_number,
        "floor_area_m2": _to_float(data.get("total_floor_area")),
        "habitable_rooms": _to_int(data.get("habitable_room_count")),
        "address": data.get("address_line_1"),
        "postcode": data.get("postcode"),
        "registrationDate": registration_date,                 # joined from the search row
    }
    _write_json(path, cert)
    return cert


# ---------------------------------------------------------------------------
# Match a comp's address to its EPC history → sale-date-matched floor area.
# ---------------------------------------------------------------------------

def _address_epc_history(paon: Optional[str], postcode: Optional[str],
                         force: bool = False,
                         offline: bool = False) -> List[Dict[str, Any]]:
    """The full EPC history at a house identifier: ``[{epcDate, m2, certNumber,
    band, address}, ...]`` (only certificates with a usable floor area), newest
    first. Empty when nothing matches. Fetches + caches each certificate's area."""
    if not paon or not postcode:
        return []
    rows = search_postcode(postcode, force=force, offline=offline)
    want_pc = _pc_slug(postcode)
    matched = [r for r in rows
               if _pc_slug(r.get("postcode")) == want_pc
               and _house_matches(paon, r.get("addressLine1"))]

    history: List[Dict[str, Any]] = []
    for r in matched:
        cert = fetch_certificate(r.get("certificateNumber"),
                                 registration_date=r.get("registrationDate"),
                                 force=force, offline=offline)
        if not cert:
            continue
        m2 = cert.get("floor_area_m2")
        if not m2 or m2 <= 0:
            continue
        history.append({
            "epcDate": r.get("registrationDate"),
            "m2": float(m2),
            "certNumber": r.get("certificateNumber"),
            "band": r.get("band"),
            "address": cert.get("address") or r.get("addressLine1"),
        })
    history.sort(key=lambda h: h.get("epcDate") or "", reverse=True)
    return history


def _lookup(paon: Optional[str], street: Optional[str], postcode: Optional[str],
            sale_date: Optional[str] = None, force: bool = False,
            offline: bool = False) -> Optional[Dict[str, Any]]:
    """Resolve a house identifier to a *sale-date-matched* EPC floor area + integrity.

    Gathers the full EPC history at the address, selects the EPC as configured at
    ``sale_date`` (latest on/before the sale; else the earliest, flagged
    ``epcAfterSaleOnly``), flags an ``areaChanged`` address (EPCs disagreeing on
    area), and grades ``areaConfidence``. Returns the provenance dict — or ``None``
    if nothing matches or no matched cert has a usable floor area. With no
    ``sale_date`` the newest EPC is used (the naive "latest EPC" baseline).
    """
    history = _address_epc_history(paon, postcode, force=force, offline=offline)
    if not history:
        return None

    selected, after_sale_only = _select_by_sale_date(history, sale_date)
    change = _detect_area_change(history)
    gap = _gap_years(selected.get("epcDate"), sale_date)
    confidence = _area_confidence(gap, change["areaChanged"], after_sale_only)

    detail = None
    if change["areaChanged"]:
        detail = {"minM2": change["minM2"], "maxM2": change["maxM2"],
                  "minDate": change["minDate"], "maxDate": change["maxDate"],
                  "epcCount": len(history)}
    return {
        "sqft": round(sqm_to_sqft(selected["m2"]), 1),
        "floor_area_m2": selected["m2"],
        "epcCertNumber": selected["certNumber"],
        "epcDate": selected.get("epcDate"),
        "epcSaleGapYears": gap,
        "areaChanged": change["areaChanged"],
        "epcAfterSaleOnly": after_sale_only,
        "areaConfidence": confidence,
        "areaChangeDetail": detail,
        "historyCount": len(history),
        "epcAddress": selected.get("address"),
        "band": selected.get("band"),
    }


def floor_area_sqft_for(paon: Optional[str], street: Optional[str],
                        postcode: Optional[str], sale_date: Optional[str] = None,
                        force: bool = False, offline: bool = False) -> Optional[float]:
    """Floor area in **sqft** for a house identifier via the EPC register, or None.

    With ``sale_date`` the area is the one configured at sale (see :func:`_lookup`);
    without it, the newest EPC (the naive "latest EPC" number). Returns ``None``
    when no certificate confidently matches (a named block with no house number, an
    unregistered property, a cert with no floor area, or — ``offline=True`` — a
    cache miss).
    """
    found = _lookup(paon, street, postcode, sale_date=sale_date,
                    force=force, offline=offline)
    return found["sqft"] if found else None


# ---------------------------------------------------------------------------
# enrich_comps.
# ---------------------------------------------------------------------------

def enrich_comps(comps: List[Comp], force: bool = False,
                 offline: bool = False) -> List[Comp]:
    """Return copies of ``comps`` with ``pricePerSqft`` + EPC provenance filled in.

    For each comp we match its ``address`` (paon + postcode) to the EPC history at
    that address and take the floor area *as configured at the comp's sale date*
    (``comp.date``). On a confident match we stamp ``pricePerSqft = price / sqft``
    and the provenance it rests on: ``sqft``, ``epcCertNumber``, ``epcDate``,
    ``epcSaleGapYears``, ``areaChanged``, ``epcAfterSaleOnly``, ``areaConfidence``
    and (when the area changed) ``epcAreaChange``. Comps with no match keep every
    one of those fields ``None``. The inputs are never mutated
    (``dataclasses.replace`` returns copies), so re-parsing the raw Land Registry
    cache still yields the ``None`` £/sqft the U9 guard test expects.

    ``landreg.parse_comp`` stamps the PPD ``transactionCategory`` as an
    INSTANCE attribute (the schema field is owned elsewhere), which
    ``dataclasses.replace`` would silently drop — so it is copied across here,
    or the Value scorer could never exclude non-standard sales from an
    enriched set.
    """
    out: List[Comp] = []
    for comp in comps:
        addr = comp.address
        found = None
        if addr is not None:
            found = _lookup(addr.paon, addr.street, addr.postcode,
                            sale_date=comp.date, force=force, offline=offline)
        if found and comp.price:
            sqft = found["sqft"]
            ppsf = round(comp.price / sqft, 1) if sqft else None
            enriched = replace(
                comp,
                pricePerSqft=ppsf, sqft=sqft, epcCertNumber=found["epcCertNumber"],
                epcDate=found["epcDate"], epcSaleGapYears=found["epcSaleGapYears"],
                areaChanged=found["areaChanged"],
                epcAfterSaleOnly=found["epcAfterSaleOnly"],
                areaConfidence=found["areaConfidence"],
                epcAreaChange=found["areaChangeDetail"])
        else:
            enriched = replace(
                comp,
                pricePerSqft=None, sqft=None, epcCertNumber=None, epcDate=None,
                epcSaleGapYears=None, areaChanged=None, epcAfterSaleOnly=None,
                areaConfidence=None, epcAreaChange=None)
        category = getattr(comp, "transactionCategory", None)
        if category is not None:
            enriched.transactionCategory = category
        out.append(enriched)
    return out


__all__ = [
    "API_BASE", "SEARCH_ENDPOINT", "CERT_ENDPOINT", "SQM_TO_SQFT",
    "SQFT_BASIS_TOLERANCE_PCT",
    "EPC_CACHE_DIR", "SHIPPED_EPC_DIR",
    "sqm_to_sqft", "sqft_basis_check", "search_postcode", "fetch_certificate",
    "floor_area_sqft_for", "enrich_comps",
]
