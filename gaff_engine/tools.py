#!/usr/bin/env python3
"""The shared tool layer. Both surfaces call these; neither owns them.

Design rules this file exists to enforce, all from the 28 Aug engineering review:

* **Surfaces differ in how they talk, not in what they answer.** A tool takes an
  optional ``progress`` callable and never writes to stdout itself. The CLI binds
  ``progress`` to stderr; the MCP server binds it to a no-op, because on that
  surface stdout IS the protocol and a stray print kills the session (E3).
* **The tool layer composes the engine, it never reimplements it.** ``_slug`` is
  imported from ``gaff_engine.landreg``, not copied — a copy already drifted once
  and lost its "unknown" fallback (E9).
* **One boundary handler, naming the upstream.** ``safe_call`` catches specific
  exceptions with distinct messages so a user learns whether the API was down,
  the cache was corrupt or the token was wrong (E10).
* **No silent bad data.** An unparseable date raises rather than yielding
  "2026-00-20" (E12).

This module lives INSIDE the package (BACKLOG R2): the skill folder and the
MCP server import ``gaff_engine.tools`` by name, so a pip-installed wheel is
enough to run either surface — no checkout, no parent-directory path hacks.
The CLI itself (``cli_main`` and the ``demo`` verb) also lives here, so the
``gaff`` console script and the copied skill script are the same code.

Offline: reads the caches the engine already writes. Standard library only.
"""
import json
import os
import re
import statistics
import sys
import urllib.error

from gaff_engine import paths
from gaff_engine.landreg import _listing_street, _slug  # E9: import, never copy

#: Two levels up = the checkout / assembled-tree root. Only used to locate the
#: shipped demo profile when running from a source tree; inside site-packages
#: it points somewhere harmless and those candidates simply do not exist.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


class ToolError(Exception):
    """A tool failed in a way the user should see, with the upstream named."""


class UsageError(ToolError):
    """A ToolError whose cause is the INVOCATION, not the data or the wire.

    The CLI's exit-code contract (0 answered / 1 error payload / 2 bad usage)
    needs to tell "fix your command" apart from "the data is cold": raising
    this subclass at the argument-validation sites is what lets safe_call tag
    the payload, instead of cli_main sniffing error-string prefixes. Still a
    ToolError, so every existing handler and message stays intact.
    """


def _noop(_msg):
    """Default progress sink. Tools must never print; the surface decides."""


def _iso(raw):
    """'Fri, 20 Feb 2026' -> '2026-02-20'. Raises rather than emitting a bad date."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", raw)
    if not m:
        raise ToolError("unparseable Land Registry date: %r" % raw)
    d, mon, y = m.groups()
    if mon not in _MONTHS:                        # E12: fail loudly, never "2026-00-20"
        raise ToolError("unknown month %r in Land Registry date %r" % (mon, raw))
    return "%s-%02d-%02d" % (y, _MONTHS[mon], int(d))


def safe_call(name, fn, args, progress=None):
    """Run one tool, converting every known failure into a named message.

    Returns ``(ok: bool, payload: dict)``. Both surfaces use this, so a fix to
    error behaviour lands once (E10). The final ``Exception`` net re-raises the
    traceback into the local log while handing the caller one clean line.
    """
    try:
        return True, fn(progress=progress or _noop, **args)
    except TypeError as exc:
        return False, {"error": "bad arguments for %s: %s" % (name, exc),
                       "usage": True}
    except UsageError as exc:
        return False, {"error": str(exc), "usage": True}
    except ToolError as exc:
        return False, {"error": str(exc)}
    except FileNotFoundError as exc:
        return False, {"error": "%s: a data file is missing (%s). Try warming this town first."
                                % (name, os.path.basename(str(exc.filename or "?")))}
    except PermissionError:
        return False, {"error": "%s: the cache directory is not writable." % name}
    except json.JSONDecodeError as exc:
        return False, {"error": "%s: a cache file is corrupt (%s). Delete it and retry."
                                % (name, exc.msg)}
    except urllib.error.HTTPError as exc:
        return False, {"error": "%s: the upstream API returned HTTP %s." % (name, exc.code)}
    except urllib.error.URLError as exc:
        return False, {"error": "%s: could not reach the upstream API (%s)." % (name, exc.reason)}
    except KeyError as exc:
        return False, {"error": "%s: unexpected data shape, missing %s." % (name, exc)}
    except Exception as exc:                       # noqa: BLE001 - last net, named + logged
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False, {"error": "%s failed unexpectedly: %s: %s"
                                % (name, type(exc).__name__, exc)}


def price_check(street: str, town: str = "LONDON", progress=None):
    """Recent Land Registry sales on one street in one town, from the cache."""
    progress = progress or _noop
    progress("looking up %s, %s" % (street, town))
    # Resolved through gaff_engine.paths so the surfaces read the same two-tier
    # cache the engine does: the user's own fetches first, then the warm cache
    # that ships with the package.
    path = paths.read_path("comps", _slug(town), "%s.json" % _slug(street))
    if path is None:
        # Truthful cold-data errors (2a): a cold STREET in a warm town must not
        # say "warm this town" — the town IS warm. Name what is actually
        # missing, and what IS cached, so the dead end becomes a next step.
        towns = _comps_towns()
        cached = towns.get(_slug(town))
        if cached:
            return {"error": "%r is warmed, but %r is not cached there yet"
                             % (town, street),
                    "hint": "warm street=%r town=%r to fetch it (one live Land "
                            "Registry call), or ask about a cached street"
                            % (street, town),
                    "town_warmed": True,
                    "streets_cached": sorted(cached)[:8]}
        return {"error": "no cached data for %r in %r" % (street, town),
                "towns_warmed": sorted(towns),
                "hint": "warm this town first (warm street=%r town=%r), "
                        "then ask again" % (street, town)}
    env = json.load(open(path))
    sales = []
    for it in env["items"]:
        amt, iso = it.get("pricePaid"), _iso(it.get("transactionDate"))
        if amt and iso:
            ptype = it.get("propertyType")
            sales.append({"price": amt, "date": iso,
                          "type": ptype.get("prefLabel", [{}])[0].get("_value")
                          if isinstance(ptype, dict) else None})
    sales.sort(key=lambda s: s["date"], reverse=True)
    prices = [s["price"] for s in sales]
    progress("%d sales found" % len(sales))
    return {"street": env["street"], "town": env["town"], "sales_found": len(sales),
            "median_price": int(statistics.median(prices)) if prices else None,
            "most_recent": sales[:5],
            "source": "HM Land Registry Price Paid (Open Government Licence v3.0)",
            "fetchedAt": env.get("fetchedAt")}


def flip_stats(town: str, progress=None):
    """Repeat-sales uplift for one town: what resellers achieved vs the market.

    Thin over the engine (gaff_engine.flips owns the analysis — T10); this
    layer only loads, reports absence, and passes progress through."""
    progress = progress or _noop
    from gaff_engine import flips as _flips
    progress("loading repeat-sales records")
    records = _flips.load_flips()
    if not records:
        return {"error": "no repeat-sales data available",
                "hint": "the flips dataset is missing from both the user cache "
                        "and the shipped data"}
    progress("analysing %d records" % len(records))
    summary = _flips.summarise(records, town)
    if "error" in summary:
        # One error vocabulary across tools (2a): every soft error carries a
        # hint naming coverage and the next step, not just the refusal.
        summary.setdefault(
            "hint", "shipped flips coverage is %s; warm flips_town=%r to build "
                    "another town (large towns are refused by the record cap)"
                    % (", ".join(summary.get("towns_available") or []) or "empty",
                       town))
    return summary


# ---------------------------------------------------------------------------
# Listing ingestion + composed verdicts (R1): the freeform adapter built as
# "the MCP path", finally reachable from a surface. Shared plumbing first.
# ---------------------------------------------------------------------------

#: The one honest data-constraint line every tool description carries (2b):
#: MCP hosts never see SKILL.md's Limits section, so the constraint travels
#: with the tool itself.
# Deliberately names NO towns: what is warmed changes (users warm streets, the
# shipped cache grows), and a hardcoded list in a tool description goes stale
# silently. The coverage tool computes the real answer.
_COVERAGE = ("Cache-first: answers come from local data only, never a live "
             "portal. Call the coverage tool for what is warmed right now; "
             "the warm tool adds more.")


def _as_dict(value, name):
    """Accept a dict, or a JSON-object string (the CLI passes key=value text)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise UsageError("%s must be a JSON object: %s" % (name, exc.msg))
    if not isinstance(value, dict):
        raise UsageError("%s must be an object, got %s" % (name, type(value).__name__))
    return value


