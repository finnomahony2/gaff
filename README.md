# Gaff

## Quickstart

```bash
git clone https://github.com/finnomahony2/gaff && cd gaff
pip install .                                # not on PyPI yet; installs from the checkout
gaff doctor                                  # secret-free self-check of the install
gaff demo                                    # the seeded street end to end, offline
gaff price_check street="De Beauvoir Road"   # sold comparables + median for a street
gaff read_listing 'text=<paste a listing here>'
gaff value_check 'fields={"address": "De Beauvoir Road, London N1", "beds": 2, "sqft": 1050, "price": 1150000}'
```

A property-taste engine. You tell it what you actually like about a home; it
scores listings against that, and separately prices them against open sold-price
data so you can see whether a home is a steal, fair, or an over.

It runs as an **MCP server** (so Claude and other MCP hosts can call it as
tools) and as a **Claude Skill** (for the slower jobs where you want to watch
progress). Both surfaces call the same engine.

The engine is stdlib-only and has no runtime dependencies.

## What it does

- **Taste scoring.** Eight weighted axes (light and volume, outdoor space,
  character and bones, width and flow, street scene, raw size, design finish,
  station proximity) combined into one score, plus named-love and anti-signal
  adjustments. The weights are yours, not ours.
- **Value verdict.** Land Registry sold comparables for the street, filtered
  like-for-like and adjusted to today with the UK House Price Index, then EPC
  floor areas to get a defensible £/sqft. The verdict says how far the asking
  price sits from the trusted local median.
- **Rules and gates.** Hard constraints (beds, baths, sqft, outdoor, lease
  years) as gates, with soft docks and flags for things worth knowing rather
  than things worth excluding.

Every score recomputes: the breakdown you get back is the arithmetic that
produced the number, not a summary of it.

## What it does not do

**It does not scrape property portals, and it ships no portal content.** There
is no crawler here. Getting listing data in is your side of the line, and your
business with whoever you get it from. The engine takes a listing object and
scores it.

## Where your data goes

This package makes no model calls and retains nothing. Precisely:

- **Local against cached data:** the deterministic tools — `price_check`,
  `flip_stats`, `read_listing`, `value_check`, `coverage` — score on your
  machine against cached open data, and your pasted listing text never leaves
  it. One caveat, stated precisely: when `value_check` needs a UK HPI month
  that is not yet cached, it fetches that one month live from
  landregistry.data.gov.uk. That request carries the HPI region slug derived
  from the listing's address (a borough/county name) and the month — never the
  listing text, the price or your profile. Warm the cache first and these
  tools make no network calls at all.
- **Your own model session:** `taste_score` is scored by the HOST model — the
  Claude (or other LLM) session you are driving Gaff from. A listing you ask
  it to taste-score is read by that model under your own account and your
  provider's retention terms. Gaff has no server, adds no calls of its own,
  and keeps nothing.
- **Open-data fetches:** `warm` (and any live EPC lookup under your token)
  sends the street or town you name to open government data APIs — never your
  listings, scores or profile. Together with the HPI cache-miss fetch above,
  that is the complete list of network activity in this package.

There is no telemetry, no analytics and no phoning home anywhere in this
package.

## Install

**Plainly: this package is not on PyPI yet.** Install from a checkout of
this repo (or a wheel someone built from it):

```bash
git clone https://github.com/finnomahony2/gaff && cd gaff
pip install -e .
python3 tests/test_u1_golden.py     # the golden verdict, offline
```

Installing puts two commands on your PATH: `gaff` (the CLI) and `gaff-mcp`
(the MCP server). Without installing, `python3 -m gaff_engine.mcp` and
`python3 surfaces/skill/gaff` do the same jobs from the checkout.

## The two surfaces

### MCP server

With the package installed:

```json
{
  "mcpServers": {
    "gaff": { "command": "gaff-mcp" }
  }
}
```

Or, pointing at a checkout without installing:

```json
{
  "mcpServers": {
    "gaff": {
      "command": "python3",
      "args": ["/absolute/path/to/gaff/surfaces/mcp_server.py"]
    }
  }
}
```

Tools:

- `price_check` — sold comparables and the trusted median for a street.
- `flip_stats` — repeat-sales uplift against the market move, so you see the
  excess over HPI rather than the raw gain.
- `read_listing` — parse a listing (your structured `fields` read, or raw
  pasted `text`) into the engine's honest Listing shape, with a completeness
  map of what was missing.
- `value_check` — ingest a listing, load cached comps, and return the value
  verdict: steal/fair/over tag, percent delta, fair estimate, confidence and
  its evidence basis. Offline.
