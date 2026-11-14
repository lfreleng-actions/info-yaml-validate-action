# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Contract tests: the caller's environment cannot pick the Python run.

A composite action inherits the caller job's environment, so a PATH,
UV_PYTHON, VIRTUAL_ENV or PYTHONPATH set by an earlier step would
otherwise choose the interpreter uv and the validator run. These tests
read action.yaml and pin the guarded setup-uv step, and require every
later uv call to name the action's own interpreter, or run under
'env -i', and every Python start to use it with -I. The 'caller-env'
job in testing.yaml, and tests/test_action_workspace.py, run the steps
against planted interpreters and modules.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - runs the action's refusal step
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

SETUP_UV_WITH = {
    "version": "0.13.4",
    "working-directory": "${{ github.action_path }}",
    "tool-bin-dir": "${{ runner.temp }}/info-yaml-validate/bin",
    "enable-cache": False,
}
SETUP_UV_ENV = {
    "UV_NO_CONFIG": "1",
    "UV_NO_PROJECT": "1",
    "UV_PYTHON_PREFERENCE": "lookup-disabled",
    "UV_PYTHON_ARCH": "",
    "UV_PYTHON_INSTALL_DIR": "${{ runner.temp }}/info-yaml-validate/python",
}
UV_PATH_EXPR = "${{ steps.setup-uv.outputs.uv-path }}"
PYTHON_PATH_EXPR = "${{ steps.python.outputs.path }}"
# Command words that start uv or a Python interpreter when looked up on
# PATH.
BARE_COMMANDS = frozenset({"uv", "uvx", "python", "python3", "pip", "pip3"})
BASH = shutil.which("bash")


def _steps() -> list[dict[str, Any]]:
    with (ROOT / "action.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["runs"]["steps"]


def _commands(script: str) -> list[list[str]]:
    """Each command in a step script, as words, continuation lines joined.

    Comments are dropped, and a command is split at ';', '&&', '||',
    '|' and '$(' so that each part starts with its command word.
    """
    joined = re.sub(r"\\\n\s*", " ", script)
    commands: list[list[str]] = []
    for line in joined.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r";|&&|\|\||\||\$\(", line):
            part = part.replace(")", " ")
            try:
                words = shlex.split(part, posix=True)
            except ValueError:
                # A quotation that spans a split point
                words = [word.strip("'\"") for word in part.split()]
            if words:
                commands.append(words)
    return commands


RUN_STEPS = [step for step in _steps() if "run" in step]
RUN_STEP_IDS = [str(step.get("id") or step.get("name")) for step in RUN_STEPS]


def _starts_uv(words: list[str]) -> bool:
    return "$UV" in words or words[0] in {"uv", "uvx"}


def _starts_python(words: list[str]) -> bool:
    """A Python started by path, literal or held in a *PYTHON variable."""
    return bool(
        re.search(r"(^|/)python[0-9.]*(\.exe)?$", words[0])
        or re.fullmatch(r"\$\{?[A-Z_]*PYTHON\}?", words[0])
    )