def _comps_towns():
    """town-slug -> set of cached street slugs, across both cache tiers."""
    towns = {}
    for base in paths.read_candidates("comps"):
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            tdir = os.path.join(base, d)
            if os.path.isdir(tdir):
                towns.setdefault(d, set()).update(
                    f[:-5] for f in os.listdir(tdir) if f.endswith(".json"))
    return towns


def _pop_epc_sqft(f):
    """Pop a caller-supplied EPC floor area (sqft) off a fields dict.

    WHY this lives at the tool boundary and not in ingest: the Listing schema
    has no EPC slot (the engine never looks the SUBJECT up in the register),
    but ``value.subject_epc_sqft`` reads an optional ``epcSqft`` attribute a
    caller attaches — the seam the sqft-basis-conflict machinery and the
    works-vs-EPC agent question were built around. Without this pop the field
    silently vanished into ``listing_from_fields`` (which ignores unknown
    keys) and both honesty features were unreachable from either surface.
    Accepts ``epc_sqft`` (the documented spelling) and ``epcSqft``.
    """
    raw = None
    for key in ("epc_sqft", "epcSqft"):
        if key in f:
            val = f.pop(key)
            if raw is None:
                raw = val
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise UsageError("epc_sqft must be a number (the EPC certificate's "
                         "floor area in sqft), got %r" % raw)
    try:
        v = float(str(raw).replace(",", "")) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        raise UsageError("epc_sqft must be a number (the EPC certificate's "
                         "floor area in sqft), got %r" % raw)
    if v <= 0:
        raise UsageError("epc_sqft must be positive, got %r" % raw)
    return v


def _ingest(fields, text, mode=None):
    """Exactly one of fields/text -> a Listing, via the engine's adapter."""
    fields = _as_dict(fields, "fields")
    if mode is not None and mode not in ("buy", "rent"):
        raise UsageError("mode must be 'buy' or 'rent', got %r" % mode)
    if (fields is None) == (text is None):
        raise UsageError("supply exactly one of 'fields' (your structured read "
                        "of the listing) or 'text' (the raw pasted listing)")
    from gaff_engine import ingest
    if fields is not None:
        f = dict(fields)
        if mode:
            f.setdefault("mode", mode)
        epc_sqft = _pop_epc_sqft(f)
        listing = ingest.listing_from_fields(f)
        if epc_sqft is not None:
            # Attached, not schema'd: subject_epc_sqft duck-reads this, so
            # every composed tool (value_check, score_listing, show_work and
            # the viewing-question triggers) sees the same figure.
            listing.epcSqft = epc_sqft
        return listing
    return ingest.listing_from_text(str(text), mode=mode)


def _completeness(listing):
    prov = getattr(listing, "provenance", None)
    return dict(getattr(prov, "completeness", None) or {})


def _listing_summary(listing):
    """The short echo the composed tools return alongside their verdicts."""
    from gaff_engine.serialize import to_jsonable
    addr = getattr(listing, "address", None)
    return {"address": getattr(addr, "display", None),
            "postcode": getattr(addr, "postcode", None),
            "propertyType": to_jsonable(getattr(listing, "propertyType", None)),
            "beds": getattr(listing, "beds", None),
            "baths": getattr(listing, "baths", None),
            "sqft": getattr(listing, "sqft", None),
            "price": getattr(getattr(getattr(listing, "buy", None),
                                     "price", None), "amount", None),
            "rentPcm": getattr(getattr(getattr(listing, "rent", None),
                                       "rentPcm", None), "amount", None)}


def _demo_profile_path():
    """The shipped demo profile (the fictional 'Sam'), wherever this checkout
    keeps it: a user's own profile.json shadows it, then the lab's public/
    copy, then the assembled package's top-level copy."""
    path = paths.data_file("profile.json")
    if path:
        return path
    for cand in (os.path.join(_ROOT, "public", "profile.json"),
                 os.path.join(_ROOT, "profile.json")):
        if os.path.exists(cand):
            return cand
    return None


