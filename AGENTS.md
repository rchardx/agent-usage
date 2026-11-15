# AGENTS.md — agent-usage

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, packaging and commit/PR conventions.
This file defines execution constraints, project boundaries and completion requirements.

## Working rules

- Read relevant files first and preserve unrelated working-tree changes.
- Prioritize correctness and observable contracts. Use the smallest complete design
  for current needs; avoid speculative abstractions and unrelated cleanup.
- Use English for code, documentation, commits and PR content.
- Ask before adding major runtime dependencies or changing runtime support.
  Necessary implementation and validation need no repeated approval.
- Never weaken checks, suppress type errors or add blanket secret allowlists.
- Do not inspect real credentials or transcripts unless explicitly required by the
  task. Never commit secrets, personal data or machine-specific paths.
- Do not commit, rewrite history, push, merge, publish or change GitHub settings
  unless requested. Review working-tree and staged diffs before committing.
- Do not post PR comments. Report progress in the task conversation; keep PR
  descriptions focused on the actual changes, rationale and validation results.

## Project boundaries

- `parsers.py` and `index.py` own normalization, checkpoints and deduplication;
  `providers.py` isolates read-only account queries. Calculate per-record costs in
  `pricing.py` before aggregation in `reports.py`; keep presentation in `render.py`
  and command handling in `cli.py`.
- Missing data stays unknown, not zero. Cache/reasoning counts are components of
  parent counts. Costs are current API-equivalent estimates, not bills.
- Preserve CLI behavior and the [JSON contract](docs/json-interface.md). Parser
  changes affecting indexed results must invalidate checkpoints and test rebuilding.
  Cache recovery must preserve source logs, credentials and unrelated snapshots.
- `.python-version` selects the development runtime; `pyproject.toml` owns minimum
  support and tool targets. Use uv to update dependencies and the lockfile together;
  do not edit the lockfile manually or raise minimum versions without a reason.

## Validation

For behavior, dependency, CI or tooling changes, run:

```bash
uv run --locked pre-commit run --all-files
uv run --locked pytest -q
git diff --check
```

For documentation-only changes, run pre-commit on the changed files and
`git diff --check`. Follow CONTRIBUTING.md for build and installed-package checks
when packaging changes. Update usage documentation when behavior changes.

- Test public behavior with independent expectations, including failures and
  unknown values. Use synthetic records and fake credentials, never live accounts.
- Report only checks actually run on the final state and their limitations.
  Local Linux checks do not verify hosted macOS or Windows CI. Preserve all required
  checks, including `pr-message`, for dependency-bot PRs too.
