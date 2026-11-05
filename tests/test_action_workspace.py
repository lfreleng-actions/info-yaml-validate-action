# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The action's uv calls against files and settings planted by the caller.

The steps run from the caller's workspace, which holds the checkout under
test, and inherit the caller job's environment. A .venv there, or a
.python-version naming an interpreter, gives uv an interpreter to run,
and a uv.toml there can point uv at another package index. A PATH,
UV_PYTHON, VIRTUAL_ENV or PYTHONPATH exported by an earlier step can do
the same. Each planted file records that it ran and fails, and the
planted index host does not resolve, so every uv call must succeed with
the real result and leave no record. uv passes over an interpreter that
fails, so only the record shows whether one ran.

Each test also runs under two sets of caller settings, as the
'caller-env' job in testing.yaml does: 'request' names an interpreter
outright, 'active-environment' points at the workspace .venv as an
activated environment would.

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
holds them, and the session installs the action's Python once.
"""

from __future__ import annotations

import os
import platform
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
PASSED_THROUGH = ("HOME", "PATH", "TMPDIR", "UV_CACHE_DIR")
# Modules the validator imports, standard library modules, and the ones
# Python runs at start-up when the working directory is on its path.
PLANTED_MODULES = (
    "base64.py",
    "info_yaml/__init__.py",
    "json.py",
    "jsonschema.py",
    "sitecustomize.py",
    "urllib/__init__.py",
    "usercustomize.py",
    "yaml/__init__.py",
)
# Interpreters and a uv a PATH entry for the workspace .venv offers
PLANTED_EXECUTABLES = ("python", "python3", "python3.13", "uv")
CALLER_LEGS = ("request", "active-environment")
UV = shutil.which("uv")
BASH = shutil.which("bash")
SETUP_UV_DEFAULT_DIR = "${{ github.workspace }}"
ACTION_PATH_EXPR = "${{ github.action_path }}"
RUNNER_TEMP_EXPR = "${{ runner.temp }}"
# What 'runner.os' gives on this host
RUNNER_OS_NAME = "macOS" if sys.platform == "darwin" else "Linux"

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
        elif step.get("id") == "python":
            steps["python"] = step
        elif step.get("name") == "Install yamllint":
            steps["install"] = step
        elif step.get("id") == "validate":
            steps["validate"] = step
    assert set(steps) == {"setup-uv", "python", "install", "validate"}
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
    """A planted workspace, the action directory and the caller's settings."""

    workspace: Path
    action_path: Path
    caller_env: dict[str, str]


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


def _caller_env(workspace: Path, leg: str) -> dict[str, str]:
    """Settings an earlier step of the caller's job exports to this action."""
    venv = workspace / ".venv"
    planted = str(venv / "bin" / "python3")
    machine = platform.machine().lower()
    env = {
        "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"pkg{os.pathsep}.",
        "PYTHONHOME": str(venv),
        "PYTHONUSERBASE": str(venv),
        "PYTHONSTARTUP": str(workspace / "sitecustomize.py"),
        "PYTHONEXECUTABLE": planted,
        "__PYVENV_LAUNCHER__": planted,
        "UV_PYTHON_ARCH": {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine),
        "UV_PYTHON_INSTALL_MIRROR": "https://127.0.0.1:9",
        "XDG_BIN_HOME": str(workspace / "xdg-bin"),
    }
    if leg == "request":
        env |= {
            "UV_PYTHON": planted,
            "UV_PYTHON_SEARCH_PATH": str(venv / "bin"),
            "UV_PYTHON_INSTALL_DIR": str(workspace / "uv-python"),
            "UV_PYTHON_PREFERENCE": "only-system",
        }
    else:
        env |= {
            "VIRTUAL_ENV": str(venv),
            "CONDA_PREFIX": str(venv),
            "CONDA_DEFAULT_ENV": "base",
            "UV_INTERNAL__PARENT_INTERPRETER": planted,
            "UV_INTERNAL__TEST_PYTHON_MANAGED": "1",
            "UV_CACHE_DIR": str(workspace / ".uv-cache"),
        }
    return env


