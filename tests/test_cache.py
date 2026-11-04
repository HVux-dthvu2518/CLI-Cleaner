"""SQLite hybrid cache: persistence, validation, invalidation and recovery."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from disk_explorer.cache.cache_service import CacheService
from disk_explorer.cache.database import CacheUnavailable, Database
from disk_explorer.cache.repository import CacheRepository, path_key
from disk_explorer.cache.schema import SCHEMA_VERSION, describe
from disk_explorer.cache.validator import CachePolicy, ValidationStatus, effective_ttl
from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult, SkippedEntry
from disk_explorer.models.skip_reason import SkipReason


def _listing(root: Path) -> DirectoryListing:
    (root / "alpha.txt").write_text("12345", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    return DirectoryListing(
        root=root,
        entries=[
            FileEntry(
                path=root / "alpha.txt",
                name="alpha.txt",
                is_directory=False,
                size=5,
                modified_ns=111,
                extension=".txt",
                scan_status=ScanStatus.COMPLETE,
            ),
            FileEntry(
                path=root / "sub",
                name="sub",
                is_directory=True,
                size=None,
                modified_ns=222,
                extension="",
                scan_status=ScanStatus.PENDING,
            ),
        ],
        scanned_entries=2,
        skipped_entries=1,
        skipped_details=[SkippedEntry(path=root / "link", reason=SkipReason.SYMLINK)],
    )


def _size_result(root: Path) -> ScanResult:
    return ScanResult(
        root=root,
        total_size=5,
        total_files=1,
        total_directories=2,
        scanned_entries=3,
        skipped_entries=1,
        skipped_details=[SkippedEntry(path=root / "link", reason=SkipReason.SYMLINK)],
    )


@pytest.fixture()
def repository(tmp_path: Path) -> CacheRepository:
    database = Database(tmp_path / "cache" / "scan.sqlite3")
    assert database.open()
    repo = CacheRepository(database, ttl_seconds=1800)
    yield repo
    database.close()


@pytest.fixture()
def service(tmp_path: Path) -> CacheService:
    cache_service = CacheService(Database(tmp_path / "cache" / "scan.sqlite3"), ttl_minutes=30)
    assert cache_service.open()
    yield cache_service
    cache_service.close()


# -- schema and persistence ------------------------------------------------
def test_schema_is_created_and_versioned(repository: CacheRepository) -> None:
    database = repository.database
    (version,) = database.fetch_one("PRAGMA user_version")
    tables = {
        row[0]
        for row in database.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert version == SCHEMA_VERSION
    assert {"listings", "sizes"} <= tables
    assert "listings" in describe() and "sizes" in describe()


def test_listing_survives_an_application_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "cache" / "scan.sqlite3"
    first = Database(database_path)
    assert first.open()
    CacheRepository(first).store_listing(_listing(tmp_path))
    first.close()  # Simulate closing the application.

    second = Database(database_path)
    assert second.open()
    listing, validation = CacheRepository(second).load_listing(tmp_path, CachePolicy())
    second.close()

    assert validation.status is ValidationStatus.OK
    assert listing is not None
    assert [entry.name for entry in listing.entries] == ["alpha.txt", "sub"]
    assert listing.entries[1].scan_status is ScanStatus.CACHED
    assert listing.skipped_details == [SkippedEntry(path=tmp_path / "link", reason=SkipReason.SYMLINK)]


def test_size_result_round_trips(repository: CacheRepository, tmp_path: Path) -> None:
    assert repository.store_size(_size_result(tmp_path))

    result, validation = repository.load_size(tmp_path, CachePolicy())

    assert validation.is_valid
    assert result is not None
    assert (result.total_size, result.total_files, result.total_directories) == (5, 1, 2)
    assert result.is_complete and not result.cancelled and result.errors == []
    assert result.skipped_summary == "1 symlink(s)"


def test_cache_hit_and_miss_are_reported(service: CacheService, tmp_path: Path) -> None:
    assert service.cached_listing(tmp_path) is None
    assert service.stats.listing_misses == 1

    assert service.store_listing(_listing(tmp_path))
    hit = service.cached_listing(tmp_path)

    assert hit is not None and hit.from_cache
    assert service.stats.listing_hits == 1
    assert "1 hit" in service.stats.summary()


# -- validation ------------------------------------------------------------
def test_ttl_expiry_forces_a_miss(service: CacheService, tmp_path: Path) -> None:
    service.store_listing(_listing(tmp_path))
    policy = CachePolicy.from_minutes(1)
    loaded, validation = service.repository.load_listing(
        tmp_path, policy, now=_future(service.repository, tmp_path, 120)
    )

    assert loaded is None
    assert validation.status is ValidationStatus.EXPIRED
    assert "TTL" in validation.detail


def _future(repository: CacheRepository, root: Path, seconds: float) -> float:
    """Timestamp ``seconds`` after the row was written."""
    created_at = repository.database.fetch_one(
        "SELECT created_at FROM listings WHERE path_key = ?", (path_key(root),)
    )[0]
    return created_at + seconds


def test_raising_the_ttl_does_not_revive_old_rows(service: CacheService, tmp_path: Path) -> None:
    """The TTL recorded with the row keeps it honest after a settings change."""
    service.store_listing(_listing(tmp_path))
    original = service.repository.database.fetch_one(
        "SELECT created_at FROM listings WHERE path_key = ?", (path_key(tmp_path),)
    )[0]
    later = original + 3600

    generous, validation = service.repository.load_listing(
        tmp_path, CachePolicy.from_minutes(600), now=later
    )

    assert generous is None
    assert validation.status is ValidationStatus.EXPIRED


def test_metadata_change_invalidates_a_listing(service: CacheService, tmp_path: Path) -> None:
    service.store_listing(_listing(tmp_path))
    assert service.cached_listing(tmp_path) is not None

    (tmp_path / "added-later.bin").write_bytes(b"x")
    _bump_mtime(tmp_path)

    assert service.cached_listing(tmp_path) is None
    assert "changed" in service.stats.last_miss_reason


def _bump_mtime(path: Path) -> None:
    """Force a distinct mtime even on coarse filesystem timestamps."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_unknown_modification_time_is_never_treated_as_unchanged(
    repository: CacheRepository, tmp_path: Path
) -> None:
    repository.store_listing(_listing(tmp_path))
    missing = tmp_path / "gone"  # Never existed: mtime is unknowable.

    listing, validation = repository.load_listing(missing, CachePolicy())

    assert listing is None
    assert validation.status is ValidationStatus.UNCACHEABLE


