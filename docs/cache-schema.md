# Hybrid cache schema

The SQLite cache is a **disposable accelerator**. Nothing in it is user data, and
deleting the file at any time must only cost a rescan. That rule drives every
decision below.

- File: `%LOCALAPPDATA%\DiskExplorerCleaner\cache\scan-cache.sqlite3` on Windows,
  `$XDG_DATA_HOME/disk-explorer-cleaner/cache/scan-cache.sqlite3` elsewhere.
  Override with `cache_path` in `settings.json`.
- Version: `PRAGMA user_version = 2`, applied by `disk_explorer.cache.schema`
  (v1 -> v2 adds `is_complete` and `errors` to `sizes`).
- Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`.
  Even in WAL mode, readers and a writer can briefly conflict, so callers retry
  instead of treating a lock as data loss.
- Access: one connection guarded by an `RLock`; every mutation happens inside an
  explicit `BEGIN IMMEDIATE` transaction.

## Why two tables

Directory listings and recursive folder sizes have different invalidations, so
they are stored separately and can be behind by different amounts.

| | `listings` | `sizes` |
| --- | --- | --- |
| Content | immediate children of one directory | recursive logical-size total |
| Validated by | directory mtime **and** TTL | TTL, root mtime, and recorded child-listing mtimes |
| Typical staleness | detected immediately by mtime | detected by child mtimes, then TTL |
| Cost of a miss | one `scandir` | a full recursive walk |

Mixing them would either force a full walk before the UI could render, or let a
stale recursive total hide behind a valid listing.

## `listings`

| Column | Type | Meaning |
| --- | --- | --- |
| `path_key` | TEXT PK | `os.path.normcase(os.path.abspath(path))`; case- and separator-insensitive on Windows |
| `path` | TEXT | original path, for diagnostics and `LIKE` prefix queries |
| `created_at` | REAL | Unix timestamp at write time |
| `ttl_seconds` | REAL | TTL in force when the row was written (`0` = no expiry) |
| `mtime_ns` | INTEGER | directory mtime at write time; `NULL` means uncacheable |
| `entry_count` | INTEGER | number of stored children |
| `skipped_count` | INTEGER | skipped entries recorded with the listing |
| `payload` | TEXT | JSON array of `[name, is_directory, size, modified_ns, extension, is_symlink, is_reparse_point]` |
| `skipped` | TEXT | JSON array of `[path, reason]`, `reason` being a `SkipReason` value |

Index: `idx_listings_created_at`. Kept sub-millisecond for 2,000-entry folders
(~1.9 µs per entry to decode), so a hit beats a `scandir` on network or
antivirus-scanned storage without adding meaningful latency locally.

## `sizes`

| Column | Type | Meaning |
| --- | --- | --- |
| `path_key` | TEXT PK | same normalization as above |
| `path` | TEXT | original path |
| `created_at` | REAL | Unix timestamp at write time |
| `ttl_seconds` | REAL | TTL in force at write time |
| `mtime_ns` | INTEGER | root directory mtime at write time |
| `total_size` | INTEGER | logical bytes (`st_size` sum) |
| `total_files` | INTEGER | regular files counted |
| `total_directories` | INTEGER | directories visited |
| `scanned_entries` | INTEGER | entries examined |
| `skipped_count` | INTEGER | skipped entries (links, reparse points, non-regular files) |
| `skipped` | TEXT | JSON array of `[path, reason]` |
| `is_complete` | INTEGER | `1` complete, `0` partial (added in v2) |
| `errors` | TEXT | JSON array of error strings, empty for a complete scan (added in v2) |

Index: `idx_sizes_created_at`. The `created_at` value doubles as a scan epoch: the
cross-check in `CacheService._first_changed_child` compares only listings written
at or after it, so a listing from an older scan is never used as evidence.

## What is never stored, and what may be partial

The invariant is **"a row always carries its own truth"**, not "only perfect scans
are cached". `is_complete` and `errors` are stored with the row and come back on
the `ScanResult`, so a reader cannot mistake a partial total for a complete one.

| Case | `listings` | `sizes` |
| --- | --- | --- |
| Complete, no errors | stored | stored (`is_complete=1`) |
| Partial **with** its error list | rejected | stored (`is_complete=0`, `errors` preserved) |
| Partial with no errors | rejected | rejected — the number would be unaccountable |
| Cancelled | rejected | rejected — the walk was abandoned mid-flight |
| Root mtime unreadable (`None`) | rejected | rejected — `None` is unknown, not "unchanged" |

**Why the asymmetry.** A partial listing would silently hide children from the
Explorer, and a miss costs one `scandir`. A partial *recursive total* is the
expensive workload — a 2.2 s walk over a 411k-entry tree where two protected
folders make the result partial — and re-walking it on every visit helps nobody.
Labelling it partial keeps the rule that matters: never claim completeness you
do not have. The v2 schema therefore allows it, and `ScanService` sets
`ScanStatus.CACHED_PARTIAL` so the UI shows `Cached·part` and the details panel
says the total excludes unreadable paths.

A display rule keeps the number honest: `scan_service.display_size()` shows a
partial total only when something was actually measured. A partial scan with no
measured bytes stays `None` ("unknown"), never `0 B`.

## Validation

`disk_explorer.cache.validator` is pure (no I/O) and decides per row:

1. **TTL** — `age > effective_ttl(policy, row_ttl)` → `EXPIRED`. The stricter of
   the current setting and the value recorded at write time wins, so raising the
   TTL in settings never retroactively trusts an old row.
2. **Future timestamp** — `created_at > now` → `UNCACHEABLE` (clock change).
3. **Mtime** — `stored != current` → `STALE`.
4. **Unknown mtime** — either side `None` → `UNCACHEABLE`.

Recursive sizes get one extra check in `CacheService.cached_size` when
`cross_check_children=True`: the mtimes of cached child listings written by the
same scan must still match. This catches a change one level down without waiting
for the TTL; deeper changes are the TTL's job, which is why the TTL is short by
default (30 minutes, `cache_ttl_minutes`).

## Invalidation

| Trigger | Call | Effect |
| --- | --- | --- |
| Manual refresh (`r` / Refresh button) | `CacheService.mark_stale(path)` | deletes `listings` + `sizes` for that path; the path stays marked until the fresh row is stored |
| Refresh including descendants | `mark_stale(path, tree=True)` | also deletes rows under `path_key LIKE path + separator + %` |
| Startup housekeeping | `CacheService.prune()` | deletes rows past their own `ttl_seconds` |
| Settings → clear | `CacheService.clear()` | empties both tables |
| Corrupted file | automatic | renamed to `scan-cache.sqlite3.corrupt-<utc-stamp>` and rebuilt empty |
| Newer schema version | automatic | cache disabled, **file left untouched** for the newer build |

## Failure handling

| Failure | Behaviour |
| --- | --- |
| Directory or file unreadable | cache disabled, every call is a miss (`CacheUnavailable`) |
| Unwritable cache directory | `Database.open()` returns `False`; the app scans normally |
| `database disk image is malformed` | one automatic recovery, then a retry |
| `database is locked` / I/O error | raised as `CacheUnavailable` so the caller rescans; no file is discarded |
| Schema newer than this build | cache disabled; the file is preserved |
| Decode error in `payload`/`skipped` | treated as a miss; unknown `SkipReason` values are dropped, not fatal |

## Migrations

`PRAGMA user_version` is the source of truth. `schema.MIGRATIONS` maps a target
version to the SQL that produces it, and `schema.migrations_after(version)`
returns the ordered steps to apply at `Database.open()`. To add a column:

1. never edit an already-released entry (`MIGRATIONS[1]`, `MIGRATIONS[2]`): an
   existing file must be able to replay them in order;
2. add the next number (for example `MIGRATIONS[3]`) using
   `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` where possible;
3. bump `SCHEMA_VERSION` to match the highest key;
4. add a test that builds the previous version, upgrades it, and asserts both the
   `user_version` and the preservation of existing rows (see
   `test_cache.py::test_v1_database_upgrades_and_keeps_its_rows`).

Rows are disposable, so a migration is allowed to drop the cache contents when a
destructive change is simpler than an `ALTER` — but it must never require the
user to delete the file by hand.

## Inspecting a cache by hand

```powershell
sqlite3 "$env:LOCALAPPDATA\DiskExplorerCleaner\cache\scan-cache.sqlite3" "PRAGMA user_version; SELECT count(*) FROM listings; SELECT count(*) FROM sizes;"
```