def _demo_person():
    from gaff_engine import elicit
    path = _demo_profile_path()
    if path is None:
        raise ToolError("no taste profile found: pass 'weights' (the eight "
                        "axis weights) or restore the shipped demo profile.json")
    with open(path, encoding="utf-8") as fh:
        return elicit.person_from_profile(json.load(fh))


def read_listing(fields=None, text=None, mode=None, progress=None):
    """One listing (structured read or raw paste) -> the engine's Listing shape."""
    progress = progress or _noop
    progress("reading the listing")
    listing = _ingest(fields, text, mode)
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable
    return {"listing": to_jsonable(listing),
            "completeness": _completeness(listing),
            "note": UNTRUSTED_LISTING_NOTE}


def _rental_blocker(listing):
    """The honest buy-only error, or None when the subject IS a sale.

    Truthful-errors rule (2a): value_check prices SALES. A rental subject has
    no asking price BY NATURE, and "no asking price on the listing" would
    blame the user for a number they never had — while the same payload
    echoes the rent it read. Name the real blocker AND the real next step:
    rent_check serves the asking-rent verdict where a rental pool exists.
    """
    ask = getattr(getattr(getattr(listing, "buy", None), "price", None), "amount", None)
    rent_pcm = getattr(getattr(getattr(listing, "rent", None), "rentPcm", None), "amount", None)
    mode = getattr(getattr(listing, "mode", None), "value", None)
    if ask is None and (mode == "rent" or rent_pcm is not None):
        return {"error": "this is a rental listing — value_check prices SALES "
                         "against sold comparables; use rent_check for an "
                         "asking-rent verdict",
                "hint": "the rent%s was read, not lost — rent_check judges it "
                        "against the local rental pool (lower confidence than "
                        "a sales verdict, and it says so)"
                        % (" (£%s pcm)" % "{:,}".format(int(rent_pcm))
                           if rent_pcm else ""),
                "listing": _listing_summary(listing)}
    return None


#: Greater London's own postcode areas (the E/EC/N/NW/SE/SW/W/WC districts).
#: An outcode in one of these places the subject in London even when the
#: pasted address never says the word — the golden's own "N1 4EJ" shape.
_LONDON_POSTCODE_AREAS = {"E", "EC", "N", "NW", "SE", "SW", "W", "WC"}


def _resolve_pool_town(listing):
    """The warmed town whose caches may price this subject, or None.

    WHY this exists (L2C P0): ``comps_for_listing`` defaults its town to
    LONDON and its nearby set to the De Beauvoir streets, so before routing
    a Leamington Spa paste was pooled against London sales and confidently
    tagged "steal" at -74% — a wrong verdict on the release's own headline
    scenario, and exactly the overclaim the abstention differentiator
    forbids. Resolution uses only facts the caches can verify, strongest
    first; when nothing places the subject, the caller refuses with a hint
    rather than guessing a city:

    1. a warmed town's name in the display address (rightmost wins — UK
       addresses end with the town, so "London Road, Leamington Spa"
       resolves to Leamington, not London);
    2. a Greater London postcode area in the outcode, when London is warmed;
    3. the subject's street cached under exactly ONE warmed town;
    4. a warmed town's name anywhere in the pasted text, only when exactly
       one matches (marketing copy name-drops towns, so ambiguity refuses).
    """
    towns = _comps_towns()
    if not towns:
        return None
    addr = getattr(listing, "address", None)
    display = str(getattr(addr, "display", None) or "").upper()
    hits = {}                              # slug -> rightmost match position
    for slug in towns:
        name = slug.replace("-", " ").upper()
        for m in re.finditer(r"\b%s\b" % re.escape(name), display):
            hits[slug] = m.start()
    if hits:
        return max(hits, key=lambda s: hits[s])
    outcode = str(getattr(addr, "outcode", None) or "").upper()
    m = re.match(r"([A-Z]{1,2})\d", outcode)
    if m and m.group(1) in _LONDON_POSTCODE_AREAS and "london" in towns:
        return "london"
    street = _listing_street(listing)
    if street:
        owners = [t for t, streets in towns.items() if _slug(street) in streets]
        if len(owners) == 1:
            return owners[0]
    desc = str(getattr(listing, "description", None) or "").upper()
    if desc:
        named = [slug for slug in towns
                 if re.search(r"\b%s\b" % re.escape(slug.replace("-", " ").upper()),
                              desc)]
        if len(named) == 1:
            return named[0]
    return None


