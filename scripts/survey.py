# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema[format-nongpl]>=4.23", "PyYAML>=6", "referencing>=0.36"]
# ///
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Survey the root INFO.yaml of every repository in some GitHub organisations.

Each file is validated twice: with the legacy behaviour of
``yaml-verify-schema.py`` (JSON Schema Draft 4, the schema never checked
against its metaschema, no ``format: uri`` checking) against the legacy
schema, and with this action's validator core (Draft 2020-12, format
checking on) against the corrected schema. The report lists both verdicts
per repository, and the new findings in full wherever the verdicts differ.

The exit code is ``0`` when no file regresses (passes the legacy check and
fails the new one), ``1`` when any does, and ``2`` when the survey cannot
run: no token, a bad option, an unusable schema or an API failure.

Only the schema checks run. The yamllint stage and the declared-repository
check do not, since neither differs between the two behaviours.

The token is read from the environment variable ``--token-env`` names,
``GITHUB_TOKEN`` by default, and never from the command line.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError, UnknownType
from referencing import Registry
from referencing.exceptions import Unresolvable

# Run as a script, this file's own directory heads sys.path rather than
# the repository root, which holds the validator core the survey measures
# and the 'scripts' package its fetcher module is imported through.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from info_yaml.checks import format_error_path, validate_schema  # noqa: E402
from info_yaml.validate import BUNDLED_SCHEMA, load_yaml, parse_yaml  # noqa: E402
from info_yaml.workspace import InfoYamlError  # noqa: E402
from scripts.survey_github import (  # noqa: E402
    INFO_PATH,
    CachingFetcher,
    Fetcher,
    GitHubFetcher,
    SurveyError,
)

DEFAULT_ORGS = (
    "onap",
    "o-ran-sc",
    "FDio",
    "opendaylight",
    "anuket-project",
    "akraino-edge-stack",
    "edgexfoundry",
    "automotive-grade-linux",
)
LEGACY_SCHEMA = REPOSITORY_ROOT / "schema" / "info-schema-legacy.yaml"

# The formats jsonschema checks with no optional dependency installed,
# which is how yaml-verify-schema.py ran. The format-nongpl extra the new
# behaviour needs would otherwise add 'uri' and others to the legacy
# check, and hide part of the difference being measured.
LEGACY_FORMATS = ("date", "email", "idn-email", "ipv4", "ipv6", "regex", "uuid")

PASS = "pass"
FAIL = "fail"
MISSING = "missing"

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2


def legacy_validate_schema(document: Any, schema: Any) -> list[str]:
    """Validate as ``yaml-verify-schema.py`` did: Draft 4, schema unchecked.

    An empty registry keeps ``$ref`` resolution off the network, as the
    validator core's own registry does. A schema the legacy validator
    cannot apply failed the legacy check, so it is reported as a finding.
    """
    validator = jsonschema.Draft4Validator(
        schema,
        format_checker=jsonschema.FormatChecker(formats=LEGACY_FORMATS),
        registry=Registry(),
    )
    try:
        errors = sorted(validator.iter_errors(document), key=str)
    except (
        SchemaError,
        UnknownType,
        Unresolvable,
        AttributeError,
        TypeError,
        re.error,
    ) as exc:
        return [f"the legacy validator could not apply the schema: {exc}"]
    return [f"{format_error_path(error)}: {error.message}" for error in errors]


@dataclass
class Outcome:
    """The two verdicts for one repository."""

    repository: str
    old: str
    new: str
    old_findings: list[str] = field(default_factory=list)
    new_findings: list[str] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        """Whether the file passes the legacy check and fails the new one."""
        return self.old == PASS and self.new == FAIL


def assess(repository: str, content: bytes | None, old: Any, new: Any) -> Outcome:
    """Validate one file under both behaviours.

    A file that is not UTF-8 or not YAML fails both, as it did before.
    """
    if content is None:
        return Outcome(repository, MISSING, MISSING)
    try:
        text = content.decode("utf-8")
        document = parse_yaml(text, Path(repository, INFO_PATH), label=INFO_PATH)
    except UnicodeDecodeError:
        finding = f"{INFO_PATH} is not valid UTF-8"
        return Outcome(repository, FAIL, FAIL, [finding], [finding])
    except InfoYamlError as exc:
        return Outcome(repository, FAIL, FAIL, [str(exc)], [str(exc)])
    old_findings = legacy_validate_schema(document, old)
    new_findings = validate_schema(document, new)
    return Outcome(
        repository,
        FAIL if old_findings else PASS,
        FAIL if new_findings else PASS,
        old_findings,
        new_findings,
    )


