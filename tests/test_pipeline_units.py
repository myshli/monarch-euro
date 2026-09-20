"""Unit tests for the parts that must be right for the ledger to stay correct."""

from __future__ import annotations

import types
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


# -- occurrence disambiguation ---------------------------------------------

def test_identical_same_day_transactions_get_distinct_keys():
    """N26 leaves entry_reference null, so the hash must not collapse repeats.

    Two identical coffees on one day are two transactions, not one.
    """
    from monarch_euro.sources.enablebanking import assign_occurrences

    rows = assign_occurrences([make_txn(reference=None) for _ in range(3)])
    assert len({t.dedupe_key() for t in rows}) == 3


def test_occurrence_ordinals_are_stable_across_refetches():
    from monarch_euro.sources.enablebanking import assign_occurrences

    first = [t.dedupe_key() for t in assign_occurrences([make_txn() for _ in range(3)])]
    second = [t.dedupe_key() for t in assign_occurrences([make_txn() for _ in range(3)])]
    assert first == second


def test_rows_with_a_reference_are_left_alone():
    from monarch_euro.sources.enablebanking import assign_occurrences

    rows = assign_occurrences([make_txn(reference="R1"), make_txn(reference="R2")])
    assert [t.occurrence for t in rows] == [0, 0]
    assert rows[0].dedupe_key() == "acct-1:ref:R1"


def test_different_transactions_do_not_share_an_ordinal():
    from monarch_euro.sources.enablebanking import assign_occurrences

    rows = assign_occurrences(
        [make_txn(counterparty="REWE"), make_txn(counterparty="EDEKA"), make_txn(counterparty="REWE")]
    )
    assert [t.occurrence for t in rows] == [0, 0, 1]


# -- transfer and code rules -----------------------------------------------

def test_incoming_top_up_is_a_transfer_not_income():
    """Money moved in from another tracked account must not count as income."""
    cat = Categorizer(load_rules(None))
    _, category = cat.apply("Thank you for adding funds", None)
    assert category == "Transfer"


def test_top_up_fee_is_a_fee_not_a_transfer():
    cat = Categorizer(load_rules(None))
    _, category = cat.apply("N26 account instant top-up fee", None, "PMNT/MDOP/FEES")
    assert category == "Financial & Legal Services"


def test_iso_fee_code_categorizes_without_matching_text():
    cat = Categorizer(load_rules(None))
    _, category = cat.apply("irgendein unbekannter text", None, "PMNT/MDOP/FEES")
    assert category == "Financial & Legal Services"


def test_code_rule_does_not_fire_without_the_code():
    cat = Categorizer(load_rules(None))
    _, category = cat.apply("irgendein unbekannter text", None, None)
    assert category is None


def test_transaction_code_flattens_berlin_group_block():
    from monarch_euro.categorize import transaction_code

    raw = {"bank_transaction_code": {"description": "PMNT", "code": "MDOP", "sub_code": "FEES"}}
    assert transaction_code(raw) == "PMNT/MDOP/FEES"
    assert transaction_code({}) is None


# -- rule/category cross-check ---------------------------------------------

def test_unknown_rule_categories_are_detected():
    """A rule naming a missing category matches but assigns nothing useful."""
    cat = Categorizer(load_rules(None))
    available = {"Groceries", "Travel"}
    unknown = cat.unknown_categories(available)
    assert "Groceries" not in unknown
    assert "Transfer" in unknown


def test_no_unknown_categories_when_all_exist():
    cat = Categorizer(load_rules(None))
    assert cat.unknown_categories(cat.referenced_categories()) == set()


# -- notifications ---------------------------------------------------------

def test_notify_is_skipped_when_unconfigured():
    from monarch_euro.notify import send

    assert send("", "", "hi") is False


def test_failure_message_names_the_fix_for_a_lapsed_consent():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["[n26] consent expired: re-run link"], 0, 0)
    assert "monarch-euro link n26" in msg


