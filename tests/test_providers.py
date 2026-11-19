import io
import json
import queue
import time
import urllib.error
from importlib.metadata import version

import pytest

from agent_usage.models import ProbeError
from agent_usage.providers import (
    CodexRPC,
    NoRedirect,
    claude_auth_inspection,
    codex_auth_inspection,
    fetch_claude,
    fetch_codex,
    normalize_claude,
    normalize_codex,
)


@pytest.mark.parametrize("unknown", ["iguana_necktie", "seven_day_future", "future_allowance"])
def test_claude_ignores_unrecognized_allowances_before_parsing(unknown):
    report = normalize_claude(
        {
            "five_hour": {"utilization": 0, "resets_at": None},
            "seven_day_sonnet": {"utilization": 25, "resets_at": None},
            "seven_day_opus": {"utilization": 50, "resets_at": None},
            unknown: {"utilization": 90, "resets_at": "unsupported timestamp"},
            "limits": [
                {"kind": unknown, "percent": 90, "resets_at": "unsupported timestamp"},
                {"kind": "weekly_all", "percent": 10, "resets_at": None},
            ],
        },
        "pro",
    )
    assert [w.label for w in report.windows] == ["All models", "Sonnet", "Opus", "All models"]
    assert [w.used_percent for w in report.windows] == [0, 25, 50, 10]
    assert unknown not in json.dumps(report.to_dict())


def test_codex_ignores_unknown_buckets_even_with_a_provider_label():
    report = normalize_codex(
        {},
        {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 0}, "limitName": "internal_alias"},
                "future_bucket": {
                    "limitName": "Looks readable",
                    "primary": {"resetsAt": "unsupported timestamp"},
                    "credits": {"balance": "100"},
                },
            },
        },
    )
    assert len(report.windows) == 1
    assert report.windows[0].label == "Codex"
    assert report.windows[0].used_percent == 0
    assert report.credits == []
    assert "internal_alias" not in json.dumps(report.to_dict())


def test_codex_unknown_compatibility_bucket_is_filtered():
    report = normalize_codex(
        {}, {"rateLimits": {"limitId": "future_bucket", "primary": {"usedPercent": 50}}}
    )
    assert report.windows == []


def credentials(paths, **extra):
    (paths.claude / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "secret-test-token",
                    "subscriptionType": "pro",
                    "expiresAt": time.time() * 1000 + 60000,
                    **extra,
                },
            }
        )
    )


def test_claude_request_stays_on_expected_origin_and_normalizes(paths, monkeypatch):
    credentials(paths)

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
            assert request.get_method() == "GET"
            assert request.get_header("User-agent") == f"agent-usage/{version('agent-usage')}"
            assert request.get_header("Authorization") == "Bearer secret-test-token"
            return io.BytesIO(b'{"five_hour":{"utilization":12,"resets_at":null}}')

    monkeypatch.setattr("urllib.request.build_opener", lambda handler: Opener())
    report = fetch_claude(paths.claude)
    assert report.windows[0].used_percent == 12
    assert "secret-test-token" not in json.dumps(report.to_dict())
    assert NoRedirect().redirect_request(None, None, 302, None, None, "https://other.test") is None


def test_claude_429_does_not_print_body_or_credentials(paths, monkeypatch):
    credentials(paths)

    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError(
                "https://example.test",
                429,
                "secret-test-token",
                {},
                io.BytesIO(b"secret-test-token"),
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda handler: Opener())
    with pytest.raises(ProbeError, match="HTTP 429") as error:
        fetch_claude(paths.claude)
    assert "secret-test-token" not in str(error.value)


def test_expired_credentials_do_not_trigger_refresh_or_network(paths, monkeypatch):
    credentials(paths, expiresAt=1)
    monkeypatch.setattr(
        "urllib.request.build_opener", lambda *args: pytest.fail("Network attempted")
    )
    with pytest.raises(ProbeError, match="expired"):
        fetch_claude(paths.claude)


def test_rpc_handles_notifications_and_refuses_server_refresh_requests():
    rpc = object.__new__(CodexRPC)
    rpc.timeout = 0.1
    rpc.sequence = 0
    rpc.messages = queue.Queue()
    sent = []
    rpc.send = sent.append
    rpc.messages.put({"method": "account/updated", "params": {}})
    rpc.messages.put({"id": 1, "method": "account/chatgptAuthTokens/refresh"})
    rpc.messages.put({"id": 1, "result": {"ok": True}})
    assert rpc.call("account/read") == {"ok": True}
    assert sent[1]["error"]["code"] == -32601


