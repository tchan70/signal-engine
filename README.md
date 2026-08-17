# Signal Engine

An LLM-powered pipeline that turns unstructured trade-alert messages into
risk-checked, managed options positions. Built and hardened against live
market conditions on a small account; published here as the broker- and
source-agnostic core. See [PORTING_NOTES.md](PORTING_NOTES.md).

```
Messages (JSONL / any source) ──→ AI Parser ──→ Decision Engine ──→ Executor
                                  (Claude +      │ conviction score    (paper |
                                   chart vision) │ cost-aware sizing    official
                                                 │ PDT gate             MCP)
                                                 ▼                        │
                          Notifications ◄── Trade Manager ◄── Position Monitor
                                            proportional trims
                                            profit tiers + trailing stops
                                            runner policy
```

## What's interesting in here

- **LLM signal parsing with an eval harness** (`parser/`, `eval/`) - Claude
  parses free-text alerts (and charts/screenshots via vision) into a strict
  schema; `eval/parser_eval.py` scores parser changes against a labelled
  set before they ship. `eval/sample_signals.jsonl` shows the schema.
- **Regulatory awareness** - a PDT (pattern day trader) gate models the
  FINRA day-trade constraints of a sub-$25k account and blocks entries that
  would burn a day trade the exit needs.
- **Spread-aware exit pricing** (`execution/price_math.py`) - liquidity-
  tiered sell pricing with a hard-won no-bid branch; the incident that
  shaped it is written up in
  [docs/postmortems/no-bid-sell-pricing.md](docs/postmortems/no-bid-sell-pricing.md).
- **LLM guardrails in anger** -
  [docs/postmortems/llm-expiry-year-invention.md](docs/postmortems/llm-expiry-year-invention.md)
  covers the parser inventing an expiry year and the healing layer that
  now catches it.
- **303 tests, all passing** - most written as regression suites against
  live incidents (the `test_session*` / `test_bug*` names are the audit
  trail of a system debugged in production).

## Quickstart

```bash
pip install -r requirements.txt
python run_paper.py                      # offline demo, bundled sample signals
ANTHROPIC_API_KEY=sk-... python run_paper.py --llm   # live Claude parsing
python -m pytest tests/ -q               # 388 passed
```

## Execution backends

`execution/base.py` defines the contract; backends are a config switch:

| backend | status |
|---|---|
| `paper`  | default - in-memory fills, seedable quotes |
| `mcp`    | skeleton adapter for the official Robinhood Agentic Trading API (equities in beta; options pending upstream) |

Live credentials, when a live backend exists, come from environment
variables only - never the repo.

## Architecture deep-dive

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers every subsystem:
ingestion, parsing, decisioning, sizing, PDT protection, execution, trade
management, trimming, runner policy, context stores, and the logging
system.