def _value_core(listing, progress):
    """The shared value pipeline body: (payload, verdict, comps).

    ``payload`` is exactly what value_check returns (a verdict payload or one
    of the honest soft-error dicts); ``verdict``/``comps`` ride alongside for
    composers (score_listing / show_work) that need the underlying objects —
    both are None/empty on the error paths. Extracted so the flagship
    one-call tool composes THE SAME code path, never a re-implementation.

    The comp pool is ROUTED by the subject's town (L2C P0): the enriched
    London file and the De Beauvoir nearby set load only for a subject that
    resolves to London; another warmed town gets its own cached streets; a
    subject no cache verifiably reaches gets an honest refusal, never a tag
    priced against somewhere else.
    """
    blocker = _rental_blocker(listing)
    if blocker is not None:
        return blocker, None, []
    from gaff_engine import landreg as _landreg
    from gaff_engine import value as _value
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable
    town = _resolve_pool_town(listing)
    if town is None:
        return ({"error": "could not place this listing in a warmed town — "
                          "no verdict is better than one priced against the "
                          "wrong city's sales",
                 "hint": "name the town in the address (warmed: %s), or warm "
                         "its town first (warm street=<street> town=<town>). %s"
                         % (", ".join(sorted(_comps_towns())) or "none",
                            _COVERAGE),
                 "listing": _listing_summary(listing)},
                None, [])
    comps = []
    if town == "london":
        progress("loading enriched comps")
        try:
            comps.extend(_value.load_enriched_comps())
        except (OSError, ValueError, TypeError):
            pass                      # cold tier: honest absence handled below
        progress("loading cached street sales")
        comps.extend(_landreg.comps_for_listing(listing, offline=True))
    else:
        town_name = town.replace("-", " ").upper()
        progress("loading cached %s sales" % town_name.title())
        streets = sorted(_comps_towns().get(town) or ())
        comps.extend(_landreg.comps_for_listing(
            listing, town=town_name,
            nearby=[s.replace("-", " ").upper() for s in streets],
            offline=True))
        # Attached, not schema'd (the epcSqft idiom): hpi.region_for reads an
        # optional ``district`` attribute before scanning the address, so a
        # subject routed by street-uniqueness alone ("Willes Road", no town
        # word) still adjusts in its own district's money, never London's.
        if getattr(listing, "district", None) is None:
            listing.district = town_name.lower()
    if not comps:
        return ({"error": "no cached comparable sales to price against",
                 "hint": "warm street=<street> town=<town> first. " + _COVERAGE},
                None, [])
    if not any(_value._is_same_street(c, listing) for c in comps):
        # The reach guard (L2C P0): a pool with no same-street sales must
        # EARN its geography — the same outcode/town evidence show_work's
        # trace uses (workings._area_evidence). A pool that cannot is the
        # London-comps-for-a-Battersea-flat case: refuse, don't tag.
        from gaff_engine import workings as _workings
        if _workings._area_evidence(listing, comps) is None:
            return ({"error": "cached sales exist, but none verifiably reach "
                              "this subject's area — no verdict rather than a "
                              "tag priced against somewhere else",
                     "hint": "warm the subject's own street (warm "
                             "street=<street> town=<town>) so the pool "
                             "reaches it. " + _COVERAGE,
                     "listing": _listing_summary(listing)},
                    None, [])
    progress("judging value against %d comps" % len(comps))
    verdict = _value.value_verdict(listing, comps)
    band = getattr(verdict, "band", None)
    payload = {"tag": to_jsonable(verdict.tag),
               "deltaPct": verdict.deltaPct,
               "fairEstimate": verdict.fairEstimate,
               "band": ({"low": band.low, "high": band.high}
                        if band is not None else None),
               "confidence": verdict.confidence,
               "basis": verdict.basis,
               "reasons": list(getattr(verdict, "reasons", None) or []),
               "comps_used": len(comps),
               "listing": _listing_summary(listing),
               "completeness": _completeness(listing),
               "source": "HM Land Registry Price Paid + EPC register + UK HPI "
                         "(Open Government Licence v3.0)",
               "note": UNTRUSTED_LISTING_NOTE}
    return payload, verdict, comps


def value_check(fields=None, text=None, progress=None):
    """Ingest + cached comps + the value verdict, composed, entirely offline."""
    progress = progress or _noop
    listing = _ingest(fields, text)
    payload, _verdict, _comps = _value_core(listing, progress)
    return payload


def taste_score(reads=None, fields=None, text=None, weights=None, progress=None):
    """Taste verdict where the HOST LLM is the taste model (the read boundary
    the engine was built around): the host supplies the axis reads, the engine
    supplies the deterministic weighting/adjustment pipeline."""
    progress = progress or _noop
    from gaff_engine import taste as _taste
    axis_keys = [a.value for a in _taste.AXIS_ORDER]
    reads = _as_dict(reads, "reads")
    if not reads:
        raise UsageError("'reads' is required — you are the taste model: score "
                        "each of the eight axes 0-10 from the listing evidence "
                        "(%s), each with an honest one-line contribution"
                        % ", ".join(axis_keys))
    listing = _ingest(fields, text)
    return _taste_core(listing, reads, weights, progress)


def _taste_core(listing, reads, weights, progress):
    """The shared taste pipeline body over an already-ingested listing.

    Extracted (not forked) from taste_score so score_listing composes the same
    validation and the same recompute contract; ``reads`` is a dict here (the
    caller has run ``_as_dict``)."""
    from gaff_engine import taste as _taste
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable
    axis_keys = [a.value for a in _taste.AXIS_ORDER]
    axes_in = _as_dict(reads.get("axes"), "reads.axes") or {}
    missing = [k for k in axis_keys if k not in axes_in]
    if missing:
        raise UsageError("reads.axes must score all eight axes; missing: %s"
                        % ", ".join(missing))
    axes = {}
    for key in axis_keys:
        entry = axes_in[key]
        if not isinstance(entry, dict) or entry.get("score") is None:
            raise UsageError("reads.axes.%s needs {'score': 0-10, "
                            "'contribution': '...'}" % key)
        try:
            score = float(entry["score"])
        except (TypeError, ValueError):
            raise UsageError("reads.axes.%s.score must be a number 0-10, got %r"
                            % (key, entry["score"]))
        if not 0.0 <= score <= 10.0:
            raise UsageError("reads.axes.%s.score must be within 0-10, got %s"
                            % (key, score))
        axes[key] = _taste.AxisRead(score, str(entry.get("contribution") or ""))
    anti = reads.get("antiSignalHits")
    if anti is not None:
        try:
            anti = [(str(s), float(p), bool(f)) for s, p, f in anti]
        except (TypeError, ValueError):
            raise UsageError("antiSignalHits must be [signal, penalty, fatal] "
                            "triples, e.g. [[\"marble\", -1.0, false]]")
    loves = reads.get("namedLoveHits")
    read = _taste.TasteRead(axes=axes,
                            namedLoveHits=list(loves) if loves is not None else None,
                            antiSignalHits=anti)
    weights = _as_dict(weights, "weights")
    if weights:
        missing_w = [k for k in axis_keys if k not in weights]
        if missing_w:
            raise UsageError("weights must cover all eight axes; missing: %s"
                            % ", ".join(missing_w))
        person = {"taste": {"weights": weights, "lovesNamed": list(loves or [])}}
    else:
        person = _demo_person()
    progress("scoring taste over the eight axes")
    tr = _taste.taste_result(listing, person,
                             _taste.RecordedModel({True: read}), run_prior=False)
    return {"score": tr.score, "base": tr.base,
            "breakdown": [{"axis": to_jsonable(a.axis), "score": a.score,
                           "weight": a.weight, "contribution": a.contribution}
                          for a in tr.axisBreakdown],
            "adjustments": to_jsonable(tr.tasteAdjustments),
            "reasons": to_jsonable(list(getattr(tr, "reasons", None) or [])),
            "note": UNTRUSTED_LISTING_NOTE}


# ---------------------------------------------------------------------------
# The flagship one-call tool + its companions (outside-review brief §5).
# score_listing composes the pieces above — never re-implements them — and
# adds the two presentation layers: the show_work trace and a templated
# narrative. No LLM is called anywhere here; the narrative is built from the
# numbers, in the repo's honest voice.
# ---------------------------------------------------------------------------


