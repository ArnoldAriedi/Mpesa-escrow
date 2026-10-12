# TIMEOUT payout reconciliation runbook

## Why this exists

A B2C payout reaches `TIMEOUT` when the application sent a payment request to
Safaricom but never received a definitive success/failure response (network
failure, Safaricom queue timeout, etc). At that point **the application does
not know whether the payment actually happened.**

`POST /admin/payouts/{payout_id}/reconcile-timeout` exists to unstick a
`TIMEOUT` payout, but it deliberately requires a human to call it — the
endpoint has no automatic trigger and the background retry sweep never
touches `TIMEOUT` payouts. This is intentional: if the payment actually did
go through on Safaricom's side and the app retries anyway, that's a real
double payment to a real person. Do not skip the verification step below to
save time.

## When you'll see this

- `GET /admin/payouts?status=TIMEOUT` lists payouts stuck in `TIMEOUT`.
- A buyer or rider reports they haven't received an expected refund/payout.
- An ops alert arrives. When a payout first becomes `TIMEOUT` you get a
  `PAYOUT_TIMEOUT` alert. If it is still `TIMEOUT` after
  `STALE_TIMEOUT_REALERT_MINUTES` (default 60), the reconciliation sweep sends
  `PAYOUT_TIMEOUT_STILL_UNRESOLVED`, and repeats it at that interval until the
  payout is resolved. The sweep runs every `RECONCILIATION_INTERVAL_SECONDS`
  (default 6 hours), so a re-alert can arrive up to one sweep interval later
  than the threshold.

## Steps

1. **Identify the payout.** Note its `id`, `escrow_transaction_id`,
   `recipient_phone`, `amount`, `purpose` (REFUND or RELEASE), and
   `originator_conversation_id` (a value like `escrow-payout-{id}-{attempt}`
   that this application generated and sent to Safaricom — record it, you'll
   need it for the lookup below).

2. **Check with Safaricom before touching the database.** Do not rely on
   application state alone. Use one of:
   - The Daraja **Transaction Status Query API**, querying by the
     `ConversationID` recorded on the payout (if present), or
   - Safaricom's own portal / statement for the paybill/till, searching by
     amount, recipient phone, and approximate timestamp, or
   - Your Safaricom relationship manager / business support line for
     high-value or ambiguous cases.

   You are trying to answer one question: **did the recipient actually
   receive this money?**

3. **If Safaricom confirms the payment did NOT go through:**
   Call `POST /admin/payouts/{payout_id}/reconcile-timeout` with your admin
   key. This reverses the internal ledger reservation for that payout and
   marks it `FAILED`, which makes it eligible for a fresh retry (either via
   `POST /admin/payouts/{payout_id}/retry` or the automatic
   `retry_failed_payouts()` sweep). Confirm in the response that
   `reservation_reversed: true`.

4. **If Safaricom confirms the payment DID go through:**
   Do **not** call `reconcile-timeout` — that would let the payout be
   retried and pay the recipient a second time. Instead call
   `POST /admin/payouts/{payout_id}/confirm-provider-completed` with a JSON
   body:

```json
   {"transaction_id": "<the real M-Pesa receipt>", "note": "<how you verified it>"}
```

   This requires the `FINANCE_ADMIN` role. In one database transaction it:
   - marks the payout `COMPLETED` and records the receipt and completion time,
   - finalizes the escrow and order status,
   - writes an audit log entry, and
   - after commit, sends the finalization SMS to the buyer, seller and rider.

   It returns `409` if the payout is not in `TIMEOUT`, `SUBMITTING` or
   `INITIATED`, or if that `transaction_id` is already recorded against a
   different payout (a guard against applying one receipt twice). Put the
   verification detail in `note`, since it is stored on the payout and in the
   audit log. Do not edit the `Payout` row by hand.

5. **If Safaricom's answer is ambiguous or you can't get a definitive
   answer:** Leave the payout in `TIMEOUT`. Escalate to whoever owns the
   Safaricom relationship. Do not guess.

## Who should have access
Only trusted operations/finance staff should hold the admin API key that can
call `reconcile-timeout`. Treat it the same as anyone with the authority to
directly move money — because functionally, that's what it is.
