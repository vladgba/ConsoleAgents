"""Chooses which agent runs the next turn.

score = strength * headroom * pacing, where headroom = 1 - utilization of the agent's fullest window
and pacing (see pacing()) uses the 5-hour and the weekly window together: quota about to be lost at a
reset is spent first, an agent spending its week too fast is spared.
  * agents in cooldown, missing, disabled, past their "active_until" date, at/above hard_limit,
    or whose "levels" do not include the step's level (light / mid / heavy) are skipped
  * agents above soft_limit are heavily penalised (used only as a last resort)
  * the agent suggested by the roadmap step, and the agent already working, get a bonus
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

from .config import WINDOWS
from .usage import AgentUsage, UsageStore


LEVELS = ("light", "mid", "heavy")


def accepts_level(acfg: dict, level: str | None) -> bool:
    """Agents without "levels" take anything. Restricted agents take only steps explicitly rated
    with one of their levels: an unrated step is treated as possibly heavy."""
    levels = acfg.get("levels")
    if not levels:
        return True
    return level in levels


def window_pace(w, now: float) -> tuple[float, str]:
    """How far an agent is behind (+) or ahead of (-) an even spend of this window.

    pace = share of the window already elapsed - share of the quota already used.
    +0.5 means half of the quota would be lost at the reset if nobody used it; -0.4 means it spends
    too fast and would run dry before the reset. A window with no activity yet ("idle") starts when
    the agent is next used, so nothing is lost yet: a weekly one gets a small +0.2 (enough to win NEW
    work over a busy agent, not enough to take a task from the agent already on it), a 5-hour one 0."""
    if w is None:
        return 0.0, ""
    minutes = WINDOWS.get(w.window)
    if not minutes:
        return 0.0, ""
    if not w.resets_at or w.resets_at <= now:
        if w.utilization < 0.02 and w.window == "weekly":
            return 0.2, "idle"
        return 0.0, ""
    span = minutes * 60
    elapsed = min(1.0, max(0.0, 1.0 - (w.resets_at - now) / span))
    return elapsed - w.utilization, f"{w.utilization:.0%} used, {elapsed:.0%} of the window gone"


def pacing(usage: AgentUsage, bal: dict, now: float) -> tuple[float, str]:
    """A score factor from BOTH windows, and a short explanation."""
    if not bal.get("pacing", True):
        return 1.0, ""
    pw, why_w = window_pace(usage.windows.get("weekly"), now)
    p5, why_5 = window_pace(usage.windows.get("five_hour"), now)
    factor = 1.0 + float(bal.get("pace_weight_weekly", 0.6)) * pw + float(bal.get("pace_weight_five_hour", 0.3)) * p5
    factor = min(float(bal.get("pace_max", 1.6)), max(float(bal.get("pace_min", 0.5)), factor))
    parts = []
    if why_w == "idle":
        parts.append("not used this week")
    elif abs(pw) >= 0.15:
        parts.append(("week under-used" if pw > 0 else "week used too fast") + (f" ({why_w})" if why_w else ""))
    if abs(p5) >= 0.25:
        parts.append("5h under-used" if p5 > 0 else "5h used too fast")
    return factor, ", ".join(parts)


def is_expired(acfg: dict, now: float) -> bool:
    until = acfg.get("active_until")
    if not until:
        return False
    try:
        last_day = dt.date.fromisoformat(str(until))
    except ValueError:
        return False  # a typo should not silently disable an agent; `aa agents` shows the value
    return dt.date.fromtimestamp(now) > last_day


@dataclass
class Candidate:
    name: str
    score: float
    usage: AgentUsage
    note: str = ""


@dataclass
class Skipped:
    name: str
    reason: str
    available_at: float | None = None  # when it may come back (for waiting)
    busy: bool = False  # working in another chat right now: free again soon


def rank_agents(cfg: dict, store: UsageStore, installed: set[str], *, hint: str | None = None,
                current: str | None = None, exclude: set[str] | None = None, level: str | None = None,
                forced: str | None = None, now: float | None = None,
                hint_bonus: float | None = None, avoid: set[str] | None = None, delegated: bool = False,
                busy: dict[str, list[dict]] | None = None) -> tuple[list[Candidate], list[Skipped]]:
    now = time.time() if now is None else now
    bal = cfg["balancer"]
    soft, hard = float(bal["soft_limit"]), float(bal["hard_limit"])
    ranked: list[Candidate] = []
    skipped: list[Skipped] = []

    for order, (name, acfg) in enumerate(cfg["agents"].items()):
        if not acfg.get("enabled", True):
            skipped.append(Skipped(name, "disabled"))
            continue
        if name not in installed:
            skipped.append(Skipped(name, "CLI not found"))
            continue
        if exclude and name in exclude:
            skipped.append(Skipped(name, "failed repeatedly this session"))
            continue
        if avoid and name in avoid:  # even when forced: it said it cannot do this well
            skipped.append(Skipped(name, "said this step is too hard for it"))
            continue
        if is_expired(acfg, now):
            skipped.append(Skipped(name, f"active_until {acfg['active_until']} has passed"))
            continue
        if name != forced and not accepts_level(acfg, level):  # the user's explicit choice wins
            skipped.append(Skipped(name, f"takes only {'/'.join(acfg['levels'])} steps, this one is {level or 'unrated'}"))
            continue
        running = (busy or {}).get(name) or []
        limit = int(acfg.get("max_parallel", bal.get("max_parallel", 1)) or 0)
        if limit and len(running) >= limit:
            where = ", ".join(f"{i.get('project') or '?'}#{i.get('chat') or '?'}" for i in running)
            skipped.append(Skipped(name, f"busy in another chat ({where})", busy=True))
            continue
        usage = store.agent_usage(name, acfg, now)
        if usage.in_cooldown(now):
            skipped.append(Skipped(name, f"cooldown ({usage.cooldown_reason})", usage.cooldown_until))
            continue
        util = usage.utilization
        if util >= hard:
            worst = usage.worst_window
            skipped.append(Skipped(name, f"{util:.0%} used (>= hard limit {hard:.0%})",
                                   worst.resets_at if worst else None))
            continue

        notes = []
        score = float(acfg.get("strength", 1.0)) * max(0.0, 1.0 - util)
        factor, why = pacing(usage, bal, now)
        score *= factor
        if why:
            notes.append(f"{why} (x{factor:.2f})")
        if util >= soft:
            score *= float(bal["over_soft_penalty"])
            notes.append(f"over soft limit {soft:.0%}")
        if hint and name == hint and util < soft:
            score *= float(hint_bonus if hint_bonus is not None else bal.get("hint_bonus", 1.25))
            notes.append("delegated to it" if delegated else "suggested by roadmap")
        if current and name == current and util < soft:
            score *= float(bal["stickiness"])
            notes.append("already on the task")
        score -= order * 1e-6  # stable tie-break by config order
        ranked.append(Candidate(name, score, usage, ", ".join(notes)))

    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked, skipped


def earliest_return(skipped: list[Skipped]) -> float | None:
    times = [s.available_at for s in skipped if s.available_at]
    return min(times) if times else None
