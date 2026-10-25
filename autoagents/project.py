"""Per-project state, stored in <project>/.autoagents/ so any agent (and you) can read it.

A project holds several chats (one per task). Each chat has its own folder:

  chats/<id>/task.json          the task, status, who worked last, agent chat ids
  chats/<id>/roadmap.json       structured roadmap (source of truth) -> ROADMAP.md (readable copy)
  chats/<id>/steps.jsonl        one line per event (turn, handover, limit, ...) -> STEPS.md
  chats/<id>/transcript.jsonl   every agent output and user message -> TRANSCRIPT.md
  chats/<id>/live.log           console output of the chat while it runs in the background
  chats.json                    which chat is the current one
  memory.json, config.json      project memory and settings, shared by all chats
"""
from __future__ import annotations

import datetime as dt
import shutil
import time
import uuid
from pathlib import Path

from .config import PROJECT_DIRNAME
from .jsonio import append_jsonl, read_json, read_jsonl, write_json
from .prompts import normalize_level
from .ui import c

# Console colours for a step's level (its priority/weight): heavy stands out most.
LEVEL_COLORS = {"light": ("green",), "mid": ("yellow",), "heavy": ("red", "bold")}

STATE_FILES = ("task.json", "roadmap.json", "ROADMAP.md", "steps.jsonl", "STEPS.md",
               "transcript.jsonl", "TRANSCRIPT.md")
GITIGNORE = "# AutoAgents runtime state (memory.json and config.json may be committed)\nchats/\n*.lock\n"
OLD_GITIGNORE = "# AutoAgents runtime state\nhistory/\ntranscript.jsonl\n"


def _stamp(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d %H:%M")


def migrate(base: Path) -> None:
    """Move the old single-task layout (files in .autoagents/ + history/<id>/) into chats/<id>/."""
    chats = base / "chats"
    old_task = read_json(base / "task.json", None)
    if isinstance(old_task, dict) and old_task.get("id"):
        dest = chats / old_task["id"]
        dest.mkdir(parents=True, exist_ok=True)
        for name in STATE_FILES:
            if (base / name).exists():
                shutil.move(str(base / name), str(dest / name))
        write_json(base / "chats.json", {"current": old_task["id"]})
    history = base / "history"
    if history.is_dir():
        for d in history.iterdir():
            if d.is_dir() and not (chats / d.name).exists():
                chats.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(chats / d.name))
        try:
            history.rmdir()
        except OSError:
            pass
    gi = base / ".gitignore"
    if gi.exists() and gi.read_text(encoding="utf-8") == OLD_GITIGNORE:
        gi.write_text(GITIGNORE, encoding="utf-8")


