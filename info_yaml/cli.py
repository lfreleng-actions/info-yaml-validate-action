# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The ``info-yaml-validate`` command, also run as a pre-commit hook.

It runs the same checks as the action, through ``info_yaml.validate``,
over each path it is given, or over ``INFO.yaml`` in the current
directory when given none. The current directory is the working tree:
paths, ``.gitreview`` and the yamllint configuration resolve against it,
and every path must stay inside it. pre-commit runs hooks from the
repository root and passes paths relative to it, so the two agree.

The output is plain text. Under GitHub Actions, where ``GITHUB_ACTIONS``
is ``true``, the command also writes ``::error`` annotations and appends
to the job summary. It never writes step outputs; those belong to the
action.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from info_yaml.validate import DEFAULT_INFO_PATH, run


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line."""
    parser = argparse.ArgumentParser(
        prog="info-yaml-validate",
        description=(
            "Lint INFO.yaml files with yamllint, validate them against the "
            "INFO.yaml schema and check the repositories they declare."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=f"files to validate (default: {DEFAULT_INFO_PATH})",
    )
    parser.add_argument(
        "--project",
        default="",
        metavar="NAME",
        help=(
            "project the files must describe (default: the [gerrit] project "
            "in .gitreview, without any .git suffix)"
        ),
    )
    parser.add_argument(
        "--schema",
        default="",
        metavar="PATH",
        help="alternative JSON schema inside the working tree "
        "(default: the bundled schema)",
    )
    parser.add_argument(
        "--no-repository-match",
        dest="repository_match",
        action="store_false",
        help="skip the check that declared repositories match the project",
    )
    parser.add_argument(
        "--no-lint-yaml",
        dest="lint_yaml",
        action="store_false",
        help="skip the yamllint stage",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate each path; exit non-zero when any of them fails."""
    args = build_parser().parse_args(argv)
    github = os.environ.get("GITHUB_ACTIONS") == "true"
    workspace = Path.cwd()
    status = 0
    for path in args.paths or [DEFAULT_INFO_PATH]:
        status |= run(
            workspace,
            path=path,
            project=args.project,
            schema=args.schema,
            require_repository_match=args.repository_match,
            lint_enabled=args.lint_yaml,
            github=github,
            outputs=False,
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
