"""Long-lived memory that survives tasks and agent changes.

  project memory  <project>/.consoleagents/ (or .autoagents/)memory.json (+ MEMORY.md): conventions, commands, pitfalls,
                  decisions about THIS project. Kept when a task is archived.
  global memory   ~/.consoleagents/ (or ~/.autoagents/)memory.json (+ MEMORY.md): facts true for every project, such as
                  your preferences, your machine, tools you use.

Agents read both at the start of a chat and add entries through the "remember" field of their report;
you manage them with `aa memory`.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from .config import PROJECT_DIRNAME, home_dir
from .jsonio import read_json, write_json

MAX_ENTRY_CHARS = 1000
SCOPES = ("project", "global")


def _key(text: str) -> str:
    return " ".join(text.lower().split())


class Memory:
    def __init__(self, path: Path, scope: str):
        self.path = Path(path)
        self.scope = scope
        self.md_path = self.path.with_name("MEMORY.md")

    @property
    def prefix(self) -> str:
        return "g" if self.scope == "global" else "p"

    def _load(self) -> dict:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("next_id", 1)
        data.setdefault("entries", [])
        return data

    def entries(self) -> list[dict]:
        return self._load()["entries"]

    def label(self, entry: dict) -> str:
        return f"{self.prefix}{entry['id']}"

    def add(self, text: str, by: str | None = None) -> dict | None:
        """Add an entry; returns None when it is empty or already known."""
        text = " ".join(str(text or "").split()).strip()
        if not text:
            return None
        if len(text) > MAX_ENTRY_CHARS:
            text = text[: MAX_ENTRY_CHARS - 1] + "…"
        data = self._load()
        if any(_key(e["text"]) == _key(text) for e in data["entries"]):
            return None
        entry = {"id": data["next_id"], "text": text, "by": by or "user", "ts": time.time()}
        data["next_id"] += 1
        data["entries"].append(entry)
        self._write(data)
        return entry

    def remove(self, ident) -> dict | None:
        raw = str(ident).strip().lower().lstrip(self.prefix)
        if not raw.isdigit():
            return None
        data = self._load()
        for i, e in enumerate(data["entries"]):
            if e["id"] == int(raw):
                data["entries"].pop(i)
                self._write(data)
                return e
        return None

    def clear(self) -> int:
        data = self._load()
        n = len(data["entries"])
        data["entries"] = []
        self._write(data)
        return n

    def text(self, max_chars: int, since: float | None = None, skip_by: str | None = None) -> str:
        """Entries as '- text' lines, newest kept when it does not all fit, shown oldest first."""
        lines: list[str] = []
        used = 0
        for e in reversed(self.entries()):
            if since is not None and e.get("ts", 0) <= since:
                continue
            if skip_by and e.get("by") == skip_by:
                continue
            line = f"- {e['text']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(reversed(lines))

    def _write(self, data: dict) -> None:
        write_json(self.path, data)
        rows = [f"- **{self.label(e)}** {e['text']}  _({e.get('by') or 'user'}, "
                f"{dt.datetime.fromtimestamp(e.get('ts', 0)).strftime('%Y-%m-%d')})_" for e in data["entries"]]
        title = "Global memory (all projects)" if self.scope == "global" else "Project memory"
        self.md_path.write_text(f"# {title}\n\n_Managed by Console Agents: change it with `ca memory`, "
                                f"not by editing this file._\n\n" + ("\n".join(rows) or "(empty)") + "\n",
                                encoding="utf-8")


def project_memory(root: Path) -> Memory:
    return Memory(Path(root) / PROJECT_DIRNAME / "memory.json", "project")


def global_memory() -> Memory:
    return Memory(home_dir() / "memory.json", "global")


def memory_section(root: Path, project_chars: int, global_chars: int, since: float | None = None,
                   skip_by: str | None = None) -> str:
    """The memory block for a prompt ('' when there is nothing to show)."""
    parts = []
    g = global_memory().text(global_chars, since, skip_by)
    p = project_memory(root).text(project_chars, since, skip_by)
    if g:
        parts.append(f"## Global (about the user and all their projects)\n{g}")
    if p:
        parts.append(f"## This project\n{p}")
    return "\n\n".join(parts)
