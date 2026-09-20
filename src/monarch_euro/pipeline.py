"""Sync orchestration: Enable Banking -> FX -> Monarch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .categorize import Categorizer, load_rules
from .config import Config
from .fx import FxConverter
from .models import SourceTransaction
from .sinks.monarch import MonarchSink
from .sources.enablebanking import (
    ConsentExpiredError,
    EnableBankingClient,
    extract_accounts,
)
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    fetched: int = 0
    posted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"fetched={self.fetched}",
            f"posted={self.posted}",
            f"skipped(dupe)={self.skipped}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def _rules_path(config: Config) -> Path:
    return config.state_dir / "rules.json"


def refresh_accounts(
    client: EnableBankingClient, store: Store, link_key: str, session_id: str
) -> None:
    """Re-read the account list for a session and persist it."""
    payload = client.get_session(session_id)
    for account in extract_accounts(payload):
        store.put_account(
            account_uid=account["uid"],
            link_key=link_key,
            identifier=account.get("identifier"),
            name=account.get("name"),
            currency=account.get("currency"),
            monarch_account_id=None,
            monarch_account_name=None,
        )


def sync(config: Config) -> SyncResult:
    result = SyncResult()
    date_from = date.today() - timedelta(days=config.lookback_days)

    rules = load_rules(_rules_path(config))
    categorizer = Categorizer(rules)

    with Store(config.db_path) as store:
        run_id = store.start_run()
        sessions = store.all_sessions()
        if not sessions:
            message = (
                "No bank sessions found. Run `monarch-euro link <key>` for each "
                "entry in ACCOUNT_LINKS before syncing."
            )
            log.error(message)
            result.errors.append(message)
            store.finish_run(run_id, "error", 0, 0, 0, message)
            return result

        links = {link.key: link for link in config.links}

        with EnableBankingClient(
            application_id=config.eb_application_id,
            private_key_path=config.eb_private_key_path,
            redirect_url=config.eb_redirect_url,
            base_url=config.eb_base_url,
        ) as client, FxConverter(
            store=store,
            target_currency=config.target_currency,
            base_url=config.fx_base_url,
        ) as fx, MonarchSink(
            email=config.monarch_email,
            password=config.monarch_password,
            mfa_secret=config.monarch_mfa_secret,
            session_path=config.monarch_session_path,
            dry_run=config.dry_run,
        ) as monarch:

            monarch.refresh_metadata()

            for session_row in sessions:
                link_key = session_row["link_key"]
                link = links.get(link_key)
                if link is None:
                    log.warning(
                        "Session %r has no matching ACCOUNT_LINKS entry; skipping.", link_key
                    )
                    continue

                try:
                    _sync_one_link(
                        config=config,
                        store=store,
                        client=client,
                        fx=fx,
                        monarch=monarch,
                        categorizer=categorizer,
                        link_key=link_key,
                        session_id=session_row["session_id"],
                        monarch_account_name=link.monarch_account_name,
                        date_from=date_from,
                        result=result,
                    )
                except ConsentExpiredError as exc:
                    message = f"[{link_key}] consent expired: {exc}"
                    log.error(message)
                    result.errors.append(message)
                except Exception as exc:  # keep other banks syncing
                    message = f"[{link_key}] sync failed: {exc}"
                    log.exception(message)
                    result.errors.append(message)

        status = "ok" if result.ok else "error"
        store.finish_run(
            run_id,
            status,
            result.fetched,
            result.posted,
            result.skipped,
            "; ".join(result.errors) or None,
        )

    return result


def _sync_one_link(
    *,
    config: Config,
    store: Store,
    client: EnableBankingClient,
    fx: FxConverter,
    monarch: MonarchSink,
    categorizer: Categorizer,
    link_key: str,
    session_id: str,
    monarch_account_name: str,
    date_from: date,
    result: SyncResult,
) -> None:
    refresh_accounts(client, store, link_key, session_id)
    accounts = store.accounts_for(link_key)
    if not accounts:
        log.warning("[%s] session returned no accounts", link_key)
        return

    monarch_account_id = monarch.ensure_account(monarch_account_name)

    for account in accounts:
        account_uid = account["account_uid"]
        store.set_monarch_account(account_uid, monarch_account_id, monarch_account_name)

        transactions: list[SourceTransaction] = client.transactions(
            account_key=link_key,
            account_uid=account_uid,
            date_from=date_from,
            include_pending=config.include_pending,
        )
        result.fetched += len(transactions)
        if not transactions:
            continue

        keys = [txn.dedupe_key() for txn in transactions]
        new_keys = store.filter_new(keys)
        fresh = [txn for txn, key in zip(transactions, keys) if key in new_keys]
        result.skipped += len(transactions) - len(fresh)

        if not fresh:
            log.info("[%s/%s] nothing new", link_key, account_uid[:8])
            continue

        fx.prefetch(fresh)
        log.info("[%s/%s] posting %d new transactions", link_key, account_uid[:8], len(fresh))

        for txn in fresh:
            converted = fx.convert(txn)
            merchant, category = categorizer.apply(txn.description, txn.counterparty)
            note = converted.note(config.note_original_amount)

            monarch_txn_id = monarch.post(
                txn=converted,
                account_id=monarch_account_id,
                merchant=merchant,
                category=category,
                note=note,
            )

            if not config.dry_run:
                store.mark_posted(
                    dedupe_key=txn.dedupe_key(),
                    account_uid=account_uid,
                    monarch_txn_id=monarch_txn_id,
                    booked_on=txn.booked_on,
                    amount=converted.amount,
                    currency=converted.currency,
                )
            result.posted += 1
