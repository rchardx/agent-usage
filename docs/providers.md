# Providers and account data

[Overview](../README.md) · [Usage](usage.md) · [Accounting](accounting.md) · [Providers](providers.md)

Codex and Claude Code contribute both account allowances and local token history.
pi and omp are multi-provider harnesses, not subscription accounts: they
contribute token history only and are excluded from quota reports.

Account allowances and local token history have separate scopes. Other devices
can affect account limits, while token reports only count logs available in the
selected local directories. See [Usage](usage.md#paths-and-privacy) for data paths
and [Accounting](accounting.md) for record normalization.

## Requirements and compatibility

Live adapters were verified on Linux/WSL with Codex CLI 0.160.0, Claude Code
2.1.289, pi 1.5.2, and omp 18.20.2. Other versions may expose different fields.
Codex must be on `PATH` for live account queries. Local token reports work
without any of the CLIs installed.

CI runs synthetic tests on Linux with Python 3.11 and 3.14, and on macOS and
Windows with Python 3.14. These checks do not validate live account adapters on
those platforms.

## Account data

- **Codex:** the installed CLI's App Server `account/read` and
  `account/rateLimits/read` methods. Only the recognized `codex` quota bucket
  is reported; additional buckets with unknown semantics are filtered out.
  Window duration is read from the response rather than assuming five-hour and
  weekly windows. `rateLimitResetCredits.availableCount` is authoritative even
  when reset detail rows are missing or capped.
- **pi / omp:** multi-provider harnesses without a subscription account. No
  account quota concept applies; they are excluded from quota reports, and
  selecting them for `quota` is an error. Only local token history is available.
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

## Status, caching and authentication

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

## References

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Claude status-line fields](https://code.claude.com/docs/en/statusline)
- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
