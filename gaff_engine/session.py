"""S1 — the session store: the Search constructor, and what persists between calls.

Why this module exists
----------------------
Five shipped modules take a ``Search`` and there has never been a way to build
one. ``feed.assemble_feed``, ``viewing.generate_checklist``,
``viewing.prepare_viewing``, ``outputs.market_report``, ``rules.evaluate_gates``
and ``together.combine_fit`` all require it; the only ``Search`` in the tree is
``fixtures.de_beauvoir.GOLDEN_SEARCH``, which carries a hand-drawn East London
polygon. :func:`search_from_answers` is the missing constructor — the ``Search``
twin of :func:`gaff_engine.elicit.person_from_answers` — and the reason it is a
foundation rather than a feature.

The three rules this module enforces
------------------------------------
* **State never ships.** Everything here writes under :func:`paths.state_path`,
  which is the user cache and nothing else. The shipped tier exists so a cold
  install can still answer about the seeded streets; a *shipped search* would
  answer a question the user did not ask. ``profile.json`` is the one deliberate
  exception and keeps its legacy path (Q4, below).
* **Every write is atomic.** Two Claude Desktop windows can hold one cache, so a
  write goes to a temp name in the same directory and is then ``os.replace``\\ d
  into place. A reader sees the old file or the new one, never half of either.
* **Every file carries a ``schemaVersion``.** A file written by a newer release
  is ignored with a plain note (:func:`search_in_use` renders it), never a crash
  and never a silent misread.

No polygon gate is ever generated. ``inside_polygon`` needs geometry a user will
not draw, so the area is carried as ``Area(label=town, confidence="rough")`` and
the gate is refused by name if asked for.

Q4, decided here (where second and third profiles live)
-------------------------------------------------------
The **unnamed** profile stays exactly where it is: ``<user cache>/profile.json``,
resolved by ``paths.data_file`` with the shipped demo behind it. **Named**
profiles live at ``<user cache>/state/profiles/<slug>.json`` with no shipped
fallback, ever. Moving the unnamed one would silently orphan any profile a user
has already written — ``data_file`` would stop finding it and fall through to the
demo with no visible change in the output, which is the exact shadow F-08 exists
to make visible. And the shipped fallback is right for one file and wrong for the
rest: ``profile.json`` has a deliberate demo default (the fictional "Sam"), while
a *shipped second person* would be meaningless. See :func:`profile_path`.

Deterministic: :func:`search_from_answers` reads no clock and touches no disk, so
the same answers always give the same ``Search`` (the ``id`` is derived from the
answers). The clock lives in exactly one place, the ``writtenAt`` stamp on the
saved file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union, get_args, get_origin, get_type_hints

from gaff_engine import paths
from gaff_engine.dashboard import MODE_MIX          # E9: import the mixes, never copy
from gaff_engine.schemas import (
    Area, Budget, Gate, Mode, Money, MoneyPeriod, Person, Ref, ScorerMix,
    Search, SearchStatus, TenureType, Threshold,
)

#: The session file's contract. Bumped only when a reader of the old shape would
#: be wrong, not when a field is added.
SESSION_SCHEMA = "gaff.session@1"

#: Where the session lives, relative to :func:`paths.state_dir`.
SESSION_FILE = "session.json"
PROFILES_DIR = "profiles"

#: The four UK nations. Asked, never inferred from a town name — Newport, Perth
#: and Hamilton each exist in more than one of them (F-01's hazard).
NATIONS = ("england", "wales", "scotland", "northern_ireland")

_NATION_ALIASES = {
    "eng": "england", "gb-eng": "england",
    "cymru": "wales", "gb-wls": "wales", "wal": "wales",
    "scot": "scotland", "alba": "scotland", "gb-sct": "scotland",
    "ni": "northern_ireland", "n_ireland": "northern_ireland",
    "northernireland": "northern_ireland", "gb-nir": "northern_ireland",
}

#: The default alert/show thresholds (the golden's, 01-domain §5.2).
DEFAULT_THRESHOLD = (6.0, 7.5)

#: How much over the stated ceiling a stretch reaches, as a percent. Mirrors
#: ``Person.riskAppetite.priceStretchPct`` and the golden's own arithmetic
#: (1,350,000 -> 1,417,500 = +5%).
DEFAULT_STRETCH_PCT = 5


class UnknownConstraint(ValueError):
    """A constraint names a gate code the rules layer cannot resolve.

    Its own class so the front door (F-01) can catch it and fold the code into
    "here is what I still need" rather than returning a usage error — while a
    typo still fails loudly instead of becoming a gate that never fires.
    """


# ---------------------------------------------------------------------------
# The constraint vocabulary — a subset of rules.RESOLVERS, with the operator
# fixed by the code rather than chosen by the caller. A user says what they
# need ("two bedrooms"); the comparison is the engine's business.
# ---------------------------------------------------------------------------

#: A number with an optional unit word: "2", "£500,000", "500k", "1.35m",
#: "1050 sqft", "90 years", "2 beds". Only a BARE trailing k/m multiplies —
#: "500 metres" is five hundred with a unit word, not five hundred million.
_NUMBER_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([a-z]*)$")


def _as_int(raw: Any) -> int:
    """A stated number, however a person actually types it."""
    if isinstance(raw, bool):                      # bool is an int subclass
        raise ValueError("expected a number, got a boolean")
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().lower().replace(",", "").replace("£", "").replace(" ", "")
    m = _NUMBER_RE.match(text)
    if not m:
        raise ValueError("expected a number, got %r" % raw)
    number, suffix = m.groups()
    mult = {"k": 1_000, "m": 1_000_000}.get(suffix, 1)   # any other word is a unit
    return int(round(float(number) * mult))


def _as_bool(raw: Any) -> bool:
    if raw is None or raw == "":
        return True                                # a bare "outdoor_present" means yes
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "yes", "y", "1", "required", "present"):
        return True
    if text in ("false", "no", "n", "0", "optional", "absent"):
        return False
    raise ValueError("expected yes/no, got %r" % raw)


def _as_tenures(raw: Any) -> List[str]:
    """A tenure list, validated against the closed enum.

    A typo here is the quiet kind of wrong: ``"freehole"`` would simply never
    match, and the user would read an unenforced gate as a cleared one.
    """
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out = []
    allowed = {t.value for t in TenureType}
    for item in items:
        value = str(item).strip().lower().replace(" ", "_").replace("-", "_")
        if value not in allowed:
            raise UnknownConstraint(
                "tenure_in: %r is not a tenure. Known: %s"
                % (item, ", ".join(sorted(allowed))))
        out.append(value)
    if not out:
        raise UnknownConstraint("tenure_in needs at least one tenure")
    return out


#: code -> (op, coercion, unit, soft-by-default). Every code here is a key of
#: ``rules.RESOLVERS``; ``inside_polygon`` is deliberately absent (see below).
CONSTRAINT_GATES: Dict[str, Tuple[str, Any, Optional[str], bool]] = {
    "min_beds":        (">=", _as_int, None, False),
    "min_baths":       (">=", _as_int, None, False),
    "min_sqft":        (">=", _as_int, "sqft", False),
    "min_receptions":  (">=", _as_int, None, False),
    "max_price":       ("<=", _as_int, None, False),
    "min_price":       (">=", _as_int, None, False),
    # Soft by default: a short lease flags and docks, it does not auto-exclude
    # (the golden's own rationale, and Person.riskAppetite.leaseYearsFloor).
    "lease_years_min": (">=", _as_int, "years", True),
    "tenure_in":       ("in", _as_tenures, None, False),
    "outdoor_present": ("==", _as_bool, None, False),
}

#: Refused by name rather than silently dropped, so the reason is legible.
_REFUSED_GATES = {
    "inside_polygon": "inside_polygon needs a drawn polygon; a stated town is "
                      "carried as the search Area (confidence 'rough'), not as "
                      "a geo gate",
}

# "min_beds>=2", "min_beds = 2", "min_beds: 2", "min_beds 2", "outdoor_present".
_CONSTRAINT_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:>=|<=|==|=|:|>|<)?\s*(.*?)\s*$")


def _as_constraint_list(raw: Any) -> List[Any]:
    """Whatever the caller sent -> a list of constraints.

    F-01's contract calls this argument *repeatable*, and a repeatable argument
    with one value arrives as a scalar. Without this, one bare
    ``"min_beds>=2"`` would be iterated character by character and refused as
    ``'m' is not a constraint`` — a nonsense error at the one door that cannot
    afford a refusal.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, dict)):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return [raw]


