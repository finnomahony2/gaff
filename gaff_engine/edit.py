"""P7 · Editing a Search — the plain-language edit loop + the fork (07-shell.md §5.4/§5.5).

The differentiator: a Search is edited in plain language ("budget to £1.5m, add
Walthamstow, threshold 7.5, 4 beds"), and the change is **previewed as a diff you
confirm** before anything moves, then applied with a **re-rank receipt** and a
**plain-words changelog**. The invariant (§5.5): *nothing mutates a Search until the
user confirms a shown diff; every applied change lands in a changelog in plain words.*

Two hard boundaries this module keeps:
- **It writes only `search@1`** (never the Person — taste corrections go through the
  P4 teach path, A11). `apply_edit` deep-copies the Search and touches no Person field.
- **It computes no score.** `apply_edit` mutates the config; the re-rank *counts* come
  from `rerank_receipt`, a pure diff of two feeds the caller (P3) produced. The shell
  requests the re-score; it never runs the engine.

`parse_instruction` is **deterministic** here (the running prototype's regex parser,
which §5.5 calls "the stub whose *shape* this contract fixes"). The real parser is an
LLM over the Search's editable fields — a parked wire-up. Swapping it in changes only
`parse_instruction`'s internals; the `EditDiff` contract, the diff-confirm, the apply,
the receipt and the changelog are unchanged.

The fork (§5.4): fork a Search, override a parameter, see the P1 `delta` and a re-rank
preview, then **Promote** (→ a sibling Search) or **Discard** (→ parent untouched,
nothing logged). The Scorer Mix cannot be forked (that is a new Search — P1 rule).
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    Area, ChangeEntry, Changelog, Delta, DeltaDirection, EditChange, EditDiff, EditEffect,
    ForkView, Gate, Money, Ref, RenderedDelta, ReRankReceipt, SettingsPanel, Subsearch,
    SearchStatus,
)

_AT = "2026-07-14T09:05:00Z"        # a fixed clock so the loop is deterministic (byte-idempotent builds + tests)


def _g(obj: Any, name: str, default: Any = None) -> Any:
    cur = obj
    for part in name.split("."):
        if cur is None:
            return default
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return default if cur is None else cur


# ---------------------------------------------------------------------------
# Current-value readers (fill an EditChange's `from_`).
# ---------------------------------------------------------------------------

def _gate(search: Any, code: str) -> Optional[Any]:
    for gt in (_g(search, "gates") or []):
        if _g(gt, "code") == code:
            return _g(gt, "value")
    return None


def _fmt_money(amount: Optional[int]) -> str:
    return "—" if amount is None else "£%s" % format(int(amount), ",")


# ---------------------------------------------------------------------------
# §5.5 — parse: text → EditDiff (PURE, previewed, never applied).
# ---------------------------------------------------------------------------

_MONEY = re.compile(r"£?\s*([\d,]+(?:\.\d+)?)\s*([mk])?", re.I)
_NUM = re.compile(r"(\d+(?:\.\d+)?)")
_ROOM = re.compile(r"(\d+)\s*(bed|bath|reception|sqft|sq ?ft)", re.I)


def _parse_money(s: str) -> Optional[int]:
    m = _MONEY.search(s)
    if not m or not m.group(1).strip(","):
        return None
    num = float(m.group(1).replace(",", ""))
    suf = (m.group(2) or "").lower()
    if suf == "m":
        num *= 1_000_000
    elif suf == "k":
        num *= 1_000
    return int(round(num))


def _place_after(clause: str, verbs: Tuple[str, ...]) -> str:
    """The place name after an add/drop verb, in the original case."""
    s = clause.strip()
    low = s.lower()
    for v in verbs:
        if low.startswith(v):
            return s[len(v):].strip(" +-").strip()
    return s.strip(" +-").strip()


def _noop(change: EditChange) -> EditChange:
    """A parsed change already at that value — surfaced honestly, not applied (§5.5 rule 2)."""
    change.effect = EditEffect.NOISE
    change.reason = "Already at that value — nothing to change."
    return change


def _parse_clause(clause: str, search: Any) -> EditChange:
    low = clause.lower().strip()

    # 1. threshold / alert (keyword wins even though it carries a number)
    if "threshold" in low or "alert" in low:
        m = _NUM.search(low)
        if m:
            to = float(m.group(1))
            field = "threshold.show" if "show" in low else "threshold.alert"
            frm = _g(search, field.split(".")[0] + "." + field.split(".")[1])
            ch = EditChange(kind="threshold", field=field, from_=frm, to=to, effect=EditEffect.REAL,
                            plain="%s threshold %s -> %s" % ("Show" if "show" in low else "Alert",
                                                             _num(frm), _num(to)))
            return _noop(ch) if frm == to else ch

    # 2. rooms (beds / baths / sqft)
    mr = _ROOM.search(low)
    if mr:
        val, unit = int(mr.group(1)), mr.group(2).lower().replace(" ", "")
        kind = {"bed": "beds", "bath": "baths", "sqft": "sqft", "sqft": "sqft",
                "reception": "receptions"}.get(unit, unit)
        code = {"beds": "min_beds", "baths": "min_baths", "sqft": "min_sqft",
                "receptions": "min_receptions"}[kind]
        frm = _gate(search, code)
        label = {"beds": "Minimum beds", "baths": "Minimum baths", "sqft": "Minimum sqft",
                 "receptions": "Minimum receptions"}[kind]
        ch = EditChange(kind=kind, field="gates[%s]" % code, from_=frm, to=val, effect=EditEffect.REAL,
                        plain="%s %s -> %s" % (label, _num(frm), val))
        return _noop(ch) if frm == val else ch

    # 3. lease floor
    if "lease" in low:
        m = _NUM.search(low)
        if m:
            val = int(float(m.group(1)))
            frm = _gate(search, "min_lease_years")
            ch = EditChange(kind="lease", field="gates[min_lease_years]", from_=frm, to=val,
                            effect=EditEffect.REAL, plain="Lease floor %s -> %s years" % (_num(frm), val))
            return _noop(ch) if frm == val else ch

    # 4. tenure
    if "freehold" in low or "leasehold" in low or "tenure" in low:
        want = "freehold only" if ("freehold" in low and "no lease" not in low) else \
               ("no leasehold" if "no lease" in low or "leasehold" in low else "any tenure")
        return EditChange(kind="tenure", field="gates[tenure]", from_=None, to=want,
                          effect=EditEffect.REAL, plain="Tenure: %s" % want)

    # 5. budget (a money token, or the word budget)
    if "budget" in low or "£" in clause or re.search(r"\d[\d,.]*\s*[mk]\b", low):
        amt = _parse_money(clause)
        if amt is not None:
            frm = _g(search, "budget.max.amount")
            ch = EditChange(kind="budget", field="budget.max", from_=frm, to=amt, effect=EditEffect.REAL,
                            plain="Max budget %s -> %s" % (_fmt_money(frm), _fmt_money(amt)))
            return _noop(ch) if frm == amt else ch

    # 6. add / drop an area
    if low.startswith(("add ", "+", "include ")):
        place = _place_after(clause, ("add", "include"))
        return EditChange(kind="addarea", field="area.polygon", from_=None, to=place,
                          effect=EditEffect.REAL, plain="+ %s (area added)" % place)
    if low.startswith(("drop ", "remove ", "exclude ", "-")):
        place = _place_after(clause, ("drop", "remove", "exclude"))
        return EditChange(kind="droparea", field="area.polygon", from_=place, to=None,
                          effect=EditEffect.REAL, plain="- %s (area removed)" % place)

    # 7. noise — matched no editable field (surfaced, never dropped, §5.5 rule 2)
    return EditChange(kind="noise", field="", from_=None, to=None, effect=EditEffect.NOISE,
                      plain=clause.strip(), reason="Not understood yet: '%s' — the real product parses freely." % clause.strip())


def _num(v: Any) -> str:
    if v is None:
        return "—"
    return ("%g" % v) if isinstance(v, float) else str(v)


def parse_instruction(text: str, search: Any) -> EditDiff:
    """Parse a plain-language instruction into an `EditDiff` (§5.5). PURE — it reads
    the Search's current values to fill each `from_`, but mutates nothing. The shell
    renders the diff and only applies on an explicit confirm (A8)."""
    clauses = [c for c in (s.strip() for s in re.split(r",|\band\b", text)) if c]
    changes = [_parse_clause(c, search) for c in clauses]
    real = [c for c in changes if c.effect == EditEffect.REAL]
    return EditDiff(changes=changes, realCount=len(real),
                    hasNoise=any(c.effect == EditEffect.NOISE for c in changes))


# ---------------------------------------------------------------------------
# §5.5 — apply: mutate search@1 ONLY (never the Person, A11).
# ---------------------------------------------------------------------------

def _set_gate(search: Any, code: str, value: Any, op: str = ">=") -> None:
    for gt in (search.gates or []):
        if getattr(gt, "code", None) == code:
            gt.value = value
            return
    if search.gates is None:
        search.gates = []
    search.gates.append(Gate(code=code, op=op, value=value))


def _apply_change(search: Any, ch: EditChange) -> None:
    k = ch.kind
    if k == "budget":
        cur = _g(search, "budget.max")
        cur_currency = getattr(cur, "currency", "GBP") if cur else "GBP"
        cur_period = getattr(cur, "period", None) if cur else None
        if search.budget is None:
            from gaff_engine.schemas import Budget
            search.budget = Budget()
        search.budget.max = Money(amount=int(ch.to), currency=cur_currency,
                                  period=cur_period) if cur_period else Money(amount=int(ch.to), currency=cur_currency)
    elif k == "threshold":
        setattr(search.threshold, ch.field.split(".")[1], ch.to)
    elif k in ("beds", "baths", "sqft", "receptions", "lease"):
        code = ch.field[ch.field.index("[") + 1:ch.field.index("]")]
        _set_gate(search, code, ch.to)
    elif k == "tenure":
        _set_gate(search, "tenure", ch.to, op="in")
    elif k == "addarea":
        _amend_area(search, add=ch.to)
    elif k == "droparea":
        _amend_area(search, drop=ch.from_)
    # geometry note: a real product recomputes area.polygon via a geo service (parked,
    # like the LLM parser); the deterministic stub records the intent on area.label.


def _amend_area(search: Any, *, add: Optional[str] = None, drop: Optional[str] = None) -> None:
    area = search.area or Area(label="your search", confidence="rough", polygon=[])
    label = area.label or "your search"
    if add:
        label = "%s + %s" % (label, add)
    if drop:
        label = label.replace(" + %s" % drop, "").replace(drop, "").strip(" +") or "your search"
    search.area = Area(label=label, confidence=area.confidence, polygon=area.polygon)


def apply_edit(search: Any, diff: EditDiff) -> Any:
    """Apply the REAL changes in `diff` to a **copy** of `search` (§5.5 rule 3). Writes
    only `search@1` fields; the returned Search is a new object, the original untouched
    (so Cancel is just "don't use the result"). Requests no re-score — the caller re-runs
    the feed and reads the counts via `rerank_receipt`."""
    out = copy.deepcopy(search)
    for ch in diff.changes:
        if ch.effect == EditEffect.REAL:
            _apply_change(out, ch)
    return out


def rerank_receipt(before_feed: Any, after_feed: Any, applied_count: int) -> ReRankReceipt:
    """The re-rank receipt (§5.5 rule 3): how the feed moved after an apply. PURE — a
    diff of two `feed.layout@1`s the caller (P3) produced. The receipt is the shell's;
    the re-ranked scores are P3's."""
    before = [c.listingRef.id for c in (before_feed.cards or [])]
    after = [c.listingRef.id for c in (after_feed.cards or [])]
    bset, aset = set(before), set(after)
    common = bset & aset
    reranked = sum(1 for cid in common if before.index(cid) != after.index(cid))
    return ReRankReceipt(
        beforeCount=len(before), afterCount=len(after), appliedCount=applied_count,
        delta=len(after) - len(before),
        movers={"entered": len(aset - bset), "left": len(bset - aset), "reranked": reranked})


