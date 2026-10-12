"""Static model-level checks that do not require a database driver.

    python -m scripts.check_models
"""
from escrow_models import Base

checks = {
    "order_items": ("orders", "RESTRICT"),
    "deliveries": ("orders", "RESTRICT"),
    "escrow_transactions": ("orders", "RESTRICT"),
    "ledger_transactions": ("escrow_transactions", "RESTRICT"),
    "ledger_entries": ("ledger_transactions", "RESTRICT"),
    "ledger_entries_account": ("ledger_accounts", "RESTRICT"),
}

failed = []
for name, (target, expected) in checks.items():
    table = "ledger_entries" if name == "ledger_entries_account" else name
    if table not in Base.metadata.tables:
        failed.append(f"missing table: {table}")
        continue
    found = False
    for fk in Base.metadata.tables[table].foreign_keys:
        if fk.target_fullname.endswith(f"{target}.id"):
            found = (fk.ondelete or "").upper() == expected
            if not found:
                failed.append(f"{table} -> {target}: expected {expected}, got {fk.ondelete}")
            break
    if not found and not any(table in x and target in x for x in failed):
        failed.append(f"{table} -> {target}: FK missing")

payout = Base.metadata.tables["payouts"]
if not any(set(c.columns.keys()) == {"escrow_transaction_id", "purpose"} for c in payout.constraints if getattr(c, "unique", False)):
    failed.append("payouts: missing unique escrow_transaction_id + purpose constraint")

if failed:
    print("MODEL CHECK FAILED")
    print("\n".join(f" - {x}" for x in failed))
    raise SystemExit(1)
print("MODEL CHECK PASSED")
