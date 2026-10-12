"""The unapplied_payments table must be identical whether built from the
SQLAlchemy model (what the tests use) or from migration 0013 (what production
uses). A drift between the two would only show up in production."""
import os
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _table_shape(db_path):
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1]: (r[2].upper().split("(")[0], bool(r[3])) for r in con.execute("PRAGMA table_info('unapplied_payments')")}
        idx = sorted(
            tuple(i[2] for i in con.execute(f"PRAGMA index_info('{r[1]}')"))
            for r in con.execute("PRAGMA index_list('unapplied_payments')")
        )
        return cols, idx
    finally:
        con.close()


def test_migration_0013_matches_the_model():
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

        m_cols, m_idx = _table_shape(migrated)
        o_cols, o_idx = _table_shape(modeled)
    assert m_cols == o_cols
    assert m_idx == o_idx