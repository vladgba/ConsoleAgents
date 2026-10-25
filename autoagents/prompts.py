"""Prompt templates and parsing of the agents' structured replies."""
from __future__ import annotations

import datetime as dt
import json
import re

REPORT_TAG = "autoagents-report"
PLAN_TAG = "autoagents-plan"


def usage_table(rows: list[tuple[str, str, float, str]]) -> str:
    """rows: (agent, description, utilization, availability note)"""
    lines = []
    for name, desc, util, note in rows:
        lines.append(f"- {name}: {util:.0%} of its usage limit used{f' ({note})' if note else ''}. Good at: {desc}")
    return "\n".join(lines)


MEMORY_HOWTO = """- "remember": durable facts worth knowing in FUTURE tasks, e.g. how to build/test this project, a
  convention, a pitfall you hit, a decision the user made. {"scope": "project", "text": "..."}. Use
  "scope": "global" only for facts about the user or their machine that hold for every project (for
  example a preference they stated). One short sentence each, no task progress, no secrets. Usually []."""


def memory_block(memory: str, title: str = "MEMORY") -> str:
    return (f"# {title} (saved by the user and earlier agents; it may be outdated, the code wins)\n{memory}"
            if memory else "")


def build_plan_prompt(task: str, usage_rows, agent: str, root: str, mode: str, memory: str = "") -> str:
    agent_names = "|".join(row[0] for row in usage_rows) or "null"
    force = {
        "always": "This task MUST get a roadmap (set \"complex\": true).",
        "never": "Do NOT make a roadmap (set \"complex\": false).",
    }.get(mode, "Decide yourself whether it is complex enough to need a roadmap.")
    return f"""You are "{agent}", the PLANNER of AutoAgents: a relay of AI coding agents (Claude, Codex, Antigravity)
that work on one project and replace each other when one reaches its usage limit.
Project directory: {root}

TASK FROM THE USER:
{task}

{memory_block(memory)}

Look at the project briefly (read-only: do NOT modify any file in this planning turn), then decide whether
this task is complex. A task is complex when it needs several distinct stages, touches many files, or is
likely to take more than one agent session. Simple tasks (one fix, one small feature, a question) need no roadmap.
{force}

If complex, write a roadmap: 3-12 ordered, concrete, verifiable steps. Each step should fit in one agent session.

Rate every step (and, when there is no roadmap, the task itself) with a level:
- "light": small isolated change, docs, reading/answering, renames; no history needed
- "mid":   a well-specified feature or fix in a few files; the step's details alone are enough context
- "heavy": architecture, cross-cutting refactors, hard debugging, or work that depends on long prior history
Some agents take ONLY light steps (see the team list): write self-contained "details" for light/mid steps (files, expected
behaviour, how to verify) so an agent with little history can do them.

For each step you may suggest which agent should do it, taking their strengths, accepted levels AND
current usage into account, so that no agent runs out in the middle of the work and the load is balanced:
{usage_table(usage_rows)}

Reply with a short explanation, then end with exactly one block in this format (valid JSON inside):
<{PLAN_TAG}>
{{"complex": true, "reason": "...", "level": "light|mid|heavy",
 "roadmap": [{{"title": "...", "details": "what to do and what done looks like", "level": "light|mid|heavy", "agent": "{agent_names}|null"}}]}}
</{PLAN_TAG}>"""


TOO_HARD = """- If this step (or the task) is harder than its rating, or harder than you can do WELL, do not push on:
  stop, set "too_hard": true and say why and what you already did in "summary". A stronger agent takes it
  over (your partial work stays on disk). Being honest here is better than a weak result."""


def job_text(step: dict | None) -> str:
    if not step:
        return "Work on the task until it is complete."
    return (f"Work on roadmap step {step['id']}: {step['title']}"
            + (f" [{step['level']}]" if step.get("level") else "")
            + (f"\nDone looks like: {step['details']}" if step.get("details") else "")
            + "\nWhen it is done you may go on with the next step only if it is rated the same level or lower and"
              " you are sure you can do it well; otherwise stop and report, so the best-suited agent gets it.")


def rules_block(rules: list[str], git_note: str) -> str:
    if not rules and not git_note:
        return ""
    lines = [f"{i}. {r}" for i, r in enumerate(rules or [], 1)]
    if git_note:
        lines.append(f"Git: {git_note}")
    return "# HOW YOU WORK (rules, in this order)\n" + "\n".join(lines)


