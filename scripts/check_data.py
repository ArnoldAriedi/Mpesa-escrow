"""Debug helper: dump stuck payouts and recent unmatched C2B payments from ./escrow.db.

    python scripts/check_data.py
"""
import sqlite3
con = sqlite3.connect("escrow.db")

print("--- 1. Order-backed payouts still INITIATED ---")
rows = con.execute("""
    SELECT p.id, p.escrow_transaction_id, p.purpose, p.amount, p.conversation_id, p.last_attempt_at
    FROM payouts p JOIN escrow_transactions e ON e.id = p.escrow_transaction_id
    WHERE p.status = 'INITIATED' AND e.order_id IS NOT NULL
    ORDER BY p.last_attempt_at""").fetchall()
print(len(rows), "row(s)")
for r in rows: print(r)

print("--- 2. Recent unmatched C2B payments (check msisdn) ---")
for r in con.execute("""
    SELECT id, mpesa_receipt_number, msisdn, length(msisdn), bill_ref_number
    FROM unmatched_c2b_payments ORDER BY id DESC LIMIT 5"""): print(r)

print("--- 2b. Raw payloads of matched C2B payments ---")
for r in con.execute("""
    SELECT provider_reference, raw_payload FROM reconciliation_records
    WHERE provider = 'MPESA_C2B' ORDER BY id DESC LIMIT 5"""): print(r)
