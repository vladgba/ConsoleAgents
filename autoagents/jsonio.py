"""Small helpers for crash-safe JSON / JSONL files."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write: a crash or Ctrl+C never leaves a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


class file_lock:
    """Cross-process lock next to `path` (several background chats share global files).
    A lock older than `stale_s` is taken over: its owner most likely died."""

    def __init__(self, path: Path, timeout_s: float = 15.0, stale_s: float = 30.0):
        self.lock = Path(str(path) + ".lock")
        self.timeout_s, self.stale_s = timeout_s, stale_s
        self.fd: int | None = None

    def __enter__(self):
        import time
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_s
        while True:
            try:
                self.fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > self.stale_s:
                        self.lock.unlink()
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    return self  # better a rare lost update than a hung relay
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            try:
                self.lock.unlink()
            except OSError:
                pass
        return False
