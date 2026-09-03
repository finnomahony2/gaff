"""show_work — the full working trace behind a scored listing (BACKLOG §2).

The 29 Aug outside review's framing: address and entity resolution is a
user-facing TRUST component, not plumbing. The breakdowns this module surfaces
all exist already — completeness maps from ingest, anchor labels and category
exclusions from the value verdict's basis, axis rows from the taste result —
but they arrive scattered across payloads a user would have to know to read.
This module is presentation only: it assembles those existing structures into
one trace and narrates it. It computes NOTHING new — every number shown is
either read straight off an input or is the same arithmetic the verdict
already performed, re-displayed so the reader can check it (the §5.7 rule-2
spirit: the breakdown IS the arithmetic, not a summary of it).

Honesty rules this file holds:

* **Never claim a finer address match than the engine made.** Comp matching is
  street+town level (landreg slugs); the engine has never resolved the
  subject's own building, and the trace says so instead of implying an exact
  match. EPC lookup of the SUBJECT does not happen in this pipeline either —
  the trace reports whether a caller supplied an EPC-derived area, not a
  lookup that never ran.
* **Absences are stated, not skipped.** A missing verdict, an unscored taste,
  an empty comp pool each get an explicit line, because a trace with silent
  gaps reads as a trace with nothing wrong.

Pure and stdlib-only: dicts in, a plain dict out, ``render_text`` for the
narrated form. Duck-typed inputs (schema objects or the tool-layer payload
dicts alike), following the codebase's ``_g`` accessor idiom.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Import, never copy (E9): the subject/comp readers are value.py's own, so the
# trace reads a listing exactly the way the verdict did.
from gaff_engine.value import (
    _comp_category, _is_same_street, _subject_street,
    subject_ask, subject_epc_sqft, subject_sqft,
)


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute-or-key among ``names`` (a dotted name walks in)."""
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            if cur is None:
                ok = False
                break
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            elif hasattr(cur, part):
                cur = getattr(cur, part)
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def _money(v: Any) -> str:
    return "{:,}".format(int(v))


# ---------------------------------------------------------------------------
# Section builders. Each reads existing structures; none computes a new fact.
# ---------------------------------------------------------------------------

def _area_evidence(listing: Any, comps: List[Any]) -> Optional[str]:
    """The geographic fact that EARNS an "area" (nearby streets) claim, or None.

    WHY: with zero same-street comps, "matched at AREA level" used to be
    asserted for ANY non-empty pool — including the shipped London comps
    against a subject in a different town entirely, which is precisely the
    overclaim this trust component exists to expose. "Nearby" must be read off
    the pool, not assumed from its existence: the subject's outcode appearing
    among the comp postcodes is the strongest available check, the subject's
    display naming a comp town the fallback. Both are re-reads of data the
    pool already carries; nothing new is computed.
    """
    comp_outs, comp_towns = set(), set()
    for c in comps:
        pc = _g(c, "address.postcode", "postcode")
        if pc:
            comp_outs.add(str(pc).split()[0].upper())
        t = _g(c, "address.town", "town")
        if t:
            comp_towns.add(str(t).strip().upper())
    outcode = _g(listing, "address.outcode")
    if outcode:
        if str(outcode).upper() in comp_outs:
            return "comps share the subject's outcode (%s)" % str(outcode).upper()
        if comp_outs:
            # A stated outcode matching NO comp is positive evidence of
            # distance, not merely absence of evidence.
            return None
    disp = str(_g(listing, "address.display") or "").upper()
    for t in sorted(comp_towns):
        if t and re.search(r"\b%s\b" % re.escape(t), disp):
            return ("comps are in the subject's town (%s) — street-level "
                    "proximity unverified" % t.title())
    return None


