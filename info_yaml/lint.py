# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The yamllint stage: configuration discovery, containment and linting.

yamllint is GPL-3.0 licensed, so this Apache-2.0 code imports nothing
from it. The stage runs the packaged ``yamllint`` command as a separate
process, as a pre-commit hook would, and parses its ``parsable`` output.
The command makes no network calls.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 - runs the pinned yamllint executable alone
import sysconfig
from pathlib import Path, PurePath
from typing import Any

import yaml

from info_yaml.report import LintFinding
from info_yaml.workspace import InfoYamlError, contain

# The names yamllint itself discovers, in its order of preference.
LINT_CONFIG_NAMES = (".yamllint", ".yamllint.yaml", ".yamllint.yml")
# The configurations yamllint bundles, which 'extends' may name without a
# path. They are listed here rather than read from the installed package,
# because that would mean locating yamllint's files, which the import-free
# boundary rules out.
BUNDLED_LINT_CONFIGS = frozenset({"default", "relaxed"})
# A stand-in for yamllint's conf directory, for the lexical check below.
# The real location is not read from the installed package; the check
# needs no more than a directory in the host's path flavour.
BUNDLED_CONF_DIR = PurePath("yamllint", "conf")
YAMLLINT_COMMAND = "yamllint"
# The virtual environment the action synced yamllint into. When set, the
# executable is taken from there and nowhere else.
YAMLLINT_ENV_DIR_VAR = "YAMLLINT_ENV_DIR"
# Where a virtual environment keeps its executables, by platform.
VENV_BIN_DIRS = ("bin", "Scripts")
# yamllint reads these from the environment. The first names a
# configuration, which the explicit '-c' or '-d' below overrides anyway;
# the second changes how yamllint decodes every file it opens, including
# the configuration, and a decoding the preflight did not apply would let
# a path read differently by the two of them.
YAMLLINT_ENV_VARS = ("YAMLLINT_CONFIG_FILE", "YAMLLINT_FILE_ENCODING")

# One record of 'yamllint -f parsable' output. The rule identifier is the
# last parenthesised token; a syntax error carries the literal '(syntax)'.
# A message may span lines: yamllint prints document text such as a
# duplicated key as decoded, newline included, so the pattern is matched
# against accumulated lines until it closes on a rule.
_PARSABLE = re.compile(
    r"(?P<path>[^\n]*?):(?P<line>\d+):(?P<column>\d+): "
    r"\[(?P<level>error|warning)\] (?P<message>.*) \((?P<rule>[a-z0-9-]+)\)",
    re.DOTALL,
)


def _leaves_bundled_dir(name: str, conf_dir: PurePath = BUNDLED_CONF_DIR) -> bool:
    """Whether yamllint's bundled lookup for ``name`` lands outside ``conf_dir``.

    yamllint joins ``<name>.yaml`` onto its conf directory and opens the
    result when it exists. The join is lexical: an absolute name, or one
    that climbs with ``..``, lands outside the directory, and yamllint
    would open whatever file sits there with no containment at all. A
    POSIX name without '/' is one path component and cannot do this; a
    Windows one can, through a drive letter or a backslash. A name that
    is relative to a drive, such as ``C:base``, joins onto the directory
    when the drives match and replaces it when they differ; the stand-in
    directory has no drive, so such a name counts as leaving, which is
    the safe answer when the real drive is unknown.
    """
    return (conf_dir / f"{name}.yaml").parent != conf_dir


