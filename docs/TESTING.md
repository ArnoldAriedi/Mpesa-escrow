# Database migration and test procedure

> File locations: see [Where to find things](../README.md#where-to-find-things) in the README.

## Fresh database

```bash
export DATABASE_URL=sqlite+aiosqlite:///./escrow.db
alembic upgrade head
pytest -q
```

For PostgreSQL:

```bash
export DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DBNAME
alembic upgrade head
pytest -q
```

## Existing database created by `Base.metadata.create_all`

Do not blindly run the baseline migration against an existing database.
First back up the database, then run:

```bash
python scripts/verify_schema.py
```

If the check passes, stamp the existing schema:

```bash
alembic stamp 0001_initial_escrow_schema
alembic upgrade head
```

If the check fails, stop and create a database-specific migration rather than
recreating the database. Financial history must not be destroyed.

## What the tests prove

- Orders with financial history cannot be hard-deleted.
- Ledger transactions cannot cascade-delete ledger entries.
- A single escrow cannot have two REFUND/RELEASE payout records.
- Two expiry workers cannot both claim the same expired escrow.
- USSD state is persisted and restored from `USSDSession.state`.

The concurrency test specifically exercises the conditional `HELD -> EXPIRED`
claim, which is the final guard even when a database ignores `FOR UPDATE`.
