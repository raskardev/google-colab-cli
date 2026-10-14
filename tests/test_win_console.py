# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the Windows console raw-mode helpers (unit U8).

These tests must pass on any OS: the Windows-specific low-level calls
(kernel32 SetConsoleMode/GetConsoleMode, msvcrt.getwch) are mocked so the
suite never touches a real console and never blocks on stdin.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from colab_cli import win_console

# Console-mode flag constants mirrored from win_console for assertions.
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


class FakeKernel32:
    """Stands in for ctypes.windll.kernel32 in tests."""

    def __init__(self, in_mode=0x0007, out_mode=0x0003, set_mode_ok=True):
        self.in_mode = in_mode
        self.out_mode = out_mode
        self.set_mode_ok = set_mode_ok
        self.set_calls = []
        self.get_calls = []

    def GetStdHandle(self, which):
        return (
            "stdin_handle" if which == win_console.STD_INPUT_HANDLE else "stdout_handle"
        )

    def GetConsoleMode(self, handle, ptr):
        self.get_calls.append(handle)
        ptr._obj.value = self.in_mode if handle == "stdin_handle" else self.out_mode
        return True

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append((handle, mode))
        return self.set_mode_ok


class TestWindowsRawConsoleContext:
    def test_sets_raw_input_and_vt_output_and_restores(self):
        fake = FakeKernel32(in_mode=0x00F7, out_mode=0x0003)
        with (
            patch.object(win_console, "_kernel32", fake),
            win_console.windows_raw_console(),
        ):
            pass

        stdin_calls = [(h, m) for h, m in fake.set_calls if h == "stdin_handle"]
        stdout_calls = [(h, m) for h, m in fake.set_calls if h == "stdout_handle"]

        expected_in = (
            0x00F7 & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT)
        ) | ENABLE_VIRTUAL_TERMINAL_INPUT
        assert stdin_calls[0] == ("stdin_handle", expected_in)
        # Original input mode restored on exit
        assert stdin_calls[-1] == ("stdin_handle", 0x00F7)

        expected_out = 0x0003 | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        assert stdout_calls[0] == ("stdout_handle", expected_out)
        # Original output mode restored on exit
        assert stdout_calls[-1] == ("stdout_handle", 0x0003)

    def test_restores_original_modes_on_exception_inside_session(self):
        fake = FakeKernel32(in_mode=0x00F7, out_mode=0x0003)
        with (
            pytest.raises(RuntimeError),
            patch.object(win_console, "_kernel32", fake),
            win_console.windows_raw_console(),
        ):
            raise RuntimeError("boom")

        stdin_restores = [m for h, m in fake.set_calls if h == "stdin_handle"]
        assert stdin_restores[-1] == 0x00F7

    def test_raises_when_stdin_is_not_a_console(self):
        fake = FakeKernel32()
        fake.GetConsoleMode = MagicMock(return_value=False)
        with (
            pytest.raises(OSError),
            patch.object(win_console, "_kernel32", fake),
            win_console.windows_raw_console(),
        ):
            pass

        # Must not have touched any console mode when stdin is not a console
        assert fake.set_calls == []

    def test_raises_when_initial_set_mode_fails(self):
        fake = FakeKernel32(set_mode_ok=False)
        with (
            pytest.raises(OSError),
            patch.object(win_console, "_kernel32", fake),
            win_console.windows_raw_console(),
        ):
            pass

        # Only the failing initial attempt, no restore attempts
        assert len(fake.set_calls) == 1

    def test_skips_stdout_mode_when_stdout_is_not_a_console(self):
        """Stdout may be redirected while stdin is a console."""
        fake = FakeKernel32(in_mode=0x0007, out_mode=0x0003)

        original_get = fake.GetConsoleMode

        def get_mode(handle, ptr):  # noqa: ANN001 - test double
            if handle == "stdout_handle":
                return False
            return original_get(handle, ptr)

        fake.GetConsoleMode = get_mode  # type: ignore[method-assign]
        with (
            patch.object(win_console, "_kernel32", fake),
            win_console.windows_raw_console(),
        ):
            pass

        stdout_set = [m for h, m in fake.set_calls if h == "stdout_handle"]
        assert stdout_set == []  # never touched, never restored


class TestReadKey:
    @staticmethod
    def _patch_getwch(values):
        mock_msvcrt = MagicMock()
        mock_msvcrt.getwch.side_effect = values
        return patch.object(win_console, "msvcrt", mock_msvcrt)

    def test_plain_character_forwarded(self):
        with self._patch_getwch(["a"]):
            assert win_console.read_key() == "a"

    def test_enter_forwarded_as_carriage_return(self):
        with self._patch_getwch(["\r"]):
            assert win_console.read_key() == "\r"

    def test_ctrl_c_forwarded_not_raised(self):
        with self._patch_getwch(["\x03"]):
            assert win_console.read_key() == "\x03"

    def test_non_ascii_forwarded(self):
        with self._patch_getwch(["é"]):
            assert win_console.read_key() == "é"

    def test_enhanced_key_prefix_mapped_to_vt_sequence(self):
        # Without VT input some enhanced keys arrive as \x00 or \xe0 prefix
        # plus a scan code; with VT input enabled this is a fallback path.
        with self._patch_getwch(["\x00", "H"]):
            assert win_console.read_key() == "\x1b[A"
        with self._patch_getwch(["\xe0", "M"]):
            assert win_console.read_key() == "\x1b[C"
        with self._patch_getwch(["\xe0", "S"]):
            assert win_console.read_key() == "\x1b[3~"

    def test_unknown_enhanced_key_dropped(self):
        with self._patch_getwch(["\xe0", "\x8b"]):
            assert win_console.read_key() == ""

    def test_eof_returns_none(self):
        mock_msvcrt = MagicMock()
        mock_msvcrt.getwch.side_effect = EOFError
        with patch.object(win_console, "msvcrt", mock_msvcrt):
            assert win_console.read_key() is None

    def test_unexpected_keyboardinterrupt_forwarded_as_ctrl_c(self):
        mock_msvcrt = MagicMock()
        mock_msvcrt.getwch.side_effect = KeyboardInterrupt
        with patch.object(win_console, "msvcrt", mock_msvcrt):
            assert win_console.read_key() == "\x03"


class TestModuleImportability:
    def test_imports_on_posix(self):
        """The module must import cleanly on non-Windows (guarded imports)."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only import check")
        assert win_console._kernel32 is None
        assert win_console.msvcrt is None
