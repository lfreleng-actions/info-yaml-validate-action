<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 📋 INFO.yaml Validation Action

Lints an `INFO.yaml` file with yamllint, checks it against a JSON schema
and confirms that the repositories it declares match the project it
describes.

Linux Foundation projects keep governance metadata — leads, committers,
issue tracking, lifecycle state — in an `INFO.yaml` file at the repository
root. This action gates changes to that file.

## What it checks

The checks run in this order:

1. **YAML lint.** The [yamllint](https://yamllint.readthedocs.io/)
   command runs over the file, as the legacy workflows did before schema
   validation. A schema-valid file can still carry trailing whitespace,
   odd indentation or a missing document start that a project's own
   linting would reject. Error-level findings fail the step;
   warning-level findings appear as warnings and do not. Running first
   means a file that fails to parse reports a located syntax error, and
   the later checks do not run.
2. **Schema conformance.** The action checks the document against a JSON
   Schema (Draft 2020-12), defaulting to the copy of `info-schema.yaml` it
   bundles. It reports every violation in one pass.
3. **Declared repository.** Every entry under `repositories` must name
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

Schema conformance and YAML lint, without the repository check:

```yaml
      - uses: lfreleng-actions/info-yaml-validate-action@<SHA>
        with:
          require-repository-match: "false"
```

Skipping the YAML lint stage:

```yaml
      - uses: lfreleng-actions/info-yaml-validate-action@<SHA>
        with:
          lint-yaml: "false"
```

## Inputs

<!-- markdownlint-disable MD013 -->

| Variable Name            | Required | Description                                                                  | Default     |
| ------------------------ | -------- | ---------------------------------------------------------------------------- | ----------- |
| path                     | False    | Workspace-relative path to the INFO.yaml file                                | `INFO.yaml` |
| project                  | False    | Project the file must describe; empty reads `.gitreview`                     |             |
| schema-path              | False    | Workspace-relative path to another schema; empty selects the bundled one     |             |
| require-repository-match | False    | Run the declared-repository check                                            | `true`      |
| lint-yaml                | False    | Run yamllint over the file before schema validation                          | `true`      |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Output Name        | Description                                                 |
| ------------------ | ----------------------------------------------------------- |
| valid              | `true` when every enabled check passed, otherwise `false`   |
| project            | Project driving the repository check; empty when skipped    |
| repositories       | Newline-separated list of repositories the file declares    |
| schema-error-count | Count of schema violations                                  |
| lint-error-count   | Count of error-level yamllint findings; `0` when skipped    |

<!-- markdownlint-enable MD013 -->

`valid` reports on the checks that ran. With `require-repository-match`
set to `false`, `true` says nothing about the repositories the file
declares; with `lint-yaml` set to `false`, it says nothing about the
file's formatting. The job summary records each skip.

The action publishes all five outputs for input and validation failures
it handles itself, so a caller running the step under `continue-on-error`
reads `valid=false` rather than an empty string. A failure in the
surrounding composite — `setup-uv` failing, or `uv run` unable to install
the locked dependencies — happens before the validator starts, and leaves
the outputs unset. Callers branching on `valid` should treat an empty
value as a failed step rather than a verdict.

## Behaviour

### YAML lint configuration

The lint stage uses the first of `.yamllint`, `.yamllint.yaml` and
`.yamllint.yml` it finds at the workspace root, the names yamllint itself
discovers. Without one it applies yamllint's `default` configuration. A
repository that wants relaxed rules expresses that in its own
configuration, for example:

```yaml
---
extends: relaxed
```

The configuration's `ignore` patterns apply too, so a configuration that
ignores the `INFO.yaml` path leaves the lint stage with nothing to report.
The job summary names the configuration in use.

### How yamllint runs

yamllint is **GPL-3.0** licensed and this action is Apache-2.0, so the
validator imports nothing from it. yamllint lives in its own `lint`
dependency group in `pyproject.toml`, pinned and hash-checked through
`uv.lock`, and not among the validator's dependencies: its PEP 723
header and script lock do not list it. The composite syncs that group
alone (`uv sync --locked --no-install-project`, restricted to the
`lint` group) into a private environment under `RUNNER_TEMP`, and the
validator runs the `yamllint` executable from that environment by
absolute path, as a separate process, as a pre-commit hook would,
reading its `parsable` output. The `dev` group includes `lint`, so the
test suite runs the same command. Dependabot's `uv` ecosystem tracks
the lock, so a yamllint release arrives as a lock bump rather than a
hand edit.

The install step runs whatever `lint-yaml` says, so that the shell
never has to agree with the validator about which spellings of the
input mean `false`. Running the script by hand, without
`YAMLLINT_ENV_DIR` set, needs a `yamllint` on `PATH`, or
`lint-yaml: false`.

yamllint applies the configuration's `locale` itself, and a `locale`
the runner does not provide fails the run. `extends` may name a
bundled configuration, `default` or `relaxed`, or a file inside the
workspace; the action does not read the installed yamllint to learn
which names it bundles, so it treats any other bare name as a
workspace path and refuses it when no such file exists.

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
  workspace, and refuses any `$ref` pointing outside the document;
  yamllint lints a file on disk and fetches nothing. This covers the
  validation step alone: the composite still runs `setup-uv`, which may
  download the `uv` binary, `uv sync --locked`, which installs the
  locked yamllint, and `uv run --locked`, which installs the pinned
  dependencies when the runner has no cache. All three fetch pinned,
  immutable content; the validator fetches nothing.
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
- The yamllint configuration gets the same containment, and so does every
  file it names: yamllint opens `extends` and `ignore-from-file` paths
  as written, so a configuration could otherwise read any file on the
  runner. yamllint's bundled configurations, `default` and `relaxed`,
  remain available to `extends`.
- yamllint runs as a child process with a fixed executable and argument
  list, no shell, and the file path after `--`. The executable comes
  from the action's own synced environment by absolute path, never from
  a `PATH` search: on Windows that search includes the current
  directory, which is the checkout under test. The child does not
  inherit `YAMLLINT_FILE_ENCODING` or `YAMLLINT_CONFIG_FILE`, so
  yamllint decodes the configuration the way the containment preflight
  did and reads the configuration the preflight examined. The action
  accepts its report when every line parses and the exit status matches
  what the parsed findings predict; a traceback, an unexpected status or
  an unreadable line fails the run rather than standing in for a
  verdict.
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
  `uv run --locked` and `uv sync --locked` install those pinned
  versions: the validator and its dependencies from the script lock,
  yamllint and its two dependencies from the `lint` group in `uv.lock`.

### What this replaces

The workflows this action supersedes in `lfit/releng-reusable-workflows`
fetched **both** `info-schema.yaml` *and* `yaml-verify-schema.py` over
`wget` from the mutable `master` branch of `lfit/releng-global-jjb`, then
ran the downloaded Python. Anyone able to write to that branch could run
arbitrary code in every INFO.yaml verification across every consuming
project. Bundling the schema and shipping the validator here closes that
path.

## About the bundled schema

`schema/info-schema.yaml` derives from
[`lfit/releng-global-jjb`](https://github.com/lfit/releng-global-jjb/blob/master/schema/info-schema.yaml).
It keeps its original **EPL-1.0** licence, which `REUSE.toml` records; the
rest of this repository uses Apache-2.0.

The upstream copy was not a valid JSON Schema of any draft. This copy
corrects it to valid **Draft 2020-12**:

<!-- markdownlint-disable MD013 -->

| Location                                    | Upstream defect                                                                     | Correction                                       |
| ------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------ |
| `$schema`                                   | The unversioned `http://json-schema.org/schema#`                                    | `https://json-schema.org/draft/2020-12/schema`   |
| `project_lead.properties.email.required`    | `required: false`, a Draft 3 spelling that later drafts ignore                      | Removed; properties are optional by default      |
| `project_lead.properties.timezone.required` | As above                                                                            | Removed                                          |
| `issue_tracking.properties.required`        | Declared a *property named `required`*, so nothing enforced `type` and `url`        | Moved to the `issue_tracking` object             |

<!-- markdownlint-enable MD013 -->

The `$id` names this repository, which now owns the copy.

### How the action validates

- The action checks the schema against the Draft 2020-12 metaschema
  **before** it validates the document. A defective schema, bundled or
  supplied through `schema-path`, fails with an error naming the
  offending location rather than a misleading document finding.
- Validation uses Draft 2020-12 with format checking enabled.
  `format: uri` uses `rfc3986-validator`, installed through the
  `jsonschema[format-nongpl]` extra; the plain `format` extra would pull
  in the GPL-licensed `rfc3987`. `format: email` checks for an `@` and
  nothing more.

These are stricter than the legacy `yaml-verify-schema.py`:
`issue_tracking.type` and `issue_tracking.url` are now required, and URI
values must parse as absolute RFC 3986 URIs. A file the old tooling
accepted can fail here on either count.

### YAML 1.1 parsing

The action parses `INFO.yaml` with PyYAML, which implements YAML 1.1, as
the legacy validator did. Unquoted scalars can load as types other than
strings and then fail the schema's `type: string`:

- `project_creation_date: 2017-02-10` loads as a date. Quote it:
  `"2017-02-10"`.
- `yes`, `no`, `on` and `off` load as booleans.

Quote such values.

## Development

```bash
uv run --frozen pytest -q
```
