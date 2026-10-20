# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the info-yaml-validate command line."""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from info_yaml import cli, report
from info_yaml import validate as core

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOOKS = Path(__file__).resolve().parent.parent / ".pre-commit-hooks.yaml"
INVALID_FIXTURES = (
    "invalid-schema",
    "invalid-uri",
    "lint-failure",
    "missing-issue-tracking-keys",
    "wrong-repository",
)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working tree, made the current directory.

    The GitHub Actions variables are cleared, since the suite itself may
    run under Actions, and the files they would name are created beside
    the tree so a test can see whether anything was written to them.
    """
    path = tmp_path / "tree"
    path.mkdir()
    monkeypatch.chdir(path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.delenv("YAMLLINT_ENV_DIR", raising=False)
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        target = tmp_path / name.lower()
        target.write_text("")
        monkeypatch.setenv(name, str(target))
    return path


def _place(tree: Path, fixture: str, target: str = "INFO.yaml") -> Path:
    destination = tree / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture / "INFO.yaml", destination)
    return destination


def _written(tree: Path, name: str) -> str:
    return (tree.parent / name.lower()).read_text()


def test_defaults_to_info_yaml_in_the_current_directory(tree: Path) -> None:
    _place(tree, "valid")
    assert cli.main(["--project", "example-project"]) == 0


@pytest.mark.parametrize("fixture", INVALID_FIXTURES)
def test_invalid_fixtures_exit_non_zero(tree: Path, fixture: str) -> None:
    _place(tree, fixture)
    assert cli.main(["--project", "example-project", "INFO.yaml"]) == 1


def test_any_failing_path_fails_the_run(tree: Path) -> None:
    _place(tree, "valid", "a/INFO.yaml")
    _place(tree, "wrong-repository", "b/INFO.yaml")
    args = ["--project", "example-project"]
    assert cli.main([*args, "a/INFO.yaml"]) == 0
    assert cli.main([*args, "a/INFO.yaml", "b/INFO.yaml"]) == 1
    assert cli.main([*args, "b/INFO.yaml", "a/INFO.yaml"]) == 1


def test_every_path_is_reported(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure in the first file must not hide the second."""
    _place(tree, "wrong-repository", "a/INFO.yaml")
    _place(tree, "invalid-schema", "b/INFO.yaml")
    cli.main(["--project", "example-project", "a/INFO.yaml", "b/INFO.yaml"])
    out = capsys.readouterr().out
    assert "File: a/INFO.yaml" in out
    assert "File: b/INFO.yaml" in out


def test_project_is_read_from_gitreview(tree: Path) -> None:
    _place(tree, "valid")
    (tree / ".gitreview").write_text("[gerrit]\nproject=example-project.git\n")
    assert cli.main([]) == 0
    (tree / ".gitreview").write_text("[gerrit]\nproject=other.git\n")
    assert cli.main([]) == 1


def test_no_repository_match_skips_the_check(tree: Path) -> None:
    _place(tree, "wrong-repository")
    assert cli.main(["--no-repository-match"]) == 0


def test_no_lint_yaml_skips_the_lint_stage(tree: Path) -> None:
    _place(tree, "lint-failure")
    assert cli.main(["--project", "example-project", "--no-lint-yaml"]) == 0


def test_schema_option_selects_another_schema(tree: Path) -> None:
    _place(tree, "valid")
    (tree / "strict.yaml").write_text(
        yaml.safe_dump({"type": "object", "required": ["no-such-key"]})
    )
    args = ["--project", "example-project", "--schema", "strict.yaml"]
    assert cli.main(args) == 1


