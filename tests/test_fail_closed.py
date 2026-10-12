"""N15/M11/M12: the USSD secret fails closed, and production mode refuses to start
with missing, short or reused secrets (development and tests are unaffected)."""
import pytest
from fastapi import HTTPException

import escrow_core as mpesa
from tests.support import FakeRequest

GOOD = {"ESCROW_API_KEY": "a" * 40, "ADMIN_API_KEY": "b" * 40,
        "MPESA_CALLBACK_SECRET": "c" * 40, "USSD_CALLBACK_SECRET": "d" * 40}


# ---- verify_ussd_secret ----

def test_unset_secret_refuses_everything(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", None)
    assert mpesa.verify_ussd_secret("anything") is False
    assert mpesa.verify_ussd_secret(None) is False
    assert mpesa.verify_ussd_secret("") is False


def test_empty_secret_is_treated_as_unset(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", "")
    assert mpesa.verify_ussd_secret("") is False


def test_correct_secret_is_accepted_and_wrong_ones_are_not(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", "right-secret")
    assert mpesa.verify_ussd_secret("right-secret") is True
    assert mpesa.verify_ussd_secret("wrong-secret") is False
    assert mpesa.verify_ussd_secret(None) is False
    assert mpesa.verify_ussd_secret("") is False


def test_a_non_ascii_header_value_does_not_crash(monkeypatch):
    # secrets.compare_digest on str raises TypeError for non-ASCII input (M11)
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", "right-secret")
    assert mpesa.verify_ussd_secret("séecret-ünicode") is False


# ---- the handler ----

async def _ussd(secret_header):
    import escrow_main

    p = escrow_main.USSDRequest(sessionId="s-fc", phoneNumber="254700000001", text="")
    return await escrow_main.ussd_callback(FakeRequest(None), p, secret_header)


@pytest.mark.asyncio
async def test_handler_answers_503_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", None)
    with pytest.raises(HTTPException) as e:
        await _ussd("whatever")
    assert e.value.status_code == 503


@pytest.mark.asyncio
async def test_handler_answers_401_for_a_wrong_or_missing_secret(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", "right-secret")
    for bad in (None, "", "wrong"):
        with pytest.raises(HTTPException) as e:
            await _ussd(bad)
        assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_handler_serves_the_menu_with_the_right_secret(monkeypatch):
    monkeypatch.setattr(mpesa, "USSD_CALLBACK_SECRET", "right-secret")
    out = await _ussd("right-secret")
    assert out.startswith("CON Escrow")


# ---- production secret checks ----

def _check(auto_create, **overrides):
    import escrow_main

    values = dict(GOOD)
    values.update(overrides)
    escrow_main._validate_production_secrets(auto_create, values)


def test_production_accepts_strong_distinct_secrets():
    _check(False)


def test_development_mode_accepts_weak_keys():
    _check(True, ESCROW_API_KEY="x", ADMIN_API_KEY="x", MPESA_CALLBACK_SECRET=None, USSD_CALLBACK_SECRET=None)


@pytest.mark.parametrize("name", list(GOOD))
def test_production_refuses_a_missing_secret(name):
    with pytest.raises(RuntimeError) as e:
        _check(False, **{name: None})
    assert name in str(e.value) and "not set" in str(e.value)


@pytest.mark.parametrize("name", list(GOOD))
def test_production_refuses_a_short_secret(name):
    with pytest.raises(RuntimeError) as e:
        _check(False, **{name: "short"})
    assert name in str(e.value) and "shorter than" in str(e.value)


def test_production_refuses_the_same_value_for_two_secrets():
    with pytest.raises(RuntimeError) as e:
        _check(False, ADMIN_API_KEY=GOOD["ESCROW_API_KEY"])
    assert "must differ" in str(e.value)


def test_production_reports_every_problem_at_once():
    with pytest.raises(RuntimeError) as e:
        _check(False, ESCROW_API_KEY="short", ADMIN_API_KEY=None)
    assert "ESCROW_API_KEY" in str(e.value) and "ADMIN_API_KEY" in str(e.value)