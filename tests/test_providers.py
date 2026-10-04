import io
import json
import queue
import time
import urllib.error

import pytest

from agent_usage.models import ProbeError
from agent_usage.providers import CodexRPC, NoRedirect, fetch_claude


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
