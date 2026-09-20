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
import os
import tempfile
import uuid
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
        path = self.out_dir / f"monarch-import-{stamp:%Y%m%d}-{uuid.uuid4().hex}.csv"
        self._rows.sort(key=lambda r: (r["Date"], r["Account"]))
        # Publish only complete files. The ledger changes after this succeeds.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8", dir=self.out_dir,
                prefix=".monarch-import-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(self._rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.out_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        log.info("Wrote %d rows to %s", len(self._rows), path)
        return path
