# { "Depends": "py-genlayer:latest" }
from genlayer import *
import json


class SettlementVault(gl.Contract):
    """
    SettlementVault - ShipGuard parametric shipping-delay insurance.

    The piece MatchGuard explicitly left out of scope: this contract
    actually moves GEN once DeliveryOracle has a final resolution. It
    never re-derives a verdict itself - it only reads the final
    decision and the policy's escrowed amounts from the two linked
    contracts, then executes exactly one of three payout paths.
    Settlement is idempotent per policy.

    See policy_registry.py's module docstring for the conventions
    carried forward from the sibling MatchGuard project, and for which
    parts (value handling, cross-contract calls) are new and unproven.

    Payout rules:
      DELAYED       -> insured receives coverage_amount (the claim
                       payout); underwriter receives premium_amount
                       (earned for underwriting the risk, regardless
                       of outcome).
      ON_TIME       -> underwriter receives both coverage_amount
                       (their collateral, returned) and premium_amount
                       (earned).
      INCONCLUSIVE  -> insured is refunded premium_amount; the
                       underwriter's coverage_amount collateral is
                       returned. No party profits or loses from an
                       ambiguous outcome.
    """

    DECISION_DELAYED = "DELAYED"
    DECISION_ON_TIME = "ON_TIME"
    DECISION_INCONCLUSIVE = "INCONCLUSIVE"

    owner: str
    registry_address: str
    oracle_address: str
    settled: TreeMap[str, str]  # policy_id -> "1" once settled

    def __init__(self):
        self.owner = self._address_to_str(gl.message.sender_address)
        self.registry_address = ""
        self.oracle_address = ""

    # ----------------------------------------------------------------
    # Helpers (identical conventions to policy_registry.py)
    # ----------------------------------------------------------------

    def _address_to_str(self, value) -> str:
        try:
            return str(Address(str(value)))
        except Exception:
            raise gl.vm.UserError(f"{value!r} is not a valid on-chain address.")

    def _send_value(self, to: str, amount) -> None:
        amount_int = int(amount)
        if amount_int <= 0:
            return
        gl.get_contract_at(Address(to)).emit(value=u256(amount_int), on="accepted")

    # ----------------------------------------------------------------
    # Owner-only, one-time wiring
    # ----------------------------------------------------------------

    @gl.public.write
    def set_linked_contracts(self, registry_address: str, oracle_address: str) -> str:
        caller = self._address_to_str(gl.message.sender_address)
        if caller.lower() != self.owner.lower():
            raise gl.vm.UserError("only the deploying owner may call set_linked_contracts.")
        if self.registry_address or self.oracle_address:
            raise gl.vm.UserError("linked contracts have already been set.")
        self.registry_address = self._address_to_str(registry_address)
        self.oracle_address = self._address_to_str(oracle_address)
        return "ok"

    # ----------------------------------------------------------------
    # Public write methods
    # ----------------------------------------------------------------

    @gl.public.write
    def settle(self, policy_id: str) -> str:
        if self.settled.get(policy_id):
            raise gl.vm.UserError("policy has already been settled.")

        resolution_raw = gl.get_contract_at(Address(self.oracle_address)).view().get_resolution_json(
            policy_id
        )
        resolution = json.loads(resolution_raw)
        if not resolution["is_final"]:
            raise gl.vm.UserError("resolution is not final yet.")

        policy_raw = gl.get_contract_at(Address(self.registry_address)).view().get_policy_json(
            policy_id
        )
        policy = json.loads(policy_raw)
        if policy["state"] != "RESOLVING":
            raise gl.vm.UserError("policy is not awaiting settlement.")

        insured = policy["insured"]
        underwriter = policy["underwriter"]
        premium = int(policy["premium_amount"])
        coverage = int(policy["coverage_amount"])
        decision = resolution["decision"]

        if decision == self.DECISION_DELAYED:
            self._send_value(insured, coverage)
            self._send_value(underwriter, premium)
        elif decision == self.DECISION_ON_TIME:
            self._send_value(underwriter, coverage + premium)
        elif decision == self.DECISION_INCONCLUSIVE:
            self._send_value(insured, premium)
            self._send_value(underwriter, coverage)
        else:
            raise gl.vm.UserError(f"unrecognized decision from oracle: {decision!r}.")

        self.settled[policy_id] = "1"
        gl.get_contract_at(Address(self.registry_address)).emit(on="accepted").mark_settled(
            policy_id, decision
        )
        return "ok"

    # ----------------------------------------------------------------
    # Public view methods
    # ----------------------------------------------------------------

    @gl.public.view
    def is_settled(self, policy_id: str) -> bool:
        return bool(self.settled.get(policy_id))