def test_setup_uv_step_is_guarded() -> None:
    """setup-uv runs no interpreter a caller variable can choose."""
    setup = [
        step
        for step in _steps()
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert len(setup) == 1
    step = setup[0]
    assert step.get("id") == "setup-uv"
    assert step.get("with") == SETUP_UV_WITH
    assert step.get("env") == SETUP_UV_ENV


def test_setup_uv_follows_a_no_modify_path_refusal() -> None:
    """An earlier step fails when the caller sets UV_NO_MODIFY_PATH."""
    steps = _steps()
    index = next(
        position
        for position, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    refusals = [
        step
        for step in steps[:index]
        if "UV_NO_MODIFY_PATH" in step.get("run", "")
        and re.search(r"\bexit 1\b", step["run"])
    ]
    assert refusals
    for step in refusals:
        assert not step.get("if")
        assert not step.get("continue-on-error")


@pytest.mark.skipif(BASH is None, reason="bash is needed on PATH")
@pytest.mark.parametrize(
    ("caller_env", "refused"),
    [
        ({}, False),
        ({"UV_NO_MODIFY_PATH_X": "1"}, False),
        ({"UV_NO_MODIFY_PATH": "1"}, True),
        ({"UV_NO_MODIFY_PATH": ""}, True),
        ({"uv_no_modify_path": "0"}, True),
        ({"Uv_No_Modify_Path": "1"}, True),
    ],
)
def test_no_modify_path_refusal_runs(caller_env: dict[str, str], refused: bool) -> None:
    """The refusal step fails on the variable, in any case, and on nothing else."""
    assert BASH is not None
    steps = [
        step
        for step in _steps()
        if step.get("name") == "Refuse an inherited UV_NO_MODIFY_PATH"
    ]
    assert len(steps) == 1
    result = subprocess.run(  # nosec B603 - the step's own script
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", steps[0]["run"]],
        env={"PATH": os.environ.get("PATH", ""), **caller_env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if refused else 0), result.stdout + result.stderr
    if refused:
        assert f"unset {next(iter(caller_env))} " in result.stdout


def test_python_step_installs_under_an_empty_environment() -> None:
    """The trusted interpreter comes from uv run under 'env -i' alone."""
    python = [step for step in _steps() if step.get("id") == "python"]
    assert len(python) == 1
    step = python[0]
    assert step["env"]["UV"] == UV_PATH_EXPR
    uv_calls = [words for words in _commands(step["run"]) if _starts_uv(words)]
    assert uv_calls
    for words in uv_calls:
        assert words[:2] == ["env", "-i"], words
        assert words[words.index("$UV") + 1 : words.index("$UV") + 3] in (
            ["python", "install"],
            ["python", "find"],
        ), words


@pytest.mark.parametrize("step", RUN_STEPS, ids=RUN_STEP_IDS)
def test_run_step_starts_nothing_from_path(step: dict[str, Any]) -> None:
    """No step runs uv, uvx or Python by a name looked up on PATH."""
    for words in _commands(step["run"]):
        assert words[0] not in BARE_COMMANDS, (step.get("name"), words)


@pytest.mark.parametrize("step", RUN_STEPS, ids=RUN_STEP_IDS)
def test_run_step_uv_calls_name_the_trusted_interpreter(
    step: dict[str, Any],
) -> None:
    """Every later uv call passes --python with an absolute interpreter."""
    env = step.get("env") or {}
    for words in _commands(step["run"]):
        if not _starts_uv(words):
            continue
        assert env.get("UV") == UV_PATH_EXPR, step.get("name")
        if words[:2] == ["env", "-i"]:
            continue
        assert "--no-config" in words, words
        assert "--python" in words, words
        python = words[words.index("--python") + 1]
        assert python.startswith("$"), words
        if python == "$PYTHON":
            assert env.get("PYTHON") == PYTHON_PATH_EXPR, step.get("name")


@pytest.mark.parametrize("step", RUN_STEPS, ids=RUN_STEP_IDS)
def test_run_step_drops_python_settings_first(step: dict[str, Any]) -> None:
    """PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ act even under -I.

    A step that runs uv outside 'env -i', or starts Python, unsets the
    caller's Python settings before it does either.
    """
    commands = _commands(step["run"])
    starts = [
        position
        for position, words in enumerate(commands)
        if (_starts_uv(words) and words[:2] != ["env", "-i"]) or _starts_python(words)
    ]
    if not starts:
        return
    scrub = [
        position
        for position, words in enumerate(commands)
        if words == ["__PYVENV_LAUNCHER__", "unset", "$name"]
    ]
    assert scrub, step.get("name")
    assert scrub[0] < starts[0], step.get("name")


@pytest.mark.parametrize("step", RUN_STEPS, ids=RUN_STEP_IDS)
def test_run_step_python_starts_are_isolated(step: dict[str, Any]) -> None:
    """A Python started by path runs with -I."""
    for words in _commands(step["run"]):
        if _starts_python(words):
            assert words[1] == "-I", words


def test_validator_interpreter_follows_the_runner_os() -> None:
    """'uv venv' puts the interpreter under Scripts on Windows, bin elsewhere.

    The validate step names it once, for both the install and the run.
    """
    validate = [step for step in _steps() if step.get("id") == "validate"]
    assert len(validate) == 1
    step = validate[0]
    assert step["env"]["VALIDATOR_BIN"] == (
        "${{ runner.os == 'Windows' && 'Scripts/python.exe' || 'bin/python' }}"
    )
    commands = _commands(step["run"])
    pythons = [words for words in commands if "--python" in words]
    starts = [words for words in commands if _starts_python(words)]
    assert ["$VALIDATOR_PYTHON", "-I"] == starts[0][:2], starts
    assert "$VALIDATOR_PYTHON" in [
        words[words.index("--python") + 1] for words in pythons
    ], pythons
    assert not any("bin/python" in word for words in commands for word in words)