def _evidence_strength(confidence):
    """The outward register for confidence (brief §2): strong/moderate/weak,
    never a bare scalar in prose. Thresholds are value.py's own bands."""
    if confidence is None:
        return "weak"
    if confidence >= 0.75:
        return "strong"
    if confidence >= 0.5:
        return "moderate"
    return "weak"


def _band_framing(ask, band):
    """Where the ask sits against the evidence band: below/within/above."""
    if ask is None or not band or band.get("low") is None:
        return None
    if ask < band["low"]:
        return "below"
    if ask > band["high"]:
        return "above"
    return "within"


def _narrative(listing, value_payload, taste_payload, questions, checklist):
    """The short plain-text story: fit, price evidence with the band framing,
    what to check, the viewing questions. Templated from the numbers — every
    sentence traces to a field the structured payload also carries."""
    lines = []
    summary = _listing_summary(listing)
    where = summary.get("address") or summary.get("postcode") or "this listing"
    beds = summary.get("beds")
    lines.append("%s%s." % (where, (", %s bed" % beds) if beds else ""))

    # Fit.
    if taste_payload:
        lines.append("Fit: taste %.1f/10 against the supplied profile "
                     "(weighted base %.1f, adjustments shown in the breakdown)."
                     % (taste_payload["score"], taste_payload["base"]))
    else:
        lines.append("Fit: unscored — no axis reads were supplied. You are the "
                     "taste model: pass reads= to score the eight axes.")

    # Price evidence — abstention stays first-class (brief §2): "insufficient
    # evidence, no tag" is the trust advantage, said plainly and without shame.
    err = value_payload.get("error")
    tag = value_payload.get("tag")
    if err:
        lines.append("Price: no verdict — %s." % err)
    elif tag == "needs_data":
        lines.append("Price: insufficient evidence, no tag. %s"
                     % (value_payload.get("basis") or ""))
    else:
        ask = summary.get("price")
        band = value_payload.get("band")
        framing = _band_framing(ask, band)
        strength = _evidence_strength(value_payload.get("confidence"))
        if framing and ask is not None:
            lines.append("Price: asking £%s sits %s the £%s–£%s evidence band "
                         "(fair estimate £%s, adjusted delta %+.1f%%) — read the "
                         "'%s' tag as conditional on that band. Price evidence: "
                         "%s (%d comps)."
                         % ("{:,}".format(ask), framing,
                            "{:,}".format(band["low"]), "{:,}".format(band["high"]),
                            "{:,}".format(value_payload["fairEstimate"]),
                            value_payload["deltaPct"], tag, strength,
                            value_payload.get("comps_used", 0)))
        else:
            lines.append("Price: %s — fair estimate £%s, adjusted delta %+.1f%%. "
                         "Price evidence: %s."
                         % (tag, "{:,}".format(value_payload["fairEstimate"]),
                            value_payload["deltaPct"], strength))
        reasons = value_payload.get("reasons") or []
        if reasons:
            lines.append(reasons[0])

    # What to check + the questions that matter.
    musts = [c for c in checklist if c.get("priority") == "must"]
    if musts:
        lines.append("Before committing: %s" % musts[0]["prompt"])
    if questions:
        lines.append("Ask the agent (%d question%s from the engine's own "
                     "uncertainty):" % (len(questions),
                                        "s" if len(questions) != 1 else ""))
        for q in questions[:4]:
            lines.append("- %s" % q["question"])
    else:
        lines.append("No agent questions fired — the engine found no material "
                     "uncertainty of its own on this listing.")
    return "\n".join(lines)


def score_listing(fields=None, text=None, reads=None, weights=None, progress=None):
    """One call, the whole verdict story: ingest → value → taste (when reads
    are supplied) → checklist + agent questions → the show_work trace and a
    templated narrative. Composes value_check / taste_score / viewing /
    workings; owns none of their logic."""
    progress = progress or _noop
    listing = _ingest(fields, text)
    from gaff_engine import viewing as _viewing
    from gaff_engine import workings as _workings
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable

    value_payload, verdict, comps = _value_core(listing, progress)

    reads = _as_dict(reads, "reads")
    taste_payload = None
    if reads:
        taste_payload = _taste_core(listing, reads, weights, progress)

    progress("generating agent questions from the engine's uncertainty")
    questions = _viewing.agent_questions(None, listing, value=value_payload,
                                         taste=taste_payload)
    # The standing checklist machinery, reused: with no full engine score there
    # are no flags to fold in, so this is the playbook + buy-question spine.
    checklist = to_jsonable(_viewing.checklist_sorted(
        _viewing.generate_checklist({}, listing, None)))

    progress("assembling the work trace")
    work = _workings.show_work(listing,
                               verdict=verdict if verdict is not None else value_payload,
                               comps=comps, taste=taste_payload)

    return {"listing": _listing_summary(listing),
            "completeness": _completeness(listing),
            "value": value_payload,
            "taste": taste_payload or {
                "skipped": "no reads supplied — you are the taste model; pass "
                           "reads= (eight axes, 0-10, each with a contribution "
                           "line) to score fit"},
            "questions": questions,
            "checklist": checklist,
            "workings": work,
            "narrative": _narrative(listing, value_payload, taste_payload,
                                    questions, checklist),
            "note": UNTRUSTED_LISTING_NOTE}


def show_work(fields=None, text=None, reads=None, weights=None, progress=None):
    """The working trace alone (plan section 9: every number traceable on
    demand): the same pipeline as score_listing, returned as the structured
    trace plus its narrated plain-text form."""
    progress = progress or _noop
    listing = _ingest(fields, text)
    from gaff_engine import workings as _workings
    value_payload, verdict, comps = _value_core(listing, progress)
    reads = _as_dict(reads, "reads")
    taste_payload = _taste_core(listing, reads, weights, progress) if reads else None
    work = _workings.show_work(listing,
                               verdict=verdict if verdict is not None else value_payload,
                               comps=comps, taste=taste_payload)
    work["rendered"] = _workings.render_text(work)
    return work