def test_effective_ttl_is_the_stricter_value() -> None:
    assert effective_ttl(60, 30) == 30
    assert effective_ttl(30, 60) == 30
    assert effective_ttl(0, 30) == 30
    assert effective_ttl(30, 0) == 30
    assert effective_ttl(0, 0) == 0


def test_policy_can_disable_ttl(repository: CacheRepository, tmp_path: Path) -> None:
    repository.store_listing(_listing(tmp_path))
    repository.database.execute("UPDATE listings SET ttl_seconds = 0")

    listing, validation = repository.load_listing(
        tmp_path, CachePolicy(ttl_seconds=0), now=time_now() + 10_000
    )

    assert validation.status is ValidationStatus.OK
    assert listing is not None


def time_now() -> float:
    import time

    return time.time()


# -- never cache incomplete work ------------------------------------------
@pytest.mark.parametrize(
    "cancelled, is_complete, errors",
    [(True, True, []), (False, False, []), (False, True, ["C:/x: Access is denied"])],
)
def test_partial_or_cancelled_listings_are_not_stored(
    repository: CacheRepository, tmp_path: Path, cancelled: bool, is_complete: bool, errors: list[str]
) -> None:
    listing = _listing(tmp_path)
    listing.cancelled = cancelled
    listing.is_complete = is_complete
    listing.errors = errors

    assert repository.store_listing(listing) is False
    assert repository.counts()["listings"] == 0


@pytest.mark.parametrize(
    "cancelled, is_complete, errors",
    [(True, True, []), (True, False, ["C:/x: partial then cancelled"]), (False, False, [])],
)
def test_cancelled_or_unexplained_sizes_are_not_stored(
    repository: CacheRepository, tmp_path: Path, cancelled: bool, is_complete: bool, errors: list[str]
) -> None:
    """A cancelled walk, or a "partial" result with no reason, is not a usable row.

    ``is_complete=False`` with no errors would store a number nobody can account
    for, so it is rejected; a partial result *with* its error list is stored and
    returned labelled partial (see the test below).
    """
    result = _size_result(tmp_path)
    result.cancelled = cancelled
    result.is_complete = is_complete
    result.errors = errors

    assert repository.store_size(result) is False
    assert repository.counts()["sizes"] == 0


