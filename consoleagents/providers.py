"""Adapters that run each AI CLI headless, stream its progress and normalise the result.

All prompts go through stdin: Windows .cmd shims cap the command line at ~8 KB and
mangle special characters, and handover prompts are much larger than that.
"""
from __future__ import annotations

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .limits import is_auth_error, is_limit_error, parse_reset
from .procs import describe
from .usage import window_for_minutes

ProgressFn = Callable[[str], None]


@dataclass
class Snapshot:
    window: str
    utilization: float
    resets_at: float | None


@dataclass
class RunResult:
    agent: str
    final_text: str = ""  # the agent's last message (holds the report block)
    log: list[str] = field(default_factory=list)  # everything it said and did, for the transcript
    tokens: int = 0  # weighted tokens (cache reads count 10%)
    exit_code: int | None = None
    error_kind: str | None = None  # limit | auth | missing | timeout | stalled | loop | crash | interrupted
    error: str = ""
    limit_reset: float | None = None
    limit_window: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    session_id: str | None = None
    cost_usd: float | None = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error_kind is None


def _short(value, limit: int = 110) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"



def _tool_summary(name: str, args: dict | None) -> str:
    args = args or {}
    lowered = {str(k).lower(): v for k, v in args.items()}
    for key in ("command", "commandline", "cmd", "file_path", "path", "absolute_path", "absolutepath",
                "targetfile", "pattern", "url", "query", "description", "prompt"):
        if lowered.get(key):
            value = describe(str(lowered[key]), 90) if key in ("command", "commandline", "cmd") else _short(lowered[key], 90)
            return f"{name}: {value}"
    return name


