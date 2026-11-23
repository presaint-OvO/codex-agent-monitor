#!/usr/bin/env python3
"""Persist one Codex hook event without affecting the agent run."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def default_state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(
            os.environ.get("LOCALAPPDATA")
            or Path.home() / "AppData" / "Local"
        )
    return Path.home() / ".local" / "state"


STATE_HOME = default_state_home()
STATE_DIR = STATE_HOME / "codex-agent-monitor"
EVENT_LOG = STATE_DIR / "events.jsonl"
EVENT_LOCK = STATE_DIR / "events.lock"


@contextmanager
def event_log_lock() -> Iterator[None]:
    """Serialize hook writers on both POSIX and native Windows."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOCK.open("a+b") as stream:
        if os.name == "nt":
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_event(event: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event["_monitor_recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    event["_monitor_recorded_ns"] = time.time_ns()
    event["_monitor_pid"] = os.getpid()
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    with event_log_lock():
        with EVENT_LOG.open("a", encoding="utf-8", newline="") as stream:
            stream.write(line)
            stream.flush()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record_event(payload)
    except Exception:
        # Observability must never interrupt or steer the Codex run.
        pass

    # SubagentStop expects JSON rather than arbitrary plain text. An empty
    # object is also harmless for the other supported hook events.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