def survey(
    fetcher: Fetcher,
    orgs: Iterable[str],
    old: Any,
    new: Any,
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, list[Outcome]]:
    """Assess every repository in each organisation."""
    results: dict[str, list[Outcome]] = {}
    for org in orgs:
        repositories = fetcher.list_repositories(org)
        progress(f"{org}: {len(repositories)} repositories")
        results[org] = [
            assess(name, fetcher.fetch_info_yaml(org, name), old, new)
            for name in repositories
        ]
    return results


def _cell(text: str) -> str:
    """Make text safe inside a Markdown table cell."""
    return html.escape(" ".join(text.split()), quote=False).replace("|", "\\|")


def _totals(outcomes: Sequence[Outcome]) -> str:
    present = [item for item in outcomes if item.old != MISSING]
    counts = {
        (old, new): sum((item.old, item.new) == (old, new) for item in present)
        for old in (PASS, FAIL)
        for new in (PASS, FAIL)
    }
    return (
        f"**Totals:** {len(outcomes)} repositories, {len(present)} with "
        f"{INFO_PATH}, {len(outcomes) - len(present)} without; "
        f"pass → pass {counts[PASS, PASS]}, "
        f"fail → fail {counts[FAIL, FAIL]}, "
        f"fail → pass {counts[FAIL, PASS]}, "
        f"**pass → fail {counts[PASS, FAIL]}**"
    )


def render(results: dict[str, list[Outcome]]) -> str:
    """Render the survey as Markdown, one table per organisation."""
    lines = [
        f"# {INFO_PATH} survey",
        "",
        "- Old: Draft 4, the legacy schema, no `format: uri` checking.",
        "- New: Draft 2020-12, the corrected schema, format checking on.",
    ]
    for org, outcomes in results.items():
        lines += ["", f"## {_cell(org)}", ""]
        present = [item for item in outcomes if item.old != MISSING]
        if present:
            lines += [
                "| Repository | Old | New | New findings |",
                "| --- | --- | --- | --- |",
            ]
            for item in present:
                findings = (
                    "<br>".join(_cell(text) for text in item.new_findings)
                    if item.old != item.new
                    else ""
                )
                lines.append(
                    f"| {_cell(item.repository)} | {item.old} | {item.new} "
                    f"| {findings} |"
                )
            lines.append("")
        absent = [_cell(item.repository) for item in outcomes if item.old == MISSING]
        if absent:
            lines += [f"Without {INFO_PATH}: {', '.join(absent)}", ""]
        lines.append(_totals(outcomes))
    return "\n".join(lines) + "\n"


def regressions(results: dict[str, list[Outcome]]) -> int:
    """Count the files that pass the legacy check and fail the new one."""
    return sum(item.regressed for items in results.values() for item in items)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="survey.py",
        description=(
            "Validate the root INFO.yaml of every public, non-archived, "
            "non-fork repository in some GitHub organisations under the "
            "legacy and the corrected schema behaviour. Exits 0 when no "
            "file regresses, 1 when any does and 2 on an error."
        ),
    )
    parser.add_argument(
        "--org",
        action="append",
        dest="orgs",
        metavar="ORG",
        help=f"organisation to survey, repeatable (default: {', '.join(DEFAULT_ORGS)})",
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        metavar="NAME",
        help="environment variable holding the API token (default: %(default)s)",
    )
    parser.add_argument(
        "--old-schema",
        type=Path,
        default=LEGACY_SCHEMA,
        metavar="PATH",
        help="legacy schema (default: schema/info-schema-legacy.yaml)",
    )
    parser.add_argument(
        "--new-schema",
        type=Path,
        default=BUNDLED_SCHEMA,
        metavar="PATH",
        help="corrected schema (default: schema/info-schema.yaml)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        metavar="DIR",
        help="keep fetched files here and reuse them on the next run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="write the Markdown report here (default: standard output)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, fetcher: Fetcher | None = None) -> int:
    """Run the survey, write the report and return the exit code."""
    args = parse_args(argv)
    try:
        old = load_yaml(args.old_schema, label="Old schema")
        new = load_yaml(args.new_schema, label="New schema")
        if fetcher is None:
            token = os.environ.get(args.token_env, "").strip()
            if not token:
                raise SurveyError(
                    f"no token in ${args.token_env}; the survey makes "
                    "authenticated requests only"
                )
            fetcher = GitHubFetcher(token)
        if args.cache_dir is not None:
            fetcher = CachingFetcher(fetcher, args.cache_dir)
        results = survey(
            fetcher,
            args.orgs or DEFAULT_ORGS,
            old,
            new,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except (InfoYamlError, SurveyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    report = render(results)
    if args.output is None:
        sys.stdout.write(report)
    else:
        args.output.write_text(report, encoding="utf-8")
    count = regressions(results)
    print(f"{count} file(s) regress under the new schema", file=sys.stderr)
    return EXIT_REGRESSION if count else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
