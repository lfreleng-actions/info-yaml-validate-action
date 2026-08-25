# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.23", "PyYAML>=6", "referencing>=0.36"]
# ///
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate an ``INFO.yaml`` file against a schema and its declared repository.

The script performs two independent checks:

1. **Schema conformance** — the file is validated against a JSON Schema,
   by default the copy of ``info-schema.yaml`` bundled with this action.
2. **Declared repository** — every entry under ``repositories`` must name
   either the project itself or an ancestor of it, so a file cannot claim
   repositories it does not own.

The ancestor rule exists for subproject repositories: a Gerrit project
``integration/distribution`` may carry an ``INFO.yaml`` whose
``repositories`` list names the parent ``integration``.

Schema validation deliberately uses Draft 4 with a format checker, matching
``yaml-verify-schema.py`` in ``lfit/releng-global-jjb`` so that files
accepted by the legacy tooling remain accepted here. Later drafts reject
constructs the bundled schema relies on, and would fail files that have
been valid for years.

The script makes **no network calls**. The schema is read from disk, either
from this action's own checkout or from a path the caller supplies. Nothing
is downloaded at run time, which is the substantive difference from the
workflows this action replaces: they fetched both the schema and the
validator over ``wget`` from a mutable branch and executed the result.

Inputs are read from ``INPUT_*`` environment variables (the GitHub Actions
convention). Reporting lives in ``info_yaml.report``.
"""

from __future__ import annotations

import configparser
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema.exceptions import SchemaError, UnknownType, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from info_yaml.report import (
    Result,
    append_to_env_file,
    escape_data,
    escape_property,
    render_summary,
    sanitise,
    write_failure_outputs,
    write_outputs,
)

DEFAULT_INFO_PATH = "INFO.yaml"
GITREVIEW_PATH = ".gitreview"

# The bundled schema lives beside this script in the action's checkout.
BUNDLED_SCHEMA = Path(__file__).resolve().parent / "schema" / "info-schema.yaml"

# PyYAML's C loader is markedly faster and is present in most wheels, but
# the pure-Python loader is a correct fallback where libyaml is absent.
LOADER = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader


TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})


class InfoYamlError(RuntimeError):
    """Raised for unrecoverable, user-actionable configuration problems."""


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


def load_yaml(path: Path, *, label: str) -> Any:
    """Read and parse a YAML document, with actionable failure messages."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise InfoYamlError(f"{label} not found at '{path}'") from None
    except OSError as exc:
        raise InfoYamlError(f"{label} at '{path}' could not be read: {exc}") from None
    except UnicodeDecodeError:
        raise InfoYamlError(f"{label} at '{path}' is not valid UTF-8") from None

    try:
        return yaml.load(text, Loader=LOADER)  # nosec B506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise InfoYamlError(f"{label} at '{path}' is not valid YAML: {exc}") from None


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
    gitreview = _contain(GITREVIEW_PATH, workspace, label=".gitreview")
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


def format_error_path(error: ValidationError) -> str:
    """Render a validation error's location as a readable path.

    ``absolute_path`` mixes mapping keys and sequence indices, so indices
    render in brackets and keys join with dots. An error at the document
    root has an empty path and reports as ``(document root)``.
    """
    parts: list[str] = []
    for element in error.absolute_path:
        if isinstance(element, int):
            parts.append(f"[{element}]")
        elif parts:
            parts.append(f".{element}")
        else:
            parts.append(str(element))
    return "".join(parts) or "(document root)"


def _refusing_registry(attempted: list[str]) -> Registry[Any]:
    """Build a registry that refuses to fetch anything, recording attempts.

    jsonschema 4.26 retrieves unresolved references over the network by
    default, emitting only a DeprecationWarning. A caller-supplied schema
    could therefore reach an arbitrary URL, or a ``file:`` path outside
    the workspace, defeating the containment applied to ``schema-path``
    and the claim that validation performs no network I/O.

    The retriever raises the protocol's own ``NoSuchResource`` rather than
    a local exception, because ``referencing`` wraps whatever it receives
    and the wrapping loses a custom type. Recording the URI instead gives
    the caller a precise message without depending on an exception chain.

    Internal pointers resolve within the document and never reach here.
    """

    def retrieve(uri: str) -> Any:
        attempted.append(uri)
        raise NoSuchResource(ref=uri)

    return Registry(retrieve=retrieve)