def test_failure_message_names_the_fix_for_an_expired_session():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["401 Authentication credentials were not provided"], 0, 0)
    assert "session expired" in msg.lower()


def test_rate_limit_message_says_no_action_needed():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["the bank's PSD2 rate limit is exhausted"], 0, 0)
    assert "no action needed" in msg


def test_html_in_bank_errors_is_escaped():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["<script>bad</script>"], 0, 0)
    assert "<script>" not in msg and "&lt;script&gt;" in msg


# -- env file integrity ----------------------------------------------------

def test_duplicate_env_keys_are_rejected(tmp_path):
    """A duplicated key means a damaged file; taking one value silently
    produces a failure that points somewhere else entirely."""
    from monarch_euro.config import ConfigError, _load_dotenv

    env = tmp_path / ".env"
    env.write_text("MONARCH_COOKIE_HEADER=\nMONARCH_COOKIE_HEADER=abc\n")
    with pytest.raises(ConfigError, match="more than once"):
        _load_dotenv(env)


def test_normal_env_file_loads(tmp_path, monkeypatch):
    from monarch_euro.config import _load_dotenv

    env = tmp_path / ".env"
    env.write_text("# comment\nFOO_X=1\nBAR_X=two\n\n")
    monkeypatch.delenv("FOO_X", raising=False)
    _load_dotenv(env)
    import os
    assert os.environ["FOO_X"] == "1"


def test_configured_bank_without_a_session_is_reported(tmp_path, monkeypatch):
    """The sync loop iterates sessions, so an unlinked bank would otherwise
    never appear and the run would report success having fetched nothing."""
    from monarch_euro import pipeline

    cfg = types.SimpleNamespace(
        state_dir=tmp_path, db_path=tmp_path / "s.sqlite3",
        links=[types.SimpleNamespace(key="n26", aspsp_name="N26", aspsp_country="DE",
                                     monarch_account_name="N26 Checking")],
        wise_accounts=[], wise_token="", lookback_days=30, target_currency="USD",
        fx_base_url="", monarch_email="", monarch_password="", monarch_mfa_secret="",
        monarch_token="", monarch_session_cookie="", monarch_csrf_token="",
        monarch_cookie_name="session_id", monarch_cookie_header="",
        monarch_session_path=tmp_path / "s.pickle", dry_run=True,
        eb_application_id="x", eb_private_key_path=tmp_path / "k",
        eb_redirect_url="", eb_base_url="", note_original_amount=True,
        include_pending=False, telegram_bot_token="", telegram_chat_id="",
        notify_on_success=False,
    )
    result = pipeline.sync(cfg)
    assert any("not linked on this machine" in e for e in result.errors)


# -- cookie hygiene --------------------------------------------------------

def test_short_lived_and_analytics_cookies_are_dropped():
    """__cf_bm lives ~30 minutes, so storing it means keeping a value that is
    stale almost immediately. Monarch accepts the request without it."""
    from monarch_euro.sinks.monarch_compat import clean_cookie_header

    raw = ("ajs_anonymous_id=abc; session_id=THESESSION; csrftoken=THECSRF; "
           "__stripe_mid=x; cf_clearance=cfc; __cf_bm=short; _dd_s=y")
    cleaned = clean_cookie_header(raw)
    assert "session_id=THESESSION" in cleaned
    assert "csrftoken=THECSRF" in cleaned
    assert "cf_clearance=cfc" in cleaned
    assert "__cf_bm" not in cleaned
    assert "ajs_anonymous_id" not in cleaned
    assert "_dd_s" not in cleaned


def test_unrecognised_cookie_header_is_left_alone():
    """Better to send something unexpected than nothing at all."""
    from monarch_euro.sinks.monarch_compat import clean_cookie_header

    assert clean_cookie_header("weird=value") == "weird=value"


def test_csrf_failure_gets_its_own_hint():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["403 CSRF Failed: Referer checking failed"], 0, 0)
    assert "CSRF" in msg and "no longer matches" in msg
