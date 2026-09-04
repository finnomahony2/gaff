"""The one cache walk, and the feasibility table that reads it (F-01, M-6).

Why this module exists
----------------------
Two things were about to be written twice.

The **cache walk** was inlined in ``tools.coverage``: forty lines that open every
comps directory in both tiers, decide which cached streets actually hold sales,
and count what is there. ``situate`` needs the same answer, and ``area_brief``
(F-03) will need it a third time. A second copy is not a style problem here — the
3 Sep accident was exactly a second copy of "does this street count", and it
overstated London's coverage by three streets in a released build. So the walk
moves here whole, and ``tools`` imports the names it used to define.

The **feasibility table** is worse, because it is user-facing. ``situate`` is the
front door and ``coverage`` is the "what have you got" verb; if they answer the
same question from two functions they will disagree inside one release, and the
user has no way to tell which lied. :func:`situation` is that one function. F-01
calls it today; M-6 is coverage adopting it, and is half an evening precisely
because this is where the answer lives.

The three states, and what each one means
-----------------------------------------
* ``yes`` — answerable **now**, from data already on this machine.
* ``no``  — not answerable now. Every ``no`` carries ``unlocked_by``: either the
  action that changes it, or the plain sentence saying no action exists. A "no"
  with nothing after it is the failure this table is built to avoid.
* ``unknown`` — cannot be decided from what the user has stated. This is the
  honest state for partial input, and it is why the front door never returns a
  usage error: an unanswered question becomes a row, not a refusal.

The three inputs the table crosses
-----------------------------------
1. **Nation.** England and Wales have HM Land Registry Price Paid and the EPC
   register; Scotland and Northern Ireland have neither, and nothing warms them.
   All four have UK HPI. Nation is asked, never inferred from a town name —
   Newport, Perth and Hamilton each exist in more than one nation. The single
   exception is evidential rather than nominal and is spelled out in
   :func:`resolve_nation`.
2. **Warmed state**, from the walk below.
3. **Local configuration**: whether an EPC token resolves, and whether a rental
   pool is present and where its lets actually are.

Offline by construction. Every function here reads local files, the environment,
and (for the EPC token *source*, never its value) the keychain. Nothing in this
module opens a socket, which is what lets ``situate`` stay out of
``netgate.NETWORK_VERBS`` while ``tests/test_netgate.py`` measures it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from gaff_engine import paths

#: A cached street envelope always writes ``"count": len(items)`` immediately
#: before ``items``, so the head of the file settles whether it holds any sales
#: without parsing the whole thing. Reading 512 bytes per file is 0.46 ms across
#: London's 28 streets; a full parse is 8.5 ms — on an 8 ms value_check, the
#: difference between a correctness check you can afford and one you cannot.
_EMPTY_ENVELOPE = re.compile(rb'"count"\s*:\s*0\s*[,}]')

#: ``<region>_<YYYY-MM>.json`` — the UK HPI cache's file name.
_HPI_FILE = re.compile(r"^(?P<region>.+)_(?P<month>\d{4}-\d{2})\.json$")

#: The six evidence types the table answers for, in the order a user meets them.
EVIDENCE = ("sold_comps", "flips", "price_per_sqft", "hpi", "epc", "rent_pool")

#: What each key is, in words a user reads.
LABELS = {
    "sold_comps": "recorded sold prices on comparable homes",
    "flips": "repeat-sales analysis (what resellers achieved versus the market)",
    "price_per_sqft": "price per square foot on comparable sales",
    "hpi": "UK House Price Index adjustment (older sales restated)",
    "epc": "EPC certificates (floor area, and the energy read)",
    "rent_pool": "asking-rent comparison against a local pool",
}

#: Said once, quoted twice: Price Paid and the EPC register share a footprint,
#: and the reason a Scottish or Northern Irish answer is a no rather than a
#: not-yet. Neither is a warm away; neither dataset exists to warm.
_NO_PRICE_PAID = {
    "scotland": "nothing in this build: sold prices in Scotland are held by "
                "Registers of Scotland, which does not publish them as open "
                "data. No warm reaches them.",
    "northern_ireland": "nothing in this build: sold prices in Northern Ireland "
                        "are held by Land & Property Services, which does not "
                        "publish them as open data. No warm reaches them.",
}
_NO_EPC_REGISTER = {
    "scotland": "nothing in this build: Scottish EPCs are held on the Scottish "
                "EPC Register, a different service this build does not read.",
    "northern_ireland": "nothing in this build: Northern Irish EPCs are held "
                        "separately from the England and Wales register this "
                        "build reads.",
}


# ---------------------------------------------------------------------------
# The walk. Moved from tools.coverage / tools._comps_towns unchanged, so there
# is exactly one rule for "does this cached street count as coverage".
# ---------------------------------------------------------------------------

def street_has_sales(path: str) -> bool:
    """False when this cached street holds no recorded sales.

    A street fetched successfully that came back with nothing — a misspelling, a
    road with no transactions, a new-build estate — is cached so it is not
    fetched again, and that is right. What is NOT right is counting it as
    coverage: ``_resolve_pool_town`` routes a subject by street uniqueness, so a
    zero-sale file makes a street name "belong" to a town on the strength of a
    file containing nothing, and ``coverage`` reports a street the user cannot
    get an answer from. The shipped warm cache carries three of these.
    """
    try:
        with open(path, "rb") as fh:
            return _EMPTY_ENVELOPE.search(fh.read(512)) is None
    except OSError:
        return False              # unreadable is not coverage either


def comps_map(include_empty: bool = False) -> Dict[str, Set[str]]:
    """town-slug -> set of cached street slugs that actually hold sales.

    ``include_empty`` returns the raw listing instead, for ``coverage``, which
    reports the fetched-but-empty streets separately rather than losing them.
    """
    towns: Dict[str, Set[str]] = {}
    for base in paths.read_candidates("comps"):
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            tdir = os.path.join(base, d)
            if not os.path.isdir(tdir):
                continue
            towns.setdefault(d, set()).update(
                f[:-5] for f in os.listdir(tdir)
                if f.endswith(".json")
                and (include_empty or street_has_sales(os.path.join(tdir, f))))
    return towns


def empty_streets(town_slug: str) -> List[str]:
    """The street slugs cached for this town that hold no sales, sorted."""
    out = set()
    for base in paths.read_candidates("comps"):
        tdir = os.path.join(base, town_slug)
        if not os.path.isdir(tdir):
            continue
        out.update(f[:-5] for f in os.listdir(tdir)
                   if f.endswith(".json")
                   and not street_has_sales(os.path.join(tdir, f)))
    return sorted(out)


def comps_detail() -> Dict[str, Dict[str, Any]]:
    """town-slug -> ``{"streets": set, "empty": set, "fetchedAt": str|None}``.

    One walk, both tiers, user cache first. ``fetchedAt`` is one file's vintage
    per town, taken from the first tier that has a street with sales in it: per
    file dates cost a read each, and every street in a town is warmed in the
    same pass, so the first is representative and the cheap answer is the honest
    one (``tools._cache_fetched_at`` reasons the same way).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for base in paths.read_candidates("comps"):
        if not os.path.isdir(base):
            continue
        for town in os.listdir(base):
            tdir = os.path.join(base, town)
            if not os.path.isdir(tdir):
                continue
            streets, empty = [], []
            for f in sorted(os.listdir(tdir)):
                if not f.endswith(".json"):
                    continue
                (streets if street_has_sales(os.path.join(tdir, f))
                 else empty).append(f[:-5])
            rec = out.setdefault(town, {"streets": set(), "empty": set(),
                                        "fetchedAt": None})
            rec["streets"].update(streets)
            rec["empty"].update(empty)
            if rec["fetchedAt"] is None and streets:
                try:
                    with open(os.path.join(tdir, streets[0] + ".json"),
                              encoding="utf-8") as fh:
                        rec["fetchedAt"] = json.load(fh).get("fetchedAt")
                except (OSError, ValueError):
                    pass
    return out


