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
When the oracle calls `mark_resolving` at the start of resolution,
`PolicyRegistry` forwards the whole escrowed pot (premium + coverage) to
`SettlementVault`, so that by the time `settle()` runs, the vault actually
holds the GEN it needs to pay out. This was found and fixed by actually
running the contracts end-to-end against the offline test stub during
development - see "Test suite" below - not just by reading the code.

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
  `@gl.public.write` method (no `.payable` decorator variant is assumed -
  plain `@gl.public.write` is used everywhere, including for methods that
  read `gl.message.value`).
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

## Policy lifecycle

```
create_and_fund_policy()  [insured, value = premium_amount]
        -> FUNDED_INSURED
fund_as_underwriter()     [underwriter, value = coverage_amount]
        -> ACTIVE
        (or cancel_if_unfunded() -> CANCELLED + premium refunded, if nobody
         underwrites before funding_deadline)
request_resolution()      [anyone, after evidence_lock_time]
        -> RESOLVING; registry forwards the escrowed pot to the vault
challenge_resolution()    [insured/underwriter, within 48h, once each]
        -> re-evaluates with extra evidence, versioned
finalize_resolution()     [anyone, after the challenge window or final_deadline]
        -> resolution.is_final = true
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

## Known limitations / areas to verify (disclosed, not hidden)

- **Native value handling is unproven.** `gl.message.value`,
  `.emit(value=...)`, and a bare value-only `.emit()` to an EOA are all
  implemented per GenLayer's public docs, but MatchGuard never needed any
  of them - these are the first things to test against a real Studio
  deployment with a small amount of GEN before trusting them with more.
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
