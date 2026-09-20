"""Monarch sink - pushes converted transactions into manual accounts.

Monarch publishes no supported customer API, so this rides on the community
`monarchmoney` client, which drives the same private GraphQL endpoint the web
app uses. That is the trade-off for automation here, and it means this layer
is the most likely thing to break if Monarch changes their backend. It is
deliberately isolated behind a narrow interface so it can be swapped for the
official MCP connector if that comes back.

The library is async; this wrapper owns a private event loop so the rest of
the pipeline stays ordinary synchronous code.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from monarchmoney import MonarchMoney, RequireMFAException

from ..models import ConvertedTransaction
from .monarch_compat import (
    build_client,
    build_raw_cookie_client,
    build_session_client,
    patch_endpoints,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Monarch's manual-account taxonomy. A EUR current account maps onto a plain
# depository/checking account; it is the closest fit and keeps the balance in
# net worth.
MANUAL_ACCOUNT_TYPE = "depository"
MANUAL_ACCOUNT_SUBTYPE = "checking"


class MonarchError(RuntimeError):
    pass


class MonarchSink:
    def __init__(
        self,
        email: str,
        password: str,
        mfa_secret: str,
        session_path: Path,
        token: str = "",
        session_cookie: str = "",
        csrf_token: str = "",
        cookie_name: str = "session_id",
        cookie_header: str = "",
        default_category: str = "Uncategorized",
        update_balance: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.email = email
        self.password = password
        self.mfa_secret = mfa_secret
        self.session_path = session_path
        self.token = token
        self.session_cookie = session_cookie
        self.csrf_token = csrf_token
        self.cookie_name = cookie_name
        self.cookie_header = cookie_header
        self.default_category = default_category
        self.update_balance = update_balance
        self.dry_run = dry_run

        self._loop = asyncio.new_event_loop()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        patch_endpoints()
        if cookie_header and csrf_token:
            self._mm = build_raw_cookie_client(cookie_header, csrf_token, session_path.parent)
            self._mm._session_file = str(session_path)
        elif session_cookie and csrf_token:
            self._mm = build_session_client(
                session_cookie, csrf_token, session_path.parent, cookie_name=cookie_name
            )
            self._mm._session_file = str(session_path)
        elif token:
            self._mm = build_client(token, session_path.parent)
            self._mm._session_file = str(session_path)
        else:
            self._mm = MonarchMoney(session_file=str(session_path))
        self._categories: dict[str, str] = {}
        self._accounts: dict[str, str] = {}
        self._logged_in = False

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass

    def __enter__(self) -> "MonarchSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth --------------------------------------------------------------

    def login(self) -> None:
        """Authenticate, reusing a saved session when one is still valid.

        The MFA secret is the TOTP seed from Monarch's security settings, not
        a six-digit code, so the library can mint codes itself and the run
        needs no human present.
        """
        if self._logged_in:
            return

        # A browser-session token skips login entirely, which is the only
        # route that still works: Monarch now answers programmatic password
        # logins with CAPTCHA_REQUIRED.
        if ((self.cookie_header or self.session_cookie) and self.csrf_token) or self.token:
            self._logged_in = True
            return

        # MONARCH_MFA_SECRET is deliberately not required: an account without
        # two-factor enabled logs in on email and password alone, and demanding
        # a seed that does not exist would block it for no reason.
        missing = [
            name
            for name, value in (
                ("MONARCH_EMAIL", self.email),
                ("MONARCH_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise MonarchError(
                f"Cannot log in to Monarch: {', '.join(missing)} not set."
            )

        try:
            self._run(
                self._mm.login(
                    email=self.email,
                    password=self.password,
                    use_saved_session=True,
                    save_session=True,
                    mfa_secret_key=self.mfa_secret or None,
                )
            )
        except RequireMFAException as exc:
            raise MonarchError(
                "Monarch demanded MFA and the stored secret was rejected. Confirm "
                "MONARCH_MFA_SECRET holds the TOTP seed (the string behind the QR "
                "code in Monarch's security settings), not a one-time code."
            ) from exc
        except Exception as exc:
            if "CAPTCHA" in str(exc).upper():
                raise MonarchError(
                    "Monarch requires a CAPTCHA for programmatic sign-in, so "
                    "password login cannot work. Sign in at app.monarch.com in a "
                    "browser and put that session's bearer token in MONARCH_TOKEN "
                    "instead - see README, 'Monarch authentication'."
                ) from exc
            if "429" in str(exc):
                # Retrying is what caused this; say so rather than inviting more.
                raise MonarchError(
                    "Monarch is rate-limiting sign-ins (HTTP 429). Wait before "
                    "trying again - roughly 15-30 minutes clears it. Repeated "
                    "attempts extend the limit rather than shortening it. Any "
                    "saved session has been left in place."
                ) from exc

            # A stale pickle produces confusing downstream errors; clear it so
            # the next run re-authenticates from scratch.
            if self.session_path.exists():
                log.warning("Login failed; discarding saved session at %s", self.session_path)
                self.session_path.unlink(missing_ok=True)
            raise MonarchError(f"Monarch login failed: {exc}") from exc
        self._logged_in = True

    def interactive_login(self, mfa_code: str | None = None) -> None:
        """Log in once, typing the MFA code by hand, and persist the session.

        This is the alternative to storing the TOTP seed. The saved session
        outlives the code by months, so unattended runs need no second factor
        on disk - at the cost of redoing this when the session eventually
        lapses.
        """
        if not self.email or not self.password:
            raise MonarchError("MONARCH_EMAIL and MONARCH_PASSWORD must be set.")

        self.session_path.unlink(missing_ok=True)
        try:
            if mfa_code:
                self._run(
                    self._mm.multi_factor_authenticate(self.email, self.password, mfa_code)
                )
                self._mm.save_session(str(self.session_path))
            else:
                self._run(
                    self._mm.login(
                        email=self.email,
                        password=self.password,
                        use_saved_session=False,
                        save_session=True,
                    )
                )
        except RequireMFAException:
            raise MonarchError(
                "Monarch asked for a two-factor code. Re-run with --code <123456> "
                "using the current code from your authenticator app."
            )
        except Exception as exc:
            raise MonarchError(f"Monarch login failed: {exc}") from exc

        self._logged_in = True
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass

    # -- lookups -----------------------------------------------------------

    def refresh_metadata(self) -> None:
        self.login()
        accounts = self._run(self._mm.get_accounts())
        self._accounts = {
            acct["displayName"]: acct["id"]
            for acct in accounts.get("accounts", [])
            if acct.get("displayName")
        }
        categories = self._run(self._mm.get_transaction_categories())
        self._categories = {
            cat["name"]: cat["id"] for cat in categories.get("categories", []) if cat.get("name")
        }
        log.debug(
            "Monarch metadata: %d accounts, %d categories",
            len(self._accounts),
            len(self._categories),
        )

    def account_names(self) -> list[str]:
        return sorted(self._accounts)

    def category_names(self) -> list[str]:
        return sorted(self._categories)

    def category_id(self, name: str | None) -> str:
        if name and name in self._categories:
            return self._categories[name]
        if self.default_category in self._categories:
            return self._categories[self.default_category]
        if not self._categories:
            raise MonarchError("No Monarch categories loaded; call refresh_metadata() first.")
        # Last resort: any category is better than failing the whole run.
        fallback = sorted(self._categories)[0]
        log.warning(
            "Category %r and default %r both missing; using %r",
            name,
            self.default_category,
            fallback,
        )
        return self._categories[fallback]

    def ensure_account(self, name: str) -> str:
        """Return the id of the manual account called `name`, creating it if absent."""
        if name in self._accounts:
            return self._accounts[name]
        if self.dry_run:
            log.info("[dry-run] would create manual account %r", name)
            return f"dry-run-account:{name}"

        log.info("Creating manual Monarch account %r", name)
        result = self._run(
            self._mm.create_manual_account(
                account_type=MANUAL_ACCOUNT_TYPE,
                account_sub_type=MANUAL_ACCOUNT_SUBTYPE,
                is_in_net_worth=True,
                account_name=name,
                account_balance=0,
            )
        )
        created = (result or {}).get("createManualAccount", {}) or {}
        errors = created.get("errors")
        if errors:
            raise MonarchError(f"Could not create account {name!r}: {errors}")
        account = created.get("account") or {}
        account_id = account.get("id")
        if not account_id:
            raise MonarchError(f"Account creation for {name!r} returned no id: {result}")
        self._accounts[name] = account_id
        return account_id

    # -- writes ------------------------------------------------------------

    def post(
        self,
        txn: ConvertedTransaction,
        account_id: str,
        merchant: str,
        category: str | None,
        note: str | None,
    ) -> str | None:
        """Create one transaction. Returns the Monarch transaction id."""
        amount = float(txn.amount)
        when = txn.source.booked_on.isoformat()

        if self.dry_run:
            log.info(
                "[dry-run] %s %-28.28s %10.2f %s  %s",
                when,
                merchant,
                amount,
                txn.currency,
                note or "",
            )
            return None

        result = self._run(
            self._mm.create_transaction(
                date=when,
                account_id=account_id,
                amount=amount,
                merchant_name=merchant[:200],
                category_id=self.category_id(category),
                notes=(note or "")[:1000],
                update_balance=self.update_balance,
            )
        )
        created = (result or {}).get("createTransaction", {}) or {}
        errors = created.get("errors")
        if errors:
            raise MonarchError(f"Monarch rejected transaction {when} {merchant!r}: {errors}")
        transaction = created.get("transaction") or {}
        transaction_id = transaction.get("id")
        if not transaction_id:
            raise MonarchError("Monarch returned no transaction ID. The write requires review.")
        return transaction_id


def format_amount(value: Decimal) -> str:
    return f"{value:,.2f}"
