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
    try:
        candidate = ((base or workspace) / relative).resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        # A NUL byte, which the OS refuses in a path, raises ValueError
        # from resolve(); a yamllint configuration can spell one as an
        # escape. A symbolic link loop raises RuntimeError on Python
        # 3.10 to 3.12. Report both rather than let them escape as a
        # traceback.
        raise InfoYamlError(f"{label} {relative!r} cannot be resolved: {exc}") from None
    if candidate.is_symlink():
        # Python 3.13 and later return a looping link unresolved instead
        # of raising. A fully resolved path is never a link, so this is
        # the loop case, reported the same way on every version.
        raise InfoYamlError(
            f"{label} {relative!r} cannot be resolved: symbolic link loop"
        )
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise InfoYamlError(
            f"{label} '{relative}' resolves outside the workspace; supply a "
            "path inside the repository"
        ) from None
    return candidate
