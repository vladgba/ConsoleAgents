"""The list of your projects (~/.autoagents/projects.json).

A project is a folder with a name. Its tasks, roadmap, logs and project memory live in the folder's
.autoagents/; usage limits, settings and global memory stay in ~/.autoagents/ and are shared by all.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .config import PROJECT_DIRNAME, home_dir
from .jsonio import read_json, write_json

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _norm(path) -> str:
    """Comparable form of a path (Windows paths are case-insensitive)."""
    text = os.path.normpath(str(Path(path).resolve()))
    return text.lower() if os.name == "nt" else text


def same_path(a, b) -> bool:
    return _norm(a) == _norm(b)


def is_inside(path, root) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else home_dir() / "projects.json"

    # -- storage ------------------------------------------------------------------------------
    def _load(self) -> dict:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("current", None)
        data.setdefault("projects", {})
        return data

    def _save(self, data: dict) -> None:
        write_json(self.path, data)

    # -- queries ------------------------------------------------------------------------------
    def all(self) -> dict[str, dict]:
        return self._load()["projects"]

    def ordered(self) -> list[str]:
        """Names in the order the projects were added: project #1 is the first one. Numbers stay put
        when you use a project; removing one renumbers the ones after it."""
        projects = self.all()
        return sorted(projects, key=lambda n: (projects[n].get("added") or 0, n))

    def number(self, name: str) -> int | None:
        names = self.ordered()
        return names.index(name) + 1 if name in names else None

    def resolve(self, ref) -> str | None:
        """A project by name, by number ('2' or '#2'), or by a unique start of its name (any case)."""
        if ref is None:
            return None
        ref = str(ref).strip()
        projects = self.all()
        if ref in projects:
            return ref
        bare = ref.lstrip("#")
        names = self.ordered()
        if bare.isdigit() and 1 <= int(bare) <= len(names):
            return names[int(bare) - 1]
        exact = [n for n in names if n.lower() == ref.lower()]
        if exact:
            return exact[0]
        starts = [n for n in names if n.lower().startswith(ref.lower())]
        return starts[0] if len(starts) == 1 else None

    def get(self, name: str) -> dict | None:
        resolved = self.resolve(name)
        return self.all().get(resolved) if resolved else None

    def current(self) -> str | None:
        data = self._load()
        cur = data["current"]
        return cur if cur in data["projects"] else None

    def by_path(self, path) -> str | None:
        """The project that contains `path` (the folder itself or one of its sub-folders)."""
        matches = [(len(_norm(info["path"])), name) for name, info in self.all().items()
                   if is_inside(path, info["path"])]
        return max(matches)[1] if matches else None  # the deepest one when projects are nested

    # -- changes ------------------------------------------------------------------------------
    def add(self, name: str, path, description: str = "") -> str:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"Invalid project name {name!r}: use letters, digits, '.', '_' or '-' (max 64).")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")
        if _norm(root / PROJECT_DIRNAME) == _norm(home_dir()) or is_inside(root, home_dir()):
            raise ValueError("The AutoAgents home folder cannot be a project.")
        data = self._load()
        if name in data["projects"]:
            raise ValueError(f"A project named {name!r} already exists ({data['projects'][name]['path']}).")
        for other, info in data["projects"].items():
            if _norm(info["path"]) == _norm(root):
                raise ValueError(f"{root} is already the project {other!r}.")
        data["projects"][name] = {"path": str(root), "description": description, "added": time.time(),
                                  "last_used": None}
        self._save(data)
        return name

    def ensure(self, path) -> str:
        """Register `path` under its folder name (made unique) unless it is already a project."""
        existing = self.by_path(path)
        if existing and same_path(self.all()[existing]["path"], path):
            return existing
        root = Path(path).resolve()
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-._") or "project"
        base = base[:56]
        names = self.all()
        name, n = base, 2
        while name in names:
            name, n = f"{base}-{n}", n + 1
        return self.add(name, root)

    def remove(self, name: str) -> dict:
        name = self.resolve(name) or name
        data = self._load()
        if name not in data["projects"]:
            raise ValueError(f"No project named {name!r}.")
        info = data["projects"].pop(name)
        if data["current"] == name:
            data["current"] = None
        self._save(data)
        return info

    def rename(self, old: str, new: str) -> None:
        old = self.resolve(old) or old
        data = self._load()
        if old not in data["projects"]:
            raise ValueError(f"No project named {old!r}.")
        if not NAME_RE.match(new or "") or new in data["projects"]:
            raise ValueError(f"Invalid or already used name {new!r}.")
        data["projects"][new] = data["projects"].pop(old)
        if data["current"] == old:
            data["current"] = new
        self._save(data)

    def use(self, name: str) -> dict:
        name = self.resolve(name) or name
        data = self._load()
        if name not in data["projects"]:
            raise ValueError(f"No project named {name!r}. See `aa project list`.")
        data["current"] = name
        data["projects"][name]["last_used"] = time.time()
        self._save(data)
        return data["projects"][name]

    def touch(self, name: str) -> None:
        """Mark it as the project worked on last (the shell opens it next time)."""
        try:
            self.use(name)
        except ValueError:
            pass
