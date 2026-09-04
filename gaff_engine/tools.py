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
# S3: TOOLS and DISPATCH are the registry's objects, re-exported here. The
# surface tests monkeypatch them in place, so they must never be rebound.
from gaff_engine.registry import (
    COERCIONS, DISPATCH, TOOLS, arg, coerce_cli_args, register)

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


def _session_place():
    """``(town, nation)`` from the saved session, or ``(None, None)``.

    S1's Search carries the town as ``area.label`` (``confidence="rough"`` — a
    name, never a polygon) and the nation as the attached ``nation``. Reading
    it is how F-02 stops guessing: a user who ran ``situate town=<somewhere>``
    has already said where they are, and asking again by defaulting to LONDON
    is the thing this item exists to end. Never raises — a missing, unreadable
    or newer session file is ``(None, None)``, the same silence
    ``session.load`` promises.
    """
    from gaff_engine import session as _session
    try:
        search, _person = _session.load()
    except Exception:                     # noqa: BLE001 - a session may not cost a question
        return None, None
    if search is None:
        return None, None
    label = getattr(getattr(search, "area", None), "label", None)
    label = str(label).strip() if label else None
    return (label or None), getattr(search, "nation", None)


def _resolve_town(street=None, town=None):
    """``(town, source)`` for a street question. F-02's resolution order.

    ``town`` was ``"LONDON"`` by default until now, which meant a user who
    warmed a Leamington street and then asked the obvious next question was
    told *"'LONDON' is warmed, but 'Willes Road' is not cached there yet"* and
    invited to spend a live call fetching a London street that does not exist —
    while the sales they wanted sat in the cache one directory away (assessment
    finding F5). A default that is right for one city and silently wrong for
    every other city is worse than no default, so there is none now.

    Strongest first, and only facts something has actually stated or cached:

    1. ``town=`` as passed. An explicit argument is never second-guessed.
    2. the one warmed town whose cache holds THIS street, when exactly one
       does, and the session does not name a different one.
    3. the session's town (S1), when it is warmed.
    4. nothing: ``(None, None)``, and the caller refuses with the warmed towns
       rather than picking a city for the user.

    **This inverts the build plan's stated order for 2 and 3, deliberately.**
    The plan puts the session's town above the street's cache owner; run that
    way, a user situated in London who asks about a Leamington street is told
    "LONDON is warmed, but Willes Road is not cached there yet" — which is the
    one sentence F-02's own acceptance criterion forbids. The evidence beats
    the preference because it is evidence *about the thing asked about*: which
    town's cache holds this street is a fact about this street, while the
    session's town is a standing statement about where the user is looking.

    When the two disagree they are not merged: two methods disagreeing is a
    stop signal, so the caller asks. But they only disagree when BOTH could
    answer — that is, when the session's town is warmed too. A saved search set
    to Leeds does not make a question about a London-cached street ambiguous,
    because Leeds holds nothing to answer it with; it makes London the only
    town in the room, and the answer says so. Requiring warmth on both sides is
    what keeps the README's own opening pair (``situate town=LEEDS`` then
    ``price_check street="De Beauvoir Road"``) answering instead of asking.
    """
    if town is not None and str(town).strip():
        return str(town).strip(), "argument"
    towns = _comps_towns()
    session_town, _nation = _session_place()
    owners = ([t for t, streets in towns.items() if _slug(street) in streets]
              if street else [])
    owner = owners[0] if len(owners) == 1 else None
    if owner is not None:
        if (session_town and _slug(session_town) != owner
                and _slug(session_town) in towns):
            return None, "conflict"       # both warmed: a real choice, so ask
        return owner.replace("-", " ").upper(), "street"
    if session_town and _slug(session_town) in towns:
        return session_town, "session"
    return None, None


#: How a town that the user did not type came to be chosen, said plainly. A
#: payload that used an inferred town without saying so is the sticky-search
#: failure Q1 names, wearing a different hat.
_TOWN_SOURCE_NOTE = {
    "session": "you did not name a town, so this is the one your saved search "
               "is set to (run situate to change it)",
    "street": "you did not name a town, and this is the only warmed town with "
              "that street cached — say town= if you meant a different one",
}


def _no_town_refusal(street, source=None):
    """The refusal when nothing places a street. Names what would fix it.

    Two shapes, because two different things went wrong. Nothing placed the
    street at all, or two things placed it differently — and a conflict that
    reads like an absence sends the user looking for the wrong fix.
    """
    towns = sorted(_comps_towns())
    session_town, _nation = _session_place()
    warmed = ", ".join(towns) or "none"
    if source == "conflict":
        owner = next((t for t in towns if _slug(street) in
                      (_comps_towns().get(t) or ())), None)
        return {"error": "%r is cached under %r, but your saved search is set "
                         "to %r — say which you meant"
                         % (street, (owner or "").replace("-", " ").upper(),
                            session_town),
                "hint": "town=%r reads the cached sales; town=%r warms it "
                        "there instead (one live Land Registry call). Guessing "
                        "between them is how a street gets priced against "
                        "another town's market. %s"
                        % ((owner or "").replace("-", " ").upper(),
                           session_town, _COVERAGE),
                "towns_warmed": towns,
                "session_town": session_town,
                "street_cached_in": (owner or "").replace("-", " ").upper(),
                "town_resolved_from": None}
    hint = ("say town=<town> (warmed: %s), or warm street=%r town=<town> to "
            "fetch a new one (one live Land Registry call). %s"
            % (warmed, street, _COVERAGE))
    out = {"error": "no town for %r, and this build no longer assumes LONDON — "
                    "a default that is right for one city is silently wrong "
                    "for every other" % street,
           "hint": hint, "towns_warmed": towns,
           "town_resolved_from": None}
    if session_town:
        out["session_town"] = session_town
        out["hint"] = ("your saved search is set to %r, which is not warmed. %s"
                       % (session_town, hint))
    return out


