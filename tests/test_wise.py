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


# -- regressions found against live data -----------------------------------

def test_recipient_object_does_not_become_a_stringified_dict():
    """details.recipient is an object; reading it blindly produced
    "{'name': ' Berlin Metropolitan School gG" as the merchant name."""
    raw = {
        "type": "DEBIT",
        "date": "2026-09-20T07:58:02.361939Z",
        "amount": {"value": -400.0, "currency": "EUR"},
        "referenceNumber": "TRANSFER-2381871428",
        "details": {
            "type": "TRANSFER",
            "description": "Sent money to  Berlin Metropolitan School gGmbH",
            "recipient": {"name": " Berlin Metropolitan School gGmbH",
                          "bankAccount": "DE37 1009 0000 7274 7170 03"},
        },
    }
    txn = normalize(raw)
    assert txn.counterparty == "Berlin Metropolitan School gGmbH"
    assert "{" not in txn.counterparty


def test_money_added_is_a_transfer_whatever_the_wording():
    """"Topped up account" defeats a /top[- ]?up/ text match, so the
    structured details.type must carry it."""
    from monarch_euro.categorize import Categorizer, load_rules, transaction_code

    raw = {
        "type": "CREDIT",
        "date": "2026-09-20T07:55:41.094832Z",
        "amount": {"value": 1000.0, "currency": "EUR"},
        "referenceNumber": "TRANSFER-2381869102",
        "details": {"type": "MONEY_ADDED", "description": "Topped up account"},
    }
    txn = normalize(raw)
    cat = Categorizer(load_rules(None))
    _, category = cat.apply(txn.description, txn.counterparty, transaction_code(raw))
    assert category == "Transfer"


def test_exchange_details_supply_an_exact_target_amount():
    """A EUR top-up funded from USD knows the real USD figure; an ECB daily
    average is only an approximation of a rate that actually executed."""
    raw = {
        "type": "CREDIT",
        "date": "2026-09-20T07:55:41.094832Z",
        "amount": {"value": 1000.0, "currency": "EUR"},
        "referenceNumber": "T-1",
        "details": {"type": "MONEY_ADDED", "description": "Topped up account"},
        "exchangeDetails": {
            "toAmount": {"value": 1000.0, "currency": "EUR"},
            "fromAmount": {"value": 1148.4, "currency": "USD"},
            "rate": 0.87078,
        },
    }
    txn = normalize(raw)
    assert txn.exact_amount == Decimal("1148.4")
    assert txn.exact_currency == "USD"


def test_exact_amount_follows_the_transaction_sign():
    raw = {
        "type": "DEBIT",
        "date": "2026-09-20T07:55:41.094832Z",
        "amount": {"value": -1000.0, "currency": "EUR"},
        "referenceNumber": "T-2",
        "details": {"type": "TRANSFER"},
        "exchangeDetails": {
            "toAmount": {"value": 1000.0, "currency": "EUR"},
            "fromAmount": {"value": 1148.4, "currency": "USD"},
        },
    }
    assert normalize(raw).exact_amount == Decimal("-1148.4")


def test_rows_without_exchange_details_carry_no_exact_amount():
    assert normalize(card_purchase()).exact_amount is None


def test_fx_prefers_the_executed_rate_over_the_ecb_average(tmp_path):
    from monarch_euro.fx import FxConverter
    from monarch_euro.store import Store

    raw = {
        "type": "CREDIT",
        "date": "2026-09-20T07:55:41.094832Z",
        "amount": {"value": 1000.0, "currency": "EUR"},
        "referenceNumber": "T-3",
        "details": {"type": "MONEY_ADDED"},
        "exchangeDetails": {
            "toAmount": {"value": 1000.0, "currency": "EUR"},
            "fromAmount": {"value": 1148.4, "currency": "USD"},
        },
    }
    txn = normalize(raw)
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_fx_rate("EUR", "USD", date(2026, 9, 20), Decimal("1.146"), date(2026, 9, 18))
        converted = FxConverter(store=store, target_currency="USD").convert(txn)

    assert converted.amount == Decimal("1148.40")   # not 1146.00
    assert converted.exact is True
    assert "Wise" in converted.note(include_original=True)
