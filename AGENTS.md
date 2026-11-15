# Agent development instructions

[CONTRIBUTING.md](CONTRIBUTING.md) defines development commands, the commit/PR
message contract, compatibility rules and privacy requirements. These apply
equally to human and agent contributions.

## Scope and architecture

- `parsers.py` normalizes synthetic/local provider records without persisting
  conversation text.
- `index.py` owns incremental SQLite checkpoints and cross-file deduplication.
- `pricing.py` calculates per-record estimates before report grouping.
- `providers.py` isolates read-only account adapters.
- `reports.py` owns aggregation; `render.py` owns terminal presentation.
- `cli.py` owns commands and options. Keep JSON and terminal semantics consistent.

Missing data remains unknown, never an invented zero. Cache and reasoning counts
are components, not additional parent tokens. Prices are current API-equivalent
estimates, not invoices. Add regression tests for these invariants.

## Working rules

- Inspect the working tree before editing. Preserve unrelated user changes.
- Use English for repository content and messages.
- Keep changes focused; avoid unrelated refactors and dependency upgrades.
- Update authoritative configuration and its lockfile together.
- Use synthetic test records and fake credentials. Never inspect or copy a user's
  actual credentials or transcripts unless the task explicitly requires it.
- Do not weaken checks, add blanket secret allowlists or hide type errors to make
  CI pass.
- Do not commit, amend, rebase, push or change GitHub settings unless requested.
- History rewriting and publication require an explicit agreed plan and a private
  recoverable backup. A force-push is not proof that sensitive history is erased.

## Verification

Run focused tests while implementing, then the completion commands in
CONTRIBUTING.md. Format with `uv run --locked ruff format .`; lint and formatting
checks should not silently modify files in CI.

Report the commands actually run, their results, and checks not run. Hosted
cross-platform CI is not verified merely because local Linux checks pass.
