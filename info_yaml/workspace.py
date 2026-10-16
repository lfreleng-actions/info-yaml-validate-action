# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The shared error type and containment of paths within the workspace."""

from __future__ import annotations

from pathlib import Path


class InfoYamlError(RuntimeError):
    """Raised for unrecoverable, user-actionable configuration problems."""


def contain(
    relative: str, workspace: Path, *, label: str, base: Path | None = None
) -> Path:
    """Resolve a caller path and confirm it stays inside the workspace.

    A path escaping the checkout would let a workflow validate a file, or
    against a schema, that nobody reviewed. ``base`` resolves a relative
    path against a directory other than the workspace, for paths a
    consumer such as yamllint resolves against its working directory.
    """
    candidate = ((base or workspace) / relative).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise InfoYamlError(
            f"{label} '{relative}' resolves outside the workspace; supply a "
            "path inside the repository"
        ) from None
    return candidate
