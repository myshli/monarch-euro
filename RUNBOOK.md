# Runbook

What to do when the sync breaks, and the maintenance it needs on a schedule.

Deployment: `root@167.99.150.73`, code in `/opt/monarch-euro`, state in
`/var/lib/monarch-euro`, running as the `monarch` user twice a day.

---

## Start here: is it healthy?

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro status'
```

A healthy result looks like this:

```
Bank sessions (1)
  n26          N26 (DE)  expires 2027-03-19T12:43:24Z  (179 days left)
Mapped accounts (2)
Transactions posted to date: 12
Recent runs (4)
  #4  2026-09-20T12:46:57+00:00  ok  fetched=12 posted=0 skipped=12
```

Three things to read:

- **`ok` on the most recent run.** `error` means look at the run's error text.
- **`skipped`, not `posted`, on repeat runs.** Everything already imported
  should be skipped. Steady `posted` on every run would mean duplicates.
- **Days left on the consent.** Under 14, re-link before it lapses.

To check both ends of the pipeline rather than just the last run:

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro doctor'
```

Logs, when you need detail:

```bash
ssh root@167.99.150.73 'journalctl -u monarch-euro -n 80 --no-pager'
```

---

## Failures, by what the alert says

Every failure sends a Telegram message naming the fix. Find it below.

### "Monarch session expired" / "CSRF rejected"

**The most common failure.** The browser session behind the sync has a fixed
lifetime and does not extend with use, so it lapses eventually no matter how
often the sync runs.