def test_partial_size_is_cached_with_its_errors_and_flags(
    repository: CacheRepository, tmp_path: Path
) -> None:
    """A partial total is cached, but never comes back claiming to be complete."""
    partial = ScanResult(
        root=tmp_path,
        total_size=42,
        total_files=2,
        total_directories=3,
        scanned_entries=5,
        errors=[f"{tmp_path / 'blocked'}: Access is denied"],
        is_complete=False,
    )

    assert repository.store_size(partial) is True
    loaded, validation = repository.load_size(tmp_path, CachePolicy())

    assert validation.is_valid
    assert loaded is not None
    assert loaded.total_size == 42  # the useful number is preserved
    assert loaded.is_complete is False  # ...but never presented as complete
    assert loaded.errors == [f"{tmp_path / 'blocked'}: Access is denied"]


def test_permission_errors_never_become_an_empty_directory(
    service: CacheService, tmp_path: Path
) -> None:
    """An inaccessible directory must never be cached as a complete empty scan."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    denied = ScanResult(
        root=blocked,
        total_size=0,
        total_files=0,
        is_complete=False,
        errors=[f"{blocked}: Permission denied"],
    )

    assert service.store_size(denied) is True  # cached, because it says why
    cached = service.cached_size(blocked)

    assert cached is not None
    assert cached.is_complete is False  # never a successful zero-byte result
    assert "Permission denied" in cached.errors[0]
    assert service.stats.partial_hits == 1
    assert "partial" in service.stats.summary()


# -- invalidation and manual refresh --------------------------------------
def test_manual_refresh_invalidates_the_listing_and_its_sizes(
    service: CacheService, tmp_path: Path
) -> None:
    child = tmp_path / "sub"
    service.store_listing(_listing(tmp_path))
    service.store_size(_size_result(tmp_path))
    service.store_size(_size_result(child))
    assert service.cached_listing(tmp_path) is not None

    removed = service.mark_stale(tmp_path)

    assert removed == 2  # listing + the size row for the same path
    assert service.cached_listing(tmp_path) is None
    assert service.cached_size(tmp_path) is None
    assert service.cached_size(child) is not None  # unrelated path untouched


def test_tree_invalidation_removes_descendants(service: CacheService, tmp_path: Path) -> None:
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    service.store_listing(_listing(tmp_path))
    service.store_size(_size_result(tmp_path))
    service.store_size(_size_result(nested))
    assert service.repository.counts()["sizes"] == 2

    removed = service.mark_stale(tmp_path, tree=True)

    assert removed >= 3
    assert service.repository.counts()["sizes"] == 0


def test_invalidated_path_is_not_served_before_the_rescan_lands(
    service: CacheService, tmp_path: Path
) -> None:
    service.store_listing(_listing(tmp_path))
    service.mark_stale(tmp_path)

    assert service.cached_listing(tmp_path) is None
    assert service.store_listing(_listing(tmp_path))  # fresh scan lands
    assert service.cached_listing(tmp_path) is not None


def test_clear_empties_both_tables(service: CacheService, tmp_path: Path) -> None:
    service.store_listing(_listing(tmp_path))
    service.store_size(_size_result(tmp_path))

    removed = service.clear()

    assert removed == 2
    assert service.repository.counts() == {"listings": 0, "sizes": 0}


def test_prune_drops_rows_past_their_recorded_ttl(service: CacheService, tmp_path: Path) -> None:
    service.store_listing(_listing(tmp_path))
    service.repository.database.execute("UPDATE listings SET created_at = created_at - 7200")

    removed = service.prune()

    assert removed == 1
    assert service.repository.counts()["listings"] == 0


# -- concurrency -----------------------------------------------------------
def test_concurrent_writes_and_reads_stay_consistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "cache" / "scan.sqlite3")
    assert database.open()
    repository = CacheRepository(database, ttl_seconds=1800)
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        folder = tmp_path / f"dir{index}"
        folder.mkdir()
        try:
            assert repository.store_listing(_listing(folder))
            assert repository.store_size(_size_result(folder))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    def reader(index: int) -> None:
        try:
            repository.load_listing(tmp_path / f"dir{index % 8}", CachePolicy())
            repository.counts()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    threads += [threading.Thread(target=reader, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    counts = repository.counts()  # read while the connection is still open
    database.close()
    assert counts == {"listings": 8, "sizes": 8}


def test_second_connection_can_write_while_the_first_is_open(tmp_path: Path) -> None:
    database_path = tmp_path / "cache" / "scan.sqlite3"
    first = Database(database_path)
    second = Database(database_path)
    assert first.open() and second.open()
    try:
        assert CacheRepository(first).store_listing(_listing(tmp_path))
        assert CacheRepository(second).store_listing(_listing(tmp_path))  # upsert, no lock error
        assert CacheRepository(first).counts()["listings"] == 1
    finally:
        second.close()
        first.close()


# -- corruption and unavailability ----------------------------------------
def test_corrupted_database_is_recovered(tmp_path: Path) -> None:
    database_path = tmp_path / "cache" / "scan.sqlite3"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"this is definitely not a sqlite database")

    database = Database(database_path)
    assert database.open() is True

    assert database.recovery_count == 1
    assert list(database_path.parent.glob("scan.sqlite3.corrupt-*"))
    repository = CacheRepository(database)
    assert repository.store_listing(_listing(tmp_path))
    assert repository.counts()["listings"] == 1
    database.close()


def test_corruption_during_a_read_degrades_to_a_miss(tmp_path: Path) -> None:
    """A damaged cache must cost a rescan, never an exception or wrong data."""
    service = CacheService(Database(tmp_path / "cache" / "scan.sqlite3"), ttl_minutes=30)
    assert service.open()
    service.store_listing(_listing(tmp_path))

    # Simulate mid-flight damage: the next query fails with DatabaseError.
    def explode(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    service.database.fetch_one = explode  # type: ignore[method-assign]
    try:
        assert service.cached_listing(tmp_path) is None
        assert "malformed" in service.stats.last_miss_reason or service.stats.listing_misses == 1
    finally:
        service.close()


def test_unwritable_cache_directory_disables_the_cache(tmp_path: Path) -> None:
    blocked = tmp_path / "ro"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    try:
        database = Database(blocked / "cache" / "scan.sqlite3")
        opened = database.open()
        repository = CacheRepository(database)
        stored = repository.store_listing(_listing(tmp_path))
        loaded = repository.load_listing(tmp_path, CachePolicy())[0]
    finally:
        os.chmod(blocked, 0o700)

    assert opened is False
    assert database.available is False
    assert stored is False and loaded is None


def test_newer_schema_version_is_refused_not_destroyed(tmp_path: Path) -> None:
    database_path = tmp_path / "cache" / "scan.sqlite3"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
    connection.commit()
    connection.close()

    database = Database(database_path)
    assert database.open() is False
    assert "newer" in (database.unavailable_reason or "")
    assert database.recovery_count == 0
    # The file must not be moved aside: a newer app version still owns it.
    assert not list(database_path.parent.glob("scan.sqlite3.corrupt-*"))
    assert database_path.exists()


def test_repository_operations_are_safe_when_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "cache" / "scan.sqlite3")
    repository = CacheRepository(database)

    assert repository.store_listing(_listing(tmp_path)) is False
    assert repository.load_listing(tmp_path, CachePolicy())[0] is None
    assert repository.invalidate(tmp_path) == 0
    assert repository.clear() == 0
    with pytest.raises(CacheUnavailable):
        database.fetch_one("SELECT 1")


# -- schema migration ------------------------------------------------------
def test_v1_database_upgrades_and_keeps_its_rows(tmp_path: Path) -> None:
    """An existing cache from an older build must upgrade, not be thrown away."""
    from disk_explorer.cache.schema import MIGRATIONS

    database_path = tmp_path / "cache" / "scan.sqlite3"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    connection.executescript(MIGRATIONS[1])  # build exactly the v1 shape
    connection.execute("PRAGMA user_version=1")
    connection.execute(
        "INSERT INTO sizes (path_key, path, created_at, ttl_seconds, mtime_ns, total_size,"
        " total_files, total_directories, scanned_entries, skipped_count, skipped)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            path_key(tmp_path),
            str(tmp_path),
            time_now(),
            1800,
            tmp_path.stat().st_mtime_ns,  # must match, or the row is legitimately stale
            999,
            5,
            3,
            9,
            0,
            "[]",
        ),
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    assert database.open() is True
    try:
        (version,) = database.fetch_one("PRAGMA user_version")
        assert version == SCHEMA_VERSION
        row = database.fetch_one(
            "SELECT total_size, is_complete, errors FROM sizes WHERE path_key = ?",
            (path_key(tmp_path),),
        )
        # v1 rows default to complete with no errors: that is what they meant.
        assert row == (999, 1, "[]")
        result, validation = CacheRepository(database).load_size(tmp_path, CachePolicy())
        assert validation.is_valid and result is not None
        assert result.total_size == 999 and result.is_complete is True
    finally:
        database.close()