def flips_counts() -> Dict[str, int]:
    """TOWN (as the records spell it) -> repeat-sale record count."""
    from gaff_engine import flips as _flips
    counts: Dict[str, int] = {}
    for r in _flips.load_flips():
        t = (r.get("town") or "").upper()
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def hpi_months() -> Dict[str, List[str]]:
    """UK HPI region slug -> the sorted months cached for it, both tiers.

    The feasibility table needs this and ``coverage`` does not: coverage reports
    that an ``hpi`` directory exists, which answers "is there any" and not "can
    you adjust MY town's sales", the question the front door is actually asked.
    """
    out: Dict[str, Set[str]] = {}
    for base in paths.read_candidates("hpi"):
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            m = _HPI_FILE.match(name)
            if m:
                out.setdefault(m.group("region"), set()).add(m.group("month"))
    return {region: sorted(months) for region, months in out.items()}


def datasets() -> List[str]:
    """The loose datasets present, in the order ``coverage`` has always listed
    them. Kept here so the two verbs cannot drift on what "present" means."""
    from gaff_engine.tools import _demo_profile_path
    out = []
    if paths.data_file("comps_enriched.json"):
        out.append("comps_enriched.json")
    if paths.read_path("hpi"):
        out.append("hpi")
    if paths.read_path("epc"):
        out.append("epc")
    if _demo_profile_path():
        out.append("profile.json")
    return out


