# agent-usage

A local-first CLI for account allowances, token usage and API-equivalent cost
estimates across Codex, Claude Code, Antigravity, pi and omp.

- **Account allowances:** quota windows, reset times and credits for Codex and
  Claude Code. Antigravity, pi and omp contribute local token history.
- **Token reports:** daily usage and breakdowns by agent, model, project or session.
- **Cost estimates:** per-request estimates using [models.dev](https://models.dev/)
  prices, with explicit explanations for unpriced records.
- **Terminal and JSON output:** readable summaries and exact machine-readable counts.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/rchardx/agent-usage
```

This installs the current `main` branch, not a versioned release. Install from
GitHub: the PyPI package named `agent-usage` belongs to a different project.

To install the tested [nightly prerelease](https://github.com/rchardx/agent-usage/releases/tag/nightly)
with the [GitHub CLI](https://cli.github.com/), run these commands in an empty directory:

```bash
repo=rchardx/agent-usage
version=$(gh release view nightly -R "$repo" --json body \
  --jq '.body | capture("^Rolling development build (?<version>[0-9a-z.+]+) from ").version')
wheel="agent_usage-$version-py3-none-any.whl"
gh release download nightly -R "$repo" -p "$wheel" && uv tool install --force "./$wheel"
agent-usage --version
```

If the nightly changes between lookup and download, repeat the commands in a fresh directory.

If the command is not found, ensure `uv tool dir --bin` is on `PATH`;
`uv tool update-shell` can configure it for a new shell session.

Local token reports read existing agent logs and do not require the agent CLIs to
be installed. Live account queries need existing agent authentication, and Codex
queries also require `codex` on `PATH`. See [provider requirements](docs/providers.md)
for supported versions and limitations.

## Quick start

```bash
agent-usage                                  # Allowances + daily usage for the last 30 days
agent-usage quota                            # Account windows, resets and credits
agent-usage tokens --since 7d --by day        # Daily token usage for the last week
agent-usage tokens --month --by model        # Calendar month, grouped by model
agent-usage tokens --today --json            # Today's exact counts and estimates
agent-usage --offline --no-cost tokens       # Local counts only; no network requests
```

Global options such as `--provider`, `--offline` and `--timezone` go **before** the
subcommand. The default period is a rolling 30 days, not the current calendar
month; use `--month` for the latter. Dates use the system timezone unless overridden.

For all options, run `agent-usage --help` and `agent-usage tokens --help`, or read
the [usage guide](docs/usage.md).

## Important limits

- **Local history is incomplete.** Deleted logs and other devices are not included;
  account allowances may reflect activity beyond these local records.
- **Estimates are not bills.** Costs use current standard API rates, not historical
  prices, subscription charges or credit consumption.
- **Unknown is not zero.** `—` means unknown; `≥…*` marks a known subtotal with
  incomplete coverage. Unpriced records are listed by agent, model and reason.
- **Local-first is not always offline.** Default reports can query accounts and
  download prices. Use `--offline` to avoid these network requests.
- **Reports are not anonymized.** The index excludes conversation text and
  credentials, but reports can contain private paths, identifiers and balances.
  See the [security policy](SECURITY.md) before sharing diagnostics.

## Documentation

| Guide | Contents |
| --- | --- |
| [Usage](docs/usage.md) | Commands, date ranges, output, data paths and diagnostics |
| [Accounting and costs](docs/accounting.md) | Token definitions, deduplication, pricing and JSON coverage |
| [JSON interface](docs/json-interface.md) | Bundled schema, compatibility policy and offline validation |
| [Providers](docs/providers.md) | Account data sources, authentication and compatibility |
| [Contributing](CONTRIBUTING.md) | Development installation, checks, hooks and PR conventions |
| [Security](SECURITY.md) | Privacy boundaries and private vulnerability reporting |

## License

[MIT](LICENSE).
