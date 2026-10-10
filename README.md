# agent-usage

A local-first CLI for Codex and Claude Code account allowances and token usage.

Reports account quota windows, automatic reset times, available reset allowances,
credits, detailed tokens from local transcripts, and API-equivalent cost estimates
using [models.dev](https://models.dev/) prices. Account allowances and local
token history have separate scopes: other devices can affect account limits,
while this tool only counts logs available in the selected local directories.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/rchardx/agent-usage
agent-usage
```

For a development checkout:

```bash
uv sync --dev
uv run agent-usage
```

To run your checkout from any directory, install its editable command once:

```bash
uv tool install --editable /absolute/path/to/agent-usage
agent-usage
```

Ensure the tool executable directory (`uv tool dir --bin`) is on `PATH`.
`uv tool update-shell` can configure it for a new shell session.

Live adapters were verified on Linux/WSL with Codex CLI 0.160.0 and Claude Code
2.1.289. Other versions may expose different fields. Codex must be on `PATH` for
live account queries. Local token reports work without either CLI installed.

## Commands

```bash
agent-usage                                 # Account allowances + last 30 days of local tokens
agent-usage quota                           # Windows, resets, credits
agent-usage --refresh quota                 # Bypass the 120-second account cache
agent-usage --offline                       # Cached allowances + local tokens; no network
agent-usage tokens --since 7d               # Rolling seven-day interval
agent-usage tokens --today                  # Today from local midnight
agent-usage tokens --month --by model       # Calendar month, grouped by model
agent-usage --no-cost tokens                # Local token counts only; no price lookup
agent-usage tokens --all --by project       # Available local history, grouped by project
agent-usage tokens --since 7d --by day      # One row per day, with a usage bar
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

The default period is a rolling **30 days**, ending now (or at `--until`), not
the current calendar month. `--since 30d` is equivalent. Choose `--month` for the
current calendar month or use explicit dates for a previous calendar month.
Rolling days are exact 24-hour durations, including across daylight-saving changes.
Only one of `--since`, `--today`, `--month`, and `--all` may be selected.

Terminal output fills the available width, separates input/output sections, and
places account panels side by side on wide terminals. Tokens use decimal
abbreviations: **K = 1,000; M = 1,000,000; B = 1,000,000,000; T = 1,000,000,000,000**.
JSON always retains exact integer token counts.

`--by day` prints one row per calendar day in the reporting timezone, with
per-provider totals, cache hit rate (cache read ÷ input), output, cost, and a bar
scaled to the busiest day. Days without records inside the range show as zero;
days cut by the period boundary are marked partial. Add `--detail` for the full
per-provider breakdown of each day.

The first token query builds an index; later queries read appended transcript
bytes. `doctor` also updates this index and reports parsing issues without
querying either account. A report can succeed while a provider is unavailable;
automation should inspect account `status` and token `coverage`, not just the
exit code. Invalid CLI arguments exit with code 2.

## Token accounting

| Normalized field | Codex source | Claude Code source |
| --- | --- | --- |
| Input, including cache | `input_tokens` | `input_tokens` + cache read + cache creation |
| Fresh input | Input minus cache read/write, when both are known | `input_tokens` |
| Cache read | `cached_input_tokens` | `cache_read_input_tokens` |
| Cache write | `cache_write_input_tokens`, if present | `cache_creation_input_tokens` |
| Cache write, 5m / 1h TTL | Not currently reported | `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` |
| Output, including reasoning | `output_tokens` | `output_tokens` |
| Reasoning | `reasoning_output_tokens`, if present | `output_tokens_details.thinking_tokens`, if present |
| Other output | Output minus reasoning, when known | Output minus reasoning, when known |
| Total | Input + output | Input + output |

Cache counts are components of normalized input. Reasoning is a component of
output. TTL counts are components of cache writes. **Do not add these components
to their parent counts again.** Tokens measure repeated request processing, not
unique words in a conversation or current context-window occupancy.

Missing fields stay unknown. Text output uses `—` for unknown and `≥…*` for the
observed subtotal of a partially covered field. Every JSON metric includes:

```json
{
  "tokens": null,
  "known_tokens": 300,
  "observed_events": 8,
  "total_events": 10
}
```

Here, two events lack the field. `300` is the known subtotal, not a complete total.
When coverage is complete, `tokens` is the full value (including a genuine zero).
The top-level JSON envelope has `schema_version: 1`.

Codex cumulative snapshots are differenced within each session; repeated
snapshots are ignored. If a cumulative counter resets, only the reported last
request is counted for that transition and a coverage note is recorded. Replayed
fork history before the child session's creation time is excluded. Initial
cumulative snapshots that lack per-request attribution are marked in coverage.
The first session metadata record owns a Codex transcript; embedded ancestor
metadata cannot change a child session's identity. Parser version changes
automatically invalidate file checkpoints and rebuild the derived index.

Claude's repeated content blocks and streaming updates are merged by request
and message identity using monotonic component counts. Copied transcripts are
deduplicated across files. Subagent transcripts are included. Historical events
are not assigned to whichever account happens to be logged in today.

Dates describe logged usage-event times, not server billing periods. Logs with
missing timestamps, unsupported usage structures, or invalid counts produce
coverage notes. Deleted logs and requests never persisted by the clients cannot
be reconstructed; `history_complete` is always false.

## Cost estimates

By default, token reports fetch the public models.dev catalog and match exact
model IDs under `openai` for Codex and `anthropic` for Claude Code. Explicit
`openai/` or `anthropic/` prefixes are accepted; model aliases are not guessed.
Prices are USD per million tokens. The catalog is cached for 24 hours;
`--refresh` refreshes it, `--offline` uses cached prices without network access,
and `--no-cost` skips pricing altogether. Failed refreshes retain a dated, visibly
stale snapshot when available. `quota` and `doctor` never fetch prices.

**These are estimates at current standard API prices, not subscription bills or
credit consumption.** Historical price changes, fast/priority/batch adjustments,
regional modifiers, tool fees, and taxes are excluded. Prices are community
maintained and can change independently of local usage records.

Costs are calculated on usage records before grouping:

- Fresh input, cache reads, cache writes, and output use their respective rates.
  Reasoning is already part of output; a separate reasoning price replaces the
  output price for that subset only when models.dev supplies one.
- Context price tiers use each request's input size, including cache, rather
  than the monthly token sum. Explicit tier thresholds take precedence over the
  catalog's legacy `context_over_200k` alias. Cumulative deltas without identifiable
  request boundaries remain unpriced for models with context tiers.
- Claude 5-minute cache writes use the catalog's `cache_write` rate. One-hour
  writes use 2× the input rate, following
  [Anthropic's cache pricing](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
  Nonzero writes without a known TTL breakdown remain unpriced.
- Missing models, required prices, or required usage details produce unknown
  costs, not zero. The table shows priced-record coverage and a marked known
  subtotal when coverage is incomplete.

JSON includes `pricing` source, fetch time, status, model price tables, and
`estimated_cost` for totals and every group. USD values are decimal strings
rounded to six decimal places after aggregation. `amount` is null when any
record is unpriced; `known_amount`, `known_components`, `priced_events`, and
`unpriced_reasons` preserve the available estimate and its coverage.

## Account data

- **Codex:** the installed CLI's App Server `account/read` and
  `account/rateLimits/read` methods. Only the recognized `codex` quota bucket
  is reported; additional buckets with unknown semantics are filtered out.
  Window duration is read from the response rather than assuming five-hour and
  weekly windows. `rateLimitResetCredits.availableCount` is authoritative even
  when reset detail rows are missing or capped.
- **Claude Code:** the client-internal `GET https://api.anthropic.com/api/oauth/usage`
  endpoint, using the existing OAuth credentials file. This is an independently
  isolated compatibility adapter, not a public API guarantee. Legacy and unified
  allowance views are deduplicated where their identity and values match.
  Recognized windows are `five_hour`, `seven_day`, `seven_day_sonnet`,
  `seven_day_opus`, and the unified `session` / `weekly_all` equivalents.
  Other fields such as `iguana_necktie` are ignored, even if they contain
  utilization or reset values. Extra usage and spend balances remain supported.
- **Credits:** provider balances, limits, usage, enabled status, and unlimited
  status remain separate. Numeric values are retained in provider units with
  `provider_units_unverified` until their scale is verified. No currency conversion
  or subscription-cost estimate is inferred. A missing balance is not zero;
  `hasCredits: false` alone does not establish a disabled billing feature.
- **Reset:** automatic window reset times and consumable reset allowances are
  distinct. Reset expiry details are displayed when returned. Missing reset
  information means "not reported", not "no resets left".

The same recognition rules apply to terminal output, JSON, and cached snapshots.
Unknown allowance windows and credit buckets are omitted rather than guessed
from their identifiers or provider-supplied names. Recognized items have readable
`label` values; JSON retains their `bucket` identifiers for machine use.

Account results include their source, observation time, age, and one of `ok`,
`cached`, `stale`, or `unavailable`. Queries run concurrently with bounded
timeouts. A failed refresh can return the last matching snapshot marked `stale`
and accompanied by the error. A past reset timestamp is displayed as such; the
tool does not manufacture a new zero-usage window. There is no automatic retry
loop when a provider rate-limits queries.

Quota caches are tied to the credentials file's location and metadata to avoid
reusing a snapshot after an account switch. Without file-backed credentials,
account caching is disabled. Claude OS-keychain credentials and automatic OAuth
refresh are not implemented; refresh an expired login using Claude Code. Codex
manages its own authentication through its installed App Server.

## Paths and privacy

Default inputs:

- Codex: `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, defaulting to
  `~/.codex` as the home directory.
- Claude: `$CLAUDE_CONFIG_DIR/projects`, defaulting to `~/.claude` as the home
  directory, including nested subagent logs.
- Cache: the platform user cache directory, typically `~/.cache/agent-usage` on
  Linux. Override it with `--cache-dir`.

```bash
agent-usage --codex-home /path/to/.codex --claude-home /path/to/.claude tokens --all
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

## Development

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

Tests use synthetic transcripts and mocked account/pricing endpoints. No account or
network credentials are required. CI checks Python 3.11 and 3.14 on Linux.

References: [Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Claude status-line fields](https://code.claude.com/docs/en/statusline), and
[Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
