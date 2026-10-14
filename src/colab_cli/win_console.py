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

"""Windows console raw-mode helpers for ``colab console`` (unit U8).

Gives the interactive remote terminal on Windows the same feel as the POSIX
path:

- Input: ``SetConsoleMode`` on the stdin handle clears
  ``ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT`` and sets
  ``ENABLE_VIRTUAL_TERMINAL_INPUT``, so keystrokes arrive immediately and
  arrow/function keys arrive as VT escape sequences. Keys are read with
  ``msvcrt.getwch()`` and forwarded one character at a time. Ctrl-C arrives as
  ``"\\x03"`` (no local SIGINT) and is forwarded to the remote shell, matching
  POSIX raw mode.
- Output: ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` is set on the stdout handle so
  the remote ANSI output renders in Windows Terminal / conhost.

All console modes are restored when the returned context manager exits, on
success, on error, and on websocket close (the caller wraps ``run_forever``).

The module imports cleanly on non-Windows platforms (``_kernel32``/``msvcrt``
stay ``None``); ``windows_raw_console`` raises ``OSError`` there so callers can
fall back to line mode.
"""

import contextlib
import ctypes
import sys

try:
    import msvcrt
except ImportError:
    # Non-Windows platform; read_key/windows_raw_console are not usable there.
    msvcrt = None  # type: ignore

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

# Console mode flags (wincon.h)
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

# Flags cleared for raw input: line buffering, echo, and Ctrl-C/Ctrl-Break
# signal synthesis (so Ctrl-C is delivered as "\x03" instead of SIGINT).
_RAW_INPUT_CLEARED = ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT

if sys.platform == "win32":
    import ctypes.wintypes

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
else:
    _kernel32 = None

# Fallback mapping for "enhanced" keys that some Windows consoles still deliver
# as a \\x00/\\xe0 prefix plus a scan code even with VT input enabled. Values
# are the equivalent VT sequences the POSIX path would send.
_ENHANCED_KEY_SEQUENCES = {
    "H": "\x1b[A",  # Up
    "P": "\x1b[B",  # Down
    "K": "\x1b[D",  # Left
    "M": "\x1b[C",  # Right
    "G": "\x1b[H",  # Home
    "O": "\x1b[F",  # End
    "S": "\x1b[3~",  # Delete
    "I": "\x1b[5~",  # Page Up
    "Q": "\x1b[6~",  # Page Down
    "R": "\x1b[2~",  # Insert
    "s": "\x1b[1;2D",  # Shift-Left
    "t": "\x1b[1;2C",  # Shift-Right
}


@contextlib.contextmanager
def windows_raw_console():
    """Context manager enabling raw console input and VT output.

    Raises ``OSError`` when the setup fails (e.g. stdin is not a Windows
    console, or SetConsoleMode rejects the new mode); in that case no mode has
    been changed yet. Original modes are always restored on exit.
    """
    if _kernel32 is None or msvcrt is None:
        raise OSError("Windows console APIs are unavailable on this platform")

    h_in = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    in_mode = ctypes.wintypes.DWORD()
    out_mode = ctypes.wintypes.DWORD()

    if not _kernel32.GetConsoleMode(h_in, ctypes.byref(in_mode)):
        raise OSError("stdin is not a Windows console")

    out_is_console = bool(_kernel32.GetConsoleMode(h_out, ctypes.byref(out_mode)))

    raw_in = (in_mode.value & ~_RAW_INPUT_CLEARED) | ENABLE_VIRTUAL_TERMINAL_INPUT
    if not _kernel32.SetConsoleMode(h_in, raw_in):
        raise OSError(f"SetConsoleMode(stdin, {raw_in:#x}) failed")

    out_touched = False
    if out_is_console:
        vt_out = out_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        out_touched = bool(_kernel32.SetConsoleMode(h_out, vt_out))

    try:
        yield
    finally:
        # Restore whatever the console had before, best effort.
        _kernel32.SetConsoleMode(h_in, in_mode.value)
        if out_touched:
            _kernel32.SetConsoleMode(h_out, out_mode.value)


def read_key():
    """Read one keypress from the Windows console.

    Returns the character as ``str`` (forwarded verbatim, UTF-8-encoded by the
    JSON layer). Returns ``None`` on EOF; ``""`` for unrecognized enhanced-key
    combinations (dropped). Never raises on Ctrl-C: ``"\\x03"`` is returned so
    the caller can forward it to the remote shell like POSIX raw mode does.
    """
    if msvcrt is None:
        raise OSError("msvcrt is unavailable on this platform")
    try:
        ch = msvcrt.getwch()
    except EOFError:
        return None
    except KeyboardInterrupt:
        # Not expected with ENABLE_PROCESSED_INPUT cleared, but if a Ctrl-C
        # surfaces as a signal anyway, forward it to the remote shell instead
        # of tearing down the CLI.
        return "\x03"
    if ch in ("\x00", "\xe0"):
        # Enhanced-key prefix; the following read carries the scan code.
        try:
            code = msvcrt.getwch()
        except (EOFError, KeyboardInterrupt):
            return ""
        return _ENHANCED_KEY_SEQUENCES.get(code, "")
    return ch