def walk() -> Dict[str, Any]:
    """What the caches hold, in the shape ``coverage`` returns (minus its note).

    ``coverage`` is now this function plus one key. The payload's bytes are
    unchanged on purpose: it is user-facing, and an extraction that reworded it
    would be a feature wearing a refactor's clothes.
    """
    comps = comps_detail()
    return {
        "comps_towns": {
            t: dict({"streets": len(v["streets"]), "fetchedAt": v["fetchedAt"]},
                    **({"fetchedButEmpty": sorted(v["empty"]),
                        "emptyNote": "fetched successfully and HM Land Registry "
                                     "holds no sales for them; they are not "
                                     "coverage and do not route a listing"}
                       if v["empty"] else {}))
            for t, v in sorted(comps.items())},
        "flips_towns": dict(sorted(flips_counts().items())),
        "datasets": datasets(),
    }


# ---------------------------------------------------------------------------
# Local configuration — the third input, and the only one that is neither a
# nation nor a cache.
# ---------------------------------------------------------------------------

def epc_token_present() -> bool:
    """Whether a token would resolve. Never reads or returns the value itself.

    ``doctor._token_source`` renders the same fact as a line for a human; this
    is the boolean the table crosses. Both go through ``paths``, so the
    resolution order stays in one place.
    """
    from gaff_engine import doctor
    return not doctor._token_source().startswith("none found")


def rent_pool_shape() -> Dict[str, Any]:
    """What the local rental pool actually is: how many lets, and where.

    Computed, never assumed. The pool is a file the *user* supplies (the package
    ships none — portal content is not redistributable), so "it is all inner
    London" is true of the pool on this machine and must be re-derived for the
    next one.
    """
    from gaff_engine import rent
    from gaff_engine.tools import _LONDON_POSTCODE_AREAS
    try:
        pool = rent.load_rent_pool()
    except (FileNotFoundError, OSError, ValueError):
        return {"present": False, "lets": 0, "outcodes": [], "areas": [],
                "all_london": False, "path": None}
    outcodes, areas = set(), set()
    for row in pool:
        code = str(row.get("outcode") or "").strip().upper()
        if not code:
            continue
        outcodes.add(code)
        m = re.match(r"([A-Z]{1,2})\d", code)
        if m:
            areas.add(m.group(1))
    return {"present": True, "lets": len(pool), "outcodes": sorted(outcodes),
            "areas": sorted(areas),
            "all_london": bool(areas) and areas <= _LONDON_POSTCODE_AREAS,
            "path": paths.data_file("rental_candidates.json")}


# ---------------------------------------------------------------------------
# Nation.
# ---------------------------------------------------------------------------

