# Terms of Use

**monarch-euro** is a personal tool, published as source code so that its
behaviour is inspectable. It is not a hosted service and nothing is offered to
the public.

## Who may use it

Anyone may read, run or modify the source code, under the licence in this
repository. Anyone who runs it does so on their own infrastructure, against
their own accounts, under their own agreements with their own banks, with
Enable Banking and with Monarch Money. They become the operator of their own
instance, responsible for it.

There is no account to register, nothing is hosted for anyone, and no support
is promised.

## No warranty

The software is provided "as is", without warranty of any kind, express or
implied. The author is not liable for any claim, damages or other liability
arising from the software or its use.

This matters more than usual here, because the tool touches financial records:

- It reads bank data through a third-party provider and writes to a
  third-party budgeting service. Either can change or break without notice.
- It converts currency using published reference rates. Converted figures are
  approximations for personal budgeting, and will not match a bank statement,
  a settled card rate, or any figure suitable for tax or accounting use.
- Monarch Money has no multi-currency support, which is the reason conversion
  happens at all. Balances shown there will drift from real account balances.
- The component that writes to Monarch uses an unofficial client, because
  Monarch publishes no supported API. It may stop working at any time.

Anyone relying on its output for financial, tax or legal purposes does so at
their own risk. Verify against your actual bank statements.

## Third-party services

Running this tool means using Enable Banking and Monarch Money, each under
their own terms. This project is not affiliated with, endorsed by, or
connected to Enable Banking, Monarch Money, N26, Revolut or Wise. All
trademarks belong to their respective owners.

## Changes

These terms may change. The version in this repository at any time is the one
that applies.
