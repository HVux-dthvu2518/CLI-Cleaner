"""SQLite schema and migration strategy for the hybrid cache.

The schema is deliberately small: two independent tables, because directory
listings and recursive folder sizes have different invalidation rules and must
never be conflated.

* ``listings`` - the immediate children of one directory. Validated against the
  directory's own modification time, which Windows/POSIX update whenever a direct
  child is added, removed or renamed.
* ``sizes`` - the recursive logical-size total for one directory. A root
  modification time cannot observe changes deeper in the tree, so these rows are
  validated by root mtime *and* TTL.

Each row stores ``ttl_seconds``: the TTL in force when it was written. Raising the
TTL in settings later must not retroactively make an old row trustworthy.

Both tables are versioned through ``PRAGMA user_version``. A row exists only for
a *complete*, error-free scan; the absence of a row is the cache miss.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

#: Connection pragmas, kept here so documentation and behaviour cannot drift.
PRAGMAS = {
    "journal_mode": "WAL",  # Allows readers during a write and survives crashes.
    "synchronous": "NORMAL",  # Durable enough for a rebuildable cache.
    "busy_timeout": 5000,  # A second app instance waits instead of failing.
}

_TABLES_V1 = """
CREATE TABLE IF NOT EXISTS listings (
    path_key      TEXT    PRIMARY KEY,
    path          TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    ttl_seconds   REAL    NOT NULL DEFAULT 0,
    mtime_ns      INTEGER,
    entry_count   INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    payload       TEXT    NOT NULL,
    skipped       TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sizes (
    path_key          TEXT    PRIMARY KEY,
    path              TEXT    NOT NULL,
    created_at        REAL    NOT NULL,
    ttl_seconds       REAL    NOT NULL DEFAULT 0,
    mtime_ns          INTEGER,
    total_size        INTEGER NOT NULL,
    total_files       INTEGER NOT NULL,
    total_directories INTEGER NOT NULL,
    scanned_entries   INTEGER NOT NULL,
    skipped_count     INTEGER NOT NULL DEFAULT 0,
    skipped           TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_listings_created_at ON listings(created_at);
CREATE INDEX IF NOT EXISTS idx_sizes_created_at ON sizes(created_at);
"""

#: ``version -> SQL`` applied in order when the stored version is older.
MIGRATIONS: dict[int, str] = {1: _TABLES_V1}


def migrations_after(version: int) -> list[tuple[int, str]]:
    """Return the ``(version, sql)`` steps needed to reach ``SCHEMA_VERSION``."""
    return [(target, MIGRATIONS[target]) for target in sorted(MIGRATIONS) if target > version]


def describe() -> str:
    """Human-readable summary of the schema, used by docs and tests."""
    return (
        f"cache schema v{SCHEMA_VERSION}; tables: listings, sizes; "
        f"pragmas: {', '.join(f'{key}={value}' for key, value in PRAGMAS.items())}"
    )
