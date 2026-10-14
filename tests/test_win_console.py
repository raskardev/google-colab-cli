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
(kernel32 GetConsoleMode/SetConsoleMode/ReadConsoleInputW) are mocked so the
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


class TestTranslateKeyEvent:
    """Pure translation of one console INPUT_RECORD key event to the text/VT
    sequence the POSIX path would have sent. Runs on any OS."""

    # dwControlKeyState bits (wincon.h)
    LEFT_CTRL_PRESSED = 0x0008
    RIGHT_ALT_PRESSED = 0x0001  # AltGr on European layouts

    VK_UP = 0x26
    VK_F5 = 0x74
    VK_F12 = 0x7B

    def test_printable_char_forwarded_verbatim(self):
        assert win_console.translate_key_event(True, 1, 0, "à", 0) == "à"

    def test_altgr_char_forwarded_plain(self):
        """AltGr (@ € # on Spanish layouts) arrives with Right-Alt + Left-Ctrl
        control state but must be forwarded as the plain character."""
        ctrl_state = self.RIGHT_ALT_PRESSED | self.LEFT_CTRL_PRESSED
        assert win_console.translate_key_event(True, 1, 0x32, "@", ctrl_state) == "@"

    def test_ctrl_c_forwarded(self):
        assert win_console.translate_key_event(True, 1, 0x43, "\x03", 0) == "\x03"

    def test_enter_backspace_tab_esc_forwarded(self):
        assert win_console.translate_key_event(True, 1, 0x0D, "\r", 0) == "\r"
        assert win_console.translate_key_event(True, 1, 0x08, "\x08", 0) == "\x08"
        assert win_console.translate_key_event(True, 1, 0x09, "\t", 0) == "\t"
        assert win_console.translate_key_event(True, 1, 0x1B, "\x1b", 0) == "\x1b"

    def test_arrow_keys_map_to_xterm_sequences(self):
        assert win_console.translate_key_event(True, 1, 0x25, "\x00", 0) == "\x1b[D"
        assert win_console.translate_key_event(True, 1, 0x26, "\x00", 0) == "\x1b[A"
        assert win_console.translate_key_event(True, 1, 0x27, "\x00", 0) == "\x1b[C"
        assert win_console.translate_key_event(True, 1, 0x28, "\x00", 0) == "\x1b[B"

    def test_navigation_keys_map_to_xterm_sequences(self):
        assert win_console.translate_key_event(True, 1, 0x24, "", 0) == "\x1b[H"
        assert win_console.translate_key_event(True, 1, 0x23, "", 0) == "\x1b[F"
        assert win_console.translate_key_event(True, 1, 0x21, "", 0) == "\x1b[5~"
        assert win_console.translate_key_event(True, 1, 0x22, "", 0) == "\x1b[6~"
        assert win_console.translate_key_event(True, 1, 0x2D, "", 0) == "\x1b[2~"
        assert win_console.translate_key_event(True, 1, 0x2E, "", 0) == "\x1b[3~"

    def test_function_keys_map_to_xterm_sequences(self):
        assert win_console.translate_key_event(True, 1, 0x70, "", 0) == "\x1bOP"
        assert win_console.translate_key_event(True, 1, 0x73, "", 0) == "\x1bOS"
        assert win_console.translate_key_event(True, 1, self.VK_F5, "", 0) == "\x1b[15~"
        assert (
            win_console.translate_key_event(True, 1, self.VK_F12, "", 0) == "\x1b[24~"
        )

    def test_unmapped_key_returns_empty_string(self):
        assert win_console.translate_key_event(True, 1, 0xFF, "", 0) == ""

    def test_repeat_count_repeats_the_keystroke(self):
        assert win_console.translate_key_event(True, 3, 0, "a", 0) == "aaa"
        assert (
            win_console.translate_key_event(True, 2, self.VK_UP, "", 0)
            == "\x1b[A\x1b[A"
        )

    def test_key_up_events_are_ignored(self):
        assert win_console.translate_key_event(False, 1, 0, "a", 0) == ""
        assert win_console.translate_key_event(False, 1, self.VK_UP, "", 0) == ""


def _fake_kernel32_with_stdin():
    return FakeKernel32()


def _records(*events):
    """Build the tuple records read_key() consumes:
    (key_down, repeat, vk, unicode_char, control_state)."""
    return list(events)


