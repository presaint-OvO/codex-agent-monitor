#!/usr/bin/env python3
"""Persist one Codex hook event without affecting the agent run."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
STATE_DIR = STATE_HOME / "codex-agent-monitor"
EVENT_LOG = STATE_DIR / "events.jsonl"


def record_event(event: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event["_monitor_recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    event["_monitor_recorded_ns"] = time.time_ns()
    event["_monitor_pid"] = os.getpid()
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    with EVENT_LOG.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(line)
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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