def resolve_nation(nation: Optional[str], place: Optional[str]) -> Dict[str, Any]:
    """Which nation's data rules apply, and how we know.

    Nation is **asked**, never inferred from a town name: Newport, Perth and
    Hamilton each exist in more than one of the four, and a table that guessed
    would tell an Edinburgh buyer that Price Paid covers them.

    There is one inference, and it is evidential rather than nominal. If the
    place is already warmed in the Price Paid comps or flips caches, then that
    place is in England or Wales — not because of how its name reads, but
    because Price Paid holds no other country's sales. That settles every
    nation-gated row here, since Price Paid and the EPC register share exactly
    that footprint. It does not settle England versus Wales, and nothing in this
    table turns on that difference; anything that ever does must ask.

    Returns ``{"nation", "england_or_wales", "source", "note"}``, where
    ``england_or_wales`` is ``True`` / ``False`` / ``None`` for unknown.
    """
    if nation:
        return {"nation": nation, "england_or_wales": nation in ("england", "wales"),
                "source": "stated", "note": None}
    if place and is_warmed(place):
        return {"nation": None, "england_or_wales": True,
                "source": "inferred_from_cache",
                "note": "you did not state a nation. %s is already in this "
                        "build's HM Land Registry Price Paid cache, and Price "
                        "Paid holds England and Wales only — so the answers "
                        "below are the England-and-Wales ones. Say the nation "
                        "if that is wrong." % _display(place)}
    return {"nation": None, "england_or_wales": None, "source": "unstated",
            "note": None}


# ---------------------------------------------------------------------------
# Place helpers. A "place" is whatever the user typed: a town, or an outcode.
# ---------------------------------------------------------------------------

def _display(place: Any) -> str:
    return str(place).strip().upper()


def town_slug(place: Any) -> Optional[str]:
    """A stated place -> the comps cache's directory name for it."""
    if not place:
        return None
    from gaff_engine.landreg import _slug           # E9: import, never copy
    return _slug(str(place))


def is_warmed(place: Any) -> bool:
    """Is this place present in either Price Paid cache (comps or flips)?"""
    slug = town_slug(place)
    if not slug:
        return False
    if comps_map().get(slug):
        return True
    return _display(place) in flips_counts()


def _outcode(place: Any) -> Optional[str]:
    """The stated place as an outcode, when it reads like one ("N1", "EC1V")."""
    text = _display(place)
    return text if re.match(r"^[A-Z]{1,2}\d[A-Z\d]?$", text) else None


# ---------------------------------------------------------------------------
# The table. One function, because the front door and the coverage verb
# answering this question differently is the failure mode M-6 names.
# ---------------------------------------------------------------------------

def _row(evidence: str, state: str, why: str,
         unlocked_by: Optional[str] = None,
         actionable: Optional[bool] = None) -> Dict[str, Any]:
    """One line of the table.

    ``unlocked_by`` is mandatory on anything that is not a ``yes``: the action
    that changes it, or the sentence saying no action exists. A "no" with
    nothing after it is a dead end wearing a fact's clothes, and
    ``tests/test_cachemap.py`` refuses to let one through.

    ``actionable`` says WHICH of those two ``unlocked_by`` is, as a flag rather
    than as a phrase a reader has to parse. A caller that sniffed the sentence
    for "nothing in this build" would silently start lying the day one of these
    is reworded — and every one of them is user-facing text.
    """
    if state != "yes" and not unlocked_by:
        raise ValueError("%s is %r with nothing after it" % (evidence, state))
    if state != "yes" and actionable is None:
        raise ValueError("%s must say whether %r is an action or a dead end"
                         % (evidence, unlocked_by))
    return {"evidence": evidence, "label": LABELS[evidence], "state": state,
            "why": why, "unlocked_by": unlocked_by, "actionable": actionable}


_ASK_NATION = ("state the nation: nation=england, wales, scotland or "
               "northern_ireland")
_ASK_PLACE = "name a town or an outcode"