# ---------------------------------------------------------------------------
# §5.5 — changelog: append-only, plain words, in order.
# ---------------------------------------------------------------------------

def changelog_append(changelog: Any, diff: EditDiff, *, search_ref: Optional[Ref] = None,
                     source: str = "nl", by_role: str = "owner", at: str = _AT) -> Changelog:
    """Append each applied (real) change to the changelog in plain words, in order
    (§5.5 rule 4). Append-only: a revert is a new entry, never a deletion. Returns a
    NEW Changelog (the old one is not mutated). `changelog=None` starts a fresh log;
    pass `search_ref` on that first append (a changelog@1 is always per-Search)."""
    entries = list((changelog.entries if changelog else None) or [])
    n = len(entries)
    for ch in diff.changes:
        if ch.effect == EditEffect.REAL:
            n += 1
            entries.append(ChangeEntry(n=n, at=at, plain=ch.plain, source=source, byRole=by_role))
    ref = (changelog.searchRef if changelog else None) or search_ref
    return Changelog(searchRef=ref, entries=entries)


def settings_panel(search: Any, changelog: Any, *, editable: bool = True,
                   pending: Optional[EditDiff] = None) -> SettingsPanel:
    """Assemble the settings.panel@1 (§5.5). `editable=false` for a viewer collaborator:
    read-only, still fully readable (no dead end)."""
    mix = _g(search, "scorerMix")
    controls = {
        "threshold": {"show": _g(search, "threshold.show"), "alert": _g(search, "threshold.alert")},
        "budget": {"max": _g(search, "budget.max.amount")},
        "area": _g(search, "area.label"),
        "gates": [{"code": _g(g, "code"), "value": _g(g, "value")} for g in (_g(search, "gates") or [])],
        "scorerMix": None if mix is None else {"taste": mix.taste, "rules": mix.rules, "value": mix.value},
    }
    return SettingsPanel(searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
                         editable=editable, controls=controls,
                         changelog=changelog, pendingDiff=pending)


