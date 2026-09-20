"""CSV sink - writes a Monarch-importable file.

Monarch has closed the programmatic door: their web app authenticates with
httpOnly session cookies rather than a bearer token, and the API login
endpoint the community client depends on now answers CAPTCHA_REQUIRED. That
is deliberate bot protection, and defeating it is not the right engineering
answer.

So the pipeline's fetch, dedupe, conversion and categorization all still run
unattended; only the final hop becomes a file you upload. Monarch's importer
needs Date, Merchant and Amount, and accepts Category, Account, Notes and
Tags alongside them.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

from ..models import ConvertedTransaction

log = logging.getLogger(__name__)

COLUMNS = ["Date", "Merchant", "Category", "Account", "Original Statement", "Notes", "Amount"]


class CsvSink:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict[str, str]] = []

    def add(
        self,
        txn: ConvertedTransaction,
        account_name: str,
        merchant: str,
        category: str | None,
        note: str | None,
    ) -> None:
        self._rows.append(
            {
                "Date": txn.source.booked_on.isoformat(),
                "Merchant": merchant,
                "Category": category or "",
                "Account": account_name,
                # Monarch keeps this verbatim, so the bank's own wording stays
                # searchable even after merchant cleanup rewrites the name.
                "Original Statement": txn.source.description or "",
                "Notes": note or "",
                "Amount": f"{txn.amount:.2f}",
            }
        )

    def __len__(self) -> int:
        return len(self._rows)

    def write(self, stamp: date | None = None) -> Path | None:
        if not self._rows:
            return None
        stamp = stamp or date.today()
        path = self.out_dir / f"monarch-import-{stamp:%Y%m%d}.csv"
        # Sort oldest-first so Monarch's running balance reads naturally.
        self._rows.sort(key=lambda r: (r["Date"], r["Account"]))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(self._rows)
        log.info("Wrote %d rows to %s", len(self._rows), path)
        return path