def price_check(street: str, town: str = None, progress=None):
    """Recent Land Registry sales on one street in one town, from the cache.

    ``town`` no longer defaults to LONDON (F-02). Unstated, it is resolved by
    :func:`_resolve_town` and the answer says which town was used and how it
    was chosen; when nothing places the street the tool refuses with the warmed
    towns instead of answering about the wrong city.
    """
    progress = progress or _noop
    town, town_source = _resolve_town(street, town)
    if town is None:
        return _no_town_refusal(street, town_source)
    progress("looking up %s, %s" % (street, town))
    # Resolved through gaff_engine.paths so the surfaces read the same two-tier
    # cache the engine does: the user's own fetches first, then the warm cache
    # that ships with the package.
    path = paths.read_path("comps", _slug(town), "%s.json" % _slug(street))
    if path is None:
        # Truthful cold-data errors (2a): a cold STREET in a warm town must not
        # say "warm this town" — the town IS warm. Name what is actually
        # missing, and what IS cached, so the dead end becomes a next step.
        #
        # F-02 turns the hint into an OFFER. The difference is not cosmetic: an
        # MCP tool cannot prompt mid-call, so the only honest pattern is
        # return-an-offer, host-asks-user, host-calls-warm. This tool must not
        # grow a fetch=true flag — the moment any verb can fetch on a flag, the
        # one network verb S4 declares becomes every verb.
        from gaff_engine import cachemap as _cachemap
        towns = _comps_towns()
        cached = towns.get(_slug(town))
        # The saved session's nation describes the session's OWN town, and
        # applies to a question about THAT town however the town reached this
        # call -- named in the argument or inherited. It says nothing about any
        # other town. Passing it unconditionally let a Scottish saved search
        # answer a question about Cambridge with "Price Paid covers England and
        # Wales only, so there is no open sold-price data for CAMBRIDGE",
        # telling a user in England that their own country's open data does not
        # exist and that no action would change it. Worse than the LONDON
        # default F-02 removed, and it shipped in 0.2.0.
        #
        # Gating on town_source == "session" instead is the obvious fix and is
        # wrong: it drops the nation when a Scottish user names their own town
        # explicitly, and test_a_stated_scottish_nation_is_offered_no_warm_at_all
        # catches that. Compare the PLACE, not how it arrived.
        session_town, session_nation = _session_place()
        same_place = bool(session_town) and _slug(session_town) == _slug(town)
        nation = _cachemap.resolve_nation(session_nation if same_place else None,
                                          town)
        out = {"town": str(town).upper(), "street": str(street).upper(),
               "town_resolved_from": town_source}
        if nation["england_or_wales"] is False:
            # F-01's lesson, reused: never print an invocation that cannot
            # work. Price Paid is England and Wales only, so a Scottish or
            # Northern Irish street has no warm to offer at all.
            out.update({"error": "HM Land Registry Price Paid covers England "
                                 "and Wales only, so there is no open sold-price "
                                 "data for %r to fetch or cache" % str(town).upper(),
                        "hint": "no action changes this: the data does not "
                                "exist to be warmed. " + _COVERAGE,
                        "towns_warmed": sorted(towns)})
            return out
        offer = _cachemap.warm_offer(street, town,
                                     nation["england_or_wales"])
        # Found by testing the ambiguous case: a street held by two OTHER towns
        # resolves to neither, so the answer falls through to the session's
        # town and says "not cached there yet" — true, and quietly hiding that
        # this build holds that very street twice over. Name where it is.
        elsewhere = sorted(t.replace("-", " ").upper()
                           for t, streets in towns.items()
                           if _slug(street) in streets and t != _slug(town))
        if elsewhere:
            out["also_cached_in"] = elsewhere
            out["also_cached_hint"] = (
                "this build already holds %r in %s — town=%r answers from the "
                "cache with no live call at all"
                % (street, " and ".join(elsewhere), elsewhere[0]))
        if cached:
            out.update({"error": "%r is warmed, but %r is not cached there yet"
                                 % (town, street),
                        "hint": "%s to fetch it (%s), or ask about a cached "
                                "street" % (offer.get("action"),
                                            offer.get("calls")),
                        "town_warmed": True,
                        "offer": offer,
                        "streets_cached": sorted(cached)[:8]})
        else:
            hint = "%s first (%s), then ask again" % (offer.get("action"),
                                                     offer.get("calls"))
            if offer.get("conditional_on"):
                hint += " — though that call is conditional on %s" % \
                        offer["conditional_on"]
            out.update({"error": "no cached data for %r in %r" % (street, town),
                        "towns_warmed": sorted(towns),
                        "town_warmed": False,
                        "offer": offer,
                        "hint": hint})
        if out.get("also_cached_hint"):
            out["hint"] = "%s %s" % (out["also_cached_hint"], out["hint"])
        if town_source in _TOWN_SOURCE_NOTE:
            out["note"] = _TOWN_SOURCE_NOTE[town_source]
        return out
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
    if not sales:
        # Cached, fetched, and genuinely empty. Say which, so the user does not
        # read silence as a failure and warm it again for the same nothing.
        return {"street": env["street"], "town": env["town"], "sales_found": 0,
                "median_price": None, "most_recent": [],
                "town_resolved_from": town_source,
                "note": "this street was fetched on %s and HM Land Registry "
                        "holds no recorded sales for it under that name — check "
                        "the spelling, or it may genuinely have none. It is not "
                        "counted as coverage and does not route a listing."
                        % (env.get("fetchedAt") or "an earlier date"),
                "source": "HM Land Registry Price Paid (Open Government Licence v3.0)",
                "fetchedAt": env.get("fetchedAt")}
    out = {"street": env["street"], "town": env["town"], "sales_found": len(sales),
           "median_price": int(statistics.median(prices)) if prices else None,
           "most_recent": sales[:5],
           "town_resolved_from": town_source,
           "source": "HM Land Registry Price Paid (Open Government Licence v3.0)",
           "fetchedAt": env.get("fetchedAt")}
    # An inferred town is stated, not slipped in. The user asked about a street
    # and is being answered about a town they did not type.
    if town_source in _TOWN_SOURCE_NOTE:
        out["note"] = _TOWN_SOURCE_NOTE[town_source]
    return out


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