# ---------------------------------------------------------------------------
# §5.4 — the sub-search fork.
# ---------------------------------------------------------------------------

_FORKABLE = {"budget", "area", "beds", "baths", "sqft", "gates", "alertPolicy", "threshold"}


def fork_delta(search: Any, overrides: Dict[str, Any]) -> Tuple[List[Delta], List[RenderedDelta]]:
    """Compute the P1 `delta[]` (raw) + its display projection for a set of overrides.
    Direction: `widen` when the override loosens (higher budget, bigger area, lower
    min-beds), `narrow` when it tightens, else `move`."""
    raw: List[Delta] = []
    shown: List[RenderedDelta] = []
    for key, new in overrides.items():
        if key == "budget":
            frm = _g(search, "budget.max.amount")
            to = new.get("max") if isinstance(new, dict) else new
            d = DeltaDirection.WIDEN if (to or 0) > (frm or 0) else DeltaDirection.NARROW
            raw.append(Delta(path="budget.max", from_=frm, to=to, direction=d))
            shown.append(RenderedDelta(path="budget.max", from_=_fmt_money(frm), to=_fmt_money(to), direction=d))
        elif key == "area":
            frm = _g(search, "area.label")
            raw.append(Delta(path="area", from_=frm, to=new, direction=DeltaDirection.WIDEN))
            shown.append(RenderedDelta(path="area", from_=str(frm), to=str(new), direction=DeltaDirection.WIDEN))
        else:
            frm = _gate(search, "min_%s" % key) if key in ("beds", "baths", "sqft") else _g(search, key)
            if isinstance(new, (int, float)) and isinstance(frm, (int, float)):
                # a min-gate: a lower floor widens, a higher floor narrows
                d = DeltaDirection.WIDEN if new < frm else (DeltaDirection.NARROW if new > frm else DeltaDirection.MOVE)
            else:
                d = DeltaDirection.MOVE
            raw.append(Delta(path=key, from_=frm, to=new, direction=d))
            shown.append(RenderedDelta(path=key, from_=_num(frm), to=_num(new), direction=d))
    return raw, shown