def _parse_constraint(raw: Any) -> Tuple[str, Any, Optional[bool], Optional[str]]:
    """One constraint -> ``(code, raw_value, soft_override, rationale)``.

    Accepts a dict (``{"code": ..., "value": ..., "soft": ..., "rationale":
    ...}``) or the compact string an MCP host or the CLI will actually send.
    """
    if isinstance(raw, dict):
        code = str(raw.get("code") or "").strip().lower()
        return code, raw.get("value"), raw.get("soft"), raw.get("rationale")
    m = _CONSTRAINT_RE.match(str(raw))
    if not m:
        raise UnknownConstraint("could not read the constraint %r" % raw)
    code, value = m.group(1).strip().lower(), m.group(2)
    return code, (value if value != "" else None), None, None


def gate_from_constraint(raw: Any) -> Gate:
    """One user constraint -> one schema-valid :class:`Gate`.

    The operator is fixed by the code, not supplied by the caller: a user states
    a need, and how it is compared is the engine's business. An unknown code
    raises :class:`UnknownConstraint` naming the whole vocabulary — a gate that
    silently never fires reads to the user as a cleared gate.
    """
    code, value, soft_override, rationale = _parse_constraint(raw)
    if code in _REFUSED_GATES:
        raise UnknownConstraint(_REFUSED_GATES[code])
    if code not in CONSTRAINT_GATES:
        raise UnknownConstraint(
            "%r is not a constraint this engine can gate on. Known: %s"
            % (code, ", ".join(sorted(CONSTRAINT_GATES))))
    op, coerce, unit, soft_default = CONSTRAINT_GATES[code]
    if value is None and coerce is not _as_bool:
        raise UnknownConstraint("%s needs a value, e.g. '%s>=2'" % (code, code))
    try:
        parsed = coerce(value)
    except UnknownConstraint:
        raise
    except (TypeError, ValueError) as exc:
        raise UnknownConstraint("%s: %s" % (code, exc))
    soft = soft_default if soft_override is None else bool(soft_override)
    return Gate(code=code, op=op, value=parsed, unit=unit,
                rationale=rationale or "stated by the user at situate",
                # Match the golden's shape: soft is set only when it is True.
                soft=True if soft else None)


