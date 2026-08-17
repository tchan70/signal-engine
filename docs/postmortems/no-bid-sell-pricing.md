# BUG-38 — the 0DTE sweep could never fill

**Found live 2026-07-31 on CELH. Fixed 2026-08-01. NOT YET DEPLOYED — `start.bat rebuild`.**
**Gates: 971 tests (was 894 after Session 17), SIX adversarial review rounds, 23 verified mutations.**
Ships together with Session 17 (`SESSION_17_2026-07-31_LOGIN_HANG_AND_ERROR_FLOOD.md`) — one rebuild covers both.

---

## The bug

CELH $30 call, 0DTE, 15:45 ET, one contract, **no bid** and roughly a $0.02 ask. The sweep did everything right — fired on time, placed an order, warned the operator — and the order could not fill, because of one line:

```python
if bid <= 0:
    return self._round_to_tick(max(0.05, ask * 0.85)) if ask > 0 else 0.05
```

That `$0.05` floor is the one **BUG-10** was raised to remove. The fix had been applied at the *order* site — `price=max(0.01, sell_price)`, whose comment reads *"the old $0.05 floor sat ABOVE the ask on dying penny contracts and could never fill"* — **but not here**, and a floor downstream cannot lower a price that has already arrived too high. So the bot offered **$0.05 against a $0.02 ask**: above the market, on a contract with fifteen minutes to live.

Three things kept it hidden for four live days:

- **This branch was the only path in the function that logged nothing.** The incident produced no pricing line at all, so it read as a quiet market rather than a mispriced order.
- **Every pre-existing test passed `bid=1.00`.** The no-bid case — precisely the population the 0DTE sweep exists for — had no coverage whatsoever.
- **The attempt log printed the intended timeout, not elapsed.** `"0/1 filled within 771s"` for an order that lived seven seconds. It did not merely fail to reveal the bug; it argued against there being one.

## What changed

**Pricing.** The no-bid branch prices off the ask (`ask × 0.85` urgent, `× 0.95` otherwise), floored at one tick, and can never exceed the ask. With **no bid AND no ask** it refuses to price at all and returns `None` — a $0.05 offer into an empty book is a standing offer to sell a possibly-valuable contract for $5 if those zeros are a quote glitch, and Session 13 exists because this feed glitches. `_floored_tick` now guards **every** return in the function.

**The rest window survives a transient empty book.** Refusing to place a blind order is right; abandoning the 13-minute window on one bad poll is not. The resting attempt now re-quotes, reserving enough of the window that any order it does place has time to work.

**The cancel verdict is settled, not snatched.** `cancel_order` polled once, half a second after the request. Robinhood's cancels are asynchronous, so "queued" at +0.5s is the *ordinary* reading — and `_cancel_and_inspect` discarded the `cancelled` flag entirely, so attempt 2 submitted a full-size close over an order that might still be working. **That is the best mechanical explanation for the 6-second termination on 2026-07-31.** It now polls until the state is terminal (bounded, monotonic) and keeps the last good read.

**Diagnostics.** The attempt line reports time actually spent. A broker-terminated order names the state that ended the wait, and offers the possible causes rather than asserting one (the operator is invited to cancel by hand in that same window). The no-bid branch is no longer silent.

**Plus, found along the way:** the caller-limit sell branch had no one-tick floor; `get_option_price` returned half of a one-sided book as a "mid" and fed it to the stop and trail; `sell_option_position` never guarded a failed expiry parse and would submit `expirationDate: None`; a zero ask discarded the caller's buy limit; six quote coercions crashed on a JSON null.

## What the review rounds cost, and bought

Six rounds. Every one of the first five found a real defect, and **three of them were defects the previous round's fix introduced.**

