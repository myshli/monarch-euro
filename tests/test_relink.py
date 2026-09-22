"""Re-linking a bank must not re-import its transactions.

Enable Banking issues a new account uid with every session. Observed on this
deployment: the same N26 account was `0f7c8e65…` on one link and `78619676…`
on the next. Dedupe keys built on the uid would all change at the next consent
renewal, and the sync would post the whole lookback window a second time.
"""

from __future__ import annotations

import sqlite3
import ssl
from datetime import date
from decimal import Decimal

from monarch_euro.models import SourceTransaction
from monarch_euro.pipeline import ledger_account, with_stable_keys
from monarch_euro.sources.enablebanking import extract_accounts
from monarch_euro.store import Store

IDENTITY = "IBAN-HASH-" + "x" * 120   # shape of an identification_hash


def txn(uid: str, *, description="REWE", amount="-42.00", ref=None, day=18) -> SourceTransaction:
    return SourceTransaction(
        account_key="n26", account_uid=uid, booked_on=date(2026, 9, day),
        amount=Decimal(amount), currency="EUR", description=description,
        counterparty=description, reference=ref, pending=False, raw={},
    )


def account_row(store: Store, uid: str) -> sqlite3.Row:
    return next(r for r in store.accounts_for("n26") if r["account_uid"] == uid)


# -- keys --------------------------------------------------------------------

def test_stable_key_does_not_depend_on_the_uid():
    stable = ledger_account("n26", IDENTITY)
    a = SourceTransaction(**{**txn("uid-A").__dict__, "ledger_account": stable})
    b = SourceTransaction(**{**txn("uid-B").__dict__, "ledger_account": stable})
    assert a.dedupe_key() == b.dedupe_key()
    assert a.legacy_dedupe_key() != b.legacy_dedupe_key()


def test_legacy_key_is_exactly_the_old_formula():
    """Adoption depends on reproducing keys already in the ledger bit for bit."""
    plain = txn("uid-A")
    keyed = SourceTransaction(**{**plain.__dict__, "ledger_account": "n26:abc"})
    assert keyed.legacy_dedupe_key() == plain.dedupe_key()
    assert plain.dedupe_key().startswith("uid-A:hash:")


def test_reference_keys_are_stable_too():
    stable = ledger_account("n26", IDENTITY)
    a = SourceTransaction(**{**txn("uid-A", ref="R1").__dict__, "ledger_account": stable})
    b = SourceTransaction(**{**txn("uid-B", ref="R1").__dict__, "ledger_account": stable})
    assert a.dedupe_key() == b.dedupe_key() == f"{stable}:ref:R1"


def test_no_identity_means_no_stable_account():
    assert ledger_account("n26", None) is None
    assert ledger_account("n26", "") is None


# -- session payloads ----------------------------------------------------------

def test_identity_comes_from_accounts_data_for_a_refreshed_session():
    """GET /sessions returns bare uids, with identities in accounts_data."""
    payload = {
        "accounts": ["uid-B"],
        "accounts_data": [{"uid": "uid-B", "identification_hash": IDENTITY}],
    }
    assert extract_accounts(payload)[0]["identity"] == IDENTITY


def test_identity_comes_from_the_account_object_at_link_time():
    payload = {"accounts": [{"uid": "uid-A", "identification_hash": IDENTITY,
                             "account_id": {"iban": "DE00"}}]}
    account = extract_accounts(payload)[0]
    assert account["identity"] == IDENTITY
    assert account["identifier"] == "DE00"


# -- store ---------------------------------------------------------------------

def test_refresh_does_not_blank_out_what_the_link_recorded(tmp_path):
    """A refresh returns no IBAN; it used to overwrite the stored one with None."""
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_account("uid-A", "n26", "DE00", "Main", "EUR", None, "N26", IDENTITY)
        store.put_account("uid-A", "n26", None, None, None, None, None, None)
        row = account_row(store, "uid-A")
        assert (row["identifier"], row["currency"], row["identity"]) == ("DE00", "EUR", IDENTITY)


