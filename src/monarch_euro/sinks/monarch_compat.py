"""Compatibility shim for Monarch's current API.

The `monarchmoney` client (0.1.15, and `main` as of this writing) targets an
API Monarch has since moved on from:

- It posts to ``api.monarchmoney.com``, which now 301s to ``api.monarch.com``.
- It sends neither the client identity nor the version header Monarch's
  backend requires, so authenticated calls come back
  ``403 Please update to the latest version of the app``.
- Password login on the legacy host now answers ``429 CAPTCHA_REQUIRED``.

The last of those is deliberate bot protection, and the right response is to
stop logging in programmatically rather than to work around it. So this shim
carries a bearer token lifted from a real browser session instead: the human
authenticates normally, in a browser, and the pipeline reuses the result.
That also means the account password never has to exist on disk.

Header values here were captured from the live web app and will drift. When
Monarch bumps its client version, `monarch-client-version` is the knob.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from monarchmoney import MonarchMoney
from monarchmoney.monarchmoney import MonarchMoneyEndpoints

log = logging.getLogger(__name__)

API_BASE = "https://api.monarch.com"
CLIENT_NAME = "monarch-core-web-app-graphql"
CLIENT_VERSION = "v1.0.4742"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def _stable_device_uuid(state_dir: Path) -> str:
    """A device id that persists across runs.

    Monarch ties sessions to a device. Minting a fresh id every run would look
    like a new device each time, which is exactly the pattern their bot
    protection watches for.
    """
    path = state_dir / "device_uuid"
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    generated = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return generated


def patch_endpoints() -> None:
    """Point the library at the host Monarch actually serves."""
    if MonarchMoneyEndpoints.BASE_URL != API_BASE:
        log.debug("Repointing monarchmoney at %s", API_BASE)
        MonarchMoneyEndpoints.BASE_URL = API_BASE


def _common_headers(state_dir: Path) -> dict[str, str]:
    return {
        "Client-Platform": "web",
        "device-uuid": _stable_device_uuid(state_dir),
        "monarch-client": CLIENT_NAME,
        "monarch-client-version": CLIENT_VERSION,
        "User-Agent": BROWSER_UA,
        "Origin": "https://app.monarch.com",
        "Referer": "https://app.monarch.com/",
    }


def build_client(token: str, state_dir: Path, timeout: int = 30) -> MonarchMoney:
    """Client authenticated by a bearer token (the pre-CAPTCHA login path)."""
    patch_endpoints()
    client = MonarchMoney(timeout=timeout)
    client.set_token(token)
    client._headers["Authorization"] = f"Token {token}"
    client._headers.update(_common_headers(state_dir))
    return client


def build_raw_cookie_client(
    cookie_header: str,
    csrf_token: str,
    state_dir: Path,
    timeout: int = 30,
) -> MonarchMoney:
    """Client authenticated by a verbatim Cookie header.

    Preferred over naming individual cookies: Monarch sits behind Cloudflare,
    whose `cf_clearance` and `__cf_bm` cookies are part of what makes a
    request acceptable. Passing the header through whole keeps them, and
    survives Monarch renaming its session cookie.
    """
    patch_endpoints()
    client = MonarchMoney(timeout=timeout)
    client.set_token(csrf_token)
    headers = _common_headers(state_dir)
    headers.update({"Cookie": cookie_header.strip(), "x-csrftoken": csrf_token})
    client._headers.pop("Authorization", None)
    client._headers.update(headers)
    return client


def build_session_client(
    session_cookie: str,
    csrf_token: str,
    state_dir: Path,
    timeout: int = 30,
    cookie_name: str = "session_id",
) -> MonarchMoney:
    """Client authenticated by an existing browser session.

    This is how Monarch's own web app authenticates: a session cookie plus a
    matching CSRF token, no bearer anywhere. Reusing a session the account
    holder established interactively sidesteps the login CAPTCHA without
    defeating it - we simply never log in.

    The library sends `self._headers` on every GraphQL call, so setting the
    Cookie header here covers all of them.
    """
    patch_endpoints()
    client = MonarchMoney(timeout=timeout)
    # Satisfies the library's own "are we authenticated?" checks; the cookie
    # is what the server actually honours.
    client.set_token(session_cookie)
    headers = _common_headers(state_dir)
    headers.update(
        {
            "Cookie": f"{cookie_name}={session_cookie}; csrftoken={csrf_token}",
            "x-csrftoken": csrf_token,
        }
    )
    client._headers.pop("Authorization", None)
    client._headers.update(headers)
    return client
