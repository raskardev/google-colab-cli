# Manual test: `colab console` raw mode on Windows (unit U8)

The automated suite covers the low-level behaviour with mocks (see
`tests/test_win_console.py` and the U8 tests in `tests/test_console.py`), but a
real Windows console needs a human with a live Colab VM and a real keyboard.
Run these steps in **Windows Terminal** (and once in a plain conhost
`cmd.exe` window) with PowerShell:

1. Start a session: `uv run colab new` (note the session name, e.g. `s1`),
   then `uv run colab console -s <name>`.

2. **Typing / echo**: characters appear immediately as you type (no need to
   press Enter first). Before this change, input only appeared after Enter.

3. **Arrow keys / history**: press Up/Down — the remote bash history cycles.
   Left/Right/Home/End/Delete move within the edited line.

4. **Ctrl-C**: press Ctrl-C at a bash prompt, then run something like
   `sleep 100` and press Ctrl-C — the remote command is interrupted and the
   console session stays alive. The CLI itself must NOT exit.

5. **Resize**: run `stty size` (or `echo $COLUMNS x $LINES`), resize the
   window, then run it again — the reported size must match the new window
   size within ~1 second, without restarting the session.

6. **Remote ANSI output**: `ls --color=auto` and `htop`-style output (e.g.
   `python -c "print('\x1b[31mRED\x1b[0m'"`) render in colour, not as raw
   `ESC[31m` text.

7. **Non-ASCII**: type e.g. `echo héllo ünïcode` — the remote echo is correct.

8. **Exit**: type `exit` — the session ends and `Connection closed.` prints;
   console modes must be fully restored afterwards (verify by typing in the
   same PowerShell window: normal line editing and echo work, Ctrl-C kills
   local commands again, `stty size` isn't applicable — just confirm the
   shell behaves normally).

9. **Piped regression**: `echo 'echo hello from pipe' | uv run colab console -s <name>`
   must still exit cleanly on EOF (prints the output and terminates).

10. Cleanup: `uv run colab sessions` shows no orphaned sessions; run
    `uv run colab stop -s <name>` if needed.

Known limitations (accepted for U8):
- Ctrl-D still does not exit the remote shell (tmux on the backend swallows
  `\x04`) — same behaviour as POSIX; use `exit` or Ctrl-C + `exit`.
- If a keypress is in flight exactly when the websocket closes, that one key
  may be swallowed by the daemon reader thread during teardown.
