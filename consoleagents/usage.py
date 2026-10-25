"""Usage ledger shared by every project (limits belong to the account, not the project).

Per agent it keeps:
  * events     - [timestamp, weighted_tokens] for each run (local estimate)
  * snapshots  - real utilization reported by the CLI (Claude, Codex) or set by hand
  * cooldown   - set when a limit was actually hit, until the reported reset time
  * learned    - budgets learned from real limit hits, to improve future estimates
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import WINDOWS, home_dir
from .jsonio import file_lock, read_json, write_json

EVENT_RETENTION_S = 8 * 24 * 3600


def window_for_minutes(minutes: float | None) -> str | None:
    """Map a window length reported by a CLI (e.g. 300, 10080) to one of our window names."""
    if not minutes:
        return None
    return min(WINDOWS, key=lambda name: abs(WINDOWS[name] - minutes))


@dataclass
class WindowUsage:
    window: str
    utilization: float  # 0..1+
    source: str  # "claude" / "codex" / "manual" / "estimate"
    resets_at: float | None
    tokens: int


@dataclass
class AgentUsage:
    name: str
    windows: dict[str, WindowUsage] = field(default_factory=dict)
    cooldown_until: float | None = None
    cooldown_reason: str = ""

    @property
    def utilization(self) -> float:
        return max((w.utilization for w in self.windows.values()), default=0.0)

    @property
    def worst_window(self) -> WindowUsage | None:
        return max(self.windows.values(), key=lambda w: w.utilization, default=None)

    def in_cooldown(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.cooldown_until and self.cooldown_until > now)


class UsageStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else home_dir() / "usage.json"

    # -- persistence ---------------------------------------------------------
    def _load(self) -> dict:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("agents", {})
        return data

    def _agent(self, data: dict, name: str) -> dict:
        rec = data["agents"].setdefault(name, {})
        rec.setdefault("events", [])
        rec.setdefault("snapshots", {})
        rec.setdefault("learned_budgets", {})
        rec.setdefault("cooldown_until", None)
        rec.setdefault("cooldown_reason", "")
        return rec

    def _update(self, name: str, fn) -> None:
        # Read-modify-write under a lock: several background chats update this file at once.
        with file_lock(self.path):
            data = self._load()
            fn(self._agent(data, name))
            write_json(self.path, data)

    # -- writes ----------------------------------------------------------------
    def record_tokens(self, name: str, tokens: int, now: float | None = None) -> None:
        now = time.time() if now is None else now

        def fn(rec):
            rec["events"].append([now, int(max(tokens, 0))])
            rec["events"] = [e for e in rec["events"] if now - e[0] < EVENT_RETENTION_S]

        self._update(name, fn)

    def set_snapshot(self, name: str, window: str, utilization: float, resets_at: float | None,
                     source: str, now: float | None = None) -> None:
        now = time.time() if now is None else now

        def fn(rec):
            rec["snapshots"][window] = {
                "utilization": max(0.0, float(utilization)),
                "resets_at": resets_at,
                "observed_at": now,
                "source": source,
            }
            if window in rec.get("disabled_windows", []):
                rec["disabled_windows"].remove(window)

        self._update(name, fn)

    def set_cooldown(self, name: str, until: float, reason: str) -> None:
        def fn(rec):
            rec["cooldown_until"] = until
            rec["cooldown_reason"] = reason

        self._update(name, fn)

    def clear(self, name: str, everything: bool = False) -> None:
        def fn(rec):
            rec["cooldown_until"] = None
            rec["cooldown_reason"] = ""
            rec["snapshots"] = {}
            for key in ("refresh", "quota_windows", "quota_observed_at", "disabled_windows"):
                rec.pop(key, None)
            if everything:
                rec["events"] = []
                rec["learned_budgets"] = {}

        self._update(name, fn)

    def record_refresh(self, name: str, result) -> None:
        """Save sanitized refresh status; replace quota data only after a successful fetch.

        Unknown periods and model pools remain available to the UI without being
        mislabeled as one of the balancer's two supported windows.
        """
        from dataclasses import asdict

        def fn(rec):
            rec["refresh"] = {"status": result.status, "message": result.message,
                              "checked_at": result.observed_at, "source": result.source}
            if result.windows:
                rec["quota_windows"] = [asdict(w) for w in result.windows]
                rec["quota_observed_at"] = result.observed_at
            if result.status not in {"ok", "partial"}:
                return
            snapshots = result.snapshots
            disabled = {w.window for w in result.windows if w.applicable and w.disabled
                        and w.window in WINDOWS and w.window not in snapshots}
            rec["disabled_windows"] = sorted(disabled)
            # A successful fetch supersedes the pool/window selection. Keeping an
            # omitted window could attach the previous model's quota to this one.
            rec["snapshots"] = snapshots

        self._update(name, fn)

    def refresh_info(self, name: str) -> dict:
        rec = self._agent(self._load(), name)
        return {**rec.get("refresh", {}), "quota_windows": rec.get("quota_windows", []),
                "quota_observed_at": rec.get("quota_observed_at"),
                "disabled_windows": rec.get("disabled_windows", []),
                "snapshots": rec.get("snapshots", {})}

    def learn_from_limit(self, name: str, window: str | None, now: float | None = None) -> None:
        """A real limit was hit: what we spent in that window is (roughly) the real budget."""
        now = time.time() if now is None else now

        def fn(rec):
            # Unknown window (agy and other CLIs do not say): it is almost always the short one. Learning a
            # weekly budget from it would mark the agent full for the whole rest of the week.
            targets = [window] if window in WINDOWS else ["five_hour"]
            for w in targets:
                spent = sum(t for ts, t in rec["events"] if now - ts < WINDOWS[w] * 60)
                if spent > 50_000:  # ignore tiny samples, they would poison the estimate
                    old = rec["learned_budgets"].get(w)
                    # Average with the previous lesson: one odd window should not swing it too far.
                    rec["learned_budgets"][w] = int(spent if old is None else (old + spent) / 2)

        self._update(name, fn)

    # -- reads -------------------------------------------------------------------
    def agent_usage(self, name: str, agent_cfg: dict, now: float | None = None) -> AgentUsage:
        now = time.time() if now is None else now
        rec = self._agent(self._load(), name)
        usage = AgentUsage(name=name)
        if rec.get("cooldown_until") and rec["cooldown_until"] > now:
            usage.cooldown_until = rec["cooldown_until"]
            usage.cooldown_reason = rec.get("cooldown_reason", "")
        budgets = dict(agent_cfg.get("budgets") or {})
        budgets.update(rec.get("learned_budgets") or {})
        if budgets.get("weekly") and budgets.get("five_hour") and budgets["weekly"] < budgets["five_hour"]:
            # A real weekly quota smaller than the guessed 5-hour budget (Antigravity's Claude models: about
            # 0.5M tokens a week): no 5-hour window can hold more than the whole week.
            budgets["five_hour"] = budgets["weekly"]

        for w, minutes in WINDOWS.items():
            span = minutes * 60
            events = [(ts, t) for ts, t in rec["events"] if now - ts < span]
            tokens = sum(t for _, t in events)
            budget = budgets.get(w) or 0
            snap = rec["snapshots"].get(w)
            snap_valid = (
                snap
                and now - snap["observed_at"] < span
                and (snap.get("resets_at") is None or snap["resets_at"] > now)
            )
            if snap_valid:
                # Real number + whatever we spent since it was observed.
                since = sum(t for ts, t in events if ts > snap["observed_at"])
                util = snap["utilization"] + (since / budget if budget else 0.0)
                usage.windows[w] = WindowUsage(w, util, snap["source"], snap.get("resets_at"), tokens)
            else:
                util = tokens / budget if budget else 0.0
                resets = (events[0][0] + span) if events else None
                usage.windows[w] = WindowUsage(w, util, "estimate", resets, tokens)
        return usage
