import datetime
import json

import pytest

from _bootstrap import (
    deploy_all,
    set_caller,
    set_value,
    clear_value,
    now_utc,
    iso,
    create_default_policy,
    fund_default_underwriter,
    INSURED_ADDRESS,
    UNDERWRITER_ADDRESS,
    STRANGER_ADDRESS,
)
from genlayer import Ledger


@pytest.fixture
def contracts():
    return deploy_all()


def test_create_and_fund_sets_state_and_credits_registry(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)

    policy = json.loads(registry.get_policy_json(policy_id))
    assert policy["state"] == "FUNDED_INSURED"
    assert policy["insured"].lower() == INSURED_ADDRESS.lower()
    assert policy["underwriter"] == ""
    assert Ledger.balance_of("0x" + "01" * 20) == kwargs["premium_amount"]


def test_wrong_attached_value_is_rejected(contracts):
    registry = contracts["registry"]
    set_caller(INSURED_ADDRESS)
    set_value(1)  # not equal to premium_amount
    with pytest.raises(Exception, match="attached value must exactly equal premium_amount"):
        registry.create_and_fund_policy(
            "Maersk",
            "https://a.example.com|https://b.example.com",
            iso(now_utc() + datetime.timedelta(days=10)),
            3,
            10**17,
            10**18,
            2,
            2,
            7,
        )
    clear_value()


def test_declared_eta_too_soon_is_rejected(contracts):
    registry = contracts["registry"]
    set_caller(INSURED_ADDRESS)
    set_value(10**17)
    with pytest.raises(Exception, match="far enough in the future"):
        registry.create_and_fund_policy(
            "Maersk",
            "https://a.example.com|https://b.example.com",
            iso(now_utc() + datetime.timedelta(days=1)),  # too soon for a 2-day waiting period
            3,
            10**17,
            10**18,
            2,
            2,
            7,
        )
    clear_value()


def test_single_tracking_source_is_rejected(contracts):
    registry = contracts["registry"]
    set_caller(INSURED_ADDRESS)
    set_value(10**17)
    with pytest.raises(Exception, match="at least 2 distinct sources"):
        registry.create_and_fund_policy(
            "Maersk",
            "https://only-one.example.com",
            iso(now_utc() + datetime.timedelta(days=10)),
            3,
            10**17,
            10**18,
            2,
            2,
            7,
        )
    clear_value()


def test_underwriter_funding_activates_policy_and_credits_registry(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)
    fund_default_underwriter(registry, policy_id, kwargs)

    policy = json.loads(registry.get_policy_json(policy_id))
    assert policy["state"] == "ACTIVE"
    assert policy["underwriter"].lower() == UNDERWRITER_ADDRESS.lower()
    assert Ledger.balance_of("0x" + "01" * 20) == kwargs["premium_amount"] + kwargs["coverage_amount"]


def test_underwriter_cannot_equal_insured(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)

    set_caller(INSURED_ADDRESS)
    set_value(kwargs["coverage_amount"])
    with pytest.raises(Exception, match="must be a different address"):
        registry.fund_as_underwriter(policy_id)
    clear_value()


def test_wrong_coverage_value_is_rejected(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)

    set_caller(UNDERWRITER_ADDRESS)
    set_value(kwargs["coverage_amount"] - 1)
    with pytest.raises(Exception, match="attached value must exactly equal coverage_amount"):
        registry.fund_as_underwriter(policy_id)
    clear_value()


def test_cancel_before_funding_deadline_reverts(contracts):
    registry = contracts["registry"]
    policy_id, _ = create_default_policy(registry)

    with pytest.raises(Exception, match="funding window has not expired yet"):
        registry.cancel_if_unfunded(policy_id)


def test_cancel_after_funding_deadline_refunds_insured(contracts, monkeypatch):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry, funding_window_days=1)

    future = now_utc() + datetime.timedelta(days=2)
    monkeypatch.setattr(type(registry), "_now_utc", lambda self: future)
    registry.cancel_if_unfunded(policy_id)

    policy = json.loads(registry.get_policy_json(policy_id))
    assert policy["state"] == "CANCELLED"
    assert Ledger.balance_of(INSURED_ADDRESS) == kwargs["premium_amount"]
    assert Ledger.balance_of("0x" + "01" * 20) == 0


def test_only_linked_oracle_can_mark_resolving(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)
    fund_default_underwriter(registry, policy_id, kwargs)

    set_caller(STRANGER_ADDRESS)
    with pytest.raises(Exception, match="only the linked DeliveryOracle"):
        registry.mark_resolving(policy_id)


def test_only_linked_vault_can_mark_settled(contracts):
    registry = contracts["registry"]
    policy_id, kwargs = create_default_policy(registry)

    set_caller(STRANGER_ADDRESS)
    with pytest.raises(Exception, match="only the linked SettlementVault"):
        registry.mark_settled(policy_id, "DELAYED")


def test_set_linked_contracts_is_one_time_and_owner_only(contracts):
    registry = contracts["registry"]
    set_caller(STRANGER_ADDRESS)
    with pytest.raises(Exception, match="only the deploying owner"):
        registry.set_linked_contracts("0x" + "09" * 20, "0x" + "0a" * 20)
