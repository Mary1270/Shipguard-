# ShipGuard

**Parametric shipping-delay insurance**, built for [GenLayer](https://genlayer.com).

ShipGuard closes a gap the sibling project MatchGuard explicitly left out of
scope ("this contract produces an authoritative settlement decision only;
actual fund movement is left to a separate escrow/payout layer"): it's a
parametric insurance system that actually custodies GEN, in per-policy
escrow, and executes payout itself once a shipment's delivery outcome is
settled. It reuses MatchGuard's proven shape - canonical event identity,
locked multi-source evidence, two-phase optimistic resolution with a bounded
challenge window, and an immutable versioned resolution history - and adds
the one piece that shape never needed before: a contract that actually holds
and moves money.

ShipGuard is a clean-room design for a different problem (custody + payout,
not just a settlement decision), but it deliberately reuses MatchGuard's
reviewed, working conventions rather than inventing new ones where a proven
pattern already exists - see "Patterns carried forward" below, and each
contract's module docstring for the specifics.

## Repository layout

```
contracts/
  policy_registry.py    # policy terms + per-policy GEN escrow
  delivery_oracle.py     # canonical identity, locked evidence, resolution
  settlement_vault.py    # reads the final decision, executes payout
tests/
  genlayer_stub/          # offline stub of the genlayer SDK (test-only)
  _bootstrap.py           # shared fixtures: deploy + wire the 3 contracts
  test_policy_registry.py
  test_delivery_oracle.py
  test_settlement_vault.py
index.html                # single-file reference frontend (genlayer-js, no build)
.github/workflows/test.yml
LICENSE
```

## Contracts

1. **`policy_registry.py`** - Source of truth for policy terms, and where
   GEN actually lands. Per-policy escrow, not a shared pool: an "insured"
   address funds a premium, a separate "underwriter" address locks matching
   coverage. Fully collateralized at all times.
2. **`delivery_oracle.py`** - Canonical event identity is a policy's
   shipment (carrier + tracking sources), read from the linked registry.
   Evidence is locked only after `evidence_lock_time` (declared ETA +
   buffer), requires at least two independent tracking sources to agree,
   and resolves in two phases: an optimistic decision, then a 48-hour
   evidence-based challenge window. Each side (insured/underwriter) gets at
   most one challenge, bounding resolution to a small number of rounds.
   Resolution history is versioned and immutable.
3. **`settlement_vault.py`** - Reads the oracle's final decision only, never
   re-derives it, and executes exactly one of three payout paths (see
   below). Settlement is idempotent per policy.

## Payout rules

| Decision      | Insured gets              | Underwriter gets                    |
|---------------|----------------------------|--------------------------------------|
| DELAYED       | `coverage_amount`           | `premium_amount`                     |
| ON_TIME       | nothing                     | `coverage_amount + premium_amount`   |
| INCONCLUSIVE  | `premium_amount` (refund)   | `coverage_amount` (collateral back)  |

INCONCLUSIVE covers both "sources never agreed" and "the final deadline
(declared ETA + 30 days) passed without ever reaching a clean resolution" -
no GEN is ever left stuck, and no party is forced into a guessed outcome.

## How settlement actually moves GEN

Premium and coverage are both paid into `PolicyRegistry` (that's where
`create_and_fund_policy` / `fund_as_underwriter` receive `gl.message.value`).
`DeliveryOracle.request_resolution` runs the nondet evidence evaluation and
stores a resolution, but - following a proven sibling pattern
(ProofWorksEscrow's `evaluate_task` / `finalize_task` split, which never
mixes a nondet block and a cross-contract call in one transaction) -
`finalize_resolution` is what actually calls the registry's
`mark_resolving`, transitioning the policy to `RESOLVING` and forwarding
the whole escrowed pot (premium + coverage) to `SettlementVault` in that
same, nondet-free call. By the time `settle()` runs, the vault already
holds the GEN it needs. The escrow-forwarding step was found and fixed by
actually running the contracts end-to-end against the offline test stub;
the request/finalize split was found by comparing against a second,
similarly GEN-moving sibling project - see "Test suite" below.

## Patterns carried forward, deliberately, from MatchGuard

- `gl.vm.UserError` for every guard failure, never a bare `UserError`.
- Records stored as JSON strings inside `TreeMap[str, str]`, never as
  `@dataclass`/`@allow_storage` structures.
- Addresses accepted as plain `str` in every public signature, normalized
  through an `_address_to_str` helper, compared case-insensitively.
- `_now_utc()` reads the GenVM-agreed deterministic clock directly
  (`datetime.datetime.now(datetime.timezone.utc)`), never through a
  nondet() web fetch. All timestamps are ISO-8601 UTC strings.
- `gl.eq_principle.prompt_comparative` (never `strict_eq`) for LLM-derived
  evidence verdicts.

## What is genuinely new here (not proven by the sibling project)

MatchGuard never moved GEN and never called another contract, so neither
mechanism below was exercised by a reviewed project. Both are the
best-effort, documented interpretation of GenLayer's public docs - the
first things to double-check on an actual Studio deployment:

- Reading attached native value via `gl.message.value` inside a
  `@gl.public.write.payable` method (confirmed against a real Studio
  deployment: a plain `@gl.public.write` never exposes a value field to
  send GEN with at all - `.payable` is required on any method meant to
  receive it).
- Cross-contract calls: `.view().method(...)` for reads,
  `.emit(on="accepted").method(...)` for triggering a write on another
  contract (the callee sees the calling *contract's* address as sender,
  not the original transaction's sender), and a bare
  `.emit(value=amount, on="accepted")` with no method chained for a native
  value payout to an address that may be a plain EOA.

## Test suite

Offline, fully deterministic tests using a minimal stub of the `genlayer`
SDK (`tests/genlayer_stub/`), extended from MatchGuard's own stub with an
in-memory contract registry (for `gl.get_contract_at`) and GEN ledger (for
`gl.message.value` and payouts) - no real network, LLM, or multi-validator
consensus involved. All 25 tests were run and passed against this stub
before being committed.

```bash
pip install pytest
pytest tests/ -v
```

Coverage includes: funding guards and state transitions in
`PolicyRegistry` (including that `cancel_if_unfunded` actually refunds the
insured and zeroes the registry's balance); evidence locking, two-source
agreement, single-source disagreement resolving to INCONCLUSIVE, the
one-challenge-per-side bound (and that each side's challenge is
independent), the forced-immediate-INCONCLUSIVE path when the final
deadline has already passed, and normal finalize-after-window in
`DeliveryOracle`; and, in `SettlementVault`, all three payout paths with
actual ledger-balance assertions (not just state checks), plus settlement
idempotency.

## Deploying

1. Deploy `PolicyRegistry`, `DeliveryOracle`, `SettlementVault` (no
   constructor arguments - each `__init__` only records its own owner).
2. Call `PolicyRegistry.set_linked_contracts(oracle_address, vault_address)`.
3. Call `DeliveryOracle.set_registry(registry_address)`.
4. Call `SettlementVault.set_linked_contracts(registry_address, oracle_address)`.

All three `set_*` calls are one-time and owner-only (the deployer of each
contract). Then update the three `_ADDRESS` constants near the top of
`index.html`'s `<script>` block.

**Live on GenLayer Studio (studionet):**

```
PolicyRegistry:  0xff341cd2B736869814ae8591c0A6183230F74A34
DeliveryOracle:  0xbA76bFD84b29260F1E0583074B92F249f4CBC561
SettlementVault: 0x3b01D37F85032a8988cE5Cd84990fE713CE92454
```

## Policy lifecycle

```
create_and_fund_policy()  [insured, value = premium_amount]
        -> FUNDED_INSURED
fund_as_underwriter()     [underwriter, value = coverage_amount]
        -> ACTIVE
        (or cancel_if_unfunded() -> CANCELLED + premium refunded, if nobody
         underwrites before funding_deadline)
request_resolution()      [anyone, after evidence_lock_time]
        -> runs the nondet evidence evaluation, stores the resolution
           (policy stays ACTIVE - see README "How settlement actually
           moves GEN" for why the registry call is deferred)
challenge_resolution()    [insured/underwriter, within 48h, once each]
        -> re-evaluates with extra evidence, versioned
finalize_resolution()     [anyone, after the challenge window or final_deadline]
        -> resolution.is_final = true; registry -> RESOLVING; escrow
           forwarded to the vault
settle()                  [anyone]
        -> SettlementVault pays out per the table above; policy -> SETTLED
```

## Frontend

`index.html` is a single-file, dependency-free (besides `genlayer-js` from
a CDN) reference frontend, in the same style as MatchGuard's: connect
MetaMask or start a throwaway local "test session" account, create and fund
a policy, underwrite it, walk it through resolution and challenge, settle
it, or read any policy's full state. Every dynamic value is rendered via
`textContent`/`createElement`, never `innerHTML`. Set the three `_ADDRESS`
constants near the top of the `<script>` block to your deployed contracts
before using it. No build step, no npm, nothing to install - open the file
or serve it as a GitHub Pages site straight from the repo root.

## Findings from live Studio testing

This project was actually deployed and exercised on GenLayer Studio during
development, not just written and left untested. Three real, load-bearing
bugs were only caught this way:

1. **The escrow-forwarding bug** (see "How settlement actually moves GEN"
   above) - `SettlementVault` had nothing to pay out with, since GEN
   landed in `PolicyRegistry` and never moved. Found by running the
   contracts end-to-end against the offline test stub, before ever
   touching Studio.
2. **`@gl.public.write.payable` is required, not just `@gl.public.write`,
   for any method meant to receive `gl.message.value`.** A plain
   `@gl.public.write` compiles and deploys fine but Studio's UI never
   offers a value field for it at all - confirmed live: `create_and_fund_policy`
   and `fund_as_underwriter` are the two methods that need it.
3. **A nondet block (web fetch + LLM) and a cross-contract call should not
   be mixed in the same transaction.** `request_resolution` used to run
   the evidence evaluation and immediately call the registry's
   `mark_resolving` in one call. Comparing against a second GEN-moving
   sibling project (ProofWorksEscrow, which splits `evaluate_task` from
   `finalize_task` for exactly this reason) led to splitting
   `request_resolution` (nondet only) from `finalize_resolution` (which
   now does the registry call, since it never runs a nondet block itself).
4. **A transaction can be FINALIZED by consensus while its contract
   execution failed.** `index.html` originally reported "Done." for any
   call whose promise resolved. Found live twice: a `request_resolution`
   call that should have reverted (a resolution already existed for that
   policy) showed success in the UI both before and after a first attempted
   fix, because that fix guessed a field name that didn't match. Pulling
   the actual failed transaction's **raw JSON** from GenLayer's own block
   explorer confirmed the real field:
   `consensus_data.leader_receipt[0].execution_result` is the literal
   string `"ERROR"` on failure (`"SUCCESS"` on success) - the same object
   level `extractReturnValue` already reads `.result.payload.readable`
   from. The final fix checks that exact field, with a whole-receipt
   substring scan kept as a second, independent fallback, and was
   verified against the real failure's exact JSON shape before being
   committed.

Confirmed working live, with real GEN, on GenLayer Studio: `gl.message.value`
correctly read and validated inside a `@gl.public.write.payable` method
(across two different calling addresses); reading another contract's state
via `.view()`; the full nondet evidence pipeline (real web fetch + LLM
verdict extraction + 5-validator agreement); `challenge_resolution`
re-evaluating with an added source; a bare `.emit(value=amount,
on="accepted")` payout to a plain EOA (`cancel_if_unfunded`); and, using a
throwaway deployment with `CHALLENGE_WINDOW_HOURS` set to `0` purely to
avoid a 48-hour wait, the entire remaining chain in one pass -
`.emit(on="accepted").mark_resolving(...)` (a real cross-contract *write*,
confirmed via the child transaction showing the Oracle's own address, not
the caller's, as `From`), the resulting GEN transfer from registry to
vault, and a full `settle()` call correctly reading both linked contracts,
paying out, and calling `mark_settled`. Every mechanism this project relies
on has now been exercised against the real network at least once.

## Known limitations / areas to verify (disclosed, not hidden)

- **An unreachable tracking source now degrades gracefully.** A dead link
  or non-2xx response used to propagate as a raw, unrecoverable contract
  error (found live: a placeholder `/track1` path 404ing crashed the
  whole `request_resolution` call). It's now caught per-source and counted
  as `UNKNOWN`, so the resolution can still reach INCONCLUSIVE (or DELAYED/
  ON_TIME, if the other source is reachable and a second real source
  agrees) instead of reverting the transaction outright.
- **A short delay between `finalize_resolution` and `settle()` may be
  needed in practice.** Cross-contract calls made via `.emit(...)` create
  separate child transactions that go through their own consensus round
  rather than landing atomically inside the parent call; on live testing
  this resolved well within a few seconds, but a caller (e.g. the
  frontend) calling `settle()` immediately in the same instant as
  `finalize_resolution` should be prepared to retry once if it sees
  "policy is not awaiting settlement".
- **GEN decimals.** `index.html` assumes 18 decimals (same as ETH/wei) when
  converting GEN amounts typed by the user into the integer base units the
  contracts expect. Confirm this against Studio.
- **A challenge can only add ONE new source per call**, same bound as
  MatchGuard; flipping a verdict that needs several additional
  corroborating sources requires several successive challenge calls, still
  capped at one per side.
- **Tracking source format.** `tracking_ref` is a `"|"`-joined list of URLs
  the oracle fetches directly and asks an LLM to interpret. Real
  carrier/tracking sites will likely need prompt tuning per carrier to get
  a reliable delivered/not-delivered verdict from rendered page text.
