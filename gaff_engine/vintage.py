"""S5 — evidence vintage: how old the evidence under a verdict actually is.

The gap this closes (post-release assessment, finding F6). ``price_check``
returns ``fetchedAt``; ``value_check`` and ``score_listing`` returned nothing at
all. The shipped London comparables end at a sale on 29 May 2026 fetched on
16 July; Leamington ends 9 July 2026 fetched on 29 August. Land Registry
publishes monthly, so a verdict is structurally two months or so behind the
market and never said so. Staleness that is not printed looks like precision.

Four facts, assembled once where the comps are loaded and threaded to every
verdict:

* **newest sale** — the freshest transaction the pool actually contains. Not the
  fetch date: a fetch on Tuesday of data that ends in April is April evidence.
* **fetched at** — when that data was pulled from the upstream.
* **HPI month** — the month every comp was adjusted TO, with its region.
  ``hpi.AS_OF_MONTH`` is pinned so the golden stays deterministic, which means a
  live verdict is quietly adjusted to a stale month. Printing it is what makes
  that visible, and it is why M-2 (today's money) comes before M-1 (the
  backtest): a backtest against a pinned month measures the pin as much as the
  model. When the subject has no HPI region (``hpi.region_for`` abstains rather
  than falling back to London), no adjustment was made and the line says so
  instead of naming a series the verdict did not use — ``hpiAdjusted`` carries
  the same fact as a boolean.

  ``hpiAdjusted`` is per POOL, not per comp: it says at least one comp moved,
  which is what a reader needs to know the estimate is in as-of money. A comp
  whose own sale month is missing from the cache still keeps factor 1.0 inside
  an adjusted pool. M-2 (today's money) reworks ``AS_OF_MONTH`` and the warm
  cache in this same area and is where per-comp coverage would belong.
* **staleness** — one plain line, past a 90-day threshold.

Two ages, because they answer different questions and only one of them is
deterministic:

* ``publicationLagDays`` — newest sale to fetch. How far behind the register was
  when we pulled. A property of the data, reproducible, testable.
* ``evidenceAgeDays`` — newest sale to today. How old the evidence is *now*.
  Wall-clock by necessity: "stale" is not a fact about a file, it is a fact
  about the moment someone is about to make a decision on it.

``line`` is the single field a skill quotes, so the surface does not have to
assemble five numbers into a sentence and get the wording subtly different each
time. Everything else is there for anything that wants the parts.

Pure: no I/O of its own. The caller passes what it loaded.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Iterable, List, Optional

#: Past this, a verdict says so out loud. Land Registry publishes monthly and
#: runs one to two months behind, so 90 days is "older than the publication
#: cycle can explain" rather than an arbitrary round number.
STALE_DAYS = 90

_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _date(raw: Any) -> Optional[datetime.date]:
    """The leading ISO date in a string, or ``None``. Accepts a bare date and a
    full timestamp alike, which is what the two cache envelopes carry."""
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    m = _ISO_DATE.match(str(raw or "").strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _pretty(day: Optional[datetime.date]) -> str:
    """``2026-04-28`` -> ``28 Apr 2026``. The register's own register."""
    if day is None:
        return "unknown"
    return "%d %s %d" % (day.day, _MONTHS[day.month - 1], day.year)


def _pretty_month(month: Optional[str]) -> str:
    """``2025-06`` -> ``Jun 2025``."""
    text = str(month or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})$", text)
    if not m:
        return text or "unknown"
    return "%s %s" % (_MONTHS[int(m.group(2)) - 1], m.group(1))


def _comp_date(comp: Any) -> Optional[datetime.date]:
    """A comp's sale date, whichever name it travels under."""
    for field in ("date", "soldDate", "transactionDate", "saleDate"):
        raw = (comp.get(field) if isinstance(comp, dict) else getattr(comp, field, None))
        day = _date(raw)
        if day is not None:
            return day
    return None


def _today() -> datetime.date:
    """The wall clock, in one place, so a test can substitute it."""
    return datetime.date.today()


