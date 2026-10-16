# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The schema conformance and declared-repository checks."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError, UnknownType, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from info_yaml.workspace import InfoYamlError


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

    The schema is checked against the Draft 2020-12 metaschema first, so
    a defective schema fails with an error naming the defect rather than
    as a misleading document finding, or not at all: a keyword the draft
    does not recognise in that position is otherwise ignored.
    """
    validator_class = jsonschema.Draft202012Validator
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise InfoYamlError(
            f"the schema itself is not valid at {exc.json_path}: {exc.message}"
        ) from None

    attempted: list[str] = []
    validator = validator_class(
        schema,
        format_checker=validator_class.FORMAT_CHECKER,
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
    except (UnknownType, AttributeError, TypeError, re.error) as exc:
        # A subschema under a keyword the metaschema does not know passes
        # check_schema unread, so a $ref can still lead validation into a
        # bad 'type' or an unparsable 'pattern'. Caught for the reason the
        # metaschema check exists: escaping would skip the failure outputs.
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