def _tool_display(summary: str) -> str:
    """Compact UI labels; transcripts retain tool identifiers for handovers."""
    name, separator, argument = summary.partition(": ")
    icon = {"read_file": "📄👁", "run_command": ">_"}.get(name)
    if icon:
        return icon + (" " + argument if separator else "")
    return "🔧 " + summary


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(proc.pid, 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class Provider:
    """Base adapter. Subclasses build the command line and interpret output lines."""

    supports_resume = True  # can continue an earlier conversation by its session id

    def __init__(self, name: str, cfg: dict, run_cfg: dict):
        self.name = name
        self.cfg = cfg
        self.run_cfg = run_cfg
        self.permission = run_cfg.get("permission", "full")

    # -- to override -----------------------------------------------------------
    def build_args(self, exe: str, cwd: Path, resume: str | None = None) -> list[str]:
        """The command line. With `resume`, continue that earlier conversation instead of starting one."""
        raise NotImplementedError

    def env(self) -> dict:
        return {}

    def stdin_payload(self, prompt: str) -> str:
        return prompt

    def preflight(self) -> str | None:
        """A reason this agent cannot run at all right now (checked before spawning), else None."""
        return None

    def handle_line(self, line: str, res: RunResult, emit: ProgressFn) -> None:
        res.log.append(line)
        emit(_short(line))

    def finalize(self, res: RunResult) -> None:
        pass

    # -- shared ------------------------------------------------------------------
    def executable(self) -> str | None:
        cmd = self.cfg.get("command") or self.name
        return shutil.which(cmd)

    def run(self, prompt: str, cwd: Path, on_progress: ProgressFn | None = None,
            timeout_s: float | None = None, resume: str | None = None, on_procs=None) -> RunResult:
        """on_procs(list of procs.Proc, agent CLI first): called every few seconds with its process tree."""
        emit = on_progress or (lambda _msg: None)
        res = RunResult(agent=self.name)
        exe = self.executable()
        if not exe:
            res.error_kind, res.error = "missing", f"'{self.cfg.get('command')}' was not found on PATH"
            return res

        problem = self.preflight()
        if problem:
            res.error_kind, res.error = "auth", problem
            return res

        resume = resume if self.supports_resume else None
        args = self.build_args(exe, cwd, resume) + list(self.cfg.get("extra_args") or [])
        env = {**os.environ, **self.env()}
        start = time.time()
        popen_kw: dict = {}
        if os.name != "nt":
            popen_kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1, **popen_kw,
            )
        except OSError as exc:
            res.error_kind, res.error = "missing", f"cannot start {exe}: {exc}"
            return res

        stderr_lines: list[str] = []
        lines: queue.Queue = queue.Queue()
        eof = object()

        def feed_stdin():
            try:
                proc.stdin.write(self.stdin_payload(prompt))
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        def read_stdout():
            try:
                for raw in proc.stdout:
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            lines.put(eof)

        def read_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip("\n"))
            except (OSError, ValueError):
                pass

        threads = [threading.Thread(target=f, daemon=True) for f in (feed_stdin, read_stdout, read_stderr)]
        for t in threads:
            t.start()

        timed_out = threading.Event()
        stall_s = float(self.run_cfg.get("stall_timeout_min", 20)) * 60
        loop_limit = int(self.run_cfg.get("loop_repeat_limit", 8))
        recent_tools: list[str] = []
        last_output = time.time()
        exited_at: float | None = None
        stop: tuple[str, str] | None = None  # (error kind, message) when we end the run ourselves

        def take(raw: str) -> None:
            nonlocal stop
            line = raw.rstrip("\n")
            if not line.strip():
                return
            before = len(res.log)
            try:
                self.handle_line(line, res, emit)
            except Exception as exc:  # a bad line must never kill the relay
                res.log.append(line)
                emit(f"(unparsed output: {exc})")
            for entry in res.log[before:]:
                if entry.startswith("[tool]"):
                    recent_tools.append(entry)
                    del recent_tools[:-loop_limit]
                    if loop_limit and len(recent_tools) == loop_limit and len(set(recent_tools)) == 1:
                        stop = ("loop", f"it repeated the same action {loop_limit} times in a row: {entry[7:150]}")

        last_sample = 0.0

        family: set[int] = {proc.pid}  # the agent and every process seen under it

        def sample() -> None:
            if not on_procs:
                return
            try:
                from .procs import orphans_of, table, tree
                tab = table()
                found = tree(proc.pid, tab) if proc.poll() is None else []
                # Children whose parent already exited (Windows keeps the dead parent's pid).
                known = {p.pid for p in found}
                found += [p for p in orphans_of(family | known, start, tab) if p.pid not in known]
                if found:
                    family.update(p.pid for p in found)
                    on_procs(found, proc.pid)
            except Exception:
                pass  # watching processes must never break a turn

        try:
            while True:
                try:
                    item = lines.get(timeout=0.5)
                except queue.Empty:
                    item = None
                now = time.time()
                if now - last_sample > 3 and proc.poll() is None:
                    last_sample = now
                    sample()
                if item is eof:
                    break
                if item is not None:
                    last_output = now
                    take(item)
                    if stop:
                        _kill_tree(proc)
                        break
                if proc.poll() is not None:
                    # The agent has exited. Its output pipe may stay open because a process it started
                    # (a Gradle daemon, adb server, dev server...) inherited it: never wait for that.
                    exited_at = exited_at or now
                    if (item is None and now - exited_at > 3) or now - exited_at > 15:
                        break
                elif timeout_s and now - start > timeout_s:
                    timed_out.set()
                    _kill_tree(proc)
                    break
                elif stall_s and now - last_output > stall_s:
                    stop = ("stalled", f"no output for {stall_s / 60:.0f} min (hung, or waiting for input)")
                    _kill_tree(proc)
                    break
            sample()  # once more after the agent ended: catches helpers it started just before exiting
            while True:  # whatever is already queued
                try:
                    item = lines.get_nowait()
                except queue.Empty:
                    break
                if item is not eof:
                    take(item)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        except KeyboardInterrupt:
            # Return what we have: the partial output still goes into the transcript.
            _kill_tree(proc)
            res.error_kind, res.error = "interrupted", "stopped by user (Ctrl+C)"
        finally:
            for t in threads:
                t.join(timeout=1)

        if stop and res.error_kind is None:
            res.error_kind, res.error = stop
        res.exit_code = proc.poll()
        if resume and not res.session_id:
            res.session_id = resume  # some CLIs do not repeat the id of a resumed conversation
        res.duration_s = time.time() - start
        stderr_text = "\n".join(stderr_lines)
        self.finalize(res)

        if res.error_kind is None:
            if timed_out.is_set():
                res.error_kind, res.error = "timeout", f"no result after {timeout_s / 60:.0f} min"
            elif res.exit_code != 0 or res.error:
                self._classify_failure(res, stderr_text)
        if res.error_kind == "limit" and res.limit_reset is None:
            res.limit_reset = parse_reset(res.error + "\n" + stderr_text, time.time())
        if not res.tokens:
            # No usage numbers from the CLI: estimate ~4 chars per token of traffic.
            res.tokens = (len(prompt) + sum(len(x) for x in res.log)) // 4
        return res

    def _classify_failure(self, res: RunResult, stderr_text: str) -> None:
        tail = "\n".join(res.log[-15:])
        evidence = "\n".join(x for x in (res.error, stderr_text, tail) if x)
        if is_limit_error(evidence):
            res.error_kind = "limit"
        elif is_auth_error(evidence):
            res.error_kind = "auth"
        else:
            res.error_kind = "crash"
        if not res.error:
            lines = [x for x in (stderr_text or tail).splitlines() if x.strip()]
            res.error = _short(" | ".join(lines[-4:]), 220) or f"exit code {res.exit_code}"


