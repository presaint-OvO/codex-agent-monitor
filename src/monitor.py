#!/usr/bin/env python3
"""A dependency-free terminal dashboard for Codex subagents."""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


AGENT_TOOL_NAMES = {"Agent", "spawn_agent", "create_subagent"}
FOLLOWUP_TOOL_NAMES = {"followup_task", "send_subagent_input", "send_message"}
LIST_AGENT_TOOL_NAMES = {"list_agents"}
INTERRUPT_AGENT_TOOL_NAMES = {"interrupt_agent"}
ACTIVE_AGENT_STATUSES = {"running", "starting"}
TERMINAL_AGENT_STATUSES = {"completed", "failed", "interrupted", "cancelled"}
MAX_DELEGATION_PROMPT_CHARS = 50_000


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


def parse_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def parse_millisecond_time(value: Any, fallback: Any) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if abs(number) >= 100_000_000_000 else number
    return parse_time(fallback)


def event_time(event: dict[str, Any]) -> float:
    return parse_time(
        event.get("_monitor_recorded_at")
        or event.get("timestamp")
        or event.get("created_at")
    )


def compact(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def multiline_text(value: Any, limit: int = MAX_DELEGATION_PROMPT_CHARS) -> str:
    """Keep prompt formatting while applying a defensive display-size limit."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except TypeError:
            text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) > limit:
        return text[: max(0, limit - 2)].rstrip() + "\n…"
    return text


def is_encrypted_prompt(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("gAAAA")


def delegation_prompt_from_input(tool_input: Any) -> tuple[str, bool]:
    """Return a readable spawn prompt and whether only ciphertext was found."""
    if isinstance(tool_input, dict):
        prompt = first_nested(
            tool_input,
            ("message", "prompt", "task", "objective", "description", "input"),
        )
    else:
        prompt = tool_input
    if is_encrypted_prompt(prompt):
        return "", True
    return multiline_text(prompt), False


def delegation_prompt_from_agent_message(content: Any) -> tuple[str, bool]:
    """Extract NEW_TASK payload text from a child rollout agent message."""
    if not isinstance(content, list):
        return "", False
    encrypted = False
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "encrypted_content":
            encrypted = True
        elif item_type == "input_text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
    combined = "\n".join(texts)
    if "Message Type: NEW_TASK" not in combined:
        return "", encrypted
    marker = "Payload:\n"
    prompt = combined.split(marker, 1)[1] if marker in combined else ""
    return multiline_text(prompt), encrypted


def first_nested(value: Any, keys: Iterable[str]) -> Any:
    wanted = tuple(keys)
    if isinstance(value, dict):
        for key in wanted:
            candidate = value.get(key)
            if candidate not in (None, "", [], {}):
                return candidate
        for candidate in value.values():
            found = first_nested(candidate, wanted)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = first_nested(candidate, wanted)
            if found not in (None, "", [], {}):
                return found
    return None


def task_from_input(tool_input: Any) -> tuple[str, str]:
    if not isinstance(tool_input, dict):
        return "subagent", compact(tool_input)
    name = first_nested(
        tool_input,
        ("task_name", "name", "agent_name", "agent_type", "description"),
    )
    task = first_nested(
        tool_input,
        ("message", "prompt", "task", "objective", "description", "input"),
    )
    clean_name = compact(name or "subagent", 80)
    if isinstance(task, str) and task.startswith("gAAAA"):
        # Recent Codex rollouts encrypt delegated prompts at rest. The task
        # name and the child agent's own progress messages remain readable.
        clean_task = f"任务标识: {clean_name}"
    else:
        clean_task = compact(task or tool_input, 600)
    return clean_name, clean_task


def agent_id_from(value: Any) -> str | None:
    found = first_nested(value, ("agent_id", "subagent_id"))
    if found:
        return compact(found, 160)
    task_name = first_nested(value, ("canonical_task_name", "task_name"))
    if isinstance(task_name, str) and task_name.startswith("/"):
        return compact(task_name, 160)
    if isinstance(value, str):
        match = re.search(
            r"(?:agent_id|subagent_id)[\"'\s:=]+([/\w.-]{6,})",
            value,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
    return None


def thread_id_from_mcp_result(value: Any) -> str | None:
    """Extract a create_thread thread ID from an MCP CallToolResult."""
    found = first_nested(value, ("threadId", "thread_id"))
    if found:
        return compact(found, 160)
    if not isinstance(value, dict):
        return None
    content = value.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        parsed = json_dict(item.get("text"))
        found = first_nested(parsed, ("threadId", "thread_id"))
        if found:
            return compact(found, 160)
    return None


def target_from(tool_input: Any) -> str | None:
    target = first_nested(tool_input, ("target", "agent_id", "subagent_id"))
    return compact(target, 160) if target else None


def short_id(value: str) -> str:
    if value.startswith("pending:"):
        return "pending"
    if "/" in value:
        return value.rsplit("/", 1)[-1]
    return value[:12]


def matches_tool(tool: str, names: set[str]) -> bool:
    """Accept both bare tool names and namespaced hook tool names."""
    if tool in names:
        return True
    return any(
        tool.endswith(separator + name)
        for name in names
        for separator in (".", "/", "__")
    )


def json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return compact(content, 1400)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return compact(" ".join(parts), 1400)
    return ""


def session_name_from_meta(meta: dict[str, Any]) -> str:
    for key in ("session_name", "session_title", "thread_name", "thread_title", "title", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return compact(value, 72)
    return ""


@dataclass
class Step:
    key: str
    tool: str
    summary: str
    started_at: float
    status: str = "running"
    output: str = ""
    finished_at: float | None = None


@dataclass
class AgentState:
    agent_id: str
    name: str = "subagent"
    task: str = ""
    delegation_prompt: str = ""
    delegation_prompt_source: str = ""
    delegation_prompt_encrypted: bool = False
    agent_type: str = ""
    status: str = "starting"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: str = ""
    session_id: str = ""
    worker_kind: str = "subagent"
    external_session_id: str = ""
    cwd: str = ""
    last_activity_at: float = field(default_factory=time.time)
    turn_ids: set[str] = field(default_factory=set)
    transcript_paths: set[str] = field(default_factory=set)
    steps: OrderedDict[str, Step] = field(default_factory=OrderedDict)


@dataclass
class SessionState:
    session_id: str
    name: str = ""
    cwd: str = ""
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    last_turn_finished_at: float | None = None


def is_visible_subagent(agent: AgentState) -> bool:
    """Exclude root/default Hook rows; named /root/... children remain visible."""
    if agent.worker_kind == "external_task":
        return True
    agent_id = agent.agent_id.strip().rstrip("/").lower()
    if agent_id in {"root", "/root"}:
        return False
    return agent.name.strip().lower() != "default" and agent.agent_type.strip().lower() != "default"


class MonitorState:
    def __init__(self) -> None:
        self.sessions: OrderedDict[str, SessionState] = OrderedDict()
        self.agents: OrderedDict[str, AgentState] = OrderedDict()
        self.pending_spawns: OrderedDict[str, str] = OrderedDict()
        self.turn_to_agent: dict[str, str] = {}
        self.transcript_to_agent: dict[str, str] = {}
        # create_thread launches a separate top-level Codex session. Keep its
        # real identity while presenting it under the session that dispatched
        # it as an explicitly labelled external task.
        self.external_task_keys: dict[str, str] = {}
        self.last_event_at: float | None = None
        self.malformed_lines = 0

    def touch_session(
        self,
        session_id: Any,
        *,
        timestamp: Any = None,
        cwd: Any = None,
        name: Any = None,
    ) -> None:
        clean_id = compact(session_id, 160)
        if not clean_id:
            return
        seen_at = parse_time(timestamp)
        clean_cwd = compact(cwd, 1000)
        clean_name = compact(name, 72)
        session = self.sessions.get(clean_id)
        if session is None:
            self.sessions[clean_id] = SessionState(
                session_id=clean_id,
                name=clean_name,
                cwd=clean_cwd,
                started_at=seen_at,
                last_activity_at=seen_at,
            )
            return
        session.started_at = min(session.started_at, seen_at)
        session.last_activity_at = max(session.last_activity_at, seen_at)
        if clean_cwd:
            session.cwd = clean_cwd
        if clean_name and not session.name:
            session.name = clean_name

    def register_external_task(
        self,
        parent_session_id: Any,
        external_session_id: Any,
        *,
        title: Any = None,
        prompt: Any = None,
        timestamp: Any = None,
        cwd: Any = None,
    ) -> AgentState | None:
        """Associate a codex_tui create_thread task with its dispatcher."""
        parent_id = compact(parent_session_id, 160)
        external_id = compact(external_session_id, 160)
        if not parent_id or not external_id or parent_id == external_id:
            return None
        seen_at = parse_time(timestamp)
        clean_title = compact(title, 120) or f"外部任务 {short_id(external_id)}"
        clean_prompt = multiline_text(prompt)
        key = f"{parent_id}::{external_id}"
        agent = self.agents.get(key)
        if agent is None:
            agent = AgentState(
                agent_id=external_id,
                name=clean_title,
                task=compact(clean_prompt, 700),
                delegation_prompt=clean_prompt,
                delegation_prompt_source="create_thread",
                worker_kind="external_task",
                external_session_id=external_id,
                status="starting",
                started_at=seen_at,
                finished_at=None,
                session_id=parent_id,
                cwd=compact(cwd, 1000),
                last_activity_at=seen_at,
            )
            self.agents[key] = agent
        else:
            agent.worker_kind = "external_task"
            agent.external_session_id = external_id
            agent.session_id = parent_id
            agent.started_at = min(agent.started_at, seen_at)
            agent.last_activity_at = max(agent.last_activity_at, seen_at)
            if clean_title:
                agent.name = clean_title
            if clean_prompt:
                agent.task = agent.task or compact(clean_prompt, 700)
                self._set_delegation_prompt(agent, clean_prompt, "create_thread")
            if cwd:
                agent.cwd = compact(cwd, 1000)
        self.external_task_keys[external_id] = key
        self.touch_session(parent_id, timestamp=seen_at, cwd=cwd)
        return agent

    def external_task(self, external_session_id: Any) -> AgentState | None:
        key = self.external_task_keys.get(compact(external_session_id, 160))
        return self.agents.get(key) if key else None

    def finish_external_task(self, external_session_id: Any, timestamp: Any) -> int:
        agent = self.external_task(external_session_id)
        if agent is None:
            return 0
        finished_at = parse_time(timestamp)
        agent.status = "completed"
        agent.finished_at = finished_at
        agent.last_activity_at = max(agent.last_activity_at, finished_at)
        self._finish_running_steps(agent)
        self.touch_session(
            agent.session_id,
            timestamp=finished_at,
            cwd=agent.cwd,
        )
        return 1

    @staticmethod
    def _scoped(value: str, event: dict[str, Any]) -> str:
        session_id = compact(event.get("session_id"), 160)
        return f"{session_id}::{value}" if session_id else value

    def _rename_agent(
        self, old_key: str, new_id: str, event: dict[str, Any]
    ) -> AgentState:
        new_key = self._scoped(new_id, event)
        if old_key == new_key:
            return self.agents[old_key]
        old = self.agents.pop(old_key)
        existing = self.agents.get(new_key)
        if existing:
            if not existing.task:
                existing.task = old.task
            if not existing.delegation_prompt and old.delegation_prompt:
                existing.delegation_prompt = old.delegation_prompt
                existing.delegation_prompt_source = old.delegation_prompt_source
            existing.delegation_prompt_encrypted = (
                existing.delegation_prompt_encrypted
                or old.delegation_prompt_encrypted
            ) and not existing.delegation_prompt
            if existing.name == "subagent":
                existing.name = old.name
            for key, step in old.steps.items():
                existing.steps.setdefault(key, step)
            existing.last_activity_at = max(
                existing.last_activity_at, old.last_activity_at
            )
            existing.turn_ids.update(old.turn_ids)
            existing.transcript_paths.update(old.transcript_paths)
            agent = existing
        else:
            old.agent_id = new_id
            self.agents[new_key] = old
            agent = old
        for mapping in (self.turn_to_agent, self.transcript_to_agent):
            for key, value in list(mapping.items()):
                if value == old_key:
                    mapping[key] = new_key
        return agent

    def _new_pending(self, event: dict[str, Any]) -> AgentState:
        tool_id = compact(event.get("tool_use_id") or f"spawn-{len(self.agents) + 1}")
        spawn_key = self._scoped(tool_id, event)
        pending_id = f"pending:{spawn_key}"
        name, task = task_from_input(event.get("tool_input"))
        prompt, prompt_encrypted = delegation_prompt_from_input(
            event.get("tool_input")
        )
        agent = self.agents.get(pending_id)
        if not agent:
            agent = AgentState(
                agent_id=pending_id,
                name=name,
                task=task,
                delegation_prompt=prompt,
                delegation_prompt_source="spawn_agent" if prompt else "",
                delegation_prompt_encrypted=prompt_encrypted,
                status="starting",
                started_at=event_time(event),
                session_id=compact(event.get("session_id"), 160),
                cwd=compact(event.get("cwd"), 1000),
                last_activity_at=event_time(event),
            )
            self.agents[pending_id] = agent
        self.pending_spawns[spawn_key] = pending_id
        return agent

    @staticmethod
    def _set_delegation_prompt(
        agent: AgentState,
        prompt: str,
        source: str,
        *,
        encrypted: bool = False,
        prefer_expanded: bool = False,
    ) -> None:
        clean_prompt = multiline_text(prompt)
        should_replace = bool(clean_prompt) and (
            not agent.delegation_prompt
            or agent.delegation_prompt_encrypted
            or (prefer_expanded and len(clean_prompt) > len(agent.delegation_prompt))
        )
        if should_replace:
            agent.delegation_prompt = clean_prompt
            agent.delegation_prompt_source = compact(source, 1000)
            agent.delegation_prompt_encrypted = False
        elif encrypted and not agent.delegation_prompt:
            agent.delegation_prompt_encrypted = True

    @staticmethod
    def _finish_running_steps(agent: AgentState) -> None:
        for step in agent.steps.values():
            if step.status == "running":
                step.status = "unknown"

    def _agent_for_target(
        self, target: Any, event: dict[str, Any], *, create: bool = False
    ) -> AgentState | None:
        agent_id = compact(target, 160)
        if not agent_id or agent_id.strip().rstrip("/").lower() in {"root", "/root"}:
            return None
        key = self._scoped(agent_id, event)
        agent = self.agents.get(key)
        if agent is not None:
            return agent

        # Collaboration tool calls sometimes address an agent by its short task
        # name (for example ``review``), while lifecycle events use its canonical
        # path (``/root/review``). Resolve that alias only when it uniquely names
        # an existing child in the same root session. An ambiguous short name is
        # deliberately ignored instead of creating a duplicate phantom agent.
        normalized_target = agent_id.strip().rstrip("/")
        if "/" not in normalized_target:
            session_id = compact(event.get("session_id"), 160)
            candidates = [
                candidate
                for candidate in self.agents.values()
                if (not session_id or candidate.session_id == session_id)
                and not candidate.agent_id.startswith("pending:")
                and candidate.agent_id.strip().rstrip("/").rsplit("/", 1)[-1]
                == normalized_target
            ]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None

        if not create:
            return None
        timestamp = event_time(event)
        agent = AgentState(
            agent_id=agent_id,
            name=short_id(agent_id),
            status="starting",
            started_at=timestamp,
            session_id=compact(event.get("session_id"), 160),
            cwd=compact(event.get("cwd"), 1000),
            last_activity_at=timestamp,
        )
        self.agents[key] = agent
        return agent

    @staticmethod
    def _decode_agent_status(value: Any) -> tuple[str, str]:
        result = ""
        if isinstance(value, str):
            status = value.strip().lower()
        elif isinstance(value, dict) and value:
            raw_status, detail = next(iter(value.items()))
            status = compact(raw_status, 80).lower()
            result = compact(detail, 1000)
        else:
            return "", ""
        aliases = {
            "active": "running",
            "started": "running",
            "complete": "completed",
            "done": "completed",
            "error": "failed",
            "stopped": "interrupted",
        }
        return aliases.get(status, status), result

    def _set_agent_status(
        self,
        target: Any,
        value: Any,
        event: dict[str, Any],
        *,
        create: bool = True,
    ) -> bool:
        status, result = self._decode_agent_status(value)
        if status not in ACTIVE_AGENT_STATUSES | TERMINAL_AGENT_STATUSES:
            return False
        agent = self._agent_for_target(target, event, create=create)
        if agent is None:
            return False
        timestamp = event_time(event)
        agent.last_activity_at = max(agent.last_activity_at, timestamp)
        agent.session_id = compact(event.get("session_id"), 160) or agent.session_id
        agent.cwd = compact(event.get("cwd"), 1000) or agent.cwd
        previous_status = agent.status
        previous_finished_at = agent.finished_at
        if status in ACTIVE_AGENT_STATUSES:
            session = self.sessions.get(agent.session_id)
            if (
                session is not None
                and session.last_turn_finished_at is not None
                and timestamp <= session.last_turn_finished_at
            ):
                if agent.status not in TERMINAL_AGENT_STATUSES:
                    agent.status = "interrupted"
                    agent.finished_at = session.last_turn_finished_at
                    self._finish_running_steps(agent)
                return True
            agent.status = status
            agent.finished_at = None
        else:
            agent.status = status
            # The native SubAgentActivity lifecycle event is normally recorded
            # just before the corresponding tool result/list refresh. Repeated
            # reports of the same terminal state must not move the finish time
            # forward and inflate the displayed duration.
            if previous_status == status and previous_finished_at is not None:
                agent.finished_at = min(previous_finished_at, timestamp)
            else:
                agent.finished_at = timestamp
            self._finish_running_steps(agent)
        if result:
            agent.result = result
        return True

    def finish_session_turn(self, session_id: Any, timestamp: Any) -> int:
        clean_id = compact(session_id, 160)
        if not clean_id:
            return 0
        finished_at = parse_time(timestamp)
        self.touch_session(clean_id, timestamp=finished_at)
        session = self.sessions[clean_id]
        if (
            session.last_turn_finished_at is None
            or finished_at > session.last_turn_finished_at
        ):
            session.last_turn_finished_at = finished_at
        changed = 0
        for agent in self.agents.values():
            if (
                agent.session_id == clean_id
                and agent.worker_kind != "external_task"
                and agent.status in ACTIVE_AGENT_STATUSES
                and agent.last_activity_at <= finished_at
            ):
                agent.status = "interrupted"
                agent.finished_at = finished_at
                if not agent.result:
                    agent.result = "主 Agent 回合已结束，未记录到该子 Agent 的完成事件。"
                self._finish_running_steps(agent)
                changed += 1
        for spawn_key, pending_id in list(self.pending_spawns.items()):
            agent = self.agents.get(pending_id)
            if agent is not None and agent.session_id == clean_id:
                self.pending_spawns.pop(spawn_key, None)
        return changed

    def _agent_for_event(self, event: dict[str, Any]) -> AgentState | None:
        direct_id = compact(event.get("agent_id") or event.get("subagent_id"), 160)
        if direct_id:
            direct_key = self._scoped(direct_id, event)
            if direct_key in self.agents:
                return self.agents[direct_key]

        turn_id = compact(event.get("turn_id"), 160)
        transcript = compact(event.get("transcript_path"), 1000)
        turn_key = self._scoped(turn_id, event) if turn_id else ""
        agent_id = self.turn_to_agent.get(turn_key) if turn_key else None
        if not agent_id and transcript:
            agent_id = self.transcript_to_agent.get(transcript)
        if agent_id:
            return self.agents.get(agent_id)

        tool_id = compact(event.get("tool_use_id"), 160)
        if tool_id:
            for agent in self.agents.values():
                if tool_id in agent.steps:
                    return agent
        return None

    def _start_agent(self, event: dict[str, Any]) -> None:
        agent_id = compact(event.get("agent_id") or event.get("subagent_id"), 160)
        if not agent_id:
            return
        agent_key = self._scoped(agent_id, event)
        agent = self.agents.get(agent_key)
        if not agent and self.pending_spawns:
            session_id = compact(event.get("session_id"), 160)
            candidates = [
                (spawn_key, pending_id)
                for spawn_key, pending_id in self.pending_spawns.items()
                if not session_id
                or self.agents[pending_id].session_id == session_id
            ]
            if not candidates and len(self.pending_spawns) == 1:
                candidates = [next(iter(self.pending_spawns.items()))]
            if candidates:
                spawn_key, pending_id = candidates[0]
                self.pending_spawns.pop(spawn_key, None)
                agent = self._rename_agent(pending_id, agent_id, event)
        if not agent:
            agent = AgentState(
                agent_id=agent_id,
                started_at=event_time(event),
                last_activity_at=event_time(event),
            )
            self.agents[agent_key] = agent

        timestamp = event_time(event)
        agent.started_at = min(agent.started_at, timestamp)
        agent.last_activity_at = max(agent.last_activity_at, timestamp)
        agent.agent_type = compact(event.get("agent_type"), 120)
        if agent.name == "subagent" and agent.agent_type:
            agent.name = agent.agent_type
        agent.session_id = compact(event.get("session_id"), 160) or agent.session_id
        agent.cwd = compact(event.get("cwd"), 1000) or agent.cwd

        session = self.sessions.get(agent.session_id)
        belongs_to_finished_turn = (
            session is not None
            and session.last_turn_finished_at is not None
            and timestamp <= session.last_turn_finished_at
        )
        if belongs_to_finished_turn:
            if agent.status not in TERMINAL_AGENT_STATUSES:
                agent.status = "interrupted"
                agent.finished_at = session.last_turn_finished_at
                self._finish_running_steps(agent)
        else:
            agent.status = "running"
            agent.finished_at = None

        turn_id = compact(event.get("turn_id"), 160)
        if turn_id:
            agent.turn_ids.add(turn_id)
            self.turn_to_agent[self._scoped(turn_id, event)] = agent_key
        transcript = compact(event.get("transcript_path"), 1000)
        if transcript:
            agent.transcript_paths.add(transcript)
            self.transcript_to_agent[transcript] = agent_key

    def _stop_agent(self, event: dict[str, Any]) -> None:
        agent_id = compact(event.get("agent_id") or event.get("subagent_id"), 160)
        if not agent_id:
            return
        agent_key = self._scoped(agent_id, event)
        agent = self.agents.get(agent_key)
        if not agent:
            agent = AgentState(
                agent_id=agent_id,
                started_at=event_time(event),
                last_activity_at=event_time(event),
            )
            self.agents[agent_key] = agent
        agent.status = "completed"
        agent.finished_at = event_time(event)
        result = compact(event.get("last_assistant_message"), 1000)
        if result:
            agent.result = result
        agent.last_activity_at = max(agent.last_activity_at, event_time(event))
        agent.agent_type = compact(event.get("agent_type"), 120) or agent.agent_type
        agent.session_id = compact(event.get("session_id"), 160) or agent.session_id
        agent.cwd = compact(event.get("cwd"), 1000) or agent.cwd
        transcript = compact(event.get("agent_transcript_path"), 1000)
        if transcript:
            agent.transcript_paths.add(transcript)
            self.transcript_to_agent[transcript] = agent_key
        self._finish_running_steps(agent)

    def _spawn_completed(self, event: dict[str, Any]) -> None:
        tool_id = compact(event.get("tool_use_id"), 160)
        spawn_key = self._scoped(tool_id, event)
        pending_id = self.pending_spawns.get(spawn_key)
        if not pending_id:
            return
        response = event.get("tool_response")
        agent_id = agent_id_from(response)
        if agent_id:
            agent = self._rename_agent(pending_id, agent_id, event)
            self.pending_spawns.pop(spawn_key, None)
            self._set_agent_status(agent_id, "running", event)
            response_name = first_nested(
                response,
                ("canonical_task_name", "task_name", "name", "agent_type"),
            )
            if response_name and agent.name == "subagent":
                agent.name = compact(response_name, 80)
            return
        # A spawn tool result without an agent identifier is a terminal
        # failure, not an indefinitely pending child. Codex reports failures
        # such as the thread limit as plain text.
        agent = self.agents.get(pending_id)
        self.pending_spawns.pop(spawn_key, None)
        if agent is not None:
            timestamp = event_time(event)
            agent.status = "failed"
            agent.finished_at = timestamp
            agent.last_activity_at = max(agent.last_activity_at, timestamp)
            agent.result = compact(response, 1000) or "子 Agent 创建失败。"
            self._finish_running_steps(agent)

    def _list_agents_completed(self, event: dict[str, Any]) -> None:
        response = event.get("tool_response")
        if not isinstance(response, dict):
            return
        agents = response.get("agents")
        if not isinstance(agents, list):
            return
        for item in agents:
            if not isinstance(item, dict):
                continue
            self._set_agent_status(
                item.get("agent_name") or item.get("agent_id"),
                item.get("agent_status") or item.get("status"),
                event,
            )

    def _interrupt_completed(self, event: dict[str, Any]) -> None:
        response = event.get("tool_response")
        if not isinstance(response, dict) or "previous_status" not in response:
            return
        target = target_from(event.get("tool_input"))
        if target:
            # A successful interrupt can only refer to an existing agent. Do
            # not synthesize a new record if Codex returned an unknown alias.
            self._set_agent_status(target, "interrupted", event, create=False)

    def _followup(self, event: dict[str, Any]) -> None:
        target = target_from(event.get("tool_input"))
        if not target:
            return
        agent = self._agent_for_target(target, event, create=False)
        if not agent:
            return
        if matches_tool(compact(event.get("tool_name"), 160), {"followup_task"}):
            self._set_agent_status(target, "running", event, create=False)
            _, followup = task_from_input(event.get("tool_input"))
            if followup:
                agent.task = f"{agent.task} / {followup}" if agent.task else followup

    def _tool_started(self, event: dict[str, Any]) -> None:
        tool = compact(event.get("tool_name"), 160) or "tool"
        if matches_tool(tool, AGENT_TOOL_NAMES):
            self._new_pending(event)
            return
        if matches_tool(tool, FOLLOWUP_TOOL_NAMES):
            self._followup(event)
            return
        agent = self._agent_for_event(event)
        if not agent:
            return
        if agent.status == "stale":
            agent.status = "running"
            agent.finished_at = None
        key = compact(event.get("tool_use_id") or f"step-{len(agent.steps) + 1}", 160)
        agent.steps[key] = Step(
            key=key,
            tool=tool,
            summary=compact(event.get("tool_input"), 500),
            started_at=event_time(event),
        )
        agent.last_activity_at = max(agent.last_activity_at, event_time(event))

    def _tool_completed(self, event: dict[str, Any]) -> None:
        tool = compact(event.get("tool_name"), 160) or "tool"
        if matches_tool(tool, AGENT_TOOL_NAMES):
            self._spawn_completed(event)
            return
        if matches_tool(tool, LIST_AGENT_TOOL_NAMES):
            self._list_agents_completed(event)
            return
        if matches_tool(tool, INTERRUPT_AGENT_TOOL_NAMES):
            self._interrupt_completed(event)
            return
        agent = self._agent_for_event(event)
        if not agent:
            return
        if agent.status == "stale":
            agent.status = "running"
            agent.finished_at = None
        key = compact(event.get("tool_use_id"), 160)
        step = agent.steps.get(key)
        if not step:
            step = Step(
                key=key or f"step-{len(agent.steps) + 1}",
                tool=tool,
                summary=compact(event.get("tool_input"), 500),
                started_at=event_time(event),
            )
            agent.steps[step.key] = step
        step.status = "completed"
        step.finished_at = event_time(event)
        step.output = compact(event.get("tool_response"), 500)
        agent.last_activity_at = max(agent.last_activity_at, event_time(event))

    def add_message(self, event: dict[str, Any], text: str, phase: str) -> None:
        if not text:
            return
        agent = self._agent_for_event(event)
        if not agent:
            self._start_agent(event)
            agent = self._agent_for_event(event)
        if not agent:
            return
        timestamp = event_time(event)
        key = compact(event.get("tool_use_id") or f"message-{timestamp}", 160)
        label = "最终答复" if phase == "final_answer" else "进度"
        agent.steps[key] = Step(
            key=key,
            tool=label,
            summary=compact(text, 700),
            started_at=timestamp,
            status="completed",
            output="",
            finished_at=timestamp,
        )
        agent.last_activity_at = max(agent.last_activity_at, timestamp)
        if (
            not agent.task
            or agent.task.startswith("任务标识:")
            or agent.task.startswith("{")
        ) and phase != "final_answer":
            agent.task = compact(text, 700)
        if phase == "final_answer":
            agent.result = compact(text, 1000)

    def apply(self, event: dict[str, Any]) -> None:
        self.last_event_at = event_time(event)
        self.touch_session(
            event.get("session_id"),
            timestamp=self.last_event_at,
            cwd=event.get("cwd"),
        )
        name = compact(event.get("hook_event_name"), 80)
        if name == "SubagentStart":
            self._start_agent(event)
        elif name == "SubagentStop":
            self._stop_agent(event)
        elif name == "PreToolUse":
            self._tool_started(event)
        elif name == "PostToolUse":
            self._tool_completed(event)

    def mark_stale(self, older_than_seconds: float) -> None:
        if older_than_seconds <= 0:
            return
        cutoff = time.time() - older_than_seconds
        for agent in self.agents.values():
            if (
                agent.status in ACTIVE_AGENT_STATUSES
                and agent.last_activity_at < cutoff
            ):
                agent.status = "stale"


class VisibilityStore:
    """Persist reversible dashboard-only hiding without touching Codex logs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.cleared_before = 0.0
        self.hidden_agents: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        cleared_before = data.get("cleared_before")
        if isinstance(cleared_before, (int, float)):
            self.cleared_before = float(cleared_before)
        hidden = data.get("hidden_agents")
        if isinstance(hidden, dict):
            self.hidden_agents = {
                compact(key, 400): float(value)
                for key, value in hidden.items()
                if isinstance(key, str) and isinstance(value, (int, float))
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cleared_before": self.cleared_before,
                    "hidden_agents": self.hidden_agents,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def is_visible(self, key: str, agent: AgentState) -> bool:
        cutoff = max(self.cleared_before, self.hidden_agents.get(key, 0.0))
        return agent.last_activity_at > cutoff

    def hidden_count(
        self, state: MonitorState, session_id: str | None = None
    ) -> int:
        return sum(
            is_visible_subagent(agent)
            and (session_id is None or (agent.session_id or UNSCOPED_SESSION) == session_id)
            and not self.is_visible(key, agent)
            for key, agent in state.agents.items()
        )

    def hide_agent(self, key: str, agent: AgentState) -> None:
        self.hidden_agents[key] = max(time.time(), agent.last_activity_at)
        self._save()

    def clear_session(self, state: MonitorState, session_id: str) -> int:
        targets = [
            (key, agent)
            for key, agent in state.agents.items()
            if (agent.session_id or UNSCOPED_SESSION) == session_id
            and is_visible_subagent(agent)
            and self.is_visible(key, agent)
        ]
        if not targets:
            return 0
        cutoff = max(time.time(), *(agent.last_activity_at for _, agent in targets))
        for key, _ in targets:
            self.hidden_agents[key] = cutoff
        self._save()
        return len(targets)

    def clear_all(self, state: MonitorState) -> int:
        targets = [
            (key, agent)
            for key, agent in state.agents.items()
            if is_visible_subagent(agent)
        ]
        if not targets:
            return 0
        self.cleared_before = max(
            time.time(),
            self.cleared_before,
            *self.hidden_agents.values(),
            *(agent.last_activity_at for _, agent in targets),
        )
        self.hidden_agents.clear()
        self._save()
        return len(targets)

    def restore(self) -> int:
        count = len(self.hidden_agents) + int(self.cleared_before > 0)
        self.cleared_before = 0.0
        self.hidden_agents.clear()
        self._save()
        return count


def visible_agent_entries(
    state: MonitorState, visibility: VisibilityStore | None = None
) -> list[tuple[str, AgentState]]:
    return [
        (key, agent)
        for key, agent in state.agents.items()
        if is_visible_subagent(agent)
        and (visibility is None or visibility.is_visible(key, agent))
    ]


def duration_span_text(value: float) -> str:
    seconds = max(0, int(value))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def duration_text(agent: AgentState, current: float) -> str:
    """Elapsed Agent span: live while active, frozen once work has ended."""
    if agent.finished_at is not None:
        end = agent.finished_at
    elif agent.status in ACTIVE_AGENT_STATUSES:
        end = current
    else:
        # A stale record has no explicit terminal event.  Freeze it at the
        # last observed activity instead of letting its duration grow forever.
        end = agent.last_activity_at
    return duration_span_text(end - agent.started_at)


def duration_label(agent: AgentState) -> str:
    """Describe whether the displayed Agent span is still live or final."""
    return "已运行" if agent.status in ACTIVE_AGENT_STATUSES else "总耗时"


def terminal_dimensions() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 32))
    return max(64, min(140, size.columns)), max(20, size.lines)


