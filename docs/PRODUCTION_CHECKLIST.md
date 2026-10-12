# Pre-launch checklist for connecting real M-Pesa money

This is a consolidated checklist, not a guarantee of correctness. Items
marked (code) exist in this repository already or were added in this pass.
Items marked (infra) or (process) need to be done outside the codebase.

## Gating item — do this first

- [ ] **(process) Confirm regulatory/licensing status.** Holding customer
  funds in escrow is a regulated activity in Kenya. Confirm with a
  lawyer/compliance advisor whether you need your own license, need to
  operate under a licensed PSP/bank's umbrella, or otherwise satisfy CBK /
  National Payment System Act requirements *before* moving real customer
  money. This gates everything below — a well-built system operating without
  the right authorization is still a problem.

## M-Pesa integration correctness

- [ ] (process) Run the full flow — order creation, STK push, escrow hold,
  QR delivery confirmation, B2C release, dispute open/resolve, refund,
  expiry — against Safaricom's actual **sandbox**, not just this repo's unit
  tests. Watch the real callback payloads land; they can differ from
  documentation in subtle ways.
- [x] (code) B2C requests now include a unique `OriginatorConversationID`
  per attempt, matching Safaricom's documented requirement, so Safaricom's
  own systems can reject accidental duplicate disbursement requests.
- [ ] (process) Apply for and receive your production B2C shortcode /
  Bulk Disbursement Account — this is separate from your STK
  Paybill/Till and is required to go live with B2C payouts.
- [ ] (infra) Confirm your production `MPESA_CALLBACK_BASE_URL` is reachable
  by Safaricom's servers over HTTPS with a valid certificate, and that
  whatever sits in front of the app (load balancer / API gateway) forwards
  the `X-Mpesa-Secret` header used for callback authentication.

## Secrets & infrastructure

- [ ] (infra) Move `MPESA_CONSUMER_SECRET`, `MPESA_INITIATOR_PASSWORD`,
  `MPESA_CALLBACK_SECRET`, `ESCROW_API_KEY`, `ADMIN_API_KEY`, and the B2C
  certificate out of a `.env` file and into a real secrets manager (AWS
  Secrets Manager, GCP Secret Manager, Vault, etc.) for production.
- [ ] (infra) Use PostgreSQL in production (`DB_AUTO_CREATE=false`, run
  `alembic upgrade head`) — this is already enforced at startup, but confirm
  your deployment actually sets these. Confirmed: the full migration chain
  (0001 through the current head) runs cleanly against an empty database.
- [x] (code) `CORS_ALLOWED_ORIGINS=*` is now rejected at startup when
  `DB_AUTO_CREATE=false`. Set it to an explicit comma-separated allowlist of
  your real frontend origin(s) before deploying.
- [ ] (infra) TLS termination in front of the API; don't serve plaintext
  HTTP in production.
- [x] (code) Payouts stuck in `TIMEOUT` now get a recurring alert
  (`PAYOUT_TIMEOUT_STILL_UNRESOLVED`) every `STALE_TIMEOUT_REALERT_MINUTES`
  from the reconciliation sweep, not just once at the moment of transition.
  Wire `OPS_ALERT_WEBHOOK_URL` to a real destination before launch — without
  it, alerts only go to logs. See `TIMEOUT_RECONCILIATION_RUNBOOK.md`.
- [ ] (infra) Set `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` for production based on
  your worker process count and Neon's connection limit (defaults are 5/5
  per process — with N worker processes that's up to `N*10` connections).
  `DB_DISABLE_POOLING` must stay unset/`false` in production; it forces a
  fresh connection per checkout and exists only for the test suite.

## Concurrency & load

- [ ] (process) Run a deliberate concurrency test with two or more real
  worker processes against a real PostgreSQL instance (not SQLite) to
  validate the `FOR UPDATE SKIP LOCKED` and conditional-`UPDATE` logic under
  actual contention. The existing test suite only exercises this at a small
  scale against SQLite.
- [ ] (process) Load-test the expiry sweep and payout-retry background loops
  under realistic order volume before scaling up transaction limits.

## Operational readiness

- [x] (docs) `TIMEOUT_RECONCILIATION_RUNBOOK.md` — read and internalize
  this before going live; `TIMEOUT` reconciliation is a manual, human step by
  design and needs an owner.
- [ ] (process) Identify who holds the admin API key and can call
  `/admin/payouts/{id}/reconcile-timeout` and `/admin/disputes/{id}/resolve`.
  These endpoints move real money — treat access to them like treasury
  access.
- [ ] (process) Start with low per-transaction and daily volume limits and a
  soak period once live, rather than opening the floodgates immediately.

## Notifications

- [x] (code) Buyers now receive SMS confirmation on delivery confirmation,
  dispute resolution, and refund/release completion (previously missing).
- [ ] (infra) Configure a real `NOTIFICATION_PROVIDER_URL` /
  `NOTIFICATION_PROVIDER_TOKEN` for production — without it, "sent" SMS just
  print to stdout, which is fine for dev but not production.
