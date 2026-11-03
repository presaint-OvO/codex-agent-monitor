#!/usr/bin/env python3
"""Install or remove Codex Agent Monitor entries in a Codex hooks file."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any


EVENTS = ("SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse")
MONITOR_MARKER = "codex-agent-monitor/hook.py"


def is_monitor_hook(hook: Any, hook_path: Path | None = None) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    if MONITOR_MARKER in normalized:
        return True
    return hook_path is not None and str(hook_path) in command


def strip_monitor_entries(config: dict[str, Any], hook_path: Path | None = None) -> int:
    hooks_by_event = config.get("hooks")
    if not isinstance(hooks_by_event, dict):
        return 0

    removed = 0
    for event in EVENTS:
        groups = hooks_by_event.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            commands = group.get("hooks")
            if not isinstance(commands, list):
                kept_groups.append(group)
                continue
            kept_commands = []
            for command in commands:
                if is_monitor_hook(command, hook_path):
                    removed += 1
                else:
                    kept_commands.append(command)
            if kept_commands:
                updated = dict(group)
                updated["hooks"] = kept_commands
                kept_groups.append(updated)
        if kept_groups:
            hooks_by_event[event] = kept_groups
        else:
            hooks_by_event.pop(event, None)
    return removed


def monitor_group(event: str, hook_path: Path) -> dict[str, Any]:
    command = f"python3 {shlex.quote(str(hook_path))}"
    group: dict[str, Any] = {
        "hooks": [{"type": "command", "command": command, "timeout": 3}]
    }
    if event in {"PreToolUse", "PostToolUse"}:
        group["matcher"] = "*"
    return group


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"拒绝修改无效 JSON：{path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"拒绝修改非对象配置：{path}")
    return value


def write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def install(config: dict[str, Any], hook_path: Path) -> int:
    removed = strip_monitor_entries(config, hook_path)
    hooks_by_event = config.setdefault("hooks", {})
    if not isinstance(hooks_by_event, dict):
        raise SystemExit("拒绝修改配置：顶层 hooks 必须是对象")
    for event in EVENTS:
        groups = hooks_by_event.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(f"拒绝修改配置：hooks.{event} 必须是数组")
        groups.append(monitor_group(event, hook_path))
    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hook", type=Path)
    args = parser.parse_args()
    if args.action == "install" and args.hook is None:
        parser.error("install 需要 --hook")
    return args


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    hook_path = args.hook.expanduser().resolve() if args.hook else None
    config = load_config(config_path)
    if args.action == "install":
        assert hook_path is not None
        replaced = install(config, hook_path)
        write_config(config_path, config)
        print(f"已配置 Codex Hooks：{config_path}（替换旧条目 {replaced} 个）")
    else:
        removed = strip_monitor_entries(config, hook_path)
        if removed:
            write_config(config_path, config)
        print(f"已移除 Codex Agent Monitor Hooks：{removed} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