class TestReadKey:
    """read_key() over a (mocked) ReadConsoleInputW record stream.

    The record reader is patched with plain tuples so these tests run on any
    OS and never touch a real console; the ctypes struct layout itself is
    covered by the Windows-only test below.
    """

    @staticmethod
    def _patch_reader(records_iter):
        """Patch _kernel32 and _read_console_records; the iterator yields one
        list of records per (simulated) blocking read."""
        batches = iter(records_iter)

        def fake_read(handle, count):
            try:
                return next(batches)
            except StopIteration:
                return []  # EOF

        return (
            patch.object(win_console, "_kernel32", _fake_kernel32_with_stdin()),
            patch.object(win_console, "_read_console_records", side_effect=fake_read),
        )

    def test_plain_char_forwarded(self):
        patches = self._patch_reader([[(True, 1, 0x41, "a", 0)]])
        with patches[0], patches[1]:
            assert win_console.read_key() == "a"

    def test_unmapped_key_skipped_not_eof(self):
        """An unmapped key event must be dropped while the session keeps
        reading; only a failed read (EOF) may end the loop."""
        patches = self._patch_reader(
            [
                [(True, 1, 0xFF, "", 0)],  # unmapped
                [(True, 1, 0x26, "", 0)],  # Up arrow -> VT sequence
            ]
        )
        with patches[0], patches[1]:
            assert win_console.read_key() == "\x1b[A"

    def test_key_up_records_skipped(self):
        patches = self._patch_reader(
            [
                [(False, 1, 0x41, "a", 0)],
                [(True, 1, 0x41, "a", 0)],
            ]
        )
        with patches[0], patches[1]:
            assert win_console.read_key() == "a"

    def test_surrogate_pair_combined(self):
        high = chr(0xD83D)
        low = chr(0xDE00)  # U+1F600
        patches = self._patch_reader([[(True, 1, 0, high, 0), (True, 1, 0, low, 0)]])
        with patches[0], patches[1]:
            assert win_console.read_key() == "\U0001f600"

    def test_unpaired_high_surrogate_dropped_before_next_key(self):
        high = chr(0xD83D)
        patches = self._patch_reader([[(True, 1, 0, high, 0), (True, 1, 0x26, "", 0)]])
        with patches[0], patches[1]:
            # A lone high surrogate cannot be UTF-8 encoded for the JSON
            # layer: it is dropped, and the next real key is forwarded.
            assert win_console.read_key() == "\x1b[A"

    def test_surrogate_pair_split_across_reads(self):
        """The two halves may arrive in separate ReadConsoleInputW batches."""
        high = chr(0xD83D)
        low = chr(0xDE00)
        patches = self._patch_reader([[(True, 1, 0, high, 0)], [(True, 1, 0, low, 0)]])
        with patches[0], patches[1]:
            assert win_console.read_key() == "\U0001f600"

    def test_failed_read_returns_none(self):
        patches = self._patch_reader([[]])
        with patches[0], patches[1]:
            assert win_console.read_key() is None

    def test_keyboardinterrupt_forwarded_as_ctrl_c(self):
        def raising_read(handle, count):
            raise KeyboardInterrupt

        with (
            patch.object(win_console, "_kernel32", _fake_kernel32_with_stdin()),
            patch.object(
                win_console, "_read_console_records", side_effect=raising_read
            ),
        ):
            assert win_console.read_key() == "\x03"

    def test_raises_when_console_apis_unavailable(self):
        with patch.object(win_console, "_kernel32", None), pytest.raises(OSError):
            win_console.read_key()


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes INPUT_RECORD parsing")
def test_input_record_parsing_windows_only():
    """The real ctypes structs parse a KEY_EVENT INPUT_RECORD correctly."""
    rec = win_console._INPUT_RECORD()
    rec.EventType = win_console.KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = True
    rec.Event.KeyEvent.wRepeatCount = 2
    rec.Event.KeyEvent.wVirtualKeyCode = 0x41
    rec.Event.KeyEvent.uChar.UnicodeChar = "à"
    rec.Event.KeyEvent.dwControlKeyState = 0x0001

    key_down, repeat, vk, uni, ctrl = win_console._record_to_tuple(rec)
    assert (key_down, repeat, vk, uni, ctrl) == (True, 2, 0x41, "à", 0x0001)


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes INPUT_RECORD parsing")
def test_read_console_records_filters_non_key_events_windows_only():
    """_read_console_records keeps KEY_EVENTs and drops other event types."""
    key = win_console._INPUT_RECORD()
    key.EventType = win_console.KEY_EVENT
    key.Event.KeyEvent.bKeyDown = True
    key.Event.KeyEvent.wRepeatCount = 1
    key.Event.KeyEvent.wVirtualKeyCode = 0x41
    key.Event.KeyEvent.uChar.UnicodeChar = "a"

    focus = win_console._INPUT_RECORD()
    focus.EventType = 0x0010  # FOCUS_EVENT — must be filtered out

    class FakeReaderKernel32:
        @staticmethod
        def ReadConsoleInputW(handle, records, count, ptr):
            for i, rec in enumerate(focus_tuple[:count]):
                records[i] = rec
            ptr._obj.value = min(len(focus_tuple), count)
            return True

    focus_tuple = [key, focus]
    with patch.object(win_console, "_kernel32", FakeReaderKernel32):
        out = win_console._read_console_records("stdin_handle", 8)
    assert out == [(True, 1, 0x41, "a", 0)]


class TestModuleImportability:
    def test_imports_on_posix(self):
        """The module must import cleanly on non-Windows (guarded imports)."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only import check")
        # The guarded Windows-only names stay unset on POSIX.
        assert win_console._kernel32 is None
        assert win_console._INPUT_RECORD is None
        # The pure translator must work without any Windows APIs loaded.
        assert win_console.translate_key_event(True, 1, 0x26, "\x00", 0) == "\x1b[A"
        assert win_console.translate_key_event(True, 2, 0, "a", 0) == "aa"
        # Console setup reports unavailability instead of raising ImportError.
        with pytest.raises(OSError), win_console.windows_raw_console():
            pass
        with pytest.raises(OSError):
            win_console.read_key()
