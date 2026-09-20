"""Normalized transaction model shared by every source and sink."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SourceTransaction:
    """One transaction as it exists at the bank, before currency conversion.

    `amount` is already signed: negative for money leaving the account,
    positive for money arriving. Sources are responsible for applying the
    credit/debit indicator so downstream code never has to think about it.
    """

    account_key: str
    account_uid: str
    booked_on: date
    amount: Decimal
    currency: str
    description: str
    counterparty: str | None
    reference: str | None
    pending: bool
    raw: dict
    exact_amount: Decimal | None = None
    """The amount in the pipeline's target currency, when the source knows it.

    Wise reports both sides of a currency conversion, so a EUR top-up funded
    from USD carries the real USD figure. Using it beats converting the EUR
    side at an ECB daily average, which is only ever an approximation of a
    rate that actually executed.
    """
    exact_currency: str | None = None
    occurrence: int = 0
    """Ordinal among otherwise-identical transactions on the same day.

    N26 leaves `entry_reference` null on most rows, so the content hash does
    the real work — and a plain content hash cannot tell two identical
    same-day transactions apart. Two €2.50 coffees at the same café would
    collapse into one and the second would never be imported. Sources assign
    this ordinal by the bank's own ordering, which is stable for booked
    transactions, so each gets a distinct identity that survives re-fetching.
    """

    def dedupe_key(self) -> str:
        """Stable identity for this transaction.

        Prefers the bank's own `entry_reference`, which is unique per account
        under the Berlin Group spec. Not every ASPSP populates it, so we fall
        back to a content hash plus an occurrence ordinal. The key deliberately
        excludes `pending`, so a transaction keeps its identity when it settles.
        """
        if self.reference:
            return f"{self.account_uid}:ref:{self.reference}"
        material = "|".join(
            [
                self.account_uid,
                self.booked_on.isoformat(),
                f"{self.amount:.2f}",
                self.currency,
                (self.counterparty or "").strip().lower(),
                (self.description or "").strip().lower(),
            ]
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        return f"{self.account_uid}:hash:{digest}:{self.occurrence}"


@dataclass(frozen=True)
class ConvertedTransaction:
    """A source transaction expressed in Monarch's single currency."""

    source: SourceTransaction
    amount: Decimal
    currency: str
    fx_rate: Decimal | None
    fx_rate_date: date | None
    exact: bool = False
    """True when the rate actually executed, rather than an ECB daily average."""

    @property
    def converted(self) -> bool:
        return self.fx_rate is not None

    def note(self, include_original: bool) -> str | None:
        """Audit trail for the conversion, stored on the Monarch transaction.

        Without this the conversion is lossy and irreversible: you could never
        reconcile a Monarch row against the original bank statement, nor
        recompute it if Monarch ever gains real currency support.
        """
        if not include_original or not self.converted:
            return None
        source = "Wise" if self.exact else "ECB"
        return (
            f"{self.source.currency} {self.source.amount:.2f} "
            f"@ {self.fx_rate} {source} {self.fx_rate_date:%Y-%m-%d} "
            f"= {self.currency} {self.amount:.2f}"
        )
