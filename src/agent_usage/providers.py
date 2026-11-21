"""Read-only provider adapters; no inference or reset redemption."""

import json
import math
import os
import queue
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeGuard

from . import USER_AGENT, __version__
from .allowances import CREDIT_LABELS, NON_SUBSCRIPTION_LABELS, WINDOW_LABELS
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
        assert self.process.stdout is not None  # Popen is always created with stdout=PIPE.
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message: dict):
        assert self.process.stdin is not None  # Popen is always created with stdin=PIPE.
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
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=1)
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


def _has_value(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _is_truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes")


def _finite_number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def non_subscription_account(
    provider: str, source: str, auth_mode: str, *, plan: str | None = None
) -> AccountReport:
    label = NON_SUBSCRIPTION_LABELS.get(auth_mode, "Non-subscription")
    return AccountReport(
        provider=provider,
        source=source,
        observed_at=utc_now(),
        status="unavailable",
        plan=plan,
        auth_mode=auth_mode,
        warnings=[f"{label} authentication is active; subscription allowances do not apply."],
        error=None,
    )


def codex_config_override(home: Path) -> tuple[str, str] | None:
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    model_provider = config.get("model_provider")
    if _has_value(model_provider):
        selected = model_provider.strip()
        if selected != "openai":
            providers = config.get("model_providers")
            entry = providers.get(selected) if isinstance(providers, dict) else None
            if selected == "gcp":
                return "gcp", "codex-config"
            if selected in ("bedrock", "amazonBedrock") or (
                isinstance(entry, dict) and isinstance(entry.get("aws"), dict)
            ):
                return "amazonBedrock", "codex-config"
            return "customGateway", "codex-config"
    if config.get("forced_login_method") == "api":
        return "apiKey", "codex-config"
    return None


def codex_auth_inspection(home: Path) -> tuple[str | None, str | None]:
    override = codex_config_override(home)
    if override is not None:
        return override
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        auth = None
    if isinstance(auth, dict):
        tokens = auth.get("tokens")
        auth_mode = auth.get("auth_mode")
        if (
            isinstance(tokens, dict)
            and _has_value(tokens.get("access_token"))
            and auth_mode not in ("apikey", "api_key", "apiKey")
        ):
            return "chatgpt", "codex-auth-file"
        if auth_mode in ("apikey", "api_key", "apiKey") or _has_value(auth.get("OPENAI_API_KEY")):
            return "apiKey", "codex-auth-file"
    if _has_value(os.environ.get("CODEX_API_KEY")) or _has_value(os.environ.get("OPENAI_API_KEY")):
        return "apiKey", "codex-env"
    return None, None


def codex_non_subscription_mode(home: Path) -> tuple[str, str] | None:
    mode, source = codex_auth_inspection(home)
    if mode and source and mode not in ("chatgpt", "chatgptAuthTokens"):
        return mode, source
    return None


def _codex_rpc_provider_mode(rpc: CodexRPC) -> str:
    try:
        providers = rpc.call("modelProvider/list", {})
    except ProbeError:
        return "customGateway"
    default_id = providers.get("defaultProviderId")
    for item in providers.get("data") or []:
        if isinstance(item, dict) and item.get("id") == default_id:
            settings = item.get("settings")
            kind = settings.get("type") if isinstance(settings, dict) else None
            if kind == "gcp":
                return "gcp"
            if kind == "bedrock":
                return "amazonBedrock"
            break
    return "customGateway"


def fetch_codex(home: Path, timeout: float = 10) -> AccountReport:
    rpc: CodexRPC | None = None
    try:
        try:
            rpc = CodexRPC(home, timeout)
            rpc.call(
                "initialize",
                {"clientInfo": {"name": "agent_usage", "version": __version__}},
            )
            rpc.send({"method": "initialized"})
            account = rpc.call("account/read", {"refreshToken": False})
        except ProbeError:
            local = codex_non_subscription_mode(home)
            if local is not None:
                return non_subscription_account("codex", local[1], local[0])
            raise
        identity = account.get("account") or {}
        auth_type = identity.get("type")
        if auth_type in ("chatgpt", "chatgptAuthTokens"):
            return normalize_codex(account, rpc.call("account/rateLimits/read"))
        if _has_value(auth_type):
            return non_subscription_account(
                "codex", "codex-app-server", auth_type.strip(), plan=identity.get("planType")
            )
        if account.get("requiresOpenaiAuth") is False:
            return non_subscription_account(
                "codex", "codex-app-server", _codex_rpc_provider_mode(rpc)
            )
        local = codex_non_subscription_mode(home)
        if local is not None:
            return non_subscription_account("codex", local[1], local[0])
        report = normalize_codex(account, {})
        report.status = "unavailable"
        report.error = "ChatGPT subscription login is required for account allowances."
        return report
    finally:
        if rpc is not None:
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
            alias = {"session": "five_hour", "weekly_all": "seven_day"}.get(limit["kind"])
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


def _claude_settings(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _claude_env_value(name: str, settings_env: dict[str, Any]) -> tuple[str, str]:
    env_val = os.environ.get(name)
    if _has_value(env_val):
        return env_val, "claude-env"
    cfg_val = settings_env.get(name)
    if _has_value(cfg_val):
        return cfg_val, "claude-settings"
    return "", ""


def claude_override_mode(home: Path) -> tuple[str, str] | None:
    settings = _claude_settings(home)
    raw_env = settings.get("env")
    settings_env = raw_env if isinstance(raw_env, dict) else {}
    for flag, mode in (
        ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
        ("CLAUDE_CODE_USE_VERTEX", "vertex"),
        ("CLAUDE_CODE_USE_FOUNDRY", "foundry"),
    ):
        value, source = _claude_env_value(flag, settings_env)
        if _is_truthy(value):
            return mode, source
    key_source = ""
    for key_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY"):
        _, source = _claude_env_value(key_var, settings_env)
        if source:
            key_source = source
            break
    if not key_source and _has_value(settings.get("apiKeyHelper")):
        key_source = "claude-settings"
    if key_source:
        base_url, _ = _claude_env_value("ANTHROPIC_BASE_URL", settings_env)
        if base_url:
            host = urllib.parse.urlsplit(base_url.strip()).hostname
            if host and host.lower() != "api.anthropic.com":
                return "customGateway", key_source
        return "api_key", key_source
    return None


def claude_auth_inspection(home: Path) -> tuple[str | None, str | None]:
    override = claude_override_mode(home)
    if override is not None:
        return override
    try:
        creds = json.loads((home / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        creds = None
    if isinstance(creds, dict):
        oauth = creds.get("claudeAiOauth")
        if isinstance(oauth, dict) and _has_value(oauth.get("accessToken")):
            return "claude.ai", "claude-credentials"
        if any(_has_value(creds.get(k)) for k in ("apiKey", "claudeAiApiKey", "primaryApiKey")):
            return "api_key", "claude-credentials"
    return None, None


def claude_non_subscription_mode(home: Path) -> tuple[str, str] | None:
    mode, source = claude_auth_inspection(home)
    if mode and source and mode != "claude.ai":
        return mode, source
    return None


def fetch_claude(home: Path, timeout: float = 10) -> AccountReport:
    non_sub = claude_non_subscription_mode(home)
    if non_sub is not None:
        return non_subscription_account("claude", non_sub[1], non_sub[0])
    try:
        auth = json.loads((home / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        token = auth["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("Missing access token")
        if auth.get("expiresAt") is not None and auth["expiresAt"] <= time.time() * 1000:
            raise ProbeError("Claude credentials expired. Refresh login in Claude Code.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProbeError(
            "Claude OAuth credentials unavailable. Log in with Claude Code or configure an API "
            "key. OS keychain credentials are not supported in this version."
        ) from exc
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
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


def _json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def antigravity_override_mode(home: Path) -> tuple[str, str] | None:
    for env_flag in ("AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI"):
        if _is_truthy(os.environ.get(env_flag)):
            return "gcp", "antigravity-env"
    settings = _json_dict(home / "settings.json")
    raw_env = settings.get("env")
    settings_env = raw_env if isinstance(raw_env, dict) else {}
    for env_flag in ("AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI"):
        if _is_truthy(settings_env.get(env_flag)):
            return "gcp", "antigravity-settings"
    raw_auth = settings.get("auth")
    auth_cfg = raw_auth if isinstance(raw_auth, dict) else {}
    if auth_cfg.get("type") == "agent-platform":
        return "gcp", "antigravity-settings"
    gcp_cfg = settings.get("gcp")
    if isinstance(gcp_cfg, dict) and (
        _has_value(gcp_cfg.get("project")) or _has_value(gcp_cfg.get("projectId"))
    ):
        return "gcp", "antigravity-settings"
    if settings.get("modelProvider") == "gemini" or auth_cfg.get("type") == "gemini-api-key":
        env_base = os.environ.get("GOOGLE_GEMINI_BASE_URL")
        cfg_base = settings_env.get("GOOGLE_GEMINI_BASE_URL")
        base_url = env_base if _has_value(env_base) else cfg_base if _has_value(cfg_base) else ""
        if base_url:
            host = urllib.parse.urlsplit(base_url.strip()).hostname
            if host and host.lower() != "generativelanguage.googleapis.com":
                source = "antigravity-env" if _has_value(env_base) else "antigravity-settings"
                return "customGateway", source
        return "apiKey", "antigravity-settings"
    config_candidates = [home / "config.json"]
    if home.name.startswith("antigravity"):
        config_candidates.append(home.parent / "config" / "config.json")
    for cfg_path in config_candidates:
        user_settings = _json_dict(cfg_path).get("userSettings")
        if isinstance(user_settings, dict) and user_settings.get("enableAdc") is True:
            return "gcp", "antigravity-config"
    return None


def antigravity_auth_inspection(home: Path) -> tuple[str | None, str | None]:
    override = antigravity_override_mode(home)
    if override is not None:
        return override
    settings = _json_dict(home / "settings.json")
    raw_auth = settings.get("auth")
    if isinstance(raw_auth, dict) and raw_auth.get("type") == "oauth-personal":
        return "google", "antigravity-settings"
    return None, None


def antigravity_non_subscription_mode(home: Path) -> tuple[str, str] | None:
    mode, source = antigravity_auth_inspection(home)
    if mode and source and mode != "google":
        return mode, source
    return None


def _run_agy_command(command_name: str, home: Path, timeout: float) -> dict[str, Any]:
    # Requires agy >= 1.5.14 so --print handles /usage and /credits without a model turn.
    cmd_args = ["agy"]
    settings_file = home / "settings.json"
    if settings_file.is_file():
        cmd_args.extend(["--settings", str(settings_file)])
    cmd_args.extend(["--print", f"/{command_name}", "--output-format", "json"])
    try:
        response = subprocess.run(
            cmd_args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise ProbeError("Cannot start agy; install Antigravity CLI and check PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"Antigravity /{command_name} timed out.") from exc
    try:
        payload = json.loads(response.stdout)
    except ValueError:
        payload = None
    if (
        response.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("status") != "SUCCESS"
    ):
        err_text = payload.get("error") if isinstance(payload, dict) else ""
        combined = f"{err_text}\n{response.stderr}".lower()
        if any(
            phrase in combined
            for phrase in (
                "authentication required",
                "not authenticated",
                "sign in",
                "login required",
                "unauthenticated",
            )
        ):
            raise ProbeError("Antigravity authentication required. Sign in with agy.")
        if "rate limit" in combined or "http 429" in combined or " 429" in combined:
            raise ProbeError(f"Antigravity /{command_name} rate limited; retry later.")
        raise ProbeError(f"Antigravity /{command_name} failed. Check agy login and CLI version.")
    cmd = payload.get("command")
    usage = payload.get("usage")
    if (
        not isinstance(cmd, dict)
        or cmd.get("name") != command_name
        or not isinstance(cmd.get("data"), dict)
        or payload.get("num_turns", 0) != 0
        or (isinstance(usage, dict) and usage.get("total_tokens", 0) != 0)
    ):
        raise ProbeError(f"Antigravity /{command_name} returned an unsupported response.")
    return cmd["data"]


def normalize_antigravity(
    usage_data: dict[str, Any], credits_data: dict[str, Any] | None = None
) -> AccountReport:
    raw_plan = usage_data.get("plan")
    report = AccountReport(
        provider="antigravity",
        source="agy-cli",
        observed_at=utc_now(),
        plan=raw_plan.strip() if _has_value(raw_plan) else None,
        auth_mode="google",
    )
    groups = usage_data.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Invalid Antigravity quota groups")
    for group in groups:
        if not isinstance(group, dict):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, str) or bucket_id not in WINDOW_LABELS["antigravity"]:
                continue
            rem = bucket.get("remaining_fraction")
            if rem is None:
                used_pct: float | None = None
            elif _finite_number(rem) and 0 <= rem <= 1:
                used_pct = round((1.0 - float(rem)) * 100, 2)
            else:
                raise ValueError("Invalid remaining_fraction")
            win_name = str(bucket.get("window") or bucket_id)
            duration = 300 if win_name == "5h" else 10080 if win_name == "weekly" else None
            report.windows.append(
                QuotaWindow(
                    bucket=bucket_id,
                    window=win_name,
                    used_percent=used_pct,
                    resets_at=iso_time(bucket.get("reset_time")),
                    duration_minutes=duration,
                    label=WINDOW_LABELS["antigravity"][bucket_id],
                )
            )
    if credits_data is not None:
        rem_credits = credits_data.get("remaining_credits")
        if "remaining_credits" not in credits_data:
            report.warnings.append("AI credits status unavailable.")
        elif rem_credits is not None:
            if not _finite_number(rem_credits) or rem_credits < 0:
                report.warnings.append("AI credits status unavailable.")
            else:
                report.credits.append(
                    CreditBalance(
                        bucket="ai_credits",
                        balance=str(rem_credits),
                        label=CREDIT_LABELS["antigravity"]["ai_credits"],
                    )
                )
    return report


def fetch_antigravity(home: Path, timeout: float = 10) -> AccountReport:
    non_sub = antigravity_non_subscription_mode(home)
    if non_sub is not None:
        return non_subscription_account("antigravity", non_sub[1], non_sub[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        usage_future = pool.submit(_run_agy_command, "usage", home, timeout)
        credits_future = pool.submit(_run_agy_command, "credits", home, timeout)
        usage_data = usage_future.result()
        credits_data: dict[str, Any] | None = None
        credits_warning: str | None = None
        try:
            credits_data = credits_future.result()
        except ProbeError:
            credits_warning = "AI credits status unavailable."
    report = normalize_antigravity(usage_data, credits_data)
    if credits_warning and credits_warning not in report.warnings:
        report.warnings.append(credits_warning)
    default_cli_home = Path("~/.gemini/antigravity-cli").expanduser().resolve()
    if home.resolve() != default_cli_home:
        report.warnings.append(
            "Antigravity OAuth sessions use the OS keyring; "
            "--antigravity-home only overrides local settings and logs."
        )
    return report
