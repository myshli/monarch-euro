"""Tests for the Wise source.

Wise reports amounts differently from the Berlin Group shape Enable Banking
uses, so the sign and identity handling needs its own coverage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from monarch_euro.config import ConfigError, _parse_wise_accounts
from monarch_euro.sources.wise import WiseClient, WiseError, WiseSCARequired


def normalize(raw: dict, currency: str = "EUR"):
    return WiseClient._normalize("wise-eur", "wise:1:2:EUR", raw, currency)


def card_purchase(**overrides) -> dict:
    raw = {
        "type": "DEBIT",
        "date": "2026-09-18T10:31:00.000Z",
        "amount": {"value": -42.00, "currency": "EUR"},
        "referenceNumber": "CARD-90210",
        "details": {
            "type": "CARD",
            "description": "Card transaction of 42.00 EUR issued by Rewe",
            "merchant": {"name": "REWE"},
        },
    }
    raw.update(overrides)
    return raw


# -- sign handling ---------------------------------------------------------

def test_debit_is_negative():
    assert normalize(card_purchase()).amount == Decimal("-42.00")


def test_credit_is_positive():
    raw = {
        "type": "CREDIT",
        "date": "2026-09-01T09:00:00.000Z",
        "amount": {"value": 2400.00, "currency": "EUR"},
        "referenceNumber": "TRANSFER-1",
        "details": {"description": "Invoice 118", "senderName": "ACME GmbH"},
    }
    txn = normalize(raw)
    assert txn.amount == Decimal("2400.00")
    assert txn.counterparty == "ACME GmbH"


def test_debit_with_unsigned_value_is_corrected():
    """Fee rows arrive as DEBIT with a positive value; the type must win."""
    txn = normalize(card_purchase(amount={"value": 1.25, "currency": "EUR"}))
    assert txn.amount == Decimal("-1.25")


def test_credit_with_negative_value_is_corrected():
    raw = card_purchase(type="CREDIT", amount={"value": -10.00, "currency": "EUR"})
    assert normalize(raw).amount == Decimal("10.00")


# -- currency --------------------------------------------------------------

def test_currency_comes_from_the_amount_block():
    txn = normalize(card_purchase(amount={"value": -20.00, "currency": "usd"}), currency="EUR")
    assert txn.currency == "USD"


def test_currency_falls_back_to_the_balance_currency():
    raw = card_purchase(amount={"value": -20.00})
    assert normalize(raw, currency="usd").currency == "USD"


# -- identity --------------------------------------------------------------

def test_reference_number_becomes_the_dedupe_key():
    txn = normalize(card_purchase())
    assert txn.dedupe_key() == "wise:1:2:EUR:ref:CARD-90210"


def test_rows_without_a_reference_still_get_a_stable_key():
    raw = card_purchase()
    del raw["referenceNumber"]
    first, second = normalize(raw), normalize(raw)
    assert first.dedupe_key() == second.dedupe_key()
    assert ":hash:" in first.dedupe_key()


def test_account_uid_separates_currencies_on_one_profile():
    """A EUR and a USD balance must never collide in the dedupe ledger."""
    eur = WiseClient._normalize("w", "wise:1:2:EUR", card_purchase(), "EUR")
    usd = WiseClient._normalize("w", "wise:1:3:USD", card_purchase(), "USD")
    assert eur.dedupe_key() != usd.dedupe_key()


# -- merchant extraction ---------------------------------------------------

def test_merchant_name_is_preferred_over_description():
    assert normalize(card_purchase()).counterparty == "REWE"


def test_description_is_used_when_no_counterparty_exists():
    raw = card_purchase(details={"description": "Balance cashback"})
    txn = normalize(raw)
    assert txn.counterparty is None
    assert txn.description == "Balance cashback"


# -- malformed rows --------------------------------------------------------

def test_row_without_amount_is_skipped():
    raw = card_purchase()
    del raw["amount"]
    assert normalize(raw) is None


def test_row_without_date_is_skipped():
    raw = card_purchase()
    del raw["date"]
    assert normalize(raw) is None


def test_unparseable_date_is_skipped():
    assert normalize(card_purchase(date="not-a-date")) is None


def test_parses_wise_zulu_timestamps():
    assert normalize(card_purchase()).booked_on == date(2026, 9, 18)


# -- statement guard -------------------------------------------------------

def test_statement_window_wider_than_wise_allows_is_rejected():
    client = WiseClient(token="x")
    with pytest.raises(WiseError, match="469 days"):
        client.statement(1, 2, "EUR", date(2024, 1, 1), date(2026, 1, 1))


def test_sca_without_a_key_raises_a_clear_error():
    client = WiseClient(token="x", private_key_path=None)
    with pytest.raises(WiseSCARequired, match="Manage public keys"):
        client._sign("one-time-token")


# -- config ----------------------------------------------------------------

def test_wise_accounts_parse(monkeypatch):
    monkeypatch.setenv("WISE_ACCOUNTS", "EUR|Wise EUR;USD|Wise USD")
    accounts = _parse_wise_accounts()
    assert [(a.currency, a.monarch_account_name) for a in accounts] == [
        ("EUR", "Wise EUR"),
        ("USD", "Wise USD"),
    ]


def test_duplicate_wise_currencies_are_rejected(monkeypatch):
    monkeypatch.setenv("WISE_ACCOUNTS", "EUR|A;EUR|B")
    with pytest.raises(ConfigError, match="Duplicate"):
        _parse_wise_accounts()


def test_malformed_wise_accounts_are_rejected(monkeypatch):
    monkeypatch.setenv("WISE_ACCOUNTS", "EUR")
    with pytest.raises(ConfigError, match="Malformed"):
        _parse_wise_accounts()
