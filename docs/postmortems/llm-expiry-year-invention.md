# BUG-41 — Parser invents the expiry YEAR for a bare M/D (missed DJT entry)

**Date:** 2026-08-05 (Live Day 6) · **Severity:** missed entry (fail-closed, no bad order) · **Status:** FIXED, 3 review rounds, deployed pending rebuild

## What happened

14:40 BST — caller_a posted:

> `DJT $16 Call 11/20 • 1 Buy 0.85 BUY LIMIT`

The parse prompt's schema demanded `"expiry": "ISO date string or '0DTE' or '1DTE' or null"`, so the model had to invent a year for "11/20". Despite `Today's date: 2026-08-05` being in the prompt, its training-year prior won: it emitted **`2025-11-20`**. Validation then (correctly, by design) refused to let a past date become an order → expiry dropped → `⏭️ SKIP: DJT - unresolvable expiry: None [p05]`. Real entry missed. the operator `!flag`ged it at 15:10.

The irony: `market_time.normalize_expiry` already resolves a bare `"11/20"` perfectly (assume this year, roll forward if past). The prompt just never let the model say it that way.

## The fix (two layers)

**1. Prompt (schema line):** expiry is now emitted **exactly as written** — `'M/D'` like `'11/20'`, `'M/D/YY'` only if the caller wrote the year, `'0DTE'`/`'1DTE'`, or null. "NEVER add or invent a year the caller did not write. The engine resolves the year against today's date; you must not." Validation keeps the raw form (as it always did for `0DTE`/`3/21`); the engine normalizes at decision time (Session 9 H8) — unchanged.

**2. Validation backstop (`_validate_and_coerce`):** if the model still emits a past ISO expiry, heal it **only** in the provably-invented-year case. All guards fail CLOSED (to the drop):

- emitted expiry is ISO-shaped (`_ISO_EXPIRY_RE`), AND
- the caller literally wrote that M/D — **token-bounded** regex `(?<![\d/.])0?M/0?D(?![\d/])` — in their **own text** (`utils/message_text.own_text`: `[RECENT CONTEXT]`/`[REPLYING TO]`/`[PING:]`/`[caller:]` stripped), AND
- the emitted year does not appear as a digit-bounded 4-digit token in the own text.

The healed date comes from `normalize_expiry` on the **caller's** M/D — the model's year never survives. Heals are noted (`expiry year corrected: … (caller wrote M/D without year …)`), never silent.

## What the adversarial review rounds caught (pattern now 5-for-5)

**Round 1 fix defects (all confirmed with executed exploits, all fixed):**

- **F1 substring bless:** `"2/20" in "12/20"` — a hallucinated `2025-02-20` healed to `2027-02-20` off a message whose only date was 12/20. → token-bounded regex.
- **F2 day/YY collision:** the `/YY` year-guard matched the caller's own DAY — `"/25"` inside `"11/25"` blocked the heal for the **most common invented year (2025) on every 25th-of-month expiry**, i.e. the original bug recurring. Also `"$20250"` contains `"2025"`. → `/YY` check deleted (caller-written years now fail md_written via the trailing-slash boundary); 4-digit year check is digit-bounded.
- **F3 context contamination:** the heal matched M/Ds inside injected `[RECENT CONTEXT]` blocks — a previous message's trade could bless the current signal's heal. → own-text scrub (regexes moved from main.py to shared `utils/message_text.py`; main delegates).
- **Fingerprint dedup break (money-path reviewer):** caller signals now carry `"8/21"` while embed alerts carry `"2026-08-21"` — the Session 11 cross-source dedup would never match again (latent until `embed_alerts.execute: true`). → `_signal_fingerprint` normalizes expiry, falling back to the raw string (never collapse distinct trades onto `"?"`).

**Round 3 (review of round 2):** decimal-quote collision — `"bid/ask 2.10/2.30"` contains a token-bounded `"10/2"` (the `1` hides behind the `.`). → `.` added to the lookbehind ONLY (lookahead unchanged, so "expiring 11/20." still heals).

## Tests

`tests/test_expiry_year_heal.py` — 27 tests, calendar pinned to 2026-08-05. Full suite: **1141** in container (was 1114). Mutations: 12 run, 11 killed; 1 survivor (`and raw_message` truthiness) confirmed masked by the sibling `md_written` guard — redundant defense-in-depth, kept deliberately.

## Files changed

- `parser/signal_parser.py` — prompt schema line, `_ISO_EXPIRY_RE`, heal in `_validate_and_coerce` (+ raw_message param, call site)
- `main.py` — `_own_text`/`_CONTEXT_MARKER_RES` delegate to `utils/message_text`; `_signal_fingerprint` normalizes expiry
- `utils/message_text.py` — NEW shared own-text scrub
- `tests/test_expiry_year_heal.py` — NEW

## Follow-ups deliberately NOT done tonight (the operator to decide)

- **No max-DTE gate on typed-channel entries** (embeds have `expiry-too-far`; typed entries don't). A bare past M/D that rolls +1 year at decision time can, if the LLM ever mistypes a recap as an entry, order a listed LEAP date. Pre-existing exposure; the prompt change makes bare M/D the majority form. Mirror the embed gate if wanted.
- **Image path:** `parse_image_signal` passes only the caption as raw_message, so the heal can't fire for screenshot-only expiries (the prompt fix still applies there). Fail-closed.
- **Prompt changed ⇒ run `run_eval.bat`** before trusting it (API credits — the operator runs). Add eval cases: DJT `[p05]`, RKLX `[p07]`, "Lets TP this XSP 0.60 40%", "0.44 20% out QCOM", "SIZED TO 0".
