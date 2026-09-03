"""spec 11 §9.1 — deterministic units for the Pairwise-Bayesian taste model.
Pure/offline: no cohort, no network. Each assertion is hand-checkable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gaff_engine import preference as P


def _approx(a, b, eps=1e-6):
    return abs(a - b) <= eps


def test_feature_map_shape_and_interaction():
    reads = {a: 10.0 for a in P.BASE_AXES}
    assert P.DIM == len(P.BASE_AXES) + 1 == 7
    fp = P.feature_map(reads, period_authentic=True)
    assert fp == [1.0] * 7, fp                       # all axes 10 -> 1.0, interaction 1*1
    fn = P.feature_map(reads, period_authentic=False)
    assert fn[-1] == 0.0 and fn[:6] == [1.0] * 6      # interaction off when not period
    # interaction tracks character x period, not the other axes
    r2 = {**{a: 5.0 for a in P.BASE_AXES}, "character_bones": 8.0}
    assert _approx(P.feature_map(r2, True)[-1], 0.8)
    print("PASS feature_map shape + interaction")


def test_intro_to_prior_ranked_and_open():
    mu0, sig = P.intro_to_prior({"tastePriorities": ["design_finish", "light_and_volume"]})
    di, li = P.FEATURES.index("design_finish"), P.FEATURES.index("light_and_volume")
    oi = P.FEATURES.index("outdoor_space")                       # unmentioned
    assert mu0[di] > mu0[li] > 0.0                              # decreasing by rank
    assert mu0[oi] == 0.0                                        # unmentioned -> 0 mean
    assert sig[di] < sig[oi]                                     # mentioned tighter than open
    assert sig[P.FEATURES.index(P.INTERACTION)] == P._S_MID
    # empty parse -> zero mean, open, interaction mid
    m2, s2 = P.intro_to_prior({})
    assert m2 == [0.0] * 7 and s2[:6] == [P._S_OPEN] * 6
    # anti-signal map pushes a negative mean only when a phrase resolves
    m3, s3 = P.intro_to_prior({"antiSignals": ["marble"]}, anti_axis_map={"marble": "design_finish"})
    assert m3[P.FEATURES.index("design_finish")] == -P._A0
    print("PASS intro_to_prior ranked + open + anti-map")


def test_invert_identity():
    H = [[4.0, 1.0, 0.0], [1.0, 3.0, 0.0], [0.0, 0.0, 2.0]]
    inv = P._invert(H)
    for i in range(3):
        for j in range(3):
            prod = sum(H[i][k] * inv[k][j] for k in range(3))
            assert _approx(prod, 1.0 if i == j else 0.0, 1e-9), (i, j, prod)
    print("PASS _invert gives H^-1")


def test_fit_zero_obs_is_prior():
    mu0, sig = P.intro_to_prior({"tastePriorities": ["design_finish"]})
    w, cov, n, _, conv = P.fit_posterior([], mu0, sig)
    assert n == 0 and conv
    assert w == mu0                                             # no data -> prior mean
    for i in range(P.DIM):                                       # cov == diag(sigma0^2)
        assert _approx(cov[i][i], sig[i] ** 2, 1e-9)
    print("PASS fit with no obs returns the prior")


def test_fit_recovers_sign_and_shrinks_covariance():
    # a synthetic user who likes design_finish (feature idx) and dislikes character
    fin = P.FEATURES.index("design_finish")
    cha = P.FEATURES.index("character_bones")
    w_true = [0.0] * P.DIM
    w_true[fin], w_true[cha] = 2.5, -2.5
    # homes vary only on those two axes; build deterministic pairwise obs
    import itertools
    homes = []
    for f in (0.2, 0.5, 0.8):
        for c in (0.2, 0.5, 0.8):
            v = [0.5] * P.DIM
            v[fin], v[cha] = f, c
            homes.append(v)
    obs = []
    for a, b in itertools.combinations(range(len(homes)), 2):
        z = [homes[a][k] - homes[b][k] for k in range(P.DIM)]
        ua = sum(w_true[k] * homes[a][k] for k in range(P.DIM))
        ub = sum(w_true[k] * homes[b][k] for k in range(P.DIM))
        if ua == ub:
            continue
        obs.append((z, 1 if ua > ub else 0))
    mu0 = [0.0] * P.DIM
    sig = [1.0] * P.DIM
    w, cov, n, _, conv = P.fit_posterior(obs, mu0, sig)
    assert conv, "Newton did not converge"
    assert w[fin] > 0.5 and w[cha] < -0.5, (w[fin], w[cha])     # signs recovered
    # posterior variance on the informed axes shrank below the prior
    assert cov[fin][fin] < sig[fin] ** 2 and cov[cha][cha] < sig[cha] ** 2
    print("PASS fit recovers signs (fin %+.2f char %+.2f) + shrinks covariance" % (w[fin], w[cha]))


def test_score_bounds_and_band_shrinks():
    mu0 = [0.0] * P.DIM
    sig = [1.0] * P.DIM
    # prior (uninformed) vs a fit with many obs on axis 0
    fin = P.FEATURES.index("design_finish")
    obs = []
    for t in range(12):
        z = [0.0] * P.DIM
        z[fin] = 0.6                                            # A better on finish
        obs.append((z, 1))
    w0, cov0, *_ = P.fit_posterior([], mu0, sig)
    w1, cov1, *_ = P.fit_posterior(obs, mu0, sig)
    phi = [0.5] * P.DIM
    phi[fin] = 0.9
    ubar, s = 0.0, 1.0
    sc0, band0 = P.home_score(w0, cov0, phi, ubar, s)
    sc1, band1 = P.home_score(w1, cov1, phi, ubar, s)
    assert 0.0 <= sc0 <= 10.0 and 0.0 <= sc1 <= 10.0
    assert band1 < band0, (band0, band1)                        # data narrows the band
    # score monotone in utility
    assert P.score(1.0, 0.0, 1.0) > P.score(-1.0, 0.0, 1.0)
    print("PASS score in [0,10], band shrinks %.2f -> %.2f with data" % (band0, band1))


def test_select_pair_argmax_and_min_z():
    # three homes; the informative pair is the one with the largest, uncertain difference
    phi_by_id = {
        "x": [0.5] * P.DIM, "y": [0.5] * P.DIM, "z": [0.5] * P.DIM,
    }
    fin = P.FEATURES.index("design_finish")
    phi_by_id["y"] = phi_by_id["y"][:]; phi_by_id["y"][fin] = 0.9
    phi_by_id["z"] = phi_by_id["z"][:]; phi_by_id["z"][fin] = 0.51   # nearly identical to x
    w = [0.0] * P.DIM
    cov = [[1.0 if i == j else 0.0 for j in range(P.DIM)] for i in range(P.DIM)]
    q = P.select_pair(["x", "y", "z"], phi_by_id, w, cov, min_z=0.15)
    assert q is not None and {q["aId"], q["bId"]} == {"x", "y"}, q   # x vs z below min_z
    assert q["axisFocus"] == "design_finish"
    # all-identical -> no admissible pair
    same = {"a": [0.5] * P.DIM, "b": [0.5] * P.DIM}
    assert P.select_pair(["a", "b"], same, w, cov, min_z=0.15) is None
    print("PASS select_pair picks the informative pair + honours min_z")


if __name__ == "__main__":
    test_feature_map_shape_and_interaction()
    test_intro_to_prior_ranked_and_open()
    test_invert_identity()
    test_fit_zero_obs_is_prior()
    test_fit_recovers_sign_and_shrinks_covariance()
    test_score_bounds_and_band_shrinks()
    test_select_pair_argmax_and_min_z()
    print("test_preference OK")