def style(text: str, code: str, colors: bool) -> str:
    return f"{code}{text}{Color.RESET}" if colors else text


def wrap_line(prefix: str, text: str, width: int, subsequent: str = "") -> list[str]:
    available = max(16, width - len(prefix))
    chunks = textwrap.wrap(text or "-", width=available) or ["-"]
    lines = [prefix + chunks[0]]
    pad = subsequent or " " * len(prefix)
    lines.extend(pad + chunk for chunk in chunks[1:])
    return lines


def render_agent(agent: AgentState, width: int, colors: bool, max_steps: int) -> list[str]:
    current = time.time()
    if agent.status in ACTIVE_AGENT_STATUSES:
        marker = style("●", Color.GREEN if agent.status == "running" else Color.YELLOW, colors)
    elif agent.status == "failed":
        marker = style("✗", Color.RED, colors)
    elif agent.status in {"interrupted", "cancelled"}:
        marker = style("■", Color.YELLOW, colors)
    else:
        marker = style("✓", Color.CYAN, colors)
    kind = "[外部任务] " if agent.worker_kind == "external_task" else ""
    title = f"{marker} {kind}{style(agent.name, Color.BOLD, colors)}"
    title += "  " + style(
        f"{short_id(agent.agent_id)} · {duration_label(agent)} {duration_text(agent, current)}",
        Color.DIM,
        colors,
    )
    lines = [title]
    if agent.cwd:
        lines.extend(
            wrap_line("  项目: ", compact(agent.cwd, width), width, "        ")
        )
    lines.extend(
        wrap_line("  任务: ", compact(agent.task, width * 2), width, "        ")
    )
    recent_steps = list(agent.steps.values())[-max_steps:]
    if recent_steps:
        lines.append("  " + style("步骤:", Color.DIM, colors))
        for step in recent_steps:
            icon = "…" if step.status == "running" else "✓" if step.status == "completed" else "?"
            summary = f"{icon} {step.tool}"
            if step.summary:
                summary += f" — {step.summary}"
            summary = compact(summary, width * 2)
            lines.extend(wrap_line("    ", summary, width, "      "))
    else:
        lines.append("  " + style("步骤: 等待工具调用", Color.DIM, colors))
    if agent.status in TERMINAL_AGENT_STATUSES and agent.result:
        lines.extend(
            wrap_line("  结果: ", compact(agent.result, width * 2), width, "        ")
        )
    return lines


