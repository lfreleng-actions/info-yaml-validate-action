# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The validation core shared by the action entry script and the CLI.

``validate`` runs the yamllint, schema and repository checks over one
file and returns a ``Result``; ``emit`` reports it; ``run`` does both and
turns a fatal ``InfoYamlError`` into a failing exit code. A front-end
only gathers the inputs, from ``INPUT_*`` variables or the command line,
so the action and the ``info-yaml-validate`` command cannot drift apart.

Two switches separate the front-ends. ``github`` emits the workflow
commands and the job summary that only a GitHub Actions runner reads;
without it the report is plain text for a terminal. ``outputs`` writes
the action's step outputs, which belong to the action alone.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from typing import Any, TextIO

import yaml

from info_yaml.checks import check_repositories, extract_repositories, validate_schema
from info_yaml.lint import lint_yaml, resolve_lint_config
from info_yaml.report import (
    LintFinding,
    Result,
    append_to_env_file,
    escape_data,
    escape_property,
    render_summary,
    sanitise,
    write_failure_outputs,
    write_outputs,
)
from info_yaml.workspace import InfoYamlError, contain

DEFAULT_INFO_PATH = "INFO.yaml"
GITREVIEW_PATH = ".gitreview"

# A built wheel carries the schema inside this package; the action's
# checkout, and an editable install, keep it at the repository root.
_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGED_SCHEMA = _PACKAGE_DIR / "info-schema.yaml"
BUNDLED_SCHEMA = (
    _PACKAGED_SCHEMA
    if _PACKAGED_SCHEMA.is_file()
    else _PACKAGE_DIR.parent / "schema" / "info-schema.yaml"
)

# PyYAML's C loader is markedly faster and is present in most wheels, but
# the pure-Python loader is a correct fallback where libyaml is absent.
LOADER = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader


def _write(stream: TextIO, line: str) -> None:
    """Write one line, escaping what the stream's encoding cannot carry.

    The report's markers are emoji. A runner's stdout is UTF-8, but a
    hook's output may be captured through a stream in a legacy encoding
    such as cp1252 on Windows, where writing them would raise
    ``UnicodeEncodeError`` before the verdict was reported. Such
    characters are written as backslash escapes instead.
    """
    encoding = getattr(stream, "encoding", None) or "utf-8"
    text = f"{line}\n".encode(encoding, "backslashreplace").decode(encoding)
    stream.write(text)


def _out(line: str) -> None:
    """Write one line of the report to stdout, where the runner reads it."""
    _write(sys.stdout, line)


