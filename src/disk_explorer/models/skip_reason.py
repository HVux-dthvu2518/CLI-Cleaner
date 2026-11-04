"""Why an entry was deliberately not traversed or counted."""

from enum import Enum


class SkipReason(str, Enum):
    """Explicit, user-facing reasons an entry was skipped by design.

    ``SkippedEntry`` records carry one of these plus the path, so the UI can
    explain exclusions instead of only reporting a bare count.
    """

    SYMLINK = "symlink"
    REPARSE_POINT = "reparse point"
    DUPLICATE_DIRECTORY = "already visited"
    NON_REGULAR_FILE = "not a regular file"


#: Human-readable explanations for the details panel.
SKIP_REASON_DETAIL = {
    SkipReason.SYMLINK: "symlink - not followed, counted in neither size nor file total",
    SkipReason.REPARSE_POINT: (
        "reparse point (junction/mount) - not followed; Windows often maps standard "
        "folders such as Documents, Downloads and Music this way, so such a folder and "
        "the files inside it do not appear in the size total"
    ),
    SkipReason.DUPLICATE_DIRECTORY: "already visited in this scan - not counted twice",
    SkipReason.NON_REGULAR_FILE: "not a regular file (device, pipe or socket) - no logical size",
}
