"""Open existing controller ledgers without provisioning replacement state."""

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal


class ExistingStoreError(ValueError):
    """Content-free storage failure for a recovery/bootstrap boundary."""


def open_database(
    path: Path,
    *,
    existing_only: bool,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
) -> sqlite3.Connection:
    try:
        target = path.resolve().as_uri() + "?mode=rw" if existing_only else path
        return sqlite3.connect(
            target, uri=existing_only, timeout=10, isolation_level=isolation_level
        )
    except (OSError, sqlite3.Error):
        raise ExistingStoreError("EXISTING_STORE_UNAVAILABLE") from None


def require_schema(path: Path, tables: Mapping[str, Sequence[str]]) -> None:
    """Check module-owned required columns through a read-only connection."""
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            actual = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table, required in tables.items():
                if table not in actual or not table.replace("_", "").isalnum():
                    raise ExistingStoreError("EXISTING_STORE_SCHEMA_UNSUPPORTED")
                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if not set(required).issubset(columns):
                    raise ExistingStoreError("EXISTING_STORE_SCHEMA_UNSUPPORTED")
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        raise ExistingStoreError("EXISTING_STORE_UNAVAILABLE") from None
