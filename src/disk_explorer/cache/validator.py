"""Metadata validation and TTL rules for cached data.

Every cached row is checked twice before it is reused:

* **TTL** - an absolute upper bound on staleness. For recursive sizes this is the
  only check that can see a change deep inside a folder, because a directory's
  modification time only reflects its *direct* children.
* **Metadata** - the directory's modification time must match the value recorded
  when the row was written. This catches added, removed or renamed direct
  children immediately, without waiting for the TTL.

The effective TTL is the stricter of the current setting and the value recorded
at write time, so raising the TTL in settings never retroactively trusts old rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValidationStatus(str, Enum):
    """Outcome of validating a cache candidate."""

    OK = "ok"
    EXPIRED = "expired"
    STALE = "stale"
    UNCACHEABLE = "uncacheable"


@dataclass(slots=True, frozen=True)
class Validation:
    """Why a cached row was accepted or rejected."""

    status: ValidationStatus
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.OK


def effective_ttl(policy_ttl: float, row_ttl: float | None) -> float:
    """Combine the configured TTL with the one recorded when the row was written.

    ``0`` means "no expiry". The stricter positive value wins; a row written under
    no expiry is still bounded by the current policy.
    """
    current = max(0.0, float(policy_ttl))
    stored = max(0.0, float(row_ttl or 0.0))
    if current <= 0:
        return stored
    if stored <= 0:
        return current
    return min(current, stored)


@dataclass(slots=True, frozen=True)
class CachePolicy:
    """Validation policy shared by listings and recursive sizes."""

    #: Rows older than this are never trusted. ``<= 0`` disables TTL checking.
    ttl_seconds: float = 1800.0
    #: Require the directory mtime to match the one recorded at write time.
    require_matching_mtime: bool = True

    @classmethod
    def from_minutes(cls, minutes: int) -> "CachePolicy":
        return cls(ttl_seconds=max(0, int(minutes)) * 60.0)

    def validate(
        self,
        *,
        created_at: float,
        stored_mtime_ns: int | None,
        current_mtime_ns: int | None,
        kind: str,
        now: float,
        row_ttl_seconds: float | None = None,
    ) -> Validation:
        """Decide whether a row written at ``created_at`` is still usable."""
        ttl = effective_ttl(self.ttl_seconds, row_ttl_seconds)
        age = now - created_at
        if age < 0:
            # Clock moved backwards (or a row from the future): do not trust it.
            return Validation(ValidationStatus.UNCACHEABLE, f"{kind} timestamp is in the future")
        if ttl > 0 and age > ttl:
            return Validation(
                ValidationStatus.EXPIRED,
                f"{kind} cached {age:.0f}s ago, TTL is {ttl:.0f}s",
            )
        if not self.require_matching_mtime:
            return Validation(ValidationStatus.OK, f"{kind} within TTL")
        if stored_mtime_ns is None or current_mtime_ns is None:
            return Validation(
                ValidationStatus.UNCACHEABLE,
                f"{kind} has no usable modification time",
            )
        if stored_mtime_ns != current_mtime_ns:
            return Validation(
                ValidationStatus.STALE,
                f"{kind} directory changed since caching",
            )
        return Validation(ValidationStatus.OK, f"{kind} unchanged and within TTL")