def test_stale_uids_are_dropped_on_relink(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_account("uid-A", "n26", None, None, None, None, None, IDENTITY)
        store.put_account("uid-B", "n26", None, None, None, None, None, IDENTITY)
        store.put_account("w-1", "wise-eur", None, None, None, None, None)
        assert store.retain_accounts("n26", {"uid-B"}) == 1
        assert [r["account_uid"] for r in store.accounts_for("n26")] == ["uid-B"]
        assert [r["account_uid"] for r in store.accounts_for("wise-eur")] == ["w-1"]


def test_retain_with_an_empty_session_keeps_everything(tmp_path):
    """An empty account list is more likely a bad response than a closed account."""
    with Store(tmp_path / "s.sqlite3") as store:
        store.put_account("uid-A", "n26", None, None, None, None, None)
        assert store.retain_accounts("n26", set()) == 0
        assert len(store.accounts_for("n26")) == 1


def test_migrate_key_moves_the_ledger_row(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.mark_posted("old", "uid-A", "m1", date(2026, 9, 18), Decimal("-1"), "USD")
        assert store.migrate_key("old", "new") is True
        assert store.filter_new(["old", "new"]) == {"old"}
        assert store.migrate_key("old", "new") is False


def test_migrate_key_collapses_onto_an_existing_new_key(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.mark_posted("old", "uid-A", "m1", date(2026, 9, 18), Decimal("-1"), "USD")
        store.mark_posted("new", "uid-A", "m1", date(2026, 9, 18), Decimal("-1"), "USD")
        store.migrate_key("old", "new")
        assert store.posted_count() == 1


def test_an_older_database_gains_the_identity_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE accounts (account_uid TEXT PRIMARY KEY, link_key TEXT NOT NULL,
                    identifier TEXT, name TEXT, currency TEXT, monarch_account_id TEXT,
                    monarch_account_name TEXT, updated_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO accounts VALUES ('uid-A','n26',NULL,NULL,NULL,NULL,'N26','t')")
    conn.commit()
    conn.close()
    with Store(path) as store:
        store.put_account("uid-A", "n26", None, None, None, None, None, IDENTITY)
        assert account_row(store, "uid-A")["identity"] == IDENTITY


# -- the scenario this all exists for -----------------------------------------

def test_relinking_does_not_reimport_anything(tmp_path):
    batch = [("REWE", "-42.00"), ("BVG", "-3.50"), ("REWE", "-42.00")]  # a same-day repeat

    def fetched(uid):
        from monarch_euro.sources.enablebanking import assign_occurrences
        return assign_occurrences([txn(uid, description=d, amount=a) for d, a in batch])

    with Store(tmp_path / "s.sqlite3") as store:
        # 1. The old version posted these under uid-keyed ledger rows.
        store.put_account("uid-A", "n26", None, None, None, None, "N26")
        for t in fetched("uid-A"):
            store.mark_posted(t.dedupe_key(), "uid-A", "m", t.booked_on, t.amount, "USD")

        # 2. First run of the new version, same session: rows are adopted.
        store.put_account("uid-A", "n26", None, None, None, None, None, IDENTITY)
        keyed = with_stable_keys(store, "n26", account_row(store, "uid-A"), fetched("uid-A"))
        assert store.filter_new([t.dedupe_key() for t in keyed]) == set()

        # 3. Consent renewed: new session, new uid, same account.
        store.put_account("uid-B", "n26", None, None, None, None, None, IDENTITY)
        store.retain_accounts("n26", {"uid-B"})
        keyed = with_stable_keys(store, "n26", account_row(store, "uid-B"), fetched("uid-B"))

        assert store.filter_new([t.dedupe_key() for t in keyed]) == set()
        assert store.posted_count() == 3


def test_without_an_identity_a_relink_would_reimport(tmp_path):
    """The failure mode, pinned so a regression is obvious."""
    with Store(tmp_path / "s.sqlite3") as store:
        for t in [txn("uid-A")]:
            store.mark_posted(t.dedupe_key(), "uid-A", "m", t.booked_on, t.amount, "USD")
        store.put_account("uid-B", "n26", None, None, None, None, None, None)
        keyed = with_stable_keys(store, "n26", account_row(store, "uid-B"), [txn("uid-B")])
        assert store.filter_new([t.dedupe_key() for t in keyed]) != set()


# -- TLS -----------------------------------------------------------------------

def test_monarch_graphql_transport_verifies_certificates():
    """gql 3.x defaults this transport to ssl=False, skipping verification."""
    from monarchmoney import MonarchMoney

    from monarch_euro.sinks.monarch_compat import patch_endpoints

    patch_endpoints()
    transport = MonarchMoney(token="t")._get_graphql_client().transport
    assert isinstance(transport.ssl, ssl.SSLContext)
    assert transport.ssl.verify_mode == ssl.CERT_REQUIRED
    assert transport.ssl.check_hostname is True


# -- alert hints -----------------------------------------------------------------

def test_wise_token_failure_is_not_reported_as_a_monarch_session():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["[wise] sync failed: GET /v1/profiles returned 401. "
                                 "The Wise personal token is invalid or expired"], 0, 0)
    assert "Wise token" in msg
    assert "Monarch session" not in msg


def test_monarch_session_hint_points_at_monarch_cookie():
    from monarch_euro.notify import format_failure

    msg = format_failure("vps", ["TransportServerError: 401, message='Unauthorized'"], 0, 0)
    assert "monarch-cookie" in msg
