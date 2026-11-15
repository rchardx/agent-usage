# Accounting and cost estimates

[Overview](../README.md) · [Usage](usage.md) · [Accounting](accounting.md) · [Providers](providers.md)

These reports describe available local records, not a server billing ledger.
Account allowances have a separate scope; see [Providers](providers.md).

## Token accounting

| Normalized field | Codex source | Claude Code source | pi / omp source |
| --- | --- | --- | --- |
| Input, including cache | `input_tokens` | `input_tokens` + cache read + cache creation | `usage.input` + `usage.cacheRead` + `usage.cacheWrite` |
| Fresh input | Input minus cache read/write, when both are known | `input_tokens` | `usage.input` |
| Cache read | `cached_input_tokens` | `cache_read_input_tokens` | `usage.cacheRead` |
| Cache write | `cache_write_input_tokens`, if present | `cache_creation_input_tokens` | `usage.cacheWrite` |
| Cache write, 5m / 1h TTL | Not currently reported | `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` | Not currently reported |
| Output, including reasoning | `output_tokens` | `output_tokens` | `usage.output` |
| Reasoning | `reasoning_output_tokens`, if present | `output_tokens_details.thinking_tokens`, if present | `usage.reasoning` (pi) / `usage.reasoningTokens` (omp), if present |
| Other output | Output minus reasoning, when known | Output minus reasoning, when known | Output minus reasoning, when known |
| Total | Input + output | Input + output | Input + output |

Cache counts are components of normalized input. Reasoning is a component of
output. TTL counts are components of cache writes. **Do not add these components
to their parent counts again.** Tokens measure repeated request processing, not
unique words in a conversation or current context-window occupancy.

### Unknown values and JSON coverage

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

### Deduplication and request attribution

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

pi and omp share one session schema with per-request assistant usage records;
no differencing is needed. Events are keyed by response or row identity within
their session. Models are reported as `responseModel` when present, namespaced
with the upstream provider that served the request. Sidecar files stored next
to a session transcript (such as `changes.jsonl`) are never parsed as usage.

### History limitations

Dates describe logged usage-event times, not server billing periods. Logs with
missing timestamps, unsupported usage structures, or invalid counts produce
coverage notes. Deleted logs and requests never persisted by the clients cannot
be reconstructed; `history_complete` is always false.

## Cost estimates

By default, token reports fetch the public models.dev catalog and match exact
model IDs under `openai` for Codex and `anthropic` for Claude Code. Explicit
`openai/` or `anthropic/` prefixes are accepted; model aliases are not guessed.
pi and omp models first match through their explicit `upstream/model` prefix
(for example `deepseek/deepseek-v4-flash`). When the upstream is a gateway absent
from the catalog (for example `radius/kimi-k3`), the model id falls back to an
exact match in first-party channels — `anthropic`, `openai`, `google`,
`deepseek`, `moonshotai`, `zai`, `xai`, `minimax`, `qwen`, `meta`, `mistralai`,
in that order — so the real model is priced at its own channel's rates.
Gateway-only aliases (such as Radius's `balanced`/`cheap` profiles) and ids with
no exact first-party match stay unpriced.
Prices are USD per million tokens. The catalog is cached for 24 hours;
`--refresh` refreshes it, `--offline` uses cached prices without network access,
and `--no-cost` skips pricing altogether. Failed refreshes retain a dated, visibly
stale snapshot when available. `quota` and `doctor` never fetch prices.

**These are estimates at current standard API prices, not subscription bills or
credit consumption.** Historical price changes, fast/priority/batch adjustments,
regional modifiers, tool fees, and taxes are excluded. Prices are community
maintained and can change independently of local usage records.

### Calculation rules

Costs are calculated on usage records before grouping:

- Fresh input, cache reads, cache writes, and output use their respective rates.
  Reasoning is already part of output; a separate reasoning price replaces the
  output price for that subset only when models.dev supplies one.
- Context price tiers use each request's input size, including cache, rather
  than the monthly token sum. Explicit tier thresholds take precedence over the
  catalog's legacy `context_over_200k` alias. Cumulative deltas without identifiable
  request boundaries remain unpriced for models with context tiers.
- Claude Code 5-minute cache writes use the catalog's `cache_write` rate.
  One-hour writes use 2× the input rate, following
  [Anthropic's cache pricing](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
  Claude Code writes without a known TTL breakdown remain unpriced; other agents
  record no TTL split, so their writes use the `cache_write` rate even on the
  `anthropic` channel.
- Missing models, required prices, or required usage details produce unknown
  costs, not zero. The table shows priced-record coverage and a marked known
  subtotal when coverage is incomplete.

### JSON cost fields and unpriced records

JSON includes `pricing` source, fetch time, status, model price tables, and
`estimated_cost` for totals and every group. USD values are decimal strings
rounded to six decimal places after aggregation. `amount` is null when any
record is unpriced; `known_amount`, `known_components`, `priced_events`, and
`unpriced_reasons` preserve the available estimate and its coverage.
`unpriced_details` breaks down unpriced records by agent (`provider`), original
`model` identifier, and `reason`, with an `events` count for each combination.
Terminal reports show this breakdown with readable reasons, for example:
`codex / codex-auto-review: 36 records — missing model price`.