# ---------------------------------------------------------------------------
# The constructors.
# ---------------------------------------------------------------------------

def normalise_nation(raw: Any) -> Optional[str]:
    """One of :data:`NATIONS`, or ``None`` when unstated. Raises on unknown.

    Loud rather than lenient: an unrecognised nation quietly becoming ``None``
    would make the feasibility table claim Price Paid coverage for Scotland.
    """
    if raw is None or str(raw).strip() == "":
        return None
    key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    key = _NATION_ALIASES.get(key.replace("_", ""), _NATION_ALIASES.get(key, key))
    if key not in NATIONS:
        raise ValueError("unknown nation %r. Known: %s" % (raw, ", ".join(NATIONS)))
    return key


def _mode(raw: Any, default: str = "buy") -> Mode:
    if raw is None or str(raw).strip() == "":
        return Mode(default)
    key = str(getattr(raw, "value", raw)).strip().lower()
    try:
        return Mode(key)
    except ValueError:
        raise ValueError("unknown mode %r. Known: %s"
                         % (raw, ", ".join(m.value for m in Mode)))


def _stable_id(prefix: str, payload: Any) -> str:
    """A deterministic id derived from content, no clock (elicit's convention)."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return prefix + hashlib.sha1(("gaff|" + blob).encode("utf-8")).hexdigest()[:24].upper()


def mix_for(mode: Any) -> ScorerMix:
    """The mode's default Scorer Mix (03-engine §5.0; Buy 55/20/25 is authoritative)."""
    taste, rules, value = MODE_MIX.get(str(getattr(mode, "value", mode)), MODE_MIX["buy"])
    return ScorerMix(taste=taste, rules=rules, value=value)