def read_text(path: Path, *, label: str) -> str:
    """Read a UTF-8 file with actionable failure messages."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise InfoYamlError(f"{label} not found at '{path}'") from None
    except OSError as exc:
        raise InfoYamlError(f"{label} at '{path}' could not be read: {exc}") from None
    except UnicodeDecodeError:
        raise InfoYamlError(f"{label} at '{path}' is not valid UTF-8") from None


def parse_yaml(text: str, path: Path, *, label: str) -> Any:
    """Parse a YAML document read from ``path``."""
    try:
        return yaml.load(text, Loader=LOADER)  # nosec B506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise InfoYamlError(f"{label} at '{path}' is not valid YAML: {exc}") from None


def load_yaml(path: Path, *, label: str) -> Any:
    """Read and parse a YAML document, with actionable failure messages."""
    return parse_yaml(read_text(path, label=label), path, label=label)


def resolve_project(explicit: str, workspace: Path) -> str:
    """Determine the project name, preferring the caller's explicit value.

    Falling back to ``.gitreview`` keeps a caller from having to repeat a
    value the repository already carries, which is how the Gerrit-side
    tooling has always identified a project. Failing loudly when neither is
    available is deliberate: guessing the project name would make the
    repository check pass against something the caller never named.
    """
    if explicit.strip():
        return explicit.strip()

    # Contain this path like the caller-supplied ones. Nothing names it in
    # the workflow, but the repository under test owns the file, and a
    # symlink at .gitreview would otherwise let it steer the read
    # anywhere on the runner: both is_file() and read_text() follow links.
    gitreview = contain(GITREVIEW_PATH, workspace, label=".gitreview")
    if not gitreview.is_file():
        raise InfoYamlError(
            "no project was given and no .gitreview file was found. Supply "
            "the 'project' input (--project), or set "
            "'require-repository-match: false' (--no-repository-match) to "
            "skip the repository check."
        )

    # Interpolation off: ConfigParser applies BasicInterpolation by
    # default, which reads '%' as the start of a substitution. A project
    # name containing a literal '%' would raise InterpolationSyntaxError,
    # and a '%(name)s' sequence would expand against other keys. Gerrit
    # project names are literal text and want reading verbatim.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(gitreview.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        raise InfoYamlError(f".gitreview could not be parsed: {exc}") from None

    project = parser.get("gerrit", "project", fallback="").strip()
    if not project:
        raise InfoYamlError(
            ".gitreview contains no 'project' key under a [gerrit] section. "
            "Supply the 'project' input (--project) explicitly instead."
        )
    # Gerrit records the project with a .git suffix; the repositories list
    # in INFO.yaml conventionally omits it.
    return project[: -len(".git")] if project.endswith(".git") else project


def resolve_schema_path(explicit: str, workspace: Path) -> Path:
    """Pick the schema to validate against, containing any caller path.

    The path is taken literally, as ``resolve_info_path`` takes its own.
    """
    if not explicit:
        return BUNDLED_SCHEMA
    return contain(explicit, workspace, label="schema-path")


def resolve_info_path(explicit: str, workspace: Path) -> Path:
    """Resolve the INFO.yaml location, containing it within the workspace.

    The path is taken literally: pre-commit passes file names as git
    records them, and a leading or trailing space is part of the name.
    The action trims its own input before it gets here.
    """
    return contain(explicit or DEFAULT_INFO_PATH, workspace, label="path")


def emit(result: Result, info_path: str, *, github: bool = True) -> int:
    """Write console output, summary, annotations and return an exit code.

    The summary and the annotations are written only when ``github`` is
    set; the plain diagnostic lines are written either way.

    Everything interpolated here passes through ``sanitise`` first. The
    runner parses stdout for workflow commands, so a value carrying a
    newline followed by ``::`` would issue one: a repository could set an
    output, mask a value, or print its own error. That applies to the
    plain diagnostic lines as much as to the annotations, which is why
    the escaping is not confined to the ``::error`` lines below.
    """
    _out("📋 INFO.yaml validation")
    _out(f"  File: {sanitise(info_path)}")
    _out(f"  Project: {sanitise(result.project) or '(not resolved)'}")
    _out(f"  Declared repositories: {len(result.repositories)}")
    for entry in result.repositories:
        _out(f"    - {sanitise(entry)}")

    if github:
        append_to_env_file("GITHUB_STEP_SUMMARY", render_summary(result, info_path))

    location = escape_property(info_path)
    if result.lint_checked:
        _out(f"  YAML lint config: {sanitise(result.lint_config)}")
    for item in result.lint_findings:
        _out(f"  YAML lint: {sanitise(item.describe())}")
        command = "error" if item.level == "error" else "warning"
        message = escape_data(f"yamllint: {item.describe()}")
        if github:
            _out(
                f"::{command} file={location},line={item.line},"
                f"col={item.column}::{message}"
            )
    for finding in result.schema_errors:
        _out(f"  Schema: {sanitise(finding)}")
        if github:
            message = escape_data(f"INFO.yaml schema violation at {finding}")
            _out(f"::error file={location}::{message}")
    for finding in result.repository_errors:
        _out(f"  Repository: {sanitise(finding)}")
        if github:
            _out(f"::error file={location}::{escape_data(finding)}")

    if result.valid:
        skipped = [
            name
            for name, checked in (
                ("YAML lint", result.lint_checked),
                ("repository check", result.repository_checked),
            )
            if not checked
        ]
        if skipped:
            _out(f"Result: valid ✅ ({' and '.join(skipped)} skipped by request)")
        else:
            _out("Result: valid ✅")
        return 0

    total = (
        len(result.lint_errors)
        + len(result.schema_errors)
        + len(result.repository_errors)
    )
    _out(f"Result: INVALID ❌ ({total} finding(s))")
    return 1


def validate(
    workspace: Path,
    *,
    path: str,
    project: str,
    schema: str,
    require_repository_match: bool,
    lint_enabled: bool,
) -> tuple[Result, str]:
    """Run every enabled check over one file.

    Returns the result and the file's path relative to the workspace, for
    reporting. Raises ``InfoYamlError`` for a problem that stops the
    checks from running at all.
    """
    info_path = resolve_info_path(path, workspace)
    schema_path = resolve_schema_path(schema, workspace)
    # Report paths relative to the workspace: an absolute runner path in
    # an annotation stops GitHub associating it with a file in the diff.
    display_path = str(info_path.relative_to(workspace.resolve()))
    # Read the file before linting it, so a missing or non-UTF-8 file
    # is reported here rather than as a yamllint failure. yamllint
    # reads the file again itself, bytes and line endings as written.
    text = read_text(info_path, label="INFO.yaml")
    lint_findings: list[LintFinding] = []
    lint_config = "default"
    if lint_enabled:
        config, lint_config = resolve_lint_config(workspace)
        lint_findings = lint_yaml(workspace, display_path, config)
    # yamllint reports a parse failure as an error-level finding without
    # a rule, and it already locates the problem; parsing again would
    # only replace that finding with a parser exception. The level
    # matters: yamllint's parsable output carries document text, such
    # as a duplicated key, and a key holding a newline can start a line
    # that reads as a rule-less finding. Such a line can only ever be
    # at the level of the rule that printed it, so a warning carrying
    # the syntax suffix is document text, not a verdict on the parse,
    # and must not switch off the schema and repository checks.
    # Decide this before loading the schema or resolving the project:
    # either can fail for its own reasons, and a failure there would
    # discard the lint finding and publish a zero lint count for a
    # file that does not parse.
    parsed = not any(
        item.rule is None and item.level == "error" for item in lint_findings
    )
    loaded_schema = load_yaml(schema_path, label="Schema") if parsed else None
    resolved_project = (
        resolve_project(project, workspace)
        if parsed and require_repository_match
        else ""
    )
    document = parse_yaml(text, info_path, label="INFO.yaml") if parsed else None
    schema_errors = validate_schema(document, loaded_schema) if parsed else []

    entries, structural_errors = extract_repositories(document) if parsed else ([], [])
    if not require_repository_match or not parsed:
        repository_errors: list[str] = []
    elif structural_errors:
        # Extraction already explained precisely what is wrong. Running
        # the entry check as well would append "no repositories are
        # declared" to a document that declares them with the wrong type,
        # contradicting the finding above it.
        repository_errors = structural_errors
    else:
        repository_errors = check_repositories(entries, resolved_project)

    result = Result(
        project=resolved_project,
        repositories=[
            entry if isinstance(entry, str) else repr(entry) for entry in entries
        ],
        schema_errors=schema_errors,
        repository_errors=repository_errors,
        repository_checked=require_repository_match,
        lint_findings=lint_findings,
        lint_checked=lint_enabled,
        lint_config=lint_config,
        parsed=parsed,
    )
    return result, display_path


def fail(exc: InfoYamlError, *, github: bool = True, outputs: bool = True) -> int:
    """Report a fatal error and publish failing outputs."""
    if github:
        _out(f"::error::{escape_data(str(exc))}")
    else:
        _write(sys.stderr, f"error: {sanitise(str(exc))}")
    if outputs:
        # Publish outputs even here, so a caller running the step under
        # continue-on-error reads 'false' rather than an empty string.
        write_failure_outputs()
    return 1


def run(
    workspace: Path,
    *,
    path: str,
    project: str,
    schema: str,
    require_repository_match: bool,
    lint_enabled: bool,
    github: bool = True,
    outputs: bool = True,
) -> int:
    """Validate one file, report the outcome and return an exit code."""
    try:
        result, display_path = validate(
            workspace,
            path=path,
            project=project,
            schema=schema,
            require_repository_match=require_repository_match,
            lint_enabled=lint_enabled,
        )
    except InfoYamlError as exc:
        return fail(exc, github=github, outputs=outputs)
    if outputs:
        write_outputs(result)
    return emit(result, display_path, github=github)