def test_paths_outside_the_tree_are_refused(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _place(tree.parent, "valid")
    assert cli.main(["--project", "example-project", "../INFO.yaml"]) == 1
    assert "outside the workspace" in capsys.readouterr().err


def test_github_workspace_does_not_move_the_tree(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pre-commit passes paths relative to the current directory."""
    _place(tree, "valid")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tree.parent))
    assert cli.main(["--project", "example-project"]) == 0


def test_paths_are_taken_literally(tree: Path) -> None:
    """pre-commit passes file names verbatim; a leading space is part of one.

    Trimming ' leading/INFO.yaml' would validate 'leading/INFO.yaml'
    instead, and pass without checking the staged file.
    """
    _place(tree, "valid", "leading/INFO.yaml")
    _place(tree, "wrong-repository", " leading/INFO.yaml")
    args = ["--project", "example-project"]
    assert cli.main([*args, "leading/INFO.yaml"]) == 0
    assert cli.main([*args, " leading/INFO.yaml"]) == 1


def _hook_args() -> list[str]:
    (hook,) = yaml.safe_load(HOOKS.read_text())
    return list(hook["args"])


@pytest.mark.parametrize(
    "hook_args",
    [
        pytest.param(_hook_args(), id="default"),
        pytest.param(["--project", "example-project", "--"], id="custom"),
    ],
)
def test_option_shaped_file_names_are_validated(
    tree: Path, capsys: pytest.CaptureFixture[str], hook_args: list[str]
) -> None:
    """pre-commit appends file names after the hook's args.

    Without a closing '--', '--project=example-project/INFO.yaml' would
    set the project, and the valid root INFO.yaml would be checked in
    place of the staged file.
    """
    staged = "--project=example-project/INFO.yaml"
    _place(tree, "valid")
    _place(tree, "wrong-repository", staged)
    (tree / ".gitreview").write_text("[gerrit]\nproject=example-project.git\n")
    assert cli.main([*hook_args, staged]) == 1
    assert f"File: {staged}" in capsys.readouterr().out


def _legacy_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")


def _read(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode("cp1252")


@pytest.mark.parametrize(
    ("fixture", "expected", "marker"),
    [("valid", 0, "\\u2705"), ("wrong-repository", 1, "\\u274c")],
)
def test_non_utf8_stdout_is_escaped(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    expected: int,
    marker: str,
) -> None:
    """A hook's output captured in cp1252 cannot carry the emoji markers."""
    _place(tree, fixture)
    stdout = _legacy_stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    assert cli.main(["--project", "example-project"]) == expected
    out = _read(stdout)
    assert "\\U0001f4cb INFO.yaml validation" in out
    assert marker in out


def test_non_utf8_stderr_is_escaped(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stderr = _legacy_stream()
    monkeypatch.setattr(sys, "stderr", stderr)
    assert cli.main(["--project", "example-project", "\u2603/INFO.yaml"]) == 1
    assert _read(stderr).startswith("error: INFO.yaml not found at '")
    assert "\\u2603" in _read(stderr)


def test_unknown_option_is_a_usage_error(tree: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--no-such-option"])
    assert excinfo.value.code == 2


def test_terminal_output_is_plain_text(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _place(tree, "wrong-repository")
    assert cli.main(["--project", "example-project"]) == 1
    out = capsys.readouterr().out
    assert "Repository: declared repository" in out
    assert not [line for line in out.splitlines() if line.startswith("::")]
    assert _written(tree, "GITHUB_STEP_SUMMARY") == ""
    assert _written(tree, "GITHUB_OUTPUT") == ""


def test_terminal_fatal_error_goes_to_stderr(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--project", "example-project", "missing/INFO.yaml"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: INFO.yaml not found")
    assert "::" not in captured.out
    assert _written(tree, "GITHUB_OUTPUT") == ""


def test_actions_output_adds_annotations_and_summary(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _place(tree, "wrong-repository")
    assert cli.main(["--project", "example-project"]) == 1
    out = capsys.readouterr().out
    assert "::error file=INFO.yaml::declared repository" in out
    assert "INFO.yaml Validation" in _written(tree, "GITHUB_STEP_SUMMARY")
    # Step outputs are the action's contract, not the command's.
    assert _written(tree, "GITHUB_OUTPUT") == ""


def test_actions_fatal_error_is_an_annotation(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cli.main(["--project", "example-project", "missing/INFO.yaml"]) == 1
    assert capsys.readouterr().out.startswith("::error::INFO.yaml not found")
    assert _written(tree, "GITHUB_OUTPUT") == ""


def test_emit_without_github_writes_no_workflow_commands(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = report.Result(
        project="example",
        schema_errors=["a finding"],
        repository_errors=["another"],
    )
    assert core.emit(result, "INFO.yaml", github=False) == 1
    assert "::" not in capsys.readouterr().out
    assert _written(tree, "GITHUB_STEP_SUMMARY") == ""
