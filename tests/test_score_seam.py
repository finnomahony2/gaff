"""S2 guard rails — the two mistakes that produce a confident wrong answer.

    python3 tests/test_score_seam.py
    python3 -m pytest tests/test_score_seam.py -v     # if pytest is installed

WRITTEN BEFORE THE CODE THEY GUARD, deliberately. Both failures below were
reproduced in the lab on 3 September 2026 against the shipped v0.1.0 package, and
neither was caught by any existing test.

1. ``engine.score`` defaults ``comps`` to ``load_enriched_comps()`` — the London
   enriched file — for a subject anywhere in the UK. A Leamington Spa flat priced
   against De Beauvoir's sales came back "fair, £1,235,000".

2. ``engine.score`` defaults ``taste_model`` to ``canonical_model()``, and
   ``taste.RecordedModel`` keys its stored reads on ``use_images``, a bool — not
   on the listing. The same Leamington flat, described as a plain 1990s block with
   no period features, scored taste 8.2 on De Beauvoir's reads, byte-identical,
   including "Victorian bones, restored floorboards, bay".

Both are one-line mistakes with a confident wrong answer as the output, which is
precisely what the abstention differentiator exists to prevent. So the two
defaults are POISONED here: they raise if they are ever reached. A tool path that
takes either one fails loudly, in this file, rather than quietly in front of a
user.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import engine, paths, tools  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# The subject: a real listing that is NOT in London, so taking either default
# is not merely sloppy, it is a wrong answer about a different town.
# ---------------------------------------------------------------------------

LEAMINGTON = {
    "address": "Willes Road, Leamington Spa",
    "postcode": "CV32 5BD",
    "beds": 2, "baths": 1, "sqft": 780,
    "price": "£365,000",
    "property_type": "flat",
    "tenure": "leasehold",
    "lease_years": 82,
    "description": ("A two-bedroom apartment in a plain 1990s block, with a "
                    "fitted kitchen, allocated parking and a communal garden. "
                    "No period features. Double glazing throughout."),
    "key_features": ["Two bedrooms", "Allocated parking", "Communal garden",
                     "No chain"],
}

EIGHT_AXES = ["light_and_volume", "outdoor_space", "character_bones",
              "width_proportion_flow", "street_scene", "raw_size_threshold",
              "design_finish", "station_proximity"]

#: An honest host read of the listing above: a plain block, scored plainly.
#: Nothing like De Beauvoir's 8.2.
PLAIN_READS = {"axes": {axis: {"score": 3.0,
                               "contribution": "a plain 1990s block; %s is "
                                               "unremarkable here" % axis}
                        for axis in EIGHT_AXES}}


class _Poison:
    """Make ``engine``'s two defaults raise, and spy on every ``engine.score``.

    The spy is what turns "the guard rail held" from an absence of noise into a
    positive assertion: it records the keyword arguments of every call, so a test
    can require that the seam was USED and that neither default was taken.
    """

    def __enter__(self):
        self.calls = []
        # Isolate the user cache too. The seam resolves the Search from the
        # SAVED SESSION when none is passed (S1), so without this the answers
        # below would depend on whether the developer running the suite happens
        # to have run situate — a hidden input to a guard-rail test. The shipped
        # data tier still supplies the warm comps, which is what routing needs.
        self._cache = tempfile.mkdtemp(prefix="gaff-seam-test-")
        self._old_cache = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self._cache
        self._orig = {name: getattr(engine, name) for name in
                      ("load_enriched_comps", "canonical_model", "score")}

        def poisoned_comps(*a, **k):
            raise AssertionError(
                "engine.score reached load_enriched_comps(): the London enriched "
                "file was about to price a subject that may be anywhere in the UK")

        def poisoned_model(*a, **k):
            raise AssertionError(
                "engine.score reached canonical_model(): the De Beauvoir taste "
                "recording was about to be replayed for a different listing")

        real_score = self._orig["score"]

        def spy(listing, person, search, **kw):
            self.calls.append(kw)
            return real_score(listing, person, search, **kw)

        engine.load_enriched_comps = poisoned_comps
        engine.canonical_model = poisoned_model
        engine.score = spy
        return self

    def __exit__(self, *exc):
        for name, fn in self._orig.items():
            setattr(engine, name, fn)
        if self._old_cache is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old_cache
        shutil.rmtree(self._cache, ignore_errors=True)
        return False


# ---------------------------------------------------------------------------
# Guard rail 1 — comps.
# ---------------------------------------------------------------------------

def test_engine_score_is_never_reached_with_comps_none():
    """Every composed tool path, with a poisoned London comp loader."""
    with _Poison() as poison:
        for tool in (tools.score_listing, tools.show_work):
            tool(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        assert poison.calls, ("no tool path reached engine.score at all — the "
                              "composed seam does not exist yet, so the guard "
                              "rail guards nothing")
        for kw in poison.calls:
            assert kw.get("comps") is not None, \
                "engine.score was called with comps=None; it would have loaded London"
            assert kw["comps"], "engine.score was called with an empty comp pool"


def test_the_comps_handed_over_are_the_routed_pool_not_londons():
    """Routing is the part engine.score does not have. The pool a Leamington
    subject is priced against must be Leamington's sales."""
    with _Poison() as poison:
        tools.score_listing(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        assert poison.calls
        comps = poison.calls[0]["comps"]
        towns = {str(getattr(c, "town", None) or (c.get("town") if isinstance(c, dict) else "")).upper()
                 for c in comps}
        assert "LONDON" not in towns, \
            "a Leamington subject was handed London comps: %s" % sorted(towns)


# ---------------------------------------------------------------------------
# Guard rail 2 — the taste model.
# ---------------------------------------------------------------------------

def test_engine_score_is_never_reached_with_taste_model_none():
    """Every composed tool path, with a poisoned golden recording."""
    with _Poison() as poison:
        for tool in (tools.score_listing, tools.show_work):
            tool(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        assert poison.calls, ("no tool path reached engine.score at all — the "
                              "composed seam does not exist yet")
        for kw in poison.calls:
            assert kw.get("taste_model") is not None, \
                ("engine.score was called with taste_model=None; it would have "
                 "replayed the De Beauvoir read for this listing")


def test_the_injected_model_replays_the_hosts_read_not_the_goldens():
    """The failure this exists for: RecordedModel keys on use_images, a bool,
    so an injected model must carry the HOST's read on BOTH keys or the text
    pass silently falls back."""
    with _Poison() as poison:
        tools.score_listing(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        model = poison.calls[0]["taste_model"]
        for use_images in (True, False):
            read = model(None, None, use_images=use_images)
            scores = {axis: read.axes[axis].score for axis in EIGHT_AXES}
            assert set(scores.values()) == {3.0}, \
                ("use_images=%s replayed something other than the host's read: %s"
                 % (use_images, scores))


def test_a_plain_block_does_not_score_de_beauvoirs_taste():
    """The end-to-end shape of the 3 September finding, as an outcome rather
    than a mechanism: a plain 1990s block honestly read must not come back at
    the golden's 8.2."""
    with _Poison():
        out = tools.score_listing(fields=dict(LEAMINGTON), reads=PLAIN_READS)
    taste = out.get("taste") or {}
    assert taste.get("score") is not None, "no taste score was produced"
    assert taste["score"] < 5.0, \
        "a plain 1990s block scored %s — the golden read leaked in" % taste["score"]


# ---------------------------------------------------------------------------
# The other half of the rail: no reads means DO NOT call engine.score at all.
# ---------------------------------------------------------------------------

def test_without_reads_the_engine_is_not_called_and_the_payload_is_value_only():
    """A host that supplies no axis reads is not a taste model, and there is no
    honest taste answer to give. Calling engine.score anyway is exactly how the
    golden recording gets replayed."""
    with _Poison() as poison:
        out = tools.score_listing(fields=dict(LEAMINGTON))
        assert poison.calls == [], \
            "engine.score was called with no host reads: %s" % poison.calls
        assert "skipped" in (out.get("taste") or {}), \
            "the no-reads payload must still say why taste was skipped"
        assert out.get("value") is not None


# ---------------------------------------------------------------------------
# And the rails must hold on the honest-refusal paths too, where there is no
# pool to route to.
# ---------------------------------------------------------------------------

def test_an_unplaceable_subject_refuses_rather_than_scoring():
    """No warmed town reaches this subject, so there is no pool. The seam must
    refuse, not fall through to the London default."""
    nowhere = {"address": "Rue de Rivoli, Paris", "beds": 2, "sqft": 700,
               "price": "£300,000", "description": "An apartment."}
    with _Poison() as poison:
        out = tools.score_listing(fields=nowhere, reads=PLAIN_READS)
        assert poison.calls == [], "engine.score ran without a routed pool"
        assert "error" in (out.get("value") or {}), \
            "an unplaceable subject must return the honest refusal"


# ---------------------------------------------------------------------------
# One run, one set of numbers.
# ---------------------------------------------------------------------------

def test_the_payload_taste_and_the_engine_taste_are_the_same_number():
    """The seam computes taste twice — once for the taste payload, once inside
    engine.score — and two taste numbers in one payload that disagree would be
    worse than either. They agree by construction: the same host read, weighted
    by the same person object."""
    for weights in (None, {axis: 5.0 for axis in EIGHT_AXES}):
        with _Poison():
            listing = tools._ingest(dict(LEAMINGTON), None)
            result, _value, taste, _comps, _ctx = tools._score_core(
                listing, reads=PLAIN_READS, weights=weights)
            assert result is not None
            assert taste["score"] == result.taste.score, \
                ("payload taste %s vs engine taste %s (weights=%s)"
                 % (taste["score"], result.taste.score, bool(weights)))


def test_the_seam_produces_what_the_tool_layer_never_could():
    """Gates, a composite, a confidence report and merged flags — the whole
    reason for the seam. The forked tool layer had none of them."""
    with _Poison():
        listing = tools._ingest(dict(LEAMINGTON), None)
        result, _v, _t, _c, _ctx = tools._score_core(listing, reads=PLAIN_READS)
        assert result.composite is not None
        assert result.rules is not None and result.rules.score is not None
        assert result.confidence is not None and result.confidence.overall is not None
        assert result.flags is not None


# ---------------------------------------------------------------------------
# Q1 — a payload may never use a sticky search, or someone else's weights,
# without saying so.
# ---------------------------------------------------------------------------

def test_the_payload_names_the_search_and_the_profile_it_used():
    with _Poison():
        out = tools.score_listing(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        ctx = out["context"]
        assert ctx["scored"] is True
        assert ctx["search"]["source"] == "default"
        assert "run situate" in ctx["search"]["note"]
        assert ctx["profile"]["source"] in ("shipped_demo", "user", "missing")


def test_a_saved_search_is_picked_up_and_named():
    from gaff_engine import session
    with _Poison():
        session.save(session.search_from_answers(
            {"mode": "buy", "nation": "england", "town": "LEAMINGTON SPA",
             "constraints": ["min_beds>=2"]}), None)
        out = tools.score_listing(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        assert out["context"]["search"]["source"] == "session"
        assert out["context"]["search"]["writtenAt"]


def test_show_work_goes_through_the_same_seam():
    with _Poison() as poison:
        work = tools.show_work(fields=dict(LEAMINGTON), reads=PLAIN_READS)
        assert poison.calls, "show_work did not reach the seam"
        assert work["context"]["scored"] is True
        assert work["rendered"]


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_engine.py.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as exc:                        # noqa: BLE001
                failures += 1
                print("FAIL %s: %s" % (name, exc))
    print("\n%s" % ("all score-seam guard rails hold" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
