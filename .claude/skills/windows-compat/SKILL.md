---
name: windows-compat
description: Rules and pitfalls for keeping Colab CLI working on native Windows as well as Linux/macOS - platform guards, cmd.exe ProxyCommand quoting, raw console input via ReadConsoleInputW, UTF-8 I/O, and writing tests that pass on Windows CI without breaking POSIX. Use when touching ssh.py, console.py, win_console.py, file I/O, subprocess calls, signals or terminal handling, or when adding tests with platform branches.
---

# Windows compatibility (Colab CLI)

Background and decisions: `docs/plans/windows-support.md` (findings F1-F21, decisions D1-D5).

## Rules for product code
- **Signals**: never reference `signal.SIGHUP`/`SIGWINCH`/`SIGTSTP` directly; use
  `getattr(signal, "SIGHUP", None)` and skip `None`.
- **POSIX-only modules** (`termios`, `tty`, `fcntl`, `pty`, `pwd`): import inside a guard or
  `try/except ImportError`. `select()` on stdin/pipes does not work on Windows (sockets only).
- **Text I/O**: always pass `encoding="utf-8"` to `open()`/`read_text()`/`write_text()`; the
  Windows default is cp1252. `tests/test_encoding.py` has a static guard with a list of fenced
  modules: add new modules that do file I/O to it.
- **Temp files reopened by another process** (editor, subprocess): use `tempfile.mkstemp` +
  `os.close(fd)` + unlink in `finally`; `NamedTemporaryFile(delete=True)` can't be reopened on
  Windows.
- **Null device**: `NUL` on win32, `/dev/null` elsewhere (see ssh `UserKnownHostsFile`).
- **ssh ProxyCommand**: Win32-OpenSSH runs it through `cmd.exe`. `ssh.py` quotes args with
  whitespace or `& | ^ < > ( )` (MSVCRT rules, trailing backslashes doubled) and rejects `%`,
  `"`, `!`, CR/LF. POSIX keeps `shlex.join`. Platform switch is the patchable `_is_windows()`.
  `CommandLineToArgvW` (shell32) is NOT a valid oracle for trailing backslashes; python.exe's
  CRT is the real consumer.
- **Console raw mode (win32)**: `win_console.py` sets console modes with `SetConsoleMode`
  and reads `INPUT_RECORD`s with `ReadConsoleInputW`. Do not go back to `msvcrt.getwch()`: it
  can't tell a typed `à` (U+00E0) from the enhanced-key prefix. Keep key translation in the
  pure `translate_key_event` so it is testable on any OS. A dropped key must never end the
  read loop (only `None` = read failure does). Resize uses a poller thread where SIGWINCH is
  missing.
- `ctypes.wintypes` imports on every platform; `ctypes.windll`/`WinDLL`/`msvcrt` do not. Keep
  `ctypes.wintypes` at module top; create kernel32 handles only under `sys.platform == "win32"`.

## Rules for tests (CI: windows-latest x py3.12/3.13 only)
- CI runs only on Windows (user decision, 2026-10-15) and no Linux machine is available locally
  (no WSL): nothing exercises POSIX, so reason about those paths explicitly. Before
  finishing, grep changed tests for `windll`, `WinDLL`, `msvcrt`, `wintypes`, `C:\\`, `NUL`,
  `CommandLineToArgvW` and check each is behind `skipif(sys.platform != "win32")` or a fake.
- Tests that patch `sys.platform` still run real module-level code imported earlier: patch the
  module's own switch (`_is_windows`, `_kernel32`) instead.
- Typer forces a color terminal when `GITHUB_ACTIONS`/`FORCE_COLOR`/`PY_COLORS` is set; tests
  that parse `--help` output rely on the autouse `plain_help_output` fixture in
  `tests/conftest.py`. Reproduce CI-only help failures locally with `GITHUB_ACTIONS=true`.
- Moving a worktree breaks `.venv` console-script trampolines ("uv trampoline failed to
  canonicalize script path"): run `uv sync --reinstall`, or use `.venv\Scripts\python -m pytest`.
- prompt_toolkit tests need `create_app_session(input=create_pipe_input(), output=DummyOutput())`
  on Windows (pytest stdout is not a Win32 console).
- Run pytest with a hard timeout (PowerShell `Start-Job` + `Wait-Job -Timeout`). A hung run
  leaves orphan `pytest.exe`/`python.exe` processes holding files in the worktree: find them
  with `Get-CimInstance Win32_Process` filtered by the worktree path and stop only those.

## Manual checks only the user can run
Interactive `colab console` (`integration/repro_windows_console_raw/MANUAL_STEPS.md`),
`colab ssh` with Win32-OpenSSH (NUL, quoting), `colab update --install` (replacing a running
`colab.exe`), `colab auth`, `colab drivemount`.