def _address_section(listing: Any, comps: Optional[List[Any]],
                     verdict: Any) -> Dict[str, Any]:
    street = _subject_street(listing)
    postcode = _g(listing, "address.postcode")
    outcode = _g(listing, "address.outcode")
    same_street = sum(1 for c in (comps or []) if _is_same_street(c, listing))
    area_evidence = None
    if not comps:
        level = "none"
    elif same_street:
        level = "street"
    else:
        area_evidence = _area_evidence(listing, comps)
        # "area" is claimed only when the pool verifiably reaches the
        # subject's geography; otherwise the honest level is "pool" — cached
        # sales exist, but none are verified to be near this subject.
        level = "area" if area_evidence else "pool"
    # The anchor tier the verdict actually stood on, read off its own basis
    # string (the verdict already recorded it; we re-present, not re-derive).
    basis = str(_g(verdict, "basis", default="") or "")
    # Tier 2/3 labels are checked FIRST: their parenthetical explains that the
    # "same-street set" was too thin, so a naive same-street test would claim
    # tier 1 exactly when the verdict fell back off it.
    if "area trusted comps" in basis or "area sold prices" in basis:
        anchor_tier = "wider area (same-street set too thin)"
    elif "all matched" in basis:
        anchor_tier = "all matched comps (thin set)"
    elif "same-street" in basis:
        anchor_tier = "same street as the subject"
    else:
        anchor_tier = None
    epc_area = subject_epc_sqft(listing)
    return {
        "subjectStreet": street.title() if street else None,
        "postcode": postcode,
        "outcode": outcode,
        "matchLevel": level,
        "areaEvidence": area_evidence,
        "sameStreetComps": same_street,
        "anchorTier": anchor_tier,
        "note": ("comp matching is street+town level — the engine never claims "
                 "to have found this exact building, only sales on the same "
                 "street or nearby"),
        "epc": {
            "lookedUp": False,
            "suppliedSqft": epc_area,
            "note": ("subject EPC area supplied by the caller"
                     if epc_area is not None else
                     "the subject was not looked up in the EPC register — no "
                     "EPC-side area exists for it in this run"),
        },
    }


def _sqft_section(listing: Any) -> Dict[str, Any]:
    sqft = subject_sqft(listing)
    comp_note = (_g(listing, "provenance.completeness") or {}).get("sqft")
    if sqft is None:
        source = "missing"
    elif comp_note == "derived":
        source = "derived from a stated sqm figure"
    else:
        source = "stated on the listing (marketing sqft)"
    epc_area = subject_epc_sqft(listing)
    from gaff_engine import epc as _epc
    basis = _epc.sqft_basis_check(sqft, epc_area)
    return {"sqft": sqft, "source": source,
            "epcSqft": epc_area,
            "basisConflict": basis}


def _comps_section(comps: Optional[List[Any]]) -> Dict[str, Any]:
    by_trust = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    excluded = unknown_cat = 0
    for c in (comps or []):
        tier = str(_g(c, "areaConfidence", default="") or "").lower()
        by_trust[tier if tier in by_trust else "unknown"] += 1
        cat = _comp_category(c)
        if cat == "additional":
            excluded += 1
        elif cat is None:
            unknown_cat += 1
    return {"pool": len(comps or []), "byTrust": by_trust,
            "excludedNonStandard": excluded,
            "unknownCategory": unknown_cat}


def _value_section(listing: Any, verdict: Any) -> Optional[Dict[str, Any]]:
    if verdict is None:
        return None
    err = _g(verdict, "error")
    if err is not None:
        return {"error": str(err)}
    tag = _enum(_g(verdict, "tag"))
    fair = _g(verdict, "fairEstimate")
    band = _g(verdict, "band")
    band_out = ({"low": _g(band, "low"), "high": _g(band, "high")}
                if band is not None else None)
    ask = subject_ask(listing)
    headline = _g(verdict, "headlineDeltaPct")
    delta = _g(verdict, "deltaPct")
    # The recompute lines: the SAME arithmetic the verdict performed, written
    # out so a reader can check it against the shown inputs.
    arithmetic: List[str] = []
    if ask is not None and fair:
        arithmetic.append(
            "headline delta = (asking %s − fair %s) / %s × 100 = %.1f%%"
            % ("£" + _money(ask), "£" + _money(fair), "£" + _money(fair),
               (ask - fair) / fair * 100.0))
    adjustments = []
    for ev in (_g(verdict, "evidence") or []):
        kind = str(_g(ev, "kind", default="") or "")
        if kind.endswith("_adj"):
            adjustments.append({"kind": kind, "label": _g(ev, "label"),
                                "valueGBP": _g(ev, "value"),
                                "text": _g(ev, "text")})
    if adjustments and headline is not None and delta is not None:
        arithmetic.append(
            "adjusted delta = headline %.1f%% moved by the adjustment(s) above"
            " → %.1f%%" % (headline, delta))
    return {"tag": tag, "asking": ask, "fairEstimate": fair, "band": band_out,
            "headlineDeltaPct": headline, "deltaPct": delta,
            "confidence": _g(verdict, "confidence"),
            "basis": _g(verdict, "basis"),
            "adjustments": adjustments, "arithmetic": arithmetic}