# The cache walk lives in gaff_engine.cachemap now (F-01): coverage, situate and
# F-03's area_brief are three callers, and a second copy of "does this cached
# street hold sales" is precisely the accident that overstated London's coverage
# by three streets in the released v0.1.0. These names stay bound here because
# _routed_comps, _resolve_pool_town and tests/test_cache_hygiene.py all use them.
from gaff_engine.cachemap import (                                # noqa: E402
    comps_map as _comps_towns, empty_streets as _empty_streets,
    street_has_sales as _street_has_sales)


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
       one matches (marketing copy name-drops towns, so ambiguity refuses);

    Every rule here is a fact about THE LISTING. The session's town is not,
    which is why F-02 applies it in ``_routed_comps`` instead of appending it
    here: a pool opened by a standing preference has to be able to say so when
    it fails, and a fifth rule in this list would be indistinguishable from the
    four that read the listing itself.
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


def _cache_fetched_at(town_slug, street_slugs):
    """``[{"name", "fetchedAt"}]`` for the cached street files a pool came from.

    One file's vintage per town (coverage does the same): per-file dates cost a
    read each and every street in a town is warmed in the same pass, so the
    first one is representative and the cheap answer is the honest one.
    """
    out = []
    for base in paths.read_candidates("comps"):
        tdir = os.path.join(base, town_slug)
        if not os.path.isdir(tdir):
            continue
        for slug in sorted(street_slugs or [])[:1] or sorted(
                f[:-5] for f in os.listdir(tdir) if f.endswith(".json"))[:1]:
            try:
                with open(os.path.join(tdir, slug + ".json"), encoding="utf-8") as fh:
                    stamp = json.load(fh).get("fetchedAt")
            except (OSError, ValueError):
                continue
            if stamp:
                out.append({"name": "%s sales cache" % town_slug.replace("-", " "),
                            "fetchedAt": stamp, "kind": "fetch"})
        break                      # the user tier shadows the shipped one
    return out


def _routed_comps(listing, progress):
    """The comp pool this subject may honestly be priced against.

    Returns ``(comps, sources, refusal)``. Exactly one of the pool and the
    refusal is meaningful — a pool, or the honest ``{error, hint}`` payload
    explaining why there is none. ``sources`` names the cache files the pool came
    from and when each was fetched, which is what S5's vintage is assembled from:
    the age of a verdict's evidence is only knowable where the evidence is
    loaded. Lifted out of ``_value_core`` (S2) and
    given a caller-visible name because THIS is the part ``engine.score`` does not
    have: ``engine.score`` defaults ``comps`` to ``load_enriched_comps()``, the
    London enriched file, for a subject anywhere in the UK. Every path that
    reaches the engine routes through here first, and ``tests/test_score_seam.py``
    poisons that default so a path that forgets fails loudly.

    The routing itself is unchanged (L2C P0): the subject's town resolves from
    facts the caches can verify, the enriched London file and the De Beauvoir
    nearby set load only for a subject that resolves to London, another warmed
    town gets its own cached streets, and a pool with no same-street sales must
    earn its geography through the reach guard or be refused.
    """
    blocker = _rental_blocker(listing)
    if blocker is not None:
        return [], [], blocker
    from gaff_engine import landreg as _landreg
    from gaff_engine import value as _value
    town = _resolve_pool_town(listing)
    # F-02: the listing placed nowhere, but the user may have said where they
    # are looking. A standing preference is weaker than anything the listing
    # itself carries, so it is consulted only here, only when all four listing
    # rules came back empty, and the pool it opens is FLAGGED — a verdict or a
    # refusal that came from the session's town must be able to say which.
    from_session = False
    if town is None:
        session_town, _nation = _session_place()
        if session_town and _slug(session_town) in _comps_towns():
            town, from_session = _slug(session_town), True
    if town is None:
        return [], [], {"error": "could not place this listing in a warmed town — "
                             "no verdict is better than one priced against the "
                             "wrong city's sales",
                    "hint": "name the town in the address (warmed: %s), or warm "
                            "its town first (warm street=<street> town=<town>). %s"
                            % (", ".join(sorted(_comps_towns())) or "none",
                               _COVERAGE),
                    "listing": _listing_summary(listing)}
    comps = []
    sources = []                        # S5: where this pool came from, and when
    if town == "london":
        progress("loading enriched comps")
        try:
            comps.extend(_value.load_enriched_comps())
            enriched = paths.data_file("comps_enriched.json")
            if enriched:
                with open(enriched, encoding="utf-8") as fh:
                    stamp = json.load(fh).get("generatedAt")
                if stamp:
                    # generatedAt is when the ENRICHMENT ran, not when the sales
                    # were pulled — a derivation, and labelled as one so it can
                    # never stand in for a fetch date.
                    sources.append({"name": "enriched London comparables",
                                    "fetchedAt": stamp, "kind": "derived"})
        except (OSError, ValueError, TypeError):
            pass                      # cold tier: honest absence handled below
        progress("loading cached street sales")
        comps.extend(_landreg.comps_for_listing(listing, offline=True))
        sources.extend(_cache_fetched_at("london", _comps_towns().get("london")))
    else:
        town_name = town.replace("-", " ").upper()
        progress("loading cached %s sales" % town_name.title())
        streets = sorted(_comps_towns().get(town) or ())
        comps.extend(_landreg.comps_for_listing(
            listing, town=town_name,
            nearby=[s.replace("-", " ").upper() for s in streets],
            offline=True))
        sources.extend(_cache_fetched_at(town, streets))
        # Attached, not schema'd (the epcSqft idiom): hpi.region_for reads an
        # optional ``district`` attribute before scanning the address, so a
        # subject routed by street-uniqueness alone ("Willes Road", no town
        # word) still adjusts in its own district's money, never London's.
        if getattr(listing, "district", None) is None:
            listing.district = town_name.lower()
    #: Said whenever the pool's geography came from the saved search rather
    #: than from the listing: the user is entitled to know the refusal is about
    #: a town THEY named for the search, not one this listing pointed at.
    session_caveat = (" This listing named no town this build could place, so "
                      "the pool came from your saved search (%s) — if the "
                      "listing is somewhere else, that is the mismatch."
                      % town.replace("-", " ").upper()) if from_session else ""
    if not comps:
        return [], [], {"error": "no cached comparable sales to price against",
                    "hint": "warm street=<street> town=<town> first. "
                            + _COVERAGE + session_caveat}
    if not any(_value._is_same_street(c, listing) for c in comps):
        # The reach guard (L2C P0): a pool with no same-street sales must
        # EARN its geography — the same outcode/town evidence show_work's
        # trace uses (workings._area_evidence). A pool that cannot is the
        # London-comps-for-a-Battersea-flat case: refuse, don't tag.
        from gaff_engine import workings as _workings
        if _workings._area_evidence(listing, comps) is None:
            return [], [], {"error": "cached sales exist, but none verifiably reach "
                                 "this subject's area — no verdict rather than a "
                                 "tag priced against somewhere else",
                        "hint": "warm the subject's own street (warm "
                                "street=<street> town=<town>) so the pool "
                                "reaches it. " + _COVERAGE + session_caveat,
                        "pool_town": town.replace("-", " ").upper(),
                        "pool_town_from": "session" if from_session
                                          else "listing",
                        "listing": _listing_summary(listing)}
    return comps, sources, None