def evidence_vintage(comps: Optional[Iterable[Any]] = None, *,
                     fetched_at: Optional[str] = None,
                     sources: Optional[List[Dict[str, Any]]] = None,
                     hpi_month: Optional[str] = None,
                     hpi_region: Optional[str] = None,
                     hpi_adjusted: Optional[bool] = None,
                     today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """The vintage struct for one pool. See the module docstring.

    ``sources`` is an optional list of ``{"name", "fetchedAt", "kind"}`` — the
    cache files this pool came from. ``kind`` is ``"fetch"`` (pulled from the
    upstream then) or ``"derived"`` (computed from an earlier fetch, like the
    enriched comparables file, whose ``generatedAt`` says when the ENRICHMENT
    ran and nothing about when the sales were pulled).

    ``fetchedAt`` is the **oldest** fetch among the sources, not the newest. A
    pool assembled from two caches is no fresher than its stalest ingredient,
    and taking the newest would have reported the shipped London pool as pulled
    on 29 August when its sales were actually pulled in mid-July — six weeks of
    invented freshness on exactly the number that exists to prevent it.

    ``hpi_adjusted`` says whether the time adjustment actually moved anything.
    The caller passes it because only the verdict knows: a region can resolve
    perfectly well and still move no comp, when its HPI months are neither
    cached nor fetchable. Left ``None`` it is inferred from the region, which is
    right for a caller that has no verdict in hand.

    Anything absent is reported as absent and never guessed: a pool with no
    dated comps says its newest sale is unknown rather than implying today.
    """
    comps = list(comps or [])
    sources = [s for s in (sources or []) if s and s.get("fetchedAt")]
    today = today or _today()

    days = sorted(d for d in (_comp_date(c) for c in comps) if d is not None)
    newest, oldest = (days[-1], days[0]) if days else (None, None)

    fetches = sorted(d for d in
                     ([_date(fetched_at)] +
                      [_date(s.get("fetchedAt")) for s in sources
                       if (s.get("kind") or "fetch") == "fetch"])
                     if d is not None)
    fetched = fetches[0] if fetches else None       # the stalest ingredient
    derived = sorted(d for d in (_date(s.get("fetchedAt")) for s in sources
                                 if s.get("kind") == "derived") if d is not None)

    adjusted = (bool(hpi_month and hpi_region) if hpi_adjusted is None
                else bool(hpi_adjusted))

    lag = (fetched - newest).days if (fetched and newest) else None
    age = (today - newest).days if newest else None
    stale = bool(age is not None and age > STALE_DAYS)

    return {
        "newestSale": newest.isoformat() if newest else None,
        "oldestSale": oldest.isoformat() if oldest else None,
        "datedComps": len(days),
        "comps": len(comps),
        "fetchedAt": fetched.isoformat() if fetched else None,
        "derivedAt": derived[-1].isoformat() if derived else None,
        "sources": sources,
        "hpiMonth": hpi_month,
        "hpiRegion": hpi_region,
        # False when no comp actually moved — either the subject could not be
        # placed in an HPI region, or its region's months were not available.
        # Either way the comps stand in the money of their own sale dates, and
        # hpiMonth names only the month the engine WOULD have adjusted to.
        "hpiAdjusted": adjusted,
        "publicationLagDays": lag,
        "evidenceAgeDays": age,
        "staleAfterDays": STALE_DAYS,
        "stale": stale,
        "line": _line(newest, fetched, age, lag, stale, hpi_month, hpi_region,
                      adjusted, len(days), len(comps)),
    }


def _line(newest, fetched, age, lag, stale, hpi_month, hpi_region, adjusted,
          dated, total) -> str:
    """The one sentence a surface quotes. Plain, and honest about absence."""
    if newest is None:
        if not total:
            return "No comparable sales under this verdict, so there is no vintage to report."
        return ("None of the %d comparable sales carries a date, so the age of "
                "this evidence is unknown — treat the verdict as undated." % total)
    parts = ["The freshest sale under this verdict is %s" % _pretty(newest)]
    if age is not None:
        parts.append("%d days old" % age)
    line = ", ".join(parts) + "."
    if fetched is not None:
        line += (" Fetched %s%s." %
                 (_pretty(fetched),
                  (", %d days after that sale" % lag) if lag is not None and lag >= 0 else ""))
    if hpi_month and adjusted and hpi_region:
        line += " Prices adjusted to %s money (%s)." % (_pretty_month(hpi_month), hpi_region)
    elif hpi_month:
        # Nothing moved, so the line must not imply that anything did. Two
        # different reasons, and a reader deserves the right one: hpi.region_for
        # abstains rather than defaulting to the London series, and a region it
        # DID place can still have no months to hand.
        why = ("no UK HPI data was available for %s" % hpi_region if hpi_region
               else "this subject's area is not in the UK HPI region map")
        line += (" Prices are NOT adjusted to %s money: %s, so each comparable "
                 "sale stands in the money of its own date."
                 % (_pretty_month(hpi_month), why))
    if stale:
        line += (" That is over %d days: HM Land Registry publishes monthly and "
                 "runs a month or two behind, so this pool is older than the "
                 "publication cycle explains. Warm the subject's street for "
                 "fresher sales." % STALE_DAYS)
    if dated < total:
        line += " %d of %d comparables carry no date." % (total - dated, total)
    return line


def render(vintage: Optional[Dict[str, Any]]) -> List[str]:
    """The narrated block for ``workings.render_text``, as lines."""
    if not vintage:
        return []
    out = ["", "Evidence vintage"]
    out.append("  %s" % vintage.get("line"))
    if vintage.get("newestSale"):
        out.append("  newest sale %s; oldest %s; %d of %d dated."
                   % (vintage["newestSale"], vintage.get("oldestSale") or "unknown",
                      vintage.get("datedComps", 0), vintage.get("comps", 0)))
    for source in vintage.get("sources") or []:
        out.append("  %s %s %s" % (source.get("name") or "source",
                                   "derived" if source.get("kind") == "derived"
                                   else "fetched", source.get("fetchedAt")))
    return out


__all__ = ["STALE_DAYS", "evidence_vintage", "render"]