def fork_view(search: Any, overrides: Dict[str, Any], *, sub_id: str,
              preview: Optional[Dict[str, Any]] = None) -> ForkView:
    """Build the `subsearch.forkview@1` (§5.4). Refuses a Scorer-Mix override (a fork
    holds the Mix fixed — that is a new Search, P1 rule). `preview` is computed by the
    caller from real feeds (`fork_preview`); the fork itself computes no score."""
    if "scorerMix" in overrides or "mix" in overrides:
        raise ValueError("a fork holds the Scorer Mix fixed — changing the Mix is a new Search, not a fork")
    raw, shown = fork_delta(search, overrides)
    inherits = {"scorerMix": True, "gates": "gates" not in overrides, "area": "area" not in overrides,
                "alertPolicy": "alertPolicy" not in overrides}
    draft = Subsearch(id=sub_id, parentRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
                      personRef=_g(search, "personRef"), inherits=inherits, overrides=dict(overrides),
                      delta=raw, status=SearchStatus.ACTIVE, createdAt=_AT)
    return ForkView(parentRef=Ref(id=_g(search, "id"), schemaVersion="search@1"), draft=draft,
                    delta=shown, preview=preview or {"parentCount": None, "subCount": None, "movers": {}},
                    resolution="open")


def fork_preview(parent_feed: Any, sub_feed: Any) -> Dict[str, Any]:
    """The re-rank preview (§5.4 beat 3): parent vs effective-config counts + movers.
    Same pure feed-diff as `rerank_receipt`."""
    r = rerank_receipt(parent_feed, sub_feed, applied_count=0)
    return {"parentCount": r.beforeCount, "subCount": r.afterCount, "movers": r.movers}