def _step_env(layout: Layout, python: TrustedPython) -> dict[str, str]:
    """Values for the expressions in the steps' env blocks, and the runner's."""
    workspace = layout.workspace
    runner_temp = workspace.parent / "runner-temp"
    lint_env = runner_temp / "info-yaml-validate" / "lint"
    return {
        "ACTION_PATH": str(layout.action_path),
        "CACHE_DIR": str(python.cache_dir),
        "GITHUB_ACTIONS": "true",
        "GITHUB_OUTPUT": str(workspace.parent / "github-output"),
        "GITHUB_STEP_SUMMARY": str(workspace.parent / "github-step-summary"),
        "GITHUB_WORKSPACE": str(workspace),
        "INPUT_LINT_YAML": "true",
        "INPUT_PATH": "INFO.yaml",
        "INPUT_PROJECT": "example-project",
        "INPUT_REQUIRE_REPOSITORY_MATCH": "true",
        "INPUT_SCHEMA_PATH": "",
        "INSTALL_DIR": str(python.install_dir),
        "PYTHON": str(python.path),
        "RUNNER_OS_NAME": RUNNER_OS_NAME,
        "RUNNER_TEMP": str(runner_temp),
        "UV": str(UV),
        "UV_PROJECT_ENVIRONMENT": str(lint_env),
        "VALIDATOR_BIN": (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        ),
        "VALIDATOR_ENV_DIR": str(runner_temp / "info-yaml-validate" / "validator"),
        "YAMLLINT_ENV_DIR": str(lint_env),
    }


