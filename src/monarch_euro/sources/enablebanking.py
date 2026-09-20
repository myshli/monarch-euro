"""Enable Banking client - PSD2 account information for N26, Revolut and Wise.

Enable Banking is a licensed AISP that fronts the banks' own PSD2 interfaces,
so this is first-party data over a supported channel rather than scraping.
Its "Restricted Production" tier permits linking your own accounts free of
charge, which is exactly this use case.

Auth is a short-lived RS256 JWT signed with the application's private key -
there is no token endpoint and nothing to refresh, so a headless VPS run
needs no secret beyond the key file itself.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import httpx
import jwt

from ..models import SourceTransaction

log = logging.getLogger(__name__)

JWT_TTL_SECONDS = 3600
MAX_PAGES = 200


class EnableBankingError(RuntimeError):
    pass


class ConsentExpiredError(EnableBankingError):
    """The PSU consent lapsed and the bank must be re-authorized in a browser."""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise EnableBankingError(f"Unparseable amount: {value!r}")


def _first_date(payload: dict, *keys: str) -> date | None:
    for key in keys:
        raw = payload.get(key)
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
    return None


class EnableBankingClient:
    def __init__(
        self,
        application_id: str,
        private_key_path: Path,
        redirect_url: str,
        base_url: str = "https://api.enablebanking.com",
        client: httpx.Client | None = None,
    ) -> None:
        self.application_id = application_id
        self.redirect_url = redirect_url
        self.base_url = base_url.rstrip("/")
        self._private_key = private_key_path.read_text(encoding="utf-8")
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "EnableBankingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth --------------------------------------------------------------

    def _jwt(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        issued = int(now)
        payload = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": issued,
            "exp": issued + JWT_TTL_SECONDS,
        }
        self._token = jwt.encode(
            payload,
            self._private_key,
            algorithm="RS256",
            headers={"typ": "JWT", "alg": "RS256", "kid": self.application_id},
        )
        self._token_expiry = issued + JWT_TTL_SECONDS
        return self._token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/json",
        }
        headers.update(kwargs.pop("headers", {}))
        resp = self._client.request(method, url, headers=headers, **kwargs)

        if resp.status_code in (401, 403):
            raise ConsentExpiredError(
                f"{method} {path} returned {resp.status_code}. The bank consent has "
                f"most likely expired - re-run `monarch-euro link` for this bank. "
                f"Body: {resp.text[:400]}"
            )
        if resp.status_code >= 400:
            raise EnableBankingError(
                f"{method} {path} failed with {resp.status_code}: {resp.text[:400]}"
            )
        if not resp.content:
            return {}
        return resp.json()

    # -- discovery ---------------------------------------------------------

    def application(self) -> dict:
        return self._request("GET", "/application")

    def aspsps(self, country: str | None = None) -> list[dict]:
        params = {"country": country.upper()} if country else None
        payload = self._request("GET", "/aspsps", params=params)
        return payload.get("aspsps", [])

    # -- authorization flow ------------------------------------------------

    def start_authorization(
        self, aspsp_name: str, aspsp_country: str, valid_days: int = 180
    ) -> tuple[str, str]:
        """Begin consent. Returns (authorization_url, state).

        180 days is the PSD2 ceiling most ASPSPs allow; the consent must be
        renewed in a browser once it lapses. That renewal is the only
        recurring manual step in this whole pipeline.
        """
        state = str(uuid.uuid4())
        valid_until = datetime.now(timezone.utc) + timedelta(days=valid_days)
        body = {
            "access": {"valid_until": valid_until.replace(microsecond=0).isoformat()},
            "aspsp": {"name": aspsp_name, "country": aspsp_country.upper()},
            "state": state,
            "redirect_url": self.redirect_url,
            "psu_type": "personal",
        }
        payload = self._request("POST", "/auth", json=body)
        url = payload.get("url")
        if not url:
            raise EnableBankingError(f"No authorization URL returned: {payload}")
        return url, state

    def create_session(self, code: str) -> dict:
        """Exchange the redirect `code` for a session plus its accounts."""
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    # -- data --------------------------------------------------------------

    def balances(self, account_uid: str) -> list[dict]:
        payload = self._request("GET", f"/accounts/{account_uid}/balances")
        return payload.get("balances", [])

    def raw_transactions(
        self, account_uid: str, date_from: date, include_pending: bool
    ) -> Iterator[dict]:
        """Yield raw transaction dicts, following `continuation_key` pagination."""
        params: dict[str, Any] = {"date_from": date_from.isoformat()}
        if include_pending:
            # Booked is the default; ask for both when pending is wanted.
            params["transaction_status"] = "BOTH"

        seen_pages = 0
        while True:
            payload = self._request(
                "GET", f"/accounts/{account_uid}/transactions", params=params
            )
            for item in payload.get("transactions", []):
                yield item

            continuation = payload.get("continuation_key")
            if not continuation:
                return
            seen_pages += 1
            if seen_pages >= MAX_PAGES:
                log.warning(
                    "Stopped paginating %s after %d pages; narrow the window.",
                    account_uid,
                    MAX_PAGES,
                )
                return
            params["continuation_key"] = continuation

    def transactions(
        self,
        account_key: str,
        account_uid: str,
        date_from: date,
        include_pending: bool = False,
    ) -> list[SourceTransaction]:
        out: list[SourceTransaction] = []
        for raw in self.raw_transactions(account_uid, date_from, include_pending):
            txn = self._normalize(account_key, account_uid, raw)
            if txn is None:
                continue
            if txn.pending and not include_pending:
                continue
            out.append(txn)
        return out

    # -- normalization -----------------------------------------------------

    @staticmethod
    def _normalize(account_key: str, account_uid: str, raw: dict) -> SourceTransaction | None:
        amount_block = raw.get("transaction_amount") or {}
        amount_raw = amount_block.get("amount")
        currency = (amount_block.get("currency") or "").upper()
        if amount_raw is None or not currency:
            log.debug("Skipping transaction with no amount: %s", raw.get("entry_reference"))
            return None

        magnitude = _decimal(amount_raw).copy_abs()

        # Berlin Group reports an unsigned amount plus a direction flag. Apply
        # it here so nothing downstream has to reason about sign conventions.
        indicator = (raw.get("credit_debit_indicator") or "").upper()
        if indicator == "DBIT":
            amount = -magnitude
        elif indicator == "CRDT":
            amount = magnitude
        else:
            # A few ASPSPs omit the flag and sign the amount directly.
            amount = _decimal(amount_raw)

        booked = _first_date(raw, "booking_date", "value_date", "transaction_date")
        if booked is None:
            log.debug("Skipping transaction with no usable date: %s", raw)
            return None

        status = (raw.get("status") or "").upper()
        pending = status == "PDNG"

        creditor = (raw.get("creditor") or {}).get("name")
        debtor = (raw.get("debtor") or {}).get("name")
        # For an outflow the interesting party is who was paid; for an inflow,
        # who paid us.
        counterparty = (creditor if amount < 0 else debtor) or creditor or debtor

        remittance = raw.get("remittance_information") or []
        if isinstance(remittance, str):
            remittance = [remittance]
        description = " ".join(str(part).strip() for part in remittance if part).strip()
        if not description:
            description = counterparty or "Unknown"

        return SourceTransaction(
            account_key=account_key,
            account_uid=account_uid,
            booked_on=booked,
            amount=amount,
            currency=currency,
            description=description,
            counterparty=counterparty,
            reference=raw.get("entry_reference"),
            pending=pending,
            raw=raw,
        )


def extract_accounts(session_payload: dict) -> list[dict]:
    """Pull a uniform account list out of a session response.

    Enable Banking has used both `uid` and `account_id` across API versions,
    and nests the human-readable identifier differently per ASPSP.
    """
    accounts = []
    for item in session_payload.get("accounts", []):
        if isinstance(item, str):
            accounts.append({"uid": item, "identifier": None, "name": None, "currency": None})
            continue
        uid = item.get("uid") or item.get("account_id") or item.get("resource_id")
        if not uid:
            continue
        account_id = item.get("account_id")
        identifier = None
        if isinstance(account_id, dict):
            identifier = account_id.get("iban") or account_id.get("other")
        identifier = identifier or item.get("iban")
        accounts.append(
            {
                "uid": uid,
                "identifier": identifier,
                "name": item.get("name") or item.get("product") or item.get("details"),
                "currency": (item.get("currency") or "").upper() or None,
            }
        )
    return accounts
