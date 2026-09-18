# { "Depends": "py-genlayer:latest" }
from genlayer import *
import json
import datetime


class PolicyRegistry(gl.Contract):
    """
    PolicyRegistry - ShipGuard parametric shipping-delay insurance.

    -------------------------------------------------------------------
    WHY THIS EXISTS / HOW IT DIFFERS FROM A SCORE-ONLY ORACLE
    -------------------------------------------------------------------
    MatchGuard settles an authoritative decision and explicitly leaves
    fund movement to a separate escrow/payout layer. ShipGuard closes
    that gap: PolicyRegistry actually custodies GEN, in per-policy
    escrow rather than a shared pool. An "insured" address funds a
    premium; a separate "underwriter" address locks matching coverage.
    Every GEN unit that enters has a defined owner at every point in
    time - nothing sits here unattributed.

    -------------------------------------------------------------------
    PATTERNS CARRIED FORWARD, DELIBERATELY, FROM MatchGuard
    -------------------------------------------------------------------
    This is a clean-room design for a different problem (custody +
    payout, not just a settlement decision), but it deliberately reuses
    proven, reviewed conventions from that sibling project rather than
    inventing new ones where a working pattern already exists:

      - `gl.vm.UserError` for every guard failure (never a bare
        `UserError`).
      - Records stored as JSON strings inside `TreeMap[str, str]`,
        never as `@dataclass`/`@allow_storage` structures - keeps the
        storage surface simple and easy to reason about in review.
      - Addresses accepted as plain `str` in every public signature,
        normalized through `_address_to_str`, and compared
        case-insensitively.
      - `_now_utc()` reads the GenVM-agreed deterministic clock
        directly (`datetime.datetime.now(datetime.timezone.utc)`),
        never through a nondet() web fetch. Timestamps are stored and
        compared as ISO-8601 UTC strings throughout, exactly as in
        MatchGuard.

    -------------------------------------------------------------------
    WHAT IS GENUINELY NEW HERE (NOT PROVEN BY THE SIBLING PROJECT)
    -------------------------------------------------------------------
    MatchGuard never moves GEN and never calls another contract - so
    neither of those two mechanisms below were exercised by a reviewed
    project. Both are implemented as the best-effort, documented
    interpretation of GenLayer's public docs, and are exactly the
    things to double- and triple-check first once this is actually
    deployed to Studio (see README.md "Known areas to verify"):

      - Reading attached native value via `gl.message.value` inside a
        `@gl.public.write` method.
      - Cross-contract calls: `.view().method(...)` for reads,
        `.emit(on="accepted").method(...)` for triggering a write on
        another contract, and a bare `.emit(value=amount, on="accepted")`
        (no method chained) for a native-value payout to an address
        that may be a plain EOA rather than a contract.
    """

    STATE_FUNDED_INSURED = "FUNDED_INSURED"
    STATE_ACTIVE = "ACTIVE"
    STATE_CANCELLED = "CANCELLED"
    STATE_RESOLVING = "RESOLVING"
    STATE_SETTLED = "SETTLED"

    MAX_CARRIER_CHARS = 120
    MAX_TRACKING_REF_CHARS = 2000
    MIN_TRACKING_SOURCES = 2

    owner: str
    oracle_address: str
    vault_address: str
    policy_count: u256
    policies: TreeMap[str, str]

    def __init__(self):
        self.owner = self._address_to_str(gl.message.sender_address)
        self.oracle_address = ""
        self.vault_address = ""
        self.policy_count = u256(0)

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _address_to_str(self, value) -> str:
        """Normalize an Address (or address-like string) into its
        canonical string form, or raise gl.vm.UserError."""
        try:
            return str(Address(str(value)))
        except Exception:
            raise gl.vm.UserError(f"{value!r} is not a valid on-chain address.")

    def _now_utc(self):
        """Return the current, GenVM-agreed UTC timestamp. Only ever
        called from deterministic code, never from inside a nondet()
        closure."""
        return datetime.datetime.now(datetime.timezone.utc)

    def _parse_iso8601_utc(self, raw: str):
        """Deterministically parse an ISO-8601 timestamp into a
        timezone-aware UTC datetime, or None if unparseable."""
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)

    def _load(self, policy_id: str) -> dict:
        raw = self.policies.get(policy_id)
        if raw is None:
            raise gl.vm.UserError(f"unknown policy_id: {policy_id!r}")
        return json.loads(raw)

    def _save(self, policy_id: str, policy: dict) -> None:
        self.policies[policy_id] = json.dumps(policy, sort_keys=True)

    def _send_value(self, to: str, amount) -> None:
        amount_int = int(amount)
        if amount_int <= 0:
            return
        gl.get_contract_at(Address(to)).emit(value=u256(amount_int), on="accepted")

    # ----------------------------------------------------------------
    # Owner-only, one-time wiring
    # ----------------------------------------------------------------

    @gl.public.write
    def set_linked_contracts(self, oracle_address: str, vault_address: str) -> str:
        caller = self._address_to_str(gl.message.sender_address)
        if caller.lower() != self.owner.lower():
            raise gl.vm.UserError("only the deploying owner may call set_linked_contracts.")
        if self.oracle_address or self.vault_address:
            raise gl.vm.UserError("linked contracts have already been set.")
        self.oracle_address = self._address_to_str(oracle_address)
        self.vault_address = self._address_to_str(vault_address)
        return "ok"

    # ----------------------------------------------------------------
    # Public write methods
    # ----------------------------------------------------------------

    @gl.public.write
    def create_and_fund_policy(
        self,
        carrier: str,
        tracking_ref: str,
        declared_eta: str,
        delay_threshold_days: u256,
        premium_amount: u256,
        coverage_amount: u256,
        waiting_period_days: u256,
        evidence_buffer_days: u256,
        funding_window_days: u256,
    ) -> str:
        """
        Create a shipping-delay policy and fund its premium in the same
        call: `gl.message.value` must exactly equal `premium_amount`.

        `carrier` + `tracking_ref` (a "|"-joined list of tracking
        source URLs, at least two) form the canonical event identity
        DeliveryOracle later locks evidence against.

        `declared_eta` must be an ISO-8601 UTC timestamp far enough in
        the future to clear `waiting_period_days` (insider-knowledge
        protection: coverage only starts `waiting_period_days` after
        purchase). Evidence unlocks at
        `declared_eta + evidence_buffer_days`, and a hard
        `final_deadline` of `declared_eta + 30 days` guarantees no GEN
        is ever left stuck on an unresolved shipment.

        Returns the policy_id used for every subsequent call.
        """
        insured = self._address_to_str(gl.message.sender_address)

        carrier_text = (carrier or "").strip()
        if not carrier_text or len(carrier_text) > self.MAX_CARRIER_CHARS:
            raise gl.vm.UserError(
                f"carrier must be non-empty and at most {self.MAX_CARRIER_CHARS} characters."
            )

        tracking_text = (tracking_ref or "").strip()
        if not tracking_text or len(tracking_text) > self.MAX_TRACKING_REF_CHARS:
            raise gl.vm.UserError(
                f"tracking_ref must be non-empty and at most {self.MAX_TRACKING_REF_CHARS} characters."
            )
        sources = [s.strip() for s in tracking_text.split("|") if s.strip()]
        if len(sources) < self.MIN_TRACKING_SOURCES:
            raise gl.vm.UserError(
                f"tracking_ref must list at least {self.MIN_TRACKING_SOURCES} distinct sources, separated by '|'."
            )

        if int(premium_amount) <= 0 or int(coverage_amount) <= 0:
            raise gl.vm.UserError("premium_amount and coverage_amount must both be positive.")
        if int(gl.message.value) != int(premium_amount):
            raise gl.vm.UserError("attached value must exactly equal premium_amount.")

        now = self._now_utc()
        eta_dt = self._parse_iso8601_utc(str(declared_eta))
        if eta_dt is None:
            raise gl.vm.UserError(
                f"declared_eta must be a valid ISO-8601 timestamp (got {declared_eta!r})."
            )

        coverage_start_dt = now + datetime.timedelta(days=int(waiting_period_days))
        if eta_dt <= coverage_start_dt:
            raise gl.vm.UserError(
                "declared_eta must be far enough in the future to clear the waiting period."
            )

        evidence_lock_dt = eta_dt + datetime.timedelta(days=int(evidence_buffer_days))
        final_deadline_dt = eta_dt + datetime.timedelta(days=30)
        funding_deadline_dt = now + datetime.timedelta(days=int(funding_window_days))

        policy_id = str(int(self.policy_count))
        self._save(
            policy_id,
            {
                "policy_id": policy_id,
                "state": self.STATE_FUNDED_INSURED,
                "insured": insured,
                "underwriter": "",
                "carrier": carrier_text,
                "tracking_ref": tracking_text,
                "declared_eta": eta_dt.isoformat(),
                "delay_threshold_days": int(delay_threshold_days),
                "premium_amount": int(premium_amount),
                "coverage_amount": int(coverage_amount),
                "coverage_start": coverage_start_dt.isoformat(),
                "evidence_lock_time": evidence_lock_dt.isoformat(),
                "final_deadline": final_deadline_dt.isoformat(),
                "funding_deadline": funding_deadline_dt.isoformat(),
                "resolution": "",
                "created_at": now.isoformat(),
            },
        )
        self.policy_count = u256(int(self.policy_count) + 1)
        return policy_id

    @gl.public.write
    def fund_as_underwriter(self, policy_id: str) -> str:
        policy = self._load(policy_id)
        if policy["state"] != self.STATE_FUNDED_INSURED:
            raise gl.vm.UserError("policy is not awaiting underwriter funding.")

        underwriter = self._address_to_str(gl.message.sender_address)
        if underwriter.lower() == policy["insured"].lower():
            raise gl.vm.UserError("underwriter must be a different address from the insured.")
        if int(gl.message.value) != int(policy["coverage_amount"]):
            raise gl.vm.UserError("attached value must exactly equal coverage_amount.")

        policy["underwriter"] = underwriter
        policy["state"] = self.STATE_ACTIVE
        self._save(policy_id, policy)
        return "ok"

    @gl.public.write
    def cancel_if_unfunded(self, policy_id: str) -> str:
        policy = self._load(policy_id)
        if policy["state"] != self.STATE_FUNDED_INSURED:
            raise gl.vm.UserError("policy is not in an unfunded state.")

        now = self._now_utc()
        deadline_dt = self._parse_iso8601_utc(policy["funding_deadline"])
        if now < deadline_dt:
            raise gl.vm.UserError("funding window has not expired yet.")

        policy["state"] = self.STATE_CANCELLED
        self._save(policy_id, policy)
        self._send_value(policy["insured"], policy["premium_amount"])
        return "ok"

    @gl.public.write
    def mark_resolving(self, policy_id: str) -> str:
        caller = self._address_to_str(gl.message.sender_address)
        if not self.oracle_address or caller.lower() != self.oracle_address.lower():
            raise gl.vm.UserError("only the linked DeliveryOracle may call mark_resolving.")

        policy = self._load(policy_id)
        if policy["state"] != self.STATE_ACTIVE:
            raise gl.vm.UserError("policy is not active.")
        policy["state"] = self.STATE_RESOLVING
        self._save(policy_id, policy)

        # PolicyRegistry is where the premium and coverage actually
        # landed (via create_and_fund_policy / fund_as_underwriter), but
        # SettlementVault is the contract responsible for paying out.
        # Forward the whole escrowed pot to the vault now, so it
        # actually holds the GEN it will need at settle() time - this
        # was found and fixed by actually running the contracts against
        # the offline test stub (see README.md "Known areas to verify"
        # for why this specific step still needs re-checking on Studio).
        total_escrow = int(policy["premium_amount"]) + int(policy["coverage_amount"])
        self._send_value(self.vault_address, total_escrow)
        return "ok"

    @gl.public.write
    def mark_settled(self, policy_id: str, resolution: str) -> str:
        caller = self._address_to_str(gl.message.sender_address)
        if not self.vault_address or caller.lower() != self.vault_address.lower():
            raise gl.vm.UserError("only the linked SettlementVault may call mark_settled.")

        policy = self._load(policy_id)
        if policy["state"] != self.STATE_RESOLVING:
            raise gl.vm.UserError("policy is not resolving.")
        policy["state"] = self.STATE_SETTLED
        policy["resolution"] = resolution
        self._save(policy_id, policy)
        return "ok"

    # ----------------------------------------------------------------
    # Public view methods
    # ----------------------------------------------------------------

    @gl.public.view
    def get_policy_json(self, policy_id: str) -> str:
        return self.policies[policy_id]

    @gl.public.view
    def get_policy_count(self) -> u256:
        return self.policy_count

    @gl.public.view
    def get_linked_contracts(self) -> str:
        return json.dumps({"oracle_address": self.oracle_address, "vault_address": self.vault_address})
