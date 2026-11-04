"""SQLite hybrid cache: persistent, metadata-validated, TTL-bounded scan results.

Layering (nothing above ``cache_service`` talks to SQLite):

``database``      connection, transactions, pragmas, corruption recovery
``schema``        tables, indexes and the ``PRAGMA user_version`` migration path
``repository``    read/write/invalidate/clear/prune, model <-> row translation
``validator``     metadata comparison and TTL policy (pure, no I/O)
``cache_service`` orchestration: freshness rules, refresh, invalidation, stats
"""

from disk_explorer.cache.cache_service import CacheService, CacheStats
from disk_explorer.cache.database import CacheUnavailable, Database
from disk_explorer.cache.repository import CacheRepository, SizeRecord, path_key
from disk_explorer.cache.schema import SCHEMA_VERSION, describe
from disk_explorer.cache.validator import CachePolicy, Validation, ValidationStatus

__all__ = [
    "CachePolicy",
    "CacheRepository",
    "CacheService",
    "CacheStats",
    "CacheUnavailable",
    "Database",
    "SCHEMA_VERSION",
    "SizeRecord",
    "Validation",
    "ValidationStatus",
    "describe",
    "path_key",
]
