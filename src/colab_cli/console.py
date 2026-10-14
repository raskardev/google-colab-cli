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
import logging
import os
import signal
import sys
import threading
import time

try:
    import termios
    import tty
except ImportError:
    # Windows has neither termios nor tty. connect_console() below degrades
    # gracefully to line-buffered input when these are unavailable.
    termios = None  # type: ignore
    tty = None  # type: ignore
from urllib.parse import urlparse

from colab_cli import win_console
import websocket

from colab_cli.state import SessionState

logger = logging.getLogger(__name__)

# Global flag to stop the read thread when the websocket closes
_is_running = False
_last_error = None

# When stdin is piped and reaches EOF, we send "exit\n" to the remote shell and
# then wait this many seconds for any remaining output (the shell's goodbye,
# tmux teardown messages, etc.) to flush before closing the websocket from the
# client side. Empirically 0.5s is enough for the typical /colab/tty backend
# wrapped in tmux + bash; bumping it just delays exit, lowering it risks
# truncating tail output.
PIPED_EOF_GRACE_SECONDS = 0.5

# Interval for polling the terminal size on platforms without SIGWINCH
# (Windows). Only active while a console session is running; the poller thread
# exits as soon as the session ends (stop event set on close). Fire-and-forget:
# no thread outlives the command.
RESIZE_POLL_INTERVAL_SECONDS = 0.5

_resize_stop_event = None

# True only while a Windows TTY session has raw console mode active; switches
# read_stdin_loop from sys.stdin.read to win_console.read_key.
_win_raw_input_active = False


