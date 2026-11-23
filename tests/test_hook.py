#!/usr/bin/env python3
"""Regression tests for the cross-platform Codex hook recorder."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = importlib.util.spec_from_file_location("monitor_hook", ROOT / "src" / "hook.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load hook.py")
monitor_hook = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor_hook
SPEC.loader.exec_module(monitor_hook)


class HookTests(unittest.TestCase):
    def test_record_event_writes_one_utf8_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            monitor_hook.STATE_DIR = state_dir
            monitor_hook.EVENT_LOG = state_dir / "events.jsonl"
            monitor_hook.EVENT_LOCK = state_dir / "events.lock"

            monitor_hook.record_event(
                {"hook_event_name": "SubagentStart", "task": "生成候选"}
            )

            lines = monitor_hook.EVENT_LOG.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["hook_event_name"], "SubagentStart")
            self.assertEqual(event["task"], "生成候选")
            self.assertIsInstance(event["_monitor_pid"], int)
            self.assertTrue(monitor_hook.EVENT_LOCK.exists())

    def test_concurrent_hook_processes_preserve_every_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_STATE_HOME"] = directory
            processes = []
            for index in range(8):
                payload = json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_use_id": f"parallel-{index}",
                    }
                )
                processes.append(
                    subprocess.Popen(
                        [sys.executable, str(ROOT / "src" / "hook.py")],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        env=environment,
                    )
                )
                processes[-1].stdin.write(payload)
                processes[-1].stdin.close()

            for process in processes:
                process.wait(timeout=10)
                stdout = process.stdout.read()
                stderr = process.stderr.read()
                process.stdout.close()
                process.stderr.close()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "{}")

            event_log = (
                Path(directory) / "codex-agent-monitor" / "events.jsonl"
            )
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(events), 8)
            self.assertEqual(
                {event["tool_use_id"] for event in events},
                {f"parallel-{index}" for index in range(8)},
            )


if __name__ == "__main__":
    unittest.main()