| Round | Found |
|---|---|
| 1 | Both reviewers, independently: the refusal sentinel `0.0` could not be told from `bid × 0.97` **underflowing** to `0.00` on a $0.01 bid — so the fix refused to sell *any* penny-bid contract. A fail-to-exit on the exact population it was written for. |
| 2 | (fixes) |
| 3 | The unconfirmed cancel above. Plus: the fix reserved no window, and the re-quote loop retried failures that had nothing to do with the book. |
| 4 | The resting attempt spent its window **twice** — re-quoting to 15:59 then confirming to 16:12, putting the cancel twelve minutes past the close, which is exactly what the margin exists to prevent and exactly a defect a prior session had already fixed once. And the cancel verdict rested on one 500 ms poll, which would have latched `sell_state_unknown` on routine unfilled exits — blocking the expiry booking that keeps a loss out of the daily P&L. |
| 5 | The settle loop could **discard a fill it had already seen**. And "no price" skipped the PDT next-day sell and the deferred caller exit — instructions already given, which never needed a price. |
| 6 | **Clean.** Nothing above LOW. |

## Lessons worth keeping

- **A fix and a floor in different places is not a fix.** BUG-10's floor lived at the order site; the bug lived one function upstream. Fix a value where it is *computed*, or the guard downstream is decoration.
- **A numeric sentinel is indistinguishable from a rounding artefact.** `0.0` meaning "no market" and `0.0` meaning "underflowed" cost a fail-to-exit. Use a sentinel that is not of the same type as the data.
- **The mirror risk is the whole design.** This bug was "cannot sell". Every fix for it had to be checked against "sells too cheaply", and that framing is what produced the refuse-to-price branch instead of a naive penny floor.
- **A log that reports intent instead of measurement is worse than no log.** "filled within 771s" for a 7-second order actively misdirected the diagnosis.
- **When a fix trades one hazard for another, check whether something else in the same round already removed the original hazard.** Round 2 kept two login flags disagreeing to avoid a rival login; single-flight login — added in the same round — had already made that impossible.
- **Verify the mechanism, don't infer it.** My first diagnosis of this bug was confidently wrong (I had the arithmetic right and the code path wrong). Grepping the log for the pricing line that *wasn't there* is what found the real branch.
- **Mutation-test the harness too.** A mutation that breaks syntax produces collection errors that look like "caught". A shutdown test with a 0.6-second window passed with every shutdown check deleted.
- **Date-pinned tests rot.** Three of these hard-coded the incident's own expiry and started failing the next day, because `normalize_expiry` refuses past dates — the third time this codebase has hit that trap. They now use a far-future date; the incident's date is narrative, not fixture.

## Deploy

1. Not during market hours with a 0DTE position open.
2. `run_tests.bat` on Windows — expect **971**.
3. `start.bat rebuild`.
4. This ships Session 17 too. Startup signature: `♻️ Bot restarted — connecting to Robinhood…` then `Agent started`, plus `robin_stocks session: 401/403 detector installed`.

## Deliberate behaviour changes to watch on the first live day

- A contract with **one side of the book missing now reports no price**, rather than half of the visible side. Dying contracts will show as unpriceable and raise the existing "not being price-managed" alert at 3/100/200/300 checks. This is honest — there is no mark — but it is more alerts than before on exactly the contracts the sweep is about to handle. Caller exits, the PDT next-day sell and the 0DTE sweep all still work without a price.
- A cancel now takes up to ~3s longer to confirm, on the unfilled path only.
- **Still unproven live:** the re-quote loop, the settle loop, and the whole no-bid pricing path. Next 0DTE is the first real exercise.

## Not done

- `c_qty <= filled_qty` in the escalation guard is unconditionally true (pre-existing, dead, and contradicted by its own comment).
- The re-quote loop reserves 30s for placement but does not account for a re-authentication inside `sell_option_position`; if placement outruns the reserve, the confirm collapses to 1s. Bounded, but residual.
- Three of the round-5 tests are source-shape assertions rather than behavioural. They fail on revert, but would pass over a textually-present, semantically-broken implementation.
