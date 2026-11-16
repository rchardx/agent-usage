# JSON interface

[Overview](../README.md) · [Usage](usage.md) · [Accounting](accounting.md)

The versioned interface is the JSON emitted by successful report commands with
`--json`. Terminal tables, help, version output and error messages are not this
interface. The `schema` command prints the schema itself, not a report envelope.

## Schema and offline validation

The authoritative schema is
[`src/agent_usage/schemas/report-v1.schema.json`](../src/agent_usage/schemas/report-v1.schema.json).
It uses JSON Schema Draft 2020-12 and is included in local installations, wheels
and source distributions. Export the schema matching the installed code with:

```bash
agent-usage schema > report-v1.schema.json
agent-usage --offline --no-cost tokens --all --json > report.json
```

Printing the schema does not query accounts, fetch prices or create an index.
Its identifier, `urn:agent-usage:report:v1`, is a schema identity, not a download
endpoint. All references are local to the document; validation requires no network.

From a development checkout, after `uv sync --locked --dev`, validate a report:

```bash
uv run --locked python scripts/validate_report.py report.json
```

The validator checks the bundled schema and timestamp formats, rejects non-JSON
numbers such as `NaN`, and exits 0 on success or 1 on failure. It does not print
the offending field value or the full report. Validation dependencies are
**development-only**; the installed CLI does not require `jsonschema`.

## Envelope and commands

Every report requires `schema_version: 1` and a timezone-aware `generated_at`
timestamp. It contains at least one report section:

| Invocation | Sections |
| --- | --- |
| `agent-usage --json` | `quota` and `tokens` |
| `agent-usage --provider pi --json` (also omp) | `tokens` only |
| `agent-usage tokens --json` | `tokens` |
| `agent-usage quota --json` | `quota` |
| `agent-usage doctor --json` | `doctor` |

Global options precede the subcommand. `tokens`, `quota` and `doctor` also accept
`--json` locally. `quota` with a token-only provider is an argument error.

- `tokens` includes the reporting timezone, period, grouping, totals, groups and
  local-history coverage. `period.start` is null for `--all`; `end_exclusive` is
  exclusive. JSON does not replace unknown values with terminal placeholders.
- `quota` has account scope. Each account has a `status` of `ok`, `cached`, `stale`
  or `unavailable`, with windows, credits, reset information and warnings/errors.
  `age_seconds` is optional; do not assume an unavailable account has a snapshot.
- `doctor` contains CLI discovery, configured paths, detected local auth modes
  and parser coverage. The optional `index_rebuilt: true` confirms an explicit
  successful rebuild; it is absent from ordinary diagnostics. Doctor output is
  not anonymized.

## Numbers, missing values and costs

Token metrics contain `tokens`, `known_tokens`, `observed_events` and `total_events`.
`tokens: null` means incomplete coverage, not zero; `known_tokens` retains the
observed subtotal. Empty selections have genuine zero counts. Token values are
JSON integers: consumers must use integer-preserving parsers for large counts.
The schema checks structure and types, not all arithmetic relationships between
counters. Accounting regression tests cover those relationships separately.

Cost `amount` is null when any selected record is unpriced. `known_amount` and
`known_components` retain priced subtotals, with event counts and unpriced reasons.
Estimated USD amounts are decimal strings with exactly six fractional digits;
use decimal arithmetic rather than converting them to binary floats.

With `--no-cost`, `pricing` and every `estimated_cost` field are absent, not zero
or null. With pricing enabled but unavailable, those fields are present and
express unknown costs. Price tables under `cost_per_million_usd` are extensible
catalog metadata, not a fixed catalog schema. See [Accounting](accounting.md) for
calculation rules, grouping and coverage semantics.

## Compatibility policy

Within schema version 1:

- Existing required fields, their types and meanings remain stable. Nullable
  values stay distinct from missing fields and genuine zero.
- New optional object fields may be added. Consumers must ignore unknown fields;
  the schema deliberately permits additional properties.
- Provider/model identifiers, issue keys and unpriced reason codes are open-ended
  strings. Consumers must handle unfamiliar values without inventing a price or
  assuming success. The explicitly enumerated account/pricing statuses and
  `group_by` values are closed sets in this version.
- Object key order and presentation formatting are not contracts. Do not depend
  on array order for identifying a provider, model or group.

Removing/renaming an existing field, changing its type or meaning, making an
optional field required, or changing a closed enum requires a new schema version
and documented migration. The schema version is independent of the package
version and the internal SQLite index version. Existing schema files must remain
available when a later JSON version is introduced.

Successful reports use stdout; operational diagnostics use stderr. Invalid
arguments exit 2. Index/storage failures and JSON encoding failures exit 1,
without emitting a partial JSON report. There is no JSON error envelope in v1.
A successful command can still contain unavailable accounts, stale prices or
partial usage: inspect `status` and `coverage`, not just the exit code. JSON
encoding rejects non-finite numbers instead of emitting invalid `NaN`/`Infinity`.

## Synthetic examples

These fixtures contain invented records and paths, not user data, and are
validated in the test suite:

- [Combined report with partial costs and an unavailable account](examples/combined-partial.json)
- [Empty token history with pricing disabled](examples/tokens-empty-no-cost.json)
- [Doctor after rebuilding an empty index](examples/doctor-rebuilt.json)

Do not post real reports without reviewing paths, identifiers and account data.
See [Security](../SECURITY.md).
