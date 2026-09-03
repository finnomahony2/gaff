"""assembleFeed — the cross-listing Buy shortlist (05-modes §5.3 / 06-dashboards).

``assemble_dashboard`` is per-Listing (one home's ordered Components). Its
companion ``assemble_feed`` is the **set** surface: the Buy shortlist — the
scored Listings of a Search, ranked, each rendered as its browse-stage *calm
card* (the compact Value Verdict + top flag + the score rings, §5.3 browse-feed
discipline), fronted by one narrated line over the whole set.

Same library, same five rules, but the unit is a set: each card reuses
:func:`gaff_engine.dashboard.select_components` at the ``browse`` stage (so a card
carries exactly the two lead Slots — never a heavy Component), and the feed sorts
by ``composite`` descending with a stable ``listingKey`` tie-break, so the order
is deterministic.

This is the M3 shortlist: the thing a first-time buyer lands on after starting a
Buy search — several homes, each with the truth verdict up front, the whole set
ranked by fit, tap one to open its full dashboard.

The invest cross-listing ``deal_table`` compare variant is contract-level (P5);
this module ships the Buy shortlist form.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from gaff_engine.dashboard import BUY_PROFILE, _narration, select_components
from gaff_engine.schemas import (
    FeedCard, FeedLayout, Mode, Narration, Ref, Stage, ValueTag,
)


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            if cur is None:
                ok = False
                break
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
            if cur is None and part != names[-1].split(".")[-1]:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _facts(listing: Any) -> str:
    bits = []
    pt = _g(listing, "propertyType")
    if pt is not None:
        bits.append(str(getattr(pt, "value", pt)).replace("_", " ").title())
    for n, unit in (("beds", "bed"), ("baths", "bath"), ("sqft", "sqft")):
        v = _g(listing, n)
        if v is not None:
            bits.append("%s %s" % (v, unit))
    return " · ".join(bits)


def _top_flag(result: Any) -> Optional[Any]:
    sev = {"serious": 0, "watch": 1, "info": 2}
    flags = _g(result, "flags", default=[]) or []
    return sorted(flags, key=lambda f: sev.get(getattr(getattr(f, "severity", None), "value",
                                                       getattr(f, "severity", None)), 9))[0] if flags else None


def _card(result: Any, listing: Any, person: Any, search: Any) -> FeedCard:
    slots = select_components(result, person, search, listing,
                             mode_profile=BUY_PROFILE, pursuit=Stage.BROWSE.value)
    vv = _g(result, "valueVerdict")
    taste = _g(result, "taste")
    return FeedCard(
        listingRef=Ref(id=_g(listing, "id"), schemaVersion="listing@1"),
        addressDisplay=_g(listing, "address.display"),
        price=_g(listing, "buy.price") or _g(listing, "rent.rentPcm"),   # headline money: sale price (buy) or pcm (rent)
        facts=_facts(listing),
        composite=_g(result, "composite"),
        taste=_g(taste, "score") if taste is not None else None,
        verdictTag=_g(vv, "tag") if vv is not None else None,
        deltaPct=_g(vv, "deltaPct") if vv is not None else None,
        headlineDeltaPct=_g(vv, "headlineDeltaPct") if vv is not None else None,
        topFlag=_top_flag(result),
        slots=slots,
        isDemo=bool(_g(listing, "provenance.isDemo", default=False)),
    )


def _feed_narration(cards: List[FeedCard], search: Any) -> Narration:
    """One line over the set: the count + the verdict spread (cites, never invents)."""
    n = len(cards)
    tags = [getattr(c.verdictTag, "value", c.verdictTag) for c in cards if c.verdictTag is not None]
    counts = {t: tags.count(t) for t in ("steal", "fair", "over") if t in tags}
    spread = ", ".join("%d %s" % (v, k) for k, v in counts.items()) or "scored on taste + value"
    area = _g(search, "area.label", "title", default="your search")
    head = "%d home%s in %s — %s." % (n, "" if n == 1 else "s", area, spread)
    top = cards[0] if cards else None
    sub = None
    if top is not None:
        sub = "Top of the list: %s at %s." % (
            (top.addressDisplay or "").split(",")[0],
            _fmt_money(top.price))
    return Narration(headline=head, subhead=sub or "Tap a home to open its full dashboard.")


def _fmt_money(m: Any) -> str:
    amt = _g(m, "amount")
    if amt is None:
        return "—"
    period = getattr(_g(m, "period"), "value", _g(m, "period"))
    suffix = {"pcm": " pcm", "pw": " pw", "pa": " pa"}.get(period, "")
    return "£%s%s" % (format(int(amt), ","), suffix)


def assemble_feed(results: List[Any], listings: List[Any], person: Any, search: Any, *,
                  mode_profile: Any = BUY_PROFILE) -> FeedLayout:
    """Rank the scored Listings into the Buy shortlist. ``results`` and
    ``listings`` are parallel lists (result[i] scored listing[i]). Returns a
    ``feed.layout@1`` of :class:`FeedCard`, sorted by composite desc (stable
    ``listingKey`` tie-break), fronted by one narrated line over the set.

    Excluded results (hard-gate fail, ``composite == 0`` / null verdict) are
    dropped from the shortlist — they never compete (§5.6 gate interaction)."""
    pairs: List[Tuple[Any, Any]] = [
        (r, l) for r, l in zip(results, listings)
        if not _g(r, "rules.excluded", default=False)]
    cards = [_card(r, l, person, search) for r, l in pairs]
    cards.sort(key=lambda c: (-(c.composite or 0.0), c.listingRef.id or ""))
    narration = _feed_narration(cards, search)
    return FeedLayout(
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        mode=Mode(_g(search, "mode.value", "mode", default="buy")) if isinstance(
            _g(search, "mode.value", "mode", default="buy"), str) else _g(search, "mode"),
        stage=Stage.BROWSE,
        cards=cards,
        narration=narration,
        sources=["HM Land Registry Price Paid", "EPC register", "profile.json"],
    )


__all__ = ["assemble_feed"]
