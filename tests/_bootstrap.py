"""
Shared test bootstrap - wires up the offline genlayer SDK stub, loads
the three ShipGuard contracts, deploys and links them. Same pattern
used by the sibling MatchGuard project, extended with
`genlayer_stub`'s cross-contract call + ledger support (see that
module's docstring) since ShipGuard, unlike MatchGuard, is three
contracts that call each other and actually move GEN.
"""

import importlib.util
import os
import sys
import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STUB_DIR = os.path.join(_THIS_DIR, "genlayer_stub")
if _STUB_DIR not in sys.path:
    sys.path.insert(0, _STUB_DIR)

_CONTRACTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "contracts")


def _load_module(name, filename):
    path = os.path.join(_CONTRACTS_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_registry_module = _load_module("shipguard_policy_registry", "policy_registry.py")
_oracle_module = _load_module("shipguard_delivery_oracle", "delivery_oracle.py")
_vault_module = _load_module("shipguard_settlement_vault", "settlement_vault.py")

PolicyRegistry = _registry_module.PolicyRegistry
DeliveryOracle = _oracle_module.DeliveryOracle
SettlementVault = _vault_module.SettlementVault
gl = _registry_module.gl
Address = _registry_module.Address
Ledger = _registry_module.Ledger
from genlayer import register_contract  # noqa: E402  (path set up above)

# Fixed, distinct addresses reused across test files.
OWNER_ADDRESS = "0x" + "00" * 20
REGISTRY_ADDRESS = "0x" + "01" * 20
ORACLE_ADDRESS = "0x" + "02" * 20
VAULT_ADDRESS = "0x" + "03" * 20
INSURED_ADDRESS = "0x" + "11" * 20
UNDERWRITER_ADDRESS = "0x" + "22" * 20
STRANGER_ADDRESS = "0x" + "33" * 20


def set_caller(address_str: str) -> None:
    """Simulate a specific wallet calling the next contract method."""
    gl.message.sender_address = Address(address_str)


def set_value(amount: int) -> None:
    """Attach native value to the next call (must be a payable method)."""
    from genlayer import u256

    gl.message.value = u256(int(amount))


def clear_value() -> None:
    set_value(0)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def deploy_all():
    """
    Deploy all three contracts (as OWNER_ADDRESS) and wire them
    together in the same order the README documents for a real
    deployment. Also resets the stub's contract registry and ledger,
    so each test starts from a clean slate.
    """
    from genlayer import _ContractRegistry  # type: ignore[attr-defined]

    _ContractRegistry.reset()
    Ledger.reset()
    clear_value()

    set_caller(OWNER_ADDRESS)
    registry = PolicyRegistry()
    oracle = DeliveryOracle()
    vault = SettlementVault()

    register_contract(REGISTRY_ADDRESS, registry)
    register_contract(ORACLE_ADDRESS, oracle)
    register_contract(VAULT_ADDRESS, vault)

    set_caller(OWNER_ADDRESS)
    registry.set_linked_contracts(ORACLE_ADDRESS, VAULT_ADDRESS)
    oracle.set_registry(REGISTRY_ADDRESS)
    vault.set_linked_contracts(REGISTRY_ADDRESS, ORACLE_ADDRESS)

    return {"registry": registry, "oracle": oracle, "vault": vault}


DEFAULT_TRACKING_REF = "https://tracker.example.com/A|https://tracker2.example.com/A"


def create_default_policy(registry, **overrides):
    """Create (and fund the premium for) a policy with sane defaults;
    any field can be overridden via kwargs."""
    now = now_utc()
    kwargs = dict(
        carrier="Maersk",
        tracking_ref=DEFAULT_TRACKING_REF,
        declared_eta=iso(now + datetime.timedelta(days=10)),
        delay_threshold_days=3,
        premium_amount=10**17,  # 0.1 GEN
        coverage_amount=10**18,  # 1 GEN
        waiting_period_days=2,
        evidence_buffer_days=2,
        funding_window_days=7,
    )
    kwargs.update(overrides)

    set_caller(INSURED_ADDRESS)
    set_value(kwargs["premium_amount"])
    policy_id = registry.create_and_fund_policy(
        kwargs["carrier"],
        kwargs["tracking_ref"],
        kwargs["declared_eta"],
        kwargs["delay_threshold_days"],
        kwargs["premium_amount"],
        kwargs["coverage_amount"],
        kwargs["waiting_period_days"],
        kwargs["evidence_buffer_days"],
        kwargs["funding_window_days"],
    )
    clear_value()
    return policy_id, kwargs


def fund_default_underwriter(registry, policy_id, kwargs):
    set_caller(UNDERWRITER_ADDRESS)
    set_value(kwargs["coverage_amount"])
    registry.fund_as_underwriter(policy_id)
    clear_value()


def mock_two_sources_agree(monkeypatch, decision: str):
    """Patch the stub's nondet web/LLM calls so both default tracking
    sources agree on `decision` ("DELAYED" or "ON_TIME")."""
    monkeypatch.setattr(
        gl.nondet.web.__class__, "render", staticmethod(lambda url, mode="text": f"page for {url}")
    )
    monkeypatch.setattr(
        gl.nondet.__class__,
        "exec_prompt",
        staticmethod(
            lambda prompt, response_format="text": (
                f'{{"delivered": true, "verdict": "{decision}", "detail": "mocked"}}'
            )
        ),
    )
