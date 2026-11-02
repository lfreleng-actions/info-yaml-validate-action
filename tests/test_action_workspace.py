# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The action's uv calls against files planted in the workspace.

The steps run from the caller's workspace, which holds the checkout under
test. A .venv there, or a .python-version naming an interpreter, gives
uv an interpreter to run, and a uv.toml there can point uv at another
package index. Each planted file records that it ran and fails, and the
planted index host does not resolve, so every uv call must succeed with
the real result and leave no record. uv passes over an interpreter that
fails, so only the record shows whether one ran.

Each test runs in two layouts. In 'published', the action's files sit
outside the workspace, as under the runner's _actions directory. In
'inside', they sit in the workspace, as for a caller that checks this
action out there; uv then also reaches the workspace's files from the
script and project directories. The 'workspace-files' job in
testing.yaml runs the action itself, with its directory as the
workspace.

The steps are read from action.yaml, never copied, so these tests run
the commands the action runs. Each step's uv calls have their own test,
so a regression in one cannot hide behind another. The install and
validate steps fetch the locked packages unless uv's cache already
holds them.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - runs uv and the action's own steps
import sys
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACTION_FILES = (
    "action.yaml",
    "info_yaml",
    "info_yaml_validate.py",
    "info_yaml_validate.py.lock",
    "pyproject.toml",
    "schema",
    "uv.lock",
)
# All a step takes from the environment running this suite. An ambient
# UV_NO_CONFIG, UV_PYTHON or VIRTUAL_ENV must not decide the outcome.
PASSED_THROUGH = ("HOME", "PATH", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")
# Modules the validator imports, and one Python runs at start-up when
# the working directory is on its path.
PLANTED_MODULES = (
    "info_yaml/__init__.py",
    "jsonschema.py",
    "sitecustomize.py",
    "yaml/__init__.py",
)
UV = shutil.which("uv")
SETUP_UV_DEFAULT_DIR = "${{ github.workspace }}"
ACTION_PATH_EXPR = "${{ github.action_path }}"

pytestmark = [
    pytest.mark.skipif(UV is None, reason="uv is needed on PATH"),
    pytest.mark.skipif(
        sys.platform == "win32", reason="the planted interpreter is a shell script"
    ),
]


def _action_steps() -> dict[str, dict[str, Any]]:
    with (ROOT / "action.yaml").open(encoding="utf-8") as handle:
        action = yaml.safe_load(handle)
    steps: dict[str, dict[str, Any]] = {}
    for step in action["runs"]["steps"]:
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
            steps["setup-uv"] = step
        elif step.get("name") == "Install yamllint":
            steps["install"] = step
        elif step.get("id") == "validate":
            steps["validate"] = step
    assert set(steps) == {"setup-uv", "install", "validate"}
    return steps


def _outputs(path: Path) -> dict[str, str]:
    """GITHUB_OUTPUT records, in either the 'k=v' or the heredoc form."""
    records: dict[str, str] = {}
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, delimiter = line.split("<<", 1)
            body = []
            for part in lines:
                if part == delimiter:
                    break
                body.append(part)
            records[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            records[name] = value
    return records


class Layout(NamedTuple):
    """A planted workspace and the action directory the steps run from."""

    workspace: Path
    action_path: Path


def _marker(workspace: Path) -> Path:
    return workspace.parent / "planted-ran"


def _plant_interpreter(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/bin/sh\necho \"$0\" >> '{marker}'\nexit 97\n", encoding="utf-8"
    )
    path.chmod(0o755)


def _base_env() -> dict[str, str]:
    return {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}


def _step_env(layout: Layout) -> dict[str, str]:
    workspace = layout.workspace
    lint_env = workspace.parent / "runner-temp" / "info-yaml-validate" / "lint"
    return {
        "ACTION_PATH": str(layout.action_path),
        "GITHUB_ACTIONS": "true",
        "GITHUB_OUTPUT": str(workspace.parent / "github-output"),
        "GITHUB_STEP_SUMMARY": str(workspace.parent / "github-step-summary"),
        "GITHUB_WORKSPACE": str(workspace),
        "INPUT_LINT_YAML": "true",
        "INPUT_PATH": "INFO.yaml",
        "INPUT_PROJECT": "example-project",
        "INPUT_REQUIRE_REPOSITORY_MATCH": "true",
        "INPUT_SCHEMA_PATH": "",
        "UV_PROJECT_ENVIRONMENT": str(lint_env),
        "YAMLLINT_ENV_DIR": str(lint_env),
    }


def _run_step(layout: Layout, step: dict[str, Any], label: str) -> None:
    """Run a step's script as the runner would, from the workspace."""
    env = _step_env(layout)
    unknown = set(step.get("env") or {}) - set(env)
    assert not unknown, f"the {label} step reads {sorted(unknown)}"
    result = subprocess.run(  # nosec B603 B607 - the step's own script
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=layout.workspace,
        env={**_base_env(), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    marker = _marker(layout.workspace)
    assert not marker.exists(), f"the {label} step ran: {marker.read_text()}"
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(params=["published", "inside"])
def layout(request: pytest.FixtureRequest, tmp_path: Path) -> Layout:
    """A workspace holding an INFO.yaml and the planted files, and the action."""
    root = tmp_path / "workspace"
    if request.param == "published":
        action_path = tmp_path / "_actions" / "info-yaml-validate"
    else:
        action_path = root / ".actions" / "info-yaml-validate"
    action_path.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    for name in ACTION_FILES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                action_path / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        else:
            shutil.copy2(source, action_path / name)
    shutil.copyfile(FIXTURES / "valid" / "INFO.yaml", root / "INFO.yaml")
    (tmp_path / "runner-temp").mkdir()
    (tmp_path / "github-output").write_text("", encoding="utf-8")

    marker = _marker(root)
    for module in PLANTED_MODULES:
        target = root / module
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "import sys\n"
            f"with open({str(marker)!r}, 'a') as handle:\n"
            "    handle.write(__file__ + '\\n')\n"
            "sys.exit(97)\n",
            encoding="utf-8",
        )
    interpreter = root / "planted-python"
    _plant_interpreter(interpreter, marker)
    (root / ".python-version").write_text(f"{interpreter}\n", encoding="utf-8")
    (root / "uv.toml").write_text(
        '[[index]]\nurl = "https://pypi.invalid/simple"\ndefault = true\n',
        encoding="utf-8",
    )
    venv = root / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        "version_info = 3.12.0\n",
        encoding="utf-8",
    )
    for name in ("python3", "python"):
        _plant_interpreter(venv / "bin" / name, marker)
    return Layout(root, action_path)


def test_setup_uv_python_probe_skips_workspace_interpreters(layout: Layout) -> None:
    """setup-uv's 'uv python find', in its working directory and step env."""
    assert UV is not None
    step = _action_steps()["setup-uv"]
    env = {str(name): str(value) for name, value in (step.get("env") or {}).items()}
    assert not any("${{" in value for value in env.values())
    directory = str((step.get("with") or {}).get("working-directory", ""))
    directory = directory or SETUP_UV_DEFAULT_DIR
    resolved = {
        SETUP_UV_DEFAULT_DIR: layout.workspace,
        ACTION_PATH_EXPR: layout.action_path,
    }
    assert directory in resolved, f"unexpected working-directory {directory!r}"
    # setup-uv runs uv from the workspace, with --directory naming its
    # working-directory input.
    result = subprocess.run(  # nosec B603 - fixed arguments
        [UV, "python", "find", "--directory", str(resolved[directory])],
        cwd=layout.workspace,
        env={**_base_env(), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    marker = _marker(layout.workspace)
    assert not marker.exists(), f"setup-uv ran: {marker.read_text()}"
    # A uv error would also leave no marker, so the probe must succeed
    # and name a real interpreter, as setup-uv needs it to.
    assert result.returncode == 0, result.stderr
    found = Path(result.stdout.strip())
    assert found.is_file(), result.stdout
    assert not found.is_relative_to(layout.workspace), result.stdout


def test_setup_uv_working_directory_sets_no_uv_settings() -> None:
    """setup-uv reads uv settings and a .venv from the action directory.

    A cache-dir there stops setup-uv exporting UV_CACHE_DIR, which the
    steps' --no-config uv calls would not then use, leaving the cache
    path missing for setup-uv's post step; a required-version there
    would conflict with the pinned version.
    """
    tomllib = pytest.importorskip("tomllib")
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git on PATH")
    assert git is not None
    if not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout of this repository")
    tracked = subprocess.run(  # nosec B603 - fixed arguments
        [git, "ls-files", "--", "uv.toml", ".python-version", ".venv"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not tracked, tracked
    with (ROOT / "pyproject.toml").open("rb") as handle:
        settings = tomllib.load(handle).get("tool", {}).get("uv", {})
    assert not {"cache-dir", "required-version"} & set(settings), settings


def test_install_step_skips_workspace_uv_files(layout: Layout) -> None:
    """'uv sync --project' into the private yamllint environment."""
    _run_step(layout, _action_steps()["install"], "install")
    lint_env = Path(_step_env(layout)["YAMLLINT_ENV_DIR"])
    assert (lint_env / "bin" / "yamllint").is_file()
    # The validator later runs yamllint under this environment's base
    # interpreter, which must not come from the workspace.
    config = (lint_env / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(
        line.split("=", 1)[1].strip()
        for line in config.splitlines()
        if line.split("=", 1)[0].strip() == "home"
    )
    assert not Path(home).resolve().is_relative_to(layout.workspace.resolve()), home


def test_validate_step_skips_workspace_files(layout: Layout) -> None:
    """'uv python find' from the action directory, then 'uv run --script'."""
    steps = _action_steps()
    _run_step(layout, steps["install"], "install")
    _run_step(layout, steps["validate"], "validate")

    records = _outputs(Path(_step_env(layout)["GITHUB_OUTPUT"]))
    assert records["valid"] == "true"
    assert records["project"] == "example-project"
    assert records["repositories"] == "example-project"
    assert records["schema-error-count"] == "0"
    assert records["lint-error-count"] == "0"
