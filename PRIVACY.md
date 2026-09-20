# Privacy Notice

**monarch-euro** is a personal, single-user tool. It is not a product, a
service, or an application offered to anyone else. It is run by its operator,
on their own machine or server, against their own bank accounts.

There are no users other than the operator. The operator is the only data
subject, the only data controller, and the only person who can access anything
the tool touches.

## What data the tool handles

When the operator runs it, the tool reads from their own bank accounts, via
[Enable Banking](https://enablebanking.com), a licensed Account Information
Service Provider regulated by the Finnish Financial Supervisory Authority:

- account identifiers and currency
- transaction date, amount, currency and status
- counterparty name and payment reference text as supplied by the bank

It converts the amounts to a single currency using published European Central
Bank reference rates, and writes the result into the operator's own Monarch
Money account.

## Where that data goes

```
operator's banks → Enable Banking (AISP) → operator's own machine → operator's Monarch account
```

Only three parties ever see it: the operator's banks, Enable Banking, and
Monarch Money — each of which the operator already has a direct relationship
with, and each governed by its own privacy policy. This tool adds no fourth
party.

Exchange rates are fetched from a public rates API. That request contains a
date and a currency pair. It carries no account, transaction or identity
information.

## Where that data is stored

Locally, in a SQLite file on the machine the operator runs the tool on. It
holds transaction identifiers, dates, amounts and cached exchange rates, and
exists so the tool can avoid importing the same transaction twice.

Credentials — the Enable Banking private key, the Monarch login — live in
local files that are excluded from version control and never leave the
operator's machine.

## What the tool does not do

- No analytics, telemetry, crash reporting or usage tracking
- No data sent anywhere except the three parties named above
- No data shared, sold, published or used for any secondary purpose
- No accounts, profiles or records for anyone other than the operator

## Retention and deletion

Local state persists until the operator deletes it, which they can do at any
time by removing the state directory. Bank access is granted through a consent
that expires automatically, at most 180 days after it is given, and can be
revoked earlier at the bank or through Enable Banking.

## Contact

Via [GitHub issues](https://github.com/myshli/monarch-euro/issues) on this
repository.
