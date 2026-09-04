"""S4 — the network consent gateway: the offline promise as a list, not a sentence.

Until now the promise was carried by one line of prose — "warm is the only tool
that touches the network" — repeated in the README, the skill and a tool
description. A sentence cannot be checked. This module turns it into a
declaration a test can measure against the running code:

    the set of tools that CAN open a socket == the set declared here

A new fetcher that forgets to declare itself fails ``tests/test_netgate.py``
rather than quietly breaking the promise. That matters imminently: F-04 puts EPC
fetches inside ``warm``, and F-15 adds four or five more fetchers (flood,
schools, crime, broadband, listed buildings), each a new upstream with its own
licence and its own outage behaviour.

What the check already caught
-----------------------------
Writing it found a real breach. ``value.py`` called ``hpi.hpi_factor(...)``
without ``offline=True``, and ``hpi_factor`` defaults to fetching. Today no
verdict fetches, but only because the shipped HPI cache happens to cover every
month the two warmed towns' comps fall in — a comp from a month it lacks (an
old sale, a new region, a rewarmed town) would have had ``value_check``, a tool
documented as entirely offline, open a socket. The call sites now force
``offline=True``, so the verdict path is offline by construction rather than by
the luck of a warm cache. That is the whole argument for a list over a sentence.

What a declaration says
-----------------------
Not just "this one is allowed to". **What it sends, and to whom** — because the
consent a user gives is to a specific disclosure, not to "the network". A street
name and a town going to HM Land Registry is a different thing from a postcode
going to the Environment Agency, and a user is entitled to be told which.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Every host any declared verb may reach. Named separately from the verbs so a
#: test can assert that a fetcher's endpoint constant actually appears here.
HOSTS = {
    "land_registry": "landregistry.data.gov.uk",
    "epc_register": "api.get-energy-performance-data.communities.gov.uk",
}


class _Verb(dict):
    """One declared network verb. A dict so it serialises into a payload."""


def _verb(sends: str, to: str, host: str, why: str,
          calls: str, licence: str) -> Dict[str, Any]:
    return _Verb(sends=sends, to=to, host=host, why=why, calls=calls,
                 licence=licence)


#: THE LIST. Tool name -> one or more declarations, one per path that can fetch.
#:
#: Frozen in the sense that matters: adding a fetcher without adding it here
#: fails the test. Keep the ``sends`` wording concrete enough that a user could
#: object to it — "a street name and a town" tells them something, "search
#: parameters" does not.
NETWORK_VERBS: Dict[str, Tuple[Dict[str, Any], ...]] = {
    "warm": (
        _verb(sends="a street name and a town",
              to="HM Land Registry Price Paid",
              host=HOSTS["land_registry"],
              why="to cache that street's recorded sales so price and value "
                  "questions can be answered offline afterwards",
              calls="one request",
              licence="Open Government Licence v3.0"),
        _verb(sends="a town name",
              to="HM Land Registry Price Paid",
              host=HOSTS["land_registry"],
              why="to build that town's repeat-sales dataset for flip_stats",
              calls="a paced whole-town pull, minutes; very large towns are "
                    "refused by a record cap",
              licence="Open Government Licence v3.0"),
    ),
}

#: The tools that may open a socket. Everything else is offline, and the test
#: measures that rather than trusting it.
NETWORK_TOOLS = frozenset(NETWORK_VERBS)


def declares(tool: str) -> bool:
    """Is this tool allowed to touch the network at all?"""
    return tool in NETWORK_VERBS


def verbs(tool: str) -> Tuple[Dict[str, Any], ...]:
    """The declarations for one tool; empty for an offline tool."""
    return NETWORK_VERBS.get(tool, ())


def consent_line(tool: str, index: int = 0) -> Optional[str]:
    """The one sentence a fetcher shows before it goes out.

    Plain, specific and in the second person, because it is asking. ``None``
    for a tool that declares nothing — a caller rendering a consent line for an
    offline tool is a bug, not a no-op, and reads as one at the call site.
    """
    declarations = verbs(tool)
    if not declarations or index >= len(declarations):
        return None
    d = declarations[index]
    return ("%s sends %s to %s (%s), %s — %s. Everything else Gaff does runs "
            "from local caches." % (tool, d["sends"], d["to"], d["host"],
                                    d["calls"], d["why"]))


def manifest() -> List[Dict[str, Any]]:
    """Every declared verb, flattened — for a coverage payload or a doctor
    bundle that wants to print what this build can reach."""
    out = []
    for tool in sorted(NETWORK_VERBS):
        for i, d in enumerate(verbs(tool)):
            entry = {"tool": tool, "index": i}
            entry.update(d)
            entry["line"] = consent_line(tool, i)
            out.append(entry)
    return out


def offline_note(tools_seen: Any = None) -> str:
    """The promise, rendered from the list rather than retyped.

    The sentence this replaces was maintained by hand in three places. Deriving
    it means it cannot drift from what the code actually does.
    """
    names = sorted(NETWORK_VERBS)
    if not names:
        return "No tool in this build touches the network."
    if len(names) == 1:
        return ("%s is the only tool that touches the network; every other tool "
                "answers from local caches." % names[0])
    return ("%s and %s are the only tools that touch the network; every other "
            "tool answers from local caches."
            % (", ".join(names[:-1]), names[-1]))


__all__ = ["HOSTS", "NETWORK_VERBS", "NETWORK_TOOLS", "declares", "verbs",
           "consent_line", "manifest", "offline_note"]
