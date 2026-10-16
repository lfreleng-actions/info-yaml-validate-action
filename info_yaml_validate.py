# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "jsonschema[format-nongpl]>=4.23",
#     "PyYAML>=6",
#     "referencing>=0.36",
#     "yamllint>=1.35",
# ]
# ///
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate an ``INFO.yaml`` file against a schema and its declared repository.

The script performs three checks, in this order:

1. **YAML lint** — yamllint runs over the file first, with the
   workspace's ``.yamllint``, ``.yamllint.yaml`` or ``.yamllint.yml``,
   or yamllint's ``default`` configuration when there is none. Running it
   first means a file that does not parse reports a located syntax error
   rather than a parser exception. Error-level findings fail the run;
   warning-level findings are reported only.
2. **Schema conformance** — the file is validated against a JSON Schema,
   by default the copy of ``info-schema.yaml`` bundled with this action.
3. **Declared repository** — every entry under ``repositories`` must name
   either the project itself or an ancestor of it, so a file cannot claim
   repositories it does not own.

The ancestor rule exists for subproject repositories: a Gerrit project
``integration/distribution`` may carry an ``INFO.yaml`` whose
``repositories`` list names the parent ``integration``.

Schema validation uses JSON Schema Draft 2020-12 with format checking
enabled. The schema itself is checked against the Draft 2020-12 metaschema
before any document is validated, so a defective schema, bundled or
caller-supplied, fails up front with a schema error. ``format: uri`` is
enforced through ``rfc3986-validator``, which the ``format-nongpl`` extra
of ``jsonschema`` installs.

Documents are parsed with PyYAML, which implements YAML 1.1: an unquoted
``2017-02-10`` loads as a date and ``yes``/``no`` load as booleans, so such
values fail a ``type: string`` constraint. Quote them.

The script makes **no network calls**. The schema is read from disk, either
from this action's own checkout or from a path the caller supplies. Nothing
is downloaded at run time, which is the substantive difference from the
workflows this action replaces: they fetched both the schema and the
validator over ``wget`` from a mutable branch and executed the result.

