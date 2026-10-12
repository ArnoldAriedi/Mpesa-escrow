# Deployment guide

> File locations: see [Where to find things](../README.md#where-to-find-things) in the README.

This system was built and tested against the Safaricom Daraja SANDBOX only. No real M-Pesa payment has gone through it. Treat the first real-money pilot as acceptance testing (see the last section).

## What you need before going live
- A registered business with an M-Pesa paybill or till for collecting payments (STK Push).
- A separate shortcode enabled for B2C payouts (and B2B if sellers are paid to a till or paybill). Guides report that one shortcode cannot use both the collection and B2C APIs; confirm with Safaricom.
- Daraja go-live approval, production consumer key/secret, passkey, initiator name and password, and the production certificate.
- A server with a permanent IP (Safaricom whitelists production server IPs) and a domain with HTTPS. Production callbacks must be HTTPS.
- PostgreSQL with automatic backups. SQLite is for development only.
- A real SMS account (the code uses Africa's Talking) and a Slack webhook for ops alerts.
- Legal advice: holding customers' money in escrow may be a regulated activity in Kenya, and you store personal data.

## Required settings (names only; never commit values)
- Database: `DATABASE_URL` (postgresql+asyncpg://...), `DB_AUTO_CREATE=false`.
- Web: `CORS_ALLOWED_ORIGINS` (explicit list; `*` is refused in production mode).
- Secrets, each at least 32 characters and all different: `ESCROW_API_KEY`, `ADMIN_API_KEY`, `MPESA_CALLBACK_SECRET`, `USSD_CALLBACK_SECRET`. The app refuses to start in production mode otherwise.
- M-Pesa: `MPESA_ENV=production`, `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_B2C_SHORTCODE` (falls back to `MPESA_SHORTCODE` if unset), `MPESA_INITIATOR_NAME`, `MPESA_INITIATOR_PASSWORD`, `MPESA_CERTIFICATE_PATH` (production certificate), `MPESA_CALLBACK_BASE_URL` (public https address, no trailing slash).
- Do NOT set `MPESA_SECURITY_CREDENTIAL` in production. It is a sandbox shortcut and is ignored unless `MPESA_ENV=sandbox`.
- Tuning: `MPESA_HTTP_TIMEOUT_SECONDS` (default 30; the sandbox needed 90), `MPESA_REVERSAL_WINDOW_HOURS` (default 24; refunds inside it use Reversal, later ones use B2C), `QR_EXPIRY_CHECK_SECONDS` (default 60), `EXPIRY_BATCH_SIZE` (default 50).
- Alerts: `OPS_ALERT_WEBHOOK_URL`, `AFRICASTALKING_API_KEY`.

## Database
Create tables with Alembic, not auto-create. Always set `DATABASE_URL` first: `alembic.ini` defaults to the SQLite file, so running `alembic upgrade head` without it migrates the wrong database. Alembic needs the `psycopg` package. After upgrading, `alembic current` should show the latest revision.

## The callback gateway (required)
Safaricom callbacks are unsigned and cannot carry a custom header. The app authenticates callbacks ONLY by an `X-Mpesa-Secret` header and refuses a secret in the URL. Put a reverse proxy in front that:
1. accepts requests to `/callbacks/` only from Safaricom's callback addresses (community-documented ranges: 196.201.212.0/24, 196.201.213.0/24, 196.201.214.0/24 - confirm the current list with Daraja support),
2. adds `X-Mpesa-Secret` with the value of `MPESA_CALLBACK_SECRET` on those requests,
3. strips any client-supplied `X-Mpesa-Secret` on every other path.
The app should listen on 127.0.0.1 only. Restrict `/admin/` paths to trusted addresses if possible. Callback paths: stk-callback, c2b-validation, c2b-confirmation, balance-callback, balance-timeout, transaction-status-callback, transaction-status-timeout, b2c-callback, b2c-timeout, reversal-callback, reversal-timeout, b2b-callback, b2b-timeout.

## Run model
Run ONE app process. Rate limiting and fraud checks keep state in memory and are not shared between processes. Background loops (expiry, payout retry, STK recovery, reconciliation) run inside the app process.

## Operations
Someone must check daily, and on every Slack alert:
- Payouts in TIMEOUT, or stuck in SUBMITTING/INITIATED: check the Safaricom statement. If paid, call `/admin/payouts/{id}/confirm-provider-completed` with the real receipt. If not, call `/admin/payouts/{id}/reconcile-timeout` (it reverses the reservation and the sweep retries).
- Unapplied payments (`/admin/unapplied-payments`): real money that no escrow could take. Refund or apply it.
- Escrows in STK_INITIATION_UNKNOWN: finish with `/admin/escrow/{id}/resolve-stk-unknown` using the receipt from the portal.
- Disputes, including ones opened automatically when a QR expires while a delivery is out: resolve with REFUND, RELEASE or PARTIAL.
- Keep the B2C float funded. The balance check alerts before payouts fail.

## First real-payment checklist
Run small payments (KES 10) through every path and save the raw callback bodies:
1. STK success, user cancel, wrong PIN, no response (1037), and a slow customer (status query returning 4999 must NOT cancel the order).
2. Delivery confirmation then B2C release; seller order with a till (B2B).
3. Refund inside the reversal window (Reversal) and after it (B2C).
4. Dispute resolution: refund, release, partial.
5. QR expiry before and after the delivery is out.
Check that amounts, phone number formats and receipt fields in real callbacks match what the handlers expect, and that the ledger balances after each path (ESCROW_HOLDING nets to zero when everything has settled).

## Tests
Run `python -m pytest tests -q` with `DATABASE_URL` pointing at a throwaway SQLite file and `OPS_ALERT_WEBHOOK_URL` and `AFRICASTALKING_API_KEY` set to empty, so tests never touch real data or send real alerts. Duplicate-callback, duplicate-confirmation and duplicate-retry behavior depends on PostgreSQL row locks and was verified by hand on PostgreSQL, not by the automated suite.