def _is_builtin_lint_config(name: str) -> bool:
    """Mirror yamllint's test for a bundled configuration such as 'default'.

    yamllint treats a name without a '/' as bundled when ``<name>.yaml``
    exists in its conf directory, and as a path otherwise. A name whose
    lookup would leave that directory is refused outright rather than
    treated as a path: the fallback would contain and inspect ``name``
    itself, while yamllint opened the ``.yaml`` the lookup resolved to,
    so the two could disagree about which file runs. A name outside the
    known set falls to the path branch and is contained in the
    workspace, which fails closed should a future yamllint bundle a
    further configuration.
    """
    if "/" in name:
        return False
    if _leaves_bundled_dir(name):
        raise InfoYamlError(
            f"yamllint 'extends' value '{name}' is neither a bundled "
            "configuration name nor a path inside the workspace"
        )
    return name in BUNDLED_LINT_CONFIGS


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
    its working directory, which is the workspace here. Containing only
    the configuration file would leave a repository free to point those
    anywhere on the runner.

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
        contain(name, workspace, label="yamllint ignore-from-file")

    extends = conf.get("extends")
    if "extends" in conf and not isinstance(extends, str):
        # yamllint tests the value for a '/' and raises TypeError on
        # anything but a string, which would end the run with a traceback
        # instead of a finding. An explicit 'extends: null' is such a
        # value: yamllint sees the key and fails on it, so it is tested
        # for presence, not for None.
        raise InfoYamlError(
            f"yamllint configuration '{config.name}' has an 'extends' value "
            f"of type {type(extends).__name__}; it must be a string"
        )
    if isinstance(extends, str) and not _is_builtin_lint_config(extends):
        parent = contain(extends, workspace, label="yamllint extends")
        if parent not in seen:
            _contain_lint_config(parent, workspace, seen)


def resolve_lint_config(workspace: Path) -> tuple[Path | None, str]:
    """Discover the workspace's yamllint configuration, or use 'default'.

    Returns the configuration file, ``None`` for yamllint's bundled
    default, and the name to report it under. Each candidate goes through
    the same containment as ``path``, so a symlinked ``.yamllint`` cannot
    steer the read outside the checkout.
    """
    for name in LINT_CONFIG_NAMES:
        candidate = contain(name, workspace, label="yamllint configuration")
        if not candidate.is_file():
            continue
        _contain_lint_config(candidate, workspace, set())
        return candidate, name
    return None, "default"


def _parse_finding(record: str) -> LintFinding | None:
    """Turn one record of parsable output into a finding, or ``None``."""
    match = _PARSABLE.fullmatch(record)
    if match is None:
        return None
    rule: str | None = match["rule"]
    message = match["message"]
    if rule == "syntax":
        # yamllint reports a syntax error without a rule; the '(syntax)'
        # suffix is part of its description, so it is kept in the message.
        rule = None
        message = f"{message} (syntax)"
    return LintFinding(
        line=int(match["line"]),
        column=int(match["column"]),
        level=match["level"],
        rule=rule,
        message=message,
    )


def _parse_report(stdout: str) -> tuple[list[LintFinding], str | None]:
    """Split parsable output into findings, plus any text left unparsed.

    Lines accumulate into a record until the record closes on a rule
    identifier, so a message holding a newline stays one finding. The
    close is textual: document text that happens to end a line with a
    parenthesised word closes the record early, and what follows is
    reported as unreadable rather than guessed at. That fails the run,
    which is the safe side.
    """
    findings: list[LintFinding] = []
    current: str | None = None
    for line in stdout.splitlines():
        if current is None:
            if not line.strip():
                continue
            current = line
        else:
            current = f"{current}\n{line}"
        finding = _parse_finding(current)
        if finding is not None:
            findings.append(finding)
            current = None
    return findings, current


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """The reason yamllint gave for failing, from stderr or else stdout.

    A configuration error is a single stderr line. A crash is a traceback
    whose frames are indented; the exception follows the last frame and
    may run to several lines, as PyYAML's marked errors do.
    """
    lines = [line.rstrip() for line in completed.stderr.splitlines() if line.strip()]
    if lines:
        frames = [i for i, line in enumerate(lines) if line.startswith("  File ")]
        if frames and lines[0].startswith("Traceback"):
            # The exception is the first unindented line after the last
            # frame, plus any indented continuation lines that follow it.
            start = next(
                (
                    i
                    for i in range(frames[-1] + 1, len(lines))
                    if not lines[i][:1].isspace()
                ),
                len(lines) - 1,
            )
            return " ".join(line.strip() for line in lines[start:])
        return lines[-1].strip()
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if lines:
        return lines[-1].strip()
    return f"exit status {completed.returncode}"


def _executable_in(directory: Path) -> str | None:
    """The yamllint executable in a virtual environment's scripts directory."""
    for name in (YAMLLINT_COMMAND, f"{YAMLLINT_COMMAND}.exe"):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def _interpreter_scripts_dir() -> Path:
    """The scripts directory of the environment running this code.

    A pre-commit hook, and ``uv tool install --with yamllint``, install
    yamllint into the same environment as the validator, so its
    executable sits beside the interpreter's own entry points.
    """
    return Path(sysconfig.get_path("scripts"))