def test_rpc_timeout_and_errors_are_bounded_and_sanitized():
    rpc = object.__new__(CodexRPC)
    rpc.timeout = 0.001
    rpc.sequence = 0
    rpc.messages = queue.Queue()
    rpc.send = lambda request: None
    with pytest.raises(ProbeError, match="timed out"):
        rpc.call("account/read")
    rpc.messages.put({"id": 2, "error": {"code": -32601, "message": "private-response"}})
    with pytest.raises(ProbeError) as error:
        rpc.call("account/read")
    assert "private-response" not in str(error.value)


@pytest.mark.parametrize("account_type", ["apiKey", "gcp", "amazonBedrock"])
def test_codex_rpc_non_subscription_modes_report_clean_unavailable(
    paths, monkeypatch, account_type
):
    called_methods = []

    class FakeRPC:
        def __init__(self, home, timeout):
            pass

        def call(self, method, params=None):
            called_methods.append(method)
            if method == "initialize":
                assert params["clientInfo"]["version"] == version("agent-usage")
                return {}
            if method == "account/read":
                return {"account": {"type": account_type}, "requiresOpenaiAuth": True}
            pytest.fail(f"Unexpected RPC call: {method}")

        def send(self, message):
            pass

        def close(self):
            pass

    monkeypatch.setattr("agent_usage.providers.CodexRPC", FakeRPC)
    report = fetch_codex(paths.codex)
    assert report.status == "unavailable"
    assert report.auth_mode == account_type
    assert report.error is None
    assert report.windows == []
    assert any("subscription allowances do not apply" in w for w in report.warnings)
    assert "account/rateLimits/read" not in called_methods


@pytest.mark.parametrize(
    ("provider_type", "expected_mode"),
    [
        ("customGateway", "customGateway"),
        ("gcp", "gcp"),
        ("bedrock", "amazonBedrock"),
    ],
)
def test_codex_rpc_custom_provider_without_openai_auth(
    paths, monkeypatch, provider_type, expected_mode
):
    class FakeRPC:
        def __init__(self, home, timeout):
            pass

        def call(self, method, params=None):
            if method == "initialize":
                return {}
            if method == "account/read":
                return {"account": None, "requiresOpenaiAuth": False}
            if method == "modelProvider/list":
                return {
                    "defaultProviderId": "custom",
                    "data": [{"id": "custom", "settings": {"type": provider_type}}],
                }
            pytest.fail(f"Unexpected RPC call: {method}")

        def send(self, message):
            pass

        def close(self):
            pass

    monkeypatch.setattr("agent_usage.providers.CodexRPC", FakeRPC)
    report = fetch_codex(paths.codex)
    assert report.status == "unavailable"
    assert report.auth_mode == expected_mode
    assert report.error is None


