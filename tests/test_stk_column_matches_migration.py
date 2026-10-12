"""EscrowTransaction.stk_last_queried_at must be identical whether the table is
built from the model (tests) or from the migrations (production)."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _column(db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = {r[1]: (r[2].upper().split("(")[0], bool(r[3])) for r in con.execute("PRAGMA table_info('escrow_transactions')")}
        return rows.get("stk_last_queried_at")
    finally:
        con.close()


def test_stk_last_queried_at_matches_the_migration():
    with tempfile.TemporaryDirectory() as tmp:
        migrated = os.path.join(tmp, "migrated.db")
        modeled = os.path.join(tmp, "modeled.db")
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{migrated.replace(os.sep, '/')}"
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                       check=True, capture_output=True)
        script = (
            "import asyncio, os\n"
            f"os.environ['DATABASE_URL']='sqlite+aiosqlite:///{modeled.replace(os.sep, '/')}'\n"
            "from escrow_models import Base, engine\n"
            "async def go():\n"
            "    async with engine.begin() as c:\n"
            "        await c.run_sync(Base.metadata.create_all)\n"
            "    await engine.dispose()\n"
            "asyncio.run(go())\n"
        )
        env2 = dict(os.environ)
        env2.pop("DATABASE_URL", None)
        subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env2, check=True, capture_output=True)
        m, o = _column(migrated), _column(modeled)
    assert m is not None and o is not None
    assert m == o