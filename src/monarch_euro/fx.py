"""Currency conversion against ECB daily reference rates.

Monarch has no concept of currency: every amount is rendered with a dollar
sign and summed directly into net worth and budgets. Importing EUR amounts
raw therefore silently corrupts every cross-account total. We convert each
transaction at the ECB rate for its own booking date - not today's rate - so
historical spending stays comparable with the US accounts already in Monarch.

Rates come from Frankfurter, a free ECB mirror requiring no API key. The ECB
publishes on TARGET business days only; for a weekend or holiday date the API
returns the most recent prior publication and tells us which date it used, so
we record the rate date we actually applied.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import httpx

from .models import ConvertedTransaction, SourceTransaction
from .store import Store

log = logging.getLogger(__name__)

CENTS = Decimal("0.01")


class FxError(RuntimeError):
    """Raised when a rate cannot be determined for a transaction."""


class FxConverter:
    def __init__(
        self,
        store: Store,
        target_currency: str,
        base_url: str = "https://api.frankfurter.dev/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self.store = store
        self.target = target_currency.upper()
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "FxConverter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- rate lookup -------------------------------------------------------

    def _fetch_range(self, currency: str, start: date, end: date) -> None:
        """Fetch and cache every published rate in a window, in one request.

        A month of transactions typically spans one window, so this turns
        what would be dozens of HTTP calls into a single one.
        """
        url = f"{self.base_url}/{start:%Y-%m-%d}..{end:%Y-%m-%d}"
        params = {"base": currency, "symbols": self.target}
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
        rates = payload.get("rates") or {}
        for day_str, quote in rates.items():
            value = quote.get(self.target)
            if value is None:
                continue
            published = date.fromisoformat(day_str)
            self.store.put_fx_rate(
                base=currency,
                quote=self.target,
                requested_date=published,
                rate=Decimal(str(value)),
                rate_date=published,
            )

    def _fetch_single(self, currency: str, on: date) -> tuple[Decimal, date]:
        """Fetch one date, honouring the API's fallback to the prior publication."""
        url = f"{self.base_url}/{on:%Y-%m-%d}"
        resp = self._client.get(url, params={"base": currency, "symbols": self.target})
        resp.raise_for_status()
        payload = resp.json()
        value = (payload.get("rates") or {}).get(self.target)
        if value is None:
            raise FxError(f"No {currency}->{self.target} rate published for {on}")
        rate = Decimal(str(value))
        rate_date = date.fromisoformat(payload["date"])
        self.store.put_fx_rate(
            base=currency,
            quote=self.target,
            requested_date=on,
            rate=rate,
            rate_date=rate_date,
        )
        return rate, rate_date

    def rate_for(self, currency: str, on: date) -> tuple[Decimal, date]:
        currency = currency.upper()
        if currency == self.target:
            return Decimal("1"), on

        cached = self.store.get_fx_rate(currency, self.target, on)
        if cached is not None:
            return cached

        # The requested day may be a weekend or ECB holiday. Walk back up to a
        # week through the cache before spending an HTTP call.
        for offset in range(1, 8):
            probe = on - timedelta(days=offset)
            cached = self.store.get_fx_rate(currency, self.target, probe)
            if cached is not None:
                rate, rate_date = cached
                self.store.put_fx_rate(currency, self.target, on, rate, rate_date)
                return rate, rate_date

        return self._fetch_single(currency, on)

    def prefetch(self, transactions: list[SourceTransaction]) -> None:
        """Warm the rate cache for everything we are about to convert."""
        windows: dict[str, tuple[date, date]] = {}
        for txn in transactions:
            currency = txn.currency.upper()
            if currency == self.target:
                continue
            low, high = windows.get(currency, (txn.booked_on, txn.booked_on))
            windows[currency] = (min(low, txn.booked_on), max(high, txn.booked_on))

        for currency, (start, end) in windows.items():
            # Pad backwards so a Monday-only batch still sees Friday's rate.
            padded_start = start - timedelta(days=7)
            try:
                self._fetch_range(currency, padded_start, end)
            except httpx.HTTPError as exc:
                # Not fatal: rate_for() will fall back to per-date lookups.
                log.warning("FX prefetch failed for %s: %s", currency, exc)

    # -- conversion --------------------------------------------------------

    def convert(self, txn: SourceTransaction) -> ConvertedTransaction:
        currency = txn.currency.upper()

        # An executed rate from the source beats a daily reference average.
        if (
            txn.exact_amount is not None
            and (txn.exact_currency or "").upper() == self.target
            and currency != self.target
        ):
            exact = txn.exact_amount.quantize(CENTS, rounding=ROUND_HALF_UP)
            rate = (
                (exact / txn.amount).quantize(Decimal("0.000001"))
                if txn.amount
                else None
            )
            return ConvertedTransaction(
                source=txn,
                amount=exact,
                currency=self.target,
                fx_rate=rate,
                fx_rate_date=txn.booked_on,
                exact=True,
            )

        if currency == self.target:
            return ConvertedTransaction(
                source=txn,
                amount=txn.amount.quantize(CENTS, rounding=ROUND_HALF_UP),
                currency=self.target,
                fx_rate=None,
                fx_rate_date=None,
            )

        rate, rate_date = self.rate_for(currency, txn.booked_on)
        converted = (txn.amount * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
        return ConvertedTransaction(
            source=txn,
            amount=converted,
            currency=self.target,
            fx_rate=rate,
            fx_rate_date=rate_date,
        )