def rent_check(fields=None, text=None, progress=None):
    """Honest rent verdict where a rental pool exists; an honest error where
    none does. The pool never ships with the package (rental comparison data
    is scraped portal content, not redistributable) — a user supplies their
    own file in the user cache, and this tool says exactly that."""
    progress = progress or _noop
    listing = _ingest(fields, text)
    from gaff_engine import rent as _rent
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable
    rent_pcm = getattr(getattr(getattr(listing, "rent", None), "rentPcm", None),
                       "amount", None)
    ask = getattr(getattr(getattr(listing, "buy", None), "price", None),
                  "amount", None)
    if rent_pcm is None:
        if ask is not None:
            return {"error": "this is a sale listing — rent_check judges ASKING "
                             "RENTS against comparable local lets; use "
                             "value_check to price a sale",
                    "listing": _listing_summary(listing)}
        return {"error": "no asking rent on the listing — rent_check needs a "
                         "£pcm (pw/pa figures are converted on ingest)",
                "listing": _listing_summary(listing)}
    progress("loading the rental pool")
    try:
        pool = _rent.load_rent_pool()
    except FileNotFoundError:
        pool = None
    if not pool:
        return {"error": "no rental pool on this machine — rental comparison "
                         "data is scraped portal content and not "
                         "redistributable, so the package ships none",
                "hint": "supply your own: put a rental_candidates.json (a list "
                        "of {outcode, beds, pcm} records from your own listing "
                        "data) at %s"
                        % os.path.join(paths.user_cache_dir(),
                                       "rental_candidates.json")}
    progress("judging the asking rent against %d pooled lets" % len(pool))
    verdict = _rent.rent_verdict(listing, pool)
    band = getattr(verdict, "band", None)
    return {"tag": to_jsonable(verdict.tag),
            "deltaPct": verdict.deltaPct,
            "fairRentPcm": verdict.fairEstimate,
            "band": ({"low": band.low, "high": band.high}
                     if band is not None else None),
            "confidence": verdict.confidence,
            "confidence_note": "asking-rent evidence only — what landlords ASK "
                               "nearby, not what lets agreed at. A weaker "
                               "signal than a sold-price verdict, by design.",
            "basis": verdict.basis,
            "reasons": list(getattr(verdict, "reasons", None) or []),
            "pool_size": len(pool),
            "listing": _listing_summary(listing),
            "completeness": _completeness(listing),
            "note": UNTRUSTED_LISTING_NOTE}


def coverage(progress=None):
    """What the caches can answer right now — the same facts the error paths
    already compute, offered BEFORE a question fails (2b)."""
    progress = progress or _noop
    progress("walking the cache tiers")
    comps = {}
    for base in paths.read_candidates("comps"):
        if not os.path.isdir(base):
            continue
        for town in os.listdir(base):
            tdir = os.path.join(base, town)
            if not os.path.isdir(tdir):
                continue
            streets = sorted(f[:-5] for f in os.listdir(tdir)
                             if f.endswith(".json"))
            rec = comps.setdefault(town, {"streets": set(), "fetchedAt": None})
            rec["streets"].update(streets)
            if rec["fetchedAt"] is None and streets:
                # one file's vintage per town is cheap; per-file ones are not
                try:
                    with open(os.path.join(tdir, streets[0] + ".json"),
                              encoding="utf-8") as fh:
                        rec["fetchedAt"] = json.load(fh).get("fetchedAt")
                except (OSError, ValueError):
                    pass
    from gaff_engine import flips as _flips
    progress("counting repeat-sales records")
    flip_counts = {}
    for r in _flips.load_flips():
        t = (r.get("town") or "").upper()
        if t:
            flip_counts[t] = flip_counts.get(t, 0) + 1
    datasets = []
    if paths.data_file("comps_enriched.json"):
        datasets.append("comps_enriched.json")
    if paths.read_path("hpi"):
        datasets.append("hpi")
    if paths.read_path("epc"):
        datasets.append("epc")
    if _demo_profile_path():
        datasets.append("profile.json")
    return {"comps_towns": {t: {"streets": len(v["streets"]),
                                "fetchedAt": v["fetchedAt"]}
                            for t, v in sorted(comps.items())},
            "flips_towns": dict(sorted(flip_counts.items())),
            "datasets": datasets,
            "note": _COVERAGE}


def warm(street=None, town=None, flips_town=None, progress=None):
    """The verb every cold-data error names (2a): cache one street's sales
    (street= + town=, one live Land Registry call), or build a town's
    repeat-sales dataset (flips_town=, a paced whole-town pull)."""
    progress = progress or _noop
    if flips_town and (street or town):
        raise UsageError("warm one thing at a time: street= (+town=) for sales "
                        "comps, or flips_town= for repeat-sales data")
    if flips_town:
        from gaff_engine import flips as _flips
        progress("building repeat-sales data for %s (live Land Registry pull; "
                 "a whole town can take minutes)" % str(flips_town).upper())
        try:
            summary = _flips.build_town(str(flips_town), progress=progress)
        except (_flips.TownTooLargeError, _flips.FlipsFetchError) as exc:
            # The T8 cap protects scale; surface it as a clear message,
            # never a traceback.
            raise ToolError(str(exc))
        out = {"warmed": "flips"}
        out.update(summary)
        return out
    if not street:
        raise UsageError("supply street= (with town=, default LONDON) to cache "
                        "one street's sales, or flips_town= to build a town's "
                        "repeat-sales data")
    town = town or "LONDON"
    from gaff_engine import landreg as _landreg
    progress("fetching Land Registry sales for %s, %s" % (street, town))
    items = _landreg.fetch_street(street, town)
    progress("%d sales cached" % len(items))
    return {"warmed": "comps", "street": str(street).upper(),
            "town": str(town).upper(), "sales_cached": len(items),
            "hint": ("price_check street=%r town=%r now answers from this cache"
                     % (street, town)) if items else
                    ("0 items can mean no recorded sales on that street, a "
                     "misspelling, or a failed fetch (a warning names which)"),
            "source": "HM Land Registry Price Paid (Open Government Licence v3.0)"}


# Shared inputSchema fragments: the two ways every listing-taking tool accepts
# its subject (exactly one of the two, enforced in _ingest with a clear error).
_FIELDS_PROP = {"type": "object", "description":
                "Your structured read of the listing: address, postcode, beds, "
                "baths, sqft or sqm, price or rent_pcm (numbers or display "
                "strings), property_type, tenure, lease_years, description, "
                "key_features, mode ('buy'/'rent'), epc_sqft (the EPC "
                "certificate's floor area in sqft, when you have the "
                "certificate — enables the marketing-vs-EPC basis check and "
                "the works-vs-EPC agent question). All optional; omit what "
                "the listing does not state — absences are recorded honestly, "
                "never guessed."}