def render(
    state: MonitorState,
    log_path: Path,
    *,
    colors: bool,
    max_steps: int,
    completed_limit: int,
    visibility: VisibilityStore | None = None,
) -> str:
    width, height = terminal_dimensions()
    current = time.time()
    visible_agents = [agent for _, agent in visible_agent_entries(state, visibility)]
    running = [a for a in visible_agents if a.status in ACTIVE_AGENT_STATUSES]
    stale = [a for a in visible_agents if a.status == "stale"]
    running.sort(key=lambda a: a.last_activity_at, reverse=True)
    completed_all = [a for a in visible_agents if a.status in TERMINAL_AGENT_STATUSES]
    completed_all.sort(key=lambda a: a.finished_at or 0, reverse=True)
    completed = completed_all if completed_limit < 0 else completed_all[:completed_limit]

    lines = [
        style("Codex 子 Agent 监控器", Color.BOLD, colors),
        style(
            f"{len(running)} 个运行中 · {len(completed_all)} 个已结束 · {datetime.now().strftime('%H:%M:%S')}",
            Color.DIM,
            colors,
        ),
        "─" * width,
        style("运行中", Color.BOLD, colors),
    ]
    if running:
        for index, agent in enumerate(running):
            if index:
                lines.append("")
            lines.extend(render_agent(agent, width, colors, max_steps))
    else:
        lines.append("  " + style("当前没有运行中的子 Agent。", Color.DIM, colors))

    lines.extend(["", style("已结束", Color.BOLD, colors)])
    shown_completed = 0
    if completed:
        content_limit = max(12, height - 4)
        for agent in completed:
            block = render_agent(agent, width, colors, max_steps)
            separator_lines = 1 if shown_completed else 0
            available = content_limit - len(lines) - separator_lines
            if available < 3:
                break
            if shown_completed:
                lines.append("")
            truncated = len(block) > available
            if truncated:
                block = block[: max(1, available - 1)]
                block.append("  " + style("…详情已按终端高度截断", Color.DIM, colors))
            lines.extend(block)
            shown_completed += 1
            if truncated:
                break
        hidden = len(completed_all) - shown_completed
        if hidden > 0:
            lines.append("  " + style(f"另有 {hidden} 个已结束 Agent 未展开。", Color.DIM, colors))
    else:
        lines.append("  " + style("尚未记录已结束的子 Agent。", Color.DIM, colors))

    hidden_count = visibility.hidden_count(state) if visibility else 0
    if not visible_agents:
        lines.extend(
            [
                "",
                style(
                    (
                        f"当前有 {hidden_count} 条子 Agent 记录已隐藏；"
                        "运行 codex-agents --restore-hidden 可恢复。"
                        if hidden_count
                        else "正在扫描 Hooks 和 Codex sessions，尚未发现子 Agent。"
                    ),
                    Color.DIM,
                    colors,
                ),
                style(
                    "已在运行的旧会话也会从 ~/.codex/sessions 自动发现。",
                    Color.DIM,
                    colors,
                ),
            ]
        )
    last_age = "从未"
    if state.last_event_at is not None:
        last_age = f"{max(0, int(current - state.last_event_at))} 秒前"
    lines.extend(
        [
            "",
            "─" * width,
            style(
                f"数据源: Hooks + sessions · 隐藏: {hidden_count} · 忽略陈旧未闭合: {len(stale)} · 最后事件: {last_age} · Ctrl-C 退出",
                Color.DIM,
                colors,
            ),
        ]
    )
    return "\n".join(lines)


