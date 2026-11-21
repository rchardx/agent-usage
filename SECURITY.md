# Security policy

## Reporting a vulnerability

Do not post credentials, exploitable vulnerability details or real transcripts in
public issues or pull requests. Use GitHub's private vulnerability reporting via
the repository's **Security → Report a vulnerability** page when available. If
private reporting is unavailable, open an issue requesting a private contact
channel without including vulnerability details.

Include the affected version, impact and minimal synthetic reproduction. Remove
tokens, cookies, account IDs, session IDs and private paths. If a credential was
exposed, revoke or rotate it immediately; deleting a file or rewriting Git history
does not revoke credentials or erase all copies.

Security fixes target the latest released version and current main branch.
Older versions do not have a separate long-term support guarantee.

## Data and trust boundaries

agent-usage reads local agent transcripts and, for live quota queries, existing
account authentication. It does not submit inference requests, refresh Claude
OAuth credentials, redeem resets or purchase credits.

- Claude bearer credentials are sent only to the fixed Anthropic usage endpoint;
  redirects are refused. This client-internal endpoint is not a public API
  compatibility guarantee.
- Codex and Antigravity live quota queries invoke the installed `codex` and `agy`
  executables on `PATH`. Install trusted agent CLIs and dependencies.
- Pricing requests fetch the public models.dev catalog without sending logs,
  credentials, token counts or model selections.
- The index omits conversation text and authentication secrets, but contains
  session identifiers, timestamps, model names and local source/project paths.
- Quota snapshots and reports can expose account usage or balances. Project/session
  reports and `doctor` output are not anonymized.

Use `--offline` to avoid live quota and pricing requests. Use
`--offline --no-cost tokens` for local token counts without price lookup. Source
logs, caches and exported reports should remain private. POSIX file permissions
do not provide equivalent access controls on every platform.

## Safe diagnostics and development

Report bugs with synthetic records rather than full sessions, raw account
responses, credential files or SQLite caches. Do not attach `auth.json`,
`.credentials.json`, browser cookies or unredacted screenshots.

Tests use fake credentials and generated transcripts, with network sockets
disabled by default. CI needs no provider API keys or account credentials.
Local secrets hooks and CI scans complement manual review; neither guarantees
that a repository or report contains no private information.