def _budget(answers: Dict[str, Any], mode: Mode) -> Optional[Budget]:
    """The stated band as a :class:`Budget`, or ``None`` when nothing is stated.

    Deliberately NOT a gate. The golden carries a budget and no ``max_price``
    gate: a ceiling is what the Value scorer reasons about, and turning it into
    a hard exclusion would drop a home at £500,001 that the user would want to
    see. A ceiling only becomes a gate when the user names it as the constraint
    that kills (``max_price<=500000``).
    """
    lo = answers.get("budget_min", answers.get("budgetMin"))
    hi = answers.get("budget_max", answers.get("budgetMax"))
    if lo is None and hi is None:
        return None
    # A rent budget is per calendar month; a purchase budget is a total.
    period = MoneyPeriod.PCM if mode == Mode.RENT else MoneyPeriod.TOTAL

    def money(raw):
        return None if raw is None else Money(amount=_as_int(raw), currency="GBP",
                                              period=period)

    hi_money = money(hi)
    stretch = None
    if hi_money is not None:
        pct = answers.get("budget_stretch_pct", answers.get("priceStretchPct",
                                                            DEFAULT_STRETCH_PCT))
        pct = float(pct or 0)
        if pct > 0:
            stretch = Money(amount=int(round(hi_money.amount * (1 + pct / 100.0))),
                            currency="GBP", period=period)
    return Budget(min=money(lo), max=hi_money, stretchMax=stretch)


def _area(answers: Dict[str, Any]) -> Optional[Area]:
    """The stated place as an :class:`Area`. Never a polygon: a user will not
    draw one, and ``confidence="rough"`` says so honestly."""
    town = answers.get("town") or answers.get("outcode") or answers.get("area")
    if not town:
        return None
    return Area(label=str(town).strip(), confidence="rough", polygon=None)


def search_from_answers(answers: Dict[str, Any]) -> Search:
    """The minimal setup answers -> a valid :class:`Search`.

    Answers, all optional (F-01's hazard: partial input must return what is
    known, never a usage error — only genuinely *wrong* input raises):

    * ``mode`` — buy | rent | invest | dream. Default buy.
    * ``nation`` — england | wales | scotland | northern_ireland. Asked, never
      inferred; attached as ``search.nation`` (the ``epcSqft`` idiom — an honest
      attribute the schema has no slot for) and persisted beside the Search.
    * ``town`` or ``outcode`` — carried as ``Area(label=..., confidence="rough")``.
    * ``budget_min`` / ``budget_max`` (+ ``budget_stretch_pct``, default 5).
    * ``constraints`` — zero or more, each a code from :data:`CONSTRAINT_GATES`
      as a dict or a compact string (``"min_beds>=2"``). A single constraint may
      be sent bare rather than in a list.
    * ``name`` / ``person`` — to point ``personRef`` at the matching Person.

    Pure: no clock, no disk. The same answers give the same ``Search``.
    """
    answers = dict(answers or {})
    mode = _mode(answers.get("mode"))
    nation = normalise_nation(answers.get("nation"))
    gates = [gate_from_constraint(c) for c in _as_constraint_list(answers.get("constraints"))]
    area = _area(answers)
    budget = _budget(answers, mode)

    person = answers.get("person")
    person_id = getattr(person, "id", None)
    if person_id is None:
        from gaff_engine.elicit import _person_id      # same id for the same name
        person_id = _person_id(answers.get("name") or "You")

    show, alert = answers.get("threshold_show"), answers.get("threshold_alert")
    title = answers.get("title") or "%s, to %s" % (
        (area.label if area is not None else "anywhere"), mode.value)

    search = Search(
        id=_stable_id("search_", {
            "mode": mode.value, "nation": nation,
            "area": area.label if area is not None else None,
            "budget": [getattr(budget.min, "amount", None) if budget else None,
                       getattr(budget.max, "amount", None) if budget else None],
            "gates": [[g.code, g.op, g.value, bool(g.soft)] for g in gates],
        }),
        personRef=Ref(id=person_id, schemaVersion="person@1"),
        title=title,
        mode=mode,
        gates=gates,
        scorerMix=mix_for(mode),
        threshold=Threshold(show=float(show if show is not None else DEFAULT_THRESHOLD[0]),
                            alert=float(alert if alert is not None else DEFAULT_THRESHOLD[1])),
        status=SearchStatus.ACTIVE,
        budget=budget,
        area=area,
        # No clock here: createdAt/updatedAt are stamped by save(), which is the
        # one place in this module that reads the time.
        createdAt=answers.get("createdAt"),
        updatedAt=answers.get("updatedAt"),
    )
    search.nation = nation          # attached, not schema'd (the epcSqft idiom)
    search.isDefault = False
    return search


