# Lessons Learned — ShipGuard

This project was deployed and exercised on GenLayer Studio throughout
development, not written and left untested. Four real, load-bearing issues
were only caught this way — each is detailed below with what was wrong,
how it was found, and the fix.

---

## 1. SettlementVault had nothing to pay out with

**What was wrong.** Premium and coverage are paid into `PolicyRegistry`
(that's where `create_and_fund_policy` / `fund_as_underwriter` receive
`gl.message.value`). The original design assumed `SettlementVault.settle()`
could simply move that GEN out to the insured/underwriter — but the GEN
had never actually left `PolicyRegistry`. `SettlementVault` held nothing.

**How it was found.** Running the full contract lifecycle end-to-end
against the offline test stub (`tests/`), before ever touching Studio.
Asserting actual GEN ledger balances after each step — not just contract
state — surfaced it immediately.

**The fix.** `PolicyRegistry.mark_resolving` now forwards the whole
escrowed pot (premium + coverage) to `SettlementVault` before the policy
enters resolution, so the vault actually holds the GEN it needs by the
time `settle()` runs.

---

## 2. `@gl.public.write.payable` is required, not just `@gl.public.write`

**What was wrong.** A plain `@gl.public.write` method compiles and deploys
without any error. Nothing in the tooling warns that it can never receive
native value.

**How it was found.** Live, on Studio: `create_and_fund_policy` was
deployed with a plain `@gl.public.write` decorator, and its write-method
form in Studio's UI simply had no field to attach GEN to the call at all.
Switching the decorator to `@gl.public.write.payable` made the "Value
(GEN)" field appear.

**The fix.** `create_and_fund_policy` and `fund_as_underwriter` — the only
two methods that ever read `gl.message.value` — use
`@gl.public.write.payable`. Every other write method keeps the plain
`@gl.public.write`.

---

## 3. A nondet block and a cross-contract call should not share a transaction

**What was wrong.** `request_resolution` ran the evidence evaluation
(a nondet block: web fetch + LLM verdict extraction) and then, in the same
call, invoked the registry's `mark_resolving` — a cross-contract write.

**How it was found.** By comparing against a second GEN-moving sibling
project, ProofWorksEscrow, whose `evaluate_task` / `finalize_task` split
exists specifically to keep a nondet block and a cross-contract call in
separate transactions. Nothing failed outright in ShipGuard's case, but
mixing them was flagged as an unproven combination worth removing rather
than shipping on a hopeful guess.

**The fix.** `request_resolution` now only runs the nondet evaluation and
stores the resolution locally. `finalize_resolution` — which never runs a
nondet block — is what actually calls the registry's `mark_resolving`
(transitioning the policy to `RESOLVING` and forwarding the escrow). The
one exception: when `request_resolution` detects the final deadline has
already passed, it skips evaluation entirely, so calling the registry in
that same transaction is safe.

---

## 4. A FINALIZED transaction is not the same as a successful one

**What was wrong.** `index.html` reported "Done." for any `writeContract`
call whose promise resolved after `waitForTransactionReceipt`. Consensus
finalizing a transaction only means validators agreed on the outcome — not
that the outcome was success. A call that reverted with a `gl.vm.UserError`
still finalizes cleanly.

**How it was found.** Live, twice. A `request_resolution` call for a
policy that already had a resolution should have reverted — instead the
UI showed "Done." A first attempted fix guessed a field name
(`txExecutionResultName`) that turned out not to match this receipt shape,
and still showed "Done." on the same failing call. Pulling that exact
transaction's **raw JSON** from GenLayer's own block explorer settled it:
`consensus_data.leader_receipt[0].execution_result` is the literal string
`"ERROR"` on failure (`"SUCCESS"` on success) — the same object level
`extractReturnValue` already reads `.result.payload.readable` from.

**The fix.** `callWrite` now checks that exact field before reporting
success, with a whole-receipt substring scan (`"rollback"` / an
`"...execution...": "error"` pattern) kept as a second, independent
fallback in case the field is ever absent or renamed. Verified against the
real failing transaction's exact JSON shape before being committed.

---

## What this adds up to

Every mechanism this project relies on — receiving native value, reading
another contract's state, writing to another contract, paying out to both
an EOA and a contract, running a real nondet evidence pipeline, and
correctly telling success from failure — has been exercised against the
real network at least once, not just written to plausibly compile. See
`README.md` → "Findings from live Studio testing" for the condensed
version, and → "Known limitations / areas to verify" for what's still
worth double-checking (an unreachable tracking source's now-graceful
degradation, timing between `finalize_resolution` and `settle()`, GEN
decimal assumptions in the frontend, and the one-challenge-per-side bound).
