"""U8 tests — the taste eval harness (03-engine §7), verify-by-nature.

Two kinds of check:
* the pure metrics (MAE, tie-corrected Spearman, within-band) against
  hand-computed toy vectors — so the ruler itself is trusted;
* the recorded calibration reproduces the published claims (1.27/0.77,
  1.35/0.79, text 1.64/0.63) and the image ablation lift is positive.

DETERMINISTIC: reads only the on-disk data/round{1,2}_scores.json. No network.

    python3 -m pytest tests/test_u8_eval.py -v
    python3 tests/test_u8_eval.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.eval import (  # noqa: E402
    CLAIMS, EvalReport, ProfileLeakageError, ablation, assert_disjoint,
    average_ranks, bootstrap_ci, calibration_reports, compare_retest, evaluate,
    evaluate_scorer, load_retest_scores, mae, pairwise_preference_accuracy,
    precision_at_k, recall_at_k, run_calibration, save_retest_template,
    spearman, within,
)


# ---------------------------------------------------------------------------
# 1 · MAE — hand-checked.
# ---------------------------------------------------------------------------

def test_mae_hand_checked():
    pred = {"a": 5.0, "b": 8.0, "c": 2.0}
    act = {"a": 4.0, "b": 6.0, "c": 2.0}       # errors 1, 2, 0 → mean 1.0
    assert mae(pred, act) == 1.0


def test_mae_zero_when_identical():
    pred = {"a": 7.0, "b": 3.0}
    assert mae(pred, dict(pred)) == 0.0


def test_mae_ignores_unpaired_keys():
    pred = {"a": 5.0, "b": 8.0, "extra": 9.0}
    act = {"a": 4.0, "b": 6.0}                  # 'extra' has no actual → excluded
    assert mae(pred, act) == 1.5                # (1 + 2)/2


# ---------------------------------------------------------------------------
# 2 · Ranks + Spearman — perfect, reversed, tie-corrected.
# ---------------------------------------------------------------------------

def test_average_ranks_no_ties():
    assert average_ranks([30.0, 10.0, 20.0]) == [3.0, 1.0, 2.0]


def test_average_ranks_with_ties():
    # two-way tie for the bottom two positions → both get rank 1.5.
    assert average_ranks([5.0, 5.0, 9.0]) == [1.5, 1.5, 3.0]
    # three-way tie → all get (1+2+3)/3 = 2.0.
    assert average_ranks([7.0, 7.0, 7.0]) == [2.0, 2.0, 2.0]


def test_spearman_perfect_and_reversed():
    pred = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}
    same = {"a": 10.0, "b": 20.0, "c": 30.0, "d": 40.0}   # monotone ↑ → +1
    rev = {"a": 40.0, "b": 30.0, "c": 20.0, "d": 10.0}    # monotone ↓ → −1
    assert spearman(pred, same) == 1.0
    assert spearman(pred, rev) == -1.0


def test_spearman_flat_series_is_zero():
    pred = {"a": 5.0, "b": 5.0, "c": 5.0}       # no variance → no rank signal
    act = {"a": 1.0, "b": 2.0, "c": 3.0}
    assert spearman(pred, act) == 0.0


def test_within_band():
    pred = {"a": 5.0, "b": 8.0, "c": 2.0}
    act = {"a": 4.0, "b": 6.0, "c": 2.0}       # errors 1, 2, 0
    assert within(pred, act, 1.0) == (2, 3)     # a, c within 1.0
    assert within(pred, act, 1.5) == (2, 3)
    assert within(pred, act, 2.0) == (3, 3)


# ---------------------------------------------------------------------------
# 3 · evaluate() — the report bundles the metrics + worst-error rows.
# ---------------------------------------------------------------------------

def test_evaluate_report_shape_and_worst():
    pred = {"1": 6.0, "2": 5.0, "3": 9.0}
    act = {"1": 4.0, "2": 5.0, "3": 8.5}       # errors 2.0, 0, 0.5
    rep = evaluate(pred, act, label="toy", labels={"1": "Barbican"})
    assert isinstance(rep, EvalReport) and rep.n == 3
    assert rep.mae == round((2.0 + 0.0 + 0.5) / 3, 4)
    assert rep.worst[0][0] == "Barbican" and rep.worst[0][3] == 2.0  # biggest miss, named


# ---------------------------------------------------------------------------
# 4 · The recorded calibration reproduces the published claims.
# ---------------------------------------------------------------------------

def test_round1_reproduces_claim():
    reps = calibration_reports()
    r1 = reps["round1"]
    assert r1.n == CLAIMS["round1"]["n"] == 15
    assert abs(r1.mae - 1.27) <= 0.01          # 1.2667 → 1.27
    assert abs(r1.spearman - 0.77) <= 0.01     # 0.7707 → 0.77
    assert r1.meets(CLAIMS["round1"]["mae"], CLAIMS["round1"]["spearman"])


def test_round2_final_reproduces_claim():
    reps = calibration_reports()
    rf = reps["round2_final"]
    assert rf.n == 11
    assert abs(rf.mae - 1.35) <= 0.01          # 1.3455
    assert abs(rf.spearman - 0.79) <= 0.01     # 0.7918
    assert rf.meets(1.35, 0.79)


def test_round2_text_prior_reproduces_claim():
    reps = calibration_reports()
    rt = reps["round2_text"]
    assert abs(rt.mae - 1.64) <= 0.01          # 1.6364
    assert abs(rt.spearman - 0.63) <= 0.01     # 0.6267


def test_image_ablation_lift_is_positive():
    """Images earn their cost: MAE falls and Spearman rises text → final."""
    abl = calibration_reports()["ablation"]
    assert abl.mae_lift > 0                     # 1.64 → 1.35
    assert abl.spearman_lift > 0               # 0.63 → 0.79
    assert round(abl.mae_lift, 2) == 0.29
    assert round(abl.spearman_lift, 2) == 0.17


def test_run_calibration_passes_and_asserts():
    reps = run_calibration(check=True)          # raises on drift
    assert reps["passed"] is True
    assert all(reps["checks"].values())


# ---------------------------------------------------------------------------
# 5 · Live mode — evaluate_scorer is a drop-in over the same metrics.
# ---------------------------------------------------------------------------

def test_evaluate_scorer_live_dropin():
    """A scorer_fn over cases produces the identical measurement as evaluate()."""
    cases = [{"id": "1", "x": 6.0}, {"id": "2", "x": 5.0}, {"id": "3", "x": 9.0}]
    actual = {"1": 4.0, "2": 5.0, "3": 8.5}
    rep = evaluate_scorer(cases, lambda c: c["x"], actual, label="live")
    # identical to feeding the predictions straight into evaluate().
    ref = evaluate({"1": 6.0, "2": 5.0, "3": 9.0}, actual)
    assert rep.mae == ref.mae and rep.spearman == ref.spearman and rep.n == 3



# ---------------------------------------------------------------------------
# 6 · Bootstrap CIs — seeded, deterministic, additive to the point estimates.
# ---------------------------------------------------------------------------

_TOY_PRED = {str(i): float(i) for i in range(1, 11)}
_TOY_ACT = {str(i): float(i) + (0.5 if i % 2 else -1.5) for i in range(1, 11)}


def test_bootstrap_ci_is_deterministic_for_a_seed():
    a = bootstrap_ci(_TOY_PRED, _TOY_ACT, mae, n_boot=500, seed=42)
    b = bootstrap_ci(_TOY_PRED, _TOY_ACT, mae, n_boot=500, seed=42)
    assert a == b
    assert a[0] <= a[1]


def test_bootstrap_ci_brackets_the_point_estimate():
    point = mae(_TOY_PRED, _TOY_ACT)                      # mean of 0.5s and 1.5s
    lo, hi = bootstrap_ci(_TOY_PRED, _TOY_ACT, mae, n_boot=1000, seed=7)
    assert lo <= point <= hi
    assert lo >= 0.5 and hi <= 1.5                        # errors are only 0.5 or 1.5


def test_bootstrap_ci_zero_width_when_perfect():
    perfect = {str(i): float(i) for i in range(1, 8)}
    assert bootstrap_ci(perfect, dict(perfect), mae, n_boot=200, seed=1) == (0.0, 0.0)


def test_bootstrap_flat_resamples_contribute_zero_not_skipped():
    """The documented convention: a resample where one side is all-tied is NOT
    skipped — Spearman's flat-series convention scores it 0.0 and it stays in
    the interval. With an all-tied predicted series every resample is flat, so
    the interval collapses to exactly (0, 0); if flat draws were skipped this
    would raise 'failed on every resample' instead."""
    flat_pred = {"a": 5.0, "b": 5.0, "c": 5.0}
    act = {"a": 1.0, "b": 2.0, "c": 3.0}
    assert bootstrap_ci(flat_pred, act, spearman, n_boot=100, seed=2) == (0.0, 0.0)


def test_evaluate_ci_mode_is_additive_only():
    """ci=True attaches intervals without moving any existing number."""
    plain = evaluate(_TOY_PRED, _TOY_ACT)
    rich = evaluate(_TOY_PRED, _TOY_ACT, ci=True, n_boot=500, seed=3)
    assert (rich.mae, rich.spearman, rich.n) == (plain.mae, plain.spearman, plain.n)
    assert plain.mae_ci is None and plain.spearman_ci is None
    assert rich.mae_ci[0] <= rich.mae <= rich.mae_ci[1]
    assert "MAE95" in rich.summary() and "MAE95" not in plain.summary()


def test_calibration_reports_carry_intervals_without_drift():
    """The recorded rounds gain CIs; the asserted claim numbers stay put."""
    reps = calibration_reports(ci=True, n_boot=500, seed=11)
    r1 = reps["round1"]
    assert abs(r1.mae - 1.27) <= 0.01                     # the claim is untouched
    assert r1.mae_ci[0] <= r1.mae <= r1.mae_ci[1]
    assert r1.spearman_ci[0] <= r1.spearman <= r1.spearman_ci[1]
    assert calibration_reports()["round1"].mae_ci is None  # default stays bare


# ---------------------------------------------------------------------------
# 7 · Top-k metrics — threshold-set tie rule, hand-checked.
# ---------------------------------------------------------------------------

def test_precision_and_recall_at_k_no_ties():
    pred = {"a": 9.0, "b": 8.0, "c": 7.0, "d": 2.0, "e": 1.0}
    act = {"a": 9.0, "b": 8.0, "d": 7.0, "c": 2.0, "e": 1.0}
    # pred top3 {a,b,c}; actual top3 {a,b,d} → 2 shared.
    assert precision_at_k(pred, act, 3) == round(2 / 3, 4)
    assert recall_at_k(pred, act, 3) == round(2 / 3, 4)
    assert precision_at_k(pred, act, 5) == 1.0            # k = n → everything


def test_top_k_boundary_ties_expand_the_set():
    """An actual tie AT the k-th place is never broken arbitrarily: both tied
    items count as truly-top, so a model picking either is not punished."""
    pred = {"a": 9.0, "b": 8.0, "c": 7.0, "d": 6.0}
    act = {"a": 9.0, "b": 8.0, "c": 8.0, "d": 1.0}        # b,c tied at 2nd
    assert precision_at_k(pred, act, 2) == 1.0            # {a,b} ⊆ {a,b,c}
    assert recall_at_k(pred, act, 2) == round(2 / 3, 4)   # 2 of the 3 truly-top


def test_pairwise_preference_accuracy_hand_checked():
    pred = {"a": 1.0, "b": 2.0, "c": 3.0}
    act = {"a": 1.0, "b": 3.0, "c": 2.0}                  # ab ✓, ac ✓, bc ✗
    assert pairwise_preference_accuracy(pred, act) == round(2 / 3, 4)


def test_pairwise_ties_follow_the_documented_rule():
    # predicted tie on a decided pair → 0.5 (chance credit, not a full miss)
    assert pairwise_preference_accuracy({"a": 5.0, "b": 5.0},
                                        {"a": 1.0, "b": 2.0}) == 0.5
    # all-actual-ties → no decided pairs → refuse rather than fake a number
    try:
        pairwise_preference_accuracy({"a": 1.0, "b": 2.0}, {"a": 3.0, "b": 3.0})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 8 · Profile-leakage guard — both directions, plus the load-time wiring.
# ---------------------------------------------------------------------------

def test_assert_disjoint_passes_and_raises_both_directions():
    assert_disjoint({"1", "2"}, {"3", "4"})               # disjoint: silent
    for a, b in ((["1", "2", "7"], ["7", "9"]), (["7", "9"], ["1", "2", "7"])):
        try:
            assert_disjoint(a, b)
            raise AssertionError("expected ProfileLeakageError")
        except ProfileLeakageError as e:
            assert "7" in str(e) and "homework" in str(e)


def test_calibration_loading_raises_on_a_declared_overlap():
    """A score file that declares profile_source_ids overlapping its own
    calibration ids must refuse to load — the wired invariant."""
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        data = _data_dir()                 # lab data/, or the shipped copies
        shutil.copy(os.path.join(data, "round2_scores.json"), tmp)
        with open(os.path.join(data, "round1_scores.json")) as fh:
            r1 = json.load(fh)
        r1["profile_source_ids"] = ["3", "999"]           # "3" is a calibration id
        with open(os.path.join(tmp, "round1_scores.json"), "w") as fh:
            json.dump(r1, fh)
        try:
            calibration_reports(tmp)
            raise AssertionError("expected ProfileLeakageError")
        except ProfileLeakageError as e:
            assert "3" in str(e) and "999" not in str(e)  # only the overlap is named
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_calibration_loading_checks_externally_declared_sources():
    calibration_reports(profile_source_ids={"listing-x"})  # disjoint: fine
    try:
        calibration_reports(profile_source_ids={"4"})      # id 4 is in both rounds
        raise AssertionError("expected ProfileLeakageError")
    except ProfileLeakageError as e:
        assert "4" in str(e)


def _data_dir():
    """The calibration data dir: the lab's repo-root ``data/`` when running
    from a checkout, else the shipped package data dir — the same fallback
    eval.py itself uses, so the round-file tests run in the assembled public
    tree too (where the files live at gaff_engine/data/, not ../data)."""
    lab = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    if os.path.isdir(lab):
        return lab
    from gaff_engine import paths
    return paths.shipped_data_dir()


def _sidecar_path():
    """The repo-local calibration_ids.json, or None when absent.

    Absence is EXPECTED in the assembled public package: the sidecar carries
    real portal listing ids and is deliberately never shipped (its own header
    and build_public's LOOSE_DATA both say so), so the two tests that read it
    SKIP there instead of failing a wheel user's suite."""
    path = os.path.join(_data_dir(), "calibration_ids.json")
    return path if os.path.exists(path) else None


def test_sidecar_declares_real_ids_and_sources():
    """The repo-local calibration_ids.json sidecar must carry a complete
    listing_ids map (ordinal → portal id) and a non-empty profile_source_ids
    list for BOTH rounds, so the leakage guard runs against the REAL id
    namespace on every default load — not just when a caller remembers to
    pass ids in. It lives outside the score files deliberately: those ship in
    the public package (build_public LOOSE_DATA) and real portal ids must not
    travel with them — which is also why this test reads ids from the sidecar
    rather than embedding any."""
    import json
    path = _sidecar_path()
    if path is None:
        print("SKIP test_sidecar_declares_real_ids_and_sources "
              "(repo-local sidecar not present — it never ships)")
        return
    with open(path) as fh:
        side = json.load(fh)
    for name, n in (("round1", 15), ("round2", 11)):
        id_map = side[name]["listing_ids"]
        assert set(id_map) == {str(i) for i in range(1, n + 1)}, name
        assert all(str(v).isdigit() and len(str(v)) >= 6 for v in id_map.values()), name
        assert side[name]["profile_source_ids"], name
    # and the score files themselves stay portal-id-free (the shipping contract)
    for fn in ("round1_scores.json", "round2_scores.json"):
        with open(os.path.join(_data_dir(), fn)) as fh:
            assert "listing_ids" not in json.load(fh), fn


def test_leakage_guard_fires_in_the_portal_id_namespace():
    """The finding this pins: an id declared in the PORTAL namespace (the real
    listing id of a calibration item, read from the sidecar map — never
    hardcoded here) must trip the guard, even though the score files key their
    scores by ordinals. Before the map the two namespaces could never collide,
    so a truthful declaration of genuine leakage false-passed."""
    import json
    path = _sidecar_path()
    if path is None:
        print("SKIP test_leakage_guard_fires_in_the_portal_id_namespace "
              "(repo-local sidecar not present — it never ships)")
        return
    with open(path) as fh:
        real_id = str(json.load(fh)["round1"]["listing_ids"]["1"])
    assert real_id.isdigit() and len(real_id) >= 6         # genuinely portal-shaped
    try:
        calibration_reports(profile_source_ids={real_id})
        raise AssertionError("expected ProfileLeakageError")
    except ProfileLeakageError as e:
        assert real_id in str(e)


def _tmp_rounds(round1_extra, sidecar):
    """A temp data dir: shipped round2, shipped round1 + ``round1_extra``
    fields, and an explicit ``sidecar`` blob (so the shipped repo-local
    sidecar cannot leak in through the paths fallback)."""
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(_data_dir(), "round2_scores.json"), tmp)
    with open(os.path.join(_data_dir(), "round1_scores.json")) as fh:
        r1 = json.load(fh)
    r1.update(round1_extra)
    with open(os.path.join(tmp, "round1_scores.json"), "w") as fh:
        json.dump(r1, fh)
    with open(os.path.join(tmp, "calibration_ids.json"), "w") as fh:
        json.dump(sidecar, fh)
    return tmp


