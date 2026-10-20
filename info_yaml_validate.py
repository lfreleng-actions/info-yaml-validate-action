# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema[format-nongpl]>=4.23", "PyYAML>=6", "referencing>=0.36"]
# ///
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate an ``INFO.yaml`` file against a schema and its declared repository.

The script performs three checks, in this order:

1. **YAML lint** — the ``yamllint`` command runs over the file first,
   with the workspace's ``.yamllint``, ``.yamllint.yaml`` or
   ``.yamllint.yml``, or yamllint's ``default`` configuration when there
   is none. Running it first means a file that does not parse reports a
   located syntax error rather than a parser exception. Error-level
   findings fail the run; warning-level findings are reported only.
   yamllint is GPL-3.0 licensed, so it runs as a separate process that
   the action installs beside this script; nothing here imports it.
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
from this action's own checkout or from a path the caller supplies, and
yamllint fetches nothing. Nothing is downloaded at run time, which is the
substantive difference from the workflows this action replaces: they
fetched both the schema and the validator over ``wget`` from a mutable
branch and executed the result.

This script is the action's entry point. It reads its inputs from
``INPUT_*`` environment variables (the GitHub Actions convention) and
hands them to the validation core in ``info_yaml.validate``. The
yamllint stage lives in ``info_yaml.lint``, the schema and repository
checks in ``info_yaml.checks``, workspace containment in
``info_yaml.workspace`` and reporting in ``info_yaml.report``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from info_yaml.validate import fail, run
from info_yaml.workspace import InfoYamlError

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


def main() -> int:
    """Entry point: read the action inputs and run the checks."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    try:
        require_repository_match = _as_bool(
            os.environ.get("INPUT_REQUIRE_REPOSITORY_MATCH", ""),
            default=True,
            name="require-repository-match",
        )
        lint_enabled = _as_bool(
            os.environ.get("INPUT_LINT_YAML", ""), default=True, name="lint-yaml"
        )
    except InfoYamlError as exc:
        return fail(exc)
    return run(
        workspace,
        path=os.environ.get("INPUT_PATH", "").strip(),
        project=os.environ.get("INPUT_PROJECT", ""),
        schema=os.environ.get("INPUT_SCHEMA_PATH", "").strip(),
        require_repository_match=require_repository_match,
        lint_enabled=lint_enabled,
    )


if __name__ == "__main__":
    sys.exit(main())
