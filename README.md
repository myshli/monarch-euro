# monarch-euro

Imports N26, Revolut and Wise transactions into Monarch Money, converted to USD
at the ECB rate for each transaction's own booking date.

Monarch is US/Canada only and has **no multi-currency support** — every amount
is treated as dollars and summed straight into net worth and budgets. Importing
EUR raw silently corrupts every cross-account total, so this pipeline converts
on the way in and records the original amount, rate and rate date in each
transaction's note:

```
EUR -42.00 @ 1.146 ECB 2026-09-18 = USD -48.13
```

That keeps the conversion auditable, reconcilable against a bank statement, and
reversible if Monarch ever grows real currency support.

## How it works

Each account takes the route that actually reaches it. PSD2 open banking only
covers EEA-registered institutions, so a US-registered account is invisible to
an aggregator no matter what IBAN its balances carry.

```
N26 (DE)      ──→ Enable Banking (PSD2 AIS) ──┐
                                              ├─→ normalize ─→ dedupe ─→ FX ─→ Monarch
Wise (any)    ──→ Wise's own API ─────────────┘    (signs,    (SQLite)  (ECB)   manual
                  EUR + USD balances                merchant                    accounts
                                                    cleanup)
Revolut (US)  ──→ connect natively in Monarch (Plaid) — no pipeline needed
```

| Account | Route | Why |
|---|---|---|
| **N26** | Enable Banking | German account, squarely inside PSD2 |
| **Wise** | Wise's own API | A US-registered profile is out of PSD2 scope even though its EUR balance has a Belgian IBAN. Wise's own API sees every balance on the profile, so the USD one comes along for free. |
| **Revolut US** | Monarch directly | Revolut is a Plaid institution and Monarch uses Plaid, so try connecting it in Monarch first. If you later open an EU Revolut, add it to `ACCOUNT_LINKS` and it goes through Enable Banking. |

- **Dedupe.** Every run re-fetches an overlapping window, because banks revise
  and late-post rows. A SQLite ledger keyed on the source's own reference
  (`entry_reference` for Enable Banking, `referenceNumber` for Wise) with a
  content-hash fallback guarantees each transaction reaches Monarch exactly
  once. This is what makes the job safe to run on a timer.
- **Currency.** Each Wise balance maps to its own Monarch account, so the USD
  balance is never run through a conversion it does not need.