def build_turn_prompt(*, agent: str, root: str, task: dict, roadmap_text: str, step: dict | None,
                      recent_steps: list[dict], history: str, handover: dict | None,
                      user_messages: list[str], usage_rows, state_dir: str, memory: str = "",
                      rules: list[str] | None = None, git_note: str = "", profile: str = "") -> str:
    parts = [f"""You are "{agent}", an autonomous coding agent in AutoAgents: a relay of AI agents (Claude, Codex, Antigravity)
working on ONE task in turns. Any turn can be your last (for example, you may hit your usage limit), and another agent
then continues from the files on disk and the logs below. So work in small, safe increments and leave the project
in a consistent state.
Project directory: {root}
Shared state (read-only for you; the orchestrator maintains it): {state_dir}/ (ROADMAP.md, STEPS.md, TRANSCRIPT.md).
Do not edit or delete anything inside {state_dir.split('/')[0]}/.
Today: {dt.date.today().isoformat()}

# TASK
{task['text']}"""]
    if memory:
        parts.append(memory_block(memory))

    if roadmap_text:
        parts.append(f"# ROADMAP (saved plan, follow it)\n{roadmap_text}")
    if recent_steps:
        log = "\n".join(f"- [{s.get('kind')}] {s.get('agent') or ''}: {s.get('summary', '')}" for s in recent_steps)
        parts.append(f"# PROGRESS LOG (most recent last)\n{log}")
    if handover:
        parts.append(f"""# HANDOVER: YOU ARE TAKING OVER FROM {handover['from'].upper()}
{handover['from']} stopped before finishing: {handover['reason']}.
Its last output (may stop mid-sentence) is at the end of the conversation history below.
The full history is in {state_dir}/TRANSCRIPT.md: read it if you need more detail.
Before redoing anything, inspect the working tree (e.g. `git status` / `git diff`, or the files it touched):
part of the work may already be done. Continue from where it stopped.""")
    if history:
        parts.append(f"# CONVERSATION HISTORY (recent, earlier agents' outputs)\n{history}")
    if user_messages:
        msgs = "\n".join(f"- {m}" for m in user_messages)
        parts.append(f"# NEW MESSAGES FROM THE USER (take them into account first)\n{msgs}")

    parts.append(f"""# YOUR JOB IN THIS TURN
{job_text(step)}
{f"Your profile: {profile}" if profile else ""}
- Act, do not ask: make reasonable assumptions and note them. Only report "blocked" if you truly need a decision
  or information from the user that you cannot infer.
- Verify your work (build / run tests) when the project allows it.
- If the plan turns out wrong, say so and propose new steps in "add_steps".
{TOO_HARD}""")
    if rules or git_note:
        parts.append(rules_block(rules or [], git_note))

    if usage_rows:
        parts.append(f"# TEAM (for delegating; usage right now)\n{usage_table(usage_rows)}")

    parts.append(f"""# REQUIRED FINAL REPORT
End your final message with exactly one block like this (valid JSON):
<{REPORT_TAG}>
{{"status": "continue|done|blocked", "completed_steps": [1], "summary": "what you did this turn",
 "next": "what should happen next", "question": "only when blocked", "add_steps": [],
 "delegate": [], "commits": [], "remember": [], "too_hard": false}}
</{REPORT_TAG}>
"done" means the WHOLE task is finished and verified. "completed_steps" lists roadmap step numbers you fully finished.
- "add_steps": [{{"title": "...", "details": "...", "level": "light|mid|heavy"}}] new roadmap steps (your plan).
- "delegate": [{{"title": "...", "details": "self-contained instructions", "level": "light|mid|heavy",
  "agent": "an agent from the team list"}}] parts for another agent; they run after the step you are on, in order.
- "commits": ["<short hash> <message>"] the commits you made this turn.
{MEMORY_HOWTO}""")
    return "\n\n".join(parts)


