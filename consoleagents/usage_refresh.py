"""Read account quotas without starting model turns.

Codex uses its documented app-server account/rateLimits/read RPC. Antigravity
uses the built-in `-p /usage` command (command.data, zero turns). Claude uses
the OAuth endpoint behind its /usage panel; that endpoint is not a public API
contract, so errors leave the last snapshot intact. Never return raw auth or
provider error bodies to the caller.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import WINDOWS
from .limits import is_auth_error
from .providers import _kill_tree


class RefreshError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class QuotaWindow:
    window: str
    utilization: float | None
    resets_at: float | None
    bucket: str
    scope: str = "account"
    applicable: bool = True
    disabled: bool = False


@dataclass
class RefreshResult:
    agent: str
    source: str
    status: str
    message: str = ""
    observed_at: float = 0
    windows: list[QuotaWindow] = field(default_factory=list)

    @property
    def snapshots(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for w in self.windows:
            if w.applicable and not w.disabled and w.window in WINDOWS and w.utilization is not None:
                if w.window not in out or w.utilization > out[w.window]["utilization"]:
                    out[w.window] = {"utilization": w.utilization, "resets_at": w.resets_at,
                                     "observed_at": self.observed_at, "source": self.source}
        return out


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _timestamp(value) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric if numeric > 0 else None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        except ValueError:
            pass
    return None


def parse_claude(data: dict, model: str | None = None) -> list[QuotaWindow]:
    out = []
    for key, value in data.items():
        if not isinstance(value, dict) or key not in {"five_hour", "seven_day"} and not key.startswith("seven_day_"):
            continue
        used = _number(value.get("utilization"))
        if used is None or used < 0 or used > 100:
            continue
        suffix = key.removeprefix("seven_day_") if key.startswith("seven_day_") else None
        out.append(QuotaWindow("five_hour" if key == "five_hour" else "weekly", used / 100,
                               _timestamp(value.get("resets_at")), key,
                               "model" if suffix else "account",
                               not suffix or bool(model and suffix in model.lower())))
    return out


def parse_codex(data: dict) -> list[QuotaWindow]:
    # Model-specific/other-product limit IDs must not replace the Codex account pool.
    pools = data.get("rateLimitsByLimitId") or {}
    limits = pools.get("codex") or data.get("rateLimits") or {}
    out = []
    selected_id = limits.get("limitId") or "codex"
    all_pools = {selected_id: limits, **{k: v for k, v in pools.items() if k != selected_id}}
    for pool_id, pool in all_pools.items():
        for key in ("primary", "secondary"):
            item = pool.get(key)
            if not isinstance(item, dict):
                continue
            used, minutes = _number(item.get("usedPercent")), _number(item.get("windowDurationMins"))
            if used is None or not 0 <= used <= 100 or not minutes or minutes <= 0:
                continue
            window = next((k for k, v in WINDOWS.items() if v == minutes), f"{minutes:g}m")
            out.append(QuotaWindow(window, used / 100, _timestamp(item.get("resetsAt")),
                                   key, pool_id, pool_id == "codex"))
    return out


def parse_antigravity(data: dict, model: str | None) -> list[QuotaWindow]:
    if data.get("status") not in (None, "SUCCESS"):
        auth = is_auth_error(str(data))
        raise RefreshError("auth" if auth else "error", "Antigravity could not read quotas; check login and retry.")
    command = data.get("command") or {}
    if command.get("name") not in ("usage", "quota") or data.get("num_turns", 0) != 0:
        raise RefreshError("unsupported", "CLI did not return a built-in quota response; update Antigravity.")
    groups = (command.get("data") or {}).get("groups") or []
    model = (model or "").lower()
    # Pro has a separate Argon pool; Ultra may expose Argon in the Gemini pool.
    argon_buckets = {b.get("id") for g in groups for b in g.get("buckets", []) if "argon" in b.get("id", "")}
    if "argon" in model and argon_buckets:
        selected = argon_buckets
    elif "gemini" in model:
        selected = {"gemini-weekly", "gemini-5h"}
    elif "claude" in model or "gpt" in model:
        selected = {"3p-weekly", "3p-5h"}
    else:
        selected = set()  # unknown default model: retain raw quotas without guessing a pool
    out = []
    for group in groups:
        for b in group.get("buckets", []):
            bucket = b.get("id", "unknown")
            disabled = b.get("disabled") is True
            remaining = _number(b.get("remaining_fraction"))
            if not disabled and (remaining is None or not 0 <= remaining <= 1):
                continue
            window = {"5h": "five_hour", "weekly": "weekly"}.get(b.get("window"), b.get("window") or "unknown")
            out.append(QuotaWindow(window, None if disabled else 1 - remaining,
                                   _timestamp(b.get("reset_time")), bucket, group.get("name", "group"),
                                   bucket in selected, disabled))
    return out


def _popen(args: list[str], cwd: Path):
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
          if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **kw)


def _finish(proc):
    if proc.poll() is None:
        _kill_tree(proc)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    for stream in (proc.stdin, proc.stdout):
        if stream:
            stream.close()


def _run_text(args: list[str], cwd: Path, timeout: float) -> str:
    proc = _popen(args, cwd)
    try:
        output, _ = proc.communicate(timeout=timeout)
        if proc.returncode:
            # Do not echo output: a CLI can include account details or tokens in errors.
            raise RefreshError("auth" if is_auth_error(output) else "error",
                               "Quota command failed; check the CLI login and version.")
        if len(output) > 2_000_000:
            raise RefreshError("error", "Quota response exceeds the size limit.")
        return output
    finally:
        _finish(proc)


def _run_json(args: list[str], cwd: Path, timeout: float) -> dict:
    data = json.loads(_run_text(args, cwd, timeout))
    if not isinstance(data, dict):
        raise ValueError()
    return data


def _antigravity_fetch(exe: str, cwd: Path, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    version = _run_text([exe, "--version"], cwd, timeout)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
    if not match or tuple(map(int, match.groups())) < (1, 2, 15):
        raise RefreshError("unsupported", "Update Antigravity to 1.2.15+ for built-in headless quota checks.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return _run_json([exe, "-p", "/usage", "--output-format", "json"], cwd, remaining)


def _codex_rpc(exe: str, cwd: Path, timeout: float) -> dict:
    proc = _popen([exe, "app-server"], cwd)
    messages: queue.Queue = queue.Queue()
    deadline = time.monotonic() + timeout

    def read():
        try:
            for line in proc.stdout:
                if len(line) > 2_000_000:
                    break
                try:
                    messages.put_nowait(json.loads(line))
                except ValueError:
                    continue
        finally:
            messages.put_nowait(None)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()

    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def receive(request_id):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            try:
                response = messages.get(timeout=left)
            except queue.Empty:
                raise TimeoutError() from None
            if response is None:
                raise RefreshError("error", "Codex app-server closed before returning quota data.")
            if not isinstance(response, dict) or response.get("id") != request_id:
                continue
            if "error" in response:
                error = str(response["error"]).lower()
                auth = any(s in error for s in ("auth", "login", "sign in", "unauthorized"))
                raise RefreshError("auth" if auth else "unsupported",
                                   "Codex cannot read subscription quotas; check ChatGPT login and CLI version.")
            return response.get("result") or {}

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "autoagents_usage", "version": "1.0"}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        return receive(2)
    finally:
        _finish(proc)
        thread.join(timeout=1)


def _claude_credentials(cfg: dict, exe: str, cwd: Path, timeout: float) -> dict:
    if cfg.get("usage_credentials_file"):
        return json.loads(Path(cfg["usage_credentials_file"]).expanduser().read_text())
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return json.loads((Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json").read_text())
    # Recognize the wrapper installed for this phone. Read its Debian user's
    # credentials through proot, with no shell interpolation or token in argv.
    with open(exe, "rb") as f:
        wrapper = f.read(4096).decode("utf-8", errors="replace")
    match = re.search(r'exec proot-distro login --user ([\w-]+) --work-dir "\$PWD" ([\w.-]+) -- (/home/[\w.-]+)/\.local/bin/claude', wrapper)
    if match:
        user, distro, home = match.groups()
        return _run_json(["proot-distro", "login", "--user", user, distro, "--", "cat",
                          home + "/.claude/.credentials.json"], cwd, timeout)
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.is_file():
        raise RefreshError("unsupported", "Claude OAuth credentials unavailable (keychain-only logins are not supported).")
    return json.loads(path.read_text())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _claude_fetch(cfg: dict, exe: str, cwd: Path, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    credentials = _claude_credentials(cfg, exe, cwd, timeout)
    oauth = credentials.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RefreshError("auth", "Claude subscription login is required; API keys do not expose plan quotas.")
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=remaining) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise RefreshError("error", "Quota response exceeds the size limit.")
    return json.loads(body)


def _agy_model(cfg: dict) -> str | None:
    if cfg.get("model"):
        return cfg["model"]
    try:
        settings = json.loads((Path.home() / ".gemini" / "antigravity-cli" / "settings.json").read_text())
        model = settings.get("model")
        return model if isinstance(model, str) else None
    except (OSError, ValueError):
        return None


def refresh_agent(name: str, cfg: dict, store, cwd: Path, timeout: float = 20) -> RefreshResult:
    """Refresh one configured account/model; preserve snapshots on any failure.

    No thread/start or turn/start RPCs, arbitrary prompts, token refresh grants,
    or paid requests are issued. Returned messages never include provider bodies.
    """
    source = cfg.get("type", name)
    result = RefreshResult(name, source, "error", observed_at=time.time())
    try:
        timeout = max(1.0, min(float(timeout), 60.0))
        deadline = time.monotonic() + timeout
        exe = shutil.which(cfg.get("command") or name)
        if not exe:
            raise RefreshError("missing", "Agent executable was not found on PATH.")
        if source == "codex":
            result.windows = parse_codex(_codex_rpc(exe, cwd, timeout))
        elif source == "claude":
            result.windows = parse_claude(_claude_fetch(cfg, exe, cwd, max(1, deadline - time.monotonic())), cfg.get("model"))
        elif source == "antigravity":
            result.windows = parse_antigravity(_antigravity_fetch(exe, cwd, timeout), _agy_model(cfg))
        else:
            raise RefreshError("unsupported", "This provider has no account quota adapter.")
        result.observed_at = time.time()
        if not result.snapshots:
            raise RefreshError("unsupported", "No supported quota windows for this model; raw quota data retained.")
        result.status = "ok"
        if any(w.applicable and w.window not in WINDOWS for w in result.windows):
            result.status, result.message = "partial", "Additional quota periods are retained without mapping them to 5-hour/weekly limits."
    except RefreshError as exc:
        result.status, result.message = exc.status, str(exc)
    except (TimeoutError, subprocess.TimeoutExpired):
        result.status, result.message = "timeout", "Account usage refresh timed out; previous data retained."
    except urllib.error.HTTPError as exc:
        result.status = "auth" if exc.code in (401, 403) else "error"
        result.message = f"Claude usage endpoint returned HTTP {exc.code}; check login or retry later."
    except urllib.error.URLError as exc:
        result.status = "timeout" if isinstance(exc.reason, TimeoutError) else "error"
        result.message = "Account usage request failed; previous data retained."
    except (OSError, ValueError, TypeError, AttributeError):
        result.status, result.message = "error", "Account usage could not be read; check login, network, and CLI version."
    store.record_refresh(name, result)
    return result
