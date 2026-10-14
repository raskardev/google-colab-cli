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

"""Windows support unit U7: ``colab update --install`` on Windows.

Covers the platform gate (Windows is now supported), the portable ``uv tool``
install detection, and the command actually handed to ``subprocess``. The real
upgrade is never executed here — ``subprocess.run`` is always mocked.
"""

import pytest

from colab_cli import auto_update

LINUX_UV = "/home/user/.local/share/uv/tools/google-colab-cli/bin/python"
WINDOWS_UV = (
    r"C:\Users\Alex\AppData\Roaming\uv\tools\google-colab-cli\Scripts\python.exe"
)
WINDOWS_UV_UPPERCASE = (
    r"C:\Users\Alex\AppData\Roaming\UV\Tools\google-colab-cli\Scripts\python.exe"
)
WINDOWS_PIP = r"C:\Python312\python.exe"
LINUX_PIP = "/usr/bin/python3"
NOT_UV_TOOLS = "/opt/uv/tools-extra/python"


# ---------- platform gate ------------------------------------------------


@pytest.mark.parametrize(
    "system, expected",
    [
        ("Linux", True),
        ("Darwin", True),
        ("Windows", True),
        ("FreeBSD", False),
    ],
)
def test_is_self_install_supported(mocker, system, expected):
    mocker.patch("colab_cli.auto_update.platform.system", return_value=system)
    assert auto_update.is_self_install_supported() is expected


# ---------- uv tool detection -------------------------------------------


@pytest.mark.parametrize(
    "executable, expected",
    [
        (LINUX_UV, True),
        (WINDOWS_UV, True),
        (WINDOWS_UV_UPPERCASE, True),
        (WINDOWS_PIP, False),
        (LINUX_PIP, False),
        (NOT_UV_TOOLS, False),
    ],
)
def test_is_uv_tool_install_detection(mocker, executable, expected):
    mocker.patch("sys.executable", executable)
    assert auto_update._is_uv_tool_install() is expected


def test_is_uv_tool_install_accepts_explicit_path():
    assert auto_update._is_uv_tool_install(WINDOWS_UV) is True
    assert auto_update._is_uv_tool_install(WINDOWS_PIP) is False


# ---------- recommended install command ---------------------------------


@pytest.mark.parametrize(
    "system, executable, expected",
    [
        ("Linux", LINUX_UV, f"uv tool install -U {auto_update.PYPI_PACKAGE_NAME}"),
        ("Windows", WINDOWS_UV, f"uv tool install -U {auto_update.PYPI_PACKAGE_NAME}"),
        (
            "Windows",
            WINDOWS_PIP,
            f"pip install --upgrade {auto_update.PYPI_PACKAGE_NAME}",
        ),
        (
            "Darwin",
            LINUX_PIP,
            f"pip install --upgrade {auto_update.PYPI_PACKAGE_NAME}",
        ),
    ],
)
def test_get_install_command(mocker, system, executable, expected):
    mocker.patch("colab_cli.auto_update.platform.system", return_value=system)
    mocker.patch("sys.executable", executable)
    assert auto_update._get_install_command() == expected


# ---------- self_install subprocess -------------------------------------


@pytest.mark.parametrize(
    "system, executable, expected_cmd",
    [
        (
            "Linux",
            LINUX_UV,
            ["uv", "tool", "install", "-U", auto_update.PYPI_PACKAGE_NAME],
        ),
        (
            "Windows",
            WINDOWS_UV,
            ["uv", "tool", "install", "-U", auto_update.PYPI_PACKAGE_NAME],
        ),
        (
            "Windows",
            WINDOWS_PIP,
            [
                WINDOWS_PIP,
                "-m",
                "pip",
                "install",
                "-U",
                auto_update.PYPI_PACKAGE_NAME,
            ],
        ),
        (
            "Darwin",
            LINUX_PIP,
            [
                LINUX_PIP,
                "-m",
                "pip",
                "install",
                "-U",
                auto_update.PYPI_PACKAGE_NAME,
            ],
        ),
    ],
)
def test_self_install_uses_portable_command(mocker, system, executable, expected_cmd):
    mocker.patch("colab_cli.auto_update.platform.system", return_value=system)
    mocker.patch("sys.executable", executable)
    run = mocker.patch(
        "colab_cli.auto_update.subprocess.run",
        return_value=mocker.Mock(returncode=0),
    )

    auto_update.self_install()

    run.assert_called_once_with(expected_cmd)
