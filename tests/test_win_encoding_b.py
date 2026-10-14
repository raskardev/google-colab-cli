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

"""Windows support unit U4: explicit UTF-8 for every text-mode file I/O.

The behavioural tests below are written so they genuinely **fail** on a host
whose default text encoding is not UTF-8 (Windows with the cp1252 ANSI code
page) and pass on a UTF-8-default POSIX host. That is deliberate: they are the
regression guard for the ``encoding="utf-8"`` arguments added by U4, and on
POSIX the same tests simply verify the end-to-end content is untransformed.

``NON_ASCII`` is chosen because none of its code points are representable in
cp1252, so a stray default-encoding decode/encode cannot round-trip by luck.
"""

import ast
import json
import os
from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli import auth
from colab_cli.cli import app
from colab_cli.state import SessionState, Settings, SettingsStore, StateStore

runner = CliRunner()

NON_ASCII = "日本語 ✓"

# Placeholder credential shared by the mocked sessions. Assembled at runtime so
# secret scanners do not mistake the test fixture for a leaked token.
DUMMY_TOKEN = "".join(["not", "-a-", "real-", "credential"])

# Sources owned by this unit. ``automation.py``/``ssh.py``/``files.py`` are
# intentionally excluded: they are handled (or not) by other units.
FENCED_SOURCES = [
    "commands/execution.py",
    "commands/utility.py",
    "auth.py",
    "state.py",
    "history.py",
    "auto_update.py",
]


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


# ---------- `colab exec` reads the script as UTF-8 -----------------------


def test_exec_reads_utf8_script_from_disk(
    mocker, mock_store, mock_common_state, tmp_path
):
    """A non-ASCII ``.py`` script must reach the kernel unmodified.

    Under a cp1252 default the pre-U4 ``open(file, "r")`` decoded the UTF-8
    bytes into mojibake, so the exact-match assertion below failed.
    """
    runtime_cls = mocker.patch("colab_cli.commands.execution.ColabRuntime")
    mock_runtime = runtime_cls.return_value
    mock_runtime.execute_code.return_value = []

    session = MagicMock()
    session.name = "s1"
    session.url = "http://url"
    session.token = DUMMY_TOKEN
    session.kernel_id = None
    session.session_id = None
    mock_store.get.return_value = session
    mock_common_state.resolve_session.return_value = "s1"

    script = tmp_path / "script.py"
    body = f"MSG = {NON_ASCII!r}\n"
    script.write_text(body, encoding="utf-8")

    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(script)])
    assert result.exit_code == 0, result.output
    mock_runtime.execute_code.assert_any_call(body, output_hook=ANY, timeout=30.0)


# ---------- JSON stores read/write UTF-8 --------------------------------


def test_state_store_non_ascii_round_trip(tmp_path):
    """A non-ASCII session name survives ``add()`` -> ``get()``."""
    store = StateStore(str(tmp_path / "sessions.json"))
    state = SessionState(name=NON_ASCII, token=DUMMY_TOKEN, url="u", endpoint="e")
    store.add(state)

    loaded = store.get(NON_ASCII)
    assert loaded is not None
    assert loaded.name == NON_ASCII