def _sold_comps(nat, place, rec) -> Dict[str, Any]:
    ew, nation = nat["england_or_wales"], nat["nation"]
    if ew is None:
        return _row("sold_comps", "unknown",
                    "which nation you are in decides whether an open sold-price "
                    "dataset exists at all", _ASK_NATION, True)
    if ew is False:
        return _row("sold_comps", "no",
                    "HM Land Registry Price Paid covers England and Wales only",
                    _NO_PRICE_PAID[nation], False)
    if not place:
        return _row("sold_comps", "unknown",
                    "Price Paid covers you, but with no town or outcode stated "
                    "I cannot say what is cached", _ASK_PLACE, True)
    if rec and rec["streets"]:
        extra = (", and %d more that came back with no sales"
                 % len(rec["empty"])) if rec["empty"] else ""
        when = (" (fetched %s)" % rec["fetchedAt"]) if rec["fetchedAt"] else ""
        return _row("sold_comps", "yes",
                    "%d street(s) cached for %s%s%s"
                    % (len(rec["streets"]), _display(place), extra, when))
    return _row("sold_comps", "no",
                "Price Paid covers %s, but nothing is cached for it here"
                % _display(place),
                "warm street=<a street in %s> town=%s"
                % (_display(place), _display(place)), True)


def _flips(nat, place, count) -> Dict[str, Any]:
    ew, nation = nat["england_or_wales"], nat["nation"]
    if ew is None:
        return _row("flips", "unknown",
                    "repeat-sales analysis is built from Price Paid, so the "
                    "nation decides whether it can exist", _ASK_NATION, True)
    if ew is False:
        return _row("flips", "no",
                    "repeat-sales analysis is built from HM Land Registry Price "
                    "Paid, which covers England and Wales only",
                    _NO_PRICE_PAID[nation], False)
    if not place:
        return _row("flips", "unknown",
                    "no town stated, so I cannot say whether a repeat-sales "
                    "dataset has been built", _ASK_PLACE, True)
    if count:
        return _row("flips", "yes",
                    "%d repeat-sale record(s) for %s, each carrying what the "
                    "market did over the same months"
                    % (count, _display(place)))
    return _row("flips", "no",
                "no repeat-sales dataset has been built for %s" % _display(place),
                "warm flips_town=%s" % _display(place), True)


def _price_per_sqft(nat, place, slug) -> Dict[str, Any]:
    ew, nation = nat["england_or_wales"], nat["nation"]
    # Said the same way in three branches: nothing in this build joins floor
    # areas to newly warmed sales, and the subject's own is a caller argument.
    subject_route = ("nothing in this build joins EPC floor areas to newly "
                     "warmed sales (BACKLOG F-04). The SUBJECT's own £/sqft "
                     "does work now: pass epc_sqft=<the certificate's floor "
                     "area in sqft> to value_check or score_listing")
    if ew is None:
        return _row("price_per_sqft", "unknown",
                    "£/sqft on comparable sales needs both Price Paid and the "
                    "EPC register, and the nation decides whether either "
                    "applies", _ASK_NATION, True)
    if ew is False:
        return _row("price_per_sqft", "no",
                    "£/sqft on comparable sales needs floor areas from the "
                    "England and Wales EPC register joined to Price Paid sales, "
                    "and neither covers you", _NO_EPC_REGISTER[nation], False)
    enriched = paths.data_file("comps_enriched.json")
    if not enriched:
        return _row("price_per_sqft", "no",
                    "no enriched comp set is present, and warming a street "
                    "fetches prices without floor areas", subject_route, False)
    matched, total, label = None, None, None
    try:
        with open(enriched, encoding="utf-8") as fh:
            blob = json.load(fh)
        matched, total, label = (blob.get("matched"), blob.get("count"),
                                 blob.get("listing"))
    except (OSError, ValueError):
        pass
    have = ("floor areas on %s of %s sales" % (matched, total)
            if matched and total else "floor areas on some sales")
    if slug == "london":
        return _row("price_per_sqft", "yes",
                    "the enriched comp set carries %s%s"
                    % (have, " (%s)" % label if label else ""))
    return _row("price_per_sqft", "no",
                "the only enriched comp set here is London's%s, so nothing "
                "joins floor areas to sales in %s"
                % (" (%s)" % label if label else "", _display(place)),
                subject_route, False)


