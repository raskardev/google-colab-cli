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

import contextlib
import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

try:
    import termios
except ImportError:
    # Windows has no termios; tests requiring raw-terminal mode are skipped.
    termios = None  # type: ignore

from colab_cli.console import connect_console, on_message, on_open
from colab_cli.state import SessionState
import pytest

needs_termios = pytest.mark.skipif(
    termios is None, reason="requires termios (POSIX only)"
)


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev",
        endpoint="some-endpoint",
    )


class SyncReaderThread:
    """threading.Thread stand-in that runs the stdin reader synchronously.

    The resize poller (if any) is not exercised by these tests.
    """

    def __init__(self, target, args=None, daemon=None):
        self.target = target
        self.args = args or ()

    def start(self):
        if self.target.__name__ == "read_stdin_loop":
            self.target(*self.args)


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.fileno")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_initialization(
    mock_isatty,
    mock_fileno,
    mock_get_term_size,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    # Setup mocks
    mock_isatty.return_value = True
    mock_fileno.return_value = 0
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_tcgetattr.return_value = ["fake_attrs"]
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    # We don't want run_forever to actually block or start threads in the test
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # 1. Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url

    # 2. Verify raw mode setup and teardown
    mock_tcgetattr.assert_called_once_with(sys.stdin.fileno())
    mock_setraw.assert_called_once_with(
        sys.stdin.fileno(),
        termios.TCSANOW,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
    )

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(),
        termios.TCSANOW,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
        ["fake_attrs"],
    )


@needs_termios
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # In a piped environment, we should not attempt to use termios or tty
    mock_tcgetattr.assert_not_called()
    mock_setraw.assert_not_called()
    mock_tcsetattr.assert_not_called()


@patch("colab_cli.console.os.get_terminal_size")
def test_on_open_sends_terminal_size(mock_get_term_size):
    mock_ws = MagicMock()
    mock_get_term_size.return_value = os.terminal_size((100, 40))

    on_open(mock_ws)

    # Verify that the initial terminal size is sent
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload == {"cols": 100, "rows": 40}


