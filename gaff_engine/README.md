# gaff_engine — U1: the deterministic core (Milestone M0)

U1 is the first build unit of the Gaff **Buy slice** walking skeleton, built
under **approach A** (engine-as-tested-library): the schemas, the contract
validator and the pure composite math as a real, tested Python module with the
golden De Beauvoir fixture as its oracle. No LLM, no I/O, standard library only
(pytest is used for the test but is optional — see below).

- `schemas.py` — dataclasses + enums for the core contracts of `docs/spec/01-domain.md` §5
  (`Person`, `Search`, `Listing`, `ScoreResult`/`ValueVerdict`, `ComponentSpec`, `ConfidenceReport`, …),
  including the additive P3 fields §9 folds in (the four gauge fields, `tasteAdjustments[]`, `rules.excluded`).
- `validate.py` — `validate(obj)` walks a dataclass off its type hints and returns a
  list of contract violations (empty == valid): the "schema validator" from `docs/build-plan.md`.
- `composite.py` — the pure `taste_score(axis_breakdown, adjustments)` and
  `composite(taste, rules, value, mix)` from `03-engine.md` §5.1/§5.6.
- `fixtures/de_beauvoir.py` — the golden `score.result@1` (01-domain §5.5b) plus its
  Person/Search/Listing/ComponentSpec siblings, self-consistent at taste **8.2** / composite **7.8**.

Golden arithmetic (03-engine §5.1/§5.6): `base = Σ(score·weight)/Σweight = 426.5/54.0 = 7.90`;
`taste = clamp(7.90 + 0.30 named-love) = 8.2`; `composite = round((8.2·55 + 7.5·20 + 7.2·25)/100, 1) = 7.8`.

Run the test (from the repo root, `property-taste/`):

```
python3 tests/test_u1_golden.py        # plain stdlib, no deps  ->  RESULT: PASS (7/7)
python3 -m pytest tests/test_u1_golden.py -v   # same tests, if pytest is installed
```
