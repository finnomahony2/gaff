"""gaff doctor — the paste-able, secret-free diagnostic bundle (T13).

The tool runs on the user's machine; the person helping them sees nothing.
This converts "it broke" into one block of text that can be pasted into an
issue or a chat as-is: versions, where the package and both cache tiers
actually resolve, what data is present, whether a token source exists, and a
handful of offline self-checks that exercise the real read paths.

Secret discipline: values never appear — the token section reports only which
SOURCE would supply one. Nothing here touches the network, so running it can
never hang on the exact outage being diagnosed.

    python3 -m gaff_engine.doctor
"""

from __future__ import annotations

import os
import platform
import sys
from typing import List, Tuple

from gaff_engine import paths


def _count(root: str) -> str:
    if not os.path.isdir(root):
        return "absent"
    n = sum(len(fs) for _, _, fs in os.walk(root))
    return "%d file(s)" % n


def _token_source() -> str:
    """Which source WOULD supply the EPC token — never the value."""
    if (os.environ.get(paths.ENV_EPC_TOKEN) or "").strip():
        return "env %s (set)" % paths.ENV_EPC_TOKEN
    if paths._keychain_token():
        return 'keychain (service "%s")' % paths.KEYCHAIN_SERVICE
    for label, p in (("~/.gaff/epc_token",
                      os.path.join(os.path.expanduser("~"), ".gaff", "epc_token")),
                     (".secrets/epc_token (dev)",
                      os.path.join(paths._REPO_ROOT, ".secrets", "epc_token"))):
        try:
            if open(p, encoding="utf-8").read().strip():
                return "file %s (present)" % label
        except (OSError, UnicodeDecodeError):
            # An unreadable or undecodable token file is a diagnosis, not a
            # crash — the report must always render (mirrors paths.epc_token).
            continue
    return "none found (EPC live lookups will refuse with instructions)"


def _selfchecks() -> "Tuple[List[str], bool]":
    """Offline exercises of the real read paths; each line PASS/FAIL + why.
    Returns ``(lines, any_failed)`` so the exit code rests on an explicit
    flag, never on grepping the rendered text (paths can contain "FAIL")."""
    out = []
    failed = [False]

    def check(name, fn):
        try:
            detail = fn()
            out.append("PASS  %-24s %s" % (name, detail))
        except Exception as exc:                       # noqa: BLE001 — the report IS the handler
            failed[0] = True
            out.append("FAIL  %-24s %s: %s" % (name, type(exc).__name__, exc))

    def golden():
        from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_PERSON
        from gaff_engine.taste import canonical_model, taste_result
        tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
        assert tr.score == 8.2, "taste recomputed to %s, expected 8.2" % tr.score
        return "taste 8.2 recomputes"

    def comps():
        from gaff_engine.landreg import fetch_street
        items = fetch_street("NORTHCHURCH ROAD", "LONDON", offline=True)
        assert items, "no comps for the seeded street in either cache tier"
        return "%d sales for the seeded street" % len(items)

    def hpi_check():
        from gaff_engine.hpi import avg_price
        v = avg_price("hackney", "flat-maisonette", "2024-06", offline=True)
        assert v, "seeded HPI month unreadable"
        return "seeded HPI month reads (%d)" % v

    def flips_check():
        from gaff_engine.flips import load_flips
        n = len(load_flips())
        assert n, "no flip records in either cache tier"
        return "%d flip records" % n

    def paste():
        from gaff_engine.ingest import listing_from_text
        L = listing_from_text("Two bed flat, £2,000 pcm, N1 1AA")
        assert L.rent and L.rent.rentPcm.amount == 2000
        return "freeform paste parses"

    for name, fn in (("golden recompute", golden), ("comps read", comps),
                     ("hpi read", hpi_check), ("flips read", flips_check),
                     ("freeform parse", paste)):
        check(name, fn)
    return out, failed[0]


def report() -> "Tuple[str, bool]":
    """The full bundle plus an explicit any-failure flag."""
    import gaff_engine
    lines = [
        "== gaff doctor ==",
        "python        %s (%s)" % (platform.python_version(), sys.executable),
        "platform      %s" % platform.platform(),
        "package       %s" % os.path.dirname(os.path.abspath(gaff_engine.__file__)),
        "",
        "-- cache tiers (reads try user first, then shipped; writes go to user) --",
        "user cache    %s" % paths.user_cache_dir(),
    ]
    for kind in paths.KINDS:
        lines.append("  %-8s    %s" % (kind, _count(paths.cache_dir(kind))))
    lines.append("shipped data  %s" % paths.shipped_data_dir())
    for kind in paths.KINDS:
        lines.append("  %-8s    %s" % (kind, _count(paths.shipped_dir(kind))))
    loose = ["comps_enriched.json", "round1_scores.json", "round2_scores.json",
             "invest_pool.json"]
    lines.append("  loose:      %s" % ", ".join(
        "%s %s" % (fn, "ok" if paths.data_file(fn) else "ABSENT") for fn in loose))
    checks, failed = _selfchecks()
    lines += [
        "",
        "-- secrets (sources only; values are never shown) --",
        "epc token     %s" % _token_source(),
        "",
        "-- offline self-checks --",
        *checks,
        "",
        "(paste this whole block when reporting a problem — it contains no",
        " secrets and no personal data beyond the paths shown above)",
    ]
    return "\n".join(lines), failed


def main() -> int:
    text, failed = report()
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