def validate_schema(document: Any, schema: Any) -> list[str]:
    """Collect every schema violation, not merely the first.

    Reporting all findings at once matters for a gating check: a caller
    fixing one error per run turns a five-minute correction into five
    review cycles.

    ``Draft4Validator.check_schema`` is deliberately *not* called first.
    The canonical ``info-schema.yaml`` is not itself valid Draft 4 — it
    carries Draft 3 spellings such as ``required: false`` inside a property
    subschema — so checking would reject the bundled schema and every
    legacy-compatible copy of it. Draft 4 ignores those keywords in
    practice, which is why the files have validated for years. See the
    README for the specific defects.

    A genuinely broken schema therefore surfaces as a runtime failure part
    way through validation rather than up front, so those are caught here
    and reported against the schema rather than the document.
    """
    attempted: list[str] = []
    validator = jsonschema.Draft4Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
        registry=_refusing_registry(attempted),
    )
    try:
        errors = sorted(validator.iter_errors(document), key=str)
    except Unresolvable as exc:
        if attempted:
            raise InfoYamlError(
                f"the schema references an external resource "
                f"('{attempted[0]}'); only self-contained schemas are "
                "supported"
            ) from None
        raise InfoYamlError(
            f"the schema contains a reference that cannot be resolved: {exc}"
        ) from None
    except SchemaError as exc:
        raise InfoYamlError(f"the schema itself is not valid: {exc.message}") from None
    except UnknownType as exc:
        raise InfoYamlError(f"the schema names an unknown type: {exc}") from None
    except (AttributeError, TypeError, re.error) as exc:
        # A malformed subschema fails inside the keyword validator rather
        # than cleanly: a bad 'type' raises TypeError/AttributeError, and
        # 'pattern'/'patternProperties' hand their value straight to
        # re.search, so an unparsable regex surfaces as re.error. Left
        # uncaught, any of them would abort before the failure outputs.
        raise InfoYamlError(f"the schema could not be applied: {exc}") from None

    return [f"{format_error_path(error)}: {error.message}" for error in errors]


def extract_repositories(document: Any) -> tuple[list[Any], list[str]]:
    """Return the raw ``repositories`` entries and any structural findings.

    Entries come back untouched, including any that are not strings. The
    bundled schema declares no ``repositories`` subschema at all, so the
    schema check reports nothing about their type; discarding a malformed
    entry here would leave nobody reporting it, and a document such as
    ``repositories: ['example', 42]`` would pass both checks.
    """
    if not isinstance(document, dict):
        return [], ["INFO.yaml does not contain a mapping at the top level"]
    if "repositories" not in document:
        return [], ["INFO.yaml declares no 'repositories' key"]
    raw = document["repositories"]
    if not isinstance(raw, list):
        return [], [
            f"'repositories' must be a list, found {type(raw).__name__}"  # noqa: COM812
        ]
    return list(raw), []  # pyright: ignore[reportUnknownArgumentType]


def check_repositories(entries: Sequence[Any], project: str) -> list[str]:
    """Verify every declared repository is the project or an ancestor of it.

    An ancestor is permitted so that a subproject repository such as
    ``integration/distribution`` may carry an ``INFO.yaml`` naming the
    parent project ``integration``.
    """
    if not entries:
        return [
            "no repositories are declared; INFO.yaml must list the "
            f"repository it describes (expected '{project}')"
        ]

    findings: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            findings.append(
                f"declared repository entry {entry!r} is not a string; "
                "every entry must name a repository"
            )
            continue
        if entry == project or project.startswith(f"{entry}/"):
            continue
        findings.append(
            f"declared repository '{entry}' is neither the project "
            f"'{project}' nor an ancestor of it"
        )
    return findings


def _contain(relative: str, workspace: Path, *, label: str) -> Path:
    """Resolve a caller path and confirm it stays inside the workspace.

    A path escaping the checkout would let a workflow validate a file, or
    against a schema, that nobody reviewed.
    """
    candidate = (workspace / relative).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise InfoYamlError(
            f"{label} '{relative}' resolves outside the workspace; supply a "
            "path inside the repository"
        ) from None
    return candidate


def resolve_schema_path(explicit: str, workspace: Path) -> Path:
    """Pick the schema to validate against, containing any caller path."""
    if not explicit.strip():
        return BUNDLED_SCHEMA
    return _contain(explicit.strip(), workspace, label="schema-path")


def resolve_info_path(explicit: str, workspace: Path) -> Path:
    """Resolve the INFO.yaml location, containing it within the workspace."""
    return _contain(explicit.strip() or DEFAULT_INFO_PATH, workspace, label="path")


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
    for finding in result.schema_errors:
        print(f"  Schema: {sanitise(finding)}")
        message = escape_data(f"INFO.yaml schema violation at {finding}")
        print(f"::error file={location}::{message}")
    for finding in result.repository_errors:
        print(f"  Repository: {sanitise(finding)}")
        print(f"::error file={location}::{escape_data(finding)}")

    if result.valid:
        if not result.repository_checked:
            print("Result: valid ✅ (repository check skipped by request)")
        else:
            print("Result: valid ✅")
        return 0

    total = len(result.schema_errors) + len(result.repository_errors)
    print(f"Result: INVALID ❌ ({total} finding(s))")
    return 1


def main() -> int:
    """Entry point: read inputs, run both checks, emit results."""
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
        info_path = resolve_info_path(raw_path, workspace)
        schema_path = resolve_schema_path(raw_schema, workspace)
        document = load_yaml(info_path, label="INFO.yaml")
        schema = load_yaml(schema_path, label="Schema")
        project = (
            resolve_project(raw_project, workspace) if require_repository_match else ""
        )
        schema_errors = validate_schema(document, schema)
    except InfoYamlError as exc:
        print(f"::error::{escape_data(str(exc))}")
        # Publish outputs even here, so a caller running the step under
        # continue-on-error reads 'false' rather than an empty string.
        write_failure_outputs()
        return 1

    entries, structural_errors = extract_repositories(document)
    if not require_repository_match:
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
    )
    # Report paths relative to the workspace: an absolute runner path in an
    # annotation stops GitHub associating it with a file in the diff.
    display_path = str(info_path.relative_to(workspace.resolve()))
    return emit(result, display_path)


if __name__ == "__main__":
    sys.exit(main())
