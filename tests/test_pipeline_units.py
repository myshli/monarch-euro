"""Unit tests for the parts that must be right for the ledger to stay correct."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from monarch_euro.categorize import Categorizer, clean_merchant, load_rules
from monarch_euro.fx import FxConverter
from monarch_euro.models import SourceTransaction
from monarch_euro.sources.enablebanking import EnableBankingClient, extract_accounts
from monarch_euro.store import Store


def make_txn(**overrides) -> SourceTransaction:
    defaults = dict(
        account_key="n26",
        account_uid="acct-1",
        booked_on=date(2026, 9, 18),
        amount=Decimal("-42.00"),
        currency="EUR",
        description="REWE SAGT DANKE",
        counterparty="REWE",
        reference=None,
        pending=False,
        raw={},
    )
    defaults.update(overrides)
    return SourceTransaction(**defaults)


# -- dedupe identity -------------------------------------------------------

def test_entry_reference_wins_over_content_hash():
    txn = make_txn(reference="ENTRY-123")
    assert txn.dedupe_key() == "acct-1:ref:ENTRY-123"


def test_content_hash_is_stable_across_calls():
    txn = make_txn()
    assert txn.dedupe_key() == txn.dedupe_key()


def test_pending_flag_does_not_change_identity():
    """A transaction must keep its key when it settles, or it double-posts."""
    pending = make_txn(pending=True)
    booked = make_txn(pending=False)
    assert pending.dedupe_key() == booked.dedupe_key()


def test_different_amounts_produce_different_keys():
    assert make_txn().dedupe_key() != make_txn(amount=Decimal("-43.00")).dedupe_key()


def test_same_day_same_amount_different_merchant_differs():
    a = make_txn(counterparty="REWE")
    b = make_txn(counterparty="EDEKA")
    assert a.dedupe_key() != b.dedupe_key()


# -- sign normalization ----------------------------------------------------

def test_debit_indicator_makes_amount_negative():
    raw = {
        "entry_reference": "x1",
        "transaction_amount": {"amount": "42.00", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "booking_date": "2026-09-18",
        "creditor": {"name": "REWE"},
        "remittance_information": ["REWE SAGT DANKE"],
    }
    txn = EnableBankingClient._normalize("n26", "acct-1", raw)
    assert txn.amount == Decimal("-42.00")
    assert txn.counterparty == "REWE"


def test_credit_indicator_makes_amount_positive():
    raw = {
        "transaction_amount": {"amount": "1500.00", "currency": "EUR"},
        "credit_debit_indicator": "CRDT",
        "booking_date": "2026-09-01",
        "debtor": {"name": "ACME GmbH"},
        "remittance_information": ["Invoice 42"],
    }
    txn = EnableBankingClient._normalize("n26", "acct-1", raw)
    assert txn.amount == Decimal("1500.00")
    assert txn.counterparty == "ACME GmbH"


def test_negative_amount_without_indicator_is_preserved():
    raw = {
        "transaction_amount": {"amount": "-9.99", "currency": "EUR"},
        "booking_date": "2026-09-18",
        "remittance_information": ["SPOTIFY"],
    }
    txn = EnableBankingClient._normalize("n26", "acct-1", raw)
    assert txn.amount == Decimal("-9.99")


def test_missing_amount_is_skipped_not_crashed():
    assert EnableBankingClient._normalize("n26", "acct-1", {"booking_date": "2026-09-18"}) is None


def test_missing_date_is_skipped():
    raw = {"transaction_amount": {"amount": "1.00", "currency": "EUR"}}
    assert EnableBankingClient._normalize("n26", "acct-1", raw) is None


def test_falls_back_through_date_fields():
    raw = {
        "transaction_amount": {"amount": "5.00", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "value_date": "2026-09-17",
    }
    txn = EnableBankingClient._normalize("n26", "acct-1", raw)
    assert txn.booked_on == date(2026, 9, 17)


def test_pending_status_is_flagged():
    raw = {
        "transaction_amount": {"amount": "5.00", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "booking_date": "2026-09-18",
        "status": "PDNG",
    }
    assert EnableBankingClient._normalize("n26", "acct-1", raw).pending is True


def test_extract_accounts_handles_nested_account_id():
    payload = {"accounts": [{"uid": "u1", "account_id": {"iban": "DE89370400440532013000"},
                             "currency": "eur", "name": "Main"}]}
    accounts = extract_accounts(payload)
    assert accounts == [
        {"uid": "u1", "identifier": "DE89370400440532013000", "name": "Main", "currency": "EUR"}
    ]


# -- merchant cleanup ------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected_absent",
    [
        ("REWE SAGT DANKE 18.09.2026", "18.09.2026"),
        ("PAYPAL *SPOTIFY XXXX1234", "XXXX1234"),
        ("SEPA DE89370400440532013000 MIETE", "DE89370400440532013000"),
        ("EDEKA 14:32:01", "14:32"),
    ],
)
def test_noise_is_stripped(raw, expected_absent):
    assert expected_absent not in clean_merchant(raw)


def test_empty_description_becomes_unknown():
    assert clean_merchant("") == "Unknown"
    assert clean_merchant("   ---  ") == "Unknown"


def test_allcaps_is_title_cased():
    assert clean_merchant("DEUTSCHE BAHN AG") == "Deutsche Bahn Ag"


def test_short_acronyms_are_left_alone():
    assert clean_merchant("IKEA") == "IKEA"


# -- categorization --------------------------------------------------------

def test_default_rules_categorize_groceries():
    cat = Categorizer(load_rules(None))
    merchant, category = cat.apply("REWE SAGT DANKE", "REWE")
    assert category == "Groceries"
    assert merchant == "REWE"


def test_rule_can_override_merchant_name():
    cat = Categorizer(load_rules(None))
    merchant, category = cat.apply("GELDAUTOMAT BERLIN", "N26")
    assert merchant == "ATM Withdrawal"
    assert category == "Cash & ATM"


def test_unmatched_returns_no_category():
    cat = Categorizer(load_rules(None))
    merchant, category = cat.apply("SOME LOCAL SHOP", "SOME LOCAL SHOP")
    assert category is None
    assert merchant == "Some Local Shop"


# -- store -----------------------------------------------------------------

def test_filter_new_excludes_posted(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.mark_posted("k1", "acct-1", "m1", date(2026, 9, 18), Decimal("-42.00"), "USD")
        assert store.filter_new(["k1", "k2"]) == {"k2"}


def test_mark_posted_is_idempotent(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        for _ in range(3):
            store.mark_posted("k1", "acct-1", "m1", date(2026, 9, 18), Decimal("-42.00"), "USD")
        assert store.posted_count() == 1


def test_filter_new_handles_batches_over_sqlite_limit(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        keys = [f"k{i}" for i in range(1200)]
        for key in keys[:600]:
            store.mark_posted(key, "a", None, date(2026, 9, 18), Decimal("-1"), "USD")
        assert store.filter_new(keys) == set(keys[600:])


def test_run_log_roundtrip(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        run_id = store.start_run()
        store.finish_run(run_id, "ok", 10, 4, 6, None)
        latest = store.recent_runs(1)[0]
        assert (latest["status"], latest["posted"], latest["skipped"]) == ("ok", 4, 6)


# -- fx --------------------------------------------------------------------

class StubbedFx(FxConverter):
    """Exercises conversion maths without touching the network."""

    def _fetch_single(self, currency, on):
        raise AssertionError("cache should have satisfied this lookup")


def test_conversion_uses_cached_rate_and_rounds_to_cents(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_fx_rate("EUR", "USD", date(2026, 9, 18), Decimal("1.146"), date(2026, 9, 18))
        fx = StubbedFx(store=store, target_currency="USD")
        converted = fx.convert(make_txn(amount=Decimal("-42.00")))
    assert converted.amount == Decimal("-48.13")   # 42.00 * 1.146 = 48.132
    assert converted.fx_rate == Decimal("1.146")


def test_weekend_date_walks_back_to_last_published_rate(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_fx_rate("EUR", "USD", date(2026, 9, 18), Decimal("1.146"), date(2026, 9, 18))
        fx = StubbedFx(store=store, target_currency="USD")
        # 2026-09-20 is a Sunday; ECB published nothing.
        converted = fx.convert(make_txn(booked_on=date(2026, 9, 20)))
    assert converted.fx_rate_date == date(2026, 9, 18)


def test_same_currency_is_not_converted(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        fx = StubbedFx(store=store, target_currency="EUR")
        converted = fx.convert(make_txn(currency="EUR"))
    assert converted.fx_rate is None
    assert converted.converted is False
    assert converted.note(include_original=True) is None


def test_note_records_original_amount_and_rate(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_fx_rate("EUR", "USD", date(2026, 9, 18), Decimal("1.146"), date(2026, 9, 18))
        fx = StubbedFx(store=store, target_currency="USD")
        note = fx.convert(make_txn()).note(include_original=True)
    assert note == "EUR -42.00 @ 1.146 ECB 2026-09-18 = USD -48.13"