def load_lines(lines: Iterable[str], state: MonitorState) -> None:
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            state.malformed_lines += 1
            continue
        if isinstance(event, dict):
            state.apply(event)


@dataclass
class FileCursor:
    offset: int = 0
    partial: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class JsonlTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0
        self.partial = ""

    def poll(self) -> list[str]:
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.partial = ""
            with self.path.open("r", encoding="utf-8") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except FileNotFoundError:
            chunk = ""
        complete: list[str] = []
        if chunk:
            data = self.partial + chunk
            pieces = data.splitlines(keepends=True)
            self.partial = ""
            for piece in pieces:
                if piece.endswith(("\n", "\r")):
                    complete.append(piece)
                else:
                    self.partial = piece
        return complete


class RolloutScanner:
    """Incrementally translate Codex rollout JSONL into monitor events.

    Hooks only affect sessions started after the configuration was loaded.
    Rollouts provide a read-only fallback for sessions that were already
    running, and also contain richer child-agent progress details.
    """

    STEP_ITEM_TYPES = {"CommandExecution", "FileChange", "ImageView"}

    def __init__(
        self,
        sessions_root: Path,
        state: MonitorState,
        *,
        history_hours: float = 24.0,
    ) -> None:
        self.sessions_root = sessions_root
        self.state = state
        self.history_seconds = max(0.0, history_hours) * 3600
        self.cursors: OrderedDict[Path, FileCursor] = OrderedDict()
        self.calls: dict[str, dict[str, Any]] = {}
        self.last_discovery = 0.0
        self.session_index_path = sessions_root.parent / "session_index.jsonl"
        self.session_index_offset = 0
        self.session_index_partial = ""
        self.session_titles: dict[str, str] = {}

    @staticmethod
    def _root_session(meta: dict[str, Any]) -> str:
        return compact(
            meta.get("session_id")
            or meta.get("parent_thread_id")
            or meta.get("id"),
            160,
        )

    @staticmethod
    def _is_child_meta(meta: dict[str, Any]) -> bool:
        if meta.get("thread_source") == "subagent" or meta.get("parent_thread_id"):
            return True
        source = meta.get("source")
        return isinstance(source, dict) and "subagent" in source

    @staticmethod
    def _child_event(
        meta: dict[str, Any], path: Path, timestamp: Any
    ) -> dict[str, Any]:
        return {
            "timestamp": timestamp,
            "session_id": RolloutScanner._root_session(meta),
            "agent_id": compact(meta.get("agent_path"), 160),
            "turn_id": compact(meta.get("id"), 160),
            "cwd": compact(meta.get("cwd"), 1000),
            "transcript_path": str(path),
            "agent_type": compact(meta.get("agent_nickname"), 120),
        }

    def _discover(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_discovery < 1.0:
            return
        self.last_discovery = now
        if not self.sessions_root.exists():
            return
        cutoff = now - self.history_seconds
        try:
            paths = sorted(self.sessions_root.rglob("*.jsonl"))
        except OSError:
            return
        for path in paths:
            if path in self.cursors:
                continue
            try:
                if self.history_seconds and path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            self.cursors[path] = FileCursor()

    def _poll_session_titles(self) -> int:
        """Read Codex's append-only session title index; the latest row wins."""
        try:
            size = self.session_index_path.stat().st_size
            if size < self.session_index_offset:
                self.session_index_offset = 0
                self.session_index_partial = ""
                self.session_titles.clear()
            with self.session_index_path.open("r", encoding="utf-8") as stream:
                stream.seek(self.session_index_offset)
                chunk = stream.read()
                self.session_index_offset = stream.tell()
        except (FileNotFoundError, OSError):
            chunk = ""

        if chunk:
            data = self.session_index_partial + chunk
            pieces = data.splitlines(keepends=True)
            self.session_index_partial = ""
            for piece in pieces:
                if not piece.endswith(("\n", "\r")):
                    self.session_index_partial = piece
                    continue
                try:
                    record = json.loads(piece)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = compact(record.get("id"), 160)
                title = compact(record.get("thread_name"), 120)
                if session_id and title:
                    self.session_titles[session_id] = title

        changed = 0
        for session_id, session in self.state.sessions.items():
            title = self.session_titles.get(session_id)
            if title and session.name != title:
                session.name = title
                changed += 1
            external = self.state.external_task(session_id)
            if external is not None and title and external.name != title:
                external.name = title
                changed += 1
        return changed

    def _ensure_child(
        self, meta: dict[str, Any], path: Path, timestamp: Any
    ) -> dict[str, Any] | None:
        if not self._is_child_meta(meta) or not meta.get("agent_path"):
            return None
        event = self._child_event(meta, path, timestamp)
        agent = self.state._agent_for_event(event)
        if agent is None or agent.status == "stale":
            self.state.apply({"hook_event_name": "SubagentStart", **event})
        return event

    def _ensure_external_task(
        self, meta: dict[str, Any], path: Path, timestamp: Any
    ) -> dict[str, Any] | None:
        """Return an event scoped to the dispatcher for a create_thread task."""
        if self._is_child_meta(meta):
            return None
        external_id = self._root_session(meta)
        agent = self.state.external_task(external_id)
        if agent is None:
            return None
        event = {
            "timestamp": timestamp,
            "session_id": agent.session_id,
            "agent_id": external_id,
            "turn_id": compact(meta.get("id"), 160) or external_id,
            "cwd": compact(meta.get("cwd"), 1000) or agent.cwd,
            "transcript_path": str(path),
        }
        event_at = parse_time(timestamp)
        should_start = agent.status in {"starting", "stale"}
        if (
            agent.status in TERMINAL_AGENT_STATUSES
            and agent.finished_at is not None
            and event_at > agent.finished_at
        ):
            should_start = True
        if should_start:
            self.state.apply({"hook_event_name": "SubagentStart", **event})
        return event

    @staticmethod
    def _step_from_item(item: dict[str, Any]) -> tuple[str, str, Any]:
        item_type = compact(item.get("type"), 120)
        if item_type == "CommandExecution":
            command = item.get("command")
            if isinstance(command, list) and command:
                summary = compact(command[-1], 700)
            else:
                summary = compact(command, 700)
            response = {
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
            }
            return "Shell", summary, response
        if item_type == "FileChange":
            changes = item.get("changes") or {}
            paths = list(changes) if isinstance(changes, dict) else changes
            return "文件修改", compact(paths, 700), {"status": item.get("status")}
        if item_type == "ImageView":
            return "查看图片", compact(item.get("path"), 700), "completed"
        return item_type or "tool", compact(item, 700), "completed"

    @staticmethod
    def _nested_tool(source: Any) -> tuple[str, Any]:
        if not isinstance(source, str):
            return "tool", source
        match = re.search(r"tools\.([A-Za-z0-9_]+)\(", source)
        if not match:
            return "tool", compact(source, 500)
        tool = match.group(1)
        rest = source[match.end() :].lstrip()
        try:
            value, _ = json.JSONDecoder().raw_decode(rest)
        except (json.JSONDecodeError, TypeError):
            value = compact(rest, 500)
        if tool == "exec_command" and isinstance(value, dict):
            value = value.get("cmd") or value
        elif tool == "apply_patch" and isinstance(value, str):
            paths = re.findall(r"\*\*\* (?:Add|Update|Delete) File: ([^\n]+)", value)
            value = {"文件": paths} if paths else compact(value, 500)
        return tool, value

    @staticmethod
    def _prompt_from_handoff_item(item: dict[str, Any]) -> tuple[str, str]:
        """Recover expanded instructions when a child reads a handoff file."""
        if item.get("type") != "CommandExecution":
            return "", ""
        paths: list[str] = []
        parsed = item.get("parsed_cmd")
        if isinstance(parsed, list):
            for action in parsed:
                if not isinstance(action, dict) or action.get("type") != "read":
                    continue
                path = multiline_text(action.get("path"), 2000)
                if path:
                    paths.append(path)
        command = item.get("command")
        if isinstance(command, list) and command:
            command_text = str(command[-1])
        else:
            command_text = str(command or "")
        candidates = paths or re.findall(r"(?:^|\s)([^\s'\"]*handoffs?/[^\s'\"]+)", command_text)
        handoff_paths = [
            path
            for path in candidates
            if re.search(r"(?:^|/)handoffs?(?:/|$)", path, re.IGNORECASE)
        ]
        if not handoff_paths:
            return "", ""
        output = item.get("stdout") or item.get("aggregated_output")
        prompt = multiline_text(output)
        if not prompt:
            return "", ""
        return prompt, f"交接文件: {handoff_paths[0]}"

    def _handle_response_item(
        self,
        payload: dict[str, Any],
        timestamp: Any,
        path: Path,
        cursor: FileCursor,
    ) -> int:
        payload_type = payload.get("type")
        meta = cursor.meta
        root_session = self._root_session(meta)

        if payload_type == "function_call" and payload.get("namespace") == "collaboration":
            call_id = compact(payload.get("call_id"), 160)
            tool_name = compact(payload.get("name"), 160)
            arguments = json_dict(payload.get("arguments"))
            event = {
                "hook_event_name": "PreToolUse",
                "timestamp": timestamp,
                "session_id": root_session,
                "cwd": compact(meta.get("cwd"), 1000),
                "tool_name": tool_name,
                "tool_use_id": call_id,
                "tool_input": arguments,
            }
            self.calls[call_id] = event
            self.state.apply(event)
            return 1

        if payload_type == "function_call_output":
            call_id = compact(payload.get("call_id"), 160)
            original = self.calls.pop(call_id, None)
            if original:
                response = json_dict(payload.get("output")) or payload.get("output")
                self.state.apply(
                    {
                        **original,
                        "hook_event_name": "PostToolUse",
                        "timestamp": timestamp,
                        "tool_response": response,
                    }
                )
                return 1

        if self._is_child_meta(meta) and meta.get("agent_path"):
            worker = self._ensure_child(meta, path, timestamp)
        else:
            worker = self._ensure_external_task(meta, path, timestamp)
        if worker is None:
            return 0
        if payload_type == "agent_message":
            prompt, encrypted = delegation_prompt_from_agent_message(
                payload.get("content")
            )
            agent = self.state._agent_for_event(worker)
            if agent is not None:
                self.state._set_delegation_prompt(
                    agent,
                    prompt,
                    "子 Agent 入站任务",
                    encrypted=encrypted,
                )
                return int(bool(prompt) or encrypted)
        if payload_type == "message" and payload.get("role") == "assistant":
            text = message_text(payload.get("content"))
            phase = compact(payload.get("phase"), 80) or "commentary"
            if text:
                self.state.add_message(
                    {
                        **worker,
                        "tool_use_id": payload.get("id"),
                    },
                    text,
                    phase,
                )
                return 1
        if payload_type == "custom_tool_call":
            tool, tool_input = self._nested_tool(payload.get("input"))
            self.state.apply(
                {
                    **worker,
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool,
                    "tool_use_id": f"rollout-call:{payload.get('call_id')}",
                    "tool_input": tool_input,
                }
            )
            return 1
        if payload_type == "custom_tool_call_output":
            call_id = f"rollout-call:{payload.get('call_id')}"
            agent = self.state._agent_for_event(worker)
            if agent and call_id in agent.steps:
                # Concrete CommandExecution/FileChange/ImageView records are
                # persisted alongside the generic code-mode call. Remove the
                # temporary running row once the call finishes.
                agent.steps.pop(call_id, None)
                agent.last_activity_at = max(agent.last_activity_at, parse_time(timestamp))
                return 1
        return 0

    def _handle_event_item(
        self,
        payload: dict[str, Any],
        timestamp: Any,
        path: Path,
        cursor: FileCursor,
    ) -> int:
        item = payload.get("item")
        if not isinstance(item, dict):
            return 0
        item_type = item.get("type")
        meta = cursor.meta
        root_session = self._root_session(meta)

        if (
            item_type == "McpToolCall"
            and item.get("server") == "codex_tui"
            and item.get("tool") == "create_thread"
        ):
            external_id = thread_id_from_mcp_result(item.get("result"))
            arguments = item.get("arguments")
            if external_id and isinstance(arguments, dict):
                created_at = parse_millisecond_time(
                    payload.get("completed_at_ms") or item.get("completed_at_ms"),
                    timestamp,
                )
                self.state.register_external_task(
                    root_session,
                    external_id,
                    title=arguments.get("title"),
                    prompt=arguments.get("prompt"),
                    timestamp=created_at,
                    cwd=meta.get("cwd"),
                )
                return 1

        if item_type == "SubAgentActivity":
            kind = item.get("kind")
            # Child agents can send messages back to the root agent. Those
            # interactions are recorded with agent_path=/root and are not a
            # child lifecycle entry for this dashboard.
            if item.get("agent_path") in {None, "", "/root"}:
                return 0
            event = {
                "timestamp": timestamp,
                "session_id": root_session,
                "agent_id": item.get("agent_path"),
                "turn_id": item.get("agent_thread_id"),
                "cwd": meta.get("cwd"),
            }
            if kind in {"started", "interacted"}:
                self.state.apply({"hook_event_name": "SubagentStart", **event})
            elif kind == "completed":
                self.state.apply({"hook_event_name": "SubagentStop", **event})
            elif kind in {"interrupted", "cancelled", "failed"}:
                self.state._set_agent_status(
                    item.get("agent_path"), kind, event, create=True
                )
            return 1

        if item_type not in self.STEP_ITEM_TYPES:
            return 0
        started_at = parse_millisecond_time(
            payload.get("started_at_ms") or item.get("started_at_ms"), timestamp
        )
        completed_at = parse_millisecond_time(
            payload.get("completed_at_ms") or item.get("completed_at_ms"), timestamp
        )
        worker = self._ensure_child(meta, path, started_at)
        if worker is None:
            worker = self._ensure_external_task(meta, path, started_at)
        if worker is None:
            return 0
        handoff_prompt, handoff_source = self._prompt_from_handoff_item(item)
        if handoff_prompt:
            agent = self.state._agent_for_event(worker)
            if agent is not None:
                self.state._set_delegation_prompt(
                    agent,
                    handoff_prompt,
                    handoff_source,
                    prefer_expanded=True,
                )
        tool, summary, response = self._step_from_item(item)
        common = {
            **worker,
            "tool_name": tool,
            "tool_use_id": item.get("id"),
            "tool_input": summary,
        }
        self.state.apply(
            {
                **common,
                "hook_event_name": "PreToolUse",
                "timestamp": started_at,
            }
        )
        if payload.get("type") == "item_started":
            return 1
        self.state.apply(
            {
                **common,
                "hook_event_name": "PostToolUse",
                "timestamp": completed_at,
                "tool_response": response,
            }
        )
        return 1

    def _handle_line(self, line: str, path: Path, cursor: FileCursor) -> int:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return 0
        if not isinstance(record, dict):
            return 0
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return 0
        timestamp = record.get("timestamp")
        record_type = record.get("type")
        if record_type == "session_meta":
            cursor.meta = payload
            self.state.touch_session(
                self._root_session(payload),
                timestamp=timestamp,
                cwd=payload.get("cwd"),
                name=(
                    ""
                    if self._is_child_meta(payload)
                    else session_name_from_meta(payload)
                ),
            )
            self._ensure_external_task(payload, path, timestamp)
            return 1
        self.state.touch_session(
            self._root_session(cursor.meta),
            timestamp=timestamp,
            cwd=cursor.meta.get("cwd"),
        )
        if record_type == "response_item":
            return self._handle_response_item(payload, timestamp, path, cursor)
        if record_type == "event_msg":
            changed = self._handle_event_item(payload, timestamp, path, cursor)
            if payload.get("type") == "task_complete":
                if self._is_child_meta(cursor.meta) and cursor.meta.get("agent_path"):
                    child = self._child_event(cursor.meta, path, timestamp)
                    agent = self.state._agent_for_event(child)
                    self.state.apply(
                        {
                            **child,
                            "hook_event_name": "SubagentStop",
                            "last_assistant_message": agent.result if agent else "",
                        }
                    )
                    changed += 1
                elif self.state.external_task(self._root_session(cursor.meta)):
                    changed += self.state.finish_external_task(
                        self._root_session(cursor.meta), timestamp
                    )
                else:
                    changed += self.state.finish_session_turn(
                        self._root_session(cursor.meta), timestamp
                    )
            return changed
        return 0

    def poll(self, *, force_discovery: bool = False) -> int:
        self._discover(force=force_discovery)
        changed = 0
        for path, cursor in list(self.cursors.items()):
            try:
                size = path.stat().st_size
                if size < cursor.offset:
                    cursor.offset = 0
                    cursor.partial = ""
                    cursor.meta = {}
                with path.open("r", encoding="utf-8") as stream:
                    stream.seek(cursor.offset)
                    chunk = stream.read()
                    cursor.offset = stream.tell()
            except (FileNotFoundError, OSError):
                continue
            if not chunk:
                continue
            data = cursor.partial + chunk
            pieces = data.splitlines(keepends=True)
            cursor.partial = ""
            for piece in pieces:
                if piece.endswith(("\n", "\r")):
                    changed += self._handle_line(piece, path, cursor)
                else:
                    cursor.partial = piece
        return changed + self._poll_session_titles()


UNSCOPED_SESSION = "__unscoped__"


def display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def clip_display(value: str, width: int, suffix: str = "") -> str:
    if width <= 0:
        return ""
    if display_width(value) <= width:
        return value
    suffix_width = display_width(suffix)
    target = max(0, width - suffix_width)
    result: list[str] = []
    used = 0
    for character in value:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        )
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    return "".join(result) + suffix


