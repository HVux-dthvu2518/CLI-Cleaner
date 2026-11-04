# Project implementation guidance

The requested Ponytail, Graphify, and UI UX Pro Max skills are applied as development guidance, adapted to this Python/Textual project rather than added as runtime dependencies: keep changes lean, make module relationships easy to trace, and prioritize responsive, accessible keyboard-first terminal UX. A generated graph is deferred while the package remains small and explicit.

- Keep the implementation no larger than the current milestone requires; reuse the standard library before adding dependencies.
- Read the existing code path before editing, and add focused tests for branching, traversal, cancellation, and data-loss-sensitive behavior.
- Keep filesystem logic independent of Textual. UI handlers must delegate blocking work to `ScanService`.
- Represent unknown sizes as `None`; never turn inaccessible or incomplete scans into a successful zero-byte result.
- Do not follow symlinks or Windows reparse points. Keep worker concurrency bounded and cancellation cooperative.
- Design for keyboard-first use and small terminal sizes; keep labels readable and state visible.
- For this small repository, the module tree and typed service contracts are the source of truth; avoid generated graph artifacts until the codebase is large enough to benefit.