def _value_core(listing, progress):
    """The shared value pipeline body: (payload, verdict, comps).

    ``payload`` is exactly what value_check returns (a verdict payload or one
    of the honest soft-error dicts); ``verdict``/``comps`` ride alongside for
    composers (score_listing / show_work) that need the underlying objects —
    both are None/empty on the error paths. Extracted so the flagship
    one-call tool composes THE SAME code path, never a re-implementation.

    The pool comes from :func:`_routed_comps`, which is where the L2C P0 town
    routing lives; this function is the verdict over whatever that returns.
    """
    from gaff_engine import value as _value
    from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
    from gaff_engine.serialize import to_jsonable
    comps, sources, refusal = _routed_comps(listing, progress)
    if refusal is not None:
        return refusal, None, []
    progress("judging value against %d comps" % len(comps))
    verdict = _value.value_verdict(listing, comps)
    # S5: the vintage is assembled HERE, where the pool and its cache files are
    # both in hand, and rides in the payload so every verdict carries it.
    from gaff_engine import hpi as _hpi
    from gaff_engine import vintage as _vintage
    # The HPI facts come off the VERDICT, which is the only thing that knows
    # whether the adjustment actually moved a comp — a region can resolve and
    # still move nothing when its months are not to hand. Re-deriving the region
    # here is what let the vintage line claim an adjustment the basis did not.
    evidence = _vintage.evidence_vintage(
        comps, sources=sources, hpi_month=_hpi.AS_OF_MONTH,
        hpi_region=getattr(verdict, "hpiRegion", None),
        hpi_adjusted=getattr(verdict, "hpiAdjusted", None))
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
               # How old the evidence under this verdict is (S5 / assessment F6).
               # Staleness that is not printed looks like precision.
               "vintage": evidence,
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


def _taste_person(weights, loves, axis_keys):
    """Whose weights run: the ``weights`` argument, else the resolved profile.

    Extracted (S2) so ``_taste_core`` and ``_score_core`` weight the same read
    with the SAME person object rather than two that merely look alike — two
    taste numbers in one payload that disagree would be worse than either.
    ``taste_result`` reads exactly three fields off a person (``taste.weights``,
    ``taste.lovesNamed``, ``taste.antiSignals``), which is why the weights branch
    can stay the light dict it has always been.
    """
    weights = _as_dict(weights, "weights")
    if weights:
        missing_w = [k for k in axis_keys if k not in weights]
        if missing_w:
            raise UsageError("weights must cover all eight axes; missing: %s"
                            % ", ".join(missing_w))
        return {"taste": {"weights": weights, "lovesNamed": list(loves or [])}}
    return _demo_person()


def _engine_person(person):
    """A real ``Person`` for ``engine.score``, which needs an ``id`` and a
    profile version for the ScoreResult's request block where ``taste_result``
    needs only the weights. A dict person (the ``weights=`` path) is lifted into
    one carrying the identical taste block, so the engine's taste read is the
    same arithmetic over the same numbers."""
    if not isinstance(person, dict):
        return person
    from gaff_engine import elicit
    taste = person.get("taste") or {}
    return elicit.person_from_profile({
        "subject": "You", "version": 3,
        "weights": taste.get("weights") or {},
        "taste_loves_named": list(taste.get("lovesNamed") or [])})


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
    person = _taste_person(weights, loves, axis_keys)
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
# S2 — THE COMPOSED SCORE SEAM. One function every composed tool calls, so the
# tool layer reaches the real engine down exactly one path with exactly one set
# of guard rails, rather than four features each inventing a stand-in.
#
# The two guard rails, both reproduced as real wrong answers in the lab on
# 3 September 2026 against the shipped v0.1.0 package:
#
#   comps=None       -> engine.score loads the London enriched file for a
#                       subject anywhere in the UK. A Leamington Spa flat came
#                       back "fair, £1,235,000" against De Beauvoir's sales.
#   taste_model=None -> engine.score replays canonical_model(), and
#                       taste.RecordedModel keys its reads on use_images, a
#                       bool, NOT on the listing. The same Leamington flat,
#                       described as a plain 1990s block, scored taste 8.2 on
#                       De Beauvoir's reads verbatim.
#
# Both are one-line mistakes whose output is a confident wrong answer, which is
# exactly what the abstention differentiator exists to prevent.
# tests/test_score_seam.py poisons both defaults so they raise if reached.
# ---------------------------------------------------------------------------


