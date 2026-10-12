"""Verify critical FK constraints before stamping an existing database.

    python scripts/verify_schema.py   (needs DATABASE_URL; see docs/TESTING.md)
"""
import os
from sqlalchemy import create_engine, inspect

URL = os.getenv("DATABASE_URL", "sqlite:///./escrow.db").replace("+asyncpg", "").replace("+aiosqlite", "")
engine = create_engine(URL)
inspector = inspect(engine)

required = [
    ("order_items", "orders", "RESTRICT"),
    ("deliveries", "orders", "RESTRICT"),
    ("escrow_transactions", "orders", "RESTRICT"),
    ("ledger_transactions", "escrow_transactions", "RESTRICT"),
    ("ledger_entries", "ledger_transactions", "RESTRICT"),
    ("ledger_entries", "ledger_accounts", "RESTRICT"),
]

failed = []
for table, target, expected in required:
    found = False
    for fk in inspector.get_foreign_keys(table):
        if target in fk.get("referred_table", ""):
            ondelete = (fk.get("options") or {}).get("ondelete", "").upper()
            found = ondelete == expected
            if not found:
                failed.append(f"{table} -> {target}: expected ON DELETE {expected}, found {ondelete or 'NONE'}")
            break
    if not found and not any(table in x for x in failed):
        failed.append(f"{table} -> {target}: foreign key not found")

if failed:
    print("SCHEMA CHECK FAILED")
    for item in failed:
        print(" -", item)
    raise SystemExit(1)

print("SCHEMA CHECK PASSED: critical financial foreign keys use RESTRICT.")