def test_namespace_mismatch_cannot_silently_pass():
    """Portal-shaped source ids + ordinal-only calibration keys + no
    listing_ids map = a disjointness check that could never fire. That
    configuration must refuse to load rather than false-pass."""
    import shutil
    tmp = _tmp_rounds({"profile_source_ids": ["10000001"]},  # synthetic portal-shaped
                      sidecar={})
    try:
        calibration_reports(tmp)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "listing_ids" in str(e) and "collide" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_incomplete_listing_ids_map_is_refused():
    """A map that skips a calibration item leaves that item invisible to the
    real-id guard — refused, naming the unmapped ordinal."""
    import shutil
    # synthetic map covering 14 of round 1's 15 items — "3" left unmapped
    partial = {str(i): "1000%04d" % i for i in range(1, 16) if i != 3}
    tmp = _tmp_rounds({"profile_source_ids": ["10000001"], "listing_ids": partial},
                      sidecar={})
    try:
        calibration_reports(tmp)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "incomplete" in str(e) and "3" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_leakage_via_a_synthetic_id_map_is_caught():
    """End-to-end in the real-id namespace with fully synthetic ids: when the
    map says calibration item 5 IS listing 10000005 and the profile declares
    10000005 as a source, loading must refuse."""
    import shutil
    full = {str(i): "1000%04d" % i for i in range(1, 16)}
    tmp = _tmp_rounds({"profile_source_ids": ["10000005"], "listing_ids": full},
                      sidecar={})
    try:
        calibration_reports(tmp)
        raise AssertionError("expected ProfileLeakageError")
    except ProfileLeakageError as e:
        assert "10000005" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9 · Test-retest self-baseline — sealed template, blind, compared honestly.
