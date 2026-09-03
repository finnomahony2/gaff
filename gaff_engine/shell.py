"""P7 · The shell — the frame that holds every view (07-shell.md §5.0/§5.3).

M5 built the front door and the no-dead-end router (`homepage.py` §5.1/§5.2).
This is the rest of the container: the persistent frame's resolved state
(`shell.layout@1`, §5.0) and the multi-Search switcher (`search.switcher@1`,
§5.3) that makes the one-Person-many-Searches architecture *navigable*.

The hard boundary (A14, §5.0): **the shell renders and routes; it computes no
score and mutates no Person.** On a switch or an edit it *requests* a re-score
(P3) and *reads* a Person (P4) — it never runs the engine itself. So this module
imports no scorer: it is pure frame/nav/switcher/route resolution, testable
offline via the router fuzz gate (§7 test 1) with no engine in the loop. The
build (`build_m8.py`) is where the real engine produces each view's payload and
these functions assemble the frame around it.

Reuses `homepage.resolve_route` / `nav_model` (the §5.1 resolver already proven
in M5) so there is exactly one no-dead-end resolver in the codebase.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from gaff_engine.homepage import nav_model, resolve_route
from gaff_engine.schemas import (
    Mode, Provenance, Ref, Route, ShellLayout, Switcher, SwitcherEntry,
)

# A fixed clock so shell_layout is deterministic (byte-idempotent builds + tests);
# the real product stamps the live resolve time. Mirrors homepage.py's fixed demo clock.
_RESOLVED_AT = "2026-07-14T08:00:00Z"

# §5.3 badge — provenance wins (the hard "no demo masquerades as real" rule, A6),
# then the Search status. `active` (or unknown) reads LIVE.
_STATUS_BADGE = {"draft": "DRAFT", "paused": "PAUSED", "archived": "ARCHIVED", "active": "LIVE"}


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """Attr-or-key access, one dotted path. Small + local (the shell reads simple
    fields off search@1 / person@1)."""
    cur = obj
    for part in name.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return default if cur is None else cur


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


# ---------------------------------------------------------------------------
# §5.0 — shell.layout@1: the persistent frame's resolved state.
# ---------------------------------------------------------------------------

def shell_layout(route: Route, *, active_search: Any = None, viewport: str = "rail",
                 theme: str = "system", overlay: Optional[str] = None,
                 anonymous: bool = False, resolved_at: str = _RESOLVED_AT) -> ShellLayout:
    """Resolve the frame state (§5.0). `anonymous` is the discriminant: on the
    homepage there is no active Search, so `activeSearchRef` and `mode` are absent
    and `route` is the mode-less homepage view (§5.2). In-app, both come from the
    active Search. Pure — a function of (route, activeSearch, theme, viewport)."""
    if anonymous or active_search is None:
        return ShellLayout(route=route, viewport=viewport, theme=theme, anonymous=True,
                           resolvedAt=resolved_at, activeSearchRef=None, mode=None,
                           overlay=overlay)
    ref = Ref(id=_g(active_search, "id"), schemaVersion="search@1")
    return ShellLayout(route=route, viewport=viewport, theme=theme, anonymous=False,
                       resolvedAt=resolved_at, activeSearchRef=ref,
                       mode=_g(active_search, "mode"), overlay=overlay)


# ---------------------------------------------------------------------------
# §5.3 — search.switcher@1: one Person, many Searches.
# ---------------------------------------------------------------------------

def _badge(search: Any) -> str:
    prov = _g(search, "provenance")
    if prov is not None and getattr(prov, "isDemo", False):
        return "DEMO PERSONA"
    return _STATUS_BADGE.get(str(_enum(_g(search, "status", "active"))), "LIVE")


def _mix_summary(search: Any) -> str:
    """`search.scorerMix` → "55/20/25" (taste/rules/value) — the lens at a glance."""
    mix = _g(search, "scorerMix")
    if mix is None:
        return ""
    return "%g/%g/%g" % (_g(mix, "taste", 0), _g(mix, "rules", 0), _g(mix, "value", 0))


def _role(search: Any) -> str:
    """The viewer's role on this Search. Default `owner` (the user's own Searches);
    a shared Search where the user is a `viewer` opens settings read-only
    downstream (§5.5), never a dead end. P1's Collaborator models *other* people,
    not self, so self-role is passed explicitly by the caller (`roles` map); this
    is the floor."""
    return "owner"


def switcher_entry(search: Any, *, subtitle: Optional[str] = None,
                   role: Optional[str] = None) -> SwitcherEntry:
    """Project one search@1 into a SwitcherEntry (§5.3). Reads existing P1 fields;
    redefines nothing (A14)."""
    return SwitcherEntry(
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        title=_g(search, "title"), mode=_g(search, "mode"),
        mixSummary=_mix_summary(search), badge=_badge(search),
        role=role if role is not None else _role(search),
        subtitle=subtitle, provenance=_g(search, "provenance"))


def build_switcher(person_ref: Ref, searches: Sequence[Any], active_ref: Ref, *,
                   subtitles: Optional[Dict[str, str]] = None,
                   roles: Optional[Dict[str, str]] = None) -> Switcher:
    """Assemble the search.switcher@1 (§5.3): every Search this Person holds, the
    active one, provenance badges. `canCreate` is always true — creating a Search
    is never a dead end (§5.7). One `personRef` across all entries is the
    architecture claim (A5)."""
    subs, rls = subtitles or {}, roles or {}
    entries = [switcher_entry(s, subtitle=subs.get(_g(s, "id")), role=rls.get(_g(s, "id")))
               for s in searches]
    return Switcher(personRef=person_ref, activeSearchRef=active_ref,
                    searches=entries, canCreate=True)


def switch_to(target_search: Any) -> Route:
    """The switch contract (§5.3): switching a Search ALWAYS lands on the new
    mode's home view — it never tries to carry a cross-mode arg (a Buy `listing`
    is meaningless in a rent Search). Returns the landing Route; the caller
    re-derives shell_layout and *requests* a P3 re-score (the shell asks, never
    computes). This is the §5.3 no-dead-end rule for search."""
    nm = nav_model(_g(target_search, "mode"))
    return Route(view=nm.home, arg=None, raw="#/" + nm.home)


def resolve_in_search(raw_hash: str, search: Any, *,
                      listing_ids: Sequence[str] = (), sub_ids: Sequence[str] = ()) -> Route:
    """Resolve a hash against a live Search (§5.1) — the no-dead-end resolver with
    this Search's mode and its resolvable listing/sub ids. Thin wrapper over the
    M5 `resolve_route` so there is one resolver, not two."""
    return resolve_route(raw_hash, _g(search, "mode"),
                         listing_ids=tuple(listing_ids), sub_ids=tuple(sub_ids))


__all__ = [
    "shell_layout", "switcher_entry", "build_switcher", "switch_to", "resolve_in_search",
]