def _search_path() -> str | None:
    """Search ``PATH`` for yamllint, never the current directory.

    ``shutil.which`` would serve, except that on Windows it searches the
    current directory first, whatever ``PATH`` says, and the current
    directory is the checkout under test. Only absolute ``PATH`` entries
    are searched, and an entry naming the current directory is skipped,
    so a repository cannot supply the executable through either route.
    """
    names = [YAMLLINT_COMMAND]
    if os.name == "nt":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        names = [
            f"{YAMLLINT_COMMAND}{ext}" for ext in extensions.split(os.pathsep) if ext
        ]
    cwd = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        try:
            if directory.resolve() == cwd:
                continue
        except OSError:
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _yamllint_executable() -> str:
    """Locate the yamllint command, preferring an install beside the validator.

    With ``YAMLLINT_ENV_DIR`` set, the executable must sit in that
    environment's ``bin`` or ``Scripts`` directory, named as the
    platform writes it; nothing else is tried. Without the variable, as
    when the script or the ``info-yaml-validate`` command runs outside
    the action, the scripts directory of the running interpreter's
    environment comes first, which is where a pre-commit hook installs
    yamllint, and then ``PATH``. Neither lookup consults the checkout
    under test, which ``shutil.which`` does on Windows through the
    current directory, so a repository cannot supply the executable.
    """
    env_dir = os.environ.get(YAMLLINT_ENV_DIR_VAR, "").strip()
    if env_dir:
        for sub in VENV_BIN_DIRS:
            executable = _executable_in(Path(env_dir) / sub)
            if executable is not None:
                return executable
        raise InfoYamlError(
            f"'{YAMLLINT_COMMAND}' was not found in {YAMLLINT_ENV_DIR_VAR} "
            f"'{env_dir}'; the action's install step did not run or failed"
        )
    executable = _executable_in(_interpreter_scripts_dir()) or _search_path()
    if executable is None:
        raise InfoYamlError(
            f"'{YAMLLINT_COMMAND}' is not on PATH; the action installs it "
            "before this step runs. Elsewhere, install it, or skip the lint "
            "stage with 'lint-yaml: false' (--no-lint-yaml)"
        )
    return executable


def lint_yaml(workspace: Path, filepath: str, config: Path | None) -> list[LintFinding]:
    """Run the yamllint command over ``filepath``, relative to ``workspace``.

    yamllint applies the configuration's ``ignore`` patterns to the path
    it is given, and resolves the relative paths a configuration names
    against its working directory, so both the path and the directory
    match what the containment preflight examined.

    yamllint exits 0 for a clean file or warnings alone, 1 for an
    error-level finding, and 255 when it rejects its configuration. It
    also exits 1 with a traceback for problems it raises rather than
    reports: a document its YAML reader refuses, such as one holding a
    NUL byte, a rule option that is an unparsable regular expression, or
    a ``locale`` the runner does not provide. Every outcome other than a
    fully parsed report becomes an ``InfoYamlError``, so the failure
    outputs are still written.
    """
    executable = _yamllint_executable()
    config_args = ["-c", str(config)] if config else ["-d", "default"]
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in YAMLLINT_ENV_VARS
    }
    try:
        completed = subprocess.run(  # nosec B603 - fixed executable, no shell
            [executable, "-f", "parsable", *config_args, "--", filepath],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise InfoYamlError(f"yamllint could not be run: {exc}") from None

    findings, unparsed = _parse_report(completed.stdout)

    has_errors = any(item.level == "error" for item in findings)
    expected_status = 1 if has_errors else 0
    if completed.returncode == 255:
        raise InfoYamlError(
            f"yamllint rejected its configuration: {_failure_detail(completed)}"
        )
    # yamllint prints findings as it goes, so a crash can follow a partial
    # report with the exit status an error-level finding would produce.
    # The traceback is the one reliable sign of it.
    crashed = "Traceback (most recent call last)" in completed.stderr
    if completed.returncode != expected_status or crashed:
        raise InfoYamlError(
            f"yamllint failed on '{filepath}': {_failure_detail(completed)}"
        )
    if unparsed:
        raise InfoYamlError(
            "yamllint wrote output this action cannot read: "
            f"{unparsed.splitlines()[0].strip()}"
        )
    return findings
