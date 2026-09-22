"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .categorize import write_default_rules
from .notify import format_failure, send
from .config import Config, ConfigError, load_config
from .envfile import EnvFileError, parse_cookie_header, update as env_update
from .pipeline import _rules_path, refresh_accounts, sync, with_stable_keys
from .sinks.csvfile import CsvSink
from .sinks.monarch import MonarchSink
from .sources.enablebanking import EnableBankingClient, extract_accounts
from .sources.wise import WiseClient, WiseError, WiseSCARequired
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # The transport libraries are chatty at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("gql").setLevel(logging.WARNING)


def _client(config: Config) -> EnableBankingClient:
    return EnableBankingClient(
        application_id=config.eb_application_id,
        private_key_path=config.eb_private_key_path,
        redirect_url=config.eb_redirect_url,
        base_url=config.eb_base_url,
    )


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    ok = True

    print("Configuration")
    print(f"  state dir         {config.state_dir}")
    print(f"  target currency   {config.target_currency}")
    print(f"  lookback days     {config.lookback_days}")
    print(f"  account links     {len(config.links)}")
    for link in config.links:
        print(f"    - {link.key}: {link.aspsp_name} ({link.aspsp_country}) "
              f"-> Monarch {link.monarch_account_name!r}")
    if not config.links:
        print("    (none - set ACCOUNT_LINKS)")
        ok = False

    print("\nEnable Banking")
    try:
        with _client(config) as client:
            app = client.application()
            print(f"  OK  application {app.get('name', '?')} "
                  f"({len(app.get('redirect_urls', []))} redirect urls registered)")
            if config.eb_redirect_url not in (app.get("redirect_urls") or []):
                print(f"  WARN  EB_REDIRECT_URL {config.eb_redirect_url!r} is not in the "
                      f"application's registered redirect URLs: {app.get('redirect_urls')}")
    except Exception as exc:
        print(f"  FAIL  {exc}")
        ok = False

    print("\nWise")
    if not config.wise_accounts:
        print("  skipped (WISE_ACCOUNTS not set)")
    elif not config.wise_token:
        print("  FAIL  WISE_ACCOUNTS is set but WISE_TOKEN is empty")
        ok = False
    else:
        try:
            with WiseClient(
                token=config.wise_token, private_key_path=config.wise_private_key_path
            ) as wise:
                profiles = wise.profiles()
                profile_id = config.wise_profile_id or next(
                    (str(p.get("id")) for p in profiles
                     if str(p.get("type", "")).lower() == "personal"),
                    str(profiles[0].get("id")),
                )
                chosen = next((p for p in profiles if str(p.get("id")) == str(profile_id)), None)
                if chosen is None:
                    print(f"  FAIL  WISE_PROFILE_ID {profile_id} not on this token "
                          f"(have {', '.join(str(p.get('id')) for p in profiles)})")
                    ok = False
                else:
                    print(f"  OK  profile {profile_id} ({chosen.get('type')})"
                          f"{'  [pinned]' if config.wise_profile_id else '  [auto-selected]'}")
                    balances = {str(b.get("currency", "")).upper(): b
                                for b in wise.balances(profile_id)}
                    for account in config.wise_accounts:
                        b = balances.get(account.currency)
                        if b is None:
                            print(f"    - {account.currency}: MISSING on this profile "
                                  f"(have: {', '.join(sorted(balances)) or 'none'})")
                            ok = False
                        else:
                            amount = (b.get("amount") or {}).get("value")
                            print(f"    - {account.currency}: balance {amount} "
                                  f"-> Monarch {account.monarch_account_name!r}")
        except Exception as exc:
            print(f"  FAIL  {exc}")
            ok = False

    print("\nMonarch")
    try:
        with MonarchSink(
            email=config.monarch_email,
            password=config.monarch_password,
            mfa_secret=config.monarch_mfa_secret,
            session_path=config.monarch_session_path,
            token=config.monarch_token,
            session_cookie=config.monarch_session_cookie,
            csrf_token=config.monarch_csrf_token,
            cookie_name=config.monarch_cookie_name,
            cookie_header=config.monarch_cookie_header,
            dry_run=True,
        ) as monarch:
            monarch.refresh_metadata()
            print(f"  OK  {len(monarch.account_names())} accounts, "
                  f"{len(monarch.category_names())} categories")

            from .categorize import Categorizer, load_rules
            from .pipeline import _rules_path

            categorizer = Categorizer(load_rules(_rules_path(config)))
            available = set(monarch.category_names())
            unknown = categorizer.unknown_categories(available)
            if unknown:
                ok = False
                print(f"\n  WARN  {len(unknown)} rule category(ies) do not exist in "
                      f"Monarch. Rules naming them will silently fall back to "
                      f"{monarch.default_category!r}:")
                for name in sorted(unknown):
                    print(f"          - {name!r}")
                print("        Fix them in state/rules.json, or create the categories "
                      "in Monarch.")
            else:
                print(f"    all {len(categorizer.referenced_categories())} rule "
                      f"categories exist in Monarch")

            print("\n  Your Monarch categories:")
            for name in monarch.category_names():
                print(f"    {name}")
            for link in config.links:
                marker = "found" if link.monarch_account_name in monarch.account_names() else \
                    "missing (will be created on first sync)"
                print(f"    - {link.monarch_account_name!r}: {marker}")
    except Exception as exc:
        print(f"  FAIL  {exc}")
        ok = False

    print("\n" + ("All checks passed." if ok else "Some checks failed - see above."))
    return 0 if ok else 1


