"""SQLite-backed state: bank sessions, account mapping, dedupe ledger, FX cache.

The dedupe ledger is what makes the sync safe to run on a timer. Every run
re-fetches an overlapping window from the bank (banks revise and late-post
transactions), and the ledger guarantees each one reaches Monarch exactly
once, no matter how many times it appears in a fetch.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    link_key      TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    aspsp_name    TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    valid_until   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_uid           TEXT PRIMARY KEY,
    link_key              TEXT NOT NULL,
    identifier            TEXT,
    name                  TEXT,
    currency              TEXT,
    monarch_account_id    TEXT,
    monarch_account_name  TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posted (
    dedupe_key      TEXT PRIMARY KEY,
    account_uid     TEXT NOT NULL,
    monarch_txn_id  TEXT,
    booked_on       TEXT NOT NULL,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    posted_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posted_account_date ON posted (account_uid, booked_on);

CREATE TABLE IF NOT EXISTS fx_rates (
    base            TEXT NOT NULL,
    quote           TEXT NOT NULL,
    requested_date  TEXT NOT NULL,
    rate            TEXT NOT NULL,
    rate_date       TEXT NOT NULL,
    PRIMARY KEY (base, quote, requested_date)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    posted      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- sessions ----------------------------------------------------------

    def put_session(
        self,
        link_key: str,
        session_id: str,
        aspsp_name: str,
        aspsp_country: str,
        valid_until: str | None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (link_key, session_id, aspsp_name, aspsp_country, valid_until, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(link_key) DO UPDATE SET
                     session_id=excluded.session_id,
                     aspsp_name=excluded.aspsp_name,
                     aspsp_country=excluded.aspsp_country,
                     valid_until=excluded.valid_until,
                     created_at=excluded.created_at""",
                (link_key, session_id, aspsp_name, aspsp_country, valid_until, _now()),
            )

    def get_session(self, link_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE link_key = ?", (link_key,)
        ).fetchone()

    def all_sessions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM sessions ORDER BY link_key").fetchall())

    def delete_session(self, link_key: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE link_key = ?", (link_key,))

    # -- accounts ----------------------------------------------------------

    def put_account(
        self,
        account_uid: str,
        link_key: str,
        identifier: str | None,
        name: str | None,
        currency: str | None,
        monarch_account_id: str | None,
        monarch_account_name: str | None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO accounts
                   (account_uid, link_key, identifier, name, currency,
                    monarch_account_id, monarch_account_name, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_uid) DO UPDATE SET
                     link_key=excluded.link_key,
                     identifier=excluded.identifier,
                     name=excluded.name,
                     currency=excluded.currency,
                     monarch_account_id=COALESCE(excluded.monarch_account_id, accounts.monarch_account_id),
                     monarch_account_name=COALESCE(excluded.monarch_account_name, accounts.monarch_account_name),
                     updated_at=excluded.updated_at""",
                (
                    account_uid,
                    link_key,
                    identifier,
                    name,
                    currency,
                    monarch_account_id,
                    monarch_account_name,
                    _now(),
                ),
            )

    def accounts_for(self, link_key: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM accounts WHERE link_key = ? ORDER BY account_uid", (link_key,)
            ).fetchall()
        )

    def all_accounts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM accounts ORDER BY link_key").fetchall())

    def set_monarch_account(self, account_uid: str, monarch_id: str, monarch_name: str) -> None:
        with self.tx() as conn:
            conn.execute(
                """UPDATE accounts
                   SET monarch_account_id = ?, monarch_account_name = ?, updated_at = ?
                   WHERE account_uid = ?""",
                (monarch_id, monarch_name, _now(), account_uid),
            )

    # -- dedupe ledger -----------------------------------------------------

    def already_posted(self, dedupe_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posted WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row is not None

    def filter_new(self, dedupe_keys: list[str]) -> set[str]:
        """Return the subset of keys not yet posted, in one query."""
        if not dedupe_keys:
            return set()
        known: set[str] = set()
        chunk = 500
        for i in range(0, len(dedupe_keys), chunk):
            batch = dedupe_keys[i : i + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT dedupe_key FROM posted WHERE dedupe_key IN ({placeholders})", batch
            ).fetchall()
            known.update(r["dedupe_key"] for r in rows)
        return set(dedupe_keys) - known

    def mark_posted(
        self,
        dedupe_key: str,
        account_uid: str,
        monarch_txn_id: str | None,
        booked_on: date,
        amount: Decimal,
        currency: str,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO posted
                   (dedupe_key, account_uid, monarch_txn_id, booked_on, amount, currency, posted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedupe_key,
                    account_uid,
                    monarch_txn_id,
                    booked_on.isoformat(),
                    f"{amount:.2f}",
                    currency,
                    _now(),
                ),
            )

    def posted_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM posted").fetchone()["c"])

    # -- fx cache ----------------------------------------------------------

    def get_fx_rate(self, base: str, quote: str, on: date) -> tuple[Decimal, date] | None:
        row = self.conn.execute(
            """SELECT rate, rate_date FROM fx_rates
               WHERE base = ? AND quote = ? AND requested_date = ?""",
            (base, quote, on.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return Decimal(row["rate"]), date.fromisoformat(row["rate_date"])

    def put_fx_rate(
        self, base: str, quote: str, requested_date: date, rate: Decimal, rate_date: date
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO fx_rates
                   (base, quote, requested_date, rate, rate_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (base, quote, requested_date.isoformat(), str(rate), rate_date.isoformat()),
            )

    # -- run log -----------------------------------------------------------

    def start_run(self) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (_now(),)
            )
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, status: str, fetched: int, posted: int, skipped: int, error: str | None
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """UPDATE runs SET finished_at = ?, status = ?, fetched = ?,
                   posted = ?, skipped = ?, error = ? WHERE id = ?""",
                (_now(), status, fetched, posted, skipped, error, run_id),
            )

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )
