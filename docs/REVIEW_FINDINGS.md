# Money-path review findings

Status key: [ ] open, [x] fixed. Details are in the review conversation.

## Critical
- [x] C1  Sent payout can be marked FAILED then auto-retried (fixed: phased initiate_payout_for_record, unknown outcomes park as TIMEOUT)
- [x] C2  HTTP 5xx treated as definite rejection (fixed: MpesaOutcomeUnknown + raise_for_definite_status)
- [x] C3  Retry sweep loses row locks after internal commit; no backoff (fixed: per-payout lock + re-check, 5 min backoff)
- [x] C4  Late/stale/racing callbacks (fixed: matched by originator attempt; stale results recorded and alerted, never applied; early callbacks matched before ConversationID is stored; same in timeout handler). Note: an odd order-dependent PRAGMA result after test_two_workers_only_one_can_claim_expired_escrow was never explained
- [x] C5  (STK side) successful payments that cannot be applied are recorded in unapplied_payments + ledger, alerted once, acknowledged with 200 (never refunded automatically). STILL OPEN: C2B confirmation path, successful payout callback with no receipt (400), admin tool to resolve unapplied payments
- [ ] C6  B2B destination read live at send time instead of from the Payout row
- [ ] C7  Callback auth relies on a proxy that is not in the repo (verify IP allowlist + header strip)

## High
- [ ] H1  SUBMITTING/INITIATED payouts cannot be reconciled as unpaid
- [ ] H2  Alerts run inside money transactions (verify ops_alerts never raises)
- [ ] H3  SMS sent inside money transactions (verify send_sms)
- [ ] H4  No over-settlement cap or alert (create_payout, finalize)
- [~] H5  Ledger overdraft guard DONE (per-escrow ESCROW_HOLDING / SELLER_PAYABLE / RIDER_PAYABLE / BUYER_REFUND_PAYABLE can never go negative; UNAPPLIED_PAYMENTS globally). STILL OPEN: idempotency-key reuse not compared against the original posting, 2-decimal precision check, debit != credit check, append-only enforcement
- [ ] H6  Privilege gaps: stk-unknown, unmatched resolve, agent/seller register, single-admin reconcile, legacy admin key
- [ ] H7  Audit entries written after money moves
- [ ] H8  Partial refund via REVERSAL; REVERSAL method never falls back to B2C

## Medium
- [ ] M1  Rider can be payee and confirmer when seller_id omitted
- [ ] M2  DB connection held during STK call
- [ ] M3  C2B match requires phone (masked MSISDN on newer Daraja)
- [ ] M4  Order amount bounds / M-Pesa limits (verify CreateOrderRequest)
- [ ] M5  Last-SUPER_ADMIN race
- [ ] M6  Payout-channel change: validation, in-flight check, second approver
- [ ] M7  Idempotency fingerprint sensitive to 10 vs 10.00
- [ ] M8  In-memory rate/velocity limits, proxy IP key, no per-phone STK throttle
- [ ] M9  national_id plain text; KYC not checked at order creation
- [ ] M10 Token/credential fetched after reservation posted
- [ ] M11 compare_digest non-ASCII 500; M12 same header name for platform/admin keys
- [ ] M13 Callback robustness (bad JSON, tx None, late failure on COMPLETED, timeout status mismatch)
- [ ] M14 DB_AUTO_CREATE default true; ensure_system_accounts per posting
- [ ] M15 Raw exception text returned to callers

## Business decisions
- [x] B1  QR expiry no longer auto-refunds once the delivery is OUT_FOR_DELIVERY or DELIVERED: the escrow and order become DISPUTED, a dispute is opened, an alert is sent, and an admin settles it with /admin/disputes/{id}/resolve
- [ ] B2  Buyer can dispute just before the rider scans
- [ ] B3  Full amount (incl. delivery fee) goes to seller; confirm commission/rider fee plan

