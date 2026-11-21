# Contributing

Use English for code, documentation, issues, PRs and commit messages. Keep changes
focused and include tests and documentation for user-visible behavior. For
substantial changes, start with an issue defining the problem and acceptance
criteria. See [SECURITY.md](SECURITY.md) before sharing diagnostics.

This guide owns setup and contribution conventions. [AGENTS.md](AGENTS.md) defines
execution constraints, project boundaries and validation requirements.

## Setup and checks

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). The repository's
`.python-version` selects Python 3.14 for local development without pinning a patch
release. This does not raise the package's `requires-python >=3.11` requirement;
Ruff and mypy retain their Python 3.11 compatibility targets. CI reads the
minimum from `pyproject.toml` and the development version from `.python-version`,
and tests both.

```bash
uv sync --locked --dev
uv run --locked pre-commit run --all-files
uv run --locked pytest -q
uv build
uv run --locked twine check --strict dist/*
git diff --check
```

Run the checkout with `uv run agent-usage`. To make the editable command
available from any directory, install it once:

```bash
uv tool install --editable /absolute/path/to/agent-usage
agent-usage
```

Ensure the tool executable directory (`uv tool dir --bin`) is on `PATH`;
`uv tool update-shell` can configure it for a new shell session.

Pre-commit owns repository hygiene, secrets checks, Markdown lint, Ruff lint and
format checks, and mypy. It manages its own pinned hook environments, including
the Node environment used by Markdown lint; the application has no Node runtime
dependency. Mypy checks source and repository scripts, including untyped function
bodies. It does not yet enforce complete annotations for the dynamic provider
payloads.

For a narrow change, run the relevant checks while iterating:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q tests/test_pricing.py
```

Run the complete checks for code and tooling changes before requesting merge;
see [validation](AGENTS.md#validation) for documentation-only changes. Format
locally with `uv run --locked ruff format .`; CI checks must not silently modify files.
Tests use synthetic records and mock endpoints; pytest disables network sockets.
Never run account queries or read real agent homes in tests. CI also tests the
installed wheel outside the checkout with empty data directories and validates
its JSON against the bundled schema.
Schema validation dependencies are development-only. JSON changes must update
`src/agent_usage/schemas/report-v1.schema.json`, contract tests and synthetic
examples as appropriate; follow [JSON compatibility rules](docs/json-interface.md).
Cache recovery tests must use isolated cache directories and preserve source
logs and unrelated snapshots, including on simulated failure.

### Nightly prerelease

The Nightly workflow checks `main` at 00:00 UTC. It skips when both the
published `nightly` release and its annotated Git tag already match `main`. To
rebuild or repair a partial upload, run
`gh workflow run nightly.yml --ref main -f force=true`. Before modifying the
release, the publish job rechecks `main`. A rerun keeps its original commit and
run number; dispatch a new run to build the latest `main`. For the same commit,
a higher **published** run number supersedes any older run, regardless of start
order. Publication uploads and verifies the new distributions on a temporary
staging draft (cleaned up on failure via an `EXIT` trap and a stale-draft sweep),
creates a publication-stamped annotated `nightly` tag object, deletes the
previous `nightly` release, advances `refs/tags/nightly`, and publishes the
draft; deleting the old release before publishing the replacement leaves a brief
non-atomic swap window where `nightly` is absent until publication completes.
Recreating the release refreshes `published_at`, while the annotated tag
updates the tag and `Source code` entry dates shown on the GitHub release page
(file timestamps inside GitHub's generated source archives still reflect the
commit time). Retrying a publish job preserves an already matching release and
annotated tag, repairs a missing or outdated tag, or re-stages from the verified
build artifacts (including if interrupted during the swap window); the next
scheduled run also repairs an interrupted publication.
The rolling `nightly` release is for testing; it does not
publish to PyPI or change the base version in `pyproject.toml`.

### Optional local hooks

Check for existing hooks before installing. Do not overwrite another project's
hook manager or change global Git configuration.

```bash
git config --get core.hooksPath
uv run --locked pre-commit install
```

The configuration installs both pre-commit and commit-msg hooks. Hooks are local
convenience checks; CI is authoritative. Ruff and mypy inspect the full checkout.
Pre-commit temporarily stashes unstaged tracked changes during a commit, but
untracked files and tool-specific exclusions still require care: review partially
staged changes rather than treating hooks as proof of the exact committed state.

## Commit and PR messages

A commit should be one coherent, independently understandable and verifiable
change. Keep implementation, tests, documentation and required lockfile updates
together. Do not retain fixup or WIP commits in the final main history.

Use this contract for both commits and the PR title/body:

```text
feat(pricing): explain unpriced records

Explain the problem, resulting behavior and rationale in one paragraph.

Key changes:
- Describe a concrete change.
- Include related tests and documentation.

Testing: uv run --locked pytest -q
```

- Types: `chore`, `docs`, `feat`, `fix`, `test`, `refactor`.
- Keep the subject under 72 characters; scope is optional and lowercase.
- Separate the subject, summary paragraph and `Key changes:` with one blank line.
- Put nonempty bullets directly after `Key changes:`, without blank lines between them.
- Replace template placeholders; describe the actual diff, not future work.
- Optional footers, including validation results, follow a blank line.
- Breaking changes use `!` or `BREAKING CHANGE:` and require a nonempty
  `Migration:` footer. Explain CLI/JSON upgrades or cache rebuilds as applicable.

Validate a message file with:

```bash
uv run --locked python scripts/check_commit_msg.py /path/to/message.txt
```

The PR workflow checks the title and body using the base branch's checker. Changes
to the checker need unit tests. Record only checks actually run. Maintainers must
edit dependency-bot PR messages to meet the same contract before merging; there is
no automatic bypass for bots.

## Compatibility and privacy

- Preserve documented CLI behavior and versioned JSON fields. Document intentional
  breaking changes rather than silently changing their meaning.
- Never guess missing token counts, model identities or prices.
- Parser changes that affect indexed results must invalidate the appropriate
  parser checkpoints and include incremental/deduplication regression tests.
- Do not commit credentials, live API responses, real transcripts, account
  balances, private project paths, or usage databases. Use synthetic fixtures.
- Secrets detectors are not privacy scrubbers. Review examples, screenshots,
  commit metadata and issue/PR content manually too.
- Never dismiss a secret finding with a blanket baseline. Investigate it, rotate
  real credentials, and use narrowly documented false-positive exceptions only.
