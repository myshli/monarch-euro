"""Wise client - reads balance statements through Wise's own official API.

Wise is handled outside Enable Banking on purpose. PSD2 open banking only
reaches accounts held at an EEA-registered institution, so a US-registered
Wise profile is not visible to an AISP even when its EUR balance carries a
Belgian IBAN. Wise's own API has no such boundary: a personal token sees
every balance on the profile, which also means the USD balance comes along
for free.

Two access regimes exist, and which one applies depends on the profile:

- EU/UK profiles fall under PSD2. Reading a statement returns 403 with an
  `x-2fa-approval` header carrying a one-time token; signing that token with
  the private key registered on the account and retrying satisfies SCA.
- Profiles outside the EEA are generally not subject to SCA, so the first
  request simply succeeds.

This client handles both: it tries plainly, and signs only when Wise asks it
to. No configuration says which regime applies, because the API tells us.
"""

from __future__ import annotations

import base64
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..models import SourceTransaction

log = logging.getLogger(__name__)

# Wise rejects statement windows longer than this.
MAX_STATEMENT_DAYS = 469


class WiseError(RuntimeError):
    pass


class WiseSCARequired(WiseError):
    """Wise demanded a signed SCA challenge and no private key was configured."""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise WiseError(f"Unparseable amount: {value!r}")


class WiseClient:
    def __init__(
        self,
        token: str,
        private_key_path: Path | None = None,
        base_url: str = "https://api.wise.com",
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._private_key_path = private_key_path
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WiseClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- SCA ---------------------------------------------------------------

    def _sign(self, one_time_token: str) -> str:
        if not self._private_key_path or not self._private_key_path.is_file():
            raise WiseSCARequired(
                "Wise requires a signed SCA challenge for this profile, but no key "
                "is configured. Generate an RSA keypair, upload the public half in "
                "Wise under Settings -> API tokens -> Manage public keys, and point "
                "WISE_PRIVATE_KEY_PATH at the private half."
            )
        # Imported lazily so the dependency is only needed by EEA profiles.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = serialization.load_pem_private_key(
            self._private_key_path.read_bytes(), password=None
        )
        signature = private_key.sign(
            one_time_token.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(signature).decode("ascii")

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        resp = self._client.get(url, headers=headers, params=params)

        # PSD2 challenge: sign the one-time token and replay the same request.
        if resp.status_code == 403 and resp.headers.get("x-2fa-approval"):
            one_time_token = resp.headers["x-2fa-approval"]
            log.info("Wise issued an SCA challenge; signing and retrying")
            signed_headers = dict(headers)
            signed_headers["x-2fa-approval"] = one_time_token
            signed_headers["X-Signature"] = self._sign(one_time_token)
            resp = self._client.get(url, headers=signed_headers, params=params)

        if resp.status_code == 401:
            raise WiseError(
                f"GET {path} returned 401. The Wise personal token is invalid or "
                f"expired - regenerate it under Settings -> API tokens."
            )
        if resp.status_code >= 400:
            raise WiseError(f"GET {path} failed with {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    # -- discovery ---------------------------------------------------------

    def profiles(self) -> list[dict]:
        """List profiles, tolerating Wise's versioned endpoints."""
        last_error: Exception | None = None
        for path in ("/v2/profiles", "/v1/profiles"):
            try:
                payload = self._get(path)
            except WiseError as exc:
                last_error = exc
                continue
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                return payload.get("profiles") or [payload]
        raise WiseError(f"Could not list Wise profiles: {last_error}")

    def balances(self, profile_id: int | str) -> list[dict]:
        """List the profile's balances (one per currency)."""
        last_error: Exception | None = None
        for path in (
            f"/v4/profiles/{profile_id}/balances",
            f"/v3/profiles/{profile_id}/balances",
        ):
            try:
                payload = self._get(path, params={"types": "STANDARD"})
            except WiseError as exc:
                last_error = exc
                continue
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                return payload.get("balances") or []
        raise WiseError(f"Could not list balances for profile {profile_id}: {last_error}")

    # -- statements --------------------------------------------------------

    def statement(
        self,
        profile_id: int | str,
        balance_id: int | str,
        currency: str,
        start: date,
        end: date,
    ) -> dict:
        if (end - start).days > MAX_STATEMENT_DAYS:
            raise WiseError(
                f"Wise statements cover at most {MAX_STATEMENT_DAYS} days; "
                f"{start}..{end} is wider."
            )
        params = {
            "currency": currency.upper(),
            "intervalStart": datetime.combine(start, time.min, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            ),
            "intervalEnd": datetime.combine(end, time.max, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.999Z"
            ),
            "type": "COMPACT",
        }
        return self._get(
            f"/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
            params=params,
        )

    def transactions(
        self,
        account_key: str,
        profile_id: int | str,
        balance_id: int | str,
        currency: str,
        date_from: date,
        date_to: date | None = None,
    ) -> list[SourceTransaction]:
        end = date_to or date.today()
        payload = self.statement(profile_id, balance_id, currency, date_from, end)
        account_uid = f"wise:{profile_id}:{balance_id}:{currency.upper()}"

        out: list[SourceTransaction] = []
        for raw in payload.get("transactions") or []:
            txn = self._normalize(account_key, account_uid, raw, currency)
            if txn is not None:
                out.append(txn)
        return out

    # -- normalization -----------------------------------------------------

    @staticmethod
    def _normalize(
        account_key: str, account_uid: str, raw: dict, fallback_currency: str
    ) -> SourceTransaction | None:
        amount_block = raw.get("amount") or {}
        value = amount_block.get("value")
        if value is None:
            log.debug("Skipping Wise row with no amount: %s", raw.get("referenceNumber"))
            return None

        amount = _decimal(value)
        currency = (amount_block.get("currency") or fallback_currency).upper()

        # Wise signs the value itself, but also sends a DEBIT/CREDIT type.
        # Trust the type when the two disagree, which happens on fee rows.
        kind = (raw.get("type") or "").upper()
        if kind == "DEBIT" and amount > 0:
            amount = -amount
        elif kind == "CREDIT" and amount < 0:
            amount = abs(amount)

        raw_date = raw.get("date")
        if not raw_date:
            return None
        try:
            booked = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).date()
        except ValueError:
            log.debug("Unparseable Wise date: %r", raw_date)
            return None

        details = raw.get("details") or {}
        merchant = (details.get("merchant") or {}).get("name")
        counterparty = (
            merchant
            or details.get("senderName")
            or details.get("recipient")
            or details.get("payerName")
        )
        description = (
            details.get("description")
            or details.get("paymentReference")
            or counterparty
            or "Wise transaction"
        )

        return SourceTransaction(
            account_key=account_key,
            account_uid=account_uid,
            booked_on=booked,
            amount=amount,
            currency=currency,
            description=str(description),
            counterparty=str(counterparty) if counterparty else None,
            reference=raw.get("referenceNumber"),
            pending=False,
            raw=raw,
        )