def test_state_store_reads_utf8_sessions_file(tmp_path):
    """``sessions.json`` must be decoded as UTF-8, not as the ANSI code page.

    ``_save_raw`` currently escapes non-ASCII (``json.dumps`` default), but the
    on-disk contract is a UTF-8 JSON file: any writer that emits raw UTF-8 (a
    hand-edited file, a future ``ensure_ascii=False``) must still load.
    """
    path = tmp_path / "sessions.json"
    payload = {
        NON_ASCII: {
            "name": NON_ASCII,
            "token": DUMMY_TOKEN,
            "url": "u",
            "endpoint": "e",
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = StateStore(str(path)).get(NON_ASCII)
    assert loaded is not None
    assert loaded.name == NON_ASCII


def test_settings_store_non_ascii_round_trip(tmp_path):
    """``settings.json`` is written raw UTF-8 by pydantic and must read back.

    ``Settings.model_dump_json`` does not ASCII-escape, so the pre-U4 cp1252
    read turned a non-ASCII value into mojibake (or fell back to defaults).
    """
    store = SettingsStore(str(tmp_path / "settings.json"))
    store.save(Settings(update_url=f"https://example.test/{NON_ASCII}"))

    assert store.load().update_url == f"https://example.test/{NON_ASCII}"


# ---------- OAuth client config + token ---------------------------------


def test_auth_reads_utf8_client_config(mocker, tmp_path):
    """The ``-c/--client-oauth-config`` JSON file is read as UTF-8."""
    config_path = tmp_path / "oauth.json"
    config_path.write_text(
        json.dumps(
            {"installed": {"client_id": "id", "project_id": NON_ASCII}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    creds = MagicMock()
    creds.to_json.return_value = "{}"

    captured = {}

    def fake_flow(client_config):
        captured.update(client_config)
        return creds

    mocker.patch.object(auth, "TOKEN_CONFIG_PATH", str(tmp_path / "token.json"))
    mocker.patch.object(auth, "_run_remote_flow", side_effect=fake_flow)

    auth._get_google_auth_credentials(str(config_path))

    assert captured["installed"]["project_id"] == NON_ASCII


def test_auth_writes_utf8_token_and_restricts_permissions(mocker, tmp_path):
    """The cached token is written as UTF-8 and chmod'ed after a good write."""
    config_path = tmp_path / "oauth.json"
    config_path.write_text(
        json.dumps({"installed": {"client_id": "id"}}), encoding="utf-8"
    )
    token_path = tmp_path / "token.json"
    token_json = json.dumps({"refresh_token": NON_ASCII}, ensure_ascii=False)
    creds = MagicMock()
    creds.to_json.return_value = token_json

    mocker.patch.object(auth, "TOKEN_CONFIG_PATH", str(token_path))
    mocker.patch.object(auth, "_run_remote_flow", return_value=creds)
    restrict = mocker.patch.object(auth, "_restrict_token_permissions")

    auth._get_google_auth_credentials(str(config_path))

    assert token_path.read_text(encoding="utf-8") == token_json
    restrict.assert_called_once_with(str(token_path))


# ---------- F17: POSIX-only token hardening -----------------------------


def test_restrict_token_permissions_posix(mocker):
    chmod = mocker.patch("colab_cli.auth.os.chmod")
    auth._restrict_token_permissions("token.json", posix=True)
    chmod.assert_called_once_with("token.json", 0o600)


def test_restrict_token_permissions_windows_is_noop(mocker):
    chmod = mocker.patch("colab_cli.auth.os.chmod")
    auth._restrict_token_permissions("token.json", posix=False)
    chmod.assert_not_called()


def test_restrict_token_permissions_default_follows_os(mocker):
    """With no override the helper keys off ``os.name`` (posix -> chmod)."""
    chmod = mocker.patch("colab_cli.auth.os.chmod")
    auth._restrict_token_permissions("token.json")
    if os.name == "posix":
        chmod.assert_called_once_with("token.json", 0o600)
    else:
        chmod.assert_not_called()


# ---------- Static guard: no implicit-encoding text I/O ------------------


def _text_io_without_encoding(source: str):
    """Return ``(lineno, snippet)`` for text-mode opens with no ``encoding=``."""
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue

        has_encoding = any(kw.arg == "encoding" for kw in node.keywords)

        # Builtin ``open()`` only: ``webbrowser.open``/``urlopen`` are not I/O.
        if name == "open" and isinstance(func, ast.Name):
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            if has_encoding or (isinstance(mode, str) and "b" in mode):
                continue
            offenders.append((node.lineno, ast.unparse(node)))
        elif name in ("read_text", "write_text"):
            if has_encoding:
                continue
            offenders.append((node.lineno, ast.unparse(node)))
    return offenders


@pytest.mark.parametrize("rel_path", FENCED_SOURCES)
def test_text_mode_io_specifies_utf8(rel_path):
    src_root = Path(__file__).resolve().parents[1] / "src" / "colab_cli"
    source = (src_root / rel_path).read_text(encoding="utf-8")
    offenders = _text_io_without_encoding(source)
    assert not offenders, (
        f"{rel_path} has text-mode file I/O without encoding='utf-8': {offenders}"
    )