- **Sink.** Monarch publishes no supported customer API, so this uses the
  community [`monarchmoney`](https://github.com/hammem/monarchmoney) client
  against the same private GraphQL endpoint the web app uses. It is isolated
  behind a narrow interface so it can be swapped for Monarch's official MCP
  connector if that returns from its current pause.

**Ongoing effort: about two minutes every six months.** PSD2 caps bank consent
at 180 days, so a few times a year you re-approve each Enable Banking bank in a
browser. Wise personal tokens do not expire on a schedule. Everything else is
unattended.

## Setup

### 1. Enable Banking

1. Sign up at [enablebanking.com](https://enablebanking.com) and open the
   Control Panel.
2. Create an application, choosing **Restricted Production** — this is the free
   tier that permits linking your own real accounts.
3. Register a redirect URL. Nothing needs to listen on it; you copy the `code`
   out of your browser's address bar. `https://localhost/callback` is fine.
4. Download the private key it generates and save it as
   `secrets/enablebanking.pem`.

### 2. Wise

Generate a personal token in Wise under **Settings → API tokens**. You need
2-step login enabled first. Put it in `WISE_TOKEN`.

Then find out whether your profile needs SCA-signed requests — this depends on
where the profile is registered, not on anything you configure, so ask Wise:

```bash
monarch-euro wise-probe
```

It lists your profiles and balances and tries to read a statement from each.
`OK` means a plain token is enough. `SCA REQUIRED` means the profile falls
under PSD2: generate an RSA keypair, upload the public half in Wise under
Settings → API tokens → Manage public keys, and point `WISE_PRIVATE_KEY_PATH`
at the private half. The client then signs Wise's challenge automatically and
nothing else changes.

Map each balance you want to its own Monarch account in `WISE_ACCOUNTS`.

### 3. Monarch

Create the three manual accounts, or let the first sync create them for you.

`MONARCH_MFA_SECRET` must be the TOTP **seed** — the string behind the QR image
in Settings → Security — not a six-digit code. Storing the seed is what lets an
unattended VPS log in without you.

### 4. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Get the exact ASPSP names for `ACCOUNT_LINKS` from the API, since they must
match character for character:

```bash
monarch-euro banks --country DE --search n26
```

### 5. Install and verify

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/monarch-euro doctor
```

`doctor` checks the key file, the Enable Banking application, the redirect URL
registration, the Monarch login, and whether each target account exists.

### 6. Link each Enable Banking bank

```bash
monarch-euro link n26
```

Only banks in `ACCOUNT_LINKS` need this. Wise authenticates with its token, and
a US Revolut connects in Monarch itself.

Each prints a URL. Open it, complete the bank's login, then paste the redirect
URL back into the prompt.

### 7. First sync

Do a dry run first — it logs every transaction it would post without writing
anything:

```bash
DRY_RUN=true monarch-euro sync
monarch-euro sync
```

For an initial backfill, widen the window once: `LOOKBACK_DAYS=365 monarch-euro sync`.
The ledger means a later normal run will not re-import any of it.

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Verify config and connectivity to both ends |
| `banks --country DE` | List ASPSP names for `ACCOUNT_LINKS` |
| `link <key>` | Authorize one bank (browser flow) |
| `unlink <key>` | Close a session; the dedupe ledger is preserved |
| `sync` | Fetch, convert and push |
| `status` | Sessions, consent expiry, mapped accounts, recent runs |
| `wise-probe` | List Wise profiles/balances and test statement access |
| `rules-init` | Write a starter `rules.json` |

`status` warns when a consent is within a week of expiring — worth watching,
since an expired consent is the one failure that needs you at a browser.

## Categorization

PSD2 remittance text is noisy: card numbers, terminal ids, IBANs and dates all
jammed into free text. Left alone it produces thousands of one-off merchants and
useless reports. `rules.json` maps regexes to merchant names and Monarch
categories:

```json
[
  {"match": "rewe|edeka|lidl|aldi", "category": "Groceries"},
  {"match": "geldautomat|\\batm\\b", "merchant": "ATM Withdrawal", "category": "Cash & ATM"}
]
```

`monarch-euro rules-init` writes a starter set aimed at German/EU merchants.
Omit `merchant` to keep the cleaned-up name from the statement. Edits take
effect on the next run; no restart needed.

## Deployment

### Docker (recommended for a VPS)

```bash
docker compose run --rm sync doctor
docker compose run --rm sync link n26     # interactive, one-off
docker compose run --rm sync              # the actual sync
```

State lives on the `./state` volume, so rebuilding the image never re-imports
old transactions. Drive it from the host crontab:

```cron
30 7,19 * * * cd /opt/monarch-euro && docker compose run --rm sync >> /var/log/monarch-euro.log 2>&1
```

### systemd

`deploy/` holds a hardened one-shot service and a twice-daily timer.

```bash
sudo useradd --system --home /var/lib/monarch-euro --create-home monarch
sudo cp -r . /opt/monarch-euro && cd /opt/monarch-euro
sudo python3 -m venv .venv && sudo .venv/bin/pip install .
sudo chown -R monarch:monarch /opt/monarch-euro /var/lib/monarch-euro
sudo chmod 600 /opt/monarch-euro/.env /opt/monarch-euro/secrets/*.pem
sudo cp deploy/monarch-euro.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now monarch-euro.timer
```

Check on it with `systemctl list-timers monarch-euro` and
`journalctl -u monarch-euro -n 50`.

Twice daily is deliberate: PSD2 permits four unattended polls per day, which is
also Enable Banking's free-tier ceiling, so this leaves headroom for retries.

### Secrets

`.env` and `secrets/` are gitignored. Both hold live banking credentials — keep
them `chmod 600` and owned by the service user. Nothing in `state/` is secret
except the Monarch session pickle, which is also credential-equivalent.

## Known limits

- **`monarchmoney` is unofficial.** Monarch documents no public API. This can
  break when they change their backend. The sink is deliberately thin so it can
  be repointed at the official MCP connector when that unpauses.
- **Consent expires every 180 days** per bank. Unavoidable — it is PSD2 law,
  not a limitation of this tool.
- **Converted balances drift from reality.** Historical rates differ from
  today's, so the Monarch balance will not match what N26 shows you. That is
  the cost of accurate historical reporting; the notes let you reconcile.
- **Revolut US is not in the pipeline.** PSD2 does not reach it. Connect it
  natively in Monarch; if that fails, export CSV until you open an EU account.
- **Pending transactions are skipped by default.** They change amount and
  identity before settling. `INCLUDE_PENDING=true` if you want them anyway.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers dedupe-key stability (including that a transaction keeps its identity
when it settles), credit/debit sign handling, merchant cleanup, rule matching,
ledger behaviour past SQLite's parameter limit, and FX rounding with the
weekend/holiday walk-back. No network or credentials required.

## Legal

- [Privacy Notice](PRIVACY.md) — what data the tool touches and where it goes
- [Terms of Use](TERMS.md) — no warranty; converted figures are approximations
- [MIT licensed](LICENSE)

Not affiliated with Enable Banking, Monarch Money, N26, Revolut or Wise.
