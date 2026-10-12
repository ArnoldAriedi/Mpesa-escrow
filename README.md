# Escrow Delivery Platform — Steps 4–11

This package extends the original `escrow_core.py`, `escrow_main.py`, and `escrow_models.py` into a single escrow delivery backend.

## Where to find things

| You want to... | Go to |
|---|---|
| Start the API | `uvicorn escrow_main:app --reload` (the FastAPI `app` lives in `escrow_main.py`) |
| Change an endpoint or background job | `escrow_main.py`. Search for the route path (`/orders`, `/admin/...`, `/callbacks/...`, `/ussd/callback`) or the function name (`initiate_payout_for_record`, `expiry_loop`, `reconciliation_loop`, `stk_recovery_sweep`) |
| Change a table, status enum or ledger rule | `escrow_models.py` (schema, `post_ledger_transaction`, `record_unapplied_payment`) |
| Change a Daraja call or callback parser | `escrow_core.py` (STK, B2C, B2B, Reversal, status/balance queries, `parse_*_callback`, `callback_url`) |
| Change SMS sending or retries | `notification_service.py` |
| Change ops alerts (Slack webhook) | `ops_alerts.py` |
| Add a database migration | `alembic/versions/` (config in `alembic.ini`; see [docs/TESTING.md](docs/TESTING.md)) |
| Set environment variables | copy `.env.example` to `.env`; production settings are listed in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Run the tests | `tests/` (shared fixtures in `tests/conftest.py` and `tests/support.py`); procedure in [docs/TESTING.md](docs/TESTING.md) |
| Register C2B URLs with Safaricom | `python -m scripts.register_c2b_urls` |
| Check the schema before stamping a database | `python scripts/verify_schema.py` |
| Capture real Safaricom callbacks in the sandbox | `scripts/sandbox_proxy.py`, or `ops/ngrok-policy.yml.example` for ngrok |
| Go live | [docs/PRODUCTION_CHECKLIST.md](docs/PRODUCTION_CHECKLIST.md), then [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Handle a stuck payout | [docs/TIMEOUT_RECONCILIATION_RUNBOOK.md](docs/TIMEOUT_RECONCILIATION_RUNBOOK.md) |
| See open risks and past fixes | [docs/REVIEW_FINDINGS.md](docs/REVIEW_FINDINGS.md), [docs/CORRECTIONS.md](docs/CORRECTIONS.md) |
| Understand the M-Pesa / USSD limits | [docs/mpesa_integration_notes.md](docs/mpesa_integration_notes.md) |
| See what the code cleanup changed | [docs/REWRITE_NOTES.md](docs/REWRITE_NOTES.md) |

## What is included

### Step 4 — Financial ledger
- Double-entry ledger
- M-Pesa clearing account
- Escrow holding account
- Rider/seller payable accounts
- Buyer refund payable account
- Idempotency keys
- Ledger inspection endpoint

### Step 5 — Orders
- Seller registration
- Order creation
- Order items
- Delivery address
- Order/escrow/delivery relationships
- Agent assignment

### Step 6 — Notifications
- Notification records
- SMS provider adapter
- Buyer escrow/QR messages
- Rider delivery messages
- Development fallback logging

### Step 7 — Buyer API
- Create order
- M-Pesa STK Push
- Buyer access token
- Order status
- Escrow status
- QR retrieval
- Disputes

### Step 8 — Rider API
- Agent registration
- Agent API keys
- Assigned deliveries
- Accept/pickup/out-for-delivery
- QR confirmation

### Step 9 — Disputes/refunds
- Open dispute
- Admin resolution
- Full refund
- Full release
- Partial split
- M-Pesa B2C settlement

### Step 10 — Reconciliation/reliability
- STK reconciliation records
- B2C payout records
- Idempotent callbacks
- Failed payout retry
- Admin payout inspection
- Background expiry/refund sweep

### Step 11 — USSD/M-Pesa integration layer
- `/ussd/callback` application endpoint
- Main menu, order status, payment initiation and cancellation
- Existing Daraja STK/B2C integration

## Important integration limitation

Python cannot independently insert a new menu item into the official M-Pesa app or the live `*334#` menu. Those channels require the appropriate Safaricom/M-Pesa commercial and technical onboarding. This project provides the backend/API contract that an approved USSD/M-Pesa integration can call.

Similarly, the SMS adapter now has a real Africa's Talking integration, set `SMS_PROVIDER=africastalking` and the `AFRICASTALKING_*` variables. A generic-webhook option (`SMS_PROVIDER=generic`) remains for any other provider. Configure one of these before production use.

## Run locally

1. Install Python 3.11+.
2. Create a virtual environment.
3. Install requirements:

```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in the M-Pesa credentials.
5. Run:

```bash
uvicorn escrow_main:app --reload
```

6. Open:

`http://127.0.0.1:8000/docs`

## Database

The development version uses SQLite by default. For production use PostgreSQL with `asyncpg`.

`init_db()` creates missing tables only when `DB_AUTO_CREATE=true`. For production set `DB_AUTO_CREATE=false`, run `alembic upgrade head`, and never rely on `create_all()` for schema evolution.

## Main lifecycle

```text
Order created
    -> PENDING_PAYMENT
    -> M-Pesa STK Push
    -> STK callback success
    -> ESCROW_HELD
    -> QR generated (3 hours)
    -> Rider delivers
    -> Rider scans QR
    -> DELIVERY_CONFIRMED
    -> payout reservation
    -> M-Pesa B2C
    -> B2C callback success
    -> RELEASED / COMPLETED
```

Timeout path:

```text
ESCROW_HELD
    -> QR expires after 3 hours
    -> REFUND_INITIATED
    -> M-Pesa B2C refund
    -> REFUNDED
```

## Buyer QR flow

After a verified STK callback, the backend creates:

1. a three-hour rider-scanning QR token;
2. a separate buyer QR-view token;
3. an SMS containing the buyer QR-view URL when `BUYER_PORTAL_BASE_URL` is configured.

The buyer-facing API uses the buyer access token and does not require the platform API key. QR responses are marked `no-store`.

## Security notes

- Keep `ESCROW_API_KEY`, `ADMIN_API_KEY`, M-Pesa secrets and callback secrets out of source control.
- Use HTTPS for all production callbacks.
- Use PostgreSQL in production.
- Add proper Alembic migrations before changing an existing production schema.
- Treat M-Pesa callbacks as untrusted input and reconcile against stored transaction identifiers, amount, payer phone, merchant request ID, and receipt number.
- Configure `MPESA_CALLBACK_SECRET` as a long random secret. M-Pesa callback endpoints authenticate with the `X-Mpesa-Secret` HTTP header; the secret is never placed in the callback URL.
- Do not blindly retry B2C requests after a network timeout. A timeout is an unknown external outcome and must be reconciled before another payout is attempted.
- Do not treat an STK request initiation response as proof of payment; only the verified callback/reconciliation flow should move funds into escrow.
- Do not expose admin endpoints to the public internet without additional controls such as network restrictions, strong authentication and audit logging.
- Financial custody/escrow may require licensing, safeguarding arrangements and/or a regulated payment partner in Kenya. The code does not itself grant regulatory authorization.
