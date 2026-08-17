# Porting notes - public extraction

This repository is the broker- and source-agnostic core of a private
signal-following trading agent that ran live on a small options account.
The extraction keeps every part of the system that constitutes the actual
engineering - parsing, evals, decisioning, position management, price math,
backtesting - and replaces the integration edges:

**Replaced edges**
- Chat-platform ingestion -> `ingest/jsonl_source.py` (generic message
  iterator; any real-time source implements the same contract)
- Brokerage execution -> `execution/paper.py` (full BaseExecutor contract,
  simulated fills). `execution/mcp_executor.py` is the skeleton adapter for
  the official Robinhood Agentic Trading API (equities in beta; options
  support pending on Robinhood's side).

**Excluded from the public tree**
- The private chat/social ingestion adapters and their tests (platform-ToS
  and third-party-content constraints)
- The original orchestrator (`main.py`) and ~90 tests that exercised it or
  brokerage-implementation internals
- Live datasets: real captured messages, trade ledgers, eval corpora
  (replaced with `eval/sample_signals.jsonl`, synthetic but schema-true)

**What survived unchanged**
- Parser, decision engine, trade constructor, trade manager, risk, price
  math, eval harness, backtester
- 303 test functions (388 including parametrisation) - all passing