1. Open [app.monarch.com](https://app.monarch.com) in Chrome, signed in
2. DevTools (⌥⌘I) → **Network** → filter `graphql` → click any row
3. **Request Headers** → copy the entire `Cookie:` line
4. With it on your clipboard:

```bash
pbpaste | ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro monarch-cookie'
```

Expect `Accepted - N accounts visible`. On `REJECTED`, nothing was written and
the old configuration still stands — copy the cookie again, making sure you
took the whole line and that you are signed in.

Ticking **Stay signed in** when you log in to Monarch usually buys a longer
session, so this comes round less often.

### "Bank consent lapsed"

PSD2 caps bank access at 180 days. This is law, not a bug, and it is the one
piece of maintenance that cannot be automated away.

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro link n26'
```

It prints a URL. Open it, complete the N26 login, and you land on a
`https://localhost/callback?code=...` page that does not load — that is
expected. Copy the whole address and finish:

```bash
ssh root@167.99.150.73 "cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro link n26 --code 'PASTE_THE_WHOLE_URL'"
```

Mind the single quotes: the URL contains `&` and your shell will otherwise cut
it short.

Re-linking is safe to repeat. Every link gets new account uids from Enable
Banking, so the ledger keys N26 on the account's `identification_hash`, which
stays the same across sessions. Transactions already imported stay recognised,
and the previous session's uids are dropped.

### "Wise token invalid or revoked"

The Wise personal token stopped working, usually because it was revoked or
deleted in Wise. Make a new one in Wise under **Settings → API tokens**, then
update this machine and the sync host together:

```bash
monarch-euro set-secret WISE_TOKEN --host root@167.99.150.73
```

### "Bank quota exhausted - resets within 24h, no action needed"

Do nothing. PSD2 allows about four unattended calls per day per bank; the next
scheduled run will succeed. You will only see this if something ran the sync
repeatedly by hand.

### "Enable Banking application is inactive"

The application lost its activation, which happens if every linked account is
unlinked. Go to the [Control Panel](https://enablebanking.com/cp/), open the
`monarch-euro` application, and use **Activate by linking accounts**. Then
re-link as under *Bank consent lapsed*.

### "monarch-euro failed to start"

This comes from systemd, not the sync, and means the process died before it
could report anything itself — a broken virtualenv, an unreadable `.env`, a
bad deploy.

```bash
ssh root@167.99.150.73 'journalctl -u monarch-euro -n 50 --no-pager'
ssh root@167.99.150.73 'ls -l /opt/monarch-euro/.env /opt/monarch-euro/secrets/'
```

`.env` must be `-rw-------` and owned by `monarch`. If ownership drifted:

```bash
ssh root@167.99.150.73 'chown -R monarch:monarch /opt/monarch-euro && chmod 600 /opt/monarch-euro/.env && chmod 700 /opt/monarch-euro/secrets && chmod 600 /opt/monarch-euro/secrets/*.pem'
```

### "Please update to the latest version of the app"

Monarch shipped a new web client and now rejects the version this sends. Get
the current one from a browser: DevTools → Network → any `graphql` request →
Request Headers → `monarch-client-version`. Then edit `CLIENT_VERSION` in
`src/monarch_euro/sinks/monarch_compat.py`, commit, and redeploy.

### Silence, but no new transactions appear

Silence means the run succeeded, so the question is whether it found anything.

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro status'
```

If `fetched=0` for an account that should have activity, its session is
missing — sessions live on the host that created them and do not travel.
Re-link as above. A configured bank with no session is reported as an error,
so this should reach you by Telegram rather than as silence.

### Duplicate transactions in Monarch

The ledger identifies transactions by source reference or content hash with an
occurrence ordinal. A process lock prevents overlapping runs against the same
database. Separate machines with separate databases can still create duplicates.
If duplicates occur:

1. Delete the duplicates in Monarch
2. Confirm only one machine is writing — your Mac should be `DRY_RUN=true`
3. Send me the two rows; matching duplicates means a dedupe-key bug

The likeliest cause is a second machine syncing with its own ledger.

### Uncertain Monarch writes

Before each write, sync saves its intent in SQLite. A successful response must
contain a transaction ID. A lost response or failed local commit leaves an
uncertain write. The next sync stops before any remote writes.

1. Run `monarch-euro status` on the sync host.
2. Find each listed transaction in its Monarch account by date, merchant, and amount.
3. If the transaction exists, record its Monarch ID:

   ```bash
   monarch-euro resolve-write 'DEDUPE_KEY' --monarch-id 'TRANSACTION_ID'
   ```

4. If the transaction is absent, release it for retry:

   ```bash
   monarch-euro resolve-write 'DEDUPE_KEY' --retry
   ```

5. Run `monarch-euro sync`.

The recovery command does not create or delete remote transactions. The
`--retry` option permits the next sync to send the transaction again. Recovery
uses the same process lock as sync and export.

If another process holds the lock, wait for it to finish before retrying.
After a process exits, the operating system releases its lock. The next sync
marks unfinished run records as errors.

---

## Regular maintenance

| Cadence | Task | Command |
|---|---|---|
| When alerted (weeks) | Refresh the Monarch session | `monarch-cookie` |
| **~March 2027**, then every 180 days | Re-link N26 | `link n26` |
| Occasionally | Add categorization rules | edit `rules.json` |
| Rarely | Update the code | `git pull` + reinstall |

Nothing else is scheduled. There is no log rotation to mind (journald handles
it), no database to prune, and no credential that expires silently — the two
that expire both announce themselves.

### Adding categorization rules

New merchants arrive as `Uncategorized`. Rules live in
`/var/lib/monarch-euro/rules.json`, matched in order, first match wins:

```json
[
  {"match": "rewe|edeka|lidl", "category": "Groceries"},
  {"match": "berlin metropolitan school", "category": "Child Care"},
  {"code": "^MONEY_ADDED$", "merchant": "Account Top-up", "category": "Transfer"}
]
```

- `match` is a case-insensitive regex over the merchant and description
- `code` matches the source's own transaction type, which is more reliable
  than text where the bank supplies it
- `merchant` rewrites the displayed name; omit it to keep the cleaned-up one
- The category must exist in Monarch **exactly** — `doctor` lists yours and
  warns about any rule naming one that does not

Write a starter file if there isn't one:

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro rules-init'
```

Changes take effect on the next run. They do not re-categorize transactions
already imported — fix those in Monarch directly.

### Updating the code

```bash
ssh root@167.99.150.73 'set -e; cd /opt/monarch-euro && git fetch -q origin && git reset -q --hard origin/main && .venv/bin/pip install -q . && chown -R monarch:monarch /opt/monarch-euro && chmod 600 .env && chmod 700 secrets && chmod 600 secrets/*.pem && sudo -u monarch .venv/bin/monarch-euro doctor'
```

---

## Controlling the schedule

```bash
# when does it next run?
ssh root@167.99.150.73 'systemctl list-timers monarch-euro.timer'

# run it now, without waiting
ssh root@167.99.150.73 'systemctl start monarch-euro.service && journalctl -u monarch-euro -n 30 --no-pager'

# stop it (leaves everything installed)
ssh root@167.99.150.73 'systemctl disable --now monarch-euro.timer'

# start it again
ssh root@167.99.150.73 'systemctl enable --now monarch-euro.timer'
```

Disable the timer before any work that might post twice, and while you are
away from a lapsed consent you do not want to be reminded about.

---

## When Monarch cannot be written to at all

If the session approach ever stops working — Monarch tightening their
protections, say — the pipeline still does everything except the final write.
Produce a file and upload it by hand:

```bash
# only transactions not yet sent to Monarch
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro export'

# the whole 90-day window, ignoring what the ledger already holds
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch .venv/bin/monarch-euro export --all --no-mark'
```

Each export writes a unique file under `/var/lib/monarch-euro/exports/`.
Export history changes only after the complete file is saved. Export history
is separate from the ledger of confirmed imports. Repeated exports skip rows
with an existing export file or a confirmed import.

1. Pause the sync timer before the manual upload.
2. Upload the file at **Monarch → Settings → Data → Import transactions**.
3. Make sure that Monarch imported every row.
4. On the sync host, record the completed import with the original export path:

   ```bash
   monarch-euro confirm-export /var/lib/monarch-euro/exports/monarch-import-DATE-ID.csv
   ```

5. Resume the sync timer.

If the upload is incomplete, finish or reconcile it before confirmation.
The confirmation command records every row in that export as imported.
An export alone does not stop live sync from importing those transactions.
The `--no-mark` option omits export history, so those files cannot use
`confirm-export`.

Existing ledger entries remain unchanged after this update. Older versions
used the same ledger for exports and imports. Those entries require manual
review if an older export failed or was never uploaded.

`Nothing new to export` after a successful sync is correct — everything is
already in Monarch. Use `--all --no-mark` when you want the window regardless.

If one source is unavailable (a bank's daily quota, say), the others are still
exported and the failure is reported as a `WARN` line.

---

## Backfilling older transactions

The normal window is 30 days. To reach further back, once:

```bash
ssh root@167.99.150.73 'cd /opt/monarch-euro && sudo -u monarch env LOOKBACK_DAYS=365 .venv/bin/monarch-euro sync'
```

The ledger stops anything already imported from arriving twice, so a wide
window is safe. Wise statements cap at 469 days; N26 usually returns less.

---

## Rebuilding from nothing

The state database holds bank sessions, the dedupe ledger and cached exchange
rates. Losing it means re-linking each bank, and the next sync re-imports its
whole window — **which will duplicate transactions already in Monarch**.

If you have to rebuild, delete the imported transactions in Monarch first, or
set `LOOKBACK_DAYS` to cover only the period since the loss.

```bash
# back it up
ssh root@167.99.150.73 'cp /var/lib/monarch-euro/monarch_euro.sqlite3 /var/lib/monarch-euro/backup-$(date +%F).sqlite3'
```

Worth doing before anything that touches `/var/lib/monarch-euro`.

---

## Rotating a secret

One command updates this machine and the sync host together, so a rotation
cannot end up half-applied:

```bash
monarch-euro set-secret TELEGRAM_BOT_TOKEN --host root@167.99.150.73
# paste the new value at the prompt
```

Works for any key: `WISE_TOKEN`, `TELEGRAM_CHAT_ID`, and so on. The Monarch
session has its own command, `monarch-cookie`, which also validates before
writing. Omit `--host` to change only the local file.

Backups of `.env` are written to `~/.monarch-euro/env-backups/`, outside the
repository, and only the five most recent are kept.

## Credentials, and where they live

| Secret | Location | Expires |
|---|---|---|
| Monarch session cookie | `.env` | Fixed lifetime from login |
| Monarch CSRF token | `.env` | 1 year |
| Wise personal token | `.env` | Until revoked in Wise |
| Enable Banking private key | `secrets/enablebanking.pem` | Until revoked |
| Bank consent | state database | 180 days |
| Telegram bot token | `.env` | Until revoked |

`.env` and `secrets/` are gitignored and must stay `0600`, owned by `monarch`.
Everything in them is live financial access. `monarch-cookie` writes a
timestamped backup of `.env` — delete those once the new session is confirmed,
since they hold the previous credentials.

To revoke everything at once: change the Monarch password (kills the session),
delete the Wise token in Wise, and delete the application in the Enable
Banking Control Panel.
