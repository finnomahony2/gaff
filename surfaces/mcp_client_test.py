#!/usr/bin/env python3
"""Scripted MCP client: boots the server, drives the protocol, asserts shapes.

Offline, deterministic, no Claude involved. Covers the two protocol rules the
28 Aug review added (E3) — a stray print from shared code must not corrupt the
frame stream, and tool execution errors must arrive as results with isError
rather than as JSON-RPC transport errors — plus the soft-error half of that
contract (BACKLOG 2a): an honest {error, hint} payload is a failed answer, so
it too must carry isError:true.

The warm tests boot the server under a bootstrap that monkeypatches
``urllib.request.urlopen`` (and zeroes the pacing sleeps) before anything else
imports, so the wire is never touched; ``GAFF_CACHE_DIR`` points at a temp dir
so nothing pollutes the real user cache.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILS = []
TOTAL = 0


def check(label, cond, detail=""):
    global TOTAL
    TOTAL += 1
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" - " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


class Server:
    """A live server subprocess; optionally noisy, optionally network-mocked."""

    def __init__(self, noisy=False, mocked=False, cache_dir=None, data_dir=None):
        env = dict(os.environ)
        if cache_dir:
            env["GAFF_CACHE_DIR"] = cache_dir
        if data_dir:
            # Overrides the shipped tier too — the rent_check tests need a
            # server whose pool presence is CONTROLLED, not inherited from
            # whichever checkout/wheel this suite happens to run in.
            env["GAFF_DATA_DIR"] = data_dir
        if noisy:
            # Register a tool that prints to stdout the way the productised
            # analysis scripts already do (_flips.py:22 etc). Test-only.
            # Import (not exec) so the server module keeps a real __file__.
            boot = (
                "import sys;sys.path.insert(0,%r);"
                "import gaff_tools as gt;"
                "gt.DISPATCH['noisy']=lambda progress=None: (print('chatty!',flush=True), {'ok':1})[1];"
                "gt.TOOLS.append({'name':'noisy','description':'t','inputSchema':{'type':'object','properties':{}}});"
                "import mcp_server;mcp_server.main()" % HERE
            )
            cmd = [sys.executable, "-c", boot]
        elif mocked:
            # Patch urllib BEFORE the surface imports anything, so the warm
            # tool's live path runs against a canned Land Registry envelope.
            boot = (
                "import sys, io, json;"
                "sys.path.insert(0,%r);sys.path.insert(0,%r);"
                "from gaff_engine import landreg, flips;"
                "landreg.REQUEST_PACING_SECONDS=0;flips.REQUEST_PACING_SECONDS=0;"
                "import urllib.request;"
                "_ENV={'result':{'items':["
                "{'pricePaid':500000,'transactionDate':'Fri, 20 Feb 2026'},"
                "{'pricePaid':650000,'transactionDate':'Sat, 21 Mar 2026'}]}};"
                "urllib.request.urlopen=lambda req,timeout=None: io.BytesIO(json.dumps(_ENV).encode());"
                "import mcp_server;mcp_server.main()" % (ROOT, HERE)
            )
            cmd = [sys.executable, "-c", boot]
        else:
            cmd = [sys.executable, os.path.join(HERE, "mcp_server.py")]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def raw(self, text):
        """Write one raw (pre-encoded) frame and read the reply line.

        The frame-guard tests need this because ``call`` encodes dicts; the
        killer frames are valid JSON that is NOT an object.
        """
        self.p.stdin.write(text + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def call(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if "id" not in msg:
            return None
        return json.loads(self.p.stdout.readline())

    def tool(self, rid, name, arguments):
        """tools/call sugar: returns (decoded payload, isError)."""
        r = self.call({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
        return (json.loads(r["result"]["content"][0]["text"]),
                r["result"]["isError"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


ALL_TOOLS = {"price_check", "flip_stats", "read_listing", "value_check",
             "taste_score", "score_listing", "show_work", "rent_check",
             "coverage", "warm"}

FULL_READS = {"axes": {
    k: {"score": 7, "contribution": "flat test read"}
    for k in ["light_and_volume", "outdoor_space", "character_bones",
              "width_proportion_flow", "street_scene", "raw_size_threshold",
              "design_finish", "station_proximity"]}}

FULL_WEIGHTS = {
    "light_and_volume": 10, "outdoor_space": 9, "character_bones": 8.5,
    "width_proportion_flow": 8, "street_scene": 8, "raw_size_threshold": 6,
    "design_finish": 4, "station_proximity": 0.5}


def main():
    print("MCP surface - scripted client")
    s = Server()
    r = s.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    check("initialize names the server 'gaff', not the spike leftover",
          r["result"]["serverInfo"]["name"] == "gaff", r["result"]["serverInfo"])
    s.call({"jsonrpc": "2.0", "method": "notifications/initialized"})

    r = s.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = r["result"]["tools"]
    names = [t["name"] for t in tools]
    check("tools/list advertises the full surface", set(names) == ALL_TOOLS, names)
    check("every tool carries an inputSchema", all("inputSchema" in t for t in tools))
    check("every tool description carries the data constraint or its own honest scope",
          all(("Cache-first" in t["description"]) or ("local" in t["description"])
              or ("network" in t["description"]) or (t["name"] == "taste_score")
              for t in tools),
          [(t["name"], t["description"][:40]) for t in tools])

    payload, is_err = s.tool(3, "flip_stats", {"town": "LEAMINGTON SPA"})
    check("flip_stats returns real analysis", payload["resales_analysed"] > 1000, payload)
    check("a successful call is not flagged isError", is_err is False)
    check("flip_stats carries its licence", "Open Government Licence" in payload["source"])

    payload, is_err = s.tool(4, "price_check",
                             {"street": "De Beauvoir Road", "town": "LONDON"})
    check("price_check returns town-scoped sales", payload["sales_found"] == 40, payload.get("error"))
    check("dates parsed to ISO", payload["most_recent"][0]["date"].startswith("20"))

    # Truthful cold-data errors (2a) — and the soft-error isError contract.
    # A deliberately unreal town: the previously-used real town got warmed into
    # the shipped cache, and this suite must not depend on which towns are warm
    # (that answer belongs to the coverage tool, not a hardcoded list here).
    payload, is_err = s.tool(5, "price_check",
                             {"street": "High Street", "town": "ATLANTIS"})
    check("a cold town names what IS warm rather than returning nothing",
          "error" in payload and "london" in payload.get("towns_warmed", []), payload)
    check("a soft error (cold town) carries isError:true, per the server's own contract",
          is_err is True)

    payload, is_err = s.tool(6, "price_check",
                             {"street": "Zzz Sample Road", "town": "LONDON"})
    check("a cold street in a WARM town blames the street, not the town",
          is_err is True and payload.get("town_warmed") is True
          and "warm this town" not in payload.get("error", "")
          and "de-beauvoir-road" in payload.get("streets_cached", []), payload)

    payload, is_err = s.tool(7, "flip_stats", {"town": "ATLANTIS"})
    check("flip_stats cold town is a soft error with a coverage hint",
          is_err is True and "flips coverage" in payload.get("hint", ""), payload)

    # E3: execution failure must be a RESULT with isError, not a transport error.
    r = s.call({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "price_check", "arguments": {"wrong": "arg"}}})
    check("execution failure returns a result, not a JSON-RPC error", "error" not in r, r.get("error"))
    check("execution failure sets isError", r.get("result", {}).get("isError") is True)
    check("the error text names the tool and the cause",
          "bad arguments for price_check" in r["result"]["content"][0]["text"])
    check("the CLI's internal usage tag does not leak onto the MCP wire",
          '"usage"' not in r["result"]["content"][0]["text"],
          r["result"]["content"][0]["text"][:120])

    # Unknown tool IS a protocol error — the client asked for something that does not exist.
    r = s.call({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}}})
    check("unknown tool is a protocol error", r.get("error", {}).get("code") == -32602)

    # read_listing: the freeform adapter finally on a surface (R1).
    payload, is_err = s.tool(10, "read_listing", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
        "sqft": 1050, "price": "Guide Price £1,150,000",
        "property_type": "maisonette", "mode": "buy"}})
    check("read_listing returns the Listing shape with completeness + note",
          is_err is False and payload["listing"]["beds"] == 2
          and payload["listing"]["sqft"] == 1050
          and isinstance(payload["completeness"], dict)
          and "UNTRUSTED" in payload["note"], payload.get("error") or payload)

    payload, is_err = s.tool(11, "read_listing", {"text": "x", "fields": {"beds": 2}})
    check("read_listing with both fields and text is an isError result naming the rule",
          is_err is True and "exactly one" in payload["error"], payload)

    # value_check: ingest -> cached comps -> verdict, offline (R1).
    payload, is_err = s.tool(12, "value_check", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
        "sqft": 1050, "price": 1150000, "property_type": "maisonette",
        "mode": "buy"}})
    check("value_check composes a full verdict offline",
          is_err is False and payload.get("tag") in ("steal", "fair", "over")
          and isinstance(payload.get("fairEstimate"), int)
          and payload.get("confidence") is not None
          and "sqft" in payload.get("basis", ""), payload.get("error") or payload)

    payload, is_err = s.tool(13, "value_check", {})
    check("value_check with no listing is an isError result",
          is_err is True and "exactly one" in payload["error"], payload)

    # taste_score: the host LLM is the taste model (R1).
    payload, is_err = s.tool(14, "taste_score", {
        "fields": {"beds": 2, "sqft": 1050},
        "reads": FULL_READS, "weights": FULL_WEIGHTS})
    check("taste_score scores the host's flat read at its base",
          is_err is False and payload["score"] == 7.0 and payload["base"] == 7.0
          and len(payload["breakdown"]) == 8, payload.get("error") or payload)

    payload, is_err = s.tool(15, "taste_score", {
        "fields": {"beds": 2},
        "reads": {"axes": {"light_and_volume": {"score": 7}}}})
    check("taste_score with missing axes names them",
          is_err is True and "missing" in payload["error"]
          and "outdoor_space" in payload["error"], payload)

    # coverage (2b): the error paths' facts, offered before a question fails.
    payload, is_err = s.tool(16, "coverage", {})
    check("coverage lists comps towns, flips towns and datasets",
          is_err is False and payload["comps_towns"]["london"]["streets"] >= 20
          and set(payload["flips_towns"]) >= {"LEAMINGTON SPA", "WARWICK",
                                              "KENILWORTH", "SOUTHAM"}
          and "comps_enriched.json" in payload["datasets"], payload)

    payload, is_err = s.tool(17, "warm", {})
    check("warm with no arguments is an isError result naming both forms",
          is_err is True and "street=" in payload["error"]
          and "flips_town=" in payload["error"], payload)

    # score_listing: the flagship one-call composition (brief §5 item 1).
    payload, is_err = s.tool(18, "score_listing", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
        "sqft": 1050, "price": 1150000, "property_type": "maisonette",
        "mode": "buy", "tenure": "leasehold", "lease_years": 89},
        "reads": FULL_READS, "weights": FULL_WEIGHTS})
    check("score_listing composes value + taste + questions + trace + narrative",
          is_err is False and payload["value"].get("tag") in ("steal", "fair", "over")
          and payload["taste"].get("score") == 7.0
          and isinstance(payload["questions"], list)
          and isinstance(payload["workings"], dict), payload.get("error") or "shape")
    check("score_listing narrative is plain text built from the numbers",
          isinstance(payload.get("narrative"), str)
          and "evidence" in payload["narrative"], payload.get("narrative", "")[:120])
    check("the 89-yr lease raises an agent question with its evidence",
          any(q["trigger"] == "short_lease" and "89" in q["evidence"]
              for q in payload["questions"]), payload["questions"])
    check("the workings trace carries every section",
          set(payload["workings"]) >= {"addressMatch", "sqftSource", "comps",
                                       "value", "taste"}, list(payload["workings"]))

    payload, is_err = s.tool(19, "score_listing", {"fields": {"beds": 2}, "text": "x"})
    check("score_listing with both fields and text is an isError naming the rule",
          is_err is True and "exactly one" in payload["error"], payload)

    payload, is_err = s.tool(20, "score_listing", {"fields": {
        "address": "De Beauvoir Road", "beds": 2, "sqft": 700, "price": 800000},
        "reads": FULL_READS})
    check("score_listing without weights falls back to the demo profile",
          is_err is False and isinstance(payload["taste"].get("score"), float),
          payload.get("error") or payload.get("taste"))

    payload, is_err = s.tool(21, "score_listing", {"fields": {
        "address": "De Beauvoir Road", "beds": 2, "sqft": 700, "price": 800000}})
    check("score_listing without reads skips taste HONESTLY, not silently",
          is_err is False and "skipped" in payload["taste"]
          and "taste model" in payload["taste"]["skipped"], payload.get("taste"))

    # show_work: every number traceable on demand (plan section 9).
    payload, is_err = s.tool(22, "show_work", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "sqft": 1050,
        "price": 1150000, "property_type": "maisonette", "mode": "buy"}})
    check("show_work returns the structured trace plus the narrated form",
          is_err is False and "HOW THIS WAS WORKED OUT" in payload.get("rendered", "")
          and payload["sqftSource"]["source"].startswith("stated")
          and isinstance(payload["comps"].get("byTrust"), dict),
          payload.get("error") or list(payload))
    check("show_work never claims a finer address match than the engine made",
          "never claims" in payload["addressMatch"]["note"], payload["addressMatch"])

    # Regression (29 Aug fixer, S4): the EPC-side area is carriable over stdio
    # — epc_sqft in fields reaches the marketing-vs-EPC basis check and the
    # works-vs-EPC agent question (both were unreachable dead code over the
    # shipped surfaces before the fix).
    payload, is_err = s.tool(40, "show_work", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "sqft": 1050,
        "price": 1150000, "property_type": "maisonette", "mode": "buy",
        "epc_sqft": 800}})
    bc = (payload.get("sqftSource") or {}).get("basisConflict") or {}
    check("epc_sqft rides the fields boundary into the basis-conflict check",
          is_err is False and bc.get("conflict") is True
          and bc.get("epcSqft") == 800.0
          and "BASIS CONFLICT" in payload.get("rendered", ""),
          payload.get("sqftSource"))

    payload, is_err = s.tool(41, "score_listing", {"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "sqft": 1050,
        "price": 1150000, "property_type": "maisonette", "mode": "buy",
        "epc_sqft": 800, "description": "Recently renovated maisonette"}})
    trigs = [q["trigger"] for q in payload.get("questions", [])]
    check("claimed works + an EPC-side area raise the works_vs_epc question",
          is_err is False and "works_vs_epc" in trigs
          and "sqft_basis_conflict" in trigs, trigs)

    # Regression (29 Aug L2C fixer): a subject no warmed cache verifiably
    # reaches now gets NO pool and NO tag — the comp pool is routed by town,
    # so a foreign-town subject can no longer be priced against the London
    # set with only the trace hedging. Synthetic address.
    payload, is_err = s.tool(42, "show_work", {"fields": {
        "address": "Nowhere Lane, ATLANTIS", "beds": 2, "sqft": 800,
        "price": 300000, "mode": "buy"}})
    check("an out-of-area subject gets a routed refusal, not a London-pool verdict",
          is_err is False and payload["addressMatch"]["matchLevel"] == "none"
          and "warmed town" in (payload.get("value") or {}).get("error", "")
          and "nearby streets" not in payload.get("rendered", ""),
          payload.get("value") or payload.get("addressMatch"))

    # rent_check: the honest wrong-mode error (the pool paths get their own
    # env-controlled servers below).
    payload, is_err = s.tool(23, "rent_check", {"fields": {
        "address": "De Beauvoir Road", "beds": 2, "price": 1150000, "mode": "buy"}})
    check("rent_check on a sale listing is an isError pointing at value_check",
          is_err is True and "sale listing" in payload["error"]
          and "value_check" in payload["error"], payload)
    s.close()

    # rent_check pool paths: both tiers overridden so the outcome is CONTROLLED
    # (this suite runs both in the lab checkout, which has a pool, and in the
    # assembled package, which deliberately ships none). Pool records are
    # synthetic: no real portal content in tests.
    print("\nrent_check - synthetic pool in a temp user cache")
    tmp = tempfile.mkdtemp(prefix="gaff-mcp-rent-")
    empty = tempfile.mkdtemp(prefix="gaff-mcp-rent-empty-")
    try:
        pool = [{"id": i, "outcode": "N1", "beds": 2, "pcm": pcm}
                for i, pcm in enumerate([1900, 2000, 2100, 2200, 2400], start=1)]
        with open(os.path.join(tmp, "rental_candidates.json"), "w") as fh:
            json.dump(pool, fh)
        r = Server(cache_dir=tmp, data_dir=tmp)
        r.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        payload, is_err = r.tool(2, "rent_check", {"fields": {
            "address": "Sample Street, London N1", "postcode": "N1",
            "beds": 2, "rent_pcm": 2000, "mode": "rent"}})
        check("rent_check judges the ask against the pool median",
              is_err is False and payload.get("tag") == "fair"
              and payload.get("fairRentPcm") == 2100, payload)
        check("rent_check states its lower confidence, not just a scalar",
              "asking-rent" in payload.get("confidence_note", "")
              and "weaker" in payload.get("confidence_note", ""), payload)
        r.close()

        e = Server(cache_dir=empty, data_dir=empty)
        e.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        payload, is_err = e.tool(2, "rent_check", {"fields": {
            "address": "Sample Street", "beds": 2, "rent_pcm": 2000,
            "mode": "rent"}})
        check("no pool -> the honest error names WHY (not redistributable) and the fix",
              is_err is True and "not" in payload["error"]
              and "redistributable" in payload["error"]
              and "rental_candidates.json" in payload.get("hint", ""), payload)
        e.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)

    # warm's live path, against a mocked wire and a temp cache (2a).
    print("\nwarm - mocked network, temp user cache")
    tmp = tempfile.mkdtemp(prefix="gaff-mcp-test-")
    try:
        m = Server(mocked=True, cache_dir=tmp)
        m.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        payload, is_err = m.tool(2, "warm", {"street": "Sample Test Street",
                                             "town": "LONDON"})
        check("warm street= caches the mocked sales",
              is_err is False and payload.get("sales_cached") == 2, payload)
        check("warm wrote into the (temp) user cache",
              os.path.exists(os.path.join(tmp, "comps", "london",
                                          "sample-test-street.json")))
        payload, is_err = m.tool(3, "warm", {"flips_town": "SAMPLEHAM"})
        check("warm flips_town= surfaces the record cap as a clear ToolError message",
              is_err is True and "more than" in payload.get("error", ""), payload)
        m.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # E3 rule 1: a tool that prints to stdout must NOT corrupt the frame stream.
    print("\nstdout guard - a tool that prints like the analysis scripts do")
    n = Server(noisy=True)
    n.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    r = n.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "noisy", "arguments": {}}})
    check("a printing tool does not corrupt the frame", r is not None and "result" in r, r)
    r = n.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "flip_stats", "arguments": {"town": "WARWICK"}}})
    check("the session survives and the next call still works",
          json.loads(r["result"]["content"][0]["text"])["resales_analysed"] > 1000)
    n.close()

    # Frame guards: malformed-but-valid-JSON frames used to kill the whole
    # process (rc=1), ending the session for every later call. The server's
    # own contract says protocol failures come back as JSON-RPC errors.
    print("\nframe guards - malformed frames must be answered, never fatal")
    f = Server()
    f.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    for frame in ("[1,2,3]", '"hello"', "42"):
        r = f.raw(frame)
        check("non-object frame %s is answered -32600 (id null)" % frame,
              r.get("error", {}).get("code") == -32600 and r.get("id") is None, r)
    r = f.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": [1, 2]})
    check("tools/call with array params is a -32600 error, not a crash",
          r.get("error", {}).get("code") == -32600, r)
    r = f.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": {"a": 1}, "arguments": {}}})
    check("an unhashable tool name is a -32602 error, not a TypeError crash",
          r.get("error", {}).get("code") == -32602, r)
    r = f.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "price_check", "arguments": [1, 2]}})
    check("non-dict arguments stay an isError RESULT (safe_call's net)",
          r.get("result", {}).get("isError") is True, r)
    r = f.call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "flip_stats", "arguments": {"town": "WARWICK"}}})
    check("the session outlives every malformed frame and still answers",
          f.p.poll() is None
          and json.loads(r["result"]["content"][0]["text"])["resales_analysed"] > 1000, r)
    f.close()

    print("\n%d checks, %d failed" % (TOTAL, len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