# ---------------------------------------------------------------------------------------------
class ClaudeProvider(Provider):
    def build_args(self, exe, cwd, resume=None):
        args = [exe, "-p", "--output-format", "stream-json", "--verbose"]
        if resume:
            args += ["--resume", resume]
        if self.permission == "full":
            args.append("--dangerously-skip-permissions")
        elif self.permission == "edits":
            args += ["--permission-mode", "acceptEdits"]
        else:
            args += ["--permission-mode", "plan"]
        if self.cfg.get("model"):
            args += ["--model", self.cfg["model"]]
        return args

    def handle_line(self, line, res, emit):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            res.log.append(line)
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            res.session_id = ev.get("session_id")
        elif kind == "assistant":
            for part in (ev.get("message") or {}).get("content") or []:
                if part.get("type") == "text" and part.get("text", "").strip():
                    res.log.append(part["text"])
                    res.final_text = part["text"]
                    emit("💬 " + _short(part["text"]))
                elif part.get("type") == "tool_use":
                    summary = _tool_summary(part.get("name", "tool"), part.get("input"))
                    res.log.append(f"[tool] {summary}")
                    emit(_tool_display(summary))
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            for wname, w in (info.get("unifiedWindows") or {}).items():
                mapped = "five_hour" if "five" in wname else "weekly" if "seven" in wname or "week" in wname else None
                if mapped and isinstance(w.get("utilization"), (int, float)):
                    res.snapshots.append(Snapshot(mapped, float(w["utilization"]), w.get("resetsAt")))
            if info.get("status") == "rejected":
                res.error_kind = "limit"
                res.limit_reset = info.get("resetsAt")
                rtype = info.get("rateLimitType") or ""
                res.limit_window = "five_hour" if "five" in rtype else "weekly" if "seven" in rtype else None
                res.error = f"Claude {rtype or 'usage'} limit reached"
        elif kind == "result":
            if ev.get("result"):
                res.final_text = ev["result"]
            res.session_id = ev.get("session_id") or res.session_id
            res.cost_usd = ev.get("total_cost_usd")
            u = ev.get("usage") or {}
            res.tokens = int((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                             + (u.get("cache_creation_input_tokens") or 0)
                             + 0.1 * (u.get("cache_read_input_tokens") or 0))
            if ev.get("is_error") and not res.error_kind:
                res.error = _short(ev.get("result") or ev.get("subtype") or "error", 400)


# ---------------------------------------------------------------------------------------------
class CodexProvider(Provider):
    def build_args(self, exe, cwd, resume=None):
        # `exec resume` has no -C / -s: the working directory comes from Popen, the sandbox from -c.
        args = [exe, "exec", "resume"] if resume else [exe, "exec", "-C", str(cwd)]
        args += ["--json", "--skip-git-repo-check"]
        if self.permission == "full":
            args.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            mode = "workspace-write" if self.permission == "edits" else "read-only"
            args += ["-c", f'sandbox_mode="{mode}"'] if resume else ["-s", mode]
        if self.cfg.get("model"):
            args += ["-m", self.cfg["model"]]
        if resume:
            args.append(resume)
        args.append("-")  # read the prompt from stdin
        return args

    def handle_line(self, line, res, emit):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            res.log.append(line)
            return
        kind = ev.get("type", "")
        item = ev.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            res.session_id = ev.get("thread_id")
        elif kind == "item.started" and itype == "command_execution":
            emit("🔧 " + describe(item.get("command", ""), 100))
        elif kind == "item.completed":
            if itype == "agent_message" and item.get("text"):
                res.log.append(item["text"])
                res.final_text = item["text"]
                emit("💬 " + _short(item["text"]))
            elif itype == "command_execution":
                res.log.append(f"[tool] shell: {_short(item.get('command', ''), 200)} -> exit {item.get('exit_code')}")
            elif itype == "file_change":
                paths = ", ".join(c.get("path", "?") for c in item.get("changes") or [])
                res.log.append(f"[tool] edited: {paths}")
                emit("📝 " + _short(paths))
            elif itype == "error":
                res.log.append(f"[error] {item.get('message')}")
        elif kind == "turn.completed":
            u = ev.get("usage") or {}
            cached = u.get("cached_input_tokens") or 0
            res.tokens += int((u.get("input_tokens") or 0) - cached + 0.1 * cached + (u.get("output_tokens") or 0))
        elif kind in ("turn.failed", "error"):
            msg = (ev.get("error") or {}).get("message") if kind == "turn.failed" else ev.get("message")
            res.error = _short(msg or json.dumps(ev), 400)
            if res.exit_code is None:
                res.exit_code = 1

    def finalize(self, res):
        if res.error and res.exit_code == 0:
            res.exit_code = 1
        self._read_rate_limits(res)

    def _read_rate_limits(self, res):
        """Codex writes real rate-limit percentages into its session rollout file."""
        if not res.session_id:
            return
        home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        files = glob.glob(str(home / "sessions" / "*" / "*" / "*" / f"*{res.session_id}.jsonl"))
        if not files:
            return
        try:
            with open(files[0], encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            limits = (ev.get("payload") or {}).get("rate_limits") or ev.get("rate_limits")
            if not isinstance(limits, dict):
                continue
            now = time.time()
            for key in ("primary", "secondary"):
                w = limits.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                window = window_for_minutes(w.get("window_minutes"))
                resets = w.get("resets_at") or (now + w["resets_in_seconds"] if w.get("resets_in_seconds") else None)
                if window:
                    res.snapshots.append(Snapshot(window, float(w["used_percent"]) / 100.0, resets))
            return


# ---------------------------------------------------------------------------------------------
class GenericProvider(Provider):
    """Any other headless CLI that reads the prompt on stdin and prints plain text.
    Config: {"type": "generic", "command": "mytool", "args": ["mytool", "--headless"]}.
    Optional resume support: "session_regex" (group 1 = the session id in its output) and
    "resume_args" (added to args, "{id}" is replaced by that id)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.supports_resume = bool(self.cfg.get("resume_args") and self.cfg.get("session_regex"))

    def build_args(self, exe, cwd, resume=None):
        args = list(self.cfg.get("args") or [exe])
        if args and args[0] == self.cfg.get("command"):
            args[0] = exe
        if resume:
            args += [str(a).replace("{id}", resume) for a in self.cfg["resume_args"]]
        return args

    def handle_line(self, line, res, emit):
        res.log.append(line)
        emit(_short(line))
        pattern = self.cfg.get("session_regex")
        if pattern and not res.session_id:
            m = re.search(pattern, line)
            if m:
                res.session_id = m.group(1)

    def finalize(self, res):
        res.final_text = "\n".join(res.log[-200:])


# ---------------------------------------------------------------------------------------------
class AntigravityProvider(Provider):
    """Google Antigravity CLI (`agy`): Gemini models on your Google account login.

    `-p -` does not read stdin, so the prompt goes through its NDJSON input mode:
    {"event": "user", "message": {"role": "user", "content": "..."}} with an empty `-p=`."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._texts: dict[int, str] = {}

    def build_args(self, exe, cwd, resume=None):
        args = [exe, "--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands"]
        if resume:
            args += ["--conversation", resume]
        args += {"full": ["--dangerously-skip-permissions"], "edits": ["--mode", "accept-edits"]}.get(
            self.permission, ["--mode", "plan"])
        if self.cfg.get("model"):
            args += ["--model", self.cfg["model"]]
        args.append("-p=")
        return args

    def stdin_payload(self, prompt):
        return json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"

    def preflight(self):
        self._texts = {}
        return None

    def handle_line(self, line, res, emit):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            res.log.append(line)
            return
        kind = ev.get("event")
        if kind == "init":
            res.session_id = ev.get("conversation_id")
        elif kind == "step_update":
            su = ev.get("step_update") or {}
            idx = su.get("step_index", -1)
            stype, state = su.get("step_type"), su.get("state")
            if stype == "agent_response":
                if su.get("text_delta"):
                    self._texts[idx] = self._texts.get(idx, "") + su["text_delta"]
                if state == "DONE" and self._texts.get(idx, "").strip():
                    text = self._texts.pop(idx)
                    res.log.append(text)
                    res.final_text = text
                    emit("💬 " + _short(text))
            elif stype == "tool" and state == "ACTIVE":
                info = su.get("tool_info") or {}
                summary = _tool_summary(info.get("name") or su.get("tool_name") or "tool", info.get("parameters"))
                res.log.append(f"[tool] {summary}")
                emit(_tool_display(summary))
        elif kind == "result":
            r = ev.get("result") or {}
            for idx in sorted(self._texts):  # text that never got a DONE state
                if self._texts[idx].strip():
                    res.log.append(self._texts[idx])
            self._texts = {}
            if r.get("response"):
                res.final_text = r["response"]
            res.session_id = r.get("conversation_id") or res.session_id
            u = r.get("usage") or {}
            res.tokens = int((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                             + 0.1 * (u.get("cache_read_tokens") or 0))
            if r.get("status") not in (None, "SUCCESS") or r.get("error"):
                res.error = _short(r.get("error") or f"status {r.get('status')}", 300)


PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider, "antigravity": AntigravityProvider,
             "generic": GenericProvider}


def make_provider(name: str, cfg: dict, run_cfg: dict) -> Provider:
    cls = PROVIDERS.get(cfg.get("type", name), GenericProvider)
    return cls(name, cfg, run_cfg)
