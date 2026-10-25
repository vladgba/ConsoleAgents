"""Configuration: built-in defaults, overridden by the global file
(~/.autoagents/config.json) and then by the project file (<project>/.autoagents/config.json)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any



def home_dir() -> Path:
    """The global AutoAgents folder (usage, settings, projects, global memory). Read at call time,
    so AUTOAGENTS_HOME can point it elsewhere (tests, a second account)."""
    return Path(os.environ.get("AUTOAGENTS_HOME") or (Path.home() / ".autoagents"))
PROJECT_DIRNAME = ".autoagents"

# Usage windows we track, in minutes. Subscription CLIs use a 5-hour and a weekly window.
WINDOWS: dict[str, int] = {"five_hour": 300, "weekly": 10080}

DEFAULT_CONFIG: dict[str, Any] = {
    # Order matters: it is the tie-break when two agents score the same.
    "agents": {
        "claude": {
            "type": "claude",
            "enabled": True,
            "command": "claude",
            "model": None,
            "strength": 1.0,
            "description": "strong at complex reasoning, architecture, refactoring, careful multi-file changes",
            "extra_args": [],
            # Fallback token budgets, used only while no real usage data is known.
            "budgets": {"five_hour": 4_000_000, "weekly": 40_000_000},
            "default_cooldown_min": 60,
        },
        "codex": {
            "type": "codex",
            "enabled": True,
            "command": "codex",
            "model": None,
            "strength": 1.0,
            "description": "strong at implementation, debugging, writing and running tests",
            "extra_args": [],
            "budgets": {"five_hour": 4_000_000, "weekly": 40_000_000},
            "default_cooldown_min": 60,
        },
        "antigravity": {
            "type": "antigravity",
            "enabled": True,
            "command": "agy",
            "model": None,
            # Runs agy's default fast model (Gemini 3.8 Flash), so it ranks below Claude/Codex
            # unless they are much fuller.
            "strength": 0.7,
            "description": "fast Gemini Flash model through your Google account (Antigravity CLI): large context, "
                           "reading big codebases, docs, simpler edits",
            "extra_args": [],
            # agy does not report its quota, so usage is estimated from tokens (learned on limit hits)
            "budgets": {"five_hour": 3_000_000, "weekly": 30_000_000},
            "default_cooldown_min": 60,
        },
        # Extra models that Antigravity offers on your Google account. They take only "light"
        # steps (well-specified work that needs little history), get a short history in their prompt,
        # and are paused on their own when their quota runs out. Set "active_until": "YYYY-MM-DD"
        # to stop using one after a date (e.g. when a promotion ends).
        "agy-claude": {
            "type": "antigravity",
            "enabled": True,
            "command": "agy",
            "model": "claude-sonnet-5-5-medium",
            "strength": 0.9,
            "levels": ["light"],
            "history_chars": 8000,
            "active_until": None,
            "description": "Claude Sonnet 5.5 via Antigravity: small, clearly specified (light) steps only",
            "extra_args": [],
            "budgets": {"five_hour": 2_000_000, "weekly": 20_000_000},
            "default_cooldown_min": 60,
        },
        "agy-gpt": {
            "type": "antigravity",
            "enabled": True,
            "command": "agy",
            "model": "gpt-oss-120b-medium",
            "strength": 0.6,
            "levels": ["light"],
            "history_chars": 8000,
            "active_until": None,
            "description": "GPT-OSS 120B via Antigravity: routine, clearly specified small (light) edits only",
            "extra_args": [],
            "budgets": {"five_hour": 2_000_000, "weekly": 20_000_000},
            "default_cooldown_min": 60,
        },
    },
    "balancer": {
        # Above soft_limit an agent is only used when nobody healthier is available.
        "soft_limit": 0.80,
        # At or above hard_limit an agent is never started (prevents hitting the wall mid-step).
        "hard_limit": 0.95,
        # Bonus for keeping the current agent (avoids needless context switches).
        "stickiness": 1.15,
        "over_soft_penalty": 0.25,
        # A step another agent delegated to a named agent: that agent gets this bonus (usage still counts).
        # Bonus for the agent the roadmap suggests for a step.
        "hint_bonus": 1.25,
        "delegate_bonus": 1.6,
        # Pacing: look at the 5-hour AND the weekly window together. Quota that is not used before a window
        # resets is lost, so an agent that is behind its pace (little used, reset coming) is preferred, and one
        # that is ahead of its pace (used a lot early in the window) is spared for later.
        # How many chats may use one agent at the same time (0 = no limit). 1 = an agent that is running a
        # turn in another chat is skipped (or waited for), so one account is never driven twice at once.
        # Per agent: agents.<name>.max_parallel.
        "max_parallel": 1,
        "pacing": True,
        "pace_weight_weekly": 0.6,
        "pace_weight_five_hour": 0.3,
        "pace_min": 0.5,  # the pacing factor stays within [pace_min, pace_max]
        "pace_max": 1.6,
    },
    "run": {
        "max_turns": 40,
        "turn_timeout_min": 45,
        # An agent that prints nothing for this long is stopped (hung, or waiting for input that never comes).
        "stall_timeout_min": 20,
        # The same tool call this many times in a row = a loop: the turn is stopped and handed over.
        "loop_repeat_limit": 8,
        # Processes an agent started that still run after its turn (Gradle daemon, adb, dev servers):
        # keep (listed by `aa ps`, killed with `aa kill`) or kill (stopped right after every turn).
        "leftover_processes": "keep",
        # Shut down when finished (/swf on, /qqq off): what happens when the last working chat ends.
        "finish_lock_android": True,  # turn off the screen of Android devices connected through adb
        "finish_shutdown": True,  # power off this PC
        "shutdown_grace_s": 60,  # seconds before power-off, to cancel with /qqq or `shutdown /a`
        # full  = agents may edit files and run commands without asking (needed for autonomy)
        # edits = file edits only (commands may be refused by the CLI)
        "permission": "full",
        # How much recent conversation is pasted into a handover prompt (full history stays on disk).
        "history_chars": 40000,  # chars of earlier conversation sent to an agent starting a new chat
        "resume_sessions": True,  # an agent continues its own chat and gets only what changed
        "session_max_turns": 8,  # after that many turns in one chat, start a new one (keeps context small)
        "max_consecutive_failures": 4,
        # When every agent is limited: wait for the earliest reset instead of stopping.
        "wait_on_limits": True,
        "max_wait_hours": 12,
        # auto = planner decides if a roadmap is needed; always / never force it.
        "planning": "auto",
        # Saved memory (facts, conventions, preferences) pasted into new chats, newest first.
        "memory_chars": 6000,  # this project's memory
        "global_memory_chars": 3000,  # memory shared by all projects
        # How every agent works, in this order. Change them with `aa settings set run.rules '[...]'`
        # (per project or --global).
        "rules": [
            "Plan if needed: when the work is bigger than one focused change and the roadmap does not cover it, "
            "propose concrete steps in \"add_steps\" (each with an honest level) and end your turn with status "
            "\"continue\" WITHOUT starting them: the orchestrator gives every step to the agent best suited "
            "for its level.",
            "Delegate if needed: hand a self-contained part to another agent with \"delegate\" (title, details "
            "that stand on their own, level, agent), for example light parts for helper agents, a part "
            "another agent is better at (also a bug you noticed that is not part of your step), or when your "
            "own usage is high. Do not also do a part you delegated.",
            "Do the work: small, safe increments, follow the project's conventions, and verify (build, tests) "
            "before you report.",
            "Commit every finished feature separately: one commit per feature or roadmap step, staging only "
            "that feature's files (git add <paths>; never sweep unrelated changes in with -A), with a short "
            "imperative message. Never push and never rewrite existing commits.",
        ],
        # auto = agents commit when the project is a git repository; off = never (you commit yourself).
        "commit": "auto",
        # off = nobody pushes (you do); on_done = aa runs `git push` itself when a chat finishes (no agent,
        # no tokens). Turn it on per project: aa settings set run.push on_done
        "push": "off",
        "push_branches": [],  # only push these branches, e.g. ["dev"]; empty = whatever branch is checked out
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def global_config_path() -> Path:
    return home_dir() / "config.json"


def project_config_path(project_root: Path) -> Path:
    return Path(project_root) / PROJECT_DIRNAME / "config.json"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid config file {path}: {exc}")


def load_config(project_root: Path | None = None) -> dict:
    cfg = deep_merge(DEFAULT_CONFIG, _read_json(global_config_path()))
    if project_root is not None:
        cfg = deep_merge(cfg, _read_json(project_config_path(project_root)))
    return cfg


def write_default_config(path: Path) -> bool:
    """Write the defaults to `path` unless it exists. Returns True when written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, indent=2)
    return True


