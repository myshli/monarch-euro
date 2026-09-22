"""Sync orchestration: Enable Banking -> FX -> Monarch."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path

from .categorize import Categorizer, load_rules, transaction_code
from .config import Config
from .fx import FxConverter
from .models import ConvertedTransaction, SourceTransaction
from .sinks.monarch import MonarchSink
from .sources.enablebanking import (
    ApplicationNotActiveError,
    AspspRateLimitedError,
    ConsentExpiredError,
    EnableBankingClient,
    extract_accounts,
)
from .sources.wise import WiseClient, WiseError
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
    accounts = extract_accounts(payload)
    for account in accounts:
        store.put_account(
            account_uid=account["uid"],
            link_key=link_key,
            identifier=account.get("identifier"),
            name=account.get("name"),
            currency=account.get("currency"),
            monarch_account_id=None,
            monarch_account_name=None,
            identity=account.get("identity"),
        )
    dropped = store.retain_accounts(link_key, {a["uid"] for a in accounts})
    if dropped:
        log.info("[%s] dropped %d account uid(s) from an earlier session", link_key, dropped)


def ledger_account(link_key: str, identity: str | None) -> str | None:
    """A ledger identity for a bank account that survives re-linking."""
    if not identity:
        return None
    return f"{link_key}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def with_stable_keys(
    store: Store, link_key: str, account: sqlite3.Row, transactions: list[SourceTransaction]
) -> list[SourceTransaction]:
    """Key transactions on the account's stable identity.

    Rows posted before stable identities existed are re-keyed in place, so
    they are recognised as already imported. That adoption works only while
    the uid that posted them is still the current one, and each run makes it
    permanent for everything in its window.

    Without an identity the transactions keep uid-based keys: the same
    behaviour as before, no worse.
    """
    stable = ledger_account(link_key, account["identity"])
    if stable is None:
        log.warning("[%s] no stable identity for account %s; keys follow its uid",
                    link_key, account["account_uid"][:8])
        return transactions

    keyed = [replace(txn, ledger_account=stable) for txn in transactions]
    adopted = sum(store.migrate_key(txn.legacy_dedupe_key(), txn.dedupe_key()) for txn in keyed)
    if adopted:
        log.info("[%s] re-keyed %d ledger row(s) to the stable account identity",
                 link_key, adopted)
    return keyed


def sync(config: Config) -> SyncResult:
    result = SyncResult()
    with Store(config.db_path) as store, store.exclusive():
        run_id = store.start_run()
        try:
            pending = store.pending_writes()
            if pending:
                raise RuntimeError(
                    "Uncertain Monarch writes require review. Run `monarch-euro status`, "
                    "then `monarch-euro resolve-write`. See RUNBOOK.md."
                )
            _sync(config, store, result)
        except Exception as exc:
            log.exception("Sync failed")
            result.errors.append(f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            result.errors.append(f"Interrupted: {type(exc).__name__}")
            raise
        finally:
            store.finish_run(
                run_id, "ok" if result.ok else "error", result.fetched,
                result.posted, result.skipped, "; ".join(result.errors) or None,
            )
    return result


def _sync(config: Config, store: Store, result: SyncResult) -> None:
    date_from = date.today() - timedelta(days=config.lookback_days)
    categorizer = Categorizer(load_rules(_rules_path(config)))
    sessions = store.all_sessions()
    if not sessions and not config.wise_accounts:
        missing = ", ".join(link.key for link in config.links) or "<key>"
        message = (
            f"No bank sessions on this machine. Run `monarch-euro link "
            f"{missing.split(', ')[0]}` (configured but not linked on this "
            f"machine: {missing}). Sessions are per-host; linking elsewhere "
            f"does not carry over."
        )
        log.error(message)
        result.errors.append(message)
        return

    links = {link.key: link for link in config.links}

    # A configured bank with no session is silent otherwise: the loop
    # below iterates sessions, so an unlinked bank simply never appears
    # and the run reports success having fetched nothing from it.
    linked = {row["link_key"] for row in sessions}
    for key in links:
        if key not in linked:
            message = (
                f"[{key}] configured in ACCOUNT_LINKS but not linked on this "
                f"machine - run `monarch-euro link {key}`. Sessions are per-host; "
                f"linking on another machine does not carry over."
            )
            log.error(message)
            result.errors.append(message)

    bank_client = EnableBankingClient(
        application_id=config.eb_application_id,
        private_key_path=config.eb_private_key_path,
        redirect_url=config.eb_redirect_url,
        base_url=config.eb_base_url,
    ) if config.links else nullcontext(None)
    with bank_client as client, FxConverter(
        store=store,
        target_currency=config.target_currency,
        base_url=config.fx_base_url,
    ) as fx, MonarchSink(
        email=config.monarch_email,
        password=config.monarch_password,
        mfa_secret=config.monarch_mfa_secret,
        session_path=config.monarch_session_path,
        token=config.monarch_token,
        session_cookie=config.monarch_session_cookie,
        csrf_token=config.monarch_csrf_token,
        cookie_name=config.monarch_cookie_name,
        cookie_header=config.monarch_cookie_header,
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
            except AspspRateLimitedError as exc:
                # Expected and self-healing; log without a traceback.
                message = f"[{link_key}] {exc}"
                log.warning(message)
                result.errors.append(message)
            except ApplicationNotActiveError as exc:
                message = f"[{link_key}] {exc}"
                log.error(message)
                result.errors.append(message)
            except ConsentExpiredError as exc:
                message = f"[{link_key}] consent expired: {exc}"
                log.error(message)
                result.errors.append(message)
            except Exception as exc:  # keep other banks syncing
                message = f"[{link_key}] sync failed: {exc}"
                log.exception(message)
                result.errors.append(message)

        if config.wise_accounts and not config.wise_token:
            message = (
                "Skipping Wise: WISE_ACCOUNTS is configured but WISE_TOKEN is "
                "empty. Generate a token in Wise under Settings -> API tokens."
            )
            log.error(message)
            result.errors.append(message)
        elif config.wise_accounts:
            try:
                _sync_wise(
                    config=config,
                    store=store,
                    fx=fx,
                    monarch=monarch,
                    categorizer=categorizer,
                    date_from=date_from,
                    result=result,
                )
            except Exception as exc:
                message = f"[wise] sync failed: {exc}"
                log.exception(message)
                result.errors.append(message)



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
        transactions = with_stable_keys(store, link_key, account, transactions)
        result.fetched += len(transactions)
        if not transactions:
            continue

        keys = [txn.dedupe_key() for txn in transactions]
        new_keys = store.filter_new(keys)
        fresh = list({txn.dedupe_key(): txn for txn in transactions
                      if txn.dedupe_key() in new_keys}.values())
        result.skipped += len(transactions) - len(fresh)

        if not fresh:
            log.info("[%s/%s] nothing new", link_key, account_uid[:8])
            continue

        fx.prefetch(fresh)
        log.info("[%s/%s] posting %d new transactions", link_key, account_uid[:8], len(fresh))

        for txn in fresh:
            converted = fx.convert(txn)
            merchant, category = categorizer.apply(
                txn.description, txn.counterparty, transaction_code(txn.raw)
            )
            note = converted.note(config.note_original_amount)

            monarch_txn_id = _post(
                store, config, monarch,
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


def _sync_wise(
    *,
    config: Config,
    store: Store,
    fx: FxConverter,
    monarch: MonarchSink,
    categorizer: Categorizer,
    date_from: date,
    result: SyncResult,
) -> None:
    """Sync Wise balances through Wise's own API.

    Wise does not go through Enable Banking: PSD2 access is scoped to
    EEA-registered institutions, so a US-registered profile is invisible to an
    AISP even when its EUR balance carries a Belgian IBAN.
    """
    with WiseClient(
        token=config.wise_token,
        private_key_path=config.wise_private_key_path,
    ) as wise:
        profiles = wise.profiles()
        if not profiles:
            raise WiseError("Wise returned no profiles for this token.")

        if config.wise_profile_id:
            # Pinned explicitly: never silently fall through to another
            # profile (a business one, say) if Wise reorders or renames.
            profile = next(
                (p for p in profiles if str(p.get("id")) == config.wise_profile_id), None
            )
            if profile is None:
                available = ", ".join(
                    "{}({})".format(p.get("id"), p.get("type")) for p in profiles
                )
                raise WiseError(
                    f"WISE_PROFILE_ID {config.wise_profile_id} is not on this token. "
                    f"Available: {available}"
                )
        else:
            profile = next(
                (p for p in profiles if str(p.get("type", "")).lower() == "personal"),
                profiles[0],
            )
        profile_id = profile.get("id")
        log.info("Wise profile %s (%s)", profile_id, profile.get("type", "?"))

        balances = wise.balances(profile_id)
        by_currency = {
            str(b.get("currency", "")).upper(): b for b in balances if b.get("currency")
        }
        log.info("Wise balances available: %s", ", ".join(sorted(by_currency)) or "none")

        for account in config.wise_accounts:
            balance = by_currency.get(account.currency)
            if balance is None:
                message = (
                    f"[wise] no {account.currency} balance on this profile "
                    f"(have: {', '.join(sorted(by_currency)) or 'none'})"
                )
                log.error(message)
                result.errors.append(message)
                continue

            monarch_account_id = monarch.ensure_account(account.monarch_account_name)
            transactions = wise.transactions(
                account_key=f"wise-{account.currency.lower()}",
                profile_id=profile_id,
                balance_id=balance.get("id"),
                currency=account.currency,
                date_from=date_from,
            )
            result.fetched += len(transactions)
            if not transactions:
                continue

            account_uid = transactions[0].account_uid
            store.put_account(
                account_uid=account_uid,
                link_key=f"wise-{account.currency.lower()}",
                identifier=str(balance.get("id")),
                name=f"Wise {account.currency}",
                currency=account.currency,
                monarch_account_id=monarch_account_id,
                monarch_account_name=account.monarch_account_name,
            )

            keys = [t.dedupe_key() for t in transactions]
            new_keys = store.filter_new(keys)
            fresh = list({t.dedupe_key(): t for t in transactions
                          if t.dedupe_key() in new_keys}.values())
            result.skipped += len(transactions) - len(fresh)
            if not fresh:
                log.info("[wise/%s] nothing new", account.currency)
                continue

            fx.prefetch(fresh)
            log.info("[wise/%s] posting %d new transactions", account.currency, len(fresh))

            for txn in fresh:
                converted = fx.convert(txn)
                merchant, category = categorizer.apply(
                    txn.description, txn.counterparty, transaction_code(txn.raw)
                )
                note = converted.note(config.note_original_amount)
                monarch_txn_id = _post(
                    store, config, monarch,
                    txn=converted,
                    account_id=monarch_account_id,
                    merchant=merchant,
                    category=category,
                    note=note,
                )
                if not config.dry_run:
                    store.mark_posted(
                        dedupe_key=txn.dedupe_key(),
                        account_uid=txn.account_uid,
                        monarch_txn_id=monarch_txn_id,
                        booked_on=txn.booked_on,
                        amount=converted.amount,
                        currency=converted.currency,
                    )
                result.posted += 1


def _post(
    store: Store,
    config: Config,
    monarch: MonarchSink,
    *,
    txn: ConvertedTransaction,
    account_id: str,
    merchant: str,
    category: str | None,
    note: str | None,
) -> str | None:
    """Persist intent before HTTP so a lost response cannot cause a blind retry."""
    if not config.dry_run:
        store.begin_write(txn, account_id, merchant)
    try:
        transaction_id = monarch.post(
            txn=txn, account_id=account_id, merchant=merchant, category=category, note=note,
        )
    except Exception as exc:
        if config.dry_run:
            raise
        raise RuntimeError(
            f"Monarch write is uncertain: {exc}. Run `monarch-euro status`, "
            "then `monarch-euro resolve-write`. See RUNBOOK.md."
        ) from exc
    if not config.dry_run and not transaction_id:
        raise RuntimeError("Monarch returned no transaction ID. The write requires review.")
    return transaction_id
