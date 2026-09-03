"""Repeat-sales flip analysis, productised from the Leamington research (T10).

The question this answers: when a home resells after a short hold, how much of
the uplift did the owner add, and how much was just the market moving under
them? For every address sold twice with a 1.5-5.5 year gap, the actual price
change is compared against the UK HPI move for that district and property type
over the same months — the **excess over market** is the closest honest proxy
for value added.

Pipeline (each stage pure over the previous one's output):

    pull_town()          town → raw PPD rows          network, paced, cached
    pair_repeat_sales()  rows → resale pairs          pure
    flip_records()       pairs → uplift/market/excess HPI-adjusted (cache-first)
    summarise()          records → the headline stats pure

``build_town()`` composes the four and writes ``flips/<town-slug>.json`` to
the user cache; ``load_flips()`` reads user cache then shipped data, so the
Leamington dataset that ships keeps working with zero network.

Scale guard (T8): a whole town of Price Paid rows is the one engine operation
whose size is unknown before it runs (the research pull was 41,713 records,
~75MB resident; a city is 5-10x that). ``estimate_exceeds()`` probes the page
AT the cap — one request — so an oversized town is refused loudly BEFORE the
bulk download starts, with the cap, the reason and the remedies in the error.
Passing a bigger ``max_records`` explicitly is the override; nothing silently
truncates.

All Land Registry + UK HPI data: Open Government Licence v3.0.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from gaff_engine import hpi, paths
from gaff_engine.landreg import ENDPOINT, USER_AGENT, _slug

PAGE_SIZE = 200
REQUEST_PACING_SECONDS = 1.1              # whole-town pulls are the heavy caller
REQUEST_TIMEOUT_SECONDS = 60

#: Default ceiling on records per town pull. The Leamington research pull
#: (four towns) was ~41,700 records / ~75MB resident; one town under this cap
#: stays comfortably inside a laptop that is also running an LLM host.
MAX_RECORDS = 25_000

#: Resale-pair window in years: under 1.5 is mostly remortgage/plot noise,
#: over 5.5 stops being a "flip" and starts being a different market.
MIN_GAP_YEARS, MAX_GAP_YEARS = 1.5, 5.5

#: Uplift outside this band is a data artefact (plot splits, share transfers),
#: not a resale — the research filter, kept verbatim.
MAX_UPLIFT_PCT, MIN_UPLIFT_PCT = 200.0, -50.0

_HPI_FIELD = {"detached": "averagePriceDetached",
              "semi-detached": "averagePriceSemiDetached",
              "terraced": "averagePriceTerraced",
              "flat-maisonette": "averagePriceFlatMaisonette"}


class TownTooLargeError(RuntimeError):
    """A town pull would exceed the record cap; refused before downloading."""


class FlipsFetchError(RuntimeError):
    """The Land Registry endpoint failed mid-pull; carries town + page context.

    A deliberate hard-fail (unlike landreg's per-street fail-soft): a bulk
    pull is an explicit operation whose partial result would silently skew
    every statistic computed from it."""


# ---------------------------------------------------------------------------
# Dates. PPD's linked-data API serves RFC-2822-style dates ("Fri, 20 Feb
# 2026"); cached research data may carry ISO. Accept both, fail loudly on
# neither — a silently mis-parsed date corrupts every gap calculation.
# ---------------------------------------------------------------------------

def parse_ppd_date(raw: Any) -> datetime:
    text = str(raw or "").strip()
    m = re.match(r"[A-Za-z]{3},\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", text)
    if m:
        return datetime.strptime("%s %s %s" % m.groups(), "%d %b %Y")
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    raise ValueError("unparseable Land Registry date: %r" % raw)


def _month(raw: Any) -> str:
    t = parse_ppd_date(raw)
    return "%04d-%02d" % (t.year, t.month)


# ---------------------------------------------------------------------------
# Stage 1 — pull a town (network, paced, cached, capped).
# ---------------------------------------------------------------------------

def _page_url(town: str, since: str, page: int, page_size: int = PAGE_SIZE) -> str:
    params = {"propertyAddress.town": town.upper(), "min-transactionDate": since,
              "_pageSize": page_size, "_page": page}
    return "%s?%s" % (ENDPOINT, urllib.parse.urlencode(params))


def _get_json(url: str, *, context: str) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, http.client.HTTPException,
            ValueError, OSError) as exc:
        raise FlipsFetchError(
            "Land Registry fetch failed during %s (%s: %s)"
            % (context, type(exc).__name__, exc)) from exc


def _label(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        node = node[0]
    if isinstance(node, dict):
        return node.get("_value") or _label(node.get("prefLabel"))
    return node


def _row(item: Dict[str, Any]) -> Dict[str, Any]:
    a = item.get("propertyAddress") or {}
    ptype = item.get("propertyType")
    return {
        "price": item.get("pricePaid"),
        "date": item.get("transactionDate"),
        "newBuild": item.get("newBuild"),
        "estate": _label((item.get("estateType") or {}).get("prefLabel")),
        "type": _label(ptype.get("prefLabel")) if isinstance(ptype, dict) else None,
        "paon": a.get("paon"), "saon": a.get("saon"), "street": a.get("street"),
        "locality": a.get("locality"), "town": a.get("town"),
        "district": a.get("district"), "postcode": a.get("postcode"),
    }


def estimate_exceeds(town: str, since: str, max_records: int = MAX_RECORDS) -> bool:
    """One-request probe: does the town exceed ``max_records`` since ``since``?

    The PPD API exposes no total count, so this asks for the page that sits AT
    the cap; any items on it mean the town is over. One paced request, before
    a single bulk page is downloaded.
    """
    # Quantised UP to whole pages, so a non-multiple cap (say 1,100) never
    # falsely refuses a town under it; the walk's exact backstop covers the
    # sub-page remainder the probe cannot see.
    cap_page = -(-max_records // PAGE_SIZE)
    time.sleep(REQUEST_PACING_SECONDS)
    payload = _get_json(_page_url(town, since, cap_page),
                        context="the size probe for %s" % town.upper())
    items = (payload.get("result") or {}).get("items") or [] \
        if isinstance(payload, dict) else []
    return bool(items)


def pull_town(town: str, since: str = "2015-01-01", *,
              max_records: int = MAX_RECORDS,
              progress: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """Every PPD row for ``town`` since ``since`` — refused loudly if too big.

    Raises :class:`TownTooLargeError` when the probe (or the walk itself, as
    the belt-and-braces backstop) finds more than ``max_records`` rows. The
    error names the cap and the remedies; raising ``max_records`` explicitly
    is the caller's informed override. Nothing silently truncates.

    Raises :class:`FlipsFetchError` on an upstream failure mid-pull — a
    deliberate hard-fail, because a partial town would silently skew every
    statistic computed from it.
    """
    say = progress or (lambda _msg: None)
    say("checking the size of %s first" % town.upper())
    if estimate_exceeds(town, since, max_records):
        raise TownTooLargeError(
            "%s has more than %d Price Paid records since %s — refusing the "
            "pull rather than filling memory. Narrow it (later `since`), or "
            "pass max_records explicitly if you really want a town this size."
            % (town.upper(), max_records, since))

    rows: List[Dict[str, Any]] = []
    page = 0
    while True:
        time.sleep(REQUEST_PACING_SECONDS)
        payload = _get_json(_page_url(town, since, page),
                            context="page %d of %s" % (page, town.upper()))
        items = (payload.get("result") or {}).get("items") or []
        if not items:
            break
        rows.extend(_row(it) for it in items)
        if len(rows) > max_records:                    # backstop, post-probe
            raise TownTooLargeError(
                "%s exceeded %d records mid-pull — stopping rather than "
                "filling memory." % (town.upper(), max_records))
        say("%s: %d records so far" % (town.upper(), len(rows)))
        page += 1
    say("%s: %d records total" % (town.upper(), len(rows)))
    return rows


# ---------------------------------------------------------------------------
# Stage 2 — pair repeat sales (pure).
# ---------------------------------------------------------------------------

def pair_repeat_sales(rows: List[Dict[str, Any]], *,
                      min_gap_years: float = MIN_GAP_YEARS,
                      max_gap_years: float = MAX_GAP_YEARS) -> List[Dict[str, Any]]:
    """Same address sold more than once → consecutive-sale pairs in the
    flip window. Address identity is (paon, saon, street, postcode); rows
    missing price, date or postcode cannot pair and are skipped.

    The window is a parameter (defaults keep the shipped 1.5-5.5yr research
    behaviour byte-for-byte) so :func:`sensitivity` can re-pair the same rows
    over alternative windows instead of treating one window as truth."""
    if not (0 <= min_gap_years < max_gap_years):
        raise ValueError("need 0 <= min_gap_years < max_gap_years, got %r-%r"
                         % (min_gap_years, max_gap_years))
    by_addr: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        if not (r.get("price") and r.get("date") and r.get("postcode")):
            continue

        key = (str(r.get("paon") or "").upper(), str(r.get("saon") or "").upper(),
               str(r.get("street") or "").upper(), str(r.get("postcode")).upper())
        by_addr.setdefault(key, []).append(r)

    pairs = []
    for sales in by_addr.values():
        if len(sales) < 2:
            continue
        try:
            sales.sort(key=lambda r: parse_ppd_date(r["date"]))
        except ValueError:
            continue                                    # a bad date poisons the address
        for a, b in zip(sales, sales[1:]):
            if a.get("newBuild"):
                # The research excluded new-build BUYS: a developer's first
                # sale is not a flip, and including it shifts the headline
                # uplift. Sell-side new-builds (rare relodgements) still pair.
                continue
            yrs = (parse_ppd_date(b["date"]) - parse_ppd_date(a["date"])).days / 365.25
            if min_gap_years <= yrs <= max_gap_years:
                pairs.append({"a": a, "b": b, "yrs": yrs})
    return pairs


# ---------------------------------------------------------------------------
# Stage 3 — HPI-adjust each pair (cache-first; offline-able).
# ---------------------------------------------------------------------------

def flip_records(pairs: List[Dict[str, Any]], *, offline: bool = False,
                 progress: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """Pairs → flip records with uplift, market move and excess over market.

    The market comparator is the UK HPI series for the pair's district and
    property type between the two sale months. Pairs whose district has no HPI
    region mapping, whose months are missing, or whose uplift is outside the
    artefact band are skipped — a record either carries a real market
    comparison or it does not exist."""
    say = progress or (lambda _msg: None)
    out, skipped = [], 0
    for i, p in enumerate(pairs):
        a, b = p["a"], p["b"]
        region = _hpi_region(a.get("district") or a.get("town"))
        field = _HPI_FIELD.get(a.get("type") or "")
        if not region or not field:
            skipped += 1
            continue
        ma, mb = _month(a["date"]), _month(b["date"])
        va = _hpi_value(region, field, ma, offline)
        vb = _hpi_value(region, field, mb, offline)
        if not va or not vb:
            skipped += 1
            continue
        actual, market = b["price"] / a["price"], vb / va
        uplift = (actual - 1) * 100
        if uplift > MAX_UPLIFT_PCT or uplift < MIN_UPLIFT_PCT:
            skipped += 1
            continue
        out.append({"street": a.get("street"), "paon": a.get("paon"),
                    "postcode": a.get("postcode"), "locality": a.get("locality"),
                    "town": a.get("town"), "district": a.get("district"),
                    "type": a.get("type"), "buy": a["price"], "sell": b["price"],
                    "buy_date": ma, "sell_date": mb, "yrs": round(p["yrs"], 1),
                    "uplift_pct": round(uplift, 1),
                    "market_pct": round((market - 1) * 100, 1),
                    "excess_pct": round((actual / market - 1) * 100, 1),
                    "gain": b["price"] - a["price"]})
        if i and i % 500 == 0:
            say("%d/%d pairs adjusted" % (i, len(pairs)))
    if skipped:
        say("%d pair(s) skipped (no HPI mapping/month, or artefact-band uplift)"
            % skipped)
    return out


def _hpi_region(district: Any) -> Optional[str]:
    """District → UK HPI region slug: lower-cased, hyphenated ("STRATFORD-ON-
    AVON" → "stratford-on-avon", "WARWICK" → "warwick").

    Deliberately NOT hpi.region_for, whose unknown-area fallback is the
    London-wide series — fine for a London listing engine, silently wrong as
    a market comparator for a Warwickshire resale. This mapping is
    fail-closed instead: a slug the HPI endpoint does not recognise fetches
    nothing and the pair is skipped, so a record never carries the wrong
    market."""
    if not district:
        return None
    return re.sub(r"[^a-z0-9]+", "-", str(district).strip().lower()).strip("-") or None


def _hpi_value(region: str, field: str, month: str, offline: bool) -> Optional[float]:
    row = hpi.fetch_month(region, month, offline=offline)
    if not isinstance(row, dict):
        return None
    v = row.get(field) or row.get("averagePrice")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Stage 4 — the headline stats (pure; the shape the surfaces return).
# ---------------------------------------------------------------------------

def summarise(records: List[Dict[str, Any]], town: str) -> Dict[str, Any]:
    """The per-town aggregation both surfaces serve, in their exact shape."""
    rows = [r for r in records if (r.get("town") or "").upper() == town.upper()]
    if not rows:
        return {"error": "no flip records for %r" % town,
                "towns_available": sorted({r["town"] for r in records if r.get("town")})}
    upl = sorted(r["uplift_pct"] for r in rows if r.get("uplift_pct") is not None)
    exc = sorted(r["excess_pct"] for r in rows if r.get("excess_pct") is not None)
    mkt = [r["market_pct"] for r in rows if r.get("market_pct") is not None]
    gains = sorted(r["gain"] for r in rows if r.get("gain") is not None)
    return {"town": town.upper(), "resales_analysed": len(rows),
            "median_uplift_pct": round(statistics.median(upl), 1) if upl else None,
            "median_market_move_pct": round(statistics.median(mkt), 1) if mkt else None,
            "median_excess_over_market_pct": round(statistics.median(exc), 1) if exc else None,
            "beat_the_market_share_pct": round(100 * sum(1 for e in exc if e > 0) / len(exc), 1) if exc else None,
            "median_cash_gain": int(statistics.median(gains)) if gains else None,
            "source": "HM Land Registry Price Paid + UK HPI (Open Government Licence v3.0)"}


# ---------------------------------------------------------------------------
# Window sensitivity. The 1.5-5.5yr flip window is a research choice, not a
# law of nature; if median uplift/excess moves materially when the window
# moves, the honest output is the instability itself, not one window's
# number presented as truth.
# ---------------------------------------------------------------------------

#: The alternative windows the outside review asked to be checked, in years.
SENSITIVITY_WINDOWS = ((1.0, 3.0), (3.0, 7.0), (5.0, 10.0))

#: Below this |median excess| (percentage points) a sign is noise, not a
#: finding — a flip from +0.2 to −0.3 is stability, not instability.
_SIGN_NOISE_FLOOR_PP = 0.5

#: Slack per window edge before the truncation note fires. Records rarely sit
#: exactly on a bound (``yrs`` is rounded to 0.1 and edges are sparse), so a
#: quarter-year of shortfall is coverage noise; more than that means the
#: input pairing genuinely does not reach the requested bound.
_TRUNCATION_TOLERANCE_YRS = 0.25


def sensitivity(records_or_pairs: List[Dict[str, Any]],
                windows: Any = SENSITIVITY_WINDOWS, *,
                min_n: int = 30, offline: bool = True) -> Dict[str, Any]:
    """Median uplift/excess per holding-gap window, plus a stability verdict.

    Accepts either flip *records* (from :func:`flip_records` /
    :func:`load_flips`) or raw *pairs* (from :func:`pair_repeat_sales`, which
    now takes the window as a parameter) — pairs are HPI-adjusted here first,
    cache-first, so the shipped data works with zero network. Windows are
    inclusive at both ends and may overlap; a record's ``yrs`` decides its
    window(s).

    "Materially", defined: between any two windows with ``n >= min_n``,
    either (a) the median excess changes SIGN with both magnitudes at or
    above ``_SIGN_NOISE_FLOOR_PP`` (a flip around zero within noise is not
    instability), or
    (b) the medians differ by at least 1.0pp AND the larger magnitude is
    more than 2x the smaller (a clear value dropping to noise level counts —
    excess attenuating to ~0 at longer holds is precisely the finding this
    exists to surface). Either sets ``stable`` False and adds a note.

    Honesty guard: ANY window whose observed years fall short of a requested
    bound by more than :data:`_TRUNCATION_TOLERANCE_YRS` is noted, naming the
    observed vs requested bounds — a truncated window silently masquerading
    as the full one is exactly the error this function exists to prevent, and
    a 25% shortfall (the shipped pairing's 1.5yr floor inside a 1-3yr window)
    misleads just as surely as a 50% one. The remedy in the note is a
    re-pair over RAW rows: the shipped flips cache holds already-paired
    records, so a full-window re-pair needs a fresh :func:`pull_town` first.
    """
    items = list(records_or_pairs)
    if items and isinstance(items[0], dict) and "a" in items[0] and "b" in items[0]:
        records = flip_records(items, offline=offline)
    else:
        records = items

    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    for lo, hi in windows:
        rows = [r for r in records
                if r.get("yrs") is not None and lo <= r["yrs"] <= hi
                and r.get("uplift_pct") is not None and r.get("excess_pct") is not None]
        entry: Dict[str, Any] = {"window_yrs": [lo, hi], "n": len(rows)}
        if rows:
            yrs = [r["yrs"] for r in rows]
            entry["yrs_observed"] = [min(yrs), max(yrs)]
            entry["median_uplift_pct"] = round(
                statistics.median(r["uplift_pct"] for r in rows), 1)
            entry["median_excess_over_market_pct"] = round(
                statistics.median(r["excess_pct"] for r in rows), 1)
            tol = _TRUNCATION_TOLERANCE_YRS
            if min(yrs) > lo + tol or max(yrs) < hi - tol:
                notes.append(
                    "window %g-%gyr only observed yrs %g-%g — the input pairing "
                    "does not cover the requested window, so this median is "
                    "really a %g-%gyr number; for the full window re-pair with "
                    "pair_repeat_sales(rows, min_gap_years=%g, max_gap_years=%g) "
                    "over RAW PPD rows (the flips cache holds already-paired "
                    "records, so pull_town(town) must fetch the rows first)."
                    % (lo, hi, min(yrs), max(yrs), min(yrs), max(yrs), lo, hi))
        out.append(entry)

    stable = True
    usable = [e for e in out if e["n"] >= min_n and
              e.get("median_excess_over_market_pct") is not None]
    floor = _SIGN_NOISE_FLOOR_PP
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            a, b = usable[i], usable[j]
            ma = a["median_excess_over_market_pct"]
            mb = b["median_excess_over_market_pct"]
            small, big = sorted((abs(ma), abs(mb)))
            where = ("windows %g-%gyr (%+.1fpp, n=%d) and %g-%gyr (%+.1fpp, n=%d)"
                     % (a["window_yrs"][0], a["window_yrs"][1], ma, a["n"],
                        b["window_yrs"][0], b["window_yrs"][1], mb, b["n"]))
            if (ma > 0) != (mb > 0) and small >= floor:
                stable = False
                notes.append("UNSTABLE: median excess flips sign between %s." % where)
            elif abs(ma - mb) >= 1.0 and big > 2 * small:
                stable = False
                notes.append(
                    "UNSTABLE: median excess magnitude moves >2x between %s." % where)
    skipped = [e for e in out if e["n"] < min_n]
    if skipped:
        notes.append("window(s) with n < %d excluded from the stability check: %s."
                     % (min_n, ", ".join("%g-%gyr (n=%d)"
                                         % (e["window_yrs"][0], e["window_yrs"][1], e["n"])
                                         for e in skipped)))
    return {"windows": out, "stable": stable, "notes": notes,
            "materially": ("excess sign flip (both |median| >= %.1fpp), or >2x "
                           "magnitude change with medians >= 1.0pp apart, "
                           "between windows with n >= %d" % (floor, min_n))}


# ---------------------------------------------------------------------------
# Load + build.
# ---------------------------------------------------------------------------

def _record_key(r: Dict[str, Any]) -> tuple:
    """One resale's identity: the address plus both ends of the transaction.
    Deduping on THIS (never on filenames) is what lets a user's regenerated
    town coexist with the shipped multi-town file without double-counting."""
    return (str(r.get("paon") or "").upper(), str(r.get("street") or "").upper(),
            str(r.get("postcode") or "").upper(),
            r.get("buy_date"), r.get("sell_date"), r.get("buy"), r.get("sell"))


def load_flips() -> List[Dict[str, Any]]:
    """Every flip dataset available, deduplicated by RECORD identity.

    User-cache files are read before shipped ones, and the first occurrence
    of a record wins, so a regenerated town shadows the shipped copy of the
    same resales record-by-record — filenames don't matter (the shipped
    leam.json holds four towns; a rebuild writes per-town names)."""
    seen, records = set(), []
    for root in paths.read_candidates("flips"):
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as fh:
                    blob = json.load(fh)
            except (ValueError, OSError) as exc:
                warnings.warn("skipping unreadable flips file %s (%s)" % (fn, exc),
                              RuntimeWarning, stacklevel=2)
                continue
            if not isinstance(blob, list):
                continue
            for r in blob:
                if not isinstance(r, dict):
                    continue
                key = _record_key(r)
                if key in seen:
                    continue
                seen.add(key)
                records.append(r)
    return records


def build_town(town: str, since: str = "2015-01-01", *,
               max_records: int = MAX_RECORDS,
               progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """The full pipeline for one town: pull → pair → adjust → write → stats.

    Writes ``flips/<town-slug>.json`` to the user cache (never the shipped
    tier) and returns the summary. This is the regeneration path for the
    dataset the lab previously built with one-off scripts."""
    rows = pull_town(town, since, max_records=max_records, progress=progress)
    records = flip_records(pair_repeat_sales(rows), progress=progress)
    path = paths.write_path("flips", "%s.json" % _slug(town))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)
    return summarise(records, town)


__all__ = [
    "MAX_RECORDS", "FlipsFetchError", "MIN_GAP_YEARS", "MAX_GAP_YEARS", "TownTooLargeError",
    "parse_ppd_date", "estimate_exceeds", "pull_town", "pair_repeat_sales",
    "flip_records", "summarise", "sensitivity", "SENSITIVITY_WINDOWS",
    "load_flips", "build_town",
]