class ProjectState:
    def __init__(self, root: Path, chat: str | None = None):
        self.root = Path(root).resolve()
        self.base = self.root / PROJECT_DIRNAME
        self.chats_dir = self.base / "chats"
        if (self.base / "task.json").exists() or (self.base / "history").is_dir():
            migrate(self.base)
        self.chat_id = self.resolve_chat(chat) if chat else self.current_chat_id()
        if chat and not self.chat_id:
            raise ValueError(f"No chat {chat!r} in {self.root}. See `aa chats`.")
        self._set_paths()

    def _set_paths(self) -> None:
        self.dir = self.chats_dir / (self.chat_id or "_none")
        self.task_path = self.dir / "task.json"
        self.roadmap_path = self.dir / "roadmap.json"
        self.roadmap_md = self.dir / "ROADMAP.md"
        self.steps_path = self.dir / "steps.jsonl"
        self.steps_md = self.dir / "STEPS.md"
        self.transcript_path = self.dir / "transcript.jsonl"
        self.transcript_md = self.dir / "TRANSCRIPT.md"
        self.live_log = self.dir / "live.log"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- chats ---------------------------------------------------------------------
    def chat_ids(self) -> list[str]:
        """Oldest first (ids start with a timestamp). Chat #1 is the first one."""
        if not self.chats_dir.is_dir():
            return []
        found = []
        for d in self.chats_dir.iterdir():
            task = read_json(d / "task.json", None)
            if isinstance(task, dict):
                found.append((task.get("created") or 0, d.name))
        return [cid for _, cid in sorted(found)]

    def current_chat_id(self) -> str | None:
        ids = self.chat_ids()
        cur = (read_json(self.base / "chats.json", {}) or {}).get("current")
        if cur in ids:
            return cur
        return ids[-1] if ids else None

    def set_current(self, chat_id: str) -> None:
        write_json(self.base / "chats.json", {"current": chat_id})

    def resolve_chat(self, ref: str) -> str | None:
        """'3' or '#3' = the third chat; otherwise a (prefix of a) chat id."""
        ids = self.chat_ids()
        ref = str(ref).strip().lstrip("#")
        if ref.isdigit() and 1 <= int(ref) <= len(ids) and len(ref) < 6:
            return ids[int(ref) - 1]
        matches = [i for i in ids if i.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def number(self, chat_id: str | None = None) -> int | None:
        ids = self.chat_ids()
        cid = chat_id or self.chat_id
        return ids.index(cid) + 1 if cid in ids else None

    def for_chat(self, chat_id: str) -> "ProjectState":
        return ProjectState(self.root, chat_id)

    # -- task ----------------------------------------------------------------------
    def load_task(self) -> dict | None:
        if not self.chat_id:
            return None
        task = read_json(self.task_path, None)
        return task if isinstance(task, dict) else None

    def save_task(self, task: dict) -> None:
        task["updated"] = time.time()
        write_json(self.task_path, task)

    def new_task(self, text: str) -> dict:
        """Start a new chat (earlier chats stay as they are and can be continued later)."""
        self.base.mkdir(parents=True, exist_ok=True)
        gitignore = self.base / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE, encoding="utf-8")
        self.chat_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self._set_paths()
        self.dir.mkdir(parents=True, exist_ok=True)
        task = {
            "id": self.chat_id,
            "text": text,
            "created": time.time(),
            "status": "new",  # new | running | blocked | waiting | done | stopped
            "turns": 0,
            "last_agent": None,
            "complex": None,
            "question": None,
            "handover": None,  # {"from", "reason"} set when an agent stops mid-way
            "user_messages": [],  # messages not yet shown to an agent
            "agents_used": [],
        }
        self.save_task(task)
        self.set_current(self.chat_id)
        self.transcript_md.write_text(f"# Transcript\n\nTask: {text}\n", encoding="utf-8")
        self.steps_md.write_text(f"# Steps log\n\nTask: {text}\n\n", encoding="utf-8")
        return task

    # -- roadmap -------------------------------------------------------------------
    def load_roadmap(self) -> list[dict]:
        data = read_json(self.roadmap_path, {})
        return list(data.get("steps") or []) if isinstance(data, dict) else []

    def save_roadmap(self, steps: list[dict]) -> None:
        for i, step in enumerate(steps, 1):
            step["id"] = i
            step.setdefault("status", "todo")
        write_json(self.roadmap_path, {"steps": steps, "updated": time.time()})
        self._render_roadmap(steps)

    def set_roadmap(self, raw_steps: list) -> list[dict]:
        steps = []
        for raw in raw_steps:
            if isinstance(raw, str):
                raw = {"title": raw}
            if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
                continue
            steps.append({
                "title": str(raw["title"]).strip(),
                "details": str(raw.get("details") or "").strip(),
                "agent": raw.get("agent") or None,
                "level": normalize_level(raw.get("level")),
                "status": "todo",
                "done_by": None,
            })
        self.save_roadmap(steps)
        return steps

    def current_step(self) -> dict | None:
        steps = self.load_roadmap()
        for status in ("in_progress", "todo"):
            for step in steps:
                if step.get("status") == status:
                    return step
        return None

    def mark_step(self, step_id: int, status: str, agent: str | None = None) -> None:
        steps = self.load_roadmap()
        for step in steps:
            if step["id"] == step_id and step.get("status") != "done":
                step["status"] = status
                if status == "done":
                    step["done_by"] = agent
        self.save_roadmap(steps)

    def apply_report(self, completed: list, add_steps: list, agent: str) -> None:
        steps = self.load_roadmap()
        if not steps and not add_steps:
            return
        ids = {int(x) for x in completed if str(x).strip().isdigit()}
        for step in steps:
            if step["id"] in ids and step.get("status") != "done":
                step["status"] = "done"
                step["done_by"] = agent
        for raw in add_steps or []:
            title = raw.get("title") if isinstance(raw, dict) else raw
            if title and str(title).strip():
                steps.append({"title": str(title).strip(),
                              "details": str(raw.get("details", "") if isinstance(raw, dict) else ""),
                              "level": normalize_level(raw.get("level")) if isinstance(raw, dict) else None,
                              "agent": (raw.get("agent") or None) if isinstance(raw, dict) else None,
                              "status": "todo", "done_by": None})
        self.save_roadmap(steps)

    def delegate(self, items: list[dict], by: str) -> list[dict]:
        """Insert steps another agent should do, right after the step being worked on (in order)."""
        steps = self.load_roadmap()
        pos = len(steps)
        for i, st in enumerate(steps):
            if st.get("status") == "in_progress":
                pos = i + 1
        new = [{"title": it["title"], "details": it.get("details", ""), "level": it.get("level"),
                "agent": it.get("agent"), "delegated_by": by, "status": "todo", "done_by": None}
               for it in items]
        steps[pos:pos] = new
        self.save_roadmap(steps)
        return new

    def update_step(self, step_id: int, **fields) -> None:
        steps = self.load_roadmap()
        for st in steps:
            if st["id"] == step_id:
                st.update(fields)
        self.save_roadmap(steps)

    def pending_steps(self) -> list[dict]:
        return [st for st in self.load_roadmap() if st.get("status") != "done"]

    def complete_all_steps(self, agent: str) -> None:
        steps = self.load_roadmap()
        for step in steps:
            if step.get("status") != "done":
                step["status"], step["done_by"] = "done", step.get("done_by") or agent
        if steps:
            self.save_roadmap(steps)

    def roadmap_text(self, steps: list[dict] | None = None, current_id: int | None = None,
                     color: bool = False) -> str:
        """Readable roadmap. color=True (console only, never files or prompts) colours open steps
        by level (light green, mid yellow, heavy red) and greys out done ones."""
        steps = self.load_roadmap() if steps is None else steps
        lines = []
        for s in steps:
            box = {"done": "[x]", "in_progress": "[~]"}.get(s.get("status"), "[ ]")
            done = s.get("status") == "done"
            styles = ("gray",) if done else LEVEL_COLORS.get(s.get("level") or "", ())
            paint = (lambda t: c(t, *styles)) if color else str
            line = f"{box} {s['id']}. {paint(s['title'])}"
            if s.get("level"):
                line += " " + paint(f"[{s['level']}]")
            if s.get("done_by"):
                line += f"  (done by {s['done_by']})"
            elif s.get("agent") and s.get("delegated_by"):
                line += f"  (delegated to {s['agent']} by {s['delegated_by']})"
            elif s.get("delegated_by"):
                line += f"  (delegated by {s['delegated_by']})"
            elif s.get("agent"):
                line += f"  (suggested: {s['agent']})"
            if current_id and s["id"] == current_id:
                line += "   <-- CURRENT"
            lines.append(line)
            if s.get("details") and s.get("status") != "done":
                lines.append(f"      {s['details']}")
        return "\n".join(lines)

    def _render_roadmap(self, steps: list[dict]) -> None:
        task = self.load_task() or {}
        body = f"# Roadmap\n\nTask: {task.get('text', '')}\n\n_Updated {_stamp()}. " \
               f"Edit roadmap.json (not this file) to change it._\n\n{self.roadmap_text(steps)}\n"
        self.roadmap_md.write_text(body, encoding="utf-8")

    # -- steps log -------------------------------------------------------------------
    def log_step(self, kind: str, agent: str | None, summary: str, **extra) -> None:
        rec = {"ts": time.time(), "kind": kind, "agent": agent, "summary": summary, **extra}
        append_jsonl(self.steps_path, rec)
        with open(self.steps_md, "a", encoding="utf-8") as fh:
            fh.write(f"- {_stamp(rec['ts'])} **{kind}**{f' ({agent})' if agent else ''}: {summary}\n")

    def recent_steps(self, n: int = 12) -> list[dict]:
        return read_jsonl(self.steps_path)[-n:]

    # -- transcript ------------------------------------------------------------------
    def add_transcript(self, role: str, agent: str | None, text: str, turn: int) -> None:
        rec = {"ts": time.time(), "turn": turn, "role": role, "agent": agent, "text": text}
        append_jsonl(self.transcript_path, rec)
        who = agent or role
        with open(self.transcript_md, "a", encoding="utf-8") as fh:
            fh.write(f"\n## Turn {turn} - {who} ({role}) - {_stamp(rec['ts'])}\n\n{text}\n")

    def transcript_count(self) -> int:
        return len(read_jsonl(self.transcript_path))

    def recent_transcript(self, max_chars: int, since: int = 0, skip_agent: str | None = None) -> str:
        """The newest transcript entries that fit in max_chars, oldest first.
        since: ignore the first `since` records (what an agent already saw in its own chat);
        skip_agent: leave out that agent's own outputs (it remembers them)."""
        chunks: list[str] = []
        used = 0
        for rec in reversed(read_jsonl(self.transcript_path)[since:]):
            if rec.get("role") == "orchestrator":
                continue  # our own prompts are rebuilt every turn, no need to repeat them
            if skip_agent and rec.get("role") == "agent" and rec.get("agent") == skip_agent:
                continue
            block = f"--- turn {rec.get('turn')} | {rec.get('agent') or rec.get('role')} ---\n{rec.get('text', '')}"
            if used + len(block) > max_chars:
                room = max_chars - used
                if room > 500:
                    chunks.append("[...older part cut...]\n" + block[-room:])
                break
            chunks.append(block)
            used += len(block)
        return "\n\n".join(reversed(chunks))