def build_followup_prompt(*, agent: str, roadmap_text: str, step: dict | None, news: str,
                          handover: dict | None, user_messages: list[str], state_dir: str,
                          memory: str = "", git_note: str = "") -> str:
    """A short prompt for an agent that continues its own earlier conversation: the task, rules and
    report format are already in its context, so only what changed since its last turn is sent."""
    parts = [f"""# NEXT AUTOAGENTS TURN (same conversation, you are still "{agent}")
The task, the rules and the report format from the start of this conversation still apply.
Files may have changed since your last turn: re-read a file before you edit it."""]
    if handover and handover.get("from") != agent:
        parts.append(f"{handover['from']} worked on the task after you and stopped: {handover['reason']}. "
                     "Inspect the working tree before redoing anything.")
    if news:
        parts.append(f"# WHAT HAPPENED SINCE YOUR LAST TURN (other agents' outputs; full log: "
                     f"{state_dir}/TRANSCRIPT.md)\n{news}")
    if user_messages:
        msgs = "\n".join(f"- {m}" for m in user_messages)
        parts.append(f"# NEW MESSAGES FROM THE USER (take them into account first)\n{msgs}")
    if memory:
        parts.append(memory_block(memory, "NEW IN MEMORY SINCE YOUR LAST TURN"))
    if roadmap_text:
        parts.append(f"# ROADMAP (current state)\n{roadmap_text}")
    parts.append(f"""# YOUR JOB IN THIS TURN
{job_text(step) if step else "Continue the task until it is complete."}
{TOO_HARD}
Same rules as before: plan if needed, delegate if needed, do the work, commit each finished feature separately.
{f"Git: {git_note}" if git_note else ""}
End your final message with the <{REPORT_TAG}>{{...}}</{REPORT_TAG}> JSON block, exactly as before
(status, completed_steps, summary, next, question, add_steps, delegate, commits, remember, too_hard).""")
    return "\n\n".join(parts)


def _extract_json_block(text: str, tag: str) -> dict | None:
    if not text:
        return None
    blocks = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S | re.I)
    candidates = list(reversed(blocks))
    if not candidates:  # tolerate a missing closing tag
        m = re.search(rf"<{tag}>\s*(\{{.*\}})", text, re.S | re.I)
        if m:
            candidates = [m.group(1)]
    for raw in candidates:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                continue
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
    return None


def parse_report(text: str) -> dict:
    data = _extract_json_block(text, REPORT_TAG)
    if data is None:
        tail = (text or "").strip()[-600:]
        return {"status": "continue", "completed_steps": [], "summary": tail or "(no output)",
                "next": "", "question": None, "add_steps": [], "remember": [], "delegate": [],
                "commits": [], "too_hard": False, "missing": True}
    status = str(data.get("status", "continue")).lower().strip()
    if status not in ("continue", "done", "blocked"):
        status = "continue"
    completed = data.get("completed_steps") or []
    if not isinstance(completed, list):
        completed = [completed]
    add_steps = data.get("add_steps") or []
    delegate = []
    for item in data.get("delegate") or [] if isinstance(data.get("delegate"), list) else []:
        if isinstance(item, str):
            item = {"title": item}
        if isinstance(item, dict) and str(item.get("title") or "").strip():
            delegate.append({"title": str(item["title"]).strip(), "details": str(item.get("details") or "").strip(),
                             "level": normalize_level(item.get("level")),
                             "agent": str(item["agent"]).strip() if item.get("agent") else None})
    commits = [str(x).strip() for x in (data.get("commits") or []) if str(x).strip()] \
        if isinstance(data.get("commits"), list) else []
    remember = []
    for item in data.get("remember") or [] if isinstance(data.get("remember"), list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            scope = "global" if str(item.get("scope", "")).lower() == "global" else "project"
            remember.append({"scope": scope, "text": str(item["text"]).strip()})
    return {
        "status": status,
        "completed_steps": completed,
        "summary": str(data.get("summary") or "").strip(),
        "next": str(data.get("next") or "").strip(),
        "question": data.get("question") or None,
        "add_steps": add_steps if isinstance(add_steps, list) else [],
        "remember": remember,
        "delegate": delegate,
        "commits": commits,
        "too_hard": data.get("too_hard") is True or str(data.get("too_hard")).lower() == "true",
        "missing": False,
    }


def parse_plan(text: str) -> dict | None:
    data = _extract_json_block(text, PLAN_TAG)
    if data is None:
        return None
    roadmap = data.get("roadmap") or []
    return {
        "complex": bool(data.get("complex")) and bool(roadmap),
        "reason": str(data.get("reason") or ""),
        "level": normalize_level(data.get("level")),
        "roadmap": roadmap if isinstance(roadmap, list) else [],
    }


def normalize_level(value) -> str | None:
    """'light' / 'mid' / 'heavy', accepting a few synonyms; None when missing or unknown."""
    v = str(value or "").strip().lower()
    aliases = {"low": "light", "easy": "light", "small": "light", "simple": "light",
               "medium": "mid", "middle": "mid", "moderate": "mid",
               "high": "heavy", "hard": "heavy", "complex": "heavy", "large": "heavy"}
    v = aliases.get(v, v)
    return v if v in ("light", "mid", "heavy") else None