- `taste_score` — the host model reads the listing and scores the eight taste
  axes; the engine runs its deterministic weighting and adjustment pipeline
  over that read. Weights are yours, or the shipped demo profile's.
- `score_listing` — the one-call verdict: paste a listing, get the fit story,
  evidence-based price context (the steal/fair/over tag conditional on the
  evidence band, NEEDS_DATA when the evidence isn't there), agent questions
  generated from the engine's own uncertainty, the full working trace, and a
  short narrative templated from the numbers. Taste is scored only when you
  supply axis reads (taste_score's contract), and skipped honestly otherwise.
- `show_work` — every number traceable on demand: address-match level (street
  / area / unverified pool — "area" is only claimed when the comp pool
  verifiably reaches the subject's outcode or town), where the floor area came
  from (pass `epc_sqft` in `fields` to enable the marketing-vs-EPC basis
  check), comp counts by trust tier with exclusions shown, the value-band
  arithmetic, taste rows with the recompute sum — structured, plus a narrated
  plain-text form. Presentation only; nothing new computed.
- `rent_check` — is the asking rent fair? Judged against comparable local lets
  from a local pool file, at honestly lower confidence than a sales verdict
  (asking rents, not agreed lets). No pool ships with the package — rental
  comparison data is not redistributable — so without your own
  `rental_candidates.json` in the user cache this returns the honest error
  saying so.
- `coverage` — what the local caches can answer right now: warmed comps towns,
  flips towns, data vintages. Scope a question before asking it.
- `warm` — fetch and cache open Land Registry data (one street's sales, or a
  town's repeat-sales set). The only tool that touches the network.

The server speaks JSON-RPC 2.0 over stdio. Tool failures come back as results
with `isError: true`, not as transport errors, so the host can show you what
went wrong instead of dropping the session.

### Claude Skill

Copy `surfaces/skill/` into your skills directory. The folder is
self-contained given a `pip install` of this package: its `gaff` script
imports `gaff_engine.tools` by name, streams progress to stderr and returns
JSON on stdout. It also adds two CLI-only verbs: `gaff demo` (the seeded
street end to end, offline, narrated) and `gaff doctor` (the diagnostic
bundle). Use this surface for the long jobs; use MCP for the quick ones.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `GAFF_CACHE_DIR` | Where fetched data is written | `~/.gaff/cache` |
| `GAFF_DATA_DIR` | Override the shipped warm cache | packaged `gaff_engine/data` |
| `GAFF_EPC_TOKEN` | EPC API token | see below |
| `GAFF_USER_AGENT` | Contact string sent upstream | a generic project string |

**Set `GAFF_USER_AGENT` to something that identifies you.** You are calling
public APIs; they are entitled to know who is calling.

### Cache

Reads check your cache first, then the warm cache that ships with the package,
so a fresh install already answers for the seeded streets. Writes only ever go
to your cache, so an install never modifies itself.

### EPC token

The package ships no EPC certificate data — the register's address fields are
licensed for energy-performance purposes only, so redistribution is not ours
to do (the value verdict for the seeded streets still works offline, because
the £/sqft there is precomputed). Any EPC lookup is a live fetch under your
own account: request a token at <https://epc.opendatacommunities.org/>, then
either:

```bash
export GAFF_EPC_TOKEN='YOUR_TOKEN'
# or, on macOS, keep it in the keychain instead of an env var:
security add-generic-password -s gaff-epc-token -a "$USER" -w 'YOUR_TOKEN'
```

Resolution order is `GAFF_EPC_TOKEN`, keychain, `~/.gaff/epc_token`. The token
is never logged or written to disk by this package.

## Tests

```bash
for f in tests/test_*.py; do python3 "$f" || break; done
python3 surfaces/mcp_client_test.py
python3 surfaces/cli_test.py
```

Everything is offline and deterministic. The model boundary is a recorded
replay, so runs are byte-stable and no test touches the network.

## Troubleshooting

```bash
python3 -m gaff_engine.doctor
```

prints a paste-able diagnostic bundle: versions, where both cache tiers
resolve, which token source is configured (never the value), and offline
self-checks over the real read paths. It contains no secrets — paste the whole
block when reporting a problem.

## Data licence

The code is MIT. The data terms differ per dataset — Land Registry data is
OGL with a specific Royal Mail / Ordnance Survey permission covering its
address fields, and EPC data is deliberately not redistributed at all. Read
[NOTICE.md](NOTICE.md) before you redistribute this package or publish
anything derived from its output.
