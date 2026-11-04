# Project implementation guidance

The requested Ponytail, Graphify, and UI UX Pro Max skills are applied as development guidance, adapted to this Python/Textual project rather than added as runtime dependencies: keep changes lean, make module relationships easy to trace, and prioritize responsive, accessible keyboard-first terminal UX. A generated graph is deferred while the package remains small and explicit.

- Keep the implementation no larger than the current milestone requires; reuse the standard library before adding dependencies.
- Read the existing code path before editing, and add focused tests for branching, traversal, cancellation, and data-loss-sensitive behavior.
- Keep filesystem logic independent of Textual. UI handlers must delegate blocking work to `ScanService`.
- Represent unknown sizes as `None`; never turn inaccessible or incomplete scans into a successful zero-byte result.
- Do not follow symlinks or Windows reparse points by default. Keep worker concurrency bounded and cancellation cooperative.
- Never drop an entry silently: a skipped entry carries a `SkipReason` in `skipped_details`, and an unreadable path stays an error. Skips keep a scan complete; errors make it partial. Do not fold one into the other.
- Keep Python and Rust engines behaviourally identical. `tests/test_engine_contract.py` defines the shared tuple contract and compares the engines when the extension is importable; `tests/test_rust_source.py` guards the build (no unstable `windows_by_handle` APIs, PyO3 signatures match `FileSystemScanner`).
- The Rust crate must build on stable Rust (MSRV in `Cargo.toml`, currently 1.80) with only stable APIs.
- The SQLite cache is a disposable accelerator, never a source of truth: keep it out of `core/` and `ui/`, never let its failures reach the user as errors, and remember that deleting the file must only cost a rescan.
- Cache rules that must not regress: listings and recursive sizes live in separate tables with separate validation; only complete, error-free scans are stored; permission errors are never cached as empty directories; a miss always means "scan it"; expired rows are invalidated, not refreshed silently.
- Keep cache policy in `cache/validator.py` (pure) and `cache/cache_service.py` (orchestration); `services/scan_service.py` only decides the order of operations. Add a test for every new invalidation path, since a wrong hit is silent data corruption from the user's point of view.
- Benchmark before claiming a performance win (`scripts/benchmark_cache.py`, `scripts/benchmark_scanner.py`). State the machine, filesystem and cache state, and say plainly when caching does not help.
- Design for keyboard-first use and small terminal sizes; keep labels readable and state visible.
- For this small repository, the module tree and typed service contracts are the source of truth; avoid generated graph artifacts until the codebase is large enough to benefit.
