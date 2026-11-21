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
Token amounts (including unscaled values) and cache-hit percentages display
exactly two decimal places, for example `123.00`, `1.20K`, and `60.00%`. USD
estimates also use two places, retaining `<$0.01` for small positive amounts.
Table labels and dates align left; numeric columns align right with space
reserved for units and coverage markers so decimal points stay aligned across
rows and totals. Session and record counts remain integers. JSON retains exact
integer token counts and the existing six-decimal USD strings.

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
The [JSON interface](json-interface.md) defines the versioned schema, compatibility
rules and offline validation. Export it with `agent-usage schema`.

### Recovering the usage index

If the usage index is damaged, incompatible, or stale after manually editing logs,
explicitly rebuild it from the currently configured source directories:

```bash
agent-usage doctor --rebuild-index
agent-usage --cache-dir /path/to/cache doctor --rebuild-index --json
```

Repeat any `--codex-home`, `--claude-home`, `--antigravity-home`, `--pi-home` and `--omp-home` overrides
used for your reports. Rebuilding requires `--provider all` (the default): it
replaces the shared index, not just a single provider's rows. It cannot recover
missing/deleted logs, and an empty set of source directories produces an empty
index. Verify your configured paths before rebuilding.

The rebuild reads local logs without querying accounts or prices. As with normal
`doctor`, installed agent executables may be probed with `--version`. It stages a
new index privately in the same cache directory, checks it and atomically replaces
only `index.sqlite3`. A failed scan, integrity check or replacement leaves the
previous index in place. Source logs, credentials, quota snapshots and cached
prices are not modified. Unreadable discovered files abort the rebuild; malformed
or unfinished log records retain the usual coverage warnings. Allow enough disk
space for both the old and new indexes during a rebuild.

Index operations use an OS-backed exclusive lock. Concurrent commands fail with a
busy message instead of racing a rebuild; retry after the other command finishes.
Process exit releases the lock, including after a crash. The small `index.lock`
file intentionally remains: **do not delete it to unlock a running process**.
A forcibly terminated rebuild may leave a private `.index-rebuild-*` staging
directory; it is not used as the active index.

Stop older agent-usage versions and external SQLite tools before rebuilding; they
do not participate in this lock. SQLite `-wal`, `-shm` or `-journal` recovery files
are never discarded by the rebuild. If they are present, close other processes
and try a normal token query to let SQLite recover, or select a fresh writable
`--cache-dir`. Rebuilds also refuse a symbolic-link index. Locking/atomic replacement
assume a local filesystem with working OS locks, not an unsupported network share.

Storage failures exit 1 with an actionable stderr message and no partial JSON.
For permission errors, use a writable cache directory; for a full disk, free
space first. Failures never silently delete the index or turn its contents into
zero usage. Missing or malformed quota/price snapshots continue to use their
existing unavailable/stale fallback behavior; rebuilding the usage index does
not refresh those snapshots.

## Paths and privacy

Default inputs:

- Codex: `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, defaulting to
  `~/.codex` as the home directory.
- Claude: `$CLAUDE_CONFIG_DIR/projects`, defaulting to `~/.claude` as the home
  directory, including nested subagent logs.
- Antigravity: `conversations/*.db` across `~/.gemini/antigravity`,
  `~/.gemini/antigravity-cli`, and `~/.gemini/antigravity-ide` by default, or
  `conversations/*.db` under a single directory via `--antigravity-home`.
- pi: `$PI_CODING_AGENT_DIR/sessions`, defaulting to `~/.pi/agent`. Only
  timestamp-named session transcripts are indexed.
- omp: `~/.omp/agent/sessions`, with the same transcript naming rule.
- Cache: the platform user cache directory, typically `~/.cache/agent-usage` on
  Linux. Override it with `--cache-dir`.

```bash
agent-usage --codex-home /path/to/.codex --claude-home /path/to/.claude tokens --all
agent-usage --antigravity-home /path/to/antigravity-cli tokens --all
agent-usage --pi-home /path/to/agent --omp-home /path/to/agent tokens --all
```

The scope is the current user and selected environment. WSL does not automatically
scan the Windows user's data. Antigravity aggregates its local client roots unless
overridden; other providers accept one home directory.

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
detected: run `doctor --rebuild-index` (or select a fresh `--cache-dir`) after
manually editing transcripts. An
unfinished final line is deferred until complete; oversized lines are reported
and left pending instead of allocating unbounded memory.

Read the [security policy](../SECURITY.md) before sharing diagnostics. Account
authentication and compatibility requirements are documented in
[Providers and account data](providers.md).