def _hpi(nat, place, slug, months_by_region) -> Dict[str, Any]:
    """UK HPI is the one row no nation gates: all four nations have an index.

    The nation still decides the ACTION, though, and that is the Edinburgh
    acceptance criterion. The only path that fetches HPI months is the
    repeat-sales pass inside ``warm flips_town=``, which is a Price Paid pull —
    so offering it to a Scottish or Northern Irish user would be offering a warm
    that cannot work, which is the one thing the front door must never do.
    """
    if not place:
        return _row("hpi", "unknown",
                    "no town stated, so I cannot say which UK HPI region "
                    "applies", _ASK_PLACE, True)
    if _outcode(place):
        return _row("hpi", "unknown",
                    "%s is an outcode, and UK HPI regions are boroughs and "
                    "districts, not postcodes" % _display(place),
                    "name the borough or district as well as the outcode", True)
    from gaff_engine import hpi as _hpi_mod
    region = _hpi_mod.region_for({"district": str(place)})
    if not region:
        return _row("hpi", "no",
                    "%s cannot be placed on a UK HPI region, and this build "
                    "refuses to guess one — an unplaced subject is adjusted by "
                    "nothing rather than by London" % _display(place),
                    "name the local authority district (nothing else reaches "
                    "the index)", True)
    months = months_by_region.get(region) or []
    if months:
        return _row("hpi", "yes",
                    "%d month(s) of the %s series are cached (%s to %s)"
                    % (len(months), region, months[0], months[-1]))
    cold = ("%s maps to the %s series and no month of it is cached, so sales "
            "stand in the money of their own dates — that costs the adjustment, "
            "not the verdict" % (_display(place), region))
    if nat["england_or_wales"] is False:
        # No callable command in this sentence, deliberately. The only pass
        # that fetches HPI months is a Price Paid pull, and printing its
        # invocation to a user it cannot serve is how a dead end reads as an
        # offer.
        return _row("hpi", "no", cold,
                    "nothing in this build: no tool fetches a single HPI month, "
                    "and the only pass that does fetch them is a repeat-sales "
                    "pull from HM Land Registry Price Paid, which holds no "
                    "sales for your nation", False)
    return _row("hpi", "no", cold,
                "warm flips_town=%s — its repeat-sales pass fetches the UK HPI "
                "months for the districts of that town's paired sales. No tool "
                "fetches a single month on its own." % _display(place), True)


def _epc(nat, token_present) -> Dict[str, Any]:
    ew, nation = nat["england_or_wales"], nat["nation"]
    if ew is None:
        return _row("epc", "unknown",
                    "the EPC register this build reads covers England and "
                    "Wales, so the nation decides whether it applies",
                    _ASK_NATION, True)
    if ew is False:
        return _row("epc", "no",
                    "the EPC register this build reads covers England and "
                    "Wales only", _NO_EPC_REGISTER[nation], False)
    return _row("epc", "no",
                "no tool in this build queries the EPC register: the "
                "enrichment that joins certificates to sales ships, and nothing "
                "calls it. An EPC token %s here."
                % ("does resolve" if token_present else "does not resolve"),
                "read the certificate yourself and pass epc_sqft=<its floor "
                "area in sqft>; the subject's £/sqft and the marketing-versus-"
                "EPC basis check both run off it. Wiring the lookup to a tool "
                "is BACKLOG F-04.", True)