def _score_core(listing, reads=None, weights=None, *, search=None, person=None,
                progress=None):
    """The one path from the tool layer to ``engine.score``.

    Returns ``(result, value_payload, taste_payload, comps, context)`` where
    ``result`` is a real ``ScoreResult`` — gates, composite, merged flags,
    confidence report — or ``None`` when the engine was deliberately not run.
    The two payload dicts are byte-for-byte what ``value_check`` and
    ``taste_score`` already return, so nothing composed on them changes shape.

    The order matters. Route the pool FIRST (:func:`_routed_comps`): the routing
    is the part ``engine.score`` does not have, and a subject no cache verifiably
    reaches gets the honest refusal rather than a tag priced against somewhere
    else. Then resolve who and what: the person from the weights argument or the
    profile, the search from the argument, the saved session, or
    ``session.default_search`` — each recorded in ``context`` so a payload can
    never use a sticky search or someone else's weights without saying so.

    Then the rail: **no host reads means the engine is not called at all.** There
    is no honest taste answer to give, and calling anyway is precisely how the
    De Beauvoir recording gets replayed onto a stranger's flat. The value-only
    payload is returned exactly as before.
    """
    progress = progress or _noop
    from gaff_engine import session as _session
    from gaff_engine import taste as _taste

    value_payload, _verdict, comps = _value_core(listing, progress)
    reads = _as_dict(reads, "reads")
    taste_payload = _taste_core(listing, reads, weights, progress) if reads else None

    search_obj, search_note = _session.search_in_use(search=search, listing=listing)
    context = {"profile": _session.profile_in_use(weights=_as_dict(weights, "weights")),
               "search": search_note,
               "scored": False,
               "why": None}

    if not reads:
        context["why"] = ("no axis reads supplied — you are the taste model, so "
                          "there is no honest taste score and the full engine "
                          "was not run")
        return None, value_payload, None, comps, context
    if not comps:
        context["why"] = ("no comparable sales this subject verifiably reaches, "
                          "so the full engine was not run — the value payload "
                          "carries the refusal and its hint")
        return None, value_payload, taste_payload, comps, context

    # Rebuild the host's read and weight it with the SAME person _taste_core
    # used, so the engine's taste is the same arithmetic over the same numbers.
    axis_keys = [a.value for a in _taste.AXIS_ORDER]
    axes = {k: _taste.AxisRead(float(v["score"]), str(v.get("contribution") or ""))
            for k, v in (_as_dict(reads.get("axes"), "reads.axes") or {}).items()}
    anti = reads.get("antiSignalHits")
    if anti is not None:
        anti = [(str(s), float(p), bool(f)) for s, p, f in anti]
    loves = reads.get("namedLoveHits")
    read = _taste.TasteRead(axes=axes,
                            namedLoveHits=list(loves) if loves is not None else None,
                            antiSignalHits=anti)
    # BOTH keys. RecordedModel keys on use_images, so a model carrying only the
    # image read would let the text pass fall through to whatever it can find —
    # which is how a stranger's listing inherits the golden's read.
    taste_model = _taste.RecordedModel({True: read, False: read})
    person_obj = _engine_person(person if person is not None
                                else _taste_person(weights, loves, axis_keys))

    progress("running the full engine: gates, composite, flags")
    from gaff_engine import engine as _engine
    try:
        # Forensics keeps its conservative default: RecordedForensicsModel falls
        # back to a no-vision read for an unknown listingKey, inventing neither a
        # kill nor a clearance.
        result = _engine.score(listing, person_obj, search_obj,
                               comps=comps, taste_model=taste_model)
    except Exception as exc:                       # noqa: BLE001
        # Named in the payload, never swallowed: the value and taste answers are
        # still true and worth returning, and the user is told what was lost.
        context["why"] = ("the full engine did not complete (%s: %s), so gates, "
                          "composite and flags are absent; the value and taste "
                          "answers above are unaffected"
                          % (type(exc).__name__, exc))
        return None, value_payload, taste_payload, comps, context

    context["scored"] = True
    context["composite"] = result.composite
    context["excluded"] = bool(getattr(result.rules, "excluded", False))
    return result, value_payload, taste_payload, comps, context


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
        # S5: how old that price evidence is, said beside the tag rather than
        # buried in the trace. Only when it is genuinely stale — a line printed
        # on every verdict is a line nobody reads.
        evidence = value_payload.get("vintage") or {}
        if evidence.get("stale"):
            lines.append("Evidence age: %s" % evidence["line"])

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

    result, value_payload, taste_payload, comps, context = _score_core(
        listing, reads=reads, weights=weights, progress=progress)
    verdict = getattr(result, "valueVerdict", None)

    progress("generating agent questions from the engine's uncertainty")
    questions = _viewing.agent_questions(result, listing, value=value_payload,
                                         taste=taste_payload)
    # The checklist's first and fourth sources are the SCORE's flags — rules
    # flags, forensics viewing flags and listing-field flags, merged by
    # engine.score. Handed {} it could only produce the playbook spine, which is
    # why the viewing pack read thin: not because viewing.py is thin, but
    # because it was handed an empty score. S2 supplies the real one.
    checklist = to_jsonable(_viewing.checklist_sorted(
        _viewing.generate_checklist(result if result is not None else {},
                                    # generate_checklist never reads the search;
                                    # only prepare_viewing does, for its searchRef.
                                    listing, None)))

    progress("assembling the work trace")
    work = _workings.show_work(listing,
                               verdict=verdict if verdict is not None else value_payload,
                               comps=comps, taste=taste_payload,
                               vintage=(value_payload or {}).get("vintage"),
                               flags=getattr(result, "flags", None))

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
            # Which search and whose weights produced the above, and whether the
            # full engine ran at all. A sticky search that never says it is
            # sticky, or the shipped demo's weights read as your own, are the
            # two silent failures this key exists to prevent.
            "context": context,
            "note": UNTRUSTED_LISTING_NOTE}


