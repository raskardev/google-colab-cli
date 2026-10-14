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
  Ctrl-C is delivered as a plain ``"\\x03"`` key event (no local SIGINT).
  Keys are read with ``ReadConsoleInputW`` (raw ``INPUT_RECORD`` structs):
  text keys — including AltGr characters such as ``@ € #``, surrogate pairs,
  Ctrl-C, ``\\r``, Backspace, Tab, Esc — are forwarded via their UnicodeChar;
  non-text keys (arrows, Home/End, PgUp/PgDn, Insert/Delete, F1-F12) are
  mapped to the xterm sequences the POSIX path would send. Reading raw input
  records (instead of ``msvcrt.getwch``) disambiguates real enhanced keys from
  typed characters like ``à`` (U+00E0) or Ctrl-Space, which share the
  ``\\x00``/``\\xe0`` prefix codes. Unmapped keys are dropped; they never end
  the input loop.
- Output: ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` is set on the stdout handle so
  the remote ANSI output renders in Windows Terminal / conhost.

All console modes are restored when the returned context manager exits, on
success, on error, and on websocket close (the caller wraps ``run_forever``).

The module imports cleanly on non-Windows platforms (``_kernel32`` and the
ctypes record structs stay ``None``); ``windows_raw_console`` raises ``OSError``
there so callers can fall back to line mode.
"""

import contextlib
import ctypes
import sys

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

# Console mode flags (wincon.h)
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

# INPUT_RECORD event types (wincon.h)
KEY_EVENT = 0x0001

# Flags cleared for raw input: line buffering, echo, and Ctrl-C/Ctrl-Break
# signal synthesis (so Ctrl-C is delivered as "\x03" instead of SIGINT).
_RAW_INPUT_CLEARED = ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT

# How many INPUT_RECORDs to fetch per blocking ReadConsoleInputW call.
_RECORD_BATCH = 16

# Virtual-key codes that produce no UnicodeChar, mapped to the xterm sequences
# a POSIX terminal would send for the same keypress. Keys without a VT
# equivalent are absent and simply dropped (translate_key_event returns "").
_VK_SEQUENCES = {
    0x21: "\x1b[5~",  # PRIOR (Page Up)
    0x22: "\x1b[6~",  # NEXT (Page Down)
    0x23: "\x1b[F",  # END
    0x24: "\x1b[H",  # HOME
    0x25: "\x1b[D",  # LEFT
    0x26: "\x1b[A",  # UP
    0x27: "\x1b[C",  # RIGHT
    0x28: "\x1b[B",  # DOWN
    0x2D: "\x1b[2~",  # INSERT
    0x2E: "\x1b[3~",  # DELETE
    0x70: "\x1bOP",  # F1
    0x71: "\x1bOQ",  # F2
    0x72: "\x1bOR",  # F3
    0x73: "\x1bOS",  # F4
    0x74: "\x1b[15~",  # F5
    0x75: "\x1b[17~",  # F6
    0x76: "\x1b[18~",  # F7
    0x77: "\x1b[19~",  # F8
    0x78: "\x1b[20~",  # F9
    0x79: "\x1b[21~",  # F10
    0x7A: "\x1b[23~",  # F11
    0x7B: "\x1b[24~",  # F12
}

if sys.platform == "win32":
    import ctypes.wintypes

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class _CHAR_UNION(ctypes.Union):
        _fields_ = [
            ("UnicodeChar", ctypes.wintypes.WCHAR),
            ("AsciiChar", ctypes.c_char),
        ]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", ctypes.wintypes.BOOL),
            ("wRepeatCount", ctypes.wintypes.WORD),
            ("wVirtualKeyCode", ctypes.wintypes.WORD),
            ("wVirtualScanCode", ctypes.wintypes.WORD),
            ("uChar", _CHAR_UNION),
            ("dwControlKeyState", ctypes.wintypes.DWORD),
        ]

    class _INPUT_RECORD(ctypes.Structure):
        class _EVENT_UNION(ctypes.Union):
            _fields_ = [
                ("KeyEvent", _KEY_EVENT_RECORD),
                # Other event types are ignored; only their size matters so
                # the union is at least as large as KEY_EVENT_RECORD.
                ("Padding", ctypes.wintypes.DWORD * 4),
            ]

        _fields_ = [
            ("EventType", ctypes.wintypes.WORD),
            ("Event", _EVENT_UNION),
        ]

else:
    _kernel32 = None  # type: ignore[assignment]
    _INPUT_RECORD = None  # type: ignore[assignment]


@contextlib.contextmanager
def windows_raw_console():
    """Context manager enabling raw console input and VT output.

    Raises ``OSError`` when the setup fails (e.g. stdin is not a Windows
    console, or SetConsoleMode rejects the new mode); in that case no mode has
    been changed yet. Original modes are always restored on exit.
    """
    if _kernel32 is None:
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


def translate_key_event(key_down, repeat, vk, unicode_char, control_state):
    """Translate one console key event into the text/VT sequence to forward.

    Pure function so it is testable on any OS.

    ``key_down``   — ``bKeyDown`` (key-up records produce no output).
    ``repeat``     — ``wRepeatCount``; the keystroke is repeated accordingly.
    ``vk``         — ``wVirtualKeyCode``.
    ``unicode_char`` — the event's UnicodeChar (``""``/``"\\x00"`` when the
      event carries no character, i.e. non-text virtual keys).
    ``control_state`` — ``dwControlKeyState`` (accepted but unused: AltGr
      characters already arrive via ``unicode_char`` and must stay plain).

    Returns the string to forward, or ``""`` for events that are dropped
    (key-up, no character and an unmapped virtual key).
    """
    if not key_down:
        return ""
    if unicode_char and unicode_char != "\x00":
        return unicode_char * repeat
    seq = _VK_SEQUENCES.get(vk, "")
    return seq * repeat if seq else ""


def _record_to_tuple(record):
    """Flatten a real ``_INPUT_RECORD`` to a plain tuple for ``read_key``.

    Returns ``(key_down, repeat, vk, unicode_char, control_state)``; the
    unicode char is a 1-char ``str`` (``"\\x00"`` when absent).
    """
    event = record.Event.KeyEvent
    return (
        bool(event.bKeyDown),
        event.wRepeatCount,
        event.wVirtualKeyCode,
        event.uChar.UnicodeChar,
        event.dwControlKeyState,
    )


def _read_console_records(handle, count):
    """Blocking read of up to ``count`` console input records.

    Returns a list of ``(key_down, repeat, vk, unicode_char, control_state)``
    tuples. An empty list means the read failed (treated as EOF by
    ``read_key``); non-key events are filtered out here.
    """
    if _INPUT_RECORD is None:
        raise OSError("Windows console structures are unavailable on this platform")
    records = (_INPUT_RECORD * count)()
    events_read = ctypes.wintypes.DWORD()
    if not _kernel32.ReadConsoleInputW(
        handle, records, count, ctypes.byref(events_read)
    ):
        return []
    return [
        _record_to_tuple(records[i])
        for i in range(events_read.value)
        if records[i].EventType == KEY_EVENT
    ]


def read_key():
    """Read the next forwarded keystroke from the Windows console.

    Returns the text/VT sequence as ``str`` (forwarded verbatim, UTF-8-encoded
    by the JSON layer), or ``None`` when the console read fails (EOF) — the
    caller ends the input loop. Events that produce no output (key-up,
    non-key records, unmapped virtual keys) are skipped: ``read_key`` keeps
    blocking until it has something to forward, so a dropped key never ends a
    session. Never raises on Ctrl-C: ``"\\x03"`` is returned so the caller can
    forward it to the remote shell like POSIX raw mode does.

    UTF-16 surrogate pairs (astral-plane characters) are combined across
    records. A lone high surrogate is corrupt console input: it is dropped
    (lone surrogates cannot be UTF-8 encoded for the JSON layer) and reading
    continues with the next key.
    """
    if _kernel32 is None:
        raise OSError("Windows console APIs are unavailable on this platform")

    h_in = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
    pending_high = ""
    while True:
        try:
            records = _read_console_records(h_in, _RECORD_BATCH)
        except KeyboardInterrupt:
            # Not expected with ENABLE_PROCESSED_INPUT cleared, but if a
            # Ctrl-C surfaces as a signal anyway, forward it to the remote
            # shell instead of tearing down the CLI.
            return "\x03"
        if not records:
            # Read failed: signal EOF to the caller.
            return None
        for key_down, repeat, vk, uni, ctrl in records:
            if not key_down:
                continue
            if uni:
                code = ord(uni)
                if 0xD800 <= code <= 0xDBFF:
                    # High surrogate: stash it and wait for the low half. If a
                    # previous one is still pending it is unpaired/corrupt —
                    # drop it rather than forwarding a lone surrogate.
                    pending_high = uni
                    continue
                if 0xDC00 <= code <= 0xDFFF and pending_high:
                    # Combine into the actual astral code point: keeping the
                    # raw UTF-16 pair would break UTF-8 encoding downstream.
                    combined = chr(
                        0x10000
                        + ((ord(pending_high) - 0xD800) << 10)
                        + (code - 0xDC00)
                    )
                    pending_high = ""
                    return combined
                text = translate_key_event(key_down, repeat, vk, uni, ctrl)
            else:
                text = translate_key_event(key_down, repeat, vk, uni, ctrl)
            if pending_high:
                # A different key arrived before the low surrogate: the pair
                # is broken. Drop the lone surrogate (it cannot be UTF-8
                # encoded) and forward this event normally.
                pending_high = ""
            if text:
                return text
            # Dropped event (unmapped virtual key, ...): keep reading.