# -- settings by dotted key (`aa settings set run.max_turns 20`) -----------------------------------
def read_config_file(path: Path) -> dict:
    return _read_json(path)


def write_config_file(path: Path, data: dict) -> None:
    from .jsonio import write_json
    write_json(path, data)


def get_key(cfg: dict, key: str):
    node: Any = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def has_key(cfg: dict, key: str) -> bool:
    try:
        get_key(cfg, key)
        return True
    except KeyError:
        return False


def set_key(cfg: dict, key: str, value) -> None:
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def unset_key(cfg: dict, key: str) -> bool:
    """Remove `key` (and parents left empty). Returns False when it was not set."""
    parts = key.split(".")
    chain = [cfg]
    for part in parts[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            return False
        chain.append(nxt)
    if parts[-1] not in chain[-1]:
        return False
    del chain[-1][parts[-1]]
    for i in range(len(chain) - 1, 0, -1):
        if not chain[i]:
            del chain[i - 1][parts[i - 1]]
    return True


def parse_value(raw: str):
    """'20' -> 20, 'true' -> True, 'null' -> None, '["light","mid"]' -> list, anything else -> str."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        low = raw.strip().lower()
        if low in ("yes", "on"):
            return True
        if low in ("no", "off"):
            return False
        return raw


def check_value(key: str, value, effective: dict) -> str | None:
    """A reason the value does not fit the setting, else None. Unknown keys are refused, except
    new fields of an agent (agents.<name>.<field>) since agent options are open-ended."""
    parts = key.split(".")
    if len(parts) < 2:
        return "give a full key such as run.max_turns or agents.codex.enabled"
    if parts[0] not in DEFAULT_CONFIG:
        return f"unknown section {parts[0]!r} (known: {', '.join(DEFAULT_CONFIG)})"
    if parts[0] == "agents":
        if len(parts) < 3:
            return "give an agent field such as agents.codex.enabled"
        if parts[1] not in effective["agents"] and parts[2] != "type":
            return (f"no agent {parts[1]!r}; to add one, first set agents.{parts[1]}.type "
                    "(claude, codex, antigravity or generic)")
    elif not has_key(DEFAULT_CONFIG, key):
        return f"unknown setting {key!r}"
    try:
        current = get_key(effective, key)
    except KeyError:
        return None
    if current is None or value is None:
        return None
    if isinstance(current, bool):
        return None if isinstance(value, bool) else "expects true or false"
    if isinstance(current, (int, float)):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else "expects a number"
    if isinstance(current, str):
        return None if isinstance(value, str) else "expects text"
    if isinstance(current, list):
        return None if isinstance(value, list) else 'expects a JSON list, e.g. ["light","mid"]'
    return None


def flatten(cfg: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v and k != "budgets":
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out
