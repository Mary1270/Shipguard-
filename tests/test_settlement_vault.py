import datetime
import json

import pytest

from _bootstrap import (
    deploy_all,
    set_caller,
    now_utc,
    create_default_policy,
    fund_default_underwriter,
    mock_two_sources_agree,
    INSURED_ADDRESS,
    UNDERWRITER_ADDRESS,
    VAULT_ADDRESS,
)
from genlayer import Ledger


def _freeze(monkeypatch, registry, oracle, when: datetime.datetime) -> None:
    monkeypatch.setattr(type(registry), "_now_utc", lambda self: when)
    monkeypatch.setattr(type(oracle), "_now_utc", lambda self: when)


def _resolved_policy(monkeypatch, decision: str):
    contracts = deploy_all()
    registry, oracle, vault = contracts["registry"], contracts["oracle"], contracts["vault"]
    policy_id, kwargs = create_default_policy(
        registry, waiting_period_days=1, evidence_buffer_days=1
    )
    fund_default_underwriter(registry, policy_id, kwargs)

    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, decision)
    oracle.request_resolution(policy_id)

    past_window = unlock + datetime.timedelta(hours=49)
    _freeze(monkeypatch, registry, oracle, past_window)
    oracle.finalize_resolution(policy_id)

    return registry, oracle, vault, policy_id, kwargs


def test_settle_before_finalization_reverts(monkeypatch):
    contracts = deploy_all()
    registry, oracle, vault = contracts["registry"], contracts["oracle"], contracts["vault"]
    policy_id, kwargs = create_default_policy(
        registry, waiting_period_days=1, evidence_buffer_days=1
    )
    fund_default_underwriter(registry, policy_id, kwargs)

    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "DELAYED")
    oracle.request_resolution(policy_id)

    with pytest.raises(Exception, match="resolution is not final yet"):
        vault.settle(policy_id)


def test_settle_delayed_pays_insured_coverage_and_underwriter_premium(monkeypatch):
    registry, oracle, vault, policy_id, kwargs = _resolved_policy(monkeypatch, "DELAYED")

    vault.settle(policy_id)

    assert vault.is_settled(policy_id) is True
    assert Ledger.balance_of(INSURED_ADDRESS) == kwargs["coverage_amount"]
    assert Ledger.balance_of(UNDERWRITER_ADDRESS) == kwargs["premium_amount"]
    assert Ledger.balance_of(VAULT_ADDRESS) == 0

    policy = json.loads(registry.get_policy_json(policy_id))
    assert policy["state"] == "SETTLED"
    assert policy["resolution"] == "DELAYED"


def test_settle_on_time_pays_underwriter_everything(monkeypatch):
    registry, oracle, vault, policy_id, kwargs = _resolved_policy(monkeypatch, "ON_TIME")

    vault.settle(policy_id)

    assert Ledger.balance_of(INSURED_ADDRESS) == 0
    assert Ledger.balance_of(UNDERWRITER_ADDRESS) == kwargs["coverage_amount"] + kwargs["premium_amount"]
    assert Ledger.balance_of(VAULT_ADDRESS) == 0


def test_settle_twice_reverts(monkeypatch):
    registry, oracle, vault, policy_id, kwargs = _resolved_policy(monkeypatch, "DELAYED")
    vault.settle(policy_id)

    with pytest.raises(Exception, match="already been settled"):
        vault.settle(policy_id)


def test_set_linked_contracts_is_one_time_and_owner_only():
    contracts = deploy_all()
    vault = contracts["vault"]
    set_caller(INSURED_ADDRESS)
    with pytest.raises(Exception, match="only the deploying owner"):
        vault.set_linked_contracts("0x" + "09" * 20, "0x" + "0a" * 20)