def pad_display(value: str, width: int) -> str:
    clipped = clip_display(value, width)
    return clipped + " " * max(0, width - display_width(clipped))


def wrap_display(value: str, width: int) -> list[str]:
    """Wrap text by terminal cell width, including CJK wide characters."""
    if width <= 0:
        return [""]
    wrapped: list[str] = []
    logical_lines = str(value or "-").splitlines() or [""]
    for logical in logical_lines:
        current: list[str] = []
        used = 0
        for character in logical:
            character_width = 0 if unicodedata.combining(character) else (
                2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
            )
            if current and used + character_width > width:
                wrapped.append("".join(current))
                current = []
                used = 0
            current.append(character)
            used += character_width
        wrapped.append("".join(current))
    return wrapped or [""]


def age_text(timestamp: float, current: float | None = None) -> str:
    seconds = max(0, int((current or time.time()) - timestamp))
    if seconds < 60:
        return f"{seconds}秒前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def project_name(cwd: str) -> str:
    if not cwd:
        return "未知项目"
    path = Path(cwd)
    return path.name or str(path)


def session_display_name(session: SessionState) -> str:
    if session.name:
        return session.name
    started = datetime.fromtimestamp(session.started_at).strftime("%m-%d %H:%M")
    return f"未命名 Session · {project_name(session.cwd)} · {started}"