## Housekeeping
- [ ] CI on Postgres (SKIP LOCKED / FOR UPDATE untestable on SQLite)
- [ ] Move root scripts into scripts/, delete .bak files
- [ ] Replace docker-compose Postgres password before deploy
- [ ] Confirm password_hash uses bcrypt/argon2
## Round 2 updates
- H2 downgraded to Low: alert never raises; move alerts after commit
- H3 refined: SMS is inside the lock/transaction (delays callback ack, expiry sweep); no rollback risk
- H1 partly solved: stale SUBMITTING becomes TIMEOUT; stale INITIATED still cannot be reversed
- B1 confirmed: expire_one refunds regardless of delivery status
- M4 confirmed: no upper bound on amount/delivery_fee, no cap on items, no buyer_phone length limit
- [x] N1  One failing escrow blocks all expiry refunds (fixed: one transaction per escrow, alerted once)
- [x] N2  Stale INITIATED payouts re-queried every sweep, starving the 11th+ (fixed: 24h cooldown per payout; note the sweep runs every 6h, not every tick)
- [ ] N3  Status query may need the receipt, not the ConversationID (verify in sandbox)
- [ ] N4  PAYOUT_STALE_INITIATED_MINUTES must exceed the HTTP timeout; re-check status before overwriting
- [ ] N5  Dev SMS fallback prints phone + QR link; refuse empty SMS_PROVIDER in production
## Round 3 updates
- Resolved: buyer endpoints check ownership (404 on mismatch); USSD scopes to caller phone, locks session, atomic cancel
- [ ] N6  No STK Push Query and no sweep for stale PENDING_PAYMENT: lost STK callbacks never recovered
- [x] N7  USSD cancel is refused while an STK request is in flight; a payment that arrives after a cancellation is held in unapplied_payments (C5)
- [x] N8  USSD pay path locks the escrow row before pushing; the 5xx-as-definite part was already fixed by C2 (tested: unknown outcome blocks a retry and alerts)
- [ ] N9  USSD request/response format vs gateway (JSON vs form; quoted JSON vs plain text) - verify in sandbox
- [ ] N10 Admin rate limiter runs before auth and keys on proxy IP: can lock out all admins
- [ ] N11 USSD request fields have no length limits
- [ ] N12 Verify get_tx_buyer, verify_ussd_secret (fail closed), generate_qr_token (token_urlsafe, >=24 bytes)
- M14 stronger: production posture depends on DB_AUTO_CREATE=false, default is insecure; invert default
- C2/C5/C7/H6 extended (USSD 5xx, cancel trigger, USSD secret via proxy, assign-agent any admin role)
- [ ] B4  Buyer QR link token in URL query (logs/history); low
## Round 4 updates (review complete)
- N4 resolved. H4 downgraded to Medium (unique(escrow,purpose) + atomic claims already block most double settlement)
- [x] N13 assign_agent locks the escrow, refuses once the escrow has moved on or the delivery is picked up, and never rewrites payee_phone after payment. STILL OPEN: it accepts any admin role (restrict to an operations role)
- [x] N14 Payout result parser (fixed: tolerant int codes, top-level TransactionID, single-object lists, UNKNOWN outcome leaves the payout untouched and alerts). Still open: a SUCCESS callback with no receipt raises 400 (C5 group)
- [ ] N15 verify_ussd_secret fails open when unset; not in startup required vars
- [ ] N16 Legacy /escrow/create calls create_order directly (Header default object); remove or fix; rider-as-payee path
- [x] N17 accept/pickup/out-for-delivery lock the delivery and require the escrow to be ESCROW_HELD
- [ ] N18 Reconciliation loop is 6h; STK recovery needs its own ~60s loop; fail only on definite answers
- [x] N19 Payout status/purpose/method CHECK constraints + originator_conversation_id index (migration 0012; migration 0008 made SQLite-safe). Alembic chain now also runs in CI on PostgreSQL
- [ ] N20 normalize_phone_number: validate ^254[17]\d{8}$
- [ ] N21 query-status holds payout lock during HTTP call
- [ ] N22 USSD new-session insert race
- H5 extended: per-escrow overdraft invariant + global debit/credit check; H6 extended: balance/ledger/reconciliation endpoints open to any admin
## Round 5 updates
- [ ] N23 conftest fresh_db drops all tables on TEST_DATABASE_URL: non-test-database guard added in CI commit
- [ ] N24 B2B AccountReference truncated to 12 chars (verify generate_public_ref length)
- C2 fix location: replace raise_for_status() at escrow_core.py send sites (STK, B2C, Reversal, B2B); keep it in get_access_token
- M10 confirmed in all four request builders (token/credential fetched inside the send try block)
- N16: no test covers /escrow/create
- [x] CI on PostgreSQL (commit 1)
## Round 6 (C5 foundation)
- [x] unapplied_payments table + UNAPPLIED_PAYMENTS ledger account + record_unapplied_payment helper (migration 0013). Not yet used by the callbacks: STK wiring is the next commit.
## Round 6b (C5 STK wiring)
- [x] stk_callback: NO_MATCHING_ESCROW, ESCROW_NOT_AWAITING_PAYMENT, INCOMPLETE_CALLBACK, AMOUNT_OR_PHONE_MISMATCH, DUPLICATE_RECEIPT, MERCHANT_REQUEST_MISMATCH are held, not dropped
- N7 receiving half done (buyer cancels, then pays anyway: money is held). STILL OPEN: the USSD cancel itself should refuse while an STK request is in flight
- [x] N6  Lost STK callbacks: query_stk_status + 60s stk_recovery_loop (escrows 3 min to 24 h old, one query per 10 min each, one transaction per escrow). PAID is parked as STK_INITIATION_UNKNOWN for an admin (the query answer has no receipt); FAILED cancels; unknown or failed queries change nothing. Daraja response shapes must be confirmed in the sandbox
- [x] Admin endpoints to list and resolve unapplied_payments: GET /admin/unapplied-payments and POST /admin/unapplied-payments/{id}/resolve (FINANCE_ADMIN, audited, REFUNDED_EXTERNALLY only, refund receipt required, ledger cleared once). NOT built: apply-to-order, write-off (needs a business/legal decision), second-approver rule
- [ ] C2B confirmation: record into unapplied_payments instead of UnmatchedC2BPayment (or link the two)
- Lesson: handlers that roll back then reuse ORM objects hit MissingGreenlet; capture plain ids before rollback()
## Round 7 (N6 follow-ups)
- [ ] After the sweep parks an escrow as PAID and the real callback arrives later, the payment is held in unapplied_payments AND the escrow awaits resolve-stk-unknown. An admin must close both; consider linking them (resolve-stk-unknown should also close the matching unapplied row)
- [ ] Confirm in the Daraja sandbox: STK Query response fields (ResponseCode, ResultCode as strings), and that a "still processing" answer comes back as an error body, not as ResultCode 0
- [ ] Migration 0014 added escrow_transactions.stk_last_queried_at (nullable)
## Round 8 (USSD)
- [x] USSD handler now has behavioural tests (tests/test_ussd_flow.py, 13 tests); it had none
- [x] Found and fixed: session expiry check subtracted a naive datetime (SQLite) from an aware one; raised TypeError on every second USSD request under SQLite (Postgres unaffected)
- [ ] N9  USSD request/response format vs the real gateway (JSON vs form-encoded; plain text vs JSON string): verify in the gateway sandbox
- [x] N15 verify_ussd_secret now fails closed (unset -> refuse; handler answers 503; non-ASCII header no longer crashes: M11)
- [ ] N22 two simultaneous first requests for a new USSD session_id both insert it; one gets a 500
- [ ] The overlapping-pay test passes on SQLite, which ignores row locks; the PostgreSQL CI step is the real proof of the lock
## Round 9 (fail closed)
- [x] M11 non-ASCII X-Ussd-Secret no longer raises (compare as bytes)
- [x] M12 production mode (DB_AUTO_CREATE=false) refuses to start with missing, short (<32) or reused ESCROW_API_KEY / ADMIN_API_KEY / MPESA_CALLBACK_SECRET / USSD_CALLBACK_SECRET
- [ ] M14 DEPLOYMENT RISK: production mode is only active when DB_AUTO_CREATE=false; the default (true) means CORS *, API docs exposed and auto-created tables. Always set DB_AUTO_CREATE=false in production
- [ ] USSD_CALLBACK_SECRET is now required for USSD to work at all: set it (and the X-Ussd-Secret header at the proxy) before enabling USSD
- [ ] N16 legacy /escrow/create still broken/untested; remove or fix (product decision)
## Round 10 (money redirection)
- [ ] C6  B2B payout sends to the seller's LIVE shortcode at send time; should use the shortcode stored on the payout and stop if the seller's differs (needs the B2B payout tests first)
- [ ] H6  register_agent / register_seller write no audit entry and accept any admin role (both lack an admin-actor argument)
- [ ] assign_agent is still open to any admin role
## Round 11 (B1 notes)
- PICKED_UP still refunds automatically at QR expiry; add DeliveryStatus.PICKED_UP to _HANDOVER_STARTED if the business wants the stricter rule
- A rider who hands over goods without ever marking out-for-delivery is not detected by this rule
- QR validity is 3 hours from payment, so a long but honest delivery now goes to admin review instead of an instant refund; watch the volume of EXPIRY_HELD_FOR_REVIEW alerts
## Round 12 (ledger overdraft guard)
- [x] post_ledger_transaction now refuses a posting that would take a guarded liability account below zero (LedgerOverdraft). ON by default; LEDGER_OVERDRAFT_GUARD=false disables it
- NOTE: the legacy tests build escrows by hand WITHOUT the MPESA_CLEARING -> ESCROW_HOLDING hold entry a real payment posts, so tests/conftest.py turns the guard OFF for them. Only tests/test_ledger_guard.py (12 tests, escrows built through the real STK callback) run with it ON. If the legacy tests are ever rebuilt on real flows, remove the _ledger_guard_off_by_default fixture
- A refused posting raises LedgerOverdraft. initiate_payout_for_record already handles a failing reservation safely (payout FAILED before anything is sent); in callback paths it surfaces as a 500, which is intended: a ledger inconsistency needs a human
- [ ] Confirm in production-like data that no real flow posts a debit before its credit (the guard is checked after the posting is flushed, so order within one transaction is fine)