def _bash_step(
    step: dict[str, Any],
    env: dict[str, str],
    cwd: Path,
    action_path: Path,
) -> subprocess.CompletedProcess[str]:
    """Run a step's script as the runner would, from its working directory."""
    assert BASH is not None
    directory = str(step.get("working-directory", ""))
    assert directory in ("", ACTION_PATH_EXPR), directory
    return subprocess.run(  # nosec B603 - the step's own script
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=action_path if directory else cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolved_step_env(
    step: dict[str, Any], known: dict[str, str], label: str
) -> dict[str, str]:
    """The step's own env block: literals as written, expressions from known."""
    env: dict[str, str] = {}
    for name, value in (step.get("env") or {}).items():
        if "${{" not in str(value):
            env[name] = str(value)
        else:
            assert name in known, f"the {label} step reads {name}"
            env[name] = known[name]
    return env


def _run_step(
    layout: Layout, python: TrustedPython, step: dict[str, Any], label: str
) -> None:
    """Run a step under the caller's settings, as the runner would."""
    known = _step_env(layout, python)
    runtime = {
        name: known[name]
        for name in (
            "GITHUB_ACTIONS",
            "GITHUB_OUTPUT",
            "GITHUB_STEP_SUMMARY",
            "GITHUB_WORKSPACE",
            "RUNNER_TEMP",
        )
    }
    env = {
        **_base_env(),
        **layout.caller_env,
        **runtime,
        **_resolved_step_env(step, known, label),
    }
    result = _bash_step(step, env, layout.workspace, layout.action_path)
    marker = _marker(layout.workspace)
    assert not marker.exists(), f"the {label} step ran: {marker.read_text()}"
    assert result.returncode == 0, result.stdout + result.stderr


class TrustedPython(NamedTuple):
    """The interpreter the action's Python step installed for the session."""

    path: Path
    install_dir: Path
    cache_dir: Path


@pytest.fixture(scope="session")
def trusted_python(tmp_path_factory: pytest.TempPathFactory) -> TrustedPython:
    """Run the action's Python step once, in a clean environment."""
    if UV is None or sys.platform == "win32":
        pytest.skip("needs uv and a POSIX shell")
    root = tmp_path_factory.mktemp("trusted-python")
    output = root / "github-output"
    output.write_text("", encoding="utf-8")
    install_dir = root / "python"
    cache_dir = Path(os.environ.get("UV_CACHE_DIR") or root / "cache")
    step = _action_steps()["python"]
    known = {
        "UV": str(UV),
        "INSTALL_DIR": str(install_dir),
        "CACHE_DIR": str(cache_dir),
        "RUNNER_OS_NAME": RUNNER_OS_NAME,
    }
    env = {
        **_base_env(),
        "GITHUB_OUTPUT": str(output),
        **_resolved_step_env(step, known, "python"),
    }
    result = _bash_step(step, env, root, ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    path = Path(_outputs(output)["path"])
    assert path.is_file()
    assert path.is_relative_to(install_dir)
    return TrustedPython(path, install_dir, cache_dir)


@pytest.fixture(params=["published", "inside"])
def action_layout(request: pytest.FixtureRequest) -> str:
    """Where the action's files sit relative to the workspace."""
    return str(request.param)


@pytest.fixture(params=CALLER_LEGS)
def caller_leg(request: pytest.FixtureRequest) -> str:
    """Which interpreter-selecting settings the caller exports."""
    return str(request.param)


@pytest.fixture
def layout(action_layout: str, caller_leg: str, tmp_path: Path) -> Layout:
    """A workspace holding an INFO.yaml and the planted files, and the action."""
    root = tmp_path / "workspace"
    if action_layout == "published":
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
    (venv / "conda-meta").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        "version_info = 3.13.0\n",
        encoding="utf-8",
    )
    for name in PLANTED_EXECUTABLES:
        _plant_interpreter(venv / "bin" / name, marker)
    for name in ("env", "bash"):
        _plant_interpreter(root / "xdg-bin" / name, marker)
    return Layout(root, action_path, _caller_env(root, caller_leg))


def test_planted_modules_run_without_isolation(layout: Layout) -> None:
    """The control: a Python started without -I imports the planted modules."""
    marker = _marker(layout.workspace)
    env = {**_base_env(), **layout.caller_env}
    # PYTHONHOME would stop it starting at all, before any import
    env.pop("PYTHONHOME")
    env.pop("PYTHONEXECUTABLE")
    env.pop("__PYVENV_LAUNCHER__")
    subprocess.run(  # nosec B603 - fixed arguments
        [sys.executable, "-c", "import json"],
        cwd=layout.workspace,
        env=env,
        capture_output=True,
        check=False,
    )
    assert marker.exists()


def test_python_step_takes_nothing_from_the_caller(
    layout: Layout, trusted_python: TrustedPython
) -> None:
    """'uv python install' and 'find' under 'env -i' and an allow-list."""
    _run_step(layout, trusted_python, _action_steps()["python"], "python")
    output = Path(_step_env(layout, trusted_python)["GITHUB_OUTPUT"])
    assert Path(_outputs(output)["path"]) == trusted_python.path


@pytest.mark.parametrize(
    ("os_name", "install_dir", "found", "accepted"),
    [
        pytest.param(
            "Windows",
            "C:\\t/info-yaml-validate/python",
            "C:\\t\\info-yaml-validate\\python\\cpython-3.13\\python.exe",
            True,
            id="windows-inside",
        ),
        pytest.param(
            "Windows",
            "C:\\t/info-yaml-validate/python",
            "C:\\t\\info-yaml-validate\\python-other\\python.exe",
            False,
            id="windows-outside",
        ),
        pytest.param(
            "Linux",
            "/t/info-yaml-validate/python",
            "/t/info-yaml-validate/python/cpython-3.13/bin/python3.13",
            True,
            id="posix-inside",
        ),
        pytest.param(
            "Linux",
            "/t/info-yaml-validate",
            "/t/info-yaml-validate\\python/bin/python3.13",
            False,
            id="posix-backslash-is-no-separator",
        ),
    ],
)
def test_python_step_checks_the_install_directory(
    tmp_path: Path, os_name: str, install_dir: str, found: str, accepted: bool
) -> None:
    """The found interpreter must sit in INSTALL_DIR, in the runner's syntax.

    On Windows uv prints backslashes while INSTALL_DIR mixes them with
    the step's forward slashes; elsewhere a backslash is no separator.
    A stand-in uv prints the path, so this runs on any POSIX host.
    """
    assert BASH is not None
    uv = tmp_path / "uv"
    quoted = found.replace("'", "'\\''")
    uv.write_text(
        f"#!/bin/sh\nif [ \"$2\" = find ]; then printf '%s\\n' '{quoted}'; fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    output = tmp_path / "github-output"
    output.write_text("", encoding="utf-8")
    known = {
        "UV": str(uv),
        "INSTALL_DIR": install_dir,
        "CACHE_DIR": str(tmp_path / "cache"),
        "RUNNER_OS_NAME": os_name,
    }
    step = _action_steps()["python"]
    env = {
        **_base_env(),
        "GITHUB_OUTPUT": str(output),
        **_resolved_step_env(step, known, "python"),
    }
    result = _bash_step(step, env, tmp_path, ROOT)
    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert _outputs(output)["path"] == found
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "outside its install directory" in result.stdout
        assert "path" not in _outputs(output)


def test_setup_uv_python_probe_runs_no_interpreter(layout: Layout) -> None:
    """setup-uv's 'uv python find', in its working directory and step env.

    The step sets an invalid UV_PYTHON_PREFERENCE, so uv refuses its
    arguments before any lookup and runs nothing, whatever the caller
    exported; setup-uv then reports the Python version as unknown.
    """
    assert UV is not None
    step = _action_steps()["setup-uv"]
    runner_temp = layout.workspace.parent / "runner-temp"
    env = {
        str(name): str(value).replace(RUNNER_TEMP_EXPR, str(runner_temp))
        for name, value in (step.get("env") or {}).items()
    }
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
        env={**_base_env(), **layout.caller_env, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    marker = _marker(layout.workspace)
    assert not marker.exists(), f"setup-uv ran: {marker.read_text()}"
    assert result.returncode != 0, result.stdout
    assert "lookup-disabled" in result.stderr, result.stderr


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


def _venv_home(env_dir: Path) -> Path:
    config = (env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    return Path(
        next(
            line.split("=", 1)[1].strip()
            for line in config.splitlines()
            if line.split("=", 1)[0].strip() == "home"
        )
    )


def test_install_step_skips_workspace_uv_files(
    layout: Layout, trusted_python: TrustedPython
) -> None:
    """'uv sync --project' into the private yamllint environment."""
    _run_step(layout, trusted_python, _action_steps()["install"], "install")
    lint_env = Path(_step_env(layout, trusted_python)["YAMLLINT_ENV_DIR"])
    assert (lint_env / "bin" / "yamllint").is_file()
    # The validator later runs yamllint under this environment's base
    # interpreter, which must be the action's own.
    assert _venv_home(lint_env) == trusted_python.path.parent
    assert not (layout.workspace / ".venv" / "lib").exists()


def test_validate_step_skips_workspace_files(
    layout: Layout, trusted_python: TrustedPython
) -> None:
    """'uv export', 'uv venv' and 'uv pip install', then 'python -I'."""
    steps = _action_steps()
    _run_step(layout, trusted_python, steps["install"], "install")
    _run_step(layout, trusted_python, steps["validate"], "validate")
    known = _step_env(layout, trusted_python)
    assert _venv_home(Path(known["VALIDATOR_ENV_DIR"])) == trusted_python.path.parent
    assert not (layout.workspace / ".venv" / "lib").exists()

    records = _outputs(Path(known["GITHUB_OUTPUT"]))
    assert records["valid"] == "true"
    assert records["project"] == "example-project"
    assert records["repositories"] == "example-project"
    assert records["schema-error-count"] == "0"
    assert records["lint-error-count"] == "0"
