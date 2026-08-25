<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 📋 INFO.yaml Validation Action

Checks an `INFO.yaml` file against a JSON schema and confirms that the
repositories it declares match the project it describes.

Linux Foundation projects keep governance metadata — leads, committers,
issue tracking, lifecycle state — in an `INFO.yaml` file at the repository
root. This action gates changes to that file.

## What it checks

1. **Schema conformance.** The action checks the document against a JSON
   Schema, defaulting to the copy of `info-schema.yaml` it bundles. It
   reports every violation in one pass.
2. **Declared repository.** Every entry under `repositories` must name
   either the project itself or an ancestor of it. Entries that are not
   strings draw a finding of their own: the bundled schema declares no
   `repositories` subschema, so nothing else would report them.

The ancestor rule exists for subproject repositories: a Gerrit project
`integration/distribution` may carry an `INFO.yaml` whose `repositories`
list names the parent `integration`.

Change isolation — the rule that an `INFO.yaml` edit must stand alone,
apart from other file changes — falls outside this action. Use
[`lfreleng-actions/change-isolation-action`](https://github.com/lfreleng-actions/change-isolation-action)
with `paths: /INFO.yaml`, which implements that rule properly.

## Usage Example

```yaml
jobs:
  info-yaml:
    name: "INFO.yaml"
    runs-on: "ubuntu-latest"
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      # The action reads the project name from .gitreview when you omit it
      - uses: lfreleng-actions/info-yaml-validate-action@<SHA>
```

Naming the project explicitly, for a repository carrying no `.gitreview`:

```yaml
      - uses: lfreleng-actions/info-yaml-validate-action@<SHA>
        with:
          project: "integration/distribution"
```

Schema conformance alone:

```yaml
      - uses: lfreleng-actions/info-yaml-validate-action@<SHA>
        with:
          require-repository-match: "false"
```

## Inputs

<!-- markdownlint-disable MD013 -->

| Variable Name            | Required | Description                                                                  | Default     |
| ------------------------ | -------- | ---------------------------------------------------------------------------- | ----------- |
| path                     | False    | Workspace-relative path to the INFO.yaml file                                | `INFO.yaml` |
| project                  | False    | Project the file must describe; empty reads `.gitreview`                     |             |
| schema-path              | False    | Workspace-relative path to another schema; empty selects the bundled one     |             |
| require-repository-match | False    | Run the declared-repository check                                            | `true`      |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Output Name        | Description                                                 |
| ------------------ | ----------------------------------------------------------- |
| valid              | `true` when every enabled check passed, otherwise `false`   |
| project            | Project driving the repository check; empty when skipped    |
| repositories       | Newline-separated list of repositories the file declares    |
| schema-error-count | Count of schema violations                                  |

<!-- markdownlint-enable MD013 -->

`valid` reports on the checks that ran. With `require-repository-match`
set to `false`, schema conformance is the sole check, so `true` says
nothing about the repositories the file declares; the job summary records
the skip.

The action publishes all four outputs for input and validation failures
it handles itself, so a caller running the step under `continue-on-error`
reads `valid=false` rather than an empty string. A failure in the
surrounding composite — `setup-uv` failing, or `uv run` unable to install
the locked dependencies — happens before the validator starts, and leaves
the outputs unset. Callers branching on `valid` should treat an empty
value as a failed step rather than a verdict.

## Behaviour

### Resolving the project name

1. The `project` input, when non-empty.
2. The `[gerrit] project` key in `.gitreview`, minus any `.git` suffix.

When neither yields a value the action **fails** rather than skipping the
check. Guessing the project name would let the check pass against
something the caller never named — a green result that confirms nothing.

Set `require-repository-match: false` to skip the check on purpose; the
job summary then records the skip.

### The action gates

Findings fail the step. The action offers no `fail-on-*` escape hatch,
matching `action-pin-audit` in `security-workflows`: a check that you can
configure never to fail is a check whose green result means nothing.

### All findings at once

The action collects schema violations and reports them together. A
validator that stops at the first error turns a five-minute correction
into five review cycles.

## Security

- **Validation performs no network I/O.** The action reads the schema
  from disk, either from its own checkout or from a path inside the
  workspace, and refuses any `$ref` pointing outside the document. This
  covers the validation step alone: the composite still runs `setup-uv`,
  which may download the `uv` binary, and `uv run --locked`, which
  installs the pinned dependencies when the runner has no cache. Both
  fetch pinned, immutable content; the validator fetches nothing.
- **External schema references get refused.** jsonschema 4.26 retrieves
  unresolved `$ref` targets over the network by default, raising a
  `DeprecationWarning` and nothing more. A caller-supplied schema could
  otherwise reach an arbitrary URL, or a `file:` path outside the
  workspace, defeating the containment applied to `schema-path`. The
  action registers a resolver that refuses every retrieval and names the
  offending URI.
- The action resolves `path` and `schema-path` with `realpath` and
  confirms both sit inside `GITHUB_WORKSPACE`, so neither can reach a file
  outside the checkout. `.gitreview` goes through the same containment:
  no workflow input names it, but the repository under test owns it, and
  a symlink there would otherwise steer the read anywhere on the runner.
  Links pointing *within* the workspace keep working.
- Step outputs use a randomised heredoc delimiter for every value drawn
  from the repository, `project` included: `.gitreview` uses INI syntax,
  which permits a multi-line value, and a newline in a single-line record
  would let it append further keys — `valid` among them.
- Values reaching the job summary lose line breaks and control characters
  before rendering, and escape backslashes ahead of pipes. A newline would
  otherwise end the table row and let the rest render as a heading or a
  fresh table.
- Boolean inputs accept recognised spellings and nothing else. A
  mis-spelling such as `enabled` fails the run rather than disabling a
  gate without saying so.
- `uv.lock` and `info_yaml_validate.py.lock` pin every dependency, and
  `uv run --locked` installs those pinned versions.

### What this replaces

The workflows this action supersedes in `lfit/releng-reusable-workflows`
fetched **both** `info-schema.yaml` *and* `yaml-verify-schema.py` over
`wget` from the mutable `master` branch of `lfit/releng-global-jjb`, then
ran the downloaded Python. Anyone able to write to that branch could run
arbitrary code in every INFO.yaml verification across every consuming
project. Bundling the schema and shipping the validator here closes that
path.

## About the bundled schema

`schema/info-schema.yaml` comes verbatim from
[`lfit/releng-global-jjb`](https://github.com/lfit/releng-global-jjb/blob/master/schema/info-schema.yaml).
It keeps its original **EPL-1.0** licence, which `REUSE.toml` records; the
rest of this repository uses Apache-2.0.

The action validates with **Draft 4** and a format checker, matching the
legacy `yaml-verify-schema.py`, so files the old tooling accepted still
pass.

The schema does not itself conform to Draft 4, so the action skips
`check_schema`. Three defects carry over from Draft 3:

<!-- markdownlint-disable MD013 -->

| Location                                    | Problem                                                                                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_lead.properties.email.required`    | `required: false` spells a Draft 3 construct; Draft 4 expects an array. Draft 4 ignores it.                                                             |
| `project_lead.properties.timezone.required` | As above. Draft 4 ignores it.                                                                                                                           |
| `issue_tracking.properties.required`        | `required` sits *inside* `properties`, so it declares a property named `required` rather than constraining anything. Nothing enforces `type` and `url`. |

<!-- markdownlint-enable MD013 -->

A unit test asserts that the schema stays non-conformant. Should upstream
correct it, that test fails, and the action can then adopt `check_schema`.

Tightening the schema falls outside this change: doing so would start
failing files that have passed for years, and belongs in a separate change
carrying its own consultation.

## Development

```bash
uv run --frozen pytest -q
```
