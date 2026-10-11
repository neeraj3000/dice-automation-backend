"""
Every test run is pinned to an isolated MongoDB database so tests can never read or
write production data. Loaded by pytest before any test module imports the app.
"""
import os
import sys
from pathlib import Path

TEST_DATABASE_NAME = os.environ.get("TEST_DATABASE_NAME", "apply2hire_testversion")
os.environ["DATABASE_NAME"] = TEST_DATABASE_NAME
os.environ["MONGODB_DB"] = TEST_DATABASE_NAME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

settings.DATABASE_NAME = TEST_DATABASE_NAME
settings.MONGODB_DB = TEST_DATABASE_NAME
assert "test" in settings.DATABASE_NAME, f"Refusing to run tests against non-test DB {settings.DATABASE_NAME!r}"