class TuiController:
    """Navigation state kept separate from background data refreshes."""

    def __init__(self, visibility: VisibilityStore | None = None) -> None:
        self.visibility = visibility
        self.page = "sessions"
        self.selected_session_id: str | None = None
        self.selected_agent_key: str | None = None
        self.session_offset = 0
        self.agent_offset = 0
        self.detail_scroll = 0

    def session_items(self, state: MonitorState) -> list[SessionState]:
        # A create_thread target is a real top-level Codex session, but it is
        # already represented under its dispatcher as an external task. Hide
        # the duplicate home-page row while preserving the real task ID.
        sessions: dict[str, SessionState] = {
            session_id: session
            for session_id, session in state.sessions.items()
            if session_id not in state.external_task_keys
        }
        unscoped = [
            agent
            for key, agent in visible_agent_entries(state, self.visibility)
            if not agent.session_id
        ]
        if unscoped:
            sessions[UNSCOPED_SESSION] = SessionState(
                session_id=UNSCOPED_SESSION,
                cwd=next((agent.cwd for agent in unscoped if agent.cwd), ""),
                started_at=min(agent.started_at for agent in unscoped),
                last_activity_at=max(agent.last_activity_at for agent in unscoped),
            )
        running_sessions = {
            agent.session_id or UNSCOPED_SESSION
            for _, agent in visible_agent_entries(state, self.visibility)
            if agent.status in ACTIVE_AGENT_STATUSES
        }
        return sorted(
            sessions.values(),
            key=lambda session: (
                session.session_id in running_sessions,
                session.last_activity_at,
            ),
            reverse=True,
        )

    def agent_items(self, state: MonitorState) -> list[tuple[str, AgentState]]:
        session_id = self.selected_session_id
        items = [
            (key, agent)
            for key, agent in state.agents.items()
            if (agent.session_id or UNSCOPED_SESSION) == session_id
            and is_visible_subagent(agent)
            and (self.visibility is None or self.visibility.is_visible(key, agent))
        ]
        status_rank = {
            "running": 5,
            "starting": 4,
            "failed": 3,
            "interrupted": 2,
            "cancelled": 2,
            "completed": 1,
            "stale": 0,
        }
        return sorted(
            items,
            key=lambda item: (
                item[1].worker_kind == "external_task",
                status_rank.get(item[1].status, 0),
                item[1].last_activity_at,
            ),
            reverse=True,
        )

    def normalize(self, state: MonitorState) -> None:
        sessions = self.session_items(state)
        session_ids = [session.session_id for session in sessions]
        if self.selected_session_id not in session_ids:
            self.selected_session_id = session_ids[0] if session_ids else None
            self.selected_agent_key = None
            self.session_offset = 0
        if self.page in {"agents", "detail"} and self.selected_session_id is None:
            self.page = "sessions"
        agents = self.agent_items(state) if self.selected_session_id else []
        agent_keys = [key for key, _ in agents]
        if self.selected_agent_key not in agent_keys:
            self.selected_agent_key = agent_keys[0] if agent_keys else None
            self.agent_offset = 0
            self.detail_scroll = 0
        if self.page == "detail" and self.selected_agent_key is None:
            self.page = "agents"

    def move(self, state: MonitorState, delta: int) -> None:
        self.normalize(state)
        if self.page == "sessions":
            items = [session.session_id for session in self.session_items(state)]
            current = self.selected_session_id
        elif self.page == "agents":
            items = [key for key, _ in self.agent_items(state)]
            current = self.selected_agent_key
        else:
            return
        if not items:
            return
        index = items.index(current) if current in items else 0
        index = max(0, min(len(items) - 1, index + delta))
        if self.page == "sessions":
            if self.selected_session_id != items[index]:
                self.selected_agent_key = None
                self.agent_offset = 0
            self.selected_session_id = items[index]
        else:
            self.selected_agent_key = items[index]

    def enter(self, state: MonitorState) -> None:
        self.normalize(state)
        if self.page == "sessions" and self.selected_session_id:
            self.page = "agents"
            self.agent_offset = 0
        elif self.page == "agents" and self.selected_agent_key:
            self.page = "detail"
            self.detail_scroll = 0

    def back(self) -> bool:
        """Return False only when q/back should exit from the home page."""
        if self.page == "detail":
            self.page = "agents"
            return True
        if self.page == "agents":
            self.page = "sessions"
            return True
        return False

    def current_agent(self, state: MonitorState) -> AgentState | None:
        return state.agents.get(self.selected_agent_key or "")


def agents_for_session(
    state: MonitorState,
    session_id: str,
    visibility: VisibilityStore | None = None,
) -> list[AgentState]:
    return [
        agent
        for key, agent in state.agents.items()
        if (agent.session_id or UNSCOPED_SESSION) == session_id
        and is_visible_subagent(agent)
        and (visibility is None or visibility.is_visible(key, agent))
    ]


def session_timeline_origin(state: MonitorState, session_id: str) -> float | None:
    """Return the first real child start for a stable per-session timeline."""
    agents = agents_for_session(state, session_id)
    if not agents:
        return None
    return min(agent.started_at for agent in agents)


def timeline_timestamp_text(
    agent: AgentState, origin: float | None, current: float
) -> str:
    """Format when an Agent ended relative to the session's first child start."""
    if origin is None:
        origin = agent.started_at
    if agent.finished_at is not None:
        end = agent.finished_at
    elif agent.status in ACTIVE_AGENT_STATUSES:
        end = current
    else:
        end = agent.last_activity_at
    elapsed = max(0, int(end - origin))
    minutes, seconds = divmod(elapsed, 60)
    return f"{minutes}m {seconds:02d}s"


def agent_list_timeline_text(
    agent: AgentState, origin: float | None, current: float
) -> str:
    """Keep the requested step count and timeline timestamp adjacent."""
    return (
        f"{len(agent.steps)}步  时间戳 "
        f"{timeline_timestamp_text(agent, origin, current)}"
    )


def agent_status_label(status: str) -> tuple[str, str]:
    labels = {
        "running": ("● 运行中", "green"),
        "starting": ("… 启动中", "yellow"),
        "completed": ("✓ 已完成", "cyan"),
        "failed": ("✗ 启动失败", "red"),
        "interrupted": ("■ 已中断", "yellow"),
        "cancelled": ("■ 已取消", "yellow"),
        "stale": ("? 已陈旧", "dim"),
    }
    return labels.get(status, (f"? {status}", "dim"))


def session_status(
    state: MonitorState,
    session: SessionState,
    current: float | None = None,
    visibility: VisibilityStore | None = None,
) -> tuple[str, str]:
    now = current or time.time()
    agents = agents_for_session(state, session.session_id, visibility)
    if any(agent.status in ACTIVE_AGENT_STATUSES for agent in agents):
        return "● Agent 运行中", "green"
    if (
        session.last_turn_finished_at is not None
        and session.last_activity_at <= session.last_turn_finished_at
    ):
        return "✓ 回合已结束", "cyan"
    if now - session.last_activity_at <= 300:
        return "◆ 会话活跃", "cyan"
    return "○ 会话空闲", "dim"