Inputs are read from ``INPUT_*`` environment variables (the GitHub Actions
convention). The yamllint stage lives in ``info_yaml.lint``, the schema
and repository checks in ``info_yaml.checks``, workspace containment in
``info_yaml.workspace`` and reporting in ``info_yaml.report``.
"""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path
from typing import Any

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

# The bundled schema lives beside this script in the action's checkout.
BUNDLED_SCHEMA = Path(__file__).resolve().parent / "schema" / "info-schema.yaml"

# PyYAML's C loader is markedly faster and is present in most wheels, but
# the pure-Python loader is a correct fallback where libyaml is absent.
LOADER = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader


TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _as_bool(value: str, *, default: bool, name: str) -> bool:
    """Parse a GitHub Actions style boolean, rejecting anything else.

    Treating an unrecognised value as false would let a mis-spelling such
    as ``enabled`` quietly switch off a gate the caller asked for, and the
    run would then report success having skipped the check.
    """
    normalised = value.strip().lower()
    if not normalised:
        return default
    if normalised in TRUE_VALUES:
        return True
    if normalised in FALSE_VALUES:
        return False
    raise InfoYamlError(
        f"input '{name}' must be a boolean such as 'true' or 'false'; "
        f"received '{value.strip()}'"
    )


def read_text(path: Path, *, label: str) -> str:
    """Read a UTF-8 file, keeping its line endings, with actionable failures.

    ``newline=''`` stops Python translating CRLF to LF on the way in.
    yamllint's ``new-lines`` rule judges the endings as written, so a
    translated read would hide a CRLF file from the default Unix rule and
    fail a correctly formatted one under a ``type: dos`` configuration.
    """
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
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
            "the 'project' input is empty and no .gitreview file was found. "
            "Supply 'project' explicitly, or set "
            "'require-repository-match: false' to skip the repository check."
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
            "Supply the 'project' input explicitly instead."
        )
    # Gerrit records the project with a .git suffix; the repositories list
    # in INFO.yaml conventionally omits it.
    return project[: -len(".git")] if project.endswith(".git") else project


def resolve_schema_path(explicit: str, workspace: Path) -> Path:
    """Pick the schema to validate against, containing any caller path."""
    if not explicit.strip():
        return BUNDLED_SCHEMA
    return contain(explicit.strip(), workspace, label="schema-path")


def resolve_info_path(explicit: str, workspace: Path) -> Path:
    """Resolve the INFO.yaml location, containing it within the workspace."""
    return contain(explicit.strip() or DEFAULT_INFO_PATH, workspace, label="path")


def emit(result: Result, info_path: str) -> int:
    """Write console output, summary, annotations and return an exit code.

    Everything interpolated here passes through ``sanitise`` first. The
    runner parses stdout for workflow commands, so a value carrying a
    newline followed by ``::`` would issue one: a repository could set an
    output, mask a value, or print its own error. That applies to the
    plain diagnostic lines as much as to the annotations, which is why
    the escaping is not confined to the ``::error`` lines below.
    """
    print("📋 INFO.yaml validation")
    print(f"  File: {sanitise(info_path)}")
    print(f"  Project: {sanitise(result.project) or '(not resolved)'}")
    print(f"  Declared repositories: {len(result.repositories)}")
    for entry in result.repositories:
        print(f"    - {sanitise(entry)}")

    append_to_env_file("GITHUB_STEP_SUMMARY", render_summary(result, info_path))
    write_outputs(result)

    location = escape_property(info_path)
    if result.lint_checked:
        print(f"  YAML lint config: {sanitise(result.lint_config)}")
    for item in result.lint_findings:
        print(f"  YAML lint: {sanitise(item.describe())}")
        command = "error" if item.level == "error" else "warning"
        message = escape_data(f"yamllint: {item.describe()}")
        print(
            f"::{command} file={location},line={item.line},col={item.column}::{message}"
        )
    for finding in result.schema_errors:
        print(f"  Schema: {sanitise(finding)}")
        message = escape_data(f"INFO.yaml schema violation at {finding}")
        print(f"::error file={location}::{message}")
    for finding in result.repository_errors:
        print(f"  Repository: {sanitise(finding)}")
        print(f"::error file={location}::{escape_data(finding)}")

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
            print(f"Result: valid ✅ ({' and '.join(skipped)} skipped by request)")
        else:
            print("Result: valid ✅")
        return 0

    total = (
        len(result.lint_errors)
        + len(result.schema_errors)
        + len(result.repository_errors)
    )
    print(f"Result: INVALID ❌ ({total} finding(s))")
    return 1


def main() -> int:
    """Entry point: read inputs, run the checks, emit results."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    raw_path = os.environ.get("INPUT_PATH", "")
    raw_project = os.environ.get("INPUT_PROJECT", "")
    raw_schema = os.environ.get("INPUT_SCHEMA_PATH", "")

    try:
        require_repository_match = _as_bool(
            os.environ.get("INPUT_REQUIRE_REPOSITORY_MATCH", ""),
            default=True,
            name="require-repository-match",
        )
        lint_enabled = _as_bool(
            os.environ.get("INPUT_LINT_YAML", ""), default=True, name="lint-yaml"
        )
        info_path = resolve_info_path(raw_path, workspace)
        schema_path = resolve_schema_path(raw_schema, workspace)
        # Report paths relative to the workspace: an absolute runner path in
        # an annotation stops GitHub associating it with a file in the diff.
        display_path = str(info_path.relative_to(workspace.resolve()))
        text = read_text(info_path, label="INFO.yaml")
        lint_findings: list[LintFinding] = []
        lint_config = "default"
        if lint_enabled:
            config, lint_config = resolve_lint_config(workspace)
            lint_findings = lint_yaml(text, config, display_path)
        # yamllint reports a parse failure as a finding without a rule, and
        # it already locates the problem; parsing again would only replace
        # that finding with a parser exception. Decide this before loading
        # the schema or resolving the project: either can fail for its own
        # reasons, and a failure there would discard the lint finding and
        # publish a zero lint count for a file that does not parse.
        parsed = not any(item.rule is None for item in lint_findings)
        schema = load_yaml(schema_path, label="Schema") if parsed else None
        project = (
            resolve_project(raw_project, workspace)
            if parsed and require_repository_match
            else ""
        )
        document = parse_yaml(text, info_path, label="INFO.yaml") if parsed else None
        schema_errors = validate_schema(document, schema) if parsed else []
    except InfoYamlError as exc:
        print(f"::error::{escape_data(str(exc))}")
        # Publish outputs even here, so a caller running the step under
        # continue-on-error reads 'false' rather than an empty string.
        write_failure_outputs()
        return 1

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
        repository_errors = check_repositories(entries, project)

    result = Result(
        project=project,
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
    return emit(result, display_path)


if __name__ == "__main__":
    sys.exit(main())