def show_work(fields=None, text=None, reads=None, weights=None, progress=None):
    """The working trace alone (plan section 9: every number traceable on
    demand): the same pipeline as score_listing, returned as the structured
    trace plus its narrated plain-text form."""
    progress = progress or _noop
    listing = _ingest(fields, text)
    from gaff_engine import workings as _workings
    # The same seam score_listing uses (S2), so the trace explains the same run
    # rather than a second one computed slightly differently.
    result, value_payload, taste_payload, comps, context = _score_core(
        listing, reads=reads, weights=weights, progress=progress)
    verdict = getattr(result, "valueVerdict", None)
    work = _workings.show_work(listing,
                               verdict=verdict if verdict is not None else value_payload,
                               comps=comps, taste=taste_payload,
                               vintage=(value_payload or {}).get("vintage"),
                               flags=getattr(result, "flags", None))
    work["context"] = context
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
    already compute, offered BEFORE a question fails (2b).

    The walk is ``cachemap.walk`` (F-01), shared with ``situate``; the payload's
    bytes are unchanged. M-6 is this verb also returning ``cachemap.situation``'s
    feasibility table, once there is a nation to key it by.
    """
    from gaff_engine import cachemap as _cachemap
    progress = progress or _noop
    progress("walking the cache tiers")
    return dict(_cachemap.walk(), note=_COVERAGE)


def warm(street=None, town=None, flips_town=None, progress=None):
    """The verb every cold-data error names (2a): cache one street's sales
    (street= + town=, one live Land Registry call), or build a town's
    repeat-sales dataset (flips_town=, a paced whole-town pull)."""
    progress = progress or _noop
    if flips_town and (street or town):
        raise UsageError("warm one thing at a time: street= (+town=) for sales "
                        "comps, or flips_town= for repeat-sales data")
    from gaff_engine import netgate as _netgate
    if flips_town:
        from gaff_engine import flips as _flips
        # S4: say what is about to be sent, and to whom, BEFORE going out. The
        # line is rendered from netgate's declaration, so it cannot drift from
        # what the code actually does.
        progress(_netgate.consent_line("warm", 1))
        progress("building repeat-sales data for %s (live Land Registry pull; "
                 "a whole town can take minutes)" % str(flips_town).upper())
        try:
            summary = _flips.build_town(str(flips_town), progress=progress)
        except (_flips.TownTooLargeError, _flips.FlipsFetchError) as exc:
            # The T8 cap protects scale; surface it as a clear message,
            # never a traceback.
            raise ToolError(str(exc))
        out = {"warmed": "flips", "sends": _netgate.consent_line("warm", 1)}
        out.update(summary)
        return out
    if not street:
        raise UsageError("supply street= (with town=) to cache one street's "
                        "sales, or flips_town= to build a town's repeat-sales "
                        "data")
    # F-02: no LONDON default here either, and for a harder reason than in
    # price_check. This verb SPENDS the live call. A wrong guess is not a
    # confusing answer the user can re-ask, it is a request sent to HM Land
    # Registry for a street in a city they never mentioned, and an empty cache
    # file left behind that ``street_has_sales`` then has to explain away. The
    # session's town needs no warmth to qualify — warming is what makes a town
    # warm — so it is simply the town the user last said they were looking in.
    if not (town and str(town).strip()):
        town, _nation = _session_place()
        if not town:
            raise UsageError(
                "supply town= : warm makes one live Land Registry call and "
                "this build no longer assumes LONDON, because a fetch aimed "
                "at the wrong city spends the call and caches nothing useful. "
                "Warmed towns so far: %s" % (", ".join(sorted(_comps_towns()))
                                             or "none"))
    from gaff_engine import landreg as _landreg
    progress(_netgate.consent_line("warm", 0))
    progress("fetching Land Registry sales for %s, %s" % (street, town))
    items = _landreg.fetch_street(street, town)
    progress("%d sales cached" % len(items))
    return {"warmed": "comps", "street": str(street).upper(),
            "town": str(town).upper(), "sales_cached": len(items),
            "sends": _netgate.consent_line("warm", 0),
            "hint": ("price_check street=%r town=%r now answers from this cache"
                     % (street, town)) if items else
                    ("0 items can mean no recorded sales on that street, a "
                     "misspelling, or a failed fetch (a warning names which)"),
            "source": "HM Land Registry Price Paid (Open Government Licence v3.0)"}


def situate(mode=None, nation=None, town=None, outcode=None, budget_min=None,
            budget_max=None, constraints=None, name=None, progress=None):
    """The front door: what this build can honestly answer for YOU, before any
    evidence call runs (F-01).

    Four answers at most — mode; nation plus town or outcode; budget; the
    constraint that kills — and three things done with them, in order:

    1. a :class:`Person` and a :class:`Search` are built (S1's constructors) and
       saved to the user cache, so later calls default the mode, the town and
       the budget instead of asking again;
    2. the feasibility table is computed by :func:`gaff_engine.cachemap.
       situation`, which crosses nation against what is warmed against local
       configuration — the SAME function ``coverage`` adopts at M-6, because a
       front door and a coverage verb that disagree inside one release leave the
       user no way to tell which one lied;
    3. what is warmed, the warms that would help with their cost in live calls,
       and whose taste profile is loaded, are returned beside it.

    **This tool does not refuse.** An MCP host will call it with everything at
    once or with nothing at all, and a refusal at the front door is the one
    refusal this build cannot afford. So nothing here raises ``UsageError``:
    what was not stated leaves its rows ``unknown`` and lands in
    ``still_needed``; what was stated but could not be read is dropped, named in
    ``not_understood`` with the vocabulary that would have worked, and never
    silently coerced into something the user did not say. An unreadable nation
    in particular stays ``None`` rather than becoming a guess, because the one
    thing worse than "I do not know where you are" is telling a Scottish user
    that Price Paid covers them.

    Nothing is written unless something was actually stated. A bare
    ``situate()`` from a host testing the waters must not clobber the search a
    user set up two calls ago.
    """
    progress = progress or _noop
    from gaff_engine import cachemap as _cachemap
    from gaff_engine import elicit as _elicit
    from gaff_engine import netgate as _netgate
    from gaff_engine import session as _session

    answers, stated, not_understood = {}, [], []

    def keep(key, value, label=None, parse=None):
        """Record one stated answer, or name it as unread. Never coerces."""
        if value is None or str(value).strip() == "":
            return None
        try:
            parsed = parse(value) if parse else value
        except (ValueError, TypeError) as exc:
            not_understood.append({"answer": label or key, "given": str(value),
                                   "why": str(exc)})
            return None
        answers[key] = parsed
        stated.append(label or key)
        return parsed

    resolved_mode = keep("mode", mode, parse=lambda v: _session._mode(v).value)
    resolved_nation = keep("nation", nation, parse=_session.normalise_nation)
    keep("town", town)
    keep("outcode", outcode)
    keep("budget_min", budget_min, label="budget_min", parse=_session._as_int)
    keep("budget_max", budget_max, label="budget_max", parse=_session._as_int)
    keep("name", name)

    # Constraints are validated ONE AT A TIME so a single unreadable code costs
    # only itself. session.UnknownConstraint exists for exactly this catch.
    good = []
    for raw in _session._as_constraint_list(constraints):
        try:
            _session.gate_from_constraint(raw)
        except _session.UnknownConstraint as exc:
            not_understood.append({"answer": "constraint", "given": str(raw),
                                   "why": str(exc)})
            continue
        good.append(raw)
    if good:
        answers["constraints"] = good
        stated.append("constraints")

    place = answers.get("town") or answers.get("outcode")

    progress("reading what is warmed")
    situation = _cachemap.situation(nation=resolved_nation, place=place)

    # The Search and the Person, built and saved — but only if the user actually
    # said something. Writing an empty search over a real one is the sticky-
    # wrong-search failure Q1 names, arriving through the front door.
    progress("building the search")
    search = _session.search_from_answers(answers)
    person = _elicit.person_from_answers(
        {"name": answers.get("name")} if answers.get("name") else {})
    written = None
    if stated:
        written = _session.save(search, person)

    still_needed = list(situation["still_needed"])
    if not answers.get("budget_max"):
        still_needed.append({"answer": "budget",
                             "why": "budget_max= sets the ceiling the Value "
                                    "scorer reasons about (a stretch of 5% is "
                                    "assumed above it); it is not a gate unless "
                                    "you name max_price as a constraint",
                             "blocks": []})

    rows = situation["feasibility"]
    yes = [r for r in rows if r["state"] == "yes"]
    impossible = [r for r in rows if r["state"] == "no"
                  and r["actionable"] is False]
    offers = situation["warms_offered"]
    where = situation["place"] or "an unnamed place"

    # Only a need that actually BLOCKS a row may be spoken of as one. A missing
    # budget is worth asking for and blocks nothing, and letting it into these
    # branches printed "I cannot say what I can answer for you yet: ." to an
    # Edinburgh user in the assembled tree.
    blocking = [n for n in still_needed if n["blocks"]]
    if yes:
        line1 = "Right now, for %s, I can answer from local data: %s." % (
            where, ", ".join(r["label"] for r in yes))
    elif blocking:
        line1 = ("I cannot say what I can answer for you yet: %s."
                 % "; ".join(n["why"] for n in blocking))
    else:
        line1 = ("Right now I can answer nothing about %s from local data."
                 % where)
    line2 = ("No action changes these: %s."
             % ", ".join(r["label"] for r in impossible)) if impossible else \
            "Nothing here is permanently out of reach."
    if blocking:
        line3 = "Tell me: %s." % blocking[0]["why"]
    elif offers:
        line3 = "The one call that changes most: %s (%s)." % (
            offers[0]["action"], offers[0]["calls"])
    else:
        line3 = "There is nothing to warm here."

    return {
        "situated": bool(stated),
        "you_said": {"mode": resolved_mode or "buy (assumed)",
                     "nation": resolved_nation, "town": answers.get("town"),
                     "outcode": answers.get("outcode"),
                     "budget_min": answers.get("budget_min"),
                     "budget_max": answers.get("budget_max"),
                     "constraints": answers.get("constraints") or []},
        "summary": [line1, line2, line3],
        "feasibility": rows,
        "counts": situation["counts"],
        "nation": situation["nation"],
        "still_needed": still_needed,
        "not_understood": not_understood,
        "warmed": situation["warmed"],
        "warms_offered": offers,
        "profile": _session.profile_in_use(),
        "search": {"title": search.title, "mode": search.mode.value,
                   "area": getattr(search.area, "label", None),
                   "gates": [g.code for g in search.gates],
                   "budget_max": getattr(getattr(search, "budget", None), "max",
                                         None) and search.budget.max.amount},
        "session_written": bool(written),
        "session_path": written,
        "next": line3,
        "note": _netgate.offline_note(),
    }


def _as_money(raw):
    """A CLI budget as a person types it: "500000", "£500,000", "500k", "1.35m".

    ``registry.as_int`` would refuse all but the first. ``session._as_int`` is
    the parser the constraint vocabulary already uses, so the ceiling typed at
    ``situate budget_max=1.35m`` and the one typed at ``max_price<=1.35m`` are
    read by the same code rather than by two that drift.
    """
    from gaff_engine.session import _as_int
    return _as_int(raw)


# Shared inputSchema fragments: the two ways every listing-taking tool accepts
# its subject (exactly one of the two, enforced in _ingest with a clear error).
_FIELDS_ARG = arg(
    "object",
    "Your structured read of the listing: address, postcode, beds, "
    "baths, sqft or sqm, price or rent_pcm (numbers or display "
    "strings), property_type, tenure, lease_years, description, "
    "key_features, mode ('buy'/'rent'), epc_sqft (the EPC "
    "certificate's floor area in sqft, when you have the "
    "certificate — enables the marketing-vs-EPC basis check and "
    "the works-vs-EPC agent question). All optional; omit what "
    "the listing does not state — absences are recorded honestly, "
    "never guessed.")
_TEXT_ARG = arg(
    "string",
    "The raw pasted listing text; parsed deterministically (regex, "
    "no guessing) — prefer 'fields' when you have read the listing "
    "yourself.")

# ---------------------------------------------------------------------------
# The tool surface, declared once (S3). Each register() call generates the MCP
# manifest entry a host reads, the DISPATCH entry both surfaces call, and the
# CLI's coercion table — so a tool is added in one place instead of two that
# had to agree by hand. gaff_engine.registry does the checking: a declaration
# naming an argument the function does not accept (or missing one it does)
# fails at import, not in front of a user.
#
# The descriptions below are USER-FACING TEXT and were moved verbatim from the
# hand-written literal this replaced; tests/test_registry.py pins the generated
# manifest byte-for-byte against tests/fixtures/tools_manifest_v0.json so a
# refactor can never quietly reword what a host reads.
# ---------------------------------------------------------------------------

register(
    situate,
    "START HERE. What this build can honestly answer for a user's own "
    "search, before any evidence call runs: a yes/no/unknown feasibility "
    "table across sold comps, repeat sales, £/sqft, HPI, EPC and rents, "
    "keyed by nation and by what is warmed locally; what is cached; the "
    "warms that would help and what each one sends; and whose taste "
    "profile is loaded. Saves the search so later calls inherit the mode, "
    "town and budget. Never refuses: unstated answers come back as "
    "'unknown' rows plus a named list of what is still needed. Ask the "
    "nation rather than guessing it from the town — Newport, Perth and "
    "Hamilton each exist in more than one UK nation.",
    mode=arg("string",
        "buy, rent, invest or dream. Defaults to buy.",
        enum=["buy", "rent", "invest", "dream"]),
    nation=arg("string",
        "england, wales, scotland or northern_ireland. ASK the user; "
        "never infer it from the town name.",
        enum=["england", "wales", "scotland", "northern_ireland"]),
    town=arg("string",
        "Town or city, e.g. 'LEEDS'."),
    outcode=arg("string",
        "Postcode outcode, e.g. 'N1' — use instead of, or beside, town."),
    budget_min=arg("integer",
        "Lower end of the budget (total for a purchase, £pcm for a rent).",
        coerce=_as_money),
    budget_max=arg("integer",
        "Upper end of the budget. Not a hard gate: name max_price as a "
        "constraint if the ceiling genuinely kills a home.",
        coerce=_as_money),
    constraints=arg("array",
        "The constraints that kill, each 'code>=value': min_beds, "
        "min_baths, min_sqft, min_receptions, max_price, min_price, "
        "lease_years_min, tenure_in, outdoor_present. One may be sent "
        "bare rather than in a list; an unreadable one is named back, "
        "not fatal."),
    name=arg("string",
        "Who this search is for; keeps a second person's profile separate."),
)

register(
    price_check,
    "Recent Land Registry sales on a UK street, with median "
    "price. Town-scoped. " + _COVERAGE,
    street=arg("string",
        "Street name, e.g. 'De Beauvoir Road'", required=True),
    town=arg("string",
        "Town, e.g. 'LONDON'. Optional: left out, it is taken from the "
        "one warmed town holding that street, else from the saved "
        "search. If neither places it, the tool asks rather than "
        "assuming a city."),
)

register(
    flip_stats,
    "Repeat-sales analysis for a town: uplift achieved versus "
    "what the market did anyway. " + _COVERAGE,
    town=arg("string",
        "Town name, e.g. 'LEAMINGTON SPA'", required=True),
)

register(
    read_listing,
    "Parse one property listing (your structured read, or raw "
    "pasted text) into the engine's honest Listing shape, with "
    "a completeness map of what was missing. Runs entirely "
    "locally; no data leaves the machine.",
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
    mode=arg("string",
        "Force buy/rent when the listing doesn't say.", enum=["buy", "rent"]),
)

register(
    value_check,
    "Is the asking price fair? Ingests the listing (fields or "
    "text), loads cached comparable sales, and returns the "
    "engine's value verdict: steal/fair/over tag, percent delta, "
    "fair estimate, confidence and its evidence basis. SALES "
    "only: a rental listing gets an honest error (rent verdicts "
    "are not served yet). " + _COVERAGE,
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
)

register(
    taste_score,
    "Score a listing against a taste profile. YOU (the host "
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
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
    reads=arg("object",
        "{axes: {<axis>: {score: 0-10, contribution: str}} for all "
        "eight axes, namedLoveHits?: [str], antiSignalHits?: "
        "[[signal, penalty, fatal]]}", required=True),
    weights=arg("object",
        "The eight axis weights; omitted -> the shipped demo "
        "profile's weights."),
)

register(
    score_listing,
    "The one-call verdict. Paste a listing (or an alert "
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
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
    reads=arg("object",
        "Optional taste reads, taste_score's exact contract: "
        "{axes: {<axis>: {score: 0-10, contribution: str}} for all "
        "eight axes, namedLoveHits?, antiSignalHits?}. Omit to skip "
        "taste and get value + questions only."),
    weights=arg("object",
        "The eight axis weights; omitted -> the shipped demo "
        "profile's weights."),
)

register(
    show_work,
    "Every number traceable on demand: the full working trace "
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
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
    reads=arg("object",
        "Optional taste reads (taste_score's contract) to include "
        "the taste arithmetic in the trace."),
    weights=arg("object",
        "The eight axis weights; omitted -> the shipped demo "
        "profile's weights."),
)

register(
    rent_check,
    "Is the asking rent fair? Judges a rental listing's £pcm "
    "against comparable local lets (same outcode + bed count) "
    "from a LOCAL rental pool file — honestly lower confidence "
    "than a sales verdict (asking rents, not agreed lets, and "
    "the payload says so). The package ships no pool (rental "
    "comparison data is scraped portal content, not "
    "redistributable): without a user-supplied "
    "rental_candidates.json in the user cache this returns the "
    "honest error naming exactly that.",
    fields=_FIELDS_ARG,
    text=_TEXT_ARG,
)

register(
    coverage,
    "What the local caches can answer right now: warmed comps "
    "towns with street counts, flips towns with record counts, "
    "data vintages, and which loose datasets are present. Call "
    "this to scope a question before asking it. " + _COVERAGE,
)

register(
    warm,
    "Fetch and cache open Land Registry data so cold questions "
    "become answerable: street= (+town=) caches one street's "
    "sales (one live call); flips_town= builds a town's "
    "repeat-sales dataset (a paced whole-town pull, minutes; "
    "very large towns are refused by a record cap). The only "
    "tool that touches the network.",
    street=arg("string",
        "Street to cache sales for"),
    town=arg("string",
        "Town for street=. Optional only if a saved search names one; "
        "this call spends a live request, so it is never aimed at a "
        "guessed city."),
    flips_town=arg("string",
        "Town to build repeat-sales data for"),
)


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
    say('  gaff price_check street="Your Street" town="YOUR TOWN"')
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
    # S3: every CLI argument arrives as a string. The registry says which ones
    # the tool means as an int, a float, a bool or a comma list, so the tool
    # itself never has to re-parse the shell's text. A bad value is an
    # INVOCATION error (exit 2), not a data one.
    try:
        args = coerce_cli_args(name, args)
    except ValueError as exc:
        print("%s: %s" % (name, exc), file=sys.stderr)
        return 2
    ok, payload = safe_call(name, fn, args, progress=_cli_progress)
    if not ok:
        print(payload["error"], file=sys.stderr)
        return 2 if payload.get("usage") else 1
    print(json.dumps(payload, indent=1))
    # A soft error (an honest {error, hint} dict) still prints — the hint is
    # the point — but the exit code must tell scripts the question was not
    # answered (2a: CLI tool errors exit non-zero).
    return 1 if isinstance(payload, dict) and "error" in payload else 0