def test_codex_env_api_key_and_missing_cli_fallback(paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-env-sentinel")

    def missing_cli(home, timeout):
        raise ProbeError("Cannot start codex; install it and check PATH.")

    monkeypatch.setattr("agent_usage.providers.CodexRPC", missing_cli)
    report = fetch_codex(paths.codex)
    assert report.status == "unavailable"
    assert report.auth_mode == "apiKey"
    assert report.source == "codex-env"
    assert report.error is None
    assert "test-openai-env-sentinel" not in json.dumps(report.to_dict())


@pytest.mark.parametrize("failing_method", ["initialize", "account/read"])
def test_codex_handshake_failure_falls_back_to_local_auth_and_closes_rpc(
    paths, monkeypatch, failing_method
):
    closed = []
    key_field = "OPENAI" + "_API_KEY"
    (paths.codex / "auth.json").write_text(
        json.dumps({key_field: "test-auth-file-sentinel"}), encoding="utf-8"
    )

    class FailingHandshakeRPC:
        def __init__(self, home, timeout):
            pass

        def call(self, method, params=None):
            if method == failing_method:
                raise ProbeError("Codex app-server exited before responding.")
            return {}

        def send(self, message):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr("agent_usage.providers.CodexRPC", FailingHandshakeRPC)
    report = fetch_codex(paths.codex)
    assert closed == [True]
    assert report.status == "unavailable"
    assert report.auth_mode == "apiKey"
    assert report.source == "codex-auth-file"
    assert report.error is None
    assert "test-auth-file-sentinel" not in json.dumps(report.to_dict())


@pytest.mark.parametrize("has_chatgpt_auth_file", [False, True])
def test_codex_handshake_failure_preserves_error_without_non_subscription_auth(
    paths, monkeypatch, has_chatgpt_auth_file
):
    closed = []
    if has_chatgpt_auth_file:
        token_field = "access" + "_token"
        (paths.codex / "auth.json").write_text(
            json.dumps({"tokens": {token_field: "test-chatgpt-token"}}), encoding="utf-8"
        )
        # An ambient env key does not mask an app-server failure for a ChatGPT auth.json account.
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-env-sentinel")

    class FailingHandshakeRPC:
        def __init__(self, home, timeout):
            pass

        def call(self, method, params=None):
            raise ProbeError("Codex initialize failed (RPC -32601). Check login and CLI version.")

        def send(self, message):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr("agent_usage.providers.CodexRPC", FailingHandshakeRPC)
    with pytest.raises(ProbeError, match="Codex initialize failed"):
        fetch_codex(paths.codex)
    assert closed == [True]


def test_codex_rate_limits_failure_after_chatgpt_auth_never_falls_back_to_api_key(
    paths, monkeypatch
):
    closed = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-env-sentinel")

    class FailingRateLimitsRPC:
        def __init__(self, home, timeout):
            pass

        def call(self, method, params=None):
            if method == "initialize":
                return {}
            if method == "account/read":
                return {"account": {"type": "chatgpt", "planType": "pro"}}
            if method == "account/rateLimits/read":
                raise ProbeError("Codex account/rateLimits/read timed out.")
            pytest.fail(f"Unexpected RPC call: {method}")

        def send(self, message):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr("agent_usage.providers.CodexRPC", FailingRateLimitsRPC)
    with pytest.raises(ProbeError, match="rateLimits/read timed out"):
        fetch_codex(paths.codex)
    assert closed == [True]


def test_codex_config_toml_inspection_never_leaks_keys(paths):
    (paths.codex / "config.toml").write_text(
        'model_provider = "google"\n'
        "[model_providers.google]\n"
        'name = "Google Gemini"\n'
        'base_url = "https://generativelanguage.googleapis.com/v1beta/openai"\n'
        'experimental_bearer_token = "secret-toml-token"\n',
        encoding="utf-8",
    )
    assert codex_auth_inspection(paths.codex) == ("customGateway", "codex-config")


@pytest.mark.parametrize(
    ("env_pairs", "settings_data", "expected_mode", "expected_source"),
    [
        ([("ANTHROPIC_API_KEY", "test-ant-sentinel")], {}, "api_key", "claude-env"),
        ([("ANTHROPIC_AUTH_TOKEN", "test-bearer-sentinel")], {}, "api_key", "claude-env"),
        (
            [
                ("ANTHROPIC_AUTH_TOKEN", "test-gw-sentinel"),
                ("ANTHROPIC_BASE_URL", "https://gateway.example.test/v1"),
            ],
            {},
            "customGateway",
            "claude-env",
        ),
        ([("CLAUDE_CODE_USE_VERTEX", "1")], {}, "vertex", "claude-env"),
        ([("CLAUDE_CODE_USE_BEDROCK", "true")], {}, "bedrock", "claude-env"),
        ([("CLAUDE_CODE_USE_FOUNDRY", "yes")], {}, "foundry", "claude-env"),
        (
            [],
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "test-settings-sentinel",
                    "ANTHROPIC_BASE_URL": "https://proxy.example.test",
                }
            },
            "customGateway",
            "claude-settings",
        ),
        (
            [],
            {"apiKeyHelper": "!must-never-execute-helper-sentinel"},
            "api_key",
            "claude-settings",
        ),
    ],
)
def test_claude_non_subscription_modes_skip_network_and_redact_secrets(
    paths, monkeypatch, env_pairs, settings_data, expected_mode, expected_source
):
    # Even if an expired OAuth file remains on disk, active non-subscription auth wins cleanly.
    credentials(paths, expiresAt=1)
    if settings_data:
        (paths.claude / "settings.json").write_text(json.dumps(settings_data), encoding="utf-8")
    for key, value in env_pairs:
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "urllib.request.build_opener", lambda *args: pytest.fail("Network attempted")
    )
    report = fetch_claude(paths.claude)
    serialized = json.dumps(report.to_dict())
    assert report.status == "unavailable"
    assert report.auth_mode == expected_mode
    assert report.source == expected_source
    assert report.error is None
    assert claude_auth_inspection(paths.claude) == (expected_mode, expected_source)
    for secret in (
        "test-ant-sentinel",
        "test-bearer-sentinel",
        "test-gw-sentinel",
        "test-settings-sentinel",
        "secret-test-token",
        "must-never-execute-helper-sentinel",
    ):
        assert secret not in serialized
