"""Read actual Windows console cells after CJK scrolling and page changes.

Run in an interactive Windows terminal with:
    python -B tests/windows_terminal_smoke.py
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("monitor_smoke", ROOT / "src" / "monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class Rect(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class BufferInfo(ctypes.Structure):
    _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", wintypes.WORD),
                ("window", Rect), ("maximum", Coord)]


class Character(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class Cell(ctypes.Structure):
    _fields_ = [("character", Character), ("attributes", wintypes.WORD)]


def snapshot(console: monitor.WindowsConsole) -> bytes:
    import msvcrt

    kernel = console.kernel
    handle = msvcrt.get_osfhandle(sys.stdout.fileno())
    kernel.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(BufferInfo)]
    kernel.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
    kernel.ReadConsoleOutputW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Cell), Coord, Coord, ctypes.POINTER(Rect)
    ]
    kernel.ReadConsoleOutputW.restype = wintypes.BOOL
    info = BufferInfo()
    if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    height, width = console.getmaxyx()
    cells = (Cell * (height * width))()
    region = Rect(info.window.Left, info.window.Top,
                  info.window.Left + width - 1, info.window.Top + height - 1)
    if not kernel.ReadConsoleOutputW(handle, cells, Coord(width, height), Coord(), ctypes.byref(region)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(cells)


def populated_state() -> monitor.MonitorState:
    state = monitor.MonitorState()
    for session_index in range(16):
        session_id = f"session-{session_index}"
        title = ("很长的中文会话名称和任务说明" * 8 if session_index % 2 else "短")
        state.sessions[session_id] = monitor.SessionState(
            session_id=session_id, name=f"{session_index} {title}",
            cwd=r"C:\Users\Example User\中文项目", started_at=100,
            last_activity_at=200 + session_index,
        )
        for agent_index in range(16):
            agent_id = f"agent-{agent_index}"
            agent = monitor.AgentState(
                agent_id=agent_id,
                name=f"生成评审 {agent_index} " + ("中文长名字" * 10 if agent_index % 2 else "短"),
                task="中文任务摘要" * (15 if agent_index % 2 else 1),
                status="completed", started_at=100, finished_at=200,
                last_activity_at=200 + agent_index, session_id=session_id,
                delegation_prompt="\n".join(
                    f"步骤 {line} " + ("一段很长的中文创建提示词" * 20 if line % 2 else "短行")
                    for line in range(20)
                ),
            )
            state.agents[f"{session_id}::{agent_id}"] = agent
    return state


def main() -> int:
    if os.name != "nt" or not sys.stdout.isatty():
        raise SystemExit("Requires a native interactive Windows console")
    monitor.configure_windows_stdio()
    state = populated_state()
    controller = monitor.TuiController()
    checks = 0

    class FrozenDatetime(monitor.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(500, tz)

    with monitor.WindowsConsole() as console:
        original_modes = list(console.saved_modes)
        view = monitor.WindowsVTView(console, colors=True)

        def verify() -> None:
            nonlocal checks
            monitor.draw_tui(view, controller, state)
            after_scroll = snapshot(console)
            # A fresh physical clear provides a reference independent of any
            # characters left over by earlier frames or longer pages.
            sys.stdout.write("\x1b[0m\x1b[2J\x1b[H")
            sys.stdout.flush()
            monitor.draw_tui(view, controller, state)
            clean = snapshot(console)
            assert after_scroll == clean, f"Residual cells on {controller.page}, scroll {controller.detail_scroll}"
            checks += 1

        # Freeze presentation clocks so only navigation can change the frame.
        with patch.object(monitor.time, "time", return_value=500), patch.object(monitor, "datetime", FrozenDatetime):
            for _ in range(20):
                controller.move(state, 1)
                verify()
            for _ in range(20):
                controller.move(state, -1)
                verify()
            controller.enter(state)
            for _ in range(20):
                controller.move(state, 1)
                verify()
            for _ in range(20):
                controller.move(state, -1)
                verify()
            controller.enter(state)
            for scroll in [0, 1, 8, 16, 32, 64, 100, 32, 8, 1, 0]:
                controller.detail_scroll = scroll
                verify()
            controller.back()
            verify()
            controller.back()
            verify()

            # Real wide-character placement must advance eight physical cells.
            view.frame("", "", "")
            view.add(2, 0, "中文内容")
            view.add(2, 8, "END")
            view.present()
            cells = snapshot(console)
            width = console.getmaxyx()[1]
            cell_array = (Cell * (len(cells) // ctypes.sizeof(Cell))).from_buffer_copy(cells)
            assert "".join(cell_array[2 * width + index].character.UnicodeChar for index in range(8, 11)) == "END"

    for handle, original in original_modes:
        restored = wintypes.DWORD()
        assert console.kernel.GetConsoleMode(handle, ctypes.byref(restored))
        assert restored.value == original, "Console mode not restored after exit"
    print(json.dumps({"success": True, "navigation_frames": checks,
                      "cjk_width_cells": 8, "residual_cells": 0,
                      "console_modes_restored": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
