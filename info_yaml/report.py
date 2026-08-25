# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reporting layer: results, escaping, step outputs and the job summary.

Kept apart from the validation logic so that the checks stay readable as
checks. Everything here is concerned with getting a result safely across
the boundary into GitHub Actions, where values land in markdown tables,
workflow commands and ``GITHUB_OUTPUT`` records — three formats with
three different escaping rules, none of which the validator should have
to think about.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field

# Control characters that would corrupt a job summary or an output record.
# Line breaks get their own handling; the rest simply go.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Result:
    """Outcome of a validation run."""

    project: str
    repositories: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    repository_errors: list[str] = field(default_factory=list)
    repository_checked: bool = True

    @property
    def valid(self) -> bool:
        """True when neither check produced a finding."""
        return not self.schema_errors and not self.repository_errors


def append_to_env_file(env_var: str, content: str) -> None:
    """Append content to a file named by an environment variable, if set."""
    path = os.environ.get(env_var)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(content)


def escape_data(value: str) -> str:
    """Escape a GitHub Actions workflow-command message payload."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """Escape a GitHub Actions workflow-command property value."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def sanitise(value: str) -> str:
    """Flatten a value to a single line and drop control characters.

    Values reaching the job summary come from the repository under test.
    A newline inside one would end the markdown table row and let the
    remainder render as a heading, a list item or a fresh table, so the
    summary would report something the validator never found.
    """
    collapsed = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _CONTROL.sub("", collapsed)


def escape_table_cell(value: str) -> str:
    r"""Escape a value for a GFM table cell.

    Backslashes go first: escaping the pipe alone leaves a trailing
    backslash in the value free to consume the escape that follows it, so
    ``a\`` plus a pipe would emit ``a\\|`` and split the cell after all.
    GFM treats ``\|`` as an escaped pipe *even inside code spans*, because
    the table parser splits cells before inline parsing runs.
    """
    return sanitise(value).replace("\\", "\\\\").replace("|", "\\|")


def code_span(value: str) -> str:
    """Wrap a value in a CommonMark code span safe for embedded backticks.

    The delimiter uses one more backtick than the longest run in the value,
    with space padding when the value starts or ends with a backtick, so an
    arbitrary value cannot terminate the span early.
    """
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def _multiline(key: str, value: str, delimiter: str) -> list[str]:
    """Render one heredoc-delimited ``GITHUB_OUTPUT`` record."""
    return [f"{key}<<{delimiter}\n", (value + "\n") if value else "", f"{delimiter}\n"]


def write_outputs(result: Result) -> None:
    """Publish step outputs via ``GITHUB_OUTPUT`` (heredoc-safe).

    Every value derived from the repository uses the heredoc form, not
    only the list-shaped one. ``project`` can arrive from ``.gitreview``,
    whose INI syntax permits a multi-line value, and a newline in a
    single-line record would let it append further keys — ``valid`` among
    them, inverting the result a caller reads.

    The delimiter is randomised per run so no value can match it.
    """
    delimiter = f"ghadelim_info_yaml_{secrets.token_hex(16)}"
    lines = [
        f"valid={str(result.valid).lower()}\n",
        f"schema-error-count={len(result.schema_errors)}\n",
        *_multiline("project", result.project, delimiter),
        *_multiline("repositories", "\n".join(result.repositories), delimiter),
    ]
    append_to_env_file("GITHUB_OUTPUT", "".join(lines))


def write_failure_outputs() -> None:
    """Publish outputs for a run that failed before it could evaluate.

    The documented contract says ``valid`` always carries a value. A
    caller using ``continue-on-error`` to branch on the result would
    otherwise read an empty string for a missing or malformed file, which
    is neither ``true`` nor ``false``.
    """
    delimiter = f"ghadelim_info_yaml_{secrets.token_hex(16)}"
    lines = [
        "valid=false\n",
        "schema-error-count=0\n",
        *_multiline("project", "", delimiter),
        *_multiline("repositories", "", delimiter),
    ]
    append_to_env_file("GITHUB_OUTPUT", "".join(lines))


def _cell(value: str) -> str:
    """Render a value as a table-safe code span.

    Findings use this too, not merely table cells. A finding quotes
    repository-controlled names, so rendering it as bare GFM would let
    ``![text](url)`` become an image and ``[text](url)`` a link in the job
    summary. A code span keeps it as the text it is.
    """
    return code_span(escape_table_cell(value))


def render_summary(result: Result, info_path: str) -> str:
    """Build a human-readable markdown summary block."""
    status = "✅ Valid" if result.valid else "❌ Invalid"
    repositories_cell = (
        "<br>".join(_cell(entry) for entry in result.repositories) or "_none declared_"
    )
    repository_check = (
        f"{len(result.repository_errors)} finding(s)"
        if result.repository_checked
        else "_skipped_"
    )
    rows = [
        "## 📋 INFO.yaml Validation",
        "",
        f"**Result:** {status}",
        "",
        "| Property | Value |",
        "| --- | --- |",
        f"| File | {_cell(info_path)} |",
        f"| Project | {_cell(result.project) if result.project else '_not resolved_'} |",
        f"| Declared repositories | {repositories_cell} |",
        f"| Schema findings | {len(result.schema_errors)} |",
        f"| Repository check | {repository_check} |",
        "",
    ]
    if result.schema_errors:
        rows.extend(["### Schema findings", ""])
        rows.extend(f"- {_cell(item)}" for item in result.schema_errors)
        rows.append("")
    if result.repository_errors:
        rows.extend(["### Repository findings", ""])
        rows.extend(f"- {_cell(item)}" for item in result.repository_errors)
        rows.append("")
    return "\n".join(rows) + "\n"