def step_duration_text(step: Step, current: float) -> str:
    end = step.finished_at if step.finished_at is not None else current
    duration = max(0.0, end - step.started_at)
    if duration < 1:
        if duration <= 0:
            return "<1s"
        return f"{max(1, round(duration * 1000))}ms"
    seconds = int(duration)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s"


def detail_lines(
    agent: AgentState, width: int, current: float | None = None
) -> list[tuple[str, str]]:
    usable = max(20, width)
    current = time.time() if current is None else current
    status, status_style = agent_status_label(agent.status)
    lines: list[tuple[str, str]] = [
        (
            f"状态: {status}    Agent {duration_label(agent)}: {duration_text(agent, current)}",
            status_style,
        ),
        (
            f"类型: {'外部调度任务（独立 Session）' if agent.worker_kind == 'external_task' else '子 Agent'}",
            "bold" if agent.worker_kind == "external_task" else "dim",
        ),
        (f"Agent: {agent.name}    ID: {agent.agent_id}", "bold"),
    ]
    if agent.worker_kind == "external_task":
        lines.extend(
            [
                (f"归属主 Session: {agent.session_id or '未标识'}", "dim"),
                (f"任务独立 Session: {agent.external_session_id or agent.agent_id}", "dim"),
            ]
        )
    else:
        lines.append((f"Session: {agent.session_id or '未标识'}", "dim"))
    if agent.cwd:
        lines.extend((line, "normal") for line in wrap_display(f"项目: {agent.cwd}", usable))
    lines.extend((line, "normal") for line in wrap_display(f"任务: {agent.task or '-'}", usable))
    lines.extend([("", "normal"), ("主 Agent 创建提示词", "bold")])
    if agent.delegation_prompt:
        source = agent.delegation_prompt_source or "本地任务记录"
        lines.append((f"来源: {source}", "dim"))
        lines.extend(
            (line, "normal")
            for line in wrap_display(agent.delegation_prompt, usable)
        )
    elif agent.delegation_prompt_encrypted:
        lines.extend(
            (line, "dim")
            for line in wrap_display(
                "Codex 在本地历史中只保存了这段提示词的密文，无法从历史记录还原明文。"
                "如果子 Agent 读取 handoffs/ 交接文件，监控器会自动在这里回填文件中的完整指令。",
                usable,
            )
        )
    else:
        lines.append(("本地记录中未捕获到创建提示词。", "dim"))
    lines.extend([("", "normal"), (f"执行记录 ({len(agent.steps)})", "bold"), ("─" * usable, "dim")])
    if not agent.steps:
        lines.append(("尚无执行步骤。", "dim"))
    for step in agent.steps.values():
        if step.status == "running":
            icon, line_style = "…", "yellow"
        elif step.status == "completed":
            icon, line_style = "✓", "green"
        else:
            icon, line_style = "?", "dim"
        timestamp = datetime.fromtimestamp(step.started_at).strftime("%H:%M:%S")
        lines.append(
            (
                f"[{timestamp}] {icon} {step.tool}  ({step_duration_text(step, current)})",
                line_style,
            )
        )
        if step.summary:
            lines.extend(
                (line, "normal")
                for line in wrap_display(f"  输入: {step.summary}", usable)
            )
        if step.output:
            lines.extend(
                (line, "dim")
                for line in wrap_display(f"  输出: {step.output}", usable)
            )
        lines.append(("", "normal"))
    if agent.result:
        lines.extend([("最终结果", "bold"), ("─" * usable, "dim")])
        lines.extend(
            (line, "normal") for line in wrap_display(agent.result, usable)
        )
    return lines


class CursesView:
    def __init__(self, screen: Any, *, colors: bool) -> None:
        self.screen = screen
        self.colors = colors and curses.has_colors()
        self.pairs: dict[str, int] = {}
        if self.colors:
            try:
                curses.start_color()
                curses.use_default_colors()
                definitions = {
                    "cyan": curses.COLOR_CYAN,
                    "green": curses.COLOR_GREEN,
                    "yellow": curses.COLOR_YELLOW,
                    "red": curses.COLOR_RED,
                }
                for index, (name, foreground) in enumerate(definitions.items(), 1):
                    curses.init_pair(index, foreground, -1)
                    self.pairs[name] = curses.color_pair(index)
            except curses.error:
                self.colors = False

    def attr(self, name: str, *, selected: bool = False) -> int:
        value = 0
        if name == "bold":
            value |= curses.A_BOLD
        elif name == "dim":
            value |= curses.A_DIM
        value |= self.pairs.get(name, 0)
        if selected:
            value |= curses.A_REVERSE
        return value

    def add(self, y: int, x: int, value: str, attr: int = 0, *, fill: bool = False) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        available = max(0, width - x - 1)
        text = pad_display(value, available) if fill else clip_display(value, available, "…")
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    def frame(self, title: str, subtitle: str, footer: str) -> tuple[int, int]:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.add(0, 0, f" {title}", self.attr("cyan") | curses.A_BOLD, fill=True)
        self.add(1, 1, subtitle, self.attr("dim"))
        if height > 2:
            self.add(height - 1, 0, f" {footer}", self.attr("dim"), fill=True)
        return height, width


def selected_index(values: list[str], selected: str | None) -> int:
    return values.index(selected) if selected in values else 0


def keep_visible(index: int, offset: int, visible: int) -> int:
    if index < offset:
        return index
    if index >= offset + visible:
        return index - visible + 1
    return max(0, offset)