_TEXT_PROP = {"type": "string", "description":
              "The raw pasted listing text; parsed deterministically (regex, "
              "no guessing) — prefer 'fields' when you have read the listing "
              "yourself."}

TOOLS = [
    {"name": "price_check",
     "description": "Recent Land Registry sales on a UK street, with median "
                    "price. Town-scoped. " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {
         "street": {"type": "string", "description": "Street name, e.g. 'De Beauvoir Road'"},
         "town": {"type": "string", "description": "Town, e.g. 'LONDON'. Defaults to LONDON."}},
         "required": ["street"]}},
    {"name": "flip_stats",
     "description": "Repeat-sales analysis for a town: uplift achieved versus "
                    "what the market did anyway. " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {
         "town": {"type": "string", "description": "Town name, e.g. 'LEAMINGTON SPA'"}},
         "required": ["town"]}},
    {"name": "read_listing",
     "description": "Parse one property listing (your structured read, or raw "
                    "pasted text) into the engine's honest Listing shape, with "
                    "a completeness map of what was missing. Runs entirely "
                    "locally; no data leaves the machine.",
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP,
         "mode": {"type": "string", "enum": ["buy", "rent"],
                  "description": "Force buy/rent when the listing doesn't say."}},
         "required": []}},
    {"name": "value_check",
     "description": "Is the asking price fair? Ingests the listing (fields or "
                    "text), loads cached comparable sales, and returns the "
                    "engine's value verdict: steal/fair/over tag, percent delta, "
                    "fair estimate, confidence and its evidence basis. SALES "
                    "only: a rental listing gets an honest error (rent verdicts "
                    "are not served yet). " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP},
         "required": []}},
    {"name": "taste_score",
     "description": "Score a listing against a taste profile. YOU (the host "
                    "model) are the taste model: read the listing evidence and "
                    "score each of the eight axes 0-10 — light_and_volume, "
                    "outdoor_space, character_bones, width_proportion_flow, "
                    "street_scene, raw_size_threshold, design_finish, "
                    "station_proximity — each with an honest one-line "
                    "contribution grounded in what the listing actually shows "
                    "(never the asking price). Optionally report namedLoveHits "
                    "(loves you saw) and antiSignalHits ([signal, penalty, "
                    "fatal] triples). The engine then runs its deterministic "
                    "weighting and adjustment pipeline over your read. Weights "
                    "default to the shipped demo profile.",
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP,
         "reads": {"type": "object", "description":
                   "{axes: {<axis>: {score: 0-10, contribution: str}} for all "
                   "eight axes, namedLoveHits?: [str], antiSignalHits?: "
                   "[[signal, penalty, fatal]]}"},
         "weights": {"type": "object", "description":
                     "The eight axis weights; omitted -> the shipped demo "
                     "profile's weights."}},
         "required": ["reads"]}},
    {"name": "score_listing",
     "description": "The one-call verdict. Paste a listing (or an alert "
                    "email's listing, as text= or your structured fields= "
                    "read): get a clear explanation of fit, evidence-based "
                    "price context, and the viewing questions that matter — "
                    "call it once per listing to rank a shortlist. Composes "
                    "ingest, the value verdict (band + steal/fair/over "
                    "conditional on the evidence, NEEDS_DATA when the evidence "
                    "isn't there), taste scoring when you supply axis reads "
                    "(same contract as taste_score — you are the taste model), "
                    "agent questions generated from the engine's own "
                    "uncertainty, the full show-your-working trace, and a "
                    "short plain narrative templated from the numbers (no LLM "
                    "call). " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP,
         "reads": {"type": "object", "description":
                   "Optional taste reads, taste_score's exact contract: "
                   "{axes: {<axis>: {score: 0-10, contribution: str}} for all "
                   "eight axes, namedLoveHits?, antiSignalHits?}. Omit to skip "
                   "taste and get value + questions only."},
         "weights": {"type": "object", "description":
                     "The eight axis weights; omitted -> the shipped demo "
                     "profile's weights."}},
         "required": []}},
    {"name": "show_work",
     "description": "Every number traceable on demand: the full working trace "
                    "for one listing — address-match level (street vs area vs "
                    "unverified pool; the engine never claims the exact "
                    "building), where the "
                    "subject's floor area came from (stated / derived / "
                    "missing, plus any marketing-vs-EPC basis conflict), comp "
                    "counts by trust tier with non-standard-sale exclusions "
                    "shown, the value-band arithmetic written out, taste axis "
                    "rows with the recompute sum, and flags — as structured "
                    "data plus a narrated plain-text 'rendered' field. "
                    "Presentation only: nothing new is computed. " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP,
         "reads": {"type": "object", "description":
                   "Optional taste reads (taste_score's contract) to include "
                   "the taste arithmetic in the trace."},
         "weights": {"type": "object", "description":
                     "The eight axis weights; omitted -> the shipped demo "
                     "profile's weights."}},
         "required": []}},
    {"name": "rent_check",
     "description": "Is the asking rent fair? Judges a rental listing's £pcm "
                    "against comparable local lets (same outcode + bed count) "
                    "from a LOCAL rental pool file — honestly lower confidence "
                    "than a sales verdict (asking rents, not agreed lets, and "
                    "the payload says so). The package ships no pool (rental "
                    "comparison data is scraped portal content, not "
                    "redistributable): without a user-supplied "
                    "rental_candidates.json in the user cache this returns the "
                    "honest error naming exactly that.",
     "inputSchema": {"type": "object", "properties": {
         "fields": _FIELDS_PROP, "text": _TEXT_PROP},
         "required": []}},
    {"name": "coverage",
     "description": "What the local caches can answer right now: warmed comps "
                    "towns with street counts, flips towns with record counts, "
                    "data vintages, and which loose datasets are present. Call "
                    "this to scope a question before asking it. " + _COVERAGE,
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "warm",
     "description": "Fetch and cache open Land Registry data so cold questions "
                    "become answerable: street= (+town=) caches one street's "
                    "sales (one live call); flips_town= builds a town's "
                    "repeat-sales dataset (a paced whole-town pull, minutes; "
                    "very large towns are refused by a record cap). The only "
                    "tool that touches the network.",
     "inputSchema": {"type": "object", "properties": {
         "street": {"type": "string", "description": "Street to cache sales for"},
         "town": {"type": "string", "description": "Town for street=. Defaults to LONDON."},
         "flips_town": {"type": "string", "description": "Town to build repeat-sales data for"}},
         "required": []}},
]