def cmd_banks(config: Config, args: argparse.Namespace) -> int:
    with _client(config) as client:
        aspsps = client.aspsps(country=args.country)
    if args.search:
        needle = args.search.lower()
        aspsps = [a for a in aspsps if needle in (a.get("name") or "").lower()]
    if not aspsps:
        print("No matching banks.")
        return 1
    for aspsp in sorted(aspsps, key=lambda a: (a.get("country", ""), a.get("name", ""))):
        psu = ",".join(aspsp.get("psu_types") or [])
        print(f"{aspsp.get('country','??'):<3} {aspsp.get('name','?'):<40} {psu}")
    print(f"\n{len(aspsps)} bank(s). Use the exact name in ACCOUNT_LINKS.")
    return 0


def cmd_link(config: Config, args: argparse.Namespace) -> int:
    """Authorize one bank.

    Runs in two steps so it works without a TTY - on a VPS over SSH, or from
    an agent - as well as interactively. Without --code it prints the
    authorization URL; with --code it exchanges the result for a session.
    """
    link = next((l for l in config.links if l.key == args.key), None)
    if link is None:
        print(f"No ACCOUNT_LINKS entry with key {args.key!r}. "
              f"Known keys: {[l.key for l in config.links]}")
        return 1

    with _client(config) as client:
        code = args.code
        if code and code.startswith("http"):
            code = _code_from_url(code)
            if code is None:
                print("That URL has no `code` parameter.")
                return 1

        if not code:
            url, state = client.start_authorization(
                aspsp_name=link.aspsp_name,
                aspsp_country=link.aspsp_country,
                valid_days=args.valid_days,
            )
            print("\nOpen this URL in a browser and complete the bank's login:\n")
            print(f"  {url}\n")
            print("You will be redirected to a URL containing a `code` parameter.")

            if not sys.stdin.isatty():
                print(f"\nThen finish with:\n  monarch-euro link {args.key} --code '<redirect URL>'")
                return 0

            print("Paste the full redirect URL (or just the code) below.\n")
            pasted = input("redirect URL or code: ").strip()
            if not pasted:
                print("Nothing pasted; aborting.")
                return 1
            code = pasted
            if pasted.startswith("http"):
                code = _code_from_url(pasted)
                if code is None:
                    print("That URL has no `code` parameter.")
                    return 1

        payload = client.create_session(code)
        session_id = payload.get("session_id")
        if not session_id:
            print(f"No session_id returned: {payload}")
            return 1

        accounts = extract_accounts(payload)
        with Store(config.db_path) as store:
            store.put_session(
                link_key=link.key,
                session_id=session_id,
                aspsp_name=link.aspsp_name,
                aspsp_country=link.aspsp_country,
                valid_until=(payload.get("access") or {}).get("valid_until"),
            )
            for account in accounts:
                store.put_account(
                    account_uid=account["uid"],
                    link_key=link.key,
                    identifier=account.get("identifier"),
                    name=account.get("name"),
                    currency=account.get("currency"),
                    monarch_account_id=None,
                    monarch_account_name=link.monarch_account_name,
                    identity=account.get("identity"),
                )
            # A re-link replaces the previous session's uids; keeping them
            # would have the sync query accounts whose session is gone.
            store.retain_accounts(link.key, {a["uid"] for a in accounts})

    print(f"\nLinked {link.key} -> session {session_id}")
    for account in accounts:
        print(f"  account {account['uid']}  {account.get('identifier') or ''} "
              f"{account.get('currency') or ''}")
    print("\nRun `monarch-euro sync` to import transactions.")
    return 0