def draw_sessions(view: CursesView, controller: TuiController, state: MonitorState) -> None:
    sessions = controller.session_items(state)
    running = sum(
        agent.status in ACTIVE_AGENT_STATUSES
        for _, agent in visible_agent_entries(state, controller.visibility)
    )
    hidden = controller.visibility.hidden_count(state) if controller.visibility else 0
    height, width = view.frame(
        "Codex Agent Monitor  /  Sessions",
        f"{len(sessions)} 个会话 · {running} 个运行中 · {hidden} 个已隐藏 · {datetime.now().strftime('%H:%M:%S')}",
        "Enter 展开  c 清当前  C 清全部  u 恢复  q 退出",
    )
    if height < 10 or width < 50:
        view.add(3, 2, "终端太小，请放大到至少 50×10。", view.attr("yellow"))
        return
    if not sessions:
        view.add(3, 2, "尚未发现 Codex session。", view.attr("dim"))
        view.add(5, 2, "监控器会持续扫描 ~/.codex/sessions 和用户级 Hooks。", view.attr("dim"))
        return
    visible = max(1, (height - 3) // 2)
    ids = [session.session_id for session in sessions]
    index = selected_index(ids, controller.selected_session_id)
    controller.session_offset = keep_visible(index, controller.session_offset, visible)
    shown = sessions[controller.session_offset : controller.session_offset + visible]
    now = time.time()
    for row_index, session in enumerate(shown):
        y = 2 + row_index * 2
        selected = session.session_id == controller.selected_session_id
        agents = agents_for_session(state, session.session_id, controller.visibility)
        active = sum(agent.status in ACTIVE_AGENT_STATUSES for agent in agents)
        ended = sum(agent.status in TERMINAL_AGENT_STATUSES for agent in agents)
        external = sum(agent.worker_kind == "external_task" for agent in agents)
        status, status_style = session_status(
            state, session, now, controller.visibility
        )
        session_label = (
            "未标识"
            if session.session_id == UNSCOPED_SESSION
            else short_id(session.session_id)
        )
        line = f" {'▶' if selected else ' '} {status}  {session_display_name(session)}"
        view.add(y, 0, line, view.attr(status_style, selected=selected), fill=True)
        supplement = (
            f"     项目: {project_name(session.cwd)} · {active}运行/{ended}结束"
            f" · {external}外部"
            f" · {age_text(session.last_activity_at, now)} · ID: {session_label}"
        )
        view.add(
            y + 1,
            0,
            supplement,
            view.attr("dim", selected=selected),
            fill=True,
        )


def draw_agents(view: CursesView, controller: TuiController, state: MonitorState) -> None:
    items = controller.agent_items(state)
    session = state.sessions.get(controller.selected_session_id or "")
    project = project_name(session.cwd) if session else "未标识会话"
    session_name = session_display_name(session) if session else "未标识会话"
    running = sum(agent.status in ACTIVE_AGENT_STATUSES for _, agent in items)
    external_count = sum(
        agent.worker_kind == "external_task" for _, agent in items
    )
    subagent_count = len(items) - external_count
    height, width = view.frame(
        f"Sessions  /  {session_name}  /  Agents",
        f"{subagent_count} 个子 Agent · {external_count} 个外部任务 · {running} 个运行中 · 项目 {project} · ID {short_id(controller.selected_session_id or '-')}",
        "Enter 详情  d 隐藏  c 清当前  C 清全部  u 恢复  q 返回",
    )
    if height < 10 or width < 50:
        view.add(3, 2, "终端太小，请放大到至少 50×10。", view.attr("yellow"))
        return
    if not items:
        hidden = (
            controller.visibility.hidden_count(
                state, controller.selected_session_id or ""
            )
            if controller.visibility
            else 0
        )
        message = (
            f"这个 session 有 {hidden} 条记录已隐藏，按 u 恢复。"
            if hidden
            else "这个 session 暂无子 Agent 记录。"
        )
        view.add(3, 2, message, view.attr("dim"))
        return
    # Reserve one line for each possible group heading. Headings are repeated
    # when a scrolled viewport starts in the middle of a group.
    visible = max(1, (height - 5) // 2)
    keys = [key for key, _ in items]
    index = selected_index(keys, controller.selected_agent_key)
    controller.agent_offset = keep_visible(index, controller.agent_offset, visible)
    shown = items[controller.agent_offset : controller.agent_offset + visible]
    now = time.time()
    timeline_origin = session_timeline_origin(
        state, controller.selected_session_id or UNSCOPED_SESSION
    )
    y = 2
    previous_kind = ""
    for key, agent in shown:
        if agent.worker_kind != previous_kind:
            if agent.worker_kind == "external_task":
                heading = f"── 外部调度任务 ({external_count}) ──"
            else:
                heading = f"── 子 Agent ({subagent_count}) ──"
            view.add(y, 2, heading, view.attr("bold"))
            y += 1
            previous_kind = agent.worker_kind
        if y + 1 >= height - 1:
            break
        selected = key == controller.selected_agent_key
        status, status_style = agent_status_label(agent.status)
        kind = "↗ 外部任务" if agent.worker_kind == "external_task" else "子 Agent"
        line = (
            f" {'▶' if selected else ' '} {status}  {kind}  {agent.name}  "
            f"[{short_id(agent.agent_id)}]  {duration_label(agent)} "
            f"{duration_text(agent, now)}"
        )
        view.add(y, 0, line, view.attr(status_style, selected=selected), fill=True)
        task = agent.task or "等待任务信息"
        if agent.worker_kind == "external_task":
            task = f"独立 Session {short_id(agent.external_session_id)} · {task}"
        supplement = agent_list_timeline_text(agent, timeline_origin, now)
        view.add(
            y + 1,
            0,
            f"     {supplement}  ·  {task}",
            view.attr("dim", selected=selected),
            fill=True,
        )
        y += 2


def draw_detail(view: CursesView, controller: TuiController, state: MonitorState) -> None:
    agent = controller.current_agent(state)
    title_name = agent.name if agent else "Agent"
    height, width = view.frame(
        f"Sessions  /  Agents  /  {title_name}",
        "详情页保持当前位置；后台刷新不会把你跳回顶部或底部",
        "↑↓ 滚动  d 隐藏  c 清当前  C 清全部  u 恢复  q 返回",
    )
    if agent is None:
        view.add(3, 2, "该 Agent 记录已不可用。", view.attr("yellow"))
        return
    content_height = max(1, height - 3)
    lines = detail_lines(agent, max(20, width - 3))
    maximum = max(0, len(lines) - content_height)
    controller.detail_scroll = max(0, min(controller.detail_scroll, maximum))
    for row, (line, line_style) in enumerate(
        lines[controller.detail_scroll : controller.detail_scroll + content_height],
        start=2,
    ):
        view.add(row, 1, line, view.attr(line_style))
    if maximum:
        progress = f" {controller.detail_scroll + 1}-{min(len(lines), controller.detail_scroll + content_height)}/{len(lines)} "
        view.add(1, max(1, width - display_width(progress) - 2), progress, view.attr("cyan"))


def draw_tui(view: CursesView, controller: TuiController, state: MonitorState) -> None:
    controller.normalize(state)
    if controller.page == "sessions":
        draw_sessions(view, controller, state)
    elif controller.page == "agents":
        draw_agents(view, controller, state)
    else:
        draw_detail(view, controller, state)
    view.screen.noutrefresh()
    curses.doupdate()


def run_tui(
    screen: Any,
    state: MonitorState,
    scanner: RolloutScanner | None,
    hook_tail: JsonlTail,
    args: argparse.Namespace,
    visibility: VisibilityStore,
) -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(max(50, int(args.poll * 1000)))
    controller = TuiController(visibility)
    view = CursesView(screen, colors=not args.no_color)
    needs_draw = True
    last_draw = 0.0
    force_discovery = False
    while True:
        now = time.monotonic()
        if needs_draw or now - last_draw >= 1.0:
            draw_tui(view, controller, state)
            last_draw = now
            needs_draw = False
        try:
            key = screen.get_wch()
        except curses.error:
            key = None
        if key is not None:
            needs_draw = True
            if key in ("q", "Q", "h", curses.KEY_LEFT, "\x1b"):
                if not controller.back():
                    return 0
            elif key == "d" and controller.page in {"agents", "detail"}:
                selected_key = controller.selected_agent_key
                agent = controller.current_agent(state)
                if selected_key and agent:
                    visibility.hide_agent(selected_key, agent)
                    if controller.page == "detail":
                        controller.page = "agents"
                    controller.normalize(state)
            elif key == "c" and controller.selected_session_id:
                visibility.clear_session(state, controller.selected_session_id)
                if controller.page == "detail":
                    controller.page = "agents"
                controller.normalize(state)
            elif key == "C":
                visibility.clear_all(state)
                if controller.page == "detail":
                    controller.page = "agents"
                controller.normalize(state)
            elif key == "u":
                visibility.restore()
                controller.normalize(state)
            elif key in ("\n", "\r", curses.KEY_ENTER, "l", curses.KEY_RIGHT):
                controller.enter(state)
            elif key in ("k", curses.KEY_UP):
                if controller.page == "detail":
                    controller.detail_scroll = max(0, controller.detail_scroll - 1)
                else:
                    controller.move(state, -1)
            elif key in ("j", curses.KEY_DOWN):
                if controller.page == "detail":
                    controller.detail_scroll += 1
                else:
                    controller.move(state, 1)
            elif key == curses.KEY_PPAGE:
                controller.detail_scroll = max(0, controller.detail_scroll - max(1, screen.getmaxyx()[0] - 4))
            elif key == curses.KEY_NPAGE:
                controller.detail_scroll += max(1, screen.getmaxyx()[0] - 4)
            elif key == "g" and controller.page == "detail":
                controller.detail_scroll = 0
            elif key == "G" and controller.page == "detail":
                agent = controller.current_agent(state)
                if agent:
                    controller.detail_scroll = max(
                        0,
                        len(detail_lines(agent, max(20, screen.getmaxyx()[1] - 3)))
                        - max(1, screen.getmaxyx()[0] - 3),
                    )
            elif key in ("r", "R"):
                force_discovery = True
            elif key == curses.KEY_RESIZE:
                pass

        hook_lines = hook_tail.poll()
        if hook_lines:
            load_lines(hook_lines, state)
            needs_draw = True
        if scanner is not None:
            changed = scanner.poll(force_discovery=force_discovery)
            force_discovery = False
            if changed:
                needs_draw = True
        state.mark_stale(args.stale_minutes * 60)


def demo_events() -> list[dict[str, Any]]:
    base = time.time() - 12

    def iso(value: float) -> str:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    return [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_use_id": "spawn-1",
            "tool_input": {
                "task_name": "test_review",
                "message": "Review the test suite and identify missing coverage.",
            },
            "_monitor_recorded_at": iso(base),
        },
        {
            "hook_event_name": "SubagentStart",
            "agent_id": "/root/test_review",
            "agent_type": "reviewer",
            "turn_id": "turn-child-1",
            "_monitor_recorded_at": iso(base + 1),
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
            "tool_input": {"command": "pytest -q"},
            "turn_id": "turn-child-1",
            "_monitor_recorded_at": iso(base + 2),
        },
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 0, "output": "42 passed"},
            "turn_id": "turn-child-1",
            "_monitor_recorded_at": iso(base + 5),
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_use_id": "tool-2",
            "tool_input": {"command": "Add missing regression tests"},
            "turn_id": "turn-child-1",
            "_monitor_recorded_at": iso(base + 7),
        },
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在终端中实时监控当前 WSL 用户的 Codex 子 Agent。"
    )
    default_state_home = Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_home / "codex-agent-monitor",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--once", action="store_true", help="输出一次当前状态后退出")
    parser.add_argument("--clear", action="store_true", help="清空 Hooks 事件缓存")
    parser.add_argument(
        "--clear-agents",
        action="store_true",
        help="隐藏当前全部子 Agent 记录后退出；不删除 Codex 日志",
    )
    parser.add_argument(
        "--restore-hidden",
        action="store_true",
        help="恢复此前隐藏的全部子 Agent 记录后退出",
    )
    parser.add_argument("--demo", action="store_true", help="显示演示面板后退出")
    parser.add_argument(
        "--native",
        action="store_true",
        help="改为打开 Codex 内置 Agent 控制中心",
    )
    parser.add_argument("--no-color", action="store_true", help="禁用 ANSI 颜色")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="禁用备用屏幕，以普通文本流输出",
    )
    parser.add_argument(
        "--no-sessions",
        action="store_true",
        help="只读取 Hooks，不扫描 Codex session rollout",
    )
    parser.add_argument(
        "--history-hours",
        type=float,
        default=12.0,
        help="初始扫描多少小时内更新的 Codex sessions",
    )
    parser.add_argument(
        "--stale-minutes",
        type=float,
        default=120.0,
        help="多少分钟无活动后将未闭合记录视为陈旧",
    )
    parser.add_argument("--max-steps", type=int, default=4, help="每个 Agent 显示的最近步骤数")
    parser.add_argument(
        "--completed",
        type=int,
        default=10,
        help="最多显示多少个已结束 Agent；-1 表示全部",
    )
    parser.add_argument("--poll", type=float, default=0.25, help="log polling interval in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    log_path = state_dir / "events.jsonl"
    visibility = VisibilityStore(state_dir / "visibility.json")

    if args.native:
        return subprocess.call(["codex", "agents", "-C", str(Path.cwd())])
    if args.clear:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        print(f"已清空 {log_path}")
        return 0
    if args.restore_hidden:
        visibility.restore()
        print("已恢复全部隐藏的子 Agent 记录。")
        return 0

    state = MonitorState()
    if args.demo:
        for event in demo_events():
            state.apply(event)
        print(
            render(
                state,
                log_path,
                colors=not args.no_color,
                max_steps=max(1, args.max_steps),
                completed_limit=args.completed,
                visibility=None,
            )
        )
        return 0

    if log_path.exists():
        load_lines(log_path.read_text(encoding="utf-8").splitlines(), state)
    scanner = None
    if not args.no_sessions:
        scanner = RolloutScanner(
            args.sessions_root.expanduser().resolve(),
            state,
            history_hours=args.history_hours,
        )
        scanner.poll(force_discovery=True)
        state.mark_stale(args.stale_minutes * 60)
    if args.clear_agents:
        count = visibility.clear_all(state)
        print(f"已隐藏 {count} 条当前子 Agent 记录；Codex 原始日志未删除。")
        print("可运行 codex-agents --restore-hidden 恢复。")
        return 0
    snapshot = render(
        state,
        log_path,
        colors=not args.no_color,
        max_steps=max(1, args.max_steps),
        completed_limit=args.completed,
        visibility=visibility,
    )
    if args.once:
        print(snapshot)
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    hook_tail = JsonlTail(log_path)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.plain
    if interactive:
        try:
            return curses.wrapper(
                run_tui, state, scanner, hook_tail, args, visibility
            )
        except KeyboardInterrupt:
            return 0

    colors = not args.no_color and sys.stdout.isatty()
    try:
        snapshot = render(
            state,
            log_path,
            colors=colors,
            max_steps=max(1, args.max_steps),
            completed_limit=args.completed,
            visibility=visibility,
        )
        sys.stdout.write(snapshot + "\n\n")
        sys.stdout.flush()
        last_snapshot = snapshot
        while True:
            lines = hook_tail.poll()
            load_lines(lines, state)
            changed = bool(lines)
            if scanner is not None:
                changed = bool(scanner.poll()) or changed
                state.mark_stale(args.stale_minutes * 60)
            if not changed:
                time.sleep(max(0.05, args.poll))
                continue
            snapshot = render(
                state,
                log_path,
                colors=colors,
                max_steps=max(1, args.max_steps),
                completed_limit=args.completed,
                visibility=visibility,
            )
            if snapshot == last_snapshot:
                time.sleep(max(0.05, args.poll))
                continue
            sys.stdout.write(snapshot + "\n\n")
            sys.stdout.flush()
            last_snapshot = snapshot
            time.sleep(max(0.05, args.poll))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
