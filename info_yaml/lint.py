# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The yamllint stage: configuration discovery, containment and linting.

yamllint runs through its Python API, so findings arrive as objects with
a line, column, rule and level, and nothing touches the network.
"""

from __future__ import annotations

import locale
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Any

import yaml
from yamllint import linter
from yamllint.config import YamlLintConfig, YamlLintConfigError

from info_yaml.report import LintFinding
from info_yaml.workspace import InfoYamlError, contain

# The names yamllint itself discovers, in its order of preference.
LINT_CONFIG_NAMES = (".yamllint", ".yamllint.yaml", ".yamllint.yml")
YAMLLINT_BUILTIN_CONFIGS = Path(linter.__file__).resolve().parent / "conf"


def _leaves_bundled_dir(conf_dir: PurePath, name: str) -> bool:
    """Whether yamllint's bundled lookup for ``name`` lands outside ``conf_dir``.

    yamllint joins ``<name>.yaml`` onto its conf directory and opens the
    result when it exists. The join is lexical: an absolute name, or one
    that climbs with ``..``, lands outside the directory, and yamllint
    would open whatever file sits there with no containment at all. A
    POSIX name without '/' is one path component and cannot do this;
    a Windows one can, through a drive letter or a backslash.
    """
    return (conf_dir / f"{name}.yaml").parent != conf_dir


def _is_builtin_lint_config(name: str) -> bool:
    """Mirror yamllint's test for a bundled configuration such as 'default'.

    A name whose lookup would leave the bundled directory is refused
    outright rather than treated as a path: the fallback would contain
    and inspect ``name`` itself, while yamllint opens the ``.yaml`` the
    lookup resolved to, so the two could disagree about which file runs.
    """
    if "/" in name:
        return False
    if _leaves_bundled_dir(YAMLLINT_BUILTIN_CONFIGS, name):
        raise InfoYamlError(
            f"yamllint 'extends' value '{name}' is neither a bundled "
            "configuration name nor a path inside the workspace"
        )
    return (YAMLLINT_BUILTIN_CONFIGS / f"{name}.yaml").is_file()


def _as_names(value: Any) -> list[str]:
    """Normalise a yamllint filename setting: one string, or a list of them."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _contain_lint_config(config: Path, workspace: Path, seen: set[Path]) -> None:
    """Confirm every file a yamllint configuration reads is in the workspace.

    yamllint opens the paths named by ``extends`` and by any
    ``ignore-from-file``, top-level or per rule, as given and relative to
    its working directory. Containing only the configuration file would
    leave a repository free to point those anywhere on the runner.

    This check fails closed. A configuration this preflight cannot read
    and parse as UTF-8 YAML is refused rather than handed to yamllint,
    which decodes other encodings for itself and would follow paths the
    preflight never saw.
    """
    seen.add(config)
    try:
        conf = yaml.safe_load(config.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise InfoYamlError(
            f"yamllint configuration '{config.name}' is not UTF-8; only UTF-8 "
            "configurations can be checked for the files they read"
        ) from None
    except (OSError, yaml.YAMLError) as exc:
        raise InfoYamlError(
            f"yamllint configuration '{config.name}' could not be read: {exc}"
        ) from None
    if not isinstance(conf, dict):
        return

    names = _as_names(conf.get("ignore-from-file"))
    rules = conf.get("rules")
    if isinstance(rules, dict):
        for rule in rules.values():
            if isinstance(rule, dict):
                names.extend(_as_names(rule.get("ignore-from-file")))
    for name in names:
        contain(name, workspace, label="yamllint ignore-from-file", base=Path.cwd())

    extends = conf.get("extends")
    if "extends" in conf and not isinstance(extends, str):
        # yamllint tests the value for a '/' and raises TypeError on
        # anything but a string, which would escape as a traceback. An
        # explicit 'extends: null' is such a value: yamllint sees the key
        # and fails on it, so it is tested for presence, not for None.
        raise InfoYamlError(
            f"yamllint configuration '{config.name}' has an 'extends' value "
            f"of type {type(extends).__name__}; it must be a string"
        )
    if isinstance(extends, str) and not _is_builtin_lint_config(extends):
        parent = contain(extends, workspace, label="yamllint extends", base=Path.cwd())
        if parent not in seen:
            _contain_lint_config(parent, workspace, seen)


def resolve_lint_config(workspace: Path) -> tuple[YamlLintConfig, str]:
    """Discover the workspace's yamllint configuration, or use 'default'.

    Returns the configuration and the name to report it under. Each
    candidate goes through the same containment as ``path``, so a
    symlinked ``.yamllint`` cannot steer the read outside the checkout.
    """
    for name in LINT_CONFIG_NAMES:
        candidate = contain(name, workspace, label="yamllint configuration")
        if not candidate.is_file():
            continue
        _contain_lint_config(candidate, workspace, set())
        try:
            return YamlLintConfig(file=str(candidate)), name
        except (YamlLintConfigError, OSError, UnicodeDecodeError, TypeError) as exc:
            # TypeError covers a malformed value yamllint applies without
            # checking its type; the preflight catches the known case,
            # and this keeps any other from escaping as a traceback.
            raise InfoYamlError(
                f"yamllint configuration '{name}' could not be loaded: {exc}"
            ) from None
    return YamlLintConfig("extends: default"), "default"


@contextmanager
def _collation(config: YamlLintConfig) -> Iterator[None]:
    """Apply the configuration's ``locale`` for the duration of a lint run.

    ``YamlLintConfig`` records the setting but only yamllint's CLI applies
    it, so ``linter.run`` alone would judge ``key-ordering`` under the
    C locale and disagree with the project's own yamllint. The previous
    collation is restored on the way out, whatever happens inside.
    """
    wanted = getattr(config, "locale", None)
    if not wanted:
        yield
        return
    previous = locale.setlocale(locale.LC_COLLATE)
    try:
        locale.setlocale(locale.LC_COLLATE, wanted)
    except locale.Error:
        raise InfoYamlError(
            f"yamllint configuration sets locale '{wanted}', which this "
            "runner does not provide"
        ) from None
    try:
        yield
    finally:
        locale.setlocale(locale.LC_COLLATE, previous)


def lint_yaml(text: str, config: YamlLintConfig, filepath: str) -> list[LintFinding]:
    """Run yamllint over ``text``; ``filepath`` drives its ``ignore`` rules.

    yamllint reports most problems as findings, but two kinds surface as
    exceptions from the generator instead: a document the YAML reader
    refuses outright, such as one holding a NUL byte, and a rule option
    that is an unparsable regular expression, which fails when the rule
    first applies it. Both are turned into ``InfoYamlError`` so the
    failure outputs are still written.
    """
    try:
        with _collation(config):
            problems = list(linter.run(text, config, filepath))
    except yaml.YAMLError as exc:
        raise InfoYamlError(f"'{filepath}' could not be read as YAML: {exc}") from None
    except re.error as exc:
        raise InfoYamlError(
            f"yamllint configuration holds an unparsable regular expression: {exc}"
        ) from None
    return [
        LintFinding(
            line=problem.line,
            column=problem.column,
            level=str(problem.level),
            rule=problem.rule,
            message=problem.desc,
        )
        for problem in problems
    ]
