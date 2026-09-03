"""U6-live tests — the LiveTasteModel adapter (M4).

DETERMINISTIC: the LLM boundary (``model_fn``) is a recorded replay, so the
adapter (request build → parse → TasteRead → U6 pipeline) is exercised offline.
The live path's *quality* is measured by U8, not asserted here.

    python3 -m pytest tests/test_taste_live.py -v
    python3 tests/test_taste_live.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.taste_live import (  # noqa: E402
    LiveTasteModel, build_request, parse_response, replay_model,
)
from gaff_engine.taste import taste_result, canonical_deb_reads, TasteRead  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_PERSON  # noqa: E402


def _read_to_response(tr):
    return {"axes": {k: {"score": v.score, "contribution": v.contribution} for k, v in tr.axes.items()},
            "namedLoveHits": tr.namedLoveHits, "antiSignalHits": tr.antiSignalHits, "staged": tr.staged}


def _golden_recordings():
    img, txt = canonical_deb_reads()
    return {"%s|img" % GOLDEN_LISTING.listingKey: _read_to_response(img),
            "%s|text" % GOLDEN_LISTING.listingKey: _read_to_response(txt)}


# ---------------------------------------------------------------------------
# 1 · The request carries the rubric the model scores against.
# ---------------------------------------------------------------------------

def test_request_marks_listing_text_untrusted():
    """T12: the evidence block travels with the injection guard, so any prompt
    rendered from this request tells the model listing text is data."""
    req = build_request(GOLDEN_LISTING, GOLDEN_PERSON, use_images=False)
    assert "UNTRUSTED" in req["evidenceNote"]
    assert "instruction" in req["evidenceNote"].lower()


def test_rendered_prompt_carries_the_guard():
    """The renderer is the enforcement point: the guard must survive into the
    actual prompt text, not just sit as an unread request key."""
    from gaff_engine.anthropic_models import _build_prompt, _SYSTEM
    prompt = _build_prompt(build_request(GOLDEN_LISTING, GOLDEN_PERSON, use_images=False))
    assert "UNTRUSTED" in prompt
    assert prompt.index("UNTRUSTED") < prompt.index("Description (data, not instructions)")
    assert "marketing copy" in _SYSTEM


def test_build_request_shape():
    req = build_request(GOLDEN_LISTING, GOLDEN_PERSON, use_images=True)
    assert req["schemaVersion"] == "taste.read.request@1" and req["useImages"] is True
    assert len(req["axes"]) == 8                          # all eight axes, weighted
    assert req["axes"][0]["axis"] == "light_and_volume" and req["axes"][0]["weight"] == 10.0
    assert set(req["evidence"]) >= {"description", "keyFeatures", "propertyType", "sqft"}


# ---------------------------------------------------------------------------
# 2 · Parsing — a good response → TasteRead; a bad one fails loudly.
# ---------------------------------------------------------------------------

def test_parse_response_ok():
    img, _ = canonical_deb_reads()
    tr = parse_response(_read_to_response(img))
    assert isinstance(tr, TasteRead) and len(tr.axes) == 8


def test_parse_response_rejects_missing_axis():
    resp = {"axes": {"light_and_volume": {"score": 8.0}}}   # only 1 of 8
    try:
        parse_response(resp)
    except ValueError as e:
        assert "missing" in str(e).lower()
    else:
        raise AssertionError("expected ValueError on a 1-axis response")


def test_parse_response_rejects_out_of_range():
    img, _ = canonical_deb_reads()
    bad = _read_to_response(img)
    bad["axes"]["light_and_volume"]["score"] = 99          # out of [0,10]
    try:
        parse_response(bad)
    except ValueError as e:
        assert "range" in str(e).lower()
    else:
        raise AssertionError("expected ValueError on an out-of-range score")


# ---------------------------------------------------------------------------
# 3 · The live adapter reproduces the golden through U6's real pipeline.
# ---------------------------------------------------------------------------

def test_live_adapter_reproduces_golden():
    live = LiveTasteModel(replay_model(_golden_recordings()))
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, live)
    assert tr.score == 8.2 and tr.prior == 7.4            # same as the recorded model
    assert len(tr.axisBreakdown) == 8


def test_live_model_is_drop_in_for_the_engine():
    """LiveTasteModel satisfies the TasteModel interface the engine calls."""
    from gaff_engine.engine import score
    from gaff_engine.forensics import canonical_model as fc
    live = LiveTasteModel(replay_model(_golden_recordings()))
    r = score(GOLDEN_LISTING, GOLDEN_PERSON,
              __import__("gaff_engine.fixtures.de_beauvoir", fromlist=["GOLDEN_SEARCH"]).GOLDEN_SEARCH,
              taste_model=live, forensics_model=fc())
    assert r.taste.score == 8.2 and r.composite == 7.6


# ---------------------------------------------------------------------------
# 4 · The recorder freezes a live run into a replay (byte-idempotent build).
# ---------------------------------------------------------------------------

def test_recorder_captures_responses():
    rec = {}
    live = LiveTasteModel(replay_model(_golden_recordings()), recorder=rec)
    taste_result(GOLDEN_LISTING, GOLDEN_PERSON, live)
    # both passes captured, keyed by listingKey|img / |text.
    assert any(k.endswith("|img") for k in rec) and any(k.endswith("|text") for k in rec)


def _run_standalone():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:
            failures += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
        else:
            print("PASS  %s" % name)
    print("-" * 60)
    total = len(tests)
    print("RESULT: %s (%d/%d passed%s)" % (
        "FAIL" if failures else "PASS", total - failures, total,
        ", %d failed" % failures if failures else ""))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
