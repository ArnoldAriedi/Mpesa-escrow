import os

from escrow_models import engine


def test_backend_matches_requested_database():
    """In the CI Postgres step TEST_DATABASE_URL is set; fail loudly if the
    suite silently ran on SQLite instead."""
    if os.environ.get("TEST_DATABASE_URL"):
        assert engine.dialect.name == "postgresql"