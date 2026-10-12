# Corrections applied to the escrow platform

## 1. Financial records are append-only

- `Order.items` no longer uses `cascade="all, delete-orphan"`.
- `OrderItem.order_id`, `Delivery.order_id`, and `EscrowTransaction.order_id` use `ON DELETE RESTRICT`.
- `Order.escrow`, `Order.delivery`, and `Order.disputes` use passive deletes.
- `LedgerTransaction.entries` no longer cascades deletes.
- `LedgerEntry` continues to use `ON DELETE RESTRICT` for both the transaction and account.
- SQLite foreign-key enforcement is enabled so local development behaves like PostgreSQL.

Orders with financial history should be cancelled/refunded/closed rather than hard-deleted.
Existing databases need a migration; `create_all()` does not alter existing foreign-key constraints.

## 2. B2C certificate handling

- The API no longer requires the certificate file to exist merely to start.
- B2C payout attempts fail with an actionable error if the certificate is missing.
- Docker Compose mounts `./certs` read-only at `/run/secrets/mpesa`.
- Set `MPESA_CERTIFICATE_PATH=/run/secrets/mpesa/SafaricomCertificate.cer`.
- Put the correct Safaricom public `.cer` file in `./certs/`.

## 3. Stateful USSD

`USSDSession.state` is now the source of truth for menu navigation. States include:

- `MAIN`
- `AWAITING_ORDER_REF_CHECK`
- `AWAITING_ORDER_REF_PAY`
- `AWAITING_ORDER_REF_CANCEL`
- `AWAITING_PAYMENT_CALLBACK`

The handler accepts both incremental USSD input and a full `*`-separated retry path.
The M-Pesa PIN is never collected by the application; it is entered on the M-Pesa handset prompt.

## 4. Concurrent expiry/payout workers

Expired escrow selection uses `FOR UPDATE SKIP LOCKED` where supported (PostgreSQL), preventing two workers from processing the same escrow row concurrently. Failed-payout retries use the same locking strategy.

SQLite does not provide PostgreSQL-style row locking, so production deployments should use PostgreSQL with multiple API/worker instances.

## 5. Structured order items

`CreateOrderRequest.items` now contains `OrderItemSchema` objects with validated:

- `name`
- positive `quantity`
- non-negative `unit_price`

This prevents malformed dictionaries from reaching SQLAlchemy model construction.

## 6. Buyer QR delivery

- A separate short-lived `qr_view_token` is generated when M-Pesa payment is confirmed.
- If `BUYER_PORTAL_BASE_URL` is configured, the buyer receives a secure QR-view URL by SMS.
- The QR image endpoint uses `Cache-Control: no-store` and the token expires with the three-hour QR window.
- The buyer API no longer requires the platform API key; the buyer access token is the buyer credential.

## 7. Atomic QR confirmation

- Rider QR confirmation now uses a conditional database update requiring `ESCROW_HELD`, matching rider assignment, an unused QR, and an unexpired QR.
- Only the request that successfully claims the row may create the release payout.

## 8. Dispute state machine

- Disputes can only be opened while funds are still `ESCROW_HELD`.
- Opening a dispute moves escrow to `DISPUTED`.
- Admin resolution requires `DISPUTED` and prevents a second active resolution.
- Partial resolutions use `PARTIALLY_SETTLED` and are finalized from the actual completed payout totals.

## 9. STK callback reconciliation

Successful callbacks must now contain and match:

- `CheckoutRequestID`
- merchant request ID when supplied
- M-Pesa receipt number
- amount
- payer phone number

The receipt is unique at the escrow level and raw callbacks are stored as JSON for reconciliation.

## 10. B2C payout lifecycle

- Every payout has an attempt counter and timestamps.
- Provider-rejected requests are reversed internally and marked `FAILED`.
- Transport/unknown outcomes are marked `TIMEOUT` and are **not** blindly retried, avoiding duplicate payouts.
- Explicitly failed payouts get a fresh ledger settlement attempt when retried.

## 11. Order amount integrity

- The M-Pesa total must be a whole number of KES before an order is created.
- When items are supplied, the server verifies `sum(quantity * unit_price) == amount` and then adds `delivery_fee`.

## 12. Buyer authentication

Buyer-facing order, escrow status, QR and dispute endpoints use the buyer access token rather than exposing the platform API key to a client application.

## 13. Production migrations

- Migration `0003_escrow_qr_callback_and_payout_lifecycle` adds the new fields and constraints.
- `DB_AUTO_CREATE=true` remains useful for development.
- Production should set `DB_AUTO_CREATE=false` and run `alembic upgrade head` before starting the API.

## 14. Safaricom app / *334# integration

- Daraja callback URLs never contain `MPESA_CALLBACK_SECRET`; callback authentication is header-only via `X-Mpesa-Secret`.
- The application still cannot insert itself into the official M-Pesa app or live `*334#` menu without Safaricom/provider onboarding. The backend exposes the application-side contracts needed after approval.

## 15. Critical race/session/callback corrections requested in review

### Delivery confirmation locking
- QR confirmation now first loads the escrow row with `SELECT ... FOR UPDATE` on PostgreSQL.
- The existing conditional `UPDATE` remains in place as the final idempotency guard, so the fix does not rely on the lock alone.

### Notification transaction boundaries
- Financial/order commits are intentionally kept before external M-Pesa calls and before post-commit notifications. This is required so a Safaricom callback cannot arrive before the escrow row exists and so an external payout has a durable internal reservation.
- Notification persistence now runs in a fresh SQLAlchemy session after the financial commit. This avoids stale/detached ORM state and keeps SMS-provider failures from corrupting the order/payment transaction.
- We do **not** hold one database transaction open while making a network call to Safaricom. A single final commit for the whole endpoint would be unsafe for an external payment workflow.

### PendingRollbackError handling
- The generic database-error path in `initiate_payout_for_record()` now explicitly rolls back before reloading and recording the `FAILED` payout state.
- Provider/network outcomes are handled separately so a rollback does not accidentally erase a payout reservation that may already have reached Safaricom.

### Partial dispute accounting
- Partial dispute resolution uses `create_payout()` for exactly one refund payout and one release payout; it does not create a second manual ledger transfer.
- The existing payout ledger reservation plus external-settlement ledger entry are distinct accounting events and are not duplicates.
- The database uniqueness constraint on `(escrow_transaction_id, purpose)` prevents duplicate refund/release payout records for the same escrow.

### Callback authentication
- Callback authentication is header-only via `X-Mpesa-Secret`.
- Callback URLs do not contain `MPESA_CALLBACK_SECRET`, so the secret is not exposed through URL/access/proxy logs.
- A trusted reverse proxy/API gateway must inject the header for Safaricom callbacks before forwarding them to the application.

### Expiry worker shutdown
- The worker explicitly handles cancellation around its sleep and re-raises `CancelledError` so application shutdown is clean and intentional.

## v7 Financial-integrity corrections

- `POST /admin/payouts/{payout_id}/reconcile-timeout` now reverses the existing B2C external-settlement ledger reservation before changing a `TIMEOUT` payout to `FAILED`. If reversal cannot be persisted, the payout remains `TIMEOUT` and is not made retryable.
- Timeout reservation reversal is idempotent using the same payout/attempt idempotency key, preventing duplicate reversal entries.
- `resolve_dispute` with `PARTIAL` now returns both refund and release payout results, including payout ID, purpose, status, and initiation outcome, instead of returning only the last loop iteration.
- Added regression tests covering the timeout ledger reversal, reversal idempotency, and both outcomes of a partial dispute resolution.

## v8 corrections (real-money readiness pass)

### B2C requests now send a unique `OriginatorConversationID`
Safaricom's current B2C documentation lists `OriginatorConversationID` as a required, application-generated field that Safaricom's own systems use to detect and reject a duplicate disbursement request. The application was only ever reading this field back from the response and never generating/sending its own. `initiate_payout_for_record()` now generates `escrow-payout-{payout.id}-{attempt}` (guaranteed unique per attempt because `attempt_count` increments on every retry), persists it to `payout.originator_conversation_id` *before* the network call so it survives a timeout, and sends it in the request. This is a second, provider-side line of defense against double payment, on top of the application's own ledger idempotency keys. See `test_b2c_payout_request_includes_unique_originator_conversation_id`.

### Missing buyer notifications
No SMS was ever sent for delivery confirmation, dispute resolution, or refund/release completion, even though every earlier lifecycle stage (order creation, agent assignment) notifies. Added, following the existing "fresh session after commit" pattern:
- `notify_delivery_confirmed_after_commit()` — fired from `confirm_delivery` when the release payout is successfully initiated.
- `notify_dispute_resolved_after_commit()` — fired from `resolve_dispute` for all three decisions (REFUND/RELEASE/PARTIAL), messaging both buyer and payee where relevant.
- `notify_escrow_finalized_after_commit()` — fired from `b2c_callback` on a successful payout callback; only sends when the escrow actually reached a terminal state (`REFUNDED`/`RELEASED`/`PARTIALLY_SETTLED`) via `_finalize_escrow_after_payouts`, so it's a no-op while other payouts in a partial settlement are still pending.

### CORS wildcard blocked in production mode
`CORS_ALLOWED_ORIGINS` silently defaulted to `*` with no guardrail. Extracted the startup checks into `_validate_production_config()` (testable independent of process env) and added: when `DB_AUTO_CREATE=false` (the existing "production mode" signal), the app now refuses to start unless `CORS_ALLOWED_ORIGINS` is an explicit allowlist. Dev/test mode (`DB_AUTO_CREATE=true`, the default) is unaffected.

### Documentation added
- `docs/TIMEOUT_RECONCILIATION_RUNBOOK.md` — the manual process an operator must follow before calling `/admin/payouts/{id}/reconcile-timeout`, since that endpoint intentionally requires a human to confirm with Safaricom first.
- `docs/PRODUCTION_CHECKLIST.md` — consolidated pre-launch checklist (secrets management, TLS, CORS, DB, concurrency, licensing pointers).

## v9 corrections (seller notifications)

### Seller was never notified
The seller received no message at any stage of the escrow lifecycle — not when the buyer's payment landed in escrow, and not when they were paid out (or when the buyer was refunded instead). Added, following the existing "fresh session after commit" pattern:
- `notify_seller_escrow_held_after_commit()` — fired from `stk_callback` alongside the existing buyer/rider notifications. Tells the seller a payment was received and is held in escrow. Deliberately excludes the QR token/link, which stays buyer/rider-only.
- `notify_seller_escrow_finalized_after_commit()` — fired from `b2c_callback` alongside `notify_escrow_finalized_after_commit`. Tells the seller how the transaction ended: paid out (with amount) when they were the actual payee, a neutral completion notice when a rider was the payee instead, a refund notice with no payment due, or the split amounts for a partial dispute settlement.

