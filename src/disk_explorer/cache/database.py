"""SQLite connection management, transactions and corruption recovery.

The cache is a disposable accelerator: no user data lives here, so every failure
mode must degrade to "cache miss" rather than to a broken application. This
module owns that contract:

* one connection guarded by a re-entrant lock (safe to call from worker threads);
* explicit ``BEGIN``/``COMMIT`` (``isolation_level=None``) so transaction
  boundaries are visible in the code rather than implied by the driver;
* WAL + ``busy_timeout`` so a second app instance waits instead of failing;
* ``sqlite3.DatabaseError`` triggers a one-shot recovery that moves the damaged
  file aside and starts a fresh schema;
* any unrecoverable problem raises :class:`CacheUnavailable`, which callers treat
  as a miss.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from disk_explorer.cache import schema


class CacheUnavailable(RuntimeError):
    """Raised when the cache cannot serve a request; callers must rescan."""


class SchemaTooNew(sqlite3.DatabaseError):
    """The cache file belongs to a newer build; it must not be discarded."""


class Database:
    """Owns the SQLite file, its schema version and a single shared connection."""

    def __init__(self, path: str | Path, logger: logging.Logger | None = None) -> None:
        self.path = Path(path)
        self.logger = logger or logging.getLogger("disk_explorer")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self.unavailable_reason: str | None = None
        self.recovery_count = 0
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._connection is not None

    def open(self) -> bool:
        """Open (creating the file if needed) and migrate. Never raises."""
        with self._lock:
            if self._connection is not None:
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                return self._degrade(f"cannot create cache directory: {error}")
            try:
                connection = self._connect()
                self._apply_pragmas(connection)
                self._verify(connection)
                self._migrate(connection)
            except SchemaTooNew as error:
                # A downgrade, not corruption: keep the file for the newer build.
                self._close_quietly()
                return self._degrade(str(error))
            except sqlite3.DatabaseError as error:
                # Typical cause: a truncated or overwritten file ("file is not a
                # database", "database disk image is malformed").
                self._close_quietly()
                if not self._recover_files(error):
                    return False
                try:
                    connection = self._connect()
                    self._apply_pragmas(connection)
                    self._migrate(connection)
                except (sqlite3.Error, OSError) as retry_error:
                    return self._degrade(f"cache recovery failed: {retry_error}")
            except OSError as error:
                return self._degrade(f"cannot open cache database: {error}")

            self._connection = connection
            self.unavailable_reason = None
            return True

    def close(self) -> None:
        with self._lock:
            self._close_quietly()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None -> autocommit; transactions are explicit below.
        return sqlite3.connect(
            str(self.path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )

    @staticmethod
    def _apply_pragmas(connection: sqlite3.Connection) -> None:
        for key, value in schema.PRAGMAS.items():
            try:
                connection.execute(f"PRAGMA {key}={value}")
            except sqlite3.OperationalError:
                # WAL is rejected on some network/special filesystems; the default
                # journal mode still works, so this is not fatal.
                continue

    @staticmethod
    def _verify(connection: sqlite3.Connection) -> None:
        """Cheap readability probe; raises DatabaseError for garbage files."""
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.execute("PRAGMA user_version").fetchone()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        (version,) = connection.execute("PRAGMA user_version").fetchone()
        if version > schema.SCHEMA_VERSION:
            raise SchemaTooNew(
                f"cache schema v{version} is newer than this build (v{schema.SCHEMA_VERSION}); "
                "using a fresh scan instead of discarding it"
            )
        for target, sql in schema.migrations_after(version):
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version={target}")
            self.logger.info(
                "Cache schema migrated",
                extra={"operation": "cache-migrate", "target": str(self.path), "result": f"v{target}"},
            )

    # -- recovery ----------------------------------------------------------
    def _recover_files(self, error: BaseException) -> bool:
        """Move the damaged database aside so a valid one can be created."""
        self.recovery_count += 1
        self.last_error = str(error)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            if self.path.exists():
                self.path.replace(backup)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
        except OSError as move_error:
            self.last_error = f"{error}; could not move damaged cache: {move_error}"
            self._degrade(self.last_error)
            return False
        self.logger.warning(
            "Recovered from a damaged cache database",
            extra={"operation": "cache-recover", "target": str(backup), "result": "recreated"},
        )
        return True

    def recover(self) -> bool:
        """Public recovery entry point used when an operation fails mid-flight."""
        with self._lock:
            self._close_quietly()
            if not self._recover_files(CacheUnavailable("cache operation failed")):
                return False
            try:
                connection = self._connect()
                self._apply_pragmas(connection)
                self._migrate(connection)
            except (sqlite3.Error, OSError) as error:
                return self._degrade(f"cache recovery failed: {error}")
            self._connection = connection
            self.unavailable_reason = None
            return True

    def _degrade(self, reason: str) -> bool:
        self.unavailable_reason = reason
        self.last_error = self.last_error or reason
        self.logger.warning(
            "Cache unavailable; falling back to scanning",
            extra={"operation": "cache-open", "target": str(self.path), "result": reason},
        )
        return False

    def _close_quietly(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass

    # -- access ------------------------------------------------------------
    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Run statements in one explicit transaction.

        ``write=True`` uses ``BEGIN IMMEDIATE`` so concurrent writers fail fast
        (after ``busy_timeout``) instead of deadlocking on an upgrade.
        """
        with self._lock:
            connection = self._require()
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                connection.execute("COMMIT")

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        """Execute one statement in autocommit mode; return affected row count."""

        def run() -> int:
            with self._lock:
                cursor = self._require().execute(sql, parameters)
                return cursor.rowcount

        return self._guard(run)

    def fetch_one(self, sql: str, parameters: tuple[object, ...] = ()) -> tuple | None:
        def run() -> tuple | None:
            with self._lock:
                return self._require().execute(sql, parameters).fetchone()

        return self._guard(run)

    def fetch_all(self, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
        def run() -> list[tuple]:
            with self._lock:
                return self._require().execute(sql, parameters).fetchall()

        return self._guard(run)

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CacheUnavailable(self.unavailable_reason or "cache database is not open")
        return self._connection

    def _guard(self, operation):
        """Run ``operation``, recovering once from corruption, else degrade."""
        try:
            return operation()
        except sqlite3.OperationalError as error:
            # "database is locked", "disk I/O error": transient, do not destroy data.
            self.last_error = str(error)
            raise CacheUnavailable(str(error)) from error
        except sqlite3.DatabaseError as error:
            self.last_error = str(error)
            self.logger.warning(
                "Cache read/write failed; rebuilding",
                extra={"operation": "cache-query", "target": str(self.path), "result": str(error)},
            )
            if self.recover():
                try:
                    return operation()
                except sqlite3.Error as retry_error:
                    raise CacheUnavailable(str(retry_error)) from retry_error
            raise CacheUnavailable(str(error)) from error

    def table_counts(self) -> dict[str, int]:
        """Row counts per cache table (diagnostics only)."""
        counts: dict[str, int] = {}
        for table in ("listings", "sizes"):
            row = self.fetch_one(f"SELECT count(*) FROM {table}")
            counts[table] = int(row[0]) if row else 0
        return counts