def default_search(mode: Any = None, listing: Any = None) -> Search:
    """The no-situate fallback, so a caller can always hand ``engine.score`` a
    Search rather than an error.

    No gates, no budget, no area — which is honest, not lax: an empty gate list
    scores 8.0 and excludes nothing, so "the user has not run situate" degrades
    to today's behaviour instead of an empty shortlist. Marked
    ``search.isDefault = True`` so every payload can say which search it used
    (Q1: a sticky wrong search that never says it is sticky is the failure mode).
    """
    if mode is None and listing is not None:
        mode = getattr(listing, "mode", None)
    resolved = _mode(mode)
    search = Search(
        id=_stable_id("search_", {"default": True, "mode": resolved.value}),
        personRef=None,
        title="no stated search (%s)" % resolved.value,
        mode=resolved,
        gates=[],
        scorerMix=mix_for(resolved),
        threshold=Threshold(show=DEFAULT_THRESHOLD[0], alert=DEFAULT_THRESHOLD[1]),
        status=SearchStatus.DRAFT,
        budget=None,
        area=None,
    )
    search.nation = None
    search.isDefault = True
    return search


# ---------------------------------------------------------------------------
# Persistence — atomic, versioned, user cache only.
# ---------------------------------------------------------------------------

def _now() -> str:
    """The one clock in this module."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: str, payload: Dict[str, Any]) -> str:
    """Write ``payload`` to ``path`` atomically: temp file in the same directory,
    then ``os.replace``. A concurrent reader sees the old file or the new one,
    never half of either. ``mkstemp`` creates the temp 0600 and ``replace``
    carries that mode across, so a session file is not world-readable.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s." % os.path.basename(path),
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def session_file() -> str:
    """The session file's path. Existence not implied."""
    return paths.state_path(SESSION_FILE)


def save(search: Any = None, person: Any = None, *, path: Optional[str] = None) -> str:
    """Persist the Search and Person as one versioned file; return its path.

    Serialised through :func:`gaff_engine.serialize.to_jsonable` and rebuilt by
    field on the way back. ``nation`` rides as its own key because it is an
    attached attribute, and ``to_jsonable`` walks declared dataclass fields only
    — writing it beside the Search is what makes the round trip honest.
    """
    from gaff_engine.serialize import to_jsonable
    stamp = _now()
    if search is not None and getattr(search, "createdAt", None) is None:
        search.createdAt = stamp
    if search is not None:
        search.updatedAt = stamp
    payload = {
        "schemaVersion": SESSION_SCHEMA,
        "writtenAt": stamp,
        "nation": getattr(search, "nation", None),
        "search": to_jsonable(search) if search is not None else None,
        "person": to_jsonable(person) if person is not None else None,
    }
    return _write_json(path or session_file(), payload)