Both use `seller.phone_number` (the seller's registered contact number), not `payout_phone`, and are no-ops when the order has no `seller_id`.

See `test_seller_notified_on_escrow_held_without_qr`, `test_seller_notified_on_release_gets_no_qr`, and `test_seller_not_notified_when_rider_is_payee_but_order_completes`.

## v10 corrections (rider scans, seller gets paid)

### Assigning a rider silently redirected the payout to the rider
Previously, `EscrowTransaction.payee_role`/`payee_phone` were set to the rider the moment a delivery agent was assigned (either at order creation via `assigned_agent_id`, or later via `/admin/orders/{order_id}/assign-agent`). Since every payout/ledger call derives its recipient and account from those two fields, this meant the **entire** escrowed amount — not just a delivery fee — was released to the rider on QR confirmation, and the seller received nothing, even though the rider's only real role is to physically deliver and scan the QR.

Fixed in two places:
- `create_order()` now picks the payee with seller priority: **seller if the order has one, rider only as a legacy fallback when there is no seller_id**, buyer as a last-resort fallback matching prior behavior for that edge case.
- `assign_agent()` now sets `assigned_agent_id` (which grants scanning/delivery-confirmation authority, checked in `confirm_delivery`) but only overwrites `payee_phone`/`payee_role` with the rider when the order has no seller. Assigning or re-assigning a rider to an order that has a seller can no longer redirect money to the rider.

The rider-facing SMS at order creation and at assignment were also reworded (`order_events`, `assign_agent`) so they describe the order value held in escrow for the seller, rather than reading like a payout amount owed to the rider.

No schema change was required — `PayeeRole.RIDER` still exists and is exercised by any legacy/no-seller order (e.g. the old `/escrow/create` endpoint), and by every existing ledger/payout call site, which already branched on `tx.payee_role` and therefore needed no further changes.

See `test_rider_scans_qr_but_seller_is_paid`, which assigns a rider to a seller's order, confirms delivery through the rider's QR scan, and asserts the B2C payout recipient is the seller's `payout_phone`, not the rider's phone.

### Still open: rider compensation
This fix makes the seller whole, but it does not pay the rider anything for the delivery. If riders should be paid a delivery fee, that needs to be a **separate** payout (e.g. splitting `order.delivery_fee` to a `RIDER_PAYABLE` account and `order.amount` to `SELLER_PAYABLE` as two payouts against the same escrow), rather than reusing the single existing `payee_phone`/`payee_role` field, which can only point to one recipient at a time. Not implemented here — flagging so it's a deliberate decision.

## v11 corrections (dead payee fallback removed, deactivated-seller safeguard)

### Removed the buyer-pays-itself dead branch
`create_order()` previously fell back to `payee_phone=buyer, payee_role=SELLER` when an order had neither `seller_id` nor `assigned_agent_id` — a release would have paid the buyer their own escrowed money, mislabeled as a seller payout. This branch is now rejected outright: an order must supply `seller_id` and/or `assigned_agent_id`, or the request fails with `422 Order must specify seller_id or assigned_agent_id so escrow release has a valid payee`. The legacy `/escrow/create` endpoint is unaffected — it already requires `assigned_agent_id`.

### Deactivated sellers can no longer receive new orders or silent payouts
- `create_order()` now rejects (`403`) new orders against a `Seller` whose `is_active` is `False`.
- `confirm_delivery()` re-checks the seller's `is_active` status at the moment of QR scan/delivery confirmation — not just at order-creation time — because a seller can be deactivated (fraud, ban, dispute history, etc.) any time between order placement and delivery. If the seller has since been deactivated, delivery is still recorded as confirmed (the buyer got their goods), but instead of firing the release payout automatically, the escrow is moved into the existing `DISPUTED` state with an auto-opened `Dispute` row (`opened_by_phone` = buyer, reason recorded), so the payout is routed through the standard admin `/admin/disputes/{id}/resolve` flow (REFUND/RELEASE/PARTIAL) instead of new one-off machinery. The buyer is notified that delivery was confirmed but payout is under review (`notify_delivery_confirmed_hold_after_commit`).

This reuses the existing dispute/admin-resolution infrastructure entirely — no new escrow status or admin endpoint was added.

## v12 corrections (real SMS provider integration)

### Africa's Talking SMS integration
`notification_service.py` previously only logged to console or POSTed to a generic, unspecified webhook shape. Added a concrete Africa's Talking Bulk SMS integration (`SMS_PROVIDER=africastalking`), the standard SMS gateway for the Kenya market:
- Correct request shape (`application/x-www-form-urlencoded`, `apiKey` header, `username`/`to`/`message`/optional `from`).
- Numbers are converted to E.164 (`+2547XXXXXXXX`) only at the Africa's Talking call boundary — the rest of the system continues to store/compare phone numbers in the bare `2547XXXXXXXX` form M-Pesa's own APIs use.
- Africa's Talking returns HTTP 200/201 even for some per-recipient failures (invalid number, blacklisted, insufficient balance, gateway rejection, etc.), so the response's `SMSMessageData.Recipients[0].statusCode` is checked explicitly; only `100/101/102` (Processed/Sent/Queued) are treated as success. Anything else raises `SmsSendError` and the `Notification` row is marked `FAILED` with the reason, exactly like an HTTP error would be.
- `AFRICASTALKING_ENV=sandbox` targets Africa's Talking's sandbox endpoint for testing before switching to `production`.

The prior generic-webhook adapter is preserved as `SMS_PROVIDER=generic` for anyone using a different provider, and the previous "no `NOTIFICATION_PROVIDER_URL`" console-logging dev fallback is unchanged when `SMS_PROVIDER` is left unset entirely, so existing deployments and local dev are unaffected.

## v13 corrections (Daraja STK/B2C payload hardening)

Cross-checked `escrow_core.py`'s M-Pesa request/response handling against Safaricom's documented Daraja STK Push and B2C contracts and tightened several gaps. **A live sandbox run against real Daraja credentials is still required before go-live** — this pass makes the code correct against the documented contract, it does not substitute for that.

- **Hardcoded `TransactionType`.** STK Push always sent `"CustomerPayBillOnline"`, which is wrong (and fails, sometimes only at the customer's phone rather than at request time) for any merchant using a Till Number instead of a PayBill. Now configurable via `MPESA_TRANSACTION_TYPE` (`CustomerPayBillOnline` or `CustomerBuyGoodsOnline`), validated at startup.
- **OAuth token was fetched fresh on every single STK/B2C call.** Added a simple in-process cache (`get_access_token`) with a conservative 55-minute TTL against Safaricom's ~60-minute token lifetime, so normal order/payout volume doesn't hammer the OAuth endpoint or risk rate-limiting.
- **HTTP 200 with a rejection was previously invisible.** Both `initiate_stk_push` and `initiate_b2c_payout` now check the response's `ResponseCode`. Safaricom can accept the HTTP request but still reject the underlying push/payout (bad shortcode/passkey, invalid initiator credentials, insufficient B2C float) while still returning HTTP 200 — this was previously only caught downstream, indirectly, via a missing `CheckoutRequestID`/`ConversationID`.
- **New `MpesaRejected` exception** distinguishes a *confirmed* provider rejection (safe to reverse any provisional ledger reservation) from a genuine network/transport failure (unknown outcome — must not be reversed, per the existing TIMEOUT handling). `initiate_payout_for_record()` in `escrow_main.py` now catches `MpesaRejected` alongside `httpx.HTTPStatusError` for that reversal-safe path, so this new check plugs into the existing double-payment safeguards instead of accidentally bypassing them (a real risk of writing "handle ResponseCode" without a dedicated exception type — it would otherwise fall into the ambiguous-outcome branch and never get reversed).

## v14 corrections (rider notifications, seller KYC gate, payout alerting, PII redaction)

### Rider was never notified on QR scan or payout completion
Confirmed this was genuinely missing: `confirm_delivery()` notified the buyer on a successful scan, but never the rider who actually did the scanning. Added two rider-facing notifications:
- **At scan time** (`notify_delivery_confirmed_after_commit`): the rider is told the buyer's payment was approved and is being released to the seller. Worded as "approved"/"being released", not "received" — the B2C payout is only initiated at this point, not yet confirmed by Safaricom.
- **At actual completion** (`notify_rider_escrow_finalized_after_commit`, called from `b2c_callback` alongside the buyer/seller finalize notifications): the rider is told the seller has genuinely received the funds, once the RELEASE payout completes for real. Only fires for `RELEASED` and only if a rider was assigned, so it stays silent for refund/legacy-no-rider cases.

### Seller KYC gate
Added `Seller.national_id`, `Seller.kyc_status` (`UNVERIFIED`/`PENDING`/`VERIFIED`/`REJECTED`), `kyc_verified_at`, `kyc_notes`, plus `POST /admin/sellers/{id}/kyc` to record a verification outcome. This does not perform verification itself (matching a national ID against a real registry is an operational process outside this codebase's reach) — it only records the result and gates on it.
`confirm_delivery()` now blocks the automatic release payout — routing it into the existing auto-opened-`Dispute`/admin-review path already built for deactivated sellers — if the seller's `kyc_status` is anything other than `VERIFIED`, in addition to the existing `is_active` check. New sellers registered via `/sellers/register` start `UNVERIFIED` (or `PENDING` if a `national_id` was supplied at registration), never pre-verified.

### Payout failure/timeout alerting
Added `ops_alerts.py`: a best-effort `alert_payout_issue()` that posts to `OPS_ALERT_WEBHOOK_URL` (any endpoint accepting `{"text": "..."}}`, e.g. a Slack incoming webhook) whenever a payout transitions to `FAILED` or `TIMEOUT`, in `initiate_payout_for_record()` and in the `/mpesa/b2c-timeout` callback. Never raises into the caller — a broken alert channel must not affect payout bookkeeping — and falls back to a log line if `OPS_ALERT_WEBHOOK_URL` isn't set. Before this, a stuck payout was discoverable only by reading logs or querying the database directly.

### PII redaction in logs
`b2c_timeout` previously logged the entire raw Safaricom callback body, which can include recipient names/phone numbers (e.g. `ReceiverPartyPublicName`). Now logs only `conversation_id` and `result_desc`.

## v15 corrections (external review triage: USSD auth, targeted locking, admin audit trail, SMS retry)

An external review of the codebase produced a 12-item list. Each item was independently verified against the actual code before acting on it — several were accurate, one was already substantially mitigated, and one was stale given an earlier product decision (rider payment model). See the conversation record for the full item-by-item verdict. What was implemented:

- **USSD auth moved to a header.** `/ussd/callback` accepted its secret as a URL query parameter (`?secret=...`), which is worse than the M-Pesa pattern since query strings routinely end up in access/proxy logs. Now reads `X-Ussd-Secret`, matching the header-based, trusted-proxy-injected pattern already used for M-Pesa callbacks.
- **Row-locking added where it was genuinely missing.** The review's claim that "payout state needs stronger locking" was partly already false — `retry_failed_payouts()` already used `with_for_update(skip_locked=True)`. The real gaps were narrower: `stk_callback`'s transaction lookup, and the admin `/admin/payouts/{id}/reconcile-timeout` and `/admin/payouts/{id}/retry` endpoints, none of which locked their row. All three now do. (The STK callback race was also less severe than described — `LedgerTransaction.idempotency_key`'s DB-level unique constraint already prevented an actual double-credit; the lock removes a spurious mid-transaction error on a legitimately retried callback rather than closing an open double-payment hole.)
- **Per-admin API keys and an audit log**, without building the full MFA/API-gateway system the review also suggested (that's an infra/ops decision, not a code-only one). Added `AdminUser` (named admin, own API key, independently revocable) and `AuditLogEntry` (append-only, part of the same transaction as the action it records). `verify_admin_key` now accepts either the legacy shared `ADMIN_API_KEY` (kept so existing deployments and the bootstrap-the-first-admin problem both keep working) or a per-admin key, and returns an actor label. Wired into the endpoints that actually move money or change trust status: `resolve_dispute`, `set_seller_kyc`, `assign_agent`, `reconcile_timeout_payout`, `retry_payout`. New `/admin/admins/register`, `/admin/admins/{id}/deactivate`, `/admin/audit-log` endpoints.
- **USSD session expiry.** `USSDSession` had no TTL; a session could in principle be resumed indefinitely from wherever it was left. Added `created_at` and a `USSD_SESSION_TTL_SECONDS` (default 180s) check that resets a stale session to the main menu instead of continuing wherever it was abandoned.
- **Bounded SMS retry.** `send_sms` now retries transient (network/timeout) failures up to `SMS_MAX_ATTEMPTS` (default 3) with linear backoff, tracked via a new `Notification.attempts` column. A confirmed provider rejection (`SmsSendError` — invalid number, blacklisted, insufficient balance) is deliberately never retried, since retrying a definite rejection wastes time and, on a paid provider, money.

### Explicitly not done from the same review, and why
- **Rider/seller split accounting** — stale. Already resolved by an earlier product decision: riders are paid independently, outside the escrow.
- **USSD integration tests** — real gap, deferred along with the rest of the test suite per instruction.
- **Redis-backed distributed rate limiting** — the underlying claim (in-memory limiter doesn't work across multiple app instances) is correct, but fixing it means adding a new infrastructure dependency (a Redis deployment) this project doesn't have yet, not just an application-code change.
- **Full MFA / API gateway for admin access** — infra/ops decision layered on top of the per-admin-key foundation now in place.
- **TLS termination, secrets manager, DB backups, monitoring** — deployment/ops configuration, not application code.
- **Regulatory licensing** — unchanged; no code resolves this.

## v16 correction (missing Alembic migration for this session's schema changes)

Caught this myself, unprompted: every schema change made across this session's fixes — `Seller.national_id`/`kyc_status`/`kyc_verified_at`/`kyc_notes`, `USSDSession.created_at`, `Notification.attempts`, and the new `AdminUser`/`AuditLogEntry` tables — was made only in `escrow_models.py`. Production runs with `DB_AUTO_CREATE=false` (enforced by `_validate_production_config`), meaning schema changes are applied exclusively through Alembic migrations, not `create_all()`. None of tonight's changes had one. On a real production database, none of this session's work would have actually taken effect — the app would crash the moment it touched `Seller.kyc_status` or tried to write an `AuditLogEntry`.

Added `alembic/versions/0005_kyc_admin_audit_and_notification_attempts.py`, following the same idempotent, dual-SQLite/PostgreSQL-safe pattern as the existing migrations (checks for existing columns/tables before adding, `batch_alter_table` for SQLite compatibility, explicit native-enum creation for PostgreSQL).

**Operational callout, not just a code note:** this migration backfills every existing `Seller` row to `kyc_status=UNVERIFIED`. Combined with the KYC gate added earlier this session, that means **every already-active seller stops receiving automatic payouts** the moment this migration and the corresponding app code both go live — each one lands in the admin-dispute-review hold path on their next delivery confirmation, until explicitly marked `VERIFIED` via `POST /admin/sellers/{id}/kyc`. If there's an existing seller base that should be grandfathered in rather than individually re-verified, that needs a deliberate one-off data decision (a bulk backfill to `VERIFIED` for pre-existing sellers, run once at deploy time) — not something this migration should silently decide either way.

## v17 corrections (C2B, Reversal, B2B, Account Balance, Transaction Status, velocity flags)

Built out the Daraja product surface beyond STK+B2C, per a "look at Safaricom's whole system" review. Each item:

- **C2B Register/Validation/Confirmation.** Until now, STK Push was the *only* way to pay — a buyer whose prompt failed, timed out, or who simply preferred paying a paybill manually via `*334#`/the M-Pesa menu had no path in. Added `register_c2b_urls()` (one-off provisioning, see `scripts/register_c2b_urls.py`) and `/mpesa/c2b-validation` + `/mpesa/c2b-confirmation`. Validation rejects unknown/already-settled order references *before* money moves (the only point where that's possible). Confirmation can only ever acknowledge — the money has already moved into the shortcode by the time it fires — so a payment that can't be automatically matched (wrong/garbled reference, amount mismatch, already-paid order) is recorded in a new `UnmatchedC2BPayment` table and raises an ops alert, rather than being silently accepted or dropped. A matched, exact-amount C2B payment now holds escrow, generates the QR, and notifies buyer/seller exactly like a successful STK callback does.
- **Reversal API preferred over B2C for refunds.** Added `initiate_reversal()` and `/mpesa/reversal-callback`/`-timeout`. `_select_refund_method()` uses Reversal whenever the original STK/C2B receipt is still within `MPESA_REVERSAL_WINDOW_HOURS` (default 24h) of `stk_callback_received_at`, falling back to B2C outside that window. Reverses the actual transaction back to the payer instead of manufacturing an unrelated new payment for the same amount.
- **B2B for merchant sellers.** Added `Seller.payout_channel`/`payout_shortcode` (`POST /admin/sellers/{id}/payout-channel`), `initiate_b2b_payout()`, and `/mpesa/b2b-callback`/`-timeout`. `_select_release_method_and_recipient()` routes a RELEASE to B2B whenever the seller has a registered Till/Paybill, so the payment settles against their actual merchant account instead of being treated like a consumer B2C transfer. Sellers without a registered shortcode are unaffected — still B2C to their phone, exactly as before.
- **Account Balance monitoring.** Added `AccountBalanceCheck`, `query_account_balance()`, `/mpesa/balance-callback`/`-timeout`, `POST /admin/mpesa/account-balance/check` to trigger a check, `GET /admin/mpesa/account-balance/latest` to read the last result. An ops alert fires automatically if the working account balance comes back below `FLOAT_LOW_BALANCE_THRESHOLD` (default KES 10,000) — catching low B2C float *before* payouts start failing for lack of funds, not just after.
- **Transaction Status querying.** Added `query_transaction_status()`, `/mpesa/transaction-status-callback`/`-timeout`, `POST /admin/payouts/{id}/query-status` — lets an admin actively ask Safaricom for a stuck payout's true state instead of only ever waiting on a callback that may never arrive. Deliberately logs the result rather than auto-transitioning the payout: the first time this path is exercised for real, that transition deserves a human look, same caution as `reconcile-timeout`.
- **Velocity/fraud flagging.** Added `VelocityGuard` (in-memory, same single-process caveat as the existing rate limiter) tracking order count/total per buyer phone over a rolling hour. Orders over the threshold are *flagged* (`Order.flagged_for_review`/`flag_reason`, `GET /admin/orders/flagged`, an ops alert) rather than blocked — a legitimate buyer having a busy hour shouldn't be locked out of paying.
- **`Payout.method`** (`B2C`/`REVERSAL`/`B2B`) added so the payout lifecycle machinery (`initiate_payout_for_record`, both callback handlers) could be generalized to all three Safaricom products uniformly instead of duplicated three times — the B2C-specific reversal/timeout/callback logic from earlier sessions is now shared (`_handle_payout_result_callback`, `_handle_payout_timeout`, `_reverse_settlement_reservation`) rather than B2C-only.

Migration: `alembic/versions/0006_c2b_reversal_b2b_balance_and_velocity_flags.py`, following the same idempotent pattern as `0005`.

**Not yet verified against a live Daraja sandbox** — same caveat as every other Daraja integration in this codebase: correct against documentation and internal review, not yet exercised against real Safaricom endpoints. C2B Validation in particular is worth confirming early, since some shortcode configurations never invoke Validation at all (Confirmation-only), which is exactly why `UnmatchedC2BPayment` exists as the backstop regardless.

## v18 corrections (concurrency/correctness pass: locking, idempotency, unknown-outcome handling)

Triggered by an external review specifically targeting race conditions and double-payment/lost-payment risks, independently verified against the code before acting (see conversation record for the full per-item verdict). Two were confirmed as genuine, already-live financial-state-corruption bugs, not just theoretical races:

- **Dispute vs QR confirmation race (confirmed severe).** `open_dispute` read `tx.status` unlocked, then wrote `DISPUTED` unconditionally. A concurrent rider QR scan (`confirm_delivery`) could create a real RELEASE payout in between, which the dispute's stale write would then silently overwrite back to `DISPUTED` — after which an admin resolving "the dispute" could create an *additional* REFUND payout, a genuine double-payment not caught by the existing `(escrow_id, purpose)` uniqueness constraint since REFUND and RELEASE are different purposes. Fixed: `open_dispute` now uses the same atomic conditional-UPDATE pattern (`WHERE status=ESCROW_HELD`) already used by `confirm_delivery`'s QR claim, so whichever operation commits first wins cleanly and the other gets a clear 409 instead of silently clobbering real state.
- **USSD cancellation vs STK callback race (confirmed severe).** The USSD cancel branch wrote `PAYMENT_FAILED` unconditionally with zero lock and zero re-check, racing directly against `stk_callback`'s properly-locked update. If the buyer's STK payment actually succeeded at the same moment (or a stale USSD session tried to cancel after the fact), a correctly-committed `ESCROW_HELD` — with real money already collected — could be silently overwritten to `PAYMENT_FAILED`. Fixed the same way: an atomic conditional UPDATE gated on `status=PENDING_PAYMENT`.

Also fixed, all real but lower severity (each already had a partial constraint-level backstop, closing them removes reliance on that backstop and the spurious errors it produces on legitimate retries):
- Row-locking added to the shared B2C/Reversal/B2B result and timeout callback handlers (a genuine lost-update was possible between a success Result and a concurrent Timeout callback for the same payout — the more serious of this pair, since it could mark an already-completed, already-paid-out transaction as `TIMEOUT`).
- Row-locking added to `resolve_dispute` (both the `Dispute` and `EscrowTransaction` rows) and to the general USSD session lookup in `ussd_callback`.
- C2B duplicate-receipt detection re-checked *after* acquiring the escrow row lock, not just before — closing a TOCTOU window where a genuinely concurrent duplicate delivery of an already-processed Confirmation callback could slip past the unlocked pre-check and get misfiled as an unmatched payment.

New, addressing the payment-initiation side (no protection existed here before at all):
- **`Order.idempotency_key`** (optional `Idempotency-Key` header, scoped per `buyer_phone` via a unique constraint): a network retry of `POST /orders` — common on Kenyan mobile networks — now returns the original order/STK result instead of silently creating a second order with its own STK push and asking the buyer to pay twice.
- **`EscrowStatus.STK_INITIATION_UNKNOWN`**: `create_order`'s STK-initiation call now distinguishes a *confirmed* rejection (`MpesaRejected`/`HTTPStatusError` — safe to mark `PAYMENT_FAILED`) from a genuinely *unknown* outcome (e.g. a network timeout after the request was already sent, where Safaricom may still be processing it and the buyer's phone could still get a real PIN prompt). The unknown case is never told "just retry" — that response text explicitly asks the buyer to wait and check status first, to avoid a retry plus the original ambiguous attempt both succeeding. New `GET /admin/escrow/stk-unknown` and `POST /admin/escrow/{id}/resolve-stk-unknown` let an admin manually resolve these against Safaricom's own statement/portal (this is fundamentally a manual reconciliation problem when the initiation response itself never arrived — there is no receipt-free way to query Safaricom for a request whose response was lost).

Two smaller correctness additions from the same review:
- **Provider amount validation on payout completion**: the payout Result callback now checks Safaricom's reported completed amount against what was expected and fires an ops alert on mismatch (trusting that the money moved, per Safaricom's authority, but never silently ignoring a discrepancy) — the same discipline `stk_callback` already applied on the money-*in* side, now applied symmetrically on the money-*out* side.
- **Real unmatched-C2B reconciliation**: `POST /admin/c2b/unmatched/{id}/apply-to-order` actually credits an unmatched payment to a real order (holding escrow, generating the QR) when an admin identifies the right match, rather than the previous tool which only flagged a record "resolved" without moving anything.

Migrations: `0007_stk_unknown_state_and_order_idempotency.py` (on top of `0006`).

**Known remaining gap, called out rather than silently left:** the order-creation idempotency check itself has a narrow TOCTOU window — two truly concurrent requests with the same `Idempotency-Key` can both pass the "no existing order" check before either commits. The `(buyer_phone, idempotency_key)` unique constraint still prevents two orders/two STK pushes from being created (the second request's commit fails), but the failure surfaces as a 500 rather than a clean idempotent response on that specific race. A third, later retry would succeed cleanly. Acceptable given how narrow the window is, but worth knowing about rather than assuming this is airtight.

## v19 corrections (checked-and-fixed subset of a broader infra review)

An external review raised ~15 items spanning security, scalability, and operations. Each was independently verified against the code (several were already done, one — in-memory USSD sessions — was factually wrong, and a couple were infra/deployment decisions rather than code gaps; see the conversation record for the full per-item verdict). Of the genuinely real, code-fixable findings, these four were implemented:

- **`mpesa_checkout_request_id` unique constraint.** `mpesa_receipt_number` already had `unique=True`; the field `stk_callback` actually looks transactions up by did not — only an index. Added (migration `0008`). NULLs are unaffected by a unique constraint, so pre-payment rows are unchanged.
- **Consistent PII masking in logs/alerts.** A prior fix (v13-era) redacted one raw-body log; several call sites added in later sessions (C2B, velocity-flag, STK-unknown alerts) reintroduced raw buyer phone numbers into `alert_payout_issue` calls and a notification retry-warning log, since the masking principle wasn't backed by a reusable helper. Added `mpesa.mask_phone()` (e.g. `"2547****5678"`) and applied it at every log/alert call site that includes a phone number. Deliberately left the SMS dev-console echo (`[SMS DEV] ...`) unmasked — that's local-developer-only output where seeing the real number is the point, not a production log stream. DB columns (`Order.buyer_phone`, `AuditLogEntry`, etc.) are a different, already access-controlled risk profile and were left as-is.
- **Notification dead-letter/retry sweep.** Previously, once `send_sms`'s own internal retry loop (`SMS_MAX_ATTEMPTS`, within one call) was exhausted, a `Notification` just sat as `FAILED` forever — payouts had a background retry sweep, an admin retry endpoint, and failure alerting; notifications had none of that. Added `retry_failed_notification()` (out-of-band retry, distinct from `send_sms`'s in-call retries) wired into the existing `expiry_loop` sweep, bounded by `NOTIFICATION_MAX_TOTAL_ATTEMPTS` (default 10), with an ops alert fired at the point a notification becomes genuinely dead-lettered (ceiling reached). New `GET /admin/notifications/failed` and `POST /admin/notifications/{id}/retry` (which resets the ceiling — an admin choosing to retry is an explicit override) for visibility and manual intervention.
- **Scheduled, automated reconciliation.** The Account Balance and Transaction Status API integrations already existed as on-demand admin-triggered tools; nothing ran them without a human remembering to. Added `reconciliation_loop()`, a second background task (started alongside `expiry_loop` in `lifespan`, its own much longer interval via `RECONCILIATION_INTERVAL_SECONDS` — these call real, metered Safaricom APIs and must not run on the 60-second cadence `expiry_loop` uses) that checks the B2C float automatically and queries Transaction Status for any payout that's been `INITIATED` for longer than `PAYOUT_STALE_INITIATED_MINUTES` with no callback ever arriving.

### Explicitly not done from the same review, and why
- **Message-broker offload for callback processing** — a real architectural direction for genuine national-payment-rail TPS, but a load-bearing redesign of the entire callback pipeline, not a quick fix. Needs a load test to size before committing to it, not a reflexive "add Celery."
- **KMS/Vault-backed secrets, Redis-backed OAuth token cache, M-Pesa IP whitelisting** — all infrastructure/deployment decisions, not application-code changes. The code's env-var-based secret loading already matches the standard interface every secrets manager uses (Vault agent injection, AWS Secrets Manager CSI driver, etc.); what's missing is the deployment wiring, not a code change here.
- **"In-memory USSD sessions"** — not acted on because it isn't true. `USSDSession` is a real database table, already shared correctly across any number of app instances via the same Postgres database every instance already connects to.
- **Multi-region Daraja failover** — not well-grounded; Safaricom doesn't publish alternate regional Daraja endpoints to route between. A smaller, real version of this concern (bounded retry-with-backoff around the outbound httpx calls to Safaricom for pure transient network blips) remains open and is a reasonable next step, distinct from "multi-region."

### Still open / out of scope for a code-only pass
- No access to a real Daraja sandbox from this environment — the STK/B2C payload shapes were cross-checked against Safaricom's current published documentation, but have not been exercised against a live sandbox call.
- Regulatory/licensing status for holding customer funds in escrow in Kenya is outside what a code review can determine.
- Real concurrency/load testing against Postgres with multiple live worker processes has not been performed (only SQLite-based unit-level race tests exist).
- Secrets manager / TLS termination / infra hardening is deployment-specific and not implemented here.

## v20 corrections (payout race, USSD payment-safety parity, RBAC, and reconciliation hardening)

Triggered by a 30-item external review of v19. Each item was independently verified against the code before acting — see the conversation record for the full per-item verdict, including which were already handled and which were overstated. Of the genuinely real, code-fixable findings, these were implemented:

- **Payout initiation race (the single most important fix in this pass).** `initiate_payout_for_record` committed the ledger reservation — releasing the caller's row lock — *before* calling Safaricom, while the payout's `status` was still `PENDING`/`FAILED`. A background retry sweep and an admin retry (or two background sweeps) could both select the same row in that window and both call Safaricom for the same payout; distinct `OriginatorConversationID`s per attempt meant the existing uniqueness protections didn't stop it. Fixed by claiming the payout as a new `SUBMITTING` status immediately before that commit — subsequent `SELECT ... WHERE status IN ('PENDING','FAILED') FOR UPDATE SKIP LOCKED` queries simply stop matching the row. A payout stuck in `SUBMITTING` (process crash mid-flight) is treated exactly like the existing `TIMEOUT` case: picked up by the reconciliation sweep, never silently retried, resolved only through admin reconciliation.
- **USSD payment-initiation parity with `/orders`.** The USSD Pay flow had neither of the two protections the `/orders` STK path already had: it caught STK failures with a bare `except Exception`, resetting to a freely-retryable state regardless of whether Safaricom had actually received the request (an unknown network timeout and a confirmed rejection were treated identically); and it checked only `escrow.status==PENDING_PAYMENT`, never whether an STK request had already been sent, so a customer re-entering the USSD flow could fire a second STK push for the same order. Both fixed to match `/orders` exactly: a confirmed rejection (`MpesaRejected`/`HTTPStatusError`) is safe to let the buyer retry; an unknown outcome sets `STK_INITIATION_UNKNOWN` (which the existing status check then blocks future USSD retries against) and tells the buyer explicitly not to pay twice; and `mpesa_checkout_request_id` is checked before ever calling `initiate_stk_push`.
- **C2B duplicate-receipt check widened to be global.** The pre-lock duplicate check only queried `UnmatchedC2BPayment`; a receipt already attached to *a different* escrow than the one this callback's bill reference points to would fall through to the mismatched-order path and hit the DB's unique constraint as a raw `IntegrityError` instead of a clean idempotent response. Now also checks `EscrowTransaction.mpesa_receipt_number` directly. The DB constraint remains the real backstop; this just detects the condition first, same principle as the existing post-lock re-check.
- **C2B Validation now checks amount, not just reference/status.** Confirmation still independently re-validates everything — some paybill configurations never invoke Validation at all — but Validation can now reject an amount mismatch before money moves, not only an unknown or already-settled reference.
- **`TransactionStatusQuery` as a durable reconciliation record.** The Transaction Status callback previously only logged a warning; an admin asking "what happened to payout #123" had no structured, queryable answer tying Safaricom's response to that payout. New table records `payout_id`, the `OriginatorConversationID` used to correlate the query to its response, result code/description, reported transaction status, receipt, and the raw response — for both the on-demand admin endpoint and the scheduled sweep. Still deliberately does not auto-transition the payout's own status; that remains a human decision, same caution already applied to `reconcile-timeout`.
- **Reconciliation-sweep locking.** The stale-`INITIATED`-payout query in `reconciliation_loop` had no `FOR UPDATE SKIP LOCKED`, so multiple app instances could both pick the same payout and both fire a Transaction Status query for it — not a double-payment risk, but wasted provider calls and confusing duplicate records. Fixed, and the sweep now also covers `SUBMITTING` (the new state from the payout-race fix above), for the same "ask Safaricom directly" reasoning already applied to `TIMEOUT`.
- **Account Balance callback row-locking.** `balance_callback`/`balance_timeout` read-then-wrote `AccountBalanceCheck` without a row lock; Safaricom's own callback retries could race and clobber each other's writes. Now `.with_for_update()`, matching the locking pattern already used everywhere else in the callback surface.
- **`OriginatorConversationID` cross-check on payout result callbacks.** The shared B2C/Reversal/B2B result handler located the payout by `ConversationID` alone and never verified `OriginatorConversationID` against the one stored when *we* sent that specific attempt. Now cross-checked before the result is applied; a mismatch is alerted and the callback is acknowledged without mutating payout state, pending manual review, rather than silently applying a result to what might be the wrong attempt.
- **`Payout.transaction_id` uniqueness.** Indexed but not unique — a Safaricom transaction receipt should never settle two different internal payouts. Added as a DB-level constraint (NULLs unaffected, standard SQL semantics).
- **Coarse admin RBAC.** Until now, any active admin — including the legacy shared `ADMIN_API_KEY` — could register more admins, including other admins with full access. Added `AdminRole` (`SUPER_ADMIN`/`FINANCE_ADMIN`/`KYC_ADMIN`/`DISPUTE_ADMIN`/`OPERATIONS_ADMIN`) and a `require_admin_role(*roles)` dependency factory (`SUPER_ADMIN` always passes). Admin registration and deactivation are now `SUPER_ADMIN`-only; KYC, payout-channel changes, dispute resolution, payout retry/reconcile-timeout/query-status, and unmatched-C2B application are gated to their matching role. `deactivate_admin` also now refuses to deactivate the last active `SUPER_ADMIN`. This is a flat permission tier, not a fine-grained policy engine — see below.
- **Admin rate limiting.** The public order endpoint already had a `rate_limit` dependency; admin endpoints had none. Added as one path-prefix middleware covering every current and future `/admin/*` route, rather than retrofitting a dependency onto ~30 individual decorators.

Migration: `alembic/versions/0009_admin_roles_payout_txn_uniqueness_and_status_queries.py` (admin role column, backfilled to `SUPER_ADMIN` for existing rows so nobody is locked out on upgrade; the `Payout.transaction_id` unique constraint; the `transaction_status_queries` table).

### Explicitly not done from the same review, and why

- **Escrow amount conflating item price + delivery fee into one settlement (the review's #12/#13).** This is the one finding from this review that changes real money movement, not just how safely it moves — the escrow currently holds `product + delivery_fee` as a single total and releases the whole thing to one payee. Whether that's actually wrong depends on a business rule this codebase can't infer: does the rider keep the delivery fee directly, does the platform collect it and pay the rider separately, is there a platform cut on top? The `Payout` table's `UniqueConstraint(escrow_transaction_id, purpose)` (one payout per purpose per escrow) would also need to change to allow a seller RELEASE and a rider RELEASE on the same escrow. Deliberately not guessed at — confirm the real settlement rule first, then implement it as its own change together with the corresponding partial-dispute-settlement fix (#13), rather than bundling a business-logic assumption into a batch of concurrency/security fixes.
- **`PaymentAttempt` as a real per-attempt history table (#4).** Architecturally correct — `EscrowTransaction` currently holds only the latest STK attempt's fields — but a genuine schema migration plus a rewrite of every STK/callback read site, not a quick addition. Worth doing once the payout-claim fix above has proven itself in practice.
- **Redis/DB-backed `VelocityGuard`/`RateLimiter` (#18).** Needs new infrastructure (Redis or a shared counter table), not a code change on its own.
- **Payload retention policy, national ID encryption at rest, bearer-token hashing (#19/#20/#21).** Policy decisions (retention windows, access control, key management) that shouldn't be defaulted silently in a code pass.
- **Notifications fully out of the callback request path (#22).** Needs real queue/worker infrastructure beyond this file.
- **A real three-way reconciliation engine — expected vs. ledger vs. provider (#23).** `/admin/reconciliation` is still a display of `ReconciliationRecord` rows; the actual comparison engine is a project of its own.
- **PostgreSQL-specific concurrency tests, a materially larger test suite, testing the migration chain against real Postgres, splitting `escrow_main.py` into routers/services, structured logging, a central state-transition service (#24-#30).** All correct observations, all sequenced as normal follow-up engineering work rather than interleaved with the financial-correctness and access-control fixes above.

### Still open / out of scope for a code-only pass

- No access to a real Daraja sandbox from this environment; nothing new here changes that caveat from prior sessions.
- The RBAC model added above is intentionally coarse (five flat tiers). It closes the specific "any admin can create any admin" and "no separation of duties for money-moving actions" gaps named in the review, not a general-purpose permission system.
- Real concurrency/load testing against Postgres with multiple live worker processes still has not been performed.

## v21 corrections (SUBMITTING state-machine propagation, reconciliation completeness, and manual-reconciliation guardrails)

Triggered by a 27-item follow-up review of v20 — largely a review of my own v20 work, and it caught real gaps, several of which were direct consequences of adding the `SUBMITTING` payout state without propagating it everywhere else in the state machine that already handled `PENDING`/`INITIATED`/`TIMEOUT`.

- **A `SUBMITTING` payout with no `transaction_id`/`conversation_id` could get stuck forever (the most important fix here).** The reconciliation sweep's stale-payout query correctly picked up stale `SUBMITTING` rows, but then unconditionally `continue`d past any row with neither ID set — exactly the case of a process crashing before Safaricom ever responded, which is precisely what `SUBMITTING` exists to catch. There is nothing to query Transaction Status *with* in that state (no ID to query against), so such a row now auto-transitions to `TIMEOUT` instead, which the existing `reconcile-timeout` admin flow already knows how to handle — never auto-retried, since Safaricom may have received the request in the instant before the crash.
- **`SUBMITTING` propagated through the rest of the state machine.** `_finalize_escrow_after_payouts` and `expire_one` both had explicit state lists (`{"PENDING","INITIATED"}` etc.) that predated `SUBMITTING` and would have treated an in-flight payout as inactive/unresolved. Both now include it.
- **Duplicate-receipt checks added in two more places that had the same v20-#5 gap.** `resolve_stk_unknown(PAID)` and `apply_unmatched_c2b_to_order` both wrote a receipt number to an escrow without checking it wasn't already attached to a different one — same class of bug as the C2B confirmation fix from the last round, just in the manual-admin paths this time.
- **`resolve_stk_unknown(PAID)` now requires and validates the actual paid amount**, instead of assuming a manually-entered receipt number necessarily paid the order's exact expected amount.
- **The order-idempotency race (open since v18) is finally closed**: the `IntegrityError` from two truly concurrent requests with the same key is now caught, and the loser returns the winner's order instead of a raw 500.
- **C2B `BusinessShortCode` is now checked** against `MPESA_SHORTCODE` in both Validation (reject) and Confirmation (alert only, since money's already moved) — closes the "don't rely solely on BillRefNumber + amount" gap.
- **B2B payouts now revalidate seller eligibility (active, KYC `VERIFIED`, channel, shortcode) immediately before submission**, not just at payout-creation time (delivery confirmation) — a seller's configuration can legitimately change in between, especially across a retry that happens much later.
- **`EscrowTransaction.payment_method` ("STK"/"C2B") added and populated at all four funding sites**, and `_select_refund_method` now gates `REVERSAL` on it explicitly instead of inferring the funding product from `mpesa_receipt_number`/`stk_callback_received_at`, which both STK and C2B populate identically.
- **`transaction_status_timeout` now actually does something** — marks the matching `TransactionStatusQuery` `TIMEOUT` instead of being a pure ack stub.
- **`admin_payouts` now masks phone recipients** (not B2B shortcodes — those aren't personal data) using the `mask_phone()` helper that already existed for this exact purpose elsewhere.
- **`AccountBalanceCheck` and `TransactionStatusQuery` rows that never receive ANY callback at all** (as opposed to a confirmed queue-timeout, which was already handled) now get swept to a new `TIMEOUT` status by the reconciliation loop, rather than sitting at `PENDING` indefinitely.
- **An explicit `/admin/payouts/{id}/confirm-provider-completed` endpoint** — the counterpart to `reconcile-timeout` that was missing. `TransactionStatusQuery` recorded what Safaricom said; there was no way to actually apply a "COMPLETED" result to a stuck payout without going around the API. Still fully manual by design: the admin supplies the real transaction ID from Safaricom's own records, checked for a clash against other payouts.
- **`TransactionStatusQuery.payout_method`** denormalized from `Payout.method` at query time, for audit records that stand on their own.
- **`UnmatchedC2BPayment.status`** (`UNMATCHED`/`UNDER_REVIEW`/`APPLIED_TO_ORDER`/`REFUNDED_EXTERNALLY`/`WRITTEN_OFF`/`CLOSED`) added alongside the existing `resolved` boolean (kept in sync, nothing reading the old field breaks) so the record says what actually happened, not just whether it's done.
- **Idempotency-Key request fingerprinting.** `Order.idempotency_fingerprint` stores a SHA-256 of the normalized order body; a retry with the same key but a different fingerprint now gets a clear 409 instead of silently returning the original (wrong) order — surfaces a client bug instead of masking it.

Migration: `alembic/versions/0010_payment_method_fingerprint_and_status_fields.py` (the four new columns above; `unmatched_c2b_payments.status` is backfilled from the existing `resolved`/`matched_order_id` columns, everything else is NULL for pre-existing rows since it can't be safely inferred).

### Not done from this round

- **#11 — `recipient_phone` also used to store a B2B shortcode.** A real design smell, but renaming/splitting the field touches every payout-creation and payout-submission call site plus anything reading the column externally. Bigger blast radius than the bug it fixes justifies bundling into this pass; worth doing as its own focused rename.
- **#20 — audit-log free-text review.** Spot-checked: no current `record_audit(...)` call interpolates a raw phone number or other sensitive field into `detail=f"..."` strings; the only real residual risk is an admin typing something sensitive into a free-text `note` field, which isn't something a code change can prevent.
- **#21–24 (national ID encryption, token hashing, Redis-backed rate limiting/velocity guard)** — unchanged from the prior round's assessment: real, but infrastructure/policy decisions, not code-only fixes.
- **#25–27 (Postgres concurrency tests, fuller integration test suite, splitting `escrow_main.py`)** — unchanged from the prior round: correct, sequenced as normal follow-up engineering work.

## v22 corrections (deployment hardening, ahead of the test-suite pass)

Not triggered by an external review — these are the contained, code-only deployment/ops items from the standing "not done" list that don't require a business decision or a large refactor, done now specifically to get infrastructure out of the way before expanding the test suite. Nothing here touches escrow/payment/ledger logic.

- **`/health` endpoint.** There was no liveness/readiness endpoint at all — `/` returns a static payload regardless of DB state. Added `GET /health`: unauthenticated, minimal (`{"status": "ok"}` or a 503), does one real `SELECT 1` round-trip so a broken DB connection is actually visible to an orchestrator instead of only to `/` returning a lie.
- **API docs hidden outside dev mode.** `/docs`, `/redoc`, and `/openapi.json` were always exposed, handing an unauthenticated caller a full map of every endpoint — including the admin surface — before they've proven any credential. Now gated on the same `DB_AUTO_CREATE` "production mode" signal `_validate_production_config` already uses, with an `EXPOSE_API_DOCS=true` escape hatch for a staging environment that's otherwise in production mode.
- **Dockerfile: non-root user, `HEALTHCHECK`, configurable worker count.** The image ran as root with no health check and a hardcoded single worker. Now creates and runs as an unprivileged `escrow` user, adds a `HEALTHCHECK` against the new `/health` endpoint, and reads worker count from `WEB_CONCURRENCY` (default `1`). That default is deliberate, not an oversight — see the in-line comment: `RateLimiter`/`VelocityGuard` are in-process and per-worker, so raising `WEB_CONCURRENCY` today silently weakens both across workers, the same gap already called out for running >1 app instance. Worth fixing together, not one before the other.
- **`docker-compose.yml`: Postgres no longer published to the host by default**, plus health checks on both services and `depends_on: condition: service_healthy` so the API container doesn't start racing Postgres's startup. A financial database reachable on `0.0.0.0:5432` from any compose deployment is an unforced exposure; the port mapping is left as a commented-out, loopback-bound example for local DB-client debugging instead.
- **`requirements.txt` pinned; `requirements.in` added as the source file.** Open-ended `>=` constraints mean `pip install` can resolve different dependency versions today vs. in six months — bad for reproducing a bug and bad for knowing what's actually running in production. Split into `requirements.in` (the loose constraints, edited by hand) and a pinned `requirements.txt` generated from it. **Caveat, stated in both files:** this was pinned without network access to resolve real current versions, so the pins are an exact-pin floor of the previous minimums, not a freshly resolved "latest compatible" set — regenerate for real with `pip-compile requirements.in` before relying on it, and keep doing so on a schedule (Dependabot/Renovate) so security patches land.

### Explicitly not done in this pass, and why

- **`PaymentAttempt` per-attempt history table, `recipient_phone`/B2B-shortcode field split, national-ID encryption at rest, Redis-backed rate limiter/velocity guard.** Each is either a real schema/read-site refactor or an infrastructure dependency this environment doesn't have — exactly the kind of change that should land *with* test coverage proving it, not immediately before writing that coverage. Left for their own focused passes, per the existing v20/v21 assessment.
- **Bearer-token hashing** — already done where it matters. `AdminUser.api_key_hash`/`DeliveryAgent.api_key_hash` are SHA-256 hashes (`hash_api_key`), not plaintext; only the one-time-issued raw key is ever returned to the caller. `buyer_access_token`/`qr_view_token` remain plaintext-but-unique-and-indexed by design — they're opaque per-order session tokens compared with `secrets.compare_digest`, not standing credentials, so hashing them would add lookup cost without a corresponding security gain.
