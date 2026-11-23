"""Regression checks for complete Windows VT frames and native key input."""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "monitor_terminal_tests", Path(__file__).resolve().parents[1] / "src" / "monitor.py"
)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class FakeConsole:
    def getmaxyx(self) -> tuple[int, int]:
        return 12, 80


class WindowsTerminalTests(unittest.TestCase):
    def test_short_page_clears_every_previous_row_without_scrolling(self) -> None:
        output = io.StringIO()
        view = monitor.WindowsVTView(FakeConsole(), colors=False, stream=output)
        view.frame("中文标题", "长说明", "返回")
        view.add(7, 1, "上一页的很长中文内容" * 10, fill=True)
        view.present()
        output.seek(0)
        output.truncate()

        view.frame("短页", "", "返回")
        view.add(2, 1, "新")
        view.present()

        frame = output.getvalue()
        self.assertNotIn("上一页", frame)
        self.assertNotIn("\n", frame)
        self.assertNotIn("\r", frame)
        for row in range(1, 13):
            self.assertIn(f"\x1b[{row};1H\x1b[0m\x1b[2K", frame)
        self.assertIn("\x1b[3;2H", frame)

    def test_content_cannot_move_cursor_or_inject_a_clear_sequence(self) -> None:
        output = io.StringIO()
        view = monitor.WindowsVTView(FakeConsole(), colors=False, stream=output)
        view.frame("title", "", "")
        view.add(2, 0, "命令\x1b[2J\n输出\t完成")
        view.present()

        self.assertNotIn("\x1b[2J", output.getvalue())
        self.assertNotIn("\n", output.getvalue())
        self.assertIn("命令 [2J 输出 完成", output.getvalue())

    def test_native_special_keys_match_the_navigation_keys(self) -> None:
        class Keyboard:
            def __init__(self) -> None:
                self.keys = iter(["\xe0", "P", "\x00", "I", "\r", "q"])

            def kbhit(self) -> bool:
                return True

            def getwch(self) -> str:
                return next(self.keys)

        console = monitor.WindowsConsole()
        console.getmaxyx = FakeConsole().getmaxyx
        console.keyboard = Keyboard()
        self.assertEqual(console.read_key(), "KEY_DOWN")
        self.assertEqual(console.read_key(), "KEY_PPAGE")
        self.assertEqual(console.read_key(), "\r")
        self.assertEqual(console.read_key(), "q")


if __name__ == "__main__":
    unittest.main()