def _rent_pool(place, slug, shape) -> Dict[str, Any]:
    supply = ("supply your own rental_candidates.json in the user cache%s"
              % (" (%s)" % os.path.join(paths.user_cache_dir(),
                                        "rental_candidates.json")))
    if not shape["present"]:
        return _row("rent_pool", "no",
                    "no rental pool file is present, and the package ships "
                    "none — portal listings are not redistributable", supply, True)
    where = ", ".join(shape["areas"]) or "no readable outcodes"
    where_n = "area" if len(shape["areas"]) == 1 else "areas"
    # The hazard worth saying out loud: rent_check does not refuse an
    # out-of-area subject. Below three same-bed lets in the subject's own
    # outcode, rent.rent_cohort widens to every same-bed let in the pool.
    widens = ("below three same-bed lets in your own outcode, rent_check "
              "widens its cohort to every same-bed let in the pool")
    oc = _outcode(place)
    if oc:
        here = [c for c in shape["outcodes"] if c == oc]
        if here:
            return _row("rent_pool", "yes",
                        "the pool holds %d asking rents and %s is one of its "
                        "outcodes; %s" % (shape["lets"], oc, widens))
        return _row("rent_pool", "no",
                    "the pool's %d asking rents carry no %s let (its outcodes "
                    "are %s), and %s — so a verdict here would price you "
                    "against those areas"
                    % (shape["lets"], oc, ", ".join(shape["outcodes"]), widens),
                    supply, True)
    if not place:
        return _row("rent_pool", "unknown",
                    "no town or outcode stated; the pool holds %d asking rents "
                    "across %s" % (shape["lets"], where), _ASK_PLACE, True)
    if slug == "london" and shape["all_london"]:
        return _row("rent_pool", "yes",
                    "the pool holds %d asking rents and every one is in a "
                    "Greater London postcode area (%s); name your outcode for "
                    "a like-for-like cohort" % (shape["lets"], where))
    return _row("rent_pool", "no",
                "every one of the pool's %d asking rents is in postcode %s "
                "%s, and you named %s; %s — so a verdict here would price you "
                "against those areas"
                % (shape["lets"], where_n, where, _display(place), widens),
                supply, True)


def warm_offer(street: Any, town: Any,
               england_or_wales: Optional[bool] = None) -> Dict[str, Any]:
    """The offer to warm ONE named street, rendered from S4's declaration.

    F-02's return-an-offer pattern. An MCP tool cannot prompt mid-call, so a
    cold street does not fetch and does not ask: it returns THIS, the host puts
    it to the user, and the user's yes becomes a ``warm`` call. The shape is
    :func:`_warms_offered`'s, from the same declaration, because a cold street
    is now offered a warm in three places — situate's table, ``price_check``,
    and the value path's refusal — and three hand-written offers are three
    chances to promise a call the fetcher does not actually make.
    """
    from gaff_engine import netgate
    declared = netgate.verbs("warm")
    if not declared:                     # no declaration, no offer to make
        return {}
    if england_or_wales is False:
        return {}                        # nothing to fetch; see the caveat below
    d = declared[0]
    offer = {"action": 'warm street="%s" town="%s"' % (_display(street),
                                                       _display(town)),
             "calls": d["calls"],
             "what_it_sends": "%s, to %s (%s)" % (d["sends"], d["to"],
                                                  d["host"]),
             "licence": d["licence"],
             "unlocks": ["sold_comps"],
             "consent": netgate.consent_line("warm", 0)}
    if england_or_wales is not True:
        # Three states, and the unknown one is not the yes. Nation is asked,
        # never inferred from a town name (Newport, Perth and Hamilton each
        # exist in more than one nation), so an unwarmed town cannot be called
        # English — but nor may the offer imply the call will return something.
        # F-01 printed a warm invocation that could not work for a Scottish
        # row; the same mistake here is an offer with an unstated condition on
        # it. State the condition.
        offer["conditional_on"] = (
            "%s being in England or Wales — HM Land Registry Price Paid holds "
            "no Scottish or Northern Irish sales, and the call would come back "
            "empty for either" % _display(town))
        offer["unlocks"] = ["sold_comps (if the nation has Price Paid)"]
    return offer