# ---------------------------------------------------------------------------

def test_retest_template_is_blind_and_sealed():
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        original = {"1": 6.0, "2": 4.8, "3": 9.0}
        path = os.path.join(tmp, "retest_round1.json")
        save_retest_template(original, path, label="round1 retest",
                             labels={"1": "Barbican triplex"})
        with open(path) as fh:
            blob = json.load(fh)
        assert sorted(blob["scores"]) == ["1", "2", "3"]
        assert all(v is None for v in blob["scores"].values())   # blind: no originals
        assert "6.0" not in open(path).read()
        try:
            save_retest_template(original, path)                 # sealed: no clobber
            raise AssertionError("expected FileExistsError")
        except FileExistsError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retest_load_refuses_a_partial_rescoring():
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        path = save_retest_template({"1": 6.0, "2": 4.8, "3": 9.0},
                                    os.path.join(tmp, "t.json"))
        with open(path) as fh:
            blob = json.load(fh)
        blob["scores"]["1"] = 5.5                                # 2 and 3 unscored
        with open(path, "w") as fh:
            json.dump(blob, fh)
        try:
            load_retest_scores(path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "2" in str(e) and "3" in str(e)
        blob["scores"].update({"2": 4.0, "3": 8.0})
        with open(path, "w") as fh:
            json.dump(blob, fh)
        assert load_retest_scores(path) == {"1": 5.5, "2": 4.0, "3": 8.0}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retest_load_refuses_a_deleted_entry():
    """The template is filled in by hand-editing JSON, so a dropped LINE is as
    realistic a slip as a null left in place — the deleted key must be named,
    not silently intersected away (which would shrink n and bias the
    self-consistency ceiling toward whichever items were rescored)."""
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        path = save_retest_template({"1": 6.0, "2": 4.8, "3": 9.0},
                                    os.path.join(tmp, "t.json"))
        with open(path) as fh:
            blob = json.load(fh)
        blob["scores"] = {"1": 5.0, "3": 8.0}              # "2" deleted outright
        with open(path, "w") as fh:
            json.dump(blob, fh)
        try:
            load_retest_scores(path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "2" in str(e) and "incomplete" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retest_load_refuses_a_typoed_stray_id():
    """A score keyed by an id outside the template's sealed list is a typo
    that would otherwise vanish in the comparison's key intersection."""
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        path = save_retest_template({"1": 6.0, "2": 4.8},
                                    os.path.join(tmp, "t.json"))
        with open(path) as fh:
            blob = json.load(fh)
        blob["scores"] = {"1": 5.0, "2": 4.0, "99": 7.0}   # "99" is not an item
        with open(path, "w") as fh:
            json.dump(blob, fh)
        try:
            load_retest_scores(path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "99" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compare_retest_refuses_mismatched_key_sets():
    """A retest covering different items than the original is a different
    experiment — refuse, naming the ids, instead of quietly intersecting
    (which used to crash unhelpfully in spearman at 1 surviving key)."""
    original = {"1": 4.0, "2": 5.0, "3": 8.5}
    try:
        compare_retest(original, {"1": 4.5, "3": 8.0})     # "2" missing
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "missing from the retest" in str(e) and "2" in str(e)
    try:
        compare_retest(original, {"1": 4.5, "2": 5.0, "3": 8.0, "4": 6.0})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "in the retest only" in str(e) and "4" in str(e)


def test_compare_retest_frames_self_mae_as_the_ceiling():
    original = {"1": 4.0, "2": 5.0, "3": 8.5}
    retest = {"1": 4.5, "2": 5.5, "3": 8.0}               # errors all 0.5, same order
    rep = compare_retest(original, retest)
    assert rep["self_mae"] == 0.5
    assert rep["self_spearman"] == 1.0
    assert rep["n"] == 3
    assert "ceiling" in rep["framing"]


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest).
# ---------------------------------------------------------------------------

def _run_standalone():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
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
    if failures:
        print("RESULT: FAIL (%d/%d passed, %d failed)" % (total - failures, total, failures))
    else:
        print("RESULT: PASS (%d/%d passed)" % (total, total))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