def promote(search: Any, view: ForkView, *, new_id: str) -> Any:
    """Promote a fork to a sibling `search@1` (§5.4): the parent's Person + Mix, the
    overrides baked in, born `status:draft`. The parent is untouched; the sibling joins
    the switcher (§5.3)."""
    sibling = copy.deepcopy(search)
    sibling.id = new_id
    sibling.status = SearchStatus.DRAFT
    sibling.title = "%s (fork)" % (_g(search, "title") or "search")
    diff = EditDiff(changes=_overrides_as_changes(search, view.draft.overrides), realCount=0, hasNoise=False)
    for ch in diff.changes:
        _apply_change(sibling, ch)
    view.resolution = "promoted"
    return sibling


def discard(view: ForkView) -> ForkView:
    """Discard a fork (§5.4): resolution `discarded`, the parent untouched, nothing
    logged (a discarded fork changed nothing)."""
    out = copy.deepcopy(view)
    out.resolution = "discarded"
    return out


def _overrides_as_changes(search: Any, overrides: Dict[str, Any]) -> List[EditChange]:
    changes: List[EditChange] = []
    for key, new in overrides.items():
        if key == "budget":
            to = new.get("max") if isinstance(new, dict) else new
            changes.append(EditChange(kind="budget", field="budget.max", from_=None, to=to,
                                      effect=EditEffect.REAL, plain="budget"))
        elif key == "area":
            changes.append(EditChange(kind="addarea", field="area.polygon", from_=None, to=str(new),
                                      effect=EditEffect.REAL, plain="area"))
        elif key in ("beds", "baths", "sqft"):
            changes.append(EditChange(kind=key, field="gates[min_%s]" % key, from_=None, to=new,
                                      effect=EditEffect.REAL, plain=key))
    return changes


__all__ = [
    "parse_instruction", "apply_edit", "rerank_receipt", "changelog_append", "settings_panel",
    "fork_delta", "fork_view", "fork_preview", "promote", "discard",
]
