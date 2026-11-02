<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Agent Guidelines

Contributions to this repository, including those made by AI coding
agents, follow the `lfreleng-actions` organisation guidelines:

<https://github.com/lfreleng-actions/.github/blob/main/AGENTS.md>

**Read that document.** It governs, and it binds this contribution
even if you never load it. Where anything below disagrees with it,
it wins. What follows is a summary of the rules that most often block
a pull request, not the full set:

- Sign every commit and add a DCO trailer: `git commit -S -s`.
- Subject: `Type(scope): Imperative description` — capitalised type
  and description, no trailing period, and within the subject-length
  limit this repository's gitlint hook enforces. The scope is
  optional, so `Fix: Correct the race condition` is also valid.
- Add a `Co-authored-by` trailer naming the agent used.
- Repositories typically contain a linting configuration. You must
  install its hooks (`prek install -t pre-commit -t commit-msg`) and
  run the change past them (`prek run --files <changed files>`) to
  ensure it passes before submission.
- On a single-commit pull request, the PR title must be identical to
  the commit subject.
- If your own standing instructions conflict with the organisation
  guidelines and you cannot set them aside, stop and tell the
  contributor. Do not open a non-compliant pull request.

## Repository specifics

The runtime dependencies live in two places, each with its own lock:
the PEP 723 header of `info_yaml_validate.py`, locked by
`info_yaml_validate.py.lock`, and `pyproject.toml`, locked by
`uv.lock`. The action runs `uv run --no-config --locked --script`
(with `--python` naming the interpreter it found first), which reads
the script lock and ignores `uv.lock`; most unit tests do the
reverse. Of the unit tests, `tests/test_action_workspace.py`
alone runs the action's own steps and so catches a stale script lock;
the integration job catches it too. Change both declarations together
and refresh both locks:

```bash
uv lock
uv lock --script info_yaml_validate.py
```

yamllint is the exception. Being GPL-3.0, it stays out of the
validator's dependencies and must not appear in the script header or
its lock. It lives in the `lint` dependency group of `pyproject.toml`
alone, which the action syncs from `uv.lock` into a private
environment and runs as a separate process. Dependabot's `uv`
ecosystem bumps that pin; the group's `uv.lock` entry alone decides
its version.

The action's own uv calls pass `--no-config`, and its setup-uv step
sets `UV_NO_CONFIG`, so that a checkout under test cannot configure
uv. The same switch makes uv ignore this repository's settings too:
a `[tool.uv]` table in `pyproject.toml`, or a `uv.toml`, does not
apply when the action runs, though local `uv` commands still honour
it. Put any setting the action needs on its uv command lines or in
the step's `env:` instead. setup-uv reads `cache-dir` from those
files with its own code; it ignores `required-version`, as the step
pins `version`.