def _code_from_url(pasted: str) -> str | None:
    query = parse_qs(urlparse(pasted).query)
    values = query.get("code")
    return values[0] if values else None


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    import socket

    host = socket.gethostname()
    try:
        result = sync(config)
    except Exception as exc:
        # An unhandled crash is exactly the case a silent timer would hide.
        send(config.telegram_bot_token, config.telegram_chat_id,
             format_failure(host, [f"{type(exc).__name__}: {exc}"], 0, 0))
        raise

    print(result.summary())
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)

    if result.errors:
        send(config.telegram_bot_token, config.telegram_chat_id,
             format_failure(host, result.errors, result.fetched, result.posted))
    elif config.notify_on_success and result.posted:
        send(config.telegram_bot_token, config.telegram_chat_id,
             f"<b>monarch-euro</b> posted {result.posted} transaction(s) on {host}")

    return 0 if result.ok else 1


def cmd_notify_test(config: Config, args: argparse.Namespace) -> int:
    import socket

    if not config.telegram_bot_token or not config.telegram_chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return 2
    ok = send(
        config.telegram_bot_token,
        config.telegram_chat_id,
        format_failure(socket.gethostname(),
                       ["This is a test. Bank consent lapsed - run: monarch-euro link n26"],
                       fetched=12, posted=0),
    )
    print("Sent." if ok else "Failed to send - check the token and chat id.")
    return 0 if ok else 1


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    with Store(config.db_path) as store:
        sessions = store.all_sessions()
        print(f"Bank sessions ({len(sessions)})")
        if not sessions:
            print("  none - run `monarch-euro link <key>`")
        for row in sessions:
            valid_until = row["valid_until"] or "unknown"
            warning = ""
            if row["valid_until"]:
                try:
                    expiry = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
                    days = (expiry - datetime.now(expiry.tzinfo)).days
                    warning = f"  ({days} days left)" if days > 7 else \
                        f"  ** RE-LINK SOON: {days} days left **"
                except ValueError:
                    pass
            print(f"  {row['link_key']:<12} {row['aspsp_name']} ({row['aspsp_country']})  "
                  f"expires {valid_until}{warning}")

        accounts = store.all_accounts()
        print(f"\nMapped accounts ({len(accounts)})")
        for row in accounts:
            print(f"  {row['link_key']:<12} {row['account_uid'][:20]:<20} "
                  f"{row['currency'] or '???':<4} -> {row['monarch_account_name'] or '(unmapped)'}")

        print(f"\nTransactions posted to date: {store.posted_count()}")

        pending = store.pending_writes()
        print(f"\nUncertain Monarch writes ({len(pending)})")
        for row in pending:
            print(f"  {row['dedupe_key']}  {row['booked_on']} "
                  f"{row['amount']} {row['currency']}  {row['merchant']} "
                  f"account={row['account_id']}")
        if pending:
            print("  Review these in Monarch, then use resolve-write. See RUNBOOK.md.")

        runs = store.recent_runs(limit=args.runs)
        print(f"\nRecent runs ({len(runs)})")
        for row in runs:
            print(f"  #{row['id']:<5} {row['started_at']}  {row['status']:<8} "
                  f"fetched={row['fetched']} posted={row['posted']} skipped={row['skipped']}")
            if row["error"]:
                print(f"         {row['error'][:160]}")
    return 0


