"""Read-only provider adapters; no inference or reset redemption."""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .allowances import CREDIT_LABELS, WINDOW_LABELS
from .models import (
    AccountReport,
    CreditBalance,
    ProbeError,
    QuotaWindow,
    ResetAllowance,
    iso_time,
    utc_now,
)


class CodexRPC:
    def __init__(self, home: Path, timeout: float = 10):
        self.timeout = timeout
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        env = {**os.environ, "CODEX_HOME": str(home)}
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
            )
        except OSError as exc:
            raise ProbeError("Cannot start codex; install it and check PATH.") from exc
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message: dict):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ProbeError("Codex app-server closed its connection.") from exc

    def call(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        request: dict[str, Any] = {"id": self.sequence, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                response = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ProbeError(f"Codex {method} timed out.") from exc
            if response is None:
                raise ProbeError("Codex app-server exited before responding.")
            if not isinstance(response, dict):
                continue
            if response.get("method") and "id" in response:
                self.send(
                    {"id": response["id"], "error": {"code": -32601, "message": "Read-only client"}}
                )
                continue
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                code = response["error"].get("code", "unknown")
                raise ProbeError(
                    f"Codex {method} failed (RPC {code}). Check login and CLI version."
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise ProbeError(f"Codex {method} returned an unsupported response.")
            return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()


def normalize_codex(account: dict, data: dict) -> AccountReport:
    identity = account.get("account") or {}
    report = AccountReport(
        provider="codex",
        source="codex-app-server",
        observed_at=utc_now(),
        plan=identity.get("planType"),
        auth_mode=identity.get("type"),
    )
    buckets = data.get("rateLimitsByLimitId")
    if not buckets and data.get("rateLimits"):
        one = data["rateLimits"]
        buckets = {one.get("limitId") or "codex": one}
    for key, bucket in sorted(
        (buckets or {}).items(), key=lambda item: (item[0] != "codex", item[0])
    ):
        if key not in WINDOW_LABELS["codex"]:
            continue
        report.plan = report.plan or bucket.get("planType")
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if window:
                report.windows.append(
                    QuotaWindow(
                        bucket=key,
                        window=name,
                        used_percent=window.get("usedPercent"),
                        resets_at=iso_time(window.get("resetsAt")),
                        duration_minutes=window.get("windowDurationMins"),
                        label=WINDOW_LABELS["codex"][key],
                    )
                )
        credits = bucket.get("credits")
        if credits is not None:
            report.credits.append(
                CreditBalance(
                    bucket=key,
                    balance=str(credits["balance"]) if credits.get("balance") is not None else None,
                    has_credits=credits.get("hasCredits"),
                    unlimited=credits.get("unlimited"),
                    expires_at=iso_time(credits.get("expiresAt")),
                    label=CREDIT_LABELS["codex"][key],
                )
            )
    resets = data.get("rateLimitResetCredits")
    if resets is not None:
        details = resets.get("credits")
        report.resets = ResetAllowance(
            available_count=resets.get("availableCount"),
            details=None
            if details is None
            else [
                {
                    "scope": row.get("resetType"),
                    "status": row.get("status"),
                    "expires_at": iso_time(row.get("expiresAt")),
                }
                for row in details
            ],
        )
    return report


def fetch_codex(home: Path, timeout: float = 10) -> AccountReport:
    rpc = CodexRPC(home, timeout)
    try:
        rpc.call(
            "initialize",
            {"clientInfo": {"name": "agent_usage", "version": "0.1.0"}},
        )
        rpc.send({"method": "initialized"})
        account = rpc.call("account/read", {"refreshToken": False})
        identity = account.get("account") or {}
        if identity.get("type") not in ("chatgpt", "chatgptAuthTokens"):
            report = normalize_codex(account, {})
            report.status = "unavailable"
            report.error = "ChatGPT subscription login is required for account allowances."
            return report
        return normalize_codex(account, rpc.call("account/rateLimits/read"))
    finally:
        rpc.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def normalize_claude(data: dict, plan: str | None) -> AccountReport:
    report = AccountReport(
        provider="claude",
        source="claude-oauth-usage-internal",
        observed_at=utc_now(),
        plan=plan,
        auth_mode="claude.ai",
        warnings=["Uses a client-internal endpoint; compatibility is not guaranteed."],
    )
    for name, value in data.items():
        if name not in WINDOW_LABELS["claude"]:
            continue
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        if value.get("utilization") is None and value.get("resets_at") is None:
            continue
        minutes = 300 if name == "five_hour" else 10080 if name.startswith("seven_day") else None
        report.windows.append(
            QuotaWindow(
                name,
                name,
                value.get("utilization"),
                iso_time(value.get("resets_at")),
                minutes,
                label=WINDOW_LABELS["claude"][name],
            )
        )
    extra = data.get("extra_usage")
    if extra is not None:
        # Keep provider units until their scaling is independently verified.
        report.credits.append(
            CreditBalance(
                bucket="extra_usage",
                enabled=extra.get("is_enabled"),
                unit=f"{extra['currency']}_provider_units_unverified"
                if extra.get("currency")
                else "provider_units_unverified",
                limit=str(extra["monthly_limit"])
                if extra.get("monthly_limit") is not None
                else None,
                used=str(extra["used_credits"]) if extra.get("used_credits") is not None else None,
                label=CREDIT_LABELS["claude"]["extra_usage"],
            )
        )
    spend = data.get("spend")
    if isinstance(spend, dict) and spend.get("balance") is not None:
        report.credits.append(
            CreditBalance(
                bucket="spend",
                balance=str(spend["balance"]),
                label=CREDIT_LABELS["claude"]["spend"],
            )
        )
    for limit in data.get("limits") or []:
        if isinstance(limit, dict) and limit.get("kind") in WINDOW_LABELS["claude"]:
            alias = {"session": "five_hour", "weekly_all": "seven_day"}.get(limit.get("kind"))
            reset_at = iso_time(limit.get("resets_at"))
            if alias and any(
                window.bucket == alias
                and window.used_percent == limit.get("percent")
                and window.resets_at == reset_at
                for window in report.windows
            ):
                continue
            report.windows.append(
                QuotaWindow(
                    bucket=str(limit.get("kind", "unknown")),
                    window=str(limit.get("group", "unknown")),
                    used_percent=limit.get("percent"),
                    resets_at=reset_at,
                    label=WINDOW_LABELS["claude"][limit["kind"]],
                )
            )
    return report


def fetch_claude(home: Path, timeout: float = 10) -> AccountReport:
    try:
        auth = json.loads((home / ".credentials.json").read_text())["claudeAiOauth"]
        token = auth["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("Missing access token")
        if auth.get("expiresAt") is not None and auth["expiresAt"] <= time.time() * 1000:
            raise ProbeError("Claude credentials expired. Refresh login in Claude Code.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProbeError(
            "Claude OAuth credentials unavailable. Log in with Claude Code. "
            "OS keychain credentials are not supported in this version."
        ) from exc
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "agent-usage/0.1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("Invalid response")
        return normalize_claude(data, auth.get("subscriptionType"))
    except urllib.error.HTTPError as exc:
        retry = " Rate limited; retry later." if exc.code == 429 else " Check Claude Code login."
        raise ProbeError(f"Claude usage request failed (HTTP {exc.code}).{retry}") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProbeError(
            "Claude usage request failed or returned an unsupported response."
        ) from exc