def _read_session(path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(payload, note)``. A missing file is ``(None, None)`` — not an error.

    A file this release cannot read is ``(None, <plain note>)``: ignored, and the
    caller says so out loud. Crashing a user's next question because a later
    release wrote their session is the wrong failure.
    """
    target = path or session_file()
    try:
        with open(target, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, "the saved session could not be read (%s); ignoring it" % exc
    except json.JSONDecodeError:
        return None, ("the saved session file is not valid JSON; ignoring it "
                      "(delete %s to start clean)" % target)
    if not isinstance(payload, dict):
        return None, "the saved session file is not an object; ignoring it"
    version = payload.get("schemaVersion")
    if version != SESSION_SCHEMA:
        return None, ("the saved session was written as %r, and this release "
                      "reads %s; ignoring it" % (version, SESSION_SCHEMA))
    return payload, None


def load(path: Optional[str] = None) -> Tuple[Optional[Search], Optional[Person]]:
    """``(Search|None, Person|None)`` from the saved session. Never raises on a
    missing, unreadable or newer file — see :func:`search_in_use` for the note."""
    payload, _note = _read_session(path)
    if payload is None:
        return None, None
    search = _rebuild(Search, payload.get("search"))
    person = _rebuild(Person, payload.get("person"))
    if search is not None:
        search.nation = payload.get("nation")
        search.isDefault = False
    return search, person


def clear(path: Optional[str] = None) -> bool:
    """Delete the saved session. ``True`` if a file was removed."""
    try:
        os.unlink(path or session_file())
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Rebuilding a dataclass from its jsonable form — the inverse of to_jsonable.
# Driven by the declared type hints, so a new schema field needs no change here.
# ---------------------------------------------------------------------------

def _rebuild(cls: Any, data: Any) -> Any:
    """``dict`` -> an instance of ``cls``, recursively. ``None`` passes through.

    Keys the class does not declare are ignored (a file from a later release
    loses its extra fields rather than the whole session), and fields the file
    omits keep the dataclass default.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        return data
    try:
        hints = get_type_hints(cls)
    except Exception:                      # pragma: no cover - resolution fallback
        hints = {}
    kwargs = {}
    for f in fields(cls):
        # to_jsonable strips one trailing underscore (ComponentInput.from_ -> "from").
        key = f.name[:-1] if f.name.endswith("_") else f.name
        if key in data:
            kwargs[f.name] = _coerce(data[key], hints.get(f.name, Any))
    return cls(**kwargs)


def _coerce(value: Any, hint: Any) -> Any:
    """One jsonable value back into the type its field declares."""
    if value is None or hint is Any:
        return value
    origin, args = get_origin(hint), get_args(hint)
    if origin is Union:                                     # Optional[X] / Union
        arms = [a for a in args if a is not type(None)]
        return _coerce(value, arms[0]) if len(arms) == 1 else value
    if origin is list:
        elem = args[0] if args else Any
        return [_coerce(v, elem) for v in (value or [])]
    if origin is dict:
        return value                                        # Dict[str, Any], verbatim
    if isinstance(hint, type):
        if is_dataclass(hint):
            return _rebuild(hint, value)
        if issubclass(hint, Enum):
            try:
                return hint(value)
            except ValueError:
                return value                                # an open/extended list
    return value


# ---------------------------------------------------------------------------
# "Which one did you use?" — the two resolvers every payload quotes.
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    """A profile name -> a safe filename stem. Rejects anything that would not
    survive the round trip, so a name can never escape the profiles directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    if not slug or slug in (".", ".."):
        raise ValueError("%r is not a usable profile name" % name)
    return slug


def profile_path(name: Optional[str] = None) -> str:
    """Where a taste profile lives. **Q4, decided in S1.**

    ``name=None`` -> the legacy path, ``<user cache>/profile.json``, which
    ``paths.data_file`` already resolves ahead of the shipped demo. Unchanged on
    purpose: a user who has written one keeps it.

    A ``name`` -> ``<user cache>/state/profiles/<slug>.json``, user cache only,
    no shipped fallback. F-10 needs a second and third person; one slot would
    have the second overwrite the first.
    """
    if name is None:
        return os.path.join(paths.user_cache_dir(), "profile.json")
    return paths.state_path(PROFILES_DIR, "%s.json" % _slug(name))


def list_profiles() -> List[str]:
    """The named profiles on disk, sorted. The unnamed one is not in this list —
    it is not named, and :func:`profile_in_use` reports it."""
    directory = paths.state_path(PROFILES_DIR)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


def profile_in_use(weights: Any = None, name: Optional[str] = None) -> Dict[str, Any]:
    """Whose taste weights are about to run, and whether they were calibrated.

    F-08's acceptance criterion, and one function. The shadow it exists to break:
    a user writes a profile, deletes it, and silently falls back to the shipped
    demo's weights with no visible change in any output.
    """
    if weights:
        return {"source": "weights_argument", "path": None, "name": None,
                "subject": None, "calibrated": False,
                "note": "the eight weights were passed in this call; no profile was read"}
    if name is not None:
        path = profile_path(name)
        if not os.path.exists(path):
            return {"source": "missing", "path": path, "name": name, "subject": None,
                    "calibrated": False,
                    "note": "no profile named %r; known: %s"
                            % (name, ", ".join(list_profiles()) or "none")}
    else:
        from gaff_engine.tools import _demo_profile_path
        path = _demo_profile_path()
        if path is None:
            return {"source": "missing", "path": None, "name": None, "subject": None,
                    "calibrated": False,
                    "note": "no taste profile found: pass weights, or write one"}
    user_own = os.path.abspath(path) == os.path.abspath(profile_path(None))
    source = "user" if (name is not None or user_own) else "shipped_demo"
    subject, calibrated = None, False
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        subject = blob.get("subject")
        calibrated = bool(blob.get("calibration"))
    except (OSError, ValueError):
        pass
    note = ("your own profile" if source == "user"
            else "the shipped demo profile (the fictional 'Sam') — these are not "
                 "your weights until you write your own")
    return {"source": source, "path": path, "name": name, "subject": subject,
            "calibrated": calibrated, "note": note}


def search_in_use(search: Any = None, mode: Any = None,
                  listing: Any = None) -> Tuple[Search, Dict[str, Any]]:
    """Resolve the Search the way every composed tool must: the argument, else
    the saved session, else :func:`default_search`. Returns it with a note that
    says which, so a payload can never use a sticky search without saying so
    (Q1). A session this release cannot read is reported, not obeyed.
    """
    if search is not None:
        return search, {"source": "argument", "title": getattr(search, "title", None),
                        "note": "the search passed to this call"}
    saved, _person = load()
    payload, read_note = _read_session()
    if saved is not None:
        return saved, {"source": "session", "title": getattr(saved, "title", None),
                       "writtenAt": (payload or {}).get("writtenAt"),
                       "path": session_file(),
                       "note": "your saved search; run situate again to change it"}
    fallback = default_search(mode=mode, listing=listing)
    note = {"source": "default", "title": fallback.title,
            "note": "no saved search — no gates, no budget, no area; "
                    "run situate to state yours"}
    if read_note:
        note["note"] = "%s; %s" % (read_note, note["note"])
    return fallback, note


__all__ = [
    "SESSION_SCHEMA", "SESSION_FILE", "PROFILES_DIR", "NATIONS",
    "CONSTRAINT_GATES", "UnknownConstraint",
    "search_from_answers", "default_search", "gate_from_constraint",
    "normalise_nation", "mix_for",
    "save", "load", "clear", "session_file",
    "profile_path", "list_profiles", "profile_in_use", "search_in_use",
]