def _warms_offered(nat, place, rec, flip_count) -> List[Dict[str, Any]]:
    """The warms that would help, with their cost, rendered from S4's list.

    Read out of ``netgate.NETWORK_VERBS`` rather than retyped, so what the front
    door tells a user it will send is the same declaration the fetcher shows
    before it goes out, and neither can drift from the other.
    """
    from gaff_engine import netgate
    if nat["england_or_wales"] is not True or not place:
        return []                 # nothing to warm, or nowhere to warm it
    town = _display(place)
    declared = netgate.verbs("warm")
    offers = []
    for index, action, unlocks, skip in (
            (0, "warm street=<a street in %s> town=%s" % (town, town),
             ["sold_comps"], False),
            (1, "warm flips_town=%s" % town, ["flips", "hpi"], bool(flip_count))):
        if skip or index >= len(declared):
            continue
        d = declared[index]
        offers.append({"action": action, "calls": d["calls"],
                       "what_it_sends": "%s, to %s (%s)"
                                        % (d["sends"], d["to"], d["host"]),
                       "licence": d["licence"], "unlocks": unlocks})
    if rec and rec["streets"] and offers and offers[0]["unlocks"] == ["sold_comps"]:
        offers[0]["action"] = ("warm street=<another street in %s> town=%s"
                               % (town, town))
    return offers


def situation(nation: Optional[str] = None,
              place: Optional[str] = None) -> Dict[str, Any]:
    """The feasibility table for a nation and a place, plus what is warmed.

    **The** function of M-6: ``situate`` calls it as its front door, and
    ``coverage`` adopts it rather than growing a second opinion. Everything it
    returns is derived — from the cache walk, from ``netgate``'s declarations,
    from the pool file actually on disk — so no sentence here can outlive the
    code that made it true.

    Partial input is normal, not an error. Anything unstated leaves its rows
    ``unknown`` and lands in ``still_needed``.
    """
    nat = resolve_nation(nation, place)
    slug = town_slug(place)
    detail = comps_detail()
    rec = detail.get(slug) if slug else None
    flip_count = flips_counts().get(_display(place)) if place else None
    months_by_region = hpi_months()
    shape = rent_pool_shape()

    rows = [
        _sold_comps(nat, place, rec),
        _flips(nat, place, flip_count),
        _price_per_sqft(nat, place, slug),
        _hpi(nat, place, slug, months_by_region),
        _epc(nat, epc_token_present()),
        _rent_pool(place, slug, shape),
    ]

    still_needed = []
    if nat["england_or_wales"] is None:
        still_needed.append({"answer": "nation", "why": _ASK_NATION,
                             "blocks": [r["evidence"] for r in rows
                                        if r["unlocked_by"] == _ASK_NATION]})
    if not place:
        still_needed.append({"answer": "town or outcode", "why": _ASK_PLACE,
                             "blocks": [r["evidence"] for r in rows
                                        if r["unlocked_by"] == _ASK_PLACE]})

    return {
        "place": _display(place) if place else None,
        "nation": nat,
        "feasibility": rows,
        "warmed": {
            "this_place": ({"streets": len(rec["streets"]),
                            "fetchedButEmpty": len(rec["empty"]),
                            "fetchedAt": rec["fetchedAt"]} if rec else None),
            "flips_records": flip_count,
            "comps_towns": {t: len(v["streets"]) for t, v in sorted(detail.items())
                            if v["streets"]},
            "flips_towns": dict(sorted(flips_counts().items())),
            "hpi_regions": sorted(months_by_region),
            "rental_pool_lets": shape["lets"] if shape["present"] else 0,
        },
        "warms_offered": _warms_offered(nat, place, rec, flip_count),
        "still_needed": still_needed,
        "counts": {"yes": sum(1 for r in rows if r["state"] == "yes"),
                   "no": sum(1 for r in rows if r["state"] == "no"),
                   "unknown": sum(1 for r in rows if r["state"] == "unknown")},
    }


__all__ = [
    "EVIDENCE", "LABELS",
    "street_has_sales", "comps_map", "empty_streets", "comps_detail",
    "flips_counts", "hpi_months", "datasets", "walk",
    "epc_token_present", "rent_pool_shape",
    "resolve_nation", "town_slug", "is_warmed", "situation", "warm_offer",
]
