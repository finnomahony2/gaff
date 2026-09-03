#!/usr/bin/env python3
"""Skill/CLI surface test — the mirror of mcp_client_test.py (E11).

The CLI carries the long-running work, so its contract is the opposite of the
MCP server's: results on stdout, progress on stderr, as it happens. Offline and
deterministic: the warm tests run the CLI under a bootstrap that monkeypatches
``urllib.request.urlopen`` (and zeroes the pacing sleeps), so the wire is never
touched, and point ``GAFF_CACHE_DIR`` at a temp dir so nothing pollutes the
real user cache.

Exit-code contract under test (BACKLOG 2a, sharpened by the 29 Aug fixer
pass): 0 = answered; 1 = the tool ran into the DATA (an honest {error, hint}
payload still prints, the hint is the point — or a runtime failure like the
flips record cap); 2 = the INVOCATION was wrong (unknown tool, non key=value
token, bad argument names, or argument-validation errors like mode=banana) —
carried by safe_call's usage tag, not by sniffing error-string prefixes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GAFF = os.path.join(HERE, "skill", "gaff")
FAILS = []
TOTAL = 0


def check(label, cond, detail=""):
    global TOTAL
    TOTAL += 1
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" - " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def run(*args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, GAFF, *args], capture_output=True,
                       text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def run_mocked(*args, cache_dir=None):
    """Run the CLI with urllib mocked to a canned Land Registry envelope.

    The bootstrap patches BEFORE the surface imports anything, exactly the way
    mcp_client_test's noisy server does, so the test never hits the network.
    """
    boot = (
        "import sys, io, json, runpy\n"
        "sys.path.insert(0, %r)\n"
        "from gaff_engine import landreg, flips\n"
        "landreg.REQUEST_PACING_SECONDS = 0\n"
        "flips.REQUEST_PACING_SECONDS = 0\n"
        "import urllib.request\n"
        "_ENV = {'result': {'items': ["
        "{'pricePaid': 500000, 'transactionDate': 'Fri, 20 Feb 2026'},"
        "{'pricePaid': 650000, 'transactionDate': 'Sat, 21 Mar 2026'}]}}\n"
        "urllib.request.urlopen = "
        "lambda req, timeout=None: io.BytesIO(json.dumps(_ENV).encode())\n"
        "sys.argv = ['gaff'] + %r\n"
        "try:\n"
        "    runpy.run_path(%r, run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    sys.exit(e.code or 0)\n"
    ) % (ROOT, list(args), GAFF)
    env = dict(os.environ)
    if cache_dir:
        env["GAFF_CACHE_DIR"] = cache_dir
    p = subprocess.run([sys.executable, "-c", boot], capture_output=True,
                       text=True, env=env)
    return p.returncode, p.stdout, p.stderr


ALL_TOOLS = ["price_check", "flip_stats", "read_listing", "value_check",
             "taste_score", "score_listing", "show_work", "rent_check",
             "coverage", "warm"]

FULL_READS = json.dumps({"axes": {
    k: {"score": 7, "contribution": "flat test read"}
    for k in ["light_and_volume", "outdoor_space", "character_bones",
              "width_proportion_flow", "street_scene", "raw_size_threshold",
              "design_finish", "station_proximity"]}})

FULL_WEIGHTS = json.dumps({
    "light_and_volume": 10, "outdoor_space": 9, "character_bones": 8.5,
    "width_proportion_flow": 8, "street_scene": 8, "raw_size_threshold": 6,
    "design_finish": 4, "station_proximity": 0.5})


def main():
    print("Skill/CLI surface")

    rc, out, err = run("--help")
    check("--help exits 0 and lists every tool plus demo and doctor",
          rc == 0 and all(t in out for t in ALL_TOOLS)
          and "demo" in out and "doctor" in out, out[:200])

    rc, out, err = run("flip_stats", "town=LEAMINGTON SPA")
    check("a real call exits 0", rc == 0, err[:120])
    payload = json.loads(out) if out.strip() else {}
    check("stdout is parseable JSON and nothing else", payload.get("resales_analysed", 0) > 1000, out[:80])
    check("the licence travels with the result", "Open Government Licence" in payload.get("source", ""))

    # The property that removed the job runner: progress DURING the call, on stderr.
    check("progress goes to stderr, not stdout", "..." in err and "..." not in out, err[:80])
    check("stdout stays clean JSON despite progress", out.lstrip().startswith("{"))

    rc, out, err = run("price_check", "street=De Beauvoir Road", "town=LONDON")
    payload = json.loads(out)
    check("price_check is town-scoped", payload.get("sales_found") == 40, payload.get("error"))

    # Truthful cold-data errors (2a), and the non-zero exit for error payloads.
    # A deliberately unreal town — the suite must not hardcode which real towns
    # are warm (that answer is the coverage tool's, and it changes).
    rc, out, err = run("price_check", "street=High Street", "town=ATLANTIS")
    payload = json.loads(out)
    check("a cold town names what IS warm",
          "london" in payload.get("towns_warmed", []), payload)
    check("a cold town's error payload exits 1", rc == 1, rc)
    check("a cold town's hint says to warm it", "warm" in payload.get("hint", ""), payload)

    rc, out, err = run("price_check", "street=Zzz Sample Road", "town=LONDON")
    payload = json.loads(out)
    check("a cold street in a WARM town does not say 'warm this town'",
          payload.get("town_warmed") is True
          and "warm this town" not in payload.get("error", "")
          and "warm this town" not in payload.get("hint", ""), payload)
    check("...it suggests warming the street and lists cached streets",
          "warm street=" in payload.get("hint", "")
          and "de-beauvoir-road" in payload.get("streets_cached", []), payload)
    check("...and exits 1", rc == 1, rc)

    rc, out, err = run("flip_stats", "town=ATLANTIS")
    payload = json.loads(out)
    check("flip_stats cold town exits 1 with a coverage hint",
          rc == 1 and "flips coverage" in payload.get("hint", "")
          and "LEAMINGTON SPA" in payload.get("hint", ""), payload)

    rc, out, err = run("no_such_tool")
    check("unknown tool exits 2 with a usage hint", rc == 2 and "unknown tool" in err, err[:80])

    rc, out, err = run("price_check", "wrong=arg")
    check("bad arguments exit 2 and name the tool", rc == 2 and "bad arguments for price_check" in err, err[:80])

    rc, out, err = run("price_check", "notakeyvalue")
    check("malformed argument exits 2", rc == 2 and "key=value" in err, err[:80])

    # read_listing: the freeform adapter finally on a surface (R1).
    rc, out, err = run("read_listing",
                       "text=2 bed maisonette for sale\nDe Beauvoir Road, London N1\n"
                       "Guide Price \u00a31,150,000. 1,050 sqft. Leasehold, chain free.")
    payload = json.loads(out)
    check("read_listing parses a paste and exits 0",
          rc == 0 and payload["listing"]["beds"] == 2
          and payload["listing"]["sqft"] == 1050, payload.get("error") or out[:120])
    check("read_listing carries completeness and the untrusted note",
          isinstance(payload.get("completeness"), dict)
          and "UNTRUSTED" in payload.get("note", ""), payload)

    rc, out, err = run("read_listing", "text=x", 'fields={"beds": 2}')
    check("read_listing with BOTH fields and text exits 2 (usage) naming the rule",
          rc == 2 and "exactly one" in err, (rc, err[:120]))

    # The finding's exact repro: an invalid argument VALUE is as much a
    # "fix your command" failure as an invalid argument NAME, so it must
    # share exit code 2, not masquerade as a data error (1).
    rc, out, err = run("read_listing", 'fields={"beds": 2}', "mode=banana")
    check("an invalid enum value exits 2 like any other wrong invocation",
          rc == 2 and "mode must be" in err, (rc, err[:120]))

    # value_check: ingest -> cached comps -> verdict, offline (R1).
    rc, out, err = run("value_check",
                       'fields={"address": "De Beauvoir Road, London N1", "beds": 2, '
                       '"baths": 2, "sqft": 1050, "price": 1150000, '
                       '"property_type": "maisonette", "mode": "buy"}')
    payload = json.loads(out)
    check("value_check returns a verdict offline",
          rc == 0 and payload.get("tag") in ("steal", "fair", "over")
          and isinstance(payload.get("fairEstimate"), int)
          and payload.get("comps_used", 0) > 50, payload.get("error") or out[:120])
    check("value_check echoes the listing summary and basis",
          payload.get("listing", {}).get("sqft") == 1050
          and "sqft" in payload.get("basis", ""), payload)

    rc, out, err = run("value_check")
    check("value_check with no listing exits 2 (usage)",
          rc == 2 and "exactly one" in err, (rc, err[:120]))

    # L2 fixer pass, updated for rent_check's arrival: a RENTAL paste must get
    # the honest blocker AND the real next step (rent_check), never "no asking
    # price" — the user supplied the price, in the rent channel, and the
    # payload echoes it.
    rc, out, err = run("value_check",
                       "text=2 bed flat to let, De Beauvoir Road, London N1\n"
                       "£2,000pcm")
    payload = json.loads(out)
    check("value_check keeps its buy-only error and points at rent_check",
          rc == 1 and "rental listing" in payload.get("error", "")
          and "rent_check" in payload.get("error", "")
          and "asking price" not in payload.get("error", "")
          and payload.get("listing", {}).get("rentPcm") == 2000, payload)

    # taste_score: the host LLM is the taste model (R1).
    rc, out, err = run("taste_score", 'fields={"beds": 2, "sqft": 1050}',
                       "reads=" + FULL_READS, "weights=" + FULL_WEIGHTS)
    payload = json.loads(out)
    check("taste_score scores a flat read at its base",
          rc == 0 and payload.get("score") == 7.0 and payload.get("base") == 7.0
          and len(payload.get("breakdown", [])) == 8, payload.get("error") or out[:120])

    rc, out, err = run("taste_score", 'fields={"beds": 2, "sqft": 1050}',
                       "reads=" + FULL_READS)
    payload = json.loads(out)
    check("taste_score without weights falls back to the shipped demo profile",
          rc == 0 and isinstance(payload.get("score"), float)
          and len(payload.get("breakdown", [])) == 8,
          payload.get("error") or err[:120])

    rc, out, err = run("taste_score", 'fields={"beds": 2}',
                       'reads={"axes": {"light_and_volume": {"score": 7}}}')
    check("taste_score with missing axes exits 2 (usage) naming them",
          rc == 2 and "missing" in err and "outdoor_space" in err, (rc, err[:120]))

    # score_listing: the flagship one-call composition (brief §5 item 1).
    rc, out, err = run("score_listing",
                       'fields={"address": "De Beauvoir Road, London N1", '
                       '"beds": 2, "baths": 2, "sqft": 1050, "price": 1150000, '
                       '"property_type": "maisonette", "mode": "buy", '
                       '"tenure": "leasehold", "lease_years": 89}',
                       "reads=" + FULL_READS, "weights=" + FULL_WEIGHTS)
    payload = json.loads(out)
    check("score_listing composes value + taste + questions + trace + narrative",
          rc == 0 and payload["value"].get("tag") in ("steal", "fair", "over")
          and payload["taste"].get("score") == 7.0
          and isinstance(payload.get("questions"), list)
          and isinstance(payload.get("workings"), dict)
          and isinstance(payload.get("narrative"), str),
          payload.get("error") or out[:120])
    check("the 89-yr lease raises a sourced agent question",
          any(q["trigger"] == "short_lease" and "89" in q["evidence"]
              for q in payload["questions"]), payload["questions"])

    rc, out, err = run("score_listing", 'fields={"beds": 2}', "text=x")
    check("score_listing with both fields and text exits 2 (usage)",
          rc == 2 and "exactly one" in err, (rc, err[:120]))

    rc, out, err = run("score_listing",
                       'fields={"address": "De Beauvoir Road", "beds": 2, '
                       '"sqft": 700, "price": 800000}')
    payload = json.loads(out)
    check("score_listing without reads skips taste honestly and still answers",
          rc == 0 and "skipped" in payload.get("taste", {})
          and payload["value"].get("tag") is not None, payload.get("taste"))

    # show_work: the trace verb (plan section 9 traceability).
    rc, out, err = run("show_work",
                       'fields={"address": "De Beauvoir Road, London N1", '
                       '"beds": 2, "sqft": 1050, "price": 1150000, '
                       '"property_type": "maisonette", "mode": "buy"}')
    payload = json.loads(out)
    check("show_work exits 0 with the structured trace and the narrated form",
          rc == 0 and "HOW THIS WAS WORKED OUT" in payload.get("rendered", "")
          and isinstance(payload.get("comps", {}).get("byTrust"), dict)
          and payload.get("sqftSource", {}).get("source", "").startswith("stated"),
          payload.get("error") or out[:120])

    # Regression (29 Aug fixer, S4): the EPC-side area must be CARRIABLE over
    # the surface — epc_sqft in fields= feeds the marketing-vs-EPC basis check
    # and the works-vs-EPC agent question; before the fix no tool caller could
    # get an EPC area onto a listing, so both were dead code here.
    rc, out, err = run("show_work",
                       'fields={"address": "De Beauvoir Road, London N1", '
                       '"beds": 2, "sqft": 1050, "price": 1150000, '
                       '"property_type": "maisonette", "mode": "buy", '
                       '"epc_sqft": 800}')
    payload = json.loads(out)
    bc = payload.get("sqftSource", {}).get("basisConflict") or {}
    check("epc_sqft rides the fields boundary into the basis-conflict check",
          rc == 0 and bc.get("conflict") is True and bc.get("epcSqft") == 800.0
          and "BASIS CONFLICT" in payload.get("rendered", ""),
          payload.get("sqftSource"))

    rc, out, err = run("score_listing",
                       'fields={"address": "De Beauvoir Road, London N1", '
                       '"beds": 2, "sqft": 1050, "price": 1150000, '
                       '"property_type": "maisonette", "mode": "buy", '
                       '"epc_sqft": 800, '
                       '"description": "Recently renovated maisonette"}')
    payload = json.loads(out)
    trigs = [q["trigger"] for q in payload.get("questions", [])]
    check("claimed works + an EPC-side area raise the works_vs_epc question",
          rc == 0 and "works_vs_epc" in trigs
          and "sqft_basis_conflict" in trigs, trigs)

    # Regression (29 Aug L2C fixer): a subject no warmed cache verifiably
    # reaches now gets NO pool and NO tag — the comp pool is routed by town,
    # so the old failure (a foreign-town subject confidently priced against
    # the London set, with only the TRACE hedging at "pool" level) cannot
    # occur. The trace shows the honest empty pool. Synthetic address.
    rc, out, err = run("show_work",
                       'fields={"address": "Nowhere Lane, ATLANTIS", "beds": 2, '
                       '"sqft": 800, "price": 300000, "mode": "buy"}')
    payload = json.loads(out)
    check("an out-of-area subject gets a routed refusal, not a London-pool verdict",
          rc == 0 and payload["addressMatch"]["matchLevel"] == "none"
          and "warmed town" in (payload.get("value", {}) or {}).get("error", "")
          and "no comparable sales matched" in payload.get("rendered", ""),
          payload.get("value") or payload.get("addressMatch"))

    rc, out, err = run("show_work",
                       'fields={"address": "Sample Close, London N1", '
                       '"postcode": "N1 4AB", "beds": 2, "sqft": 800, '
                       '"price": 800000, "mode": "buy"}')
    payload = json.loads(out)
    check("an in-outcode subject off its own street still earns AREA, evidence named",
          rc == 0 and payload["addressMatch"]["matchLevel"] == "area"
          and "outcode" in (payload["addressMatch"].get("areaEvidence") or ""),
          payload.get("addressMatch"))

    # rent_check: both pool outcomes CONTROLLED via the env overrides, because
    # this suite runs in the lab checkout (pool present) and in the assembled
    # package (no pool ships). Pool records are synthetic.
    rc, out, err = run("rent_check",
                       'fields={"beds": 2, "price": 500000, "mode": "buy"}')
    payload = json.loads(out)
    check("rent_check on a sale listing exits 1 pointing at value_check",
          rc == 1 and "sale listing" in payload.get("error", "")
          and "value_check" in payload.get("error", ""), payload)

    tmp = tempfile.mkdtemp(prefix="gaff-cli-rent-")
    empty = tempfile.mkdtemp(prefix="gaff-cli-rent-empty-")
    try:
        pool = [{"id": i, "outcode": "N1", "beds": 2, "pcm": pcm}
                for i, pcm in enumerate([1900, 2000, 2100, 2200, 2400], start=1)]
        with open(os.path.join(tmp, "rental_candidates.json"), "w") as fh:
            json.dump(pool, fh)
        env = {"GAFF_CACHE_DIR": tmp, "GAFF_DATA_DIR": tmp}
        rc, out, err = run("rent_check",
                           'fields={"address": "Sample Street, London N1", '
                           '"postcode": "N1", "beds": 2, "rent_pcm": 2000, '
                           '"mode": "rent"}', env_extra=env)
        payload = json.loads(out)
        check("rent_check judges the ask against the pool median and exits 0",
              rc == 0 and payload.get("tag") == "fair"
              and payload.get("fairRentPcm") == 2100, payload)
        check("rent_check states its lower confidence in words",
              "asking-rent" in payload.get("confidence_note", ""), payload)
        env = {"GAFF_CACHE_DIR": empty, "GAFF_DATA_DIR": empty}
        rc, out, err = run("rent_check",
                           'fields={"beds": 2, "rent_pcm": 2000, "mode": "rent"}',
                           env_extra=env)
        payload = json.loads(out)
        check("no pool -> exits 1 with the honest why + the user-cache fix",
              rc == 1 and "redistributable" in payload.get("error", "")
              and "rental_candidates.json" in payload.get("hint", ""), payload)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)

    # coverage: the error paths' facts, before a question fails (2b).
    rc, out, err = run("coverage")
    payload = json.loads(out)
    check("coverage lists warmed comps towns with street counts",
          rc == 0 and payload["comps_towns"]["london"]["streets"] >= 20, payload)
    check("coverage lists the four shipped flips towns",
          set(payload["flips_towns"]) >= {"LEAMINGTON SPA", "WARWICK",
                                          "KENILWORTH", "SOUTHAM"}, payload)
    check("coverage names the loose datasets and the constraint",
          "comps_enriched.json" in payload["datasets"]
          and "Cache-first" in payload.get("note", ""), payload)

    # warm: mocked network, temp cache (2a).
    tmp = tempfile.mkdtemp(prefix="gaff-cli-test-")
    try:
        rc, out, err = run_mocked("warm", "street=Sample Test Street",
                                  "town=LONDON", cache_dir=tmp)
        payload = json.loads(out) if out.strip() else {}
        check("warm street= caches the mocked sales and exits 0",
              rc == 0 and payload.get("sales_cached") == 2, err[:200] or out[:120])
        check("warm wrote into the (temp) user cache",
              os.path.exists(os.path.join(tmp, "comps", "london",
                                          "sample-test-street.json")))
        rc, out, err = run_mocked("warm", "flips_town=SAMPLEHAM", cache_dir=tmp)
        check("warm flips_town= surfaces the record cap as a clear message",
              rc == 1 and "more than" in err and "Traceback" not in err, err[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rc, out, err = run("warm")
    check("warm with no arguments exits 2 (usage) naming both forms",
          rc == 2 and "street=" in err and "flips_town=" in err, (rc, err[:120]))

    # demo: the zero-configuration first run (2a), offline, plain text.
    rc, out, err = run("demo")
    check("demo exits 0 and narrates all three acts",
          rc == 0 and "price_check" in out and "8.2" in out
          and "value_check" in out and "FAILED" not in out, out[:300])
    check("demo is plain text, not JSON", not out.lstrip().startswith("{"))

    print("\n%d checks, %d failed" % (TOTAL, len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
