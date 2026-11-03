#!/usr/bin/env python3
"""Regression tests for the global Codex subagent monitor."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = importlib.util.spec_from_file_location(
    "codex_agent_monitor", ROOT / "src" / "monitor.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def record(timestamp: str, record_type: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": record_type, "payload": payload},
        ensure_ascii=False,
    ) + "\n"


class RolloutScannerTests(unittest.TestCase):
    def test_rollout_step_uses_real_millisecond_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout-child.jsonl"
            started_ms = 1_793_606_400_000
            completed_ms = started_ms + 4_250
            rollout.write_text(
                record(
                    "2026-11-02T08:00:00Z",
                    "session_meta",
                    {
                        "id": "child-thread",
                        "session_id": "root-thread",
                        "parent_thread_id": "root-thread",
                        "thread_source": "subagent",
                        "agent_path": "/root/review",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-02T08:00:04.250Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": "command-real-duration",
                            "command": ["/bin/bash", "-lc", "sleep 4"],
                            "status": "completed",
                            "exit_code": 0,
                        },
                        "started_at_ms": started_ms,
                        "completed_at_ms": completed_ms,
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            step = state.agents["root-thread::/root/review"].steps[
                "command-real-duration"
            ]
            self.assertAlmostEqual(step.finished_at - step.started_at, 4.25)
            self.assertEqual(monitor.step_duration_text(step, step.finished_at), "4s")

    def test_subsecond_step_duration_is_not_displayed_as_zero(self) -> None:
        step = monitor.Step(
            key="quick",
            tool="Shell",
            summary="true",
            started_at=100.0,
            status="completed",
            finished_at=100.025,
        )
        self.assertEqual(monitor.step_duration_text(step, 100.025), "25ms")

    def test_failed_spawn_is_terminal_not_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout-root.jsonl"
            rollout.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "root-thread", "cwd": "/work/project"},
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "spawn-failed",
                        "arguments": json.dumps(
                            {"task_name": "review", "message": "Review it."}
                        ),
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "spawn-failed",
                        "output": "collab spawn failed: agent thread limit reached",
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            agent = next(iter(state.agents.values()))
            self.assertEqual(agent.status, "failed")
            self.assertIn("thread limit reached", agent.result)
            self.assertEqual(state.pending_spawns, {})

    def test_list_agents_and_interrupt_results_set_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout-root.jsonl"
            rollout.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "root-thread", "cwd": "/work/project"},
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "agent_thread_id": "child-one",
                            "agent_path": "/root/review",
                        },
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "interrupt_agent",
                        "call_id": "interrupt-one",
                        # Codex accepts and records a short task name here even
                        # though SubAgentActivity uses the canonical path.
                        "arguments": json.dumps({"target": "review"}),
                    },
                )
                + record(
                    "2026-11-02T10:00:02.500Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "interrupted",
                            "agent_thread_id": "child-one",
                            "agent_path": "/root/review",
                        },
                    },
                )
                + record(
                    "2026-11-02T10:00:03Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "interrupt-one",
                        "output": json.dumps({"previous_status": "running"}),
                    },
                )
                + record(
                    "2026-11-02T10:00:04Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "list_agents",
                        "call_id": "list-one",
                        "arguments": "{}",
                    },
                )
                + record(
                    "2026-11-02T10:00:05Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "list-one",
                        "output": json.dumps(
                            {
                                "agents": [
                                    {"agent_name": "/root", "agent_status": "running"},
                                    {
                                        "agent_name": "/root/review",
                                        "agent_status": "interrupted",
                                    },
                                    {
                                        "agent_name": "/root/test",
                                        "agent_status": {"completed": "Tests pass."},
                                    },
                                ]
                            }
                        ),
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            self.assertNotIn("root-thread::/root", state.agents)
            self.assertNotIn("root-thread::review", state.agents)
            self.assertEqual(
                state.agents["root-thread::/root/review"].status, "interrupted"
            )
            self.assertAlmostEqual(
                state.agents["root-thread::/root/review"].finished_at,
                monitor.parse_time("2026-11-02T10:00:02.500Z"),
            )
            completed = state.agents["root-thread::/root/test"]
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result, "Tests pass.")

    def test_root_task_completion_closes_unresolved_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "rollout-2026-11-02T10-00-00-root.jsonl"
            child = root / "rollout-2026-11-02T10-00-01-child.jsonl"
            parent.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "root-thread", "cwd": "/work/project"},
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "agent_thread_id": "child-thread",
                            "agent_path": "/root/review",
                        },
                    },
                )
                + record(
                    "2026-11-02T10:00:03Z",
                    "event_msg",
                    {"type": "task_complete"},
                ),
                encoding="utf-8",
            )
            # This older child rollout is discovered after the parent file.
            # Its progress must not reopen a turn that already ended.
            child.write_text(
                record(
                    "2026-11-02T10:00:01Z",
                    "session_meta",
                    {
                        "id": "child-thread",
                        "session_id": "root-thread",
                        "parent_thread_id": "root-thread",
                        "thread_source": "subagent",
                        "agent_path": "/root/review",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "progress",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Still working."}],
                    },
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "old-nested-spawn",
                        "arguments": json.dumps(
                            {"task_name": "nested", "message": "Nested historical task."}
                        ),
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "old-nested-spawn",
                        "output": json.dumps({"task_name": "/root/nested"}),
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            agent = state.agents["root-thread::/root/review"]
            self.assertEqual(agent.status, "interrupted")
            self.assertEqual(
                state.agents["root-thread::/root/nested"].status, "interrupted"
            )
            self.assertIsNotNone(state.sessions["root-thread"].last_turn_finished_at)

    def test_running_child_steps_and_completion_are_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "sessions"
            root.mkdir()
            parent = root / "rollout-2026-11-02T10-00-00-root.jsonl"
            child = root / "rollout-2026-11-02T10-00-01-child.jsonl"
            session_index = base / "session_index.jsonl"
            session_index.write_text(
                json.dumps(
                    {
                        "id": "root-thread",
                        "thread_name": "旧标题",
                        "updated_at": "2026-11-02T10:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {
                        "id": "root-thread",
                        "thread_name": "修复登录页面并补充回归测试",
                        "updated_at": "2026-11-02T10:00:01Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            parent.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "root-thread", "cwd": "/work/project"},
                )
                + record(
                    "2026-11-02T10:00:00Z",
                    "response_item",
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "<environment_context>ignored</environment_context>"}
                        ],
                    },
                )
                + record(
                    "2026-11-02T10:00:00Z",
                    "response_item",
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "运行 pytest，这条命令不是 session 标题"}
                        ],
                    },
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "spawn-1",
                        "arguments": json.dumps(
                            {"task_name": "review", "message": "Review the tests."}
                        ),
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "spawn-1",
                        "output": json.dumps({"task_name": "/root/review"}),
                    },
                )
                + record(
                    "2026-11-02T10:00:03Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "agent_thread_id": "child-thread",
                            "agent_path": "/root/review",
                        },
                    },
                ),
                encoding="utf-8",
            )
            child.write_text(
                record(
                    "2026-11-02T10:00:03Z",
                    "session_meta",
                    {
                        "id": "child-thread",
                        "session_id": "root-thread",
                        "parent_thread_id": "root-thread",
                        "thread_source": "subagent",
                        "agent_path": "/root/review",
                        "agent_nickname": "Curie",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-02T10:00:04Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "message-1",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Checking tests."}],
                    },
                )
                + record(
                    "2026-11-02T10:00:05Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": "command-1",
                            "command": ["/bin/bash", "-lc", "pytest -q"],
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            scanner = monitor.RolloutScanner(root, state, history_hours=1)
            scanner.poll(force_discovery=True)

            self.assertIn("root-thread", state.sessions)
            self.assertEqual(state.sessions["root-thread"].cwd, "/work/project")
            self.assertEqual(state.sessions["root-thread"].name, "修复登录页面并补充回归测试")
            agent = state.agents["root-thread::/root/review"]
            self.assertEqual(agent.status, "running")
            self.assertEqual(agent.name, "review")
            self.assertEqual(agent.task, "Review the tests.")
            self.assertEqual(agent.delegation_prompt, "Review the tests.")
            self.assertEqual(agent.delegation_prompt_source, "spawn_agent")
            self.assertEqual(agent.cwd, "/work/project")
            self.assertIn("pytest -q", agent.steps["command-1"].summary)

            with session_index.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "id": "root-thread",
                            "thread_name": "运行中改名后的标题",
                            "updated_at": "2026-11-02T10:00:05Z",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            scanner.poll()
            self.assertEqual(state.sessions["root-thread"].name, "运行中改名后的标题")

            with parent.open("a", encoding="utf-8") as stream:
                stream.write(
                    record(
                        "2026-11-02T10:00:06Z",
                        "event_msg",
                        {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "kind": "completed",
                                "agent_thread_id": "child-thread",
                                "agent_path": "/root/review",
                            },
                        },
                    )
                )
            with child.open("a", encoding="utf-8") as stream:
                stream.write(
                    record(
                        "2026-11-02T10:00:06Z",
                        "response_item",
                        {
                            "type": "message",
                            "id": "message-2",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "All tests pass."}],
                        },
                    )
                    + record(
                        "2026-11-02T10:00:07Z",
                        "event_msg",
                        {"type": "task_complete"},
                    )
                )

            scanner.poll(force_discovery=True)
            self.assertEqual(agent.status, "completed")
            self.assertEqual(agent.result, "All tests pass.")

    def test_handoff_file_backfills_an_encrypted_spawn_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "00-parent.jsonl"
            child = root / "01-child.jsonl"
            prompt = (
                "你是无历史的独立 Stage 2 r07 生成 Agent。\n"
                "只允许读取公开 question 与本轮 candidate。\n\n"
                "新一轮只围绕虚化与层次假设。"
            )
            parent.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "root-thread", "cwd": "/work/project"},
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "response_item",
                    {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "spawn-encrypted",
                        "arguments": json.dumps(
                            {
                                "task_name": "stage2_r07",
                                "message": "gAAAA-encrypted-prompt",
                            }
                        ),
                    },
                )
                + record(
                    "2026-11-02T10:00:02Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "spawn-encrypted",
                        "output": json.dumps({"task_name": "/root/stage2_r07"}),
                    },
                ),
                encoding="utf-8",
            )
            child.write_text(
                record(
                    "2026-11-02T10:00:02Z",
                    "session_meta",
                    {
                        "id": "child-thread",
                        "session_id": "root-thread",
                        "parent_thread_id": "root-thread",
                        "thread_source": "subagent",
                        "agent_path": "/root/stage2_r07",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-02T10:00:03Z",
                    "response_item",
                    {
                        "type": "agent_message",
                        "author": "/root",
                        "recipient": "/root/stage2_r07",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: NEW_TASK\nPayload:\n",
                            },
                            {
                                "type": "encrypted_content",
                                "encrypted_content": "gAAAA-encrypted-prompt",
                            },
                        ],
                    },
                )
                + record(
                    "2026-11-02T10:00:04Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": "read-handoff",
                            "command": [
                                "/bin/bash",
                                "-lc",
                                "cat /work/project/handoffs/stage-02-r07-generation.txt",
                            ],
                            "parsed_cmd": [
                                {
                                    "type": "read",
                                    "path": "/work/project/handoffs/stage-02-r07-generation.txt",
                                }
                            ],
                            "stdout": prompt,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            agent = state.agents["root-thread::/root/stage2_r07"]
            self.assertEqual(agent.delegation_prompt, prompt)
            self.assertIn("stage-02-r07-generation.txt", agent.delegation_prompt_source)
            self.assertFalse(agent.delegation_prompt_encrypted)
            details = "\n".join(
                line for line, _ in monitor.detail_lines(agent, 120)
            )
            self.assertIn("主 Agent 创建提示词", details)
            self.assertIn("新一轮只围绕虚化与层次假设", details)

    def test_create_thread_is_grouped_as_an_external_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "00-parent.jsonl"
            external = root / "01-external.jsonl"
            prompt = "读取 handoff 并生成 r07 候选。"
            parent.write_text(
                record(
                    "2026-11-03T02:05:20Z",
                    "session_meta",
                    {
                        "id": "root-thread",
                        "thread_source": "user",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-03T02:05:24Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "completed_at_ms": 1_793_671_524_000,
                        "item": {
                            "type": "McpToolCall",
                            "server": "codex_tui",
                            "tool": "create_thread",
                            "arguments": {
                                "title": "Stage 2 r07 生成",
                                "prompt": prompt,
                            },
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {"threadId": "external-thread"}
                                        ),
                                    }
                                ]
                            },
                        },
                    },
                ),
                encoding="utf-8",
            )
            external.write_text(
                record(
                    "2026-11-03T02:05:24.100Z",
                    "session_meta",
                    {
                        "id": "external-thread",
                        "thread_source": "user",
                        "cwd": "/work/project",
                    },
                )
                + record(
                    "2026-11-03T02:05:25Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "progress",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [
                            {"type": "output_text", "text": "开始生成候选。"}
                        ],
                    },
                )
                + record(
                    "2026-11-03T02:05:26Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "id": "render",
                            "command": ["bash", "-lc", "run_v4.py draft"],
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                )
                + record(
                    "2026-11-03T02:05:27Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "done",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": "r07 候选已完成。"}
                        ],
                    },
                )
                + record(
                    "2026-11-03T02:05:28Z",
                    "event_msg",
                    {"type": "task_complete"},
                ),
                encoding="utf-8",
            )

            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )

            key = "root-thread::external-thread"
            agent = state.agents[key]
            self.assertEqual(agent.worker_kind, "external_task")
            self.assertEqual(agent.session_id, "root-thread")
            self.assertEqual(agent.external_session_id, "external-thread")
            self.assertEqual(agent.name, "Stage 2 r07 生成")
            self.assertEqual(agent.delegation_prompt, prompt)
            self.assertEqual(agent.status, "completed")
            self.assertIn("render", agent.steps)
            self.assertEqual(agent.result, "r07 候选已完成。")

            controller = monitor.TuiController()
            session_ids = [item.session_id for item in controller.session_items(state)]
            self.assertIn("root-thread", session_ids)
            self.assertNotIn("external-thread", session_ids)
            controller.selected_session_id = "root-thread"
            self.assertEqual(controller.agent_items(state)[0][0], key)
            details = "\n".join(
                line for line, _ in monitor.detail_lines(agent, 120)
            )
            self.assertIn("外部调度任务（独立 Session）", details)
            self.assertIn("归属主 Session: root-thread", details)
            self.assertIn("任务独立 Session: external-thread", details)

    def test_messages_to_root_are_not_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout-root-message.jsonl"
            rollout.write_text(
                record(
                    "2026-11-02T10:00:00Z",
                    "session_meta",
                    {"id": "child-thread", "session_id": "root-thread"},
                )
                + record(
                    "2026-11-02T10:00:01Z",
                    "event_msg",
                    {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "interacted",
                            "agent_thread_id": "root-thread",
                            "agent_path": "/root",
                        },
                    },
                ),
                encoding="utf-8",
            )
            state = monitor.MonitorState()
            monitor.RolloutScanner(root, state, history_hours=1).poll(
                force_discovery=True
            )
            self.assertEqual(state.agents, {})

    def test_stale_unclosed_agent_is_not_running(self) -> None:
        state = monitor.MonitorState()
        state.apply(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "old-session",
                "agent_id": "/root/old",
                "timestamp": "2020-01-01T00:00:00Z",
            }
        )
        state.mark_stale(60)
        self.assertEqual(state.agents["old-session::/root/old"].status, "stale")

    def test_code_mode_nested_tool_is_summarized(self) -> None:
        tool, value = monitor.RolloutScanner._nested_tool(
            'const r = await tools.exec_command({"cmd":"pytest -q"}); text(r.output);'
        )
        self.assertEqual(tool, "exec_command")
        self.assertEqual(value, "pytest -q")

    def test_render_respects_terminal_height(self) -> None:
        state = monitor.MonitorState()
        for index in range(5):
            common = {
                "session_id": "session",
                "agent_id": f"/root/agent-{index}",
                "timestamp": f"2026-11-02T10:00:0{index}Z",
                "cwd": "/work/a-very-long-project-path",
            }
            state.apply({"hook_event_name": "SubagentStart", **common})
            agent = state.agents[f"session::/root/agent-{index}"]
            agent.task = "Inspect a long list of project details and report findings. " * 5
            for step in range(6):
                state.apply(
                    {
                        **common,
                        "hook_event_name": "PostToolUse",
                        "tool_name": "Shell",
                        "tool_use_id": f"{index}-{step}",
                        "tool_input": "pytest --very-long-option " * 8,
                    }
                )
            state.apply({"hook_event_name": "SubagentStop", **common})

        original = monitor.terminal_dimensions
        try:
            monitor.terminal_dimensions = lambda: (80, 24)
            output = monitor.render(
                state,
                Path("/tmp/events.jsonl"),
                colors=False,
                max_steps=4,
                completed_limit=10,
            )
        finally:
            monitor.terminal_dimensions = original

        self.assertLessEqual(len(output.splitlines()), 24)
        self.assertIn("另有", output)


class TuiControllerTests(unittest.TestCase):
    @staticmethod
    def populated_state() -> object:
        state = monitor.MonitorState()
        state.apply(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "session-one",
                "agent_id": "/root/review",
                "agent_type": "review",
                "cwd": "/work/one",
                "timestamp": "2026-11-02T10:00:00Z",
            }
        )
        state.apply(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "session-two",
                "agent_id": "/root/test",
                "agent_type": "test",
                "cwd": "/work/two",
                "timestamp": "2026-11-02T10:01:00Z",
            }
        )
        return state

    def test_three_level_navigation_and_back(self) -> None:
        state = self.populated_state()
        controller = monitor.TuiController()
        controller.normalize(state)

        self.assertEqual(controller.page, "sessions")
        self.assertEqual(controller.selected_session_id, "session-two")
        controller.enter(state)
        self.assertEqual(controller.page, "agents")
        self.assertEqual(controller.selected_agent_key, "session-two::/root/test")
        controller.enter(state)
        self.assertEqual(controller.page, "detail")
        self.assertTrue(controller.back())
        self.assertEqual(controller.page, "agents")
        self.assertTrue(controller.back())
        self.assertEqual(controller.page, "sessions")
        self.assertFalse(controller.back())

    def test_background_refresh_keeps_selection_and_detail_scroll(self) -> None:
        state = self.populated_state()
        controller = monitor.TuiController()
        controller.normalize(state)
        controller.enter(state)
        controller.enter(state)
        controller.detail_scroll = 7
        selected_session = controller.selected_session_id
        selected_agent = controller.selected_agent_key

        state.touch_session(
            "newer-session",
            timestamp="2026-11-02T10:02:00Z",
            cwd="/work/newer",
        )
        controller.normalize(state)

        self.assertEqual(controller.page, "detail")
        self.assertEqual(controller.selected_session_id, selected_session)
        self.assertEqual(controller.selected_agent_key, selected_agent)
        self.assertEqual(controller.detail_scroll, 7)

    def test_cjk_clipping_uses_terminal_cell_width(self) -> None:
        self.assertEqual(monitor.display_width("会话 A"), 6)
        self.assertLessEqual(monitor.display_width(monitor.clip_display("中文内容", 5, "…")), 5)

    def test_default_hook_row_is_not_shown_as_subagent(self) -> None:
        state = self.populated_state()
        state.apply(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "session-two",
                "agent_id": "default-thread-id",
                "agent_type": "default",
                "cwd": "/work/two",
                "timestamp": "2026-11-02T10:02:00Z",
            }
        )
        controller = monitor.TuiController()
        controller.normalize(state)

        names = [agent.name for _, agent in controller.agent_items(state)]
        self.assertEqual(names, ["test"])
        self.assertEqual(
            [agent.name for agent in monitor.agents_for_session(state, "session-two")],
            ["test"],
        )

    def test_session_display_name_prefers_user_facing_name(self) -> None:
        session = monitor.SessionState(
            session_id="session-id",
            name="实现交互式监控器",
            cwd="/work/project",
        )
        self.assertEqual(monitor.session_display_name(session), "实现交互式监控器")

    def test_agent_total_duration_freezes_at_completion(self) -> None:
        completed = monitor.AgentState(
            agent_id="/root/review",
            name="review",
            started_at=100.0,
            finished_at=160.0,
            status="completed",
            last_activity_at=160.0,
        )
        current = 3_700.0

        self.assertEqual(monitor.duration_text(completed, current), "1m 00s")
        self.assertEqual(monitor.duration_text(completed, current + 3_600), "1m 00s")
        self.assertEqual(monitor.duration_label(completed), "总耗时")
        details = monitor.detail_lines(completed, 120, current)
        self.assertIn("Agent 总耗时: 1m 00s", details[0][0])
        self.assertNotIn("启动至今", details[0][0])

        running = monitor.AgentState(
            agent_id="/root/build",
            name="build",
            started_at=100.0,
            status="running",
            last_activity_at=150.0,
        )
        self.assertEqual(monitor.duration_text(running, 160.0), "1m 00s")
        self.assertEqual(monitor.duration_label(running), "已运行")

    def test_agent_list_timestamp_uses_first_session_child_as_zero(self) -> None:
        state = monitor.MonitorState()
        first = monitor.AgentState(
            agent_id="/root/first",
            name="first",
            session_id="session",
            started_at=100.0,
            finished_at=125.0,
            status="completed",
            last_activity_at=125.0,
        )
        second = monitor.AgentState(
            agent_id="/root/second",
            name="second",
            session_id="session",
            started_at=110.0,
            finished_at=225.0,
            status="completed",
            last_activity_at=225.0,
        )
        running = monitor.AgentState(
            agent_id="/root/running",
            name="running",
            session_id="session",
            started_at=140.0,
            status="running",
            last_activity_at=160.0,
        )
        unrelated = monitor.AgentState(
            agent_id="/root/old-other-session",
            session_id="other-session",
            started_at=10.0,
        )
        state.agents["session::/root/first"] = first
        state.agents["session::/root/second"] = second
        state.agents["session::/root/running"] = running
        state.agents["other-session::/root/old"] = unrelated

        origin = monitor.session_timeline_origin(state, "session")

        self.assertEqual(origin, 100.0)
        self.assertEqual(monitor.timeline_timestamp_text(first, origin, 300.0), "0m 25s")
        self.assertEqual(monitor.timeline_timestamp_text(second, origin, 300.0), "2m 05s")
        self.assertEqual(monitor.timeline_timestamp_text(running, origin, 170.0), "1m 10s")
        second.steps["one"] = monitor.Step(
            key="one", tool="Shell", summary="", started_at=120.0
        )
        second.steps["two"] = monitor.Step(
            key="two", tool="Read", summary="", started_at=130.0
        )
        self.assertEqual(
            monitor.agent_list_timeline_text(second, origin, 300.0),
            "2步  时间戳 2m 05s",
        )

    def test_session_timeline_origin_ignores_default_root_agent(self) -> None:
        state = monitor.MonitorState()
        state.agents["session::default"] = monitor.AgentState(
            agent_id="default-thread",
            name="default",
            agent_type="default",
            session_id="session",
            started_at=10.0,
        )
        state.agents["session::/root/worker"] = monitor.AgentState(
            agent_id="/root/worker",
            name="worker",
            session_id="session",
            started_at=100.0,
        )

        self.assertEqual(monitor.session_timeline_origin(state, "session"), 100.0)

    def test_agent_list_draws_timestamp_immediately_after_step_count(self) -> None:
        class RecordingView:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def frame(self, *_args: object) -> tuple[int, int]:
                return 24, 120

            def attr(self, *_args: object, **_kwargs: object) -> int:
                return 0

            def add(
                self,
                _y: int,
                _x: int,
                text: str,
                _attr: int = 0,
                *,
                fill: bool = False,
            ) -> None:
                del fill
                self.lines.append(text)

        state = monitor.MonitorState()
        state.sessions["session"] = monitor.SessionState(
            session_id="session", cwd="/work/project", started_at=100.0
        )
        agent = monitor.AgentState(
            agent_id="/root/build",
            name="build",
            task="Build the feature",
            session_id="session",
            started_at=100.0,
            finished_at=225.0,
            status="completed",
            last_activity_at=225.0,
        )
        agent.steps["one"] = monitor.Step(
            key="one", tool="Shell", summary="", started_at=120.0
        )
        state.agents["session::/root/build"] = agent
        controller = monitor.TuiController()
        controller.selected_session_id = "session"
        controller.selected_agent_key = "session::/root/build"
        view = RecordingView()

        monitor.draw_agents(view, controller, state)

        self.assertTrue(
            any("1步  时间戳 2m 05s" in line for line in view.lines),
            view.lines,
        )

    def test_hidden_agents_persist_and_new_activity_reappears(self) -> None:
        state = self.populated_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visibility.json"
            visibility = monitor.VisibilityStore(path)
            review_key = "session-one::/root/review"
            review = state.agents[review_key]

            visibility.hide_agent(review_key, review)
            self.assertFalse(visibility.is_visible(review_key, review))

            reloaded = monitor.VisibilityStore(path)
            self.assertFalse(reloaded.is_visible(review_key, review))
            controller = monitor.TuiController(reloaded)
            controller.selected_session_id = "session-one"
            self.assertEqual(controller.agent_items(state), [])

            review.last_activity_at = reloaded.hidden_agents[review_key] + 1
            self.assertTrue(reloaded.is_visible(review_key, review))
            self.assertEqual(len(controller.agent_items(state)), 1)

            self.assertEqual(reloaded.clear_session(state, "session-one"), 1)
            self.assertEqual(controller.agent_items(state), [])
            self.assertEqual(reloaded.clear_all(state), 2)
            self.assertEqual(monitor.visible_agent_entries(state, reloaded), [])

            reloaded.restore()
            self.assertEqual(len(monitor.visible_agent_entries(state, reloaded)), 2)


if __name__ == "__main__":
    unittest.main()