def on_message(ws, message):
    """Callback for when a message is received from the server."""
    try:
        data = json.loads(message)
        if "data" in data:
            # The backend sends raw ANSI escape sequences and string content.
            # We write it directly to stdout buffer to avoid python print() formatting.
            sys.stdout.buffer.write(data["data"].encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception as e:
        logger.debug(f"Error parsing message: {e}")


def on_error(ws, error):
    """Callback for when a websocket error occurs."""
    global _last_error
    _last_error = error
    logger.error(f"WebSocket Error: {error}")


def on_close(ws, close_status_code, close_msg):
    """Callback for when the websocket is closed."""
    global _is_running
    _is_running = False
    stop_resize_poller()


def _resize_poller(ws, stop_event, interval=RESIZE_POLL_INTERVAL_SECONDS):
    """Polls the terminal size and forwards it when it changes.

    Used on platforms without SIGWINCH (Windows). Every size change —
    including the first successful probe after earlier failures — is
    forwarded with the same payload as send_terminal_size. The poller is
    started right after on_open sent the initial size, so a duplicate first
    send is harmless.
    """
    last = None
    while not stop_event.is_set():
        try:
            size = os.get_terminal_size()
            current = (size.columns, size.lines)
            if current != last:
                ws.send(json.dumps({"cols": current[0], "rows": current[1]}))
            last = current
        except Exception as e:
            # Redirected/invalid stdin or a dying websocket: keep polling until
            # the stop event fires so a transient error doesn't kill resize.
            logger.debug(f"Resize poll failed: {e}")
        stop_event.wait(interval)


def send_terminal_size(ws):
    """Sends the current terminal size to the remote backend."""
    try:
        size = os.get_terminal_size()
        payload = json.dumps({"cols": size.columns, "rows": size.lines})
        ws.send(payload)
    except Exception as e:
        logger.debug(f"Failed to send terminal size: {e}")


def start_resize_poller(ws):
    """Starts the resize-polling thread when SIGWINCH is unavailable.

    No-op on POSIX (SIGWINCH handles resize) and when stdin is not a TTY
    (piped sessions have no meaningful terminal size).
    """
    global _resize_stop_event
    if getattr(signal, "SIGWINCH", None) is not None:
        return
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
        return
    _resize_stop_event = threading.Event()
    thread = threading.Thread(
        target=_resize_poller, args=(ws, _resize_stop_event), daemon=True
    )
    thread.start()


def stop_resize_poller():
    """Signals the resize poller (if any) to exit."""
    if _resize_stop_event is not None:
        _resize_stop_event.set()


def read_stdin_loop(ws, is_tty):
    """Reads keystrokes from stdin and forwards them over the websocket.

    On Windows TTY sessions (raw console mode active) characters come from
    win_console.read_key(); otherwise sys.stdin.read(1) is used, which yields
    line-buffered input on Windows and raw bytes on POSIX raw mode.
    """
    while _is_running:
        try:
            if _win_raw_input_active and is_tty:
                char = win_console.read_key()
                if char is None:
                    # EOF from the Windows console: behave like the POSIX TTY
                    # path (stop reading; the remote shell owns the socket).
                    break
            else:
                # Read a single character (or escape sequence byte)
                char = sys.stdin.read(1)
            if not char:
                if not is_tty:
                    # Piped input has reached EOF. The remote /colab/tty
                    # endpoint wraps bash in tmux which intercepts \x04
                    # (Ctrl-D) as a literal character, so it never exits.
                    # Instead send "exit\n" so bash voluntarily terminates,
                    # wait a short grace period for the shell's goodbye
                    # output to drain back to us, then close the websocket
                    # ourselves to guarantee the client unblocks.
                    try:
                        ws.send(json.dumps({"data": "exit\n"}))
                    except Exception:
                        pass
                    time.sleep(PIPED_EOF_GRACE_SECONDS)
                    try:
                        ws.close()
                    except Exception:
                        pass
                break
            ws.send(json.dumps({"data": char}))
        except Exception:
            break


def on_open(ws):
    """Callback for when the websocket connection is opened."""
    global _is_running
    _is_running = True

    # Send initial terminal size
    send_terminal_size(ws)

    # Setup the background thread to read from stdin
    is_tty = sys.stdin.isatty()
    thread = threading.Thread(target=read_stdin_loop, args=(ws, is_tty), daemon=True)
    thread.start()

    # On platforms without SIGWINCH (Windows), watch for resizes by polling.
    start_resize_poller(ws)


def connect_console(session: SessionState):
    """
    Connects to the Colab TTY endpoint and sets up a raw terminal session.

    POSIX TTYs get termios raw mode + SIGWINCH resize. Windows TTYs get raw
    input + VT output via colab_cli.win_console (and resize via polling, see
    start_resize_poller). Piped stdin keeps line mode on every platform.
    """
    global _is_running, _last_error, _win_raw_input_active
    _last_error = None

    # Construct the WebSocket URL from the base URL
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={session.token}"

    is_tty = sys.stdin.isatty()
    fd = None
    old_settings = None
    can_raw = termios is not None and tty is not None and is_tty
    if can_raw:
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        except Exception:
            fd = None
            old_settings = None
            can_raw = False

    ws = websocket.WebSocketApp(
        url=ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def handle_sigwinch(signum, frame):
        """Handle window resize events."""
        if _is_running:
            send_terminal_size(ws)

    sigwinch = getattr(signal, "SIGWINCH", None)
    with contextlib.ExitStack() as stack:
        if sys.platform == "win32" and is_tty:
            # Raw keyboard input + VT rendering for the whole session. If the
            # console setup fails (e.g. stdin is not a real console), degrade
            # to line mode instead of failing the session.
            try:
                stack.enter_context(win_console.windows_raw_console())
                _win_raw_input_active = True
            except OSError as e:
                logger.debug(f"Windows raw console unavailable: {e}")
        try:
            if can_raw:
                tty.setraw(fd, termios.TCSANOW)
                if sigwinch is not None:
                    signal.signal(sigwinch, handle_sigwinch)

            # This is a blocking call until the connection is closed
            ws.run_forever()

            if _last_error:
                # Re-raise or wrap terminal errors
                err_msg = str(_last_error)
                if "404" in err_msg or "401" in err_msg:
                    # We raise a standard exception that the caller can recognize
                    raise RuntimeError(f"Connection failed: {err_msg}")
        finally:
            if can_raw:
                # Always ensure the terminal is restored to its original state
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, old_settings)
                except Exception:
                    pass
                # Restore the default signal handler for resize
                if sigwinch is not None:
                    try:
                        signal.signal(sigwinch, signal.SIG_DFL)
                    except Exception:
                        pass
            # Stop the resize poller (Windows) before restoring console modes
            # and deactivate the raw-key reader so the daemon stdin thread
            # cannot read keys from a restored console.
            _is_running = False
            stop_resize_poller()
            _win_raw_input_active = False
        print("\r\nConnection closed.")
