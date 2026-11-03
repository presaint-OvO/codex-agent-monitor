#!/usr/bin/env python3
"""Regression tests for safe Codex hook configuration merging."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = importlib.util.spec_from_file_location(
    "configure_hooks", ROOT / "scripts" / "configure_hooks.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load configure_hooks.py")
configure_hooks = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configure_hooks
SPEC.loader.exec_module(configure_hooks)


class ConfigureHooksTests(unittest.TestCase):
    def test_install_and_remove_preserve_unrelated_hooks(self) -> None:
        existing = {
            "description": "user configuration",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Shell",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 /opt/example/audit.py",
                                "timeout": 7,
                            }
                        ],
                    }
                ],
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "notify-session"}
                        ]
                    }
                ],
            },
        }
        original = json.loads(json.dumps(existing))
        hook_path = Path("/tmp/share/codex-agent-monitor/hook.py")

        replaced = configure_hooks.install(existing, hook_path)

        self.assertEqual(replaced, 0)
        self.assertEqual(existing["description"], original["description"])
        self.assertEqual(
            existing["hooks"]["SessionStart"], original["hooks"]["SessionStart"]
        )
        self.assertEqual(existing["hooks"]["PreToolUse"][0], original["hooks"]["PreToolUse"][0])
        for event in configure_hooks.EVENTS:
            self.assertTrue(
                any(
                    configure_hooks.is_monitor_hook(command, hook_path)
                    for group in existing["hooks"][event]
                    for command in group["hooks"]
                )
            )

        removed = configure_hooks.strip_monitor_entries(existing, hook_path)

        self.assertEqual(removed, 4)
        self.assertEqual(existing, original)

    def test_reinstall_replaces_old_monitor_entries(self) -> None:
        config: dict = {}
        old_path = Path("/old/codex-agent-monitor/hook.py")
        new_path = Path("/new/codex-agent-monitor/hook.py")
        configure_hooks.install(config, old_path)

        replaced = configure_hooks.install(config, new_path)

        self.assertEqual(replaced, 4)
        serialized = json.dumps(config)
        self.assertNotIn(str(old_path), serialized)
        self.assertIn(str(new_path), serialized)

    def test_invalid_json_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hooks.json"
            config_path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(SystemExit):
                configure_hooks.load_config(config_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "{invalid")


if __name__ == "__main__":
    unittest.main()