@patch("colab_cli.console.sys.stdout.buffer.write")
@patch("colab_cli.console.sys.stdout.buffer.flush")
def test_on_message_writes_to_stdout(mock_flush, mock_write):
    mock_ws = MagicMock()
    test_data = "Hello \x1b[34mWorld\x1b[0m"
    message_json = json.dumps({"data": test_data})

    on_message(mock_ws, message_json)

    # Verify that the data is written exactly as received
    mock_write.assert_called_once_with(test_data.encode("utf-8"))
    mock_flush.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_piped_sends_exit_and_closes_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is piped and reaches EOF, the read thread should send 'exit\\n'
    to the remote shell and then close the websocket from the client side.

    The remote shell at /colab/tty is wrapped in tmux which swallows the bare
    \\x04 (Ctrl-D) we used to send, so EOF used to leave the websocket open
    indefinitely. Sending 'exit\\n' + ws.close() guarantees clean termination.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = False
    # Simulate piped stdin: returns one line then EOF
    mock_stdin.read.side_effect = ["e", "c", "h", "o", " ", "h", "i", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    console_mod._is_running = True
    with (
        patch("colab_cli.console.threading.Thread", SyncReaderThread),
        # Use a tiny grace period for the test
        patch("colab_cli.console.PIPED_EOF_GRACE_SECONDS", 0.01),
    ):
        on_open(mock_ws)

    # Collect what was sent to the websocket
    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]

    # Initial send is the terminal size; everything after is stdin chars or our exit string.
    # Verify "exit\n" was sent on EOF (one send per character)
    assert {"data": "exit\n"} in sent_payloads, (
        f"Expected 'exit\\n' to be sent on piped EOF, got: {sent_payloads}"
    )

    # Verify we closed the websocket from the client side
    mock_ws.close.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_tty_does_not_close_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is a real TTY and read() returns empty (which happens on
    Ctrl-D in raw mode), we should NOT inject 'exit\\n' or close the websocket
    \u2014 the user is in interactive mode and may have intended Ctrl-D as a literal
    char. The websocket lifecycle is owned by the remote shell in this case.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    # TTY EOF is rare but possible; should be passed through transparently
    mock_stdin.read.side_effect = [""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncReaderThread):
        on_open(mock_ws)

    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert {"data": "exit\n"} not in sent_payloads
    mock_ws.close.assert_not_called()


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_no_termios_degrades_gracefully(mock_isatty, mock_ws_app, mock_session):
    """Windows has no termios/tty/SIGWINCH. connect_console must not crash.

    Regression test for `ModuleNotFoundError: No module named 'termios'`
    which broke every `colab` command on Windows via
    cli -> execution -> console imports.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with (
        patch.object(console_mod, "termios", None),
        patch.object(console_mod, "tty", None),
        patch("colab_cli.console.threading.Thread"),
    ):
        connect_console(mock_session)

    mock_ws_instance.run_forever.assert_called_once()


# --- Windows resize polling (U8 step 1) ---


def test_resize_poller_sends_on_change_and_sends_once_per_size():
    """The resize poller forwards a {cols, rows} payload only when the
    terminal size actually changes."""
    import colab_cli.console as console_mod

    sizes = iter(
        [
            os.terminal_size((80, 24)),
            os.terminal_size((80, 24)),
            os.terminal_size((120, 40)),
        ]
    )
    stop_event = threading.Event()

    def fake_size():
        try:
            return next(sizes)
        except StopIteration:
            stop_event.set()  # end the poll loop deterministically
            return os.terminal_size((120, 40))

    mock_ws = MagicMock()
    with patch("colab_cli.console.os.get_terminal_size", side_effect=fake_size):
        console_mod._resize_poller(mock_ws, stop_event, interval=0)

    payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert payloads == [
        {"cols": 80, "rows": 24},
        {"cols": 120, "rows": 40},
    ]


def test_resize_poller_exits_when_stop_event_set():
    import colab_cli.console as console_mod

    stop_event = threading.Event()
    stop_event.set()
    mock_ws = MagicMock()

    console_mod._resize_poller(mock_ws, stop_event, interval=0)

    mock_ws.send.assert_not_called()


def test_resize_poller_survives_size_query_errors():
    """A failing os.get_terminal_size (e.g. redirected stdin) must not kill
    the poller or send a payload."""
    import colab_cli.console as console_mod

    stop_event = threading.Event()
    calls = {"n": 0}

    def failing_size():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("not a terminal")
        stop_event.set()
        return os.terminal_size((80, 24))

    mock_ws = MagicMock()
    with patch("colab_cli.console.os.get_terminal_size", side_effect=failing_size):
        console_mod._resize_poller(mock_ws, stop_event, interval=0)

    # The first successful probe after errors becomes (and forwards) the
    # recovered baseline; no crash, no retry spam.
    payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert payloads == [{"cols": 80, "rows": 24}]


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
def test_on_open_starts_resize_poller_without_sigwinch(mock_isatty, mock_get_term_size):
    """On platforms without SIGWINCH (Windows) a resize-poller thread starts
    when stdin is a TTY; it must not start on POSIX (SIGWINCH covers it)."""
    import colab_cli.console as console_mod

    started = []

    class RecordingThread:
        def __init__(self, target=None, args=None, daemon=None):
            self.target = target

        def start(self):
            started.append(self)

    mock_isatty.return_value = True
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_ws = MagicMock()

    # Simulate Windows: no SIGWINCH
    with (
        patch.object(console_mod.signal, "SIGWINCH", None, create=True),
        patch("colab_cli.console.threading.Thread", RecordingThread),
    ):
        console_mod._is_running = True
        try:
            on_open(mock_ws)
            targets = [t.target for t in started]
            assert console_mod._resize_poller in targets
            assert len(started) == 2  # stdin reader + resize poller
        finally:
            console_mod._is_running = False
            console_mod.stop_resize_poller()


def test_on_open_does_not_start_resize_poller_with_sigwinch():
    """POSIX keeps SIGWINCH: no polling thread."""
    import colab_cli.console as console_mod

    started = []

    class RecordingThread:
        def __init__(self, target=None, args=None, daemon=None):
            self.target = target

        def start(self):
            started.append(self)

    mock_ws = MagicMock()
    fake_sigwinch = 28  # truthy stand-in for signal.SIGWINCH
    with (
        patch.object(console_mod.signal, "SIGWINCH", fake_sigwinch, create=True),
        patch("colab_cli.console.threading.Thread", RecordingThread),
        patch.object(console_mod.sys.stdin, "isatty", return_value=True),
        patch.object(console_mod.sys, "stdin", MagicMock(read=lambda n: "")),
    ):
        console_mod._is_running = True
        try:
            on_open(mock_ws)
            targets = [t.target for t in started]
            assert console_mod._resize_poller not in targets
            assert len(started) == 1  # stdin reader only
        finally:
            console_mod._is_running = False
            console_mod.stop_resize_poller()


def test_stop_resize_poller_signals_event():
    import colab_cli.console as console_mod

    # Simulate Windows (no SIGWINCH) with a TTY so the poller actually starts
    with (
        patch.object(console_mod.signal, "SIGWINCH", None, create=True),
        patch.object(console_mod.sys.stdin, "isatty", return_value=True),
        patch("colab_cli.console.threading.Thread"),
    ):
        console_mod.start_resize_poller(MagicMock())
        assert console_mod._resize_stop_event is not None
        assert not console_mod._resize_stop_event.is_set()

        console_mod.stop_resize_poller()
        assert console_mod._resize_stop_event.is_set()


# --- Windows raw-key reader integration (U8 step 2) ---


def test_read_stdin_uses_win_read_key_when_active():
    """When use_win_raw is passed (Windows TTY), the stdin reader loop
    sources characters from colab_cli.win_console.read_key instead of
    sys.stdin.read, and a None key ends the loop without closing the socket."""
    import colab_cli.console as console_mod

    mock_ws = MagicMock()
    keys = iter(["l", "s", "\r", None])
    with patch.object(
        console_mod.win_console, "read_key", side_effect=lambda: next(keys)
    ):
        console_mod._is_running = True
        try:
            console_mod.read_stdin_loop(mock_ws, is_tty=True, use_win_raw=True)
        finally:
            console_mod._is_running = False

    sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert sent == [{"data": "l"}, {"data": "s"}, {"data": "\r"}]
    mock_ws.close.assert_not_called()


def test_read_stdin_piped_path_ignores_win_read_key():
    """Piped mode must keep using sys.stdin.read even when the caller passes
    use_win_raw=True (no regression on the piped path)."""
    import colab_cli.console as console_mod

    mock_ws = MagicMock()
    chars = iter(["h", "i", "\n", ""])
    mock_stdin = MagicMock()
    mock_stdin.read.side_effect = lambda n: next(chars)
    with patch.object(console_mod.sys, "stdin", mock_stdin):
        console_mod._is_running = True
        try:
            with patch("colab_cli.console.PIPED_EOF_GRACE_SECONDS", 0.01):
                console_mod.read_stdin_loop(mock_ws, is_tty=False, use_win_raw=True)
        finally:
            console_mod._is_running = False

    sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert {"data": "exit\n"} in sent
    mock_ws.close.assert_called_once()


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_connect_console_activates_win_raw_mode(mock_isatty, mock_ws_app, mock_session):
    """On win32 with a TTY, connect_console enters the Windows raw-console
    context (so raw input + VT output are active during run_forever) and wires
    the raw reader mode into the on_open callback."""
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    entered = []

    @contextlib.contextmanager
    def fake_raw_console():
        entered.append("enter")
        try:
            yield
        finally:
            entered.append("exit")

    with (
        patch("colab_cli.console.sys.platform", "win32"),
        patch.object(console_mod.win_console, "windows_raw_console", fake_raw_console),
        patch("colab_cli.console.threading.Thread"),
    ):
        console_mod._is_running = True
        connect_console(mock_session)

    assert entered == ["enter", "exit"]
    on_open_cb = mock_ws_app.call_args.kwargs["on_open"]
    assert on_open_cb.keywords == {"use_win_raw": True}
    mock_ws_instance.run_forever.assert_called_once()


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_connect_console_falls_back_when_win_raw_setup_fails(
    mock_isatty, mock_ws_app, mock_session
):
    """If the raw-mode setup fails (e.g. not a real console), the session
    still runs in degraded line mode instead of crashing."""
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    def failing_raw_console():
        raise OSError("stdin is not a console")
        yield  # pragma: no cover - makes it a generator for contextmanager

    with (
        patch("colab_cli.console.sys.platform", "win32"),
        patch.object(
            console_mod.win_console,
            "windows_raw_console",
            side_effect=OSError("stdin is not a console"),
        ),
        patch("colab_cli.console.threading.Thread"),
    ):
        connect_console(mock_session)  # must not raise

    mock_ws_instance.run_forever.assert_called_once()
    on_open_cb = mock_ws_app.call_args.kwargs["on_open"]
    assert on_open_cb.keywords == {"use_win_raw": False}
