# Usage guide

[Overview](../README.md) · [Usage](usage.md) · [Accounting](accounting.md) · [Providers](providers.md)

Install from GitHub as described in the [README](../README.md#install).
For command-line help, run `agent-usage --help` or
`agent-usage tokens --help`.

## Commands

```bash
agent-usage                                 # Account allowances + last 30 days, by day
agent-usage quota                           # Codex/Claude windows, resets, credits
agent-usage --refresh quota                 # Bypass the 120-second account cache
agent-usage --offline                       # Cached allowances + local tokens; no network
agent-usage tokens --since 7d               # Rolling seven-day interval
agent-usage tokens --today                  # Today from local midnight
agent-usage tokens --month --by model       # Calendar month, grouped by model
agent-usage --no-cost tokens                # Local token counts only; no price lookup
agent-usage tokens --all --by project       # Available local history, grouped by project
agent-usage tokens --since 7d --by day       # One row per day, with a usage bar
agent-usage tokens --since 2026-10-01 --until 2026-10-05 --by day --detail
agent-usage --provider claude tokens --all  # Select one provider
agent-usage --timezone Asia/Tokyo tokens    # Explicit reporting timezone
agent-usage tokens --all --json             # Machine-readable report
agent-usage --json quota                    # Global JSON flag also works
agent-usage doctor                          # Offline discovery and parser diagnostics
```

Global options (`--provider`, `--timezone`, `--offline`, `--refresh`, `--no-cost`, `--timeout`,
and path overrides) precede the subcommand. `tokens`, `quota`, and `doctor` also
accept their own `--json` flag. `--until` is exclusive; dates without an offset
use the reporting timezone, which defaults to the system timezone.

## Dates and reporting timezone

The default period is a rolling **30 days**, ending now (or at `--until`), not
the current calendar month. `--since 30d` is equivalent. Choose `--month` for the
current calendar month or use explicit dates for a previous calendar month.
Rolling days are exact 24-hour durations, including across daylight-saving changes.
Only one of `--since`, `--today`, `--month`, and `--all` may be selected.

## Terminal and JSON output

Terminal output fills the available width, separates input/output sections, and
places account panels side by side on wide terminals. Tokens use decimal
abbreviations: **K = 1,000; M = 1,000,000; B = 1,000,000,000; T = 1,000,000,000,000**.
JSON always retains exact integer token counts.

`--by day` prints one row per calendar day in the reporting timezone with fixed
metric columns — input, cache read/write, cache hit rate (cache read ÷ input),
output, total, and cost — plus a bar scaled to the busiest day. Columns adapt to
the terminal width, and all providers merge into each day's row, so the layout
stays stable no matter how many harnesses are installed. Days without records
inside the range show as zero; days cut by the period boundary are marked
partial. Add `--detail` for the full per-provider breakdown of each day.
The default `agent-usage` report uses this daily view; its JSON output keeps
provider totals, as with `agent-usage tokens --json`.

## Indexing and diagnostics

The first token query builds an index; later queries read appended transcript
bytes. `doctor` also updates this index and reports parsing issues without
querying either account. A report can succeed while a provider is unavailable;
automation should inspect account `status` and token `coverage`, not just the
exit code. Invalid CLI arguments exit with code 2.

See [Accounting and cost estimates](accounting.md) for token definitions,
unknown-value semantics and the JSON fields used to express incomplete coverage.

## Paths and privacy

Default inputs:

- Codex: `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, defaulting to
  `~/.codex` as the home directory.
- Claude: `$CLAUDE_CONFIG_DIR/projects`, defaulting to `~/.claude` as the home
  directory, including nested subagent logs.
- pi: `$PI_CODING_AGENT_DIR/sessions`, defaulting to `~/.pi/agent`. Only
  timestamp-named session transcripts are indexed.
- omp: `~/.omp/agent/sessions`, with the same transcript naming rule.
- Cache: the platform user cache directory, typically `~/.cache/agent-usage` on
  Linux. Override it with `--cache-dir`.

```bash
agent-usage --codex-home /path/to/.codex --claude-home /path/to/.claude tokens --all
agent-usage --pi-home /path/to/agent --omp-home /path/to/agent tokens --all
```

The scope is the current user and selected environment. WSL does not automatically
scan the Windows user's data. Each provider currently accepts one home directory.

The tool sends no prompts, redeems no resets, purchases no credits, and does not
write client configuration. Credentials stay in memory for the direct Claude
request and are never saved in this tool's cache or report. HTTP redirects are
refused for that request so bearer credentials cannot be forwarded to another origin.
Pricing downloads send no credentials, model selections, token counts, or logs
to models.dev; matching and cost calculations run locally.

The SQLite cache stores usage counters, timestamps, session identities, model
names, source paths, and project paths, but no conversation text or authentication
secrets. JSON reports grouped by project/session expose those local identifiers.
Cache files are private to the user where POSIX permissions are available. The
cache is rebuildable from retained source logs. It is not a durable archive of
deleted sessions.

Incremental reads assume append-only client logs. Inode changes, truncation,
same-size rewrites, and modifications near the last indexed offset trigger
reindexing. Arbitrary edits to earlier bytes followed by an append are not fully
detected: select a fresh `--cache-dir` after manually editing transcripts. An
unfinished final line is deferred until complete; oversized lines are reported
and left pending instead of allocating unbounded memory.

Read the [security policy](../SECURITY.md) before sharing diagnostics. Account
authentication and compatibility requirements are documented in
[Providers and account data](providers.md).