DISPATCH = {"price_check": price_check, "flip_stats": flip_stats,
            "read_listing": read_listing, "value_check": value_check,
            "taste_score": taste_score, "score_listing": score_listing,
            "show_work": show_work, "rent_check": rent_check,
            "coverage": coverage, "warm": warm}


# ---------------------------------------------------------------------------
# The CLI surface (the skill script and the `gaff` console entry point). It
# lives HERE, beside the tools, so the copied skill folder and a pip install
# run the identical code (R2/R3). Tool functions above still never print;
# printing is this surface's job alone.
# ---------------------------------------------------------------------------


def _cli_progress(msg):
    """Stream progress as it happens. stderr keeps stdout parseable as JSON."""
    print("  ... %s" % msg, file=sys.stderr, flush=True)


def demo():
    """The seeded street end to end, OFFLINE, as a short narrated report.

    Plain text, not JSON (like doctor): it exists to show a first-hour user
    what the machine does with zero configuration. Everything below reads the
    shipped cache — price_check on the golden street, the golden taste
    recompute (the same replay the golden test pins at 8.2), and the value
    story for a maisonette on that street.
    """
    failures = 0
    say = print

    say("GAFF DEMO — the seeded street, end to end, offline")
    say("=" * 52)

    say("")
    say("1. What sold on De Beauvoir Road? (price_check)")
    ok, p = safe_call("price_check", DISPATCH["price_check"],
                      {"street": "De Beauvoir Road", "town": "LONDON"},
                      progress=_cli_progress)
    if ok and "error" not in p:
        say("   %d cached sales; median £%s."
            % (p["sales_found"], "{:,}".format(p["median_price"])))
        for s in p["most_recent"][:3]:
            say("   - £%s on %s (%s)"
                % ("{:,}".format(s["price"]), s["date"], s.get("type") or "?"))
        say("   source: %s" % p["source"])
    else:
        failures += 1
        say("   FAILED: %s" % p.get("error"))

    say("")
    say("2. Would the demo taste profile like a maisonette there? (taste)")
    # The canonical De Beauvoir recording replayed through the REAL pipeline —
    # deterministic, and pinned by tests/test_u6_taste.py at 8.2.
    from gaff_engine.taste import canonical_model, taste_result
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_PERSON
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    say("   taste %.1f / 10 (weighted base %.1f, text-only prior %.1f)"
        % (tr.score, tr.base, tr.prior))
    for r in (getattr(tr, "reasons", None) or [])[:2]:
        say("   - %s" % (getattr(r, "text", None) or r))
    if tr.score != 8.2:
        failures += 1
        say("   FAILED: expected the golden 8.2, got %s" % tr.score)

    say("")
    say("3. Is £1,150,000 a fair ask for it? (value_check)")
    ok, v = safe_call("value_check", DISPATCH["value_check"], {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
        "sqft": 1050, "price": 1150000, "property_type": "maisonette",
        "mode": "buy", "tenure": "leasehold", "lease_years": 96}},
        progress=_cli_progress)
    if ok and "error" not in v and v.get("fairEstimate") is not None:
        say("   verdict: %s — fair estimate £%s, asking delta %+.1f%%, "
            "confidence %.2f"
            % (v["tag"], "{:,}".format(v["fairEstimate"]), v["deltaPct"],
               v["confidence"]))
        say("   basis: %s" % v["basis"])
        for r in v.get("reasons", [])[:2]:
            say("   - %s" % r)
    else:
        failures += 1
        say("   FAILED: %s" % (v.get("error") or v.get("basis")))

    say("")
    say("Everything above ran offline from the shipped cache. Try your own street:")
    say('  gaff price_check street="Your Street" town=LONDON')
    say('  gaff warm street="Your Street" town="YOUR TOWN"   (fetches + caches it)')
    say('  gaff coverage                                     (what is cached now)')
    return 1 if failures else 0


def cli_main():
    """The `gaff` CLI: results on stdout as JSON, progress on stderr as it
    happens (the property that removed the need for a background job runner).
    Exit codes: 0 answered; 1 the tool ran into the data (an honest
    {error, hint} payload still prints — the hint is the point — or a runtime
    failure went to stderr); 2 the INVOCATION was wrong (unknown tool,
    non key=value token, bad argument names, or argument-validation
    UsageErrors like mode=banana) — carried by the payload's 'usage' tag,
    not by sniffing error-string prefixes."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: gaff <tool> [key=value ...]\n\ntools:")
        for t in TOOLS:
            args = ", ".join(t["inputSchema"]["properties"])
            print("  %-12s %s  (%s)" % (t["name"], t["description"], args))
        print("  %-12s %s" % ("demo", "The seeded street end to end, offline, narrated."))
        print("  %-12s %s" % ("doctor", "Paste-able, secret-free diagnostic bundle."))
        return 0
    name = sys.argv[1]
    if name == "doctor":
        # Diagnostic bundle: plain text, not JSON — it exists to be pasted.
        from gaff_engine.doctor import main as doctor_main
        return doctor_main()
    if name == "demo":
        return demo()
    fn = DISPATCH.get(name)
    if fn is None:
        print("unknown tool: %s (try --help)" % name, file=sys.stderr)
        return 2
    args = {}
    for tok in sys.argv[2:]:
        if "=" not in tok:
            print("arguments must be key=value, got %r" % tok, file=sys.stderr)
            return 2
        k, v = tok.split("=", 1)
        args[k] = v
    ok, payload = safe_call(name, fn, args, progress=_cli_progress)
    if not ok:
        print(payload["error"], file=sys.stderr)
        return 2 if payload.get("usage") else 1
    print(json.dumps(payload, indent=1))
    # A soft error (an honest {error, hint} dict) still prints — the hint is
    # the point — but the exit code must tell scripts the question was not
    # answered (2a: CLI tool errors exit non-zero).
    return 1 if isinstance(payload, dict) and "error" in payload else 0
