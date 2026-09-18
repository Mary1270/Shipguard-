import datetime
import json

import pytest

from _bootstrap import (
    deploy_all,
    set_caller,
    now_utc,
    iso,
    create_default_policy,
    fund_default_underwriter,
    mock_two_sources_agree,
    INSURED_ADDRESS,
    UNDERWRITER_ADDRESS,
    STRANGER_ADDRESS,
)


@pytest.fixture
def rig():
    contracts = deploy_all()
    registry, oracle = contracts["registry"], contracts["oracle"]
    policy_id, kwargs = create_default_policy(
        registry, waiting_period_days=1, evidence_buffer_days=1
    )
    fund_default_underwriter(registry, policy_id, kwargs)
    return {**contracts, "policy_id": policy_id, "kwargs": kwargs}


def _freeze(monkeypatch, registry, oracle, when: datetime.datetime) -> None:
    monkeypatch.setattr(type(registry), "_now_utc", lambda self: when)
    monkeypatch.setattr(type(oracle), "_now_utc", lambda self: when)


def test_request_resolution_before_evidence_lock_time_reverts(rig):
    with pytest.raises(Exception, match="evidence lock time has not been reached yet"):
        rig["oracle"].request_resolution(rig["policy_id"])


def test_two_sources_agree_delayed_and_forwards_escrow_to_vault(rig, monkeypatch):
    registry, oracle, vault = rig["registry"], rig["oracle"], rig["vault"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "DELAYED")

    oracle.request_resolution(rig["policy_id"])

    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["decision"] == "DELAYED"
    assert resolution["is_final"] is False

    policy = json.loads(registry.get_policy_json(rig["policy_id"]))
    assert policy["state"] == "RESOLVING"

    from genlayer import Ledger

    total = rig["kwargs"]["premium_amount"] + rig["kwargs"]["coverage_amount"]
    assert Ledger.balance_of("0x" + "03" * 20) == total  # VAULT_ADDRESS


def test_single_source_agreement_is_inconclusive(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)

    from genlayer import _Nondet, _NondetWeb

    monkeypatch.setattr(_NondetWeb, "render", staticmethod(lambda url, mode="text": "page"))
    calls = {"n": 0}

    def alternating(prompt, response_format="text"):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            return '{"delivered": true, "verdict": "DELAYED", "detail": "mocked"}'
        return '{"delivered": false, "verdict": "UNKNOWN", "detail": "mocked"}'

    monkeypatch.setattr(_Nondet, "exec_prompt", staticmethod(alternating))

    oracle.request_resolution(rig["policy_id"])
    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["decision"] == "INCONCLUSIVE"


def test_challenge_requires_being_a_party(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "ON_TIME")
    oracle.request_resolution(rig["policy_id"])

    set_caller(STRANGER_ADDRESS)
    with pytest.raises(Exception, match="only the insured or underwriter"):
        oracle.challenge_resolution(rig["policy_id"], "https://extra.example.com/A")


def test_challenge_bounded_to_once_per_side(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "ON_TIME")
    oracle.request_resolution(rig["policy_id"])

    set_caller(INSURED_ADDRESS)
    oracle.challenge_resolution(rig["policy_id"], "https://extra.example.com/A")

    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["version"] == 2

    set_caller(INSURED_ADDRESS)
    with pytest.raises(Exception, match="insured has already used their one challenge"):
        oracle.challenge_resolution(rig["policy_id"], "https://extra2.example.com/A")

    # The underwriter still has their own, independent challenge available.
    set_caller(UNDERWRITER_ADDRESS)
    oracle.challenge_resolution(rig["policy_id"], "https://extra3.example.com/A")
    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["version"] == 3


def test_finalize_before_challenge_window_closes_reverts(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "ON_TIME")
    oracle.request_resolution(rig["policy_id"])

    with pytest.raises(Exception, match="challenge window is still open"):
        oracle.finalize_resolution(rig["policy_id"])


def test_finalize_after_challenge_window_marks_final(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    unlock = now_utc() + datetime.timedelta(days=15)
    _freeze(monkeypatch, registry, oracle, unlock)
    mock_two_sources_agree(monkeypatch, "ON_TIME")
    oracle.request_resolution(rig["policy_id"])

    past_window = unlock + datetime.timedelta(hours=49)
    _freeze(monkeypatch, registry, oracle, past_window)
    oracle.finalize_resolution(rig["policy_id"])

    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["is_final"] is True


def test_past_final_deadline_forces_immediate_inconclusive(rig, monkeypatch):
    registry, oracle = rig["registry"], rig["oracle"]
    past_deadline = now_utc() + datetime.timedelta(days=45)  # declared_eta + 30d already gone
    _freeze(monkeypatch, registry, oracle, past_deadline)
    mock_two_sources_agree(monkeypatch, "DELAYED")

    oracle.request_resolution(rig["policy_id"])
    resolution = json.loads(oracle.get_resolution_json(rig["policy_id"]))
    assert resolution["decision"] == "INCONCLUSIVE"
    assert resolution["is_final"] is True  # no extra finalize_resolution call needed