def cmd_resolve_write(config: Config, args: argparse.Namespace) -> int:
    with Store(config.db_path) as store, store.exclusive():
        try:
            store.resolve_write(args.key, args.monarch_id)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    print("Write recorded as posted." if args.monarch_id else "Write released for the next sync.")
    return 0


def cmd_confirm_export(config: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    with Store(config.db_path) as store, store.exclusive():
        try:
            count = store.confirm_export(Path(args.path))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    print(f"Recorded {count} manually imported transaction(s).")
    return 0


def cmd_rules_init(config: Config, args: argparse.Namespace) -> int:
    path = _rules_path(config)
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite.")
        return 1
    write_default_rules(path)
    print(f"Wrote starter rules to {path}. Edit it to taste; no restart needed.")
    return 0


def cmd_unlink(config: Config, args: argparse.Namespace) -> int:
    with Store(config.db_path) as store:
        row = store.get_session(args.key)
        if row is None:
            print(f"No session for {args.key!r}.")
            return 1
        try:
            with _client(config) as client:
                client.delete_session(row["session_id"])
        except Exception as exc:
            print(f"Warning: could not delete remote session: {exc}")
        store.delete_session(args.key)
    print(f"Unlinked {args.key}. The dedupe ledger is preserved, so re-linking "
          f"will not re-import old transactions.")
    return 0


def cmd_wise_probe(config: Config, args: argparse.Namespace) -> int:
    """Enumerate the Wise profile and its balances, and report whether SCA applies.

    Whether a profile needs SCA-signed requests depends on where it is
    registered, not on anything we can configure, so the only reliable answer
    comes from asking Wise.
    """
    if not config.wise_token:
        print("WISE_TOKEN is not set. Generate a personal token in Wise under "
              "Settings -> API tokens (needs 2-step login enabled).")
        return 2

    with WiseClient(
        token=config.wise_token, private_key_path=config.wise_private_key_path
    ) as wise:
        try:
            profiles = wise.profiles()
        except WiseError as exc:
            print(f"Could not list profiles: {exc}")
            return 1

        print(f"Profiles ({len(profiles)})")
        for profile in profiles:
            print(f"  id={profile.get('id')}  type={profile.get('type')}")

        personal = next(
            (p for p in profiles if str(p.get("type", "")).lower() == "personal"),
            profiles[0],
        )
        profile_id = personal.get("id")

        try:
            balances = wise.balances(profile_id)
        except WiseError as exc:
            print(f"Could not list balances: {exc}")
            return 1

        print(f"\nBalances on profile {profile_id} ({len(balances)})")
        for balance in balances:
            amount = (balance.get("amount") or {}).get("value")
            print(f"  {str(balance.get('currency','?')):<5} id={balance.get('id')}  "
                  f"balance={amount}")

        # The real question: can we read a statement, and did SCA get involved?
        print("\nStatement access")
        end = date.today()
        start = end - timedelta(days=args.days)
        for balance in balances:
            currency = str(balance.get("currency", "")).upper()
            if args.currency and currency != args.currency.upper():
                continue
            try:
                payload = wise.statement(profile_id, balance.get("id"), currency, start, end)
            except WiseSCARequired as exc:
                print(f"  {currency:<5} SCA REQUIRED - {exc}")
                continue
            except WiseError as exc:
                print(f"  {currency:<5} FAILED - {exc}")
                continue
            count = len(payload.get("transactions") or [])
            print(f"  {currency:<5} OK - {count} transactions in the last {args.days} days")

            for raw in (payload.get("transactions") or [])[: args.sample]:
                txn = WiseClient._normalize("wise", "probe", raw, currency)
                if txn:
                    print(f"          {txn.booked_on}  {txn.amount:>10.2f} {txn.currency}  "
                          f"{(txn.counterparty or txn.description)[:40]}")
    return 0


def cmd_monarch_login(config: Config, args: argparse.Namespace) -> int:
    """Establish a Monarch session without storing a TOTP seed."""
    from .sinks.monarch import MonarchError

    with MonarchSink(
        email=config.monarch_email,
        password=config.monarch_password,
        mfa_secret=config.monarch_mfa_secret,
        session_path=config.monarch_session_path,
        dry_run=True,
    ) as monarch:
        try:
            monarch.interactive_login(mfa_code=args.code)
        except MonarchError as exc:
            print(str(exc))
            return 1
        monarch.refresh_metadata()
        print(f"Signed in. Session saved to {config.monarch_session_path}")
        print(f"  {len(monarch.account_names())} accounts, "
              f"{len(monarch.category_names())} categories visible")
        print("\nThe session persists across runs, so MONARCH_MFA_SECRET can stay empty.")
    return 0


def cmd_export(config: Config, args: argparse.Namespace) -> int:
    """Fetch, convert and categorize, then write a Monarch-importable CSV.

    Everything except the final upload runs unattended; Monarch's CAPTCHA
    only blocks the write, not the work.
    """
    from .categorize import Categorizer, load_rules, transaction_code
    from .fx import FxConverter
    from .pipeline import _rules_path
    from .sources.wise import WiseClient
    from datetime import timedelta

    date_from = date.today() - timedelta(days=args.days)
    categorizer = Categorizer(load_rules(_rules_path(config)))
    sink = CsvSink(config.state_dir / "exports")
    fetched = skipped = 0

    exported = []
    seen_keys = set()
    with Store(config.db_path) as store, store.exclusive(), FxConverter(
        store, config.target_currency, base_url=config.fx_base_url
    ) as fx:
        if store.pending_writes():
            raise RuntimeError("Resolve uncertain Monarch writes before exporting. See RUNBOOK.md.")
        batches: list[tuple[str, str, list]] = []

        problems: list[str] = []

        for session_row in store.all_sessions():
            link = next((l for l in config.links if l.key == session_row["link_key"]), None)
            if link is None:
                continue
            # One bank being unavailable must not discard the others' data.
            try:
                with _client(config) as eb:
                    # Same account identities and keys as sync, or an export
                    # would disagree with the ledger about what is imported.
                    refresh_accounts(eb, store, link.key, session_row["session_id"])
                    for account in store.accounts_for(link.key):
                        txns = eb.transactions(
                            link.key, account["account_uid"], date_from,
                            include_pending=config.include_pending,
                        )
                        txns = with_stable_keys(store, link.key, account, txns)
                        batches.append((link.key, link.monarch_account_name, txns))
            except Exception as exc:
                problems.append(f"[{link.key}] {exc}")

        if config.wise_accounts and not config.wise_token:
            problems.append("[wise] WISE_TOKEN is empty. No Wise transactions were exported.")
        if config.wise_accounts and config.wise_token:
          try:
            with WiseClient(
                token=config.wise_token, private_key_path=config.wise_private_key_path
            ) as wise:
                profiles = wise.profiles()
                pid = config.wise_profile_id or str(profiles[0].get("id"))
                balances = {str(b.get("currency","")).upper(): b for b in wise.balances(pid)}
                for wa in config.wise_accounts:
                    b = balances.get(wa.currency)
                    if b is None:
                        print(f"  warn: no {wa.currency} balance on Wise profile {pid}")
                        continue
                    txns = wise.transactions(
                        f"wise-{wa.currency.lower()}", pid, b.get("id"), wa.currency, date_from
                    )
                    batches.append((f"wise-{wa.currency.lower()}", wa.monarch_account_name, txns))
          except Exception as exc:
            problems.append(f"[wise] {exc}")

        for _key, account_name, txns in batches:
            fetched += len(txns)
            if not txns:
                continue
            if args.new_only:
                keys = [t.dedupe_key() for t in txns]
                new_keys = store.filter_unexported(keys)
                fresh = [t for t, k in zip(txns, keys) if k in new_keys]
                skipped += len(txns) - len(fresh)
            else:
                fresh = txns
            if not fresh:
                continue
            before_dedupe = len(fresh)
            fresh = list({t.dedupe_key(): t for t in fresh
                          if t.dedupe_key() not in seen_keys}.values())
            skipped += before_dedupe - len(fresh)
            seen_keys.update(t.dedupe_key() for t in fresh)
            fx.prefetch(fresh)
            for txn in fresh:
                converted = fx.convert(txn)
                merchant, category = categorizer.apply(
                    txn.description, txn.counterparty, transaction_code(txn.raw)
                )
                sink.add(converted, account_name, merchant, category,
                         converted.note(config.note_original_amount))
                exported.append(converted)

        path = sink.write()
        if args.mark and path:
            store.mark_exported(exported, path)
    print(f"fetched={fetched} written={len(sink)} skipped(already exported)={skipped}"
          + (f" errors={len(problems)}" if problems else ""))
    for problem in problems:
        print(f"  WARN {problem}", file=sys.stderr)
    if path:
        print(f"\n{path}")
        print("\nUpload at app.monarch.com -> Settings -> Data -> Import transactions")
        if args.mark:
            print(f"After a complete upload, run: monarch-euro confirm-export {path}")
        if not args.mark:
            print("NOT marked as exported (--mark to record them and avoid duplicates next time)")
    else:
        print("Nothing new to export."
              + (" Use --all to export the whole window regardless of the ledger."
                 if not args.new_only is False else ""))
    return 1 if problems else 0


def cmd_monarch_cookie(config: Config, args: argparse.Namespace) -> int:
    """Refresh the Monarch session from a pasted Cookie header.

    Validated against the live API before anything is written, so a bad paste
    leaves a working configuration untouched rather than replacing it with
    something that fails at 07:30 tomorrow.
    """
    from pathlib import Path

    from .sinks.monarch import MonarchError, MonarchSink

    raw = args.cookie
    if not raw:
        if sys.stdin.isatty():
            print("Paste the Cookie header from DevTools -> Network -> any")
            print("`graphql` request -> Request Headers -> Cookie, then press Enter:\n")
        raw = sys.stdin.readline()
    if not raw or not raw.strip():
        print("Nothing pasted.")
        return 1

    cookies = parse_cookie_header(raw)
    session = cookies.get("session_id") or cookies.get("sessionid")
    csrf = cookies.get("csrftoken")

    if not session or not csrf:
        print("That does not look like a Monarch Cookie header.")
        print(f"  found: {', '.join(sorted(cookies)) or 'nothing'}")
        print("  need:  session_id and csrftoken")
        return 1

    cookie_name = "session_id" if "session_id" in cookies else "sessionid"
    header = f"{cookie_name}={session}; csrftoken={csrf}"

    print(f"Parsed {len(cookies)} cookie(s); keeping {cookie_name} and csrftoken.")
    print("Checking them against Monarch before writing...")

    with MonarchSink(
        email="", password="", mfa_secret="",
        session_path=config.monarch_session_path,
        cookie_header=header, csrf_token=csrf, dry_run=True,
    ) as monarch:
        try:
            monarch.refresh_metadata()
        except Exception as exc:
            print(f"\nREJECTED - {exc}")
            print("Nothing was written; your existing configuration is untouched.")
            return 1
        accounts = len(monarch.account_names())

    env_path = Path(args.env)
    try:
        backup = env_update(
            env_path,
            {
                "MONARCH_COOKIE_HEADER": header,
                "MONARCH_CSRF_TOKEN": csrf,
                "MONARCH_COOKIE_NAME": cookie_name,
            },
        )
    except EnvFileError as exc:
        print(f"\nCould not update {env_path}: {exc}")
        return 1

    print(f"\nAccepted - {accounts} accounts visible.")
    print(f"Wrote {env_path}" + (f" (backup: {backup.name})" if backup else ""))
    return 0


def cmd_set_secret(config: Config, args: argparse.Namespace) -> int:
    """Set one secret in .env here and, optionally, on the sync host too.

    Rotating a credential otherwise means editing the same value in two
    places by hand, which is both tedious and easy to get half-done.
    """
    import subprocess
    from pathlib import Path

    key = args.key.upper()
    value = args.value
    if value is None:
        if sys.stdin.isatty():
            print(f"Paste the new value for {key}, then press Enter:\n")
        value = sys.stdin.readline().strip()
    if not value:
        print("Nothing given; aborting.")
        return 1

    env_path = Path(args.env)
    try:
        backup = env_update(env_path, {key: value})
    except EnvFileError as exc:
        print(f"Could not update {env_path}: {exc}")
        return 1
    print(f"{key} set in {env_path}" + (f"  (backup: {backup})" if backup else ""))

    if not args.host:
        return 0

    # Update the remote in place rather than copying the whole file, so local
    # settings (DRY_RUN in particular) never leak onto the sync host.
    remote_cmd = (
        f"cd {args.remote_path} && "
        f"sudo -u {args.remote_user} {args.remote_path}/.venv/bin/monarch-euro "
        f"set-secret {key}"
    )
    try:
        completed = subprocess.run(
            ["ssh", args.host, remote_cmd],
            input=value + "\n", text=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Could not reach {args.host}: {exc}")
        return 1

    output = (completed.stdout + completed.stderr).strip().splitlines()
    for line in output[-3:]:
        print(f"  {args.host}: {line}")
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monarch-euro",
        description="Import N26 / Revolut / Wise transactions into Monarch Money.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--env", default=".env", help="path to .env file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="verify configuration and connectivity")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("banks", help="list banks available via Enable Banking")
    p.add_argument("--country", help="ISO country code, e.g. DE")
    p.add_argument("--search", help="filter by name substring")
    p.set_defaults(func=cmd_banks)

    p = sub.add_parser("link", help="authorize one bank (opens a browser flow)")
    p.add_argument("key", help="ACCOUNT_LINKS key, e.g. n26")
    p.add_argument("--valid-days", type=int, default=180,
                   help="consent lifetime; PSD2 caps this at 180 for most banks")
    p.add_argument("--code", help="the redirect URL or its `code`, to finish a started link")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="close a bank session")
    p.add_argument("key")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("sync", help="fetch, convert and push transactions")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("export", help="write a Monarch-importable CSV")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--new-only", action="store_true", default=True,
                   help="skip transactions already exported (default)")
    p.add_argument("--all", dest="new_only", action="store_false",
                   help="export the whole window regardless of the ledger")
    p.add_argument("--mark", action="store_true", default=True,
                   help="record exported rows so they are not exported twice (default)")
    p.add_argument("--no-mark", dest="mark", action="store_false")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("monarch-cookie",
                       help="refresh the Monarch session from a pasted Cookie header")
    p.add_argument("--cookie", help="the Cookie header; omit to read from stdin")
    p.set_defaults(func=cmd_monarch_cookie)

    p = sub.add_parser("set-secret",
                       help="set one secret in .env, here and optionally on the sync host")
    p.add_argument("key", help="e.g. TELEGRAM_BOT_TOKEN, WISE_TOKEN")
    p.add_argument("--value", help="the value; omit to read from stdin")
    p.add_argument("--host", help="also update this ssh host, e.g. root@167.99.150.73")
    p.add_argument("--remote-path", default="/opt/monarch-euro")
    p.add_argument("--remote-user", default="monarch")
    p.set_defaults(func=cmd_set_secret)

    p = sub.add_parser("notify-test", help="send a sample failure notification")
    p.set_defaults(func=cmd_notify_test)

    p = sub.add_parser("status", help="show sessions, accounts and recent runs")
    p.add_argument("--runs", type=int, default=10)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("monarch-login",
                       help="sign in to Monarch once and save the session (no TOTP seed needed)")
    p.add_argument("--code", help="current 6-digit code from your authenticator app")
    p.set_defaults(func=cmd_monarch_login)

    p = sub.add_parser("wise-probe", help="list Wise profiles/balances and test statement access")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--currency", help="only probe this currency")
    p.add_argument("--sample", type=int, default=3, help="sample transactions to print")
    p.set_defaults(func=cmd_wise_probe)

    p = sub.add_parser("resolve-write", help="resolve an uncertain write after review in Monarch")
    p.add_argument("key", help="dedupe key from status")
    resolution = p.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--monarch-id", help="ID of the transaction found in Monarch")
    resolution.add_argument("--retry", action="store_true",
                            help="allow retry after confirming the transaction is absent")
    p.set_defaults(func=cmd_resolve_write)

    p = sub.add_parser("confirm-export", help="record a completed manual CSV import")
    p.add_argument("path", help="original path of the completely imported export")
    p.set_defaults(func=cmd_confirm_export)

    p = sub.add_parser("rules-init", help="write a starter rules.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_rules_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        from pathlib import Path

        config = load_config(Path(args.env))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        return int(args.func(config, args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
