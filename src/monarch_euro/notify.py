"""Failure notifications over Telegram.

On a laptop a failed sync is obvious - you ran it, you saw it fail. On a
timer-driven VPS it is silent: the job fails at 07:30 and the first symptom
is missing transactions weeks later. That matters more here than for most
jobs, because the two most likely failures are both expected and recoverable
(a lapsed bank consent, an expired Monarch session) and both need a human
within a day or two.

Notification never raises. A pipeline that fell over because it could not
report falling over would be worse than one that stayed quiet.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_MESSAGE = 3500


def send(token: str, chat_id: str, text: str, timeout: float = 15.0) -> bool:
    """Send a Telegram message. Returns whether it was delivered."""
    if not token or not chat_id:
        log.debug("Telegram not configured; skipping notification")
        return False

    body = text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 3] + "..."
    try:
        response = httpx.post(
            f"{API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        log.warning("Telegram notification failed to send: %s", exc)
        return False

    if response.status_code != 200:
        log.warning(
            "Telegram rejected the notification (%s): %s",
            response.status_code,
            response.text[:200],
        )
        return False
    return True


def format_failure(host: str, errors: list[str], fetched: int, posted: int) -> str:
    """A message that says what broke and what to do about it."""
    lines = [
        "<b>monarch-euro sync failed</b>",
        f"<i>{host}</i>",
        "",
        f"fetched {fetched} · posted {posted} · {len(errors)} error(s)",
        "",
    ]
    for error in errors[:5]:
        lines.append(f"• {_escape(error[:400])}")
    if len(errors) > 5:
        lines.append(f"• …and {len(errors) - 5} more")

    hint = _hint(errors)
    if hint:
        lines += ["", f"<b>{hint}</b>"]
    return "\n".join(lines)


def _hint(errors: list[str]) -> str | None:
    """Name the fix for the failures we expect to recur.

    Order matters: the specific checks come before the generic ones. A Wise
    token failure is also a 401, and used to be reported as an expired
    Monarch session.
    """
    joined = " ".join(errors).lower()
    if "wise personal token" in joined or ("[wise]" in joined and "401" in joined):
        return ("Wise token invalid or revoked - make a new one in Wise, then: "
                "monarch-euro set-secret WISE_TOKEN --host <sync host>")
    if "consent" in joined or "re-run" in joined and "link" in joined:
        return "Bank consent lapsed - run: monarch-euro link n26"
    if "csrf failed" in joined or "referer" in joined:
        return ("CSRF rejected - the stored token no longer matches the session. Refresh it: "
                "pbpaste | ssh <sync host> '... monarch-euro monarch-cookie' (RUNBOOK)")
    if ("captcha" in joined or "authentication credentials" in joined
            or "unauthorized" in joined or "401" in joined):
        return ("Monarch session expired - refresh it: "
                "pbpaste | ssh <sync host> '... monarch-euro monarch-cookie' (RUNBOOK)")
    if "rate limit" in joined or "429" in joined:
        return "Bank quota exhausted - resets within 24h, no action needed"
    if "not active" in joined:
        return "Enable Banking application is inactive - re-link in the Control Panel"
    return None


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
