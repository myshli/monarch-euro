"""Environment-driven configuration.

Everything is read from the process environment so the same image runs on a
laptop and on a VPS with no code changes. `.env` is loaded only when present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise ConfigError(
            f"{key} is not set. Copy .env.example to .env and fill it in, "
            f"or export {key} in the environment."
        )
    return value


def _flag(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AccountLink:
    """Maps one bank account at the source to one manual account in Monarch."""

    key: str
    aspsp_name: str
    aspsp_country: str
    monarch_account_name: str


@dataclass(frozen=True)
class Config:
    # --- Enable Banking (source) ---
    eb_application_id: str
    eb_private_key_path: Path
    eb_base_url: str
    eb_redirect_url: str

    # --- Monarch (sink) ---
    monarch_email: str
    monarch_password: str
    monarch_mfa_secret: str

    # --- Behaviour ---
    target_currency: str
    fx_base_url: str
    note_original_amount: bool
    lookback_days: int
    include_pending: bool
    dry_run: bool

    # --- Paths ---
    state_dir: Path
    links: list[AccountLink] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "monarch_euro.sqlite3"

    @property
    def monarch_session_path(self) -> Path:
        return self.state_dir / "monarch_session.pickle"


def _parse_links() -> list[AccountLink]:
    """Parse ACCOUNT_LINKS.

    Format is a semicolon-separated list of
    ``key|ASPSP name|COUNTRY|Monarch account name``, e.g.

        n26|N26|DE|N26 Checking;revolut|Revolut|LT|Revolut EUR
    """
    raw = os.environ.get("ACCOUNT_LINKS", "").strip()
    if not raw:
        return []
    links: list[AccountLink] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 4:
            raise ConfigError(
                f"Malformed ACCOUNT_LINKS entry {chunk!r}. Expected "
                "'key|ASPSP name|COUNTRY|Monarch account name'."
            )
        links.append(
            AccountLink(
                key=parts[0],
                aspsp_name=parts[1],
                aspsp_country=parts[2].upper(),
                monarch_account_name=parts[3],
            )
        )
    keys = [link.key for link in links]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate ACCOUNT_LINKS keys: {sorted(duplicates)}")
    return links


def load_config(dotenv: Path | None = None) -> Config:
    _load_dotenv(dotenv or Path(".env"))

    state_dir = Path(os.environ.get("STATE_DIR", "./state")).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    key_path = Path(_require("EB_PRIVATE_KEY_PATH")).expanduser()
    if not key_path.is_file():
        raise ConfigError(f"EB_PRIVATE_KEY_PATH points at a missing file: {key_path}")

    return Config(
        eb_application_id=_require("EB_APPLICATION_ID"),
        eb_private_key_path=key_path,
        eb_base_url=os.environ.get("EB_BASE_URL", "https://api.enablebanking.com").rstrip("/"),
        eb_redirect_url=_require("EB_REDIRECT_URL"),
        monarch_email=_require("MONARCH_EMAIL"),
        monarch_password=_require("MONARCH_PASSWORD"),
        monarch_mfa_secret=_require("MONARCH_MFA_SECRET"),
        target_currency=os.environ.get("TARGET_CURRENCY", "USD").upper(),
        fx_base_url=os.environ.get("FX_BASE_URL", "https://api.frankfurter.dev/v1").rstrip("/"),
        note_original_amount=_flag("NOTE_ORIGINAL_AMOUNT", True),
        lookback_days=int(os.environ.get("LOOKBACK_DAYS", "30")),
        include_pending=_flag("INCLUDE_PENDING", False),
        dry_run=_flag("DRY_RUN", False),
        state_dir=state_dir,
        links=_parse_links(),
    )