def _taste_section(taste: Any) -> Optional[Dict[str, Any]]:
    if taste is None:
        return None
    rows_in = _g(taste, "breakdown") or _g(taste, "axisBreakdown") or []
    rows = [{"axis": _enum(_g(r, "axis")), "score": _g(r, "score"),
             "weight": _g(r, "weight"), "contribution": _g(r, "contribution")}
            for r in rows_in]
    adjs_in = _g(taste, "adjustments") or _g(taste, "tasteAdjustments") or []
    adjs = [{"kind": _g(a, "kind"), "delta": _g(a, "delta"),
             "source": _g(a, "source")} for a in adjs_in]
    base, score = _g(taste, "base"), _g(taste, "score")
    delta_sum = round(sum(float(a["delta"] or 0) for a in adjs), 4)
    recompute = None
    if base is not None and score is not None:
        recompute = ("base %.1f %s Σ adjustments %+.1f = %.1f (clamped to the "
                     "emitted %.1f)" % (base, "+" if delta_sum >= 0 else "",
                                        delta_sum, base + delta_sum, score))
    return {"base": base, "score": score, "rows": rows,
            "adjustments": adjs, "adjustmentSum": delta_sum,
            "recompute": recompute}


def show_work(listing: Any, verdict: Any = None, comps: Optional[List[Any]] = None,
              taste: Any = None, flags: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Assemble the full work trace for one scored listing, from EXISTING data.

    ``verdict`` may be a :class:`ValueVerdict` or the value_check payload dict
    (including its soft-error form); ``taste`` a TasteResult or the taste_score
    payload; ``comps`` the comp pool the verdict saw. Everything is optional —
    an absent piece is traced as absent, never invented.
    """
    from gaff_engine.serialize import to_jsonable
    return {
        "addressMatch": _address_section(listing, comps, verdict),
        "sqftSource": _sqft_section(listing),
        "comps": _comps_section(comps),
        "value": _value_section(listing, verdict),
        "taste": _taste_section(taste),
        "flags": to_jsonable(list(flags or [])),
    }


# ---------------------------------------------------------------------------
# The narrated form.
# ---------------------------------------------------------------------------

def render_text(work: Dict[str, Any]) -> str:
    """The trace as plain narrated text — the show-your-working printout."""
    out: List[str] = ["HOW THIS WAS WORKED OUT", "=" * 24]

    am = work.get("addressMatch") or {}
    out.append("")
    out.append("Address match")
    street = am.get("subjectStreet")
    out.append("  subject: %s%s" % (street or "street unknown",
                                    (", %s" % am["postcode"]) if am.get("postcode") else
                                    ((", %s" % am["outcode"]) if am.get("outcode") else "")))
    level = am.get("matchLevel")
    if level == "street":
        out.append("  matched at STREET level: %d cached sale(s) on the subject's own street."
                   % am.get("sameStreetComps", 0))
    elif level == "area":
        out.append("  no sales on the subject's own street — matched at AREA level (%s)."
                   % (am.get("areaEvidence") or "nearby streets"))
    elif level == "pool":
        out.append("  no sales on the subject's own street — and the cached pool's sales "
                   "are NOT verified to be near this subject. Treat the price view as a "
                   "pool comparison, not a local one.")
    else:
        out.append("  no comparable sales matched at any level.")
    if am.get("anchorTier"):
        out.append("  the verdict anchored on: %s." % am["anchorTier"])
    out.append("  (%s)" % am.get("note"))
    epc = am.get("epc") or {}
    out.append("  EPC: %s." % epc.get("note"))

    sq = work.get("sqftSource") or {}
    out.append("")
    out.append("Floor area")
    if sq.get("sqft") is not None:
        out.append("  %s sqft — %s." % (_money(sq["sqft"]), sq.get("source")))
    else:
        out.append("  none — %s." % sq.get("source"))
    bc = sq.get("basisConflict")
    if bc and bc.get("conflict"):
        out.append("  BASIS CONFLICT: marketing %s sqft vs EPC %s sqft (%.0f%% apart) — "
                   "priced on the marketing figure, and the verdict trusts itself less for it."
                   % (_money(bc["statedSqft"]), _money(bc["epcSqft"]), bc["diffPct"]))
    elif bc:
        out.append("  EPC area %s sqft agrees within tolerance." % _money(bc["epcSqft"]))

    cp = work.get("comps") or {}
    out.append("")
    out.append("Comparable sales")
    out.append("  %d in the pool; by floor-area trust: %d high, %d medium, %d low, %d unknown."
               % (cp.get("pool", 0), cp["byTrust"]["high"], cp["byTrust"]["medium"],
                  cp["byTrust"]["low"], cp["byTrust"]["unknown"]))
    if cp.get("excludedNonStandard"):
        out.append("  %d excluded as non-standard PPD rows (repossession / power-of-sale / "
                   "non-private transfer) — visible here, never in the estimate."
                   % cp["excludedNonStandard"])
    if cp.get("unknownCategory"):
        out.append("  %d carry no transaction category (treated as standard — an assumption, "
                   "and it is shown as one)." % cp["unknownCategory"])

    val = work.get("value")
    out.append("")
    out.append("Value")
    if val is None:
        out.append("  no value verdict was produced in this run.")
    elif val.get("error"):
        out.append("  %s" % val["error"])
    else:
        if val.get("tag") == "needs_data":
            out.append("  insufficient evidence — no tag. %s" % (val.get("basis") or ""))
        else:
            band = val.get("band") or {}
            if band.get("low") is not None:
                out.append("  evidence band £%s–£%s; fair estimate £%s; tag: %s."
                           % (_money(band["low"]), _money(band["high"]),
                              _money(val["fairEstimate"]), val.get("tag")))
            for line in val.get("arithmetic") or []:
                out.append("  %s" % line)
            for a in val.get("adjustments") or []:
                out.append("  adjustment: %s" % (a.get("text") or a.get("label")))
            if val.get("confidence") is not None:
                out.append("  confidence %.2f — basis: %s" % (val["confidence"],
                                                              val.get("basis")))

    ts = work.get("taste")
    out.append("")
    out.append("Taste")
    if ts is None:
        out.append("  not scored in this run (no axis reads were supplied).")
    else:
        for r in ts.get("rows") or []:
            out.append("  %-24s %4.1f × weight %4.1f  %s"
                       % (str(r.get("axis")), float(r.get("score") or 0),
                          float(r.get("weight") or 0),
                          ("— %s" % r["contribution"]) if r.get("contribution") else ""))
        for a in ts.get("adjustments") or []:
            out.append("  adjustment %+0.1f (%s: %s)"
                       % (float(a.get("delta") or 0), a.get("kind"), a.get("source")))
        if ts.get("recompute"):
            out.append("  recompute: %s" % ts["recompute"])

    flags = work.get("flags") or []
    if flags:
        out.append("")
        out.append("Flags")
        for f in flags:
            out.append("  %s" % (_g(f, "text") or _g(f, "code") or str(f)))
    return "\n".join(out)


__all__ = ["show_work", "render_text"]
