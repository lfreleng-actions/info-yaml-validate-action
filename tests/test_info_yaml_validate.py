# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the INFO.yaml validator."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import jsonschema
import pytest
import yaml

import info_yaml_validate as iyv
from info_yaml import lint, report
from info_yaml import validate as core

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A document that satisfies the bundled schema. Individual tests copy this
# and mutate one field, so a failure names the field it broke.
VALID_INFO: dict[str, Any] = {
    "project": "example",
    "project_creation_date": "2026-01-01",
    "project_category": "",
    "lifecycle_state": "Incubation",
    "project_lead": {
        "name": "Ada Lovelace",
        "email": "ada@example.org",
        "id": "ada",
        "company": "Example",
        "timezone": "Europe/London",
    },
    "primary_contact": {
        "name": "Ada Lovelace",
        "email": "ada@example.org",
        "id": "ada",
        "company": "Example",
        "timezone": "Europe/London",
    },
    "issue_tracking": {
        "type": "jira",
        "url": "https://jira.example.org/browse/EXAMPLE",
        "key": "EXAMPLE",
    },
    "mailing_list": {"type": "groups.io", "url": "https://lists.example.org"},
    "realtime_discussion": {"type": "irc", "server": "irc.example.org"},
    "repositories": ["example"],
    "committers": [
        {
            "name": "Ada Lovelace",
            "email": "ada@example.org",
            "id": "ada",
            "company": "Example",
            "timezone": "Europe/London",
        }
    ],
    "tsc": {"approval": "https://example.org/tsc"},
}


@pytest.fixture
def schema() -> Any:
    """The schema bundled with the action."""
    return yaml.safe_load(core.BUNDLED_SCHEMA.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_valid_document_produces_no_findings(schema: Any) -> None:
    assert core.validate_schema(VALID_INFO, schema) == []


def test_missing_required_key_is_reported(schema: Any) -> None:
    document = {key: value for key, value in VALID_INFO.items() if key != "committers"}
    findings = core.validate_schema(document, schema)
    assert any("committers" in finding for finding in findings)


def test_all_findings_are_collected_not_just_the_first(schema: Any) -> None:
    """A caller fixing one error per run turns one correction into many."""
    document = {
        key: value
        for key, value in VALID_INFO.items()
        if key not in {"committers", "tsc", "repositories"}
    }
    assert len(core.validate_schema(document, schema)) >= 3


def test_invalid_lifecycle_state_is_reported(schema: Any) -> None:
    document = dict(VALID_INFO, lifecycle_state="Flourishing")
    findings = core.validate_schema(document, schema)
    assert any("lifecycle_state" in finding for finding in findings)


def test_every_documented_lifecycle_state_is_accepted(schema: Any) -> None:
    for state in (
        "Incubation",
        "Proposal",
        "Mature",
        "Core",
        "Top Level",
        "Unmaintained",
        "Archived",
        "Null",
        "Integration",
    ):
        document = dict(VALID_INFO, lifecycle_state=state)
        assert core.validate_schema(document, schema) == [], state


def test_unknown_key_in_user_object_is_reported(schema: Any) -> None:
    """The user object sets additionalProperties: false."""
    lead = dict(VALID_INFO["project_lead"], nickname="ada")
    document = dict(VALID_INFO, project_lead=lead)
    findings = core.validate_schema(document, schema)
    assert any("project_lead" in finding for finding in findings)


def test_unknown_type_in_schema_is_reported_against_the_schema() -> None:
    """A broken caller schema must not read as a document finding."""
    with pytest.raises(core.InfoYamlError, match=r"schema itself.*\$\.type"):
        core.validate_schema(VALID_INFO, {"type": "not-a-real-type"})


def test_unparsable_pattern_is_reported_against_the_schema() -> None:
    """The metaschema checks 'pattern' as a regex before it reaches re.search."""
    schema = {"type": "object", "properties": {"project": {"pattern": "["}}}
    with pytest.raises(core.InfoYamlError, match="schema itself is not valid"):
        core.validate_schema({"project": "example"}, schema)


def test_unparsable_pattern_properties_is_reported() -> None:
    schema = {"type": "object", "patternProperties": {"(": {"type": "string"}}}
    with pytest.raises(core.InfoYamlError, match="schema itself is not valid"):
        core.validate_schema({"project": "example"}, schema)


# The metaschema only describes the keywords it knows, so a subschema
# stored under an unknown keyword passes check_schema unread and fails
# only once a $ref leads document validation into it. Each case below
# would otherwise escape main()'s InfoYamlError handler and abort the
# run before the failure outputs are written.
@pytest.mark.parametrize(
    "subschema",
    [
        pytest.param({"type": "not-a-real-type"}, id="unknown-type"),
        pytest.param(
            {"type": "object", "properties": {"project": {"pattern": "["}}},
            id="unparsable-pattern",
        ),
        pytest.param(
            {"type": "object", "patternProperties": {"(": {}}},
            id="unparsable-pattern-properties",
        ),
    ],
)
def test_defect_behind_internal_ref_is_reported_against_the_schema(
    subschema: dict[str, Any],
) -> None:
    schema = {"$ref": "#/custom", "custom": subschema}
    # The defect is invisible to the metaschema check...
    jsonschema.Draft202012Validator.check_schema(schema)
    # ...and must still surface as a schema error, not an uncaught exception.
    with pytest.raises(core.InfoYamlError, match="schema could not be applied"):
        core.validate_schema({"project": "example"}, schema)


def test_bundled_schema_is_valid_draft_2020_12(schema: Any) -> None:
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("key", ["type", "url"])
def test_issue_tracking_key_is_required(schema: Any, key: str) -> None:
    tracking = {k: v for k, v in VALID_INFO["issue_tracking"].items() if k != key}
    document = dict(VALID_INFO, issue_tracking=tracking)
    findings = core.validate_schema(document, schema)
    assert any(
        finding.startswith("issue_tracking:") and f"'{key}'" in finding
        for finding in findings
    )


def test_invalid_uri_is_reported(schema: Any) -> None:
    tracking = dict(VALID_INFO["issue_tracking"], url="not a valid uri")
    document = dict(VALID_INFO, issue_tracking=tracking)
    findings = core.validate_schema(document, schema)
    assert any(finding.startswith("issue_tracking.url:") for finding in findings)


def test_invalid_caller_schema_fails_before_the_document_is_checked() -> None:
    """A Draft 3 'required: false' must read as a schema defect."""
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string", "required": False}},
    }
    with pytest.raises(core.InfoYamlError, match=r"schema itself.*required"):
        core.validate_schema({"email": "ada@example.org"}, schema)


@pytest.mark.parametrize(
    ("fixture", "valid"),
    [
        ("valid", True),
        ("wrong-repository", True),
        ("invalid-schema", False),
        ("missing-issue-tracking-keys", False),
        ("invalid-uri", False),
        # Schema-valid on purpose: only yamllint should reject it.
        ("lint-failure", True),
    ],
)
def test_fixture_schema_verdict(schema: Any, fixture: str, valid: bool) -> None:
    document = core.load_yaml(FIXTURES / fixture / "INFO.yaml", label="INFO.yaml")
    assert (core.validate_schema(document, schema) == []) is valid


def test_bundled_schema_still_validates_a_good_document(schema: Any) -> None:
    """Non-conformance must stay harmless in practice, not just in theory."""
    assert core.validate_schema(VALID_INFO, schema) == []


def test_findings_are_ordered_deterministically(schema: Any) -> None:
    """Unstable ordering makes a diff between two runs unreadable."""
    document = {
        key: value
        for key, value in VALID_INFO.items()
        if key not in {"committers", "tsc"}
    }
    assert core.validate_schema(document, schema) == core.validate_schema(
        document, schema
    )


# --------------------------------------------------------------------------
# Error path formatting
# --------------------------------------------------------------------------


def test_error_path_renders_nested_keys_and_indices(schema: Any) -> None:
    committers = [dict(VALID_INFO["committers"][0], nickname="ada")]
    document = dict(VALID_INFO, committers=committers)
    findings = core.validate_schema(document, schema)
    assert any(finding.startswith("committers[0]:") for finding in findings)


def test_root_level_error_reports_document_root() -> None:
    findings = core.validate_schema([], {"type": "object"})
    assert findings and findings[0].startswith("(document root):")


# --------------------------------------------------------------------------
# Repository check
# --------------------------------------------------------------------------


def test_exact_repository_match_passes() -> None:
    assert core.check_repositories(["example"], "example") == []


def test_ancestor_repository_is_permitted() -> None:
    """A subproject may carry an INFO.yaml naming its parent project."""
    assert core.check_repositories(["integration"], "integration/distribution") == []


def test_unrelated_repository_is_rejected() -> None:
    findings = core.check_repositories(["something-else"], "example")
    assert len(findings) == 1
    assert "something-else" in findings[0]


def test_partial_name_is_not_treated_as_an_ancestor() -> None:
    """'integ' is a string prefix of 'integration' but not a path ancestor."""
    findings = core.check_repositories(["integ"], "integration")
    assert len(findings) == 1


def test_descendant_repository_is_rejected() -> None:
    """The rule permits ancestors, not children."""
    findings = core.check_repositories(["integration/distribution"], "integration")
    assert len(findings) == 1


def test_every_offending_entry_is_reported() -> None:
    findings = core.check_repositories(["bad-one", "example", "bad-two"], "example")
    assert len(findings) == 2


def test_empty_repository_list_is_a_finding() -> None:
    """Silence here would let a file declaring nothing pass the check."""
    findings = core.check_repositories([], "example")
    assert len(findings) == 1
    assert "no repositories" in findings[0]


def test_extract_repositories_preserves_non_string_entries() -> None:
    """The schema constrains nothing here, so nothing else reports them."""
    entries, structural = core.extract_repositories({"repositories": ["example", 42]})
    assert entries == ["example", 42]
    assert structural == []


def test_non_string_entry_is_reported() -> None:
    """Discarding it would let repositories: ['example', 42] pass clean."""
    findings = core.check_repositories(["example", 42], "example")
    assert len(findings) == 1
    assert "not a string" in findings[0]


def test_non_string_entry_survives_the_bundled_schema(schema: Any) -> None:
    """Proves the repository check is the only thing that can catch it."""
    document = dict(VALID_INFO, repositories=["example", 42])
    assert core.validate_schema(document, schema) == []


def test_missing_repositories_key_is_a_structural_finding() -> None:
    entries, structural = core.extract_repositories({"project": "example"})
    assert entries == []
    assert len(structural) == 1
    assert "no 'repositories' key" in structural[0]


def test_repositories_of_the_wrong_type_is_a_structural_finding() -> None:
    entries, structural = core.extract_repositories({"repositories": "example"})
    assert entries == []
    assert "must be a list" in structural[0]


def test_non_mapping_document_is_a_structural_finding() -> None:
    entries, structural = core.extract_repositories([])
    assert entries == []
    assert "mapping" in structural[0]


@pytest.mark.parametrize(
    "document",
    [
        {"repositories": "example"},
        {"project": "example"},
        [],
    ],
)
def test_a_structural_problem_suppresses_the_entry_check(document: Any) -> None:
    """Running both would contradict the precise finding with a vague one.

    Extraction yields no entries for a malformed document, so the entry
    check would append "no repositories are declared" beneath a finding
    that just explained the key holds the wrong type.
    """
    entries, structural = core.extract_repositories(document)
    assert len(structural) == 1
    assert entries == []
    # The guard in main() picks one or the other, never both.
    assert "no repositories are declared" not in structural[0]


# --------------------------------------------------------------------------
# Project resolution
# --------------------------------------------------------------------------


def test_explicit_project_wins(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=other.git\n")
    assert core.resolve_project("explicit", tmp_path) == "explicit"


def test_project_read_from_gitreview(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text(
        textwrap.dedent(
            """\
            [gerrit]
            host=gerrit.example.org
            port=29418
            project=integration/distribution.git
            """
        )
    )
    assert core.resolve_project("", tmp_path) == "integration/distribution"


def test_gitreview_without_git_suffix_is_used_verbatim(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=example\n")
    assert core.resolve_project("", tmp_path) == "example"


def test_missing_gitreview_fails_loudly(tmp_path: Path) -> None:
    """Guessing would make the check pass against an unnamed project."""
    with pytest.raises(core.InfoYamlError, match="no .gitreview"):
        core.resolve_project("", tmp_path)


def test_gitreview_without_project_key_fails(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nhost=gerrit.example.org\n")
    with pytest.raises(core.InfoYamlError, match="no 'project' key"):
        core.resolve_project("", tmp_path)


def test_unparseable_gitreview_fails(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("this is not ini\n[[[\n")
    with pytest.raises(core.InfoYamlError):
        core.resolve_project("", tmp_path)


def test_percent_in_project_is_read_verbatim(tmp_path: Path) -> None:
    """ConfigParser interpolation would otherwise raise on a literal %."""
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=odd%name.git\n")
    assert core.resolve_project("", tmp_path) == "odd%name"


def test_interpolation_sequence_is_not_expanded(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text(
        "[gerrit]\nhost=example.org\nproject=%(host)s/thing.git\n"
    )
    assert core.resolve_project("", tmp_path) == "%(host)s/thing"


# --------------------------------------------------------------------------
# Path containment
# --------------------------------------------------------------------------


def test_info_path_defaults_to_repository_root(tmp_path: Path) -> None:
    assert core.resolve_info_path("", tmp_path) == (tmp_path / "INFO.yaml").resolve()


def test_info_path_escaping_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(core.InfoYamlError, match="outside the workspace"):
        core.resolve_info_path("../secret.yaml", workspace)


def test_schema_path_escaping_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(core.InfoYamlError, match="outside the workspace"):
        core.resolve_schema_path("../evil-schema.yaml", workspace)


def test_empty_schema_path_selects_the_bundled_schema(tmp_path: Path) -> None:
    assert core.resolve_schema_path("", tmp_path) == core.BUNDLED_SCHEMA


# --------------------------------------------------------------------------
# Symlink escapes
#
# Path.resolve() follows links, so containment catches these; the tests
# pin that, because a future switch to a non-resolving comparison would
# reopen every one of them.
# --------------------------------------------------------------------------


@pytest.fixture
def escape_target(tmp_path: Path) -> Path:
    """A file outside the workspace that a symlink could point at."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.yaml"
    secret.write_text("project: stolen\n")
    return secret


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def test_symlinked_info_path_cannot_escape(tmp_path: Path, escape_target: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "INFO.yaml").symlink_to(escape_target)
    with pytest.raises(core.InfoYamlError, match="outside the workspace"):
        core.resolve_info_path("", workspace)


def test_symlinked_schema_path_cannot_escape(
    tmp_path: Path, escape_target: Path
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "schema.yaml").symlink_to(escape_target)
    with pytest.raises(core.InfoYamlError, match="outside the workspace"):
        core.resolve_schema_path("schema.yaml", workspace)


def test_symlinked_gitreview_cannot_escape(tmp_path: Path, escape_target: Path) -> None:
    """No workflow input names .gitreview, but the repository owns it."""
    workspace = _workspace(tmp_path)
    (workspace / ".gitreview").symlink_to(escape_target)
    with pytest.raises(core.InfoYamlError, match="outside the workspace"):
        core.resolve_project("", workspace)


def test_symlink_within_the_workspace_still_works(tmp_path: Path) -> None:
    """Containment must reject escapes, not links as such."""
    workspace = _workspace(tmp_path)
    (workspace / "real.yaml").write_text("project: example\n")
    (workspace / "INFO.yaml").symlink_to(workspace / "real.yaml")
    assert core.resolve_info_path("", workspace) == (workspace / "real.yaml").resolve()


def test_bundled_schema_exists_and_parses() -> None:
    """The action ships the schema; a missing file breaks every consumer."""
    assert core.BUNDLED_SCHEMA.is_file()
    assert isinstance(
        yaml.safe_load(core.BUNDLED_SCHEMA.read_text(encoding="utf-8")), dict
    )


# --------------------------------------------------------------------------
# YAML loading
# --------------------------------------------------------------------------


def test_missing_file_reports_the_path(tmp_path: Path) -> None:
    with pytest.raises(core.InfoYamlError, match="not found"):
        core.load_yaml(tmp_path / "absent.yaml", label="INFO.yaml")


def test_malformed_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "INFO.yaml"
    path.write_text("project: [unclosed\n")
    with pytest.raises(core.InfoYamlError, match="not valid YAML"):
        core.load_yaml(path, label="INFO.yaml")


def test_non_utf8_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "INFO.yaml"
    path.write_bytes(b"project: \xff\xfe\n")
    with pytest.raises(core.InfoYamlError, match="not valid UTF-8"):
        core.load_yaml(path, label="INFO.yaml")


# --------------------------------------------------------------------------
# Result and boolean parsing
# --------------------------------------------------------------------------


def test_result_is_valid_only_when_both_checks_are_clean() -> None:
    assert report.Result(project="example").valid
    assert not report.Result(project="example", schema_errors=["x"]).valid
    assert not report.Result(project="example", repository_errors=["x"]).valid


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_truthy_boolean_inputs(value: str) -> None:
    assert iyv._as_bool(value, default=False, name="flag")


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_falsy_boolean_inputs(value: str) -> None:
    assert not iyv._as_bool(value, default=True, name="flag")


@pytest.mark.parametrize("value", ["enabled", "disabled", "nonsense", "2", "y"])
def test_unrecognised_boolean_input_is_rejected(value: str) -> None:
    """A mis-spelling must not quietly switch a gate off."""
    with pytest.raises(core.InfoYamlError, match="must be a boolean"):
        iyv._as_bool(value, default=True, name="require-repository-match")


def test_empty_boolean_input_takes_the_default() -> None:
    assert iyv._as_bool("", default=True, name="flag")
    assert not iyv._as_bool("   ", default=False, name="flag")


# --------------------------------------------------------------------------
# External schema references
# --------------------------------------------------------------------------


def test_external_https_reference_is_refused() -> None:
    """jsonschema 4.26 would otherwise fetch this over the network."""
    schema = {
        "type": "object",
        "properties": {"project": {"$ref": "https://example.org/schema.json"}},
    }
    with pytest.raises(core.InfoYamlError, match="external resource"):
        core.validate_schema({"project": "example"}, schema)


def test_external_file_reference_is_refused() -> None:
    """A file: ref would escape the workspace containment on schema-path."""
    schema = {
        "type": "object",
        "properties": {"project": {"$ref": "file:///etc/passwd"}},
    }
    with pytest.raises(core.InfoYamlError, match="external resource"):
        core.validate_schema({"project": "example"}, schema)


def test_internal_reference_still_resolves() -> None:
    """Refusing external refs must not break self-contained schemas."""
    schema = {
        "definitions": {"name": {"type": "string"}},
        "type": "object",
        "properties": {"project": {"$ref": "#/definitions/name"}},
    }
    assert core.validate_schema({"project": "example"}, schema) == []
    assert core.validate_schema({"project": 42}, schema) != []


# --------------------------------------------------------------------------
# Output and summary safety
# --------------------------------------------------------------------------


def test_outputs_use_a_random_delimiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository name must not be able to inject extra output keys."""
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    report.write_outputs(report.Result(project="example", repositories=["example"]))
    written = output.read_text()
    assert "valid=true" in written
    assert "project<<ghadelim_info_yaml_" in written
    assert "repositories<<ghadelim_info_yaml_" in written


def _parse_output_records(written: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT the way the runner does.

    Content inside a heredoc block belongs to that key's value, however
    much it looks like another record, so a naive substring search would
    call a contained injection attempt a success.
    """
    records: dict[str, str] = {}
    lines = written.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            records[key] = "\n".join(body)
        elif "=" in line:
            key, _, value = line.partition("=")
            records[key] = value
        index += 1
    return records


def test_project_cannot_inject_further_output_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .gitreview value may span lines; INI permits it."""
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    report.write_outputs(report.Result(project="evil\nvalid=true"))
    records = _parse_output_records(output.read_text())
    # The injected text stays inside the project value; 'valid' keeps the
    # verdict the reporter computed.
    assert records["project"] == "evil\nvalid=true"
    assert records["valid"] == "true"


def test_a_repository_name_cannot_forge_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = report.Result(
        project="example",
        repositories=["a\nvalid=true"],
        repository_errors=["forced failure"],
    )
    report.write_outputs(result)
    records = _parse_output_records(output.read_text())
    assert records["valid"] == "false"


def test_failure_outputs_publish_a_false_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """continue-on-error callers must read 'false', not an empty string."""
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    report.write_failure_outputs()
    written = output.read_text()
    assert "valid=false" in written
    assert "schema-error-count=0" in written


def test_outputs_are_skipped_when_the_env_var_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    report.write_outputs(report.Result(project="example"))


def test_summary_escapes_pipes_in_repository_names() -> None:
    result = report.Result(project="example", repositories=["odd|name"])
    assert "odd\\|name" in report.render_summary(result, "INFO.yaml")


def test_summary_flattens_line_breaks() -> None:
    """A newline would end the table row and let the rest render freely."""
    result = report.Result(project="example", repositories=["a\n## Injected"])
    rendered = report.render_summary(result, "INFO.yaml")
    assert "\n## Injected" not in rendered


def test_summary_strips_control_characters() -> None:
    assert "\x00" not in report.escape_table_cell("a\x00b")


def test_trailing_backslash_cannot_neutralise_the_pipe_escape() -> None:
    r"""'a\' + '|' must not emit 'a\\|', which GFM reads as a cell break."""
    assert report.escape_table_cell("a\\|b") == "a\\\\\\|b"
    assert report.escape_table_cell("a\\") == "a\\\\"


def test_summary_renders_findings_as_code_spans() -> None:
    """A finding quotes repository-controlled names; GFM must not apply."""
    result = report.Result(
        project="example", repository_errors=["![pwn](https://example.org/x.png)"]
    )
    rendered = report.render_summary(result, "INFO.yaml")
    assert "- `![pwn](https://example.org/x.png)`" in rendered


def test_summary_renders_schema_findings_as_code_spans() -> None:
    result = report.Result(project="example", schema_errors=["[link](https://x.test)"])
    assert "- `[link](https://x.test)`" in report.render_summary(result, "INFO.yaml")


def test_sanitise_collapses_crlf() -> None:
    assert report.sanitise("a\r\nb") == "a b"
    assert report.sanitise("a\rb") == "a b"


# --------------------------------------------------------------------------
# Console output cannot forge workflow commands
# --------------------------------------------------------------------------


def test_console_output_cannot_issue_a_workflow_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runner parses stdout; a newline plus '::' would issue a command.

    The runner recognises a command only at the start of a line, so the
    assertion is positional rather than a substring search: sanitised text
    may still read '::set-output' mid-line, where it is inert.
    """
    result = report.Result(
        project="example\n::error::forged",
        repositories=["a\n::set-output name=valid::true"],
        repository_errors=["finding\n::warning::forged"],
    )
    core.emit(result, "INFO.yaml")

    commands = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.lstrip().startswith("::")
    ]
    # Only the action's own annotations may open a command, and each of
    # those names the file it reports against.
    assert commands
    for line in commands:
        assert line.startswith("::error file="), line


def test_code_span_survives_embedded_backticks() -> None:
    assert report.code_span("a`b") == "``a`b``"
    assert report.code_span("`edge`") == "`` `edge` ``"


def test_annotation_payloads_are_escaped() -> None:
    assert report.escape_data("a\nb") == "a%0Ab"
    assert report.escape_property("a:b,c") == "a%3Ab%2Cc"


def test_summary_records_a_skipped_repository_check() -> None:
    """A green result must say what it did not check."""
    result = report.Result(project="", repository_checked=False)
    assert "_skipped_" in report.render_summary(result, "INFO.yaml")


# --------------------------------------------------------------------------
# YAML lint stage
# --------------------------------------------------------------------------

LINT_FAILURE = FIXTURES / "lint-failure" / "INFO.yaml"
# yamllint 'default' reports two colons errors in the fixture.
LINT_FAILURE_ERRORS = 2


def _run_main(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    info_text: str | None,
    **inputs: str,
) -> tuple[int, dict[str, str], str, str]:
    """Run the entry point against a scratch workspace.

    The workspace sits under tmp_path, so yamllint discovers no
    configuration unless a test writes one, and falls back to 'default'.
    ``info_text`` is written to INFO.yaml; ``None`` leaves whatever the
    test placed there. Returns the exit code, the parsed outputs, the
    summary and stdout.
    """
    if info_text is not None:
        (workspace / "INFO.yaml").write_text(info_text, encoding="utf-8")
    output = workspace.parent / "github-output"
    summary = workspace.parent / "github-summary"
    output.write_text("")
    summary.write_text("")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("INPUT_PATH", "INFO.yaml")
    monkeypatch.setenv("INPUT_PROJECT", "example-project")
    monkeypatch.delenv("INPUT_SCHEMA_PATH", raising=False)
    monkeypatch.delenv("INPUT_REQUIRE_REPOSITORY_MATCH", raising=False)
    monkeypatch.delenv("INPUT_LINT_YAML", raising=False)
    for key, value in inputs.items():
        monkeypatch.setenv(f"INPUT_{key.upper()}", value)
    code = iyv.main()
    records = _parse_output_records(output.read_text())
    return code, records, summary.read_text(), capsys.readouterr().out


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace directory beneath tmp_path."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_lint_findings_fail_a_schema_valid_file(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == str(LINT_FAILURE_ERRORS)
    assert records["schema-error-count"] == "0"
    assert "::error file=INFO.yaml,line=8,col=10" in out


def test_repository_yamllint_config_is_honoured(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """'relaxed' demotes the colons rule to a warning, which does not fail."""
    (workspace / ".yamllint").write_text("---\nextends: relaxed\n")
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert records["lint-error-count"] == "0"
    assert "::warning file=INFO.yaml,line=8,col=10" in out
    assert "`.yamllint`" in summary


@pytest.mark.parametrize("name", [".yamllint.yaml", ".yamllint.yml"])
def test_alternative_yamllint_config_names_are_found(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    (workspace / name).write_text("---\nextends: relaxed\n")
    code, records, _, _ = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["lint-error-count"] == "0"


def test_disabled_lint_stage_is_skipped_and_recorded(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, records, summary, out = _run_main(
        workspace,
        monkeypatch,
        capsys,
        LINT_FAILURE.read_text(encoding="utf-8"),
        lint_yaml="false",
    )
    assert code == 0
    assert records["valid"] == "true"
    assert records["lint-error-count"] == "0"
    assert "| YAML lint | _skipped_ |" in summary
    assert "YAML lint skipped by request" in out


def test_unrecognised_lint_yaml_value_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, records, _, out = _run_main(
        workspace,
        monkeypatch,
        capsys,
        LINT_FAILURE.read_text(encoding="utf-8"),
        lint_yaml="enabled",
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "lint-yaml" in out


def test_lint_warnings_are_reported_but_do_not_fail(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dropping the '---' leaves a single document-start warning."""
    text = (FIXTURES / "valid" / "INFO.yaml").read_text(encoding="utf-8")
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, text.removeprefix("---\n")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert records["lint-error-count"] == "0"
    assert "::warning file=INFO.yaml,line=5,col=1" in out
    assert "document-start" in summary


def test_unparsable_file_reports_the_lint_finding(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Lint runs first, so a syntax error arrives as a located finding."""
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, "---\nproject: [unclosed\n"
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == "1"
    assert "::error file=INFO.yaml,line=" in out
    assert "syntax error" in out
    assert "is not valid YAML" not in out
    assert "_not run: the file is not valid YAML_" in summary


def test_symlinked_yamllint_config_cannot_escape(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    escape_target: Path,
) -> None:
    (workspace / ".yamllint").symlink_to(escape_target)
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "outside the workspace" in out


@pytest.mark.parametrize(
    "config",
    [
        "---\nextends: {target}\n",
        "---\nextends: default\nignore-from-file: {target}\n",
        "---\nextends: default\nrules:\n  colons:\n    ignore-from-file: {target}\n",
    ],
)
def test_yamllint_config_cannot_read_outside_the_workspace(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    escape_target: Path,
    config: str,
) -> None:
    """yamllint reads 'extends' and 'ignore-from-file' paths verbatim."""
    (workspace / ".yamllint").write_text(config.format(target=escape_target))
    code, _, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert "outside the workspace" in out


def test_yamllint_config_may_extend_a_file_in_the_workspace(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace / "lint").mkdir()
    (workspace / "lint" / "base.yaml").write_text("---\nextends: relaxed\n")
    (workspace / ".yamllint").write_text("---\nextends: lint/base.yaml\n")
    code, records, _, _ = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["lint-error-count"] == "0"


def test_invalid_yamllint_config_fails_loudly(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace / ".yamllint").write_text("---\nrules:\n  no-such-rule: enable\n")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "yamllint rejected its configuration" in out
    assert 'no such rule: "no-such-rule"' in out


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "via-extends"])
def test_non_utf8_yamllint_config_is_refused(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    escape_target: Path,
    indirect: bool,
) -> None:
    """yamllint decodes other encodings itself and would follow unchecked paths.

    The containment preflight reads UTF-8. A UTF-16 configuration naming
    an external 'extends' used to slip past it, because the preflight gave
    up on the decode error while yamllint went on to load the file. It
    must fail closed instead, whether the encoded file is the
    configuration itself or one it extends.
    """
    encoded = f"---\nextends: {escape_target}\n".encode("utf-16")
    if indirect:
        (workspace / "lint").mkdir()
        (workspace / "lint" / "base.yaml").write_bytes(encoded)
        (workspace / ".yamllint").write_text("---\nextends: lint/base.yaml\n")
    else:
        (workspace / ".yamllint").write_bytes(encoded)
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "is not UTF-8" in out
    assert str(escape_target) not in out


@pytest.mark.parametrize("value", ["null", "false", "[default]", "{a: b}"])
def test_non_string_extends_is_reported_not_raised(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    """yamllint raises TypeError on a non-string 'extends'; that must not escape."""
    (workspace / ".yamllint").write_text(f"---\nextends: {value}\n")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "'extends' value" in out
    assert "Traceback" not in out


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("strict", "could not be read", id="unknown-bare-name"),
        pytest.param("..", "outside the workspace", id="parent-directory"),
    ],
)
def test_extends_outside_the_bundled_set_is_a_workspace_path(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    expected: str,
) -> None:
    """Only 'default' and 'relaxed' are bundled; any other name is a path.

    The bundled set is fixed in this code rather than read from the
    installed yamllint, so a name it does not know falls to the path
    branch and is contained in the workspace: fail closed, never a read
    of whatever yamllint's lookup might find.
    """
    (workspace / ".yamllint").write_text(f"---\nextends: {name}\n")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert expected in out


# yamllint's bundled-configuration lookup joins '<name>.yaml' onto its conf
# directory with os.path.join semantics. The containment preflight must
# refuse any name whose lookup leaves that directory, and pathlib's pure
# Windows paths let the Windows semantics run on any host. The stand-in
# directory has no drive, so a drive-relative name counts as leaving.
@pytest.mark.parametrize(
    ("name", "leaves"),
    [
        pytest.param("default", False, id="bare-name"),
        pytest.param("..", False, id="dots-become-a-filename"),
        pytest.param("C:relative", True, id="drive-relative-unknown-drive"),
        pytest.param(r"C:\ws\base", True, id="absolute-drive-path"),
        pytest.param("D:relative", True, id="other-drive"),
        pytest.param(r"..\outside", True, id="backslash-traversal"),
        pytest.param(r"\\server\share\x", True, id="unc-path"),
        pytest.param(r"sub\x", True, id="backslash-subdirectory"),
    ],
)
def test_bundled_lookup_escape_detection_on_windows(name: str, leaves: bool) -> None:
    conf = PureWindowsPath("yamllint", "conf")
    assert lint._leaves_bundled_dir(name, conf) is leaves


def test_bundled_lookup_cannot_leave_the_conf_dir_on_posix() -> None:
    """Without a '/' a POSIX name is one component; the slash test covers the rest."""
    conf = PurePosixPath("yamllint", "conf")
    for name in ("default", "..", r"C:\ws\base", r"..\outside", "C:relative"):
        assert lint._leaves_bundled_dir(name, conf) is False


def test_extends_that_escapes_the_bundled_lookup_is_refused(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    escape_target: Path,
) -> None:
    """An 'extends' yamllint would resolve outside its conf directory fails closed.

    With 'extends: C:\\ws\\base' on Windows, the preflight would contain
    and inspect 'base' while yamllint's lookup opened the sibling
    'base.yaml', so a benign 'base' beside a 'base.yaml' that reaches
    outside the workspace would pass the preflight and run unchecked.
    The escape itself needs Windows path semantics, so the detector is
    stood in for here; what this pins is the integrated outcome: the run
    fails with the failure outputs written, before yamllint loads
    anything, and the sibling is never read.
    """
    monkeypatch.setattr(lint, "_leaves_bundled_dir", lambda name, *a: True)
    (workspace / "base").write_text("---\nextends: relaxed\n")
    (workspace / "base.yaml").write_text(f"---\nextends: {escape_target}\n")
    (workspace / ".yamllint").write_text("---\nextends: base\n")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "neither a bundled configuration name nor a path" in out
    assert str(escape_target) not in out


def test_warning_carrying_the_syntax_suffix_does_not_skip_the_checks(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Document text in yamllint's output must not pass for a parse verdict.

    key-duplicates prints the duplicated key, and a key holding a newline
    splits that record so its first line ends in '(syntax)'. With the
    rule demoted to a warning by the repository's own configuration, the
    report passes the exit-status check with no errors; taking the line
    for a syntax error would switch off the schema and repository checks
    and report the file valid. yamllint reports syntax errors at error
    level alone, so a warning never stands for one.
    """
    (workspace / ".yamllint").write_text(
        "---\nextends: default\nrules:\n  key-duplicates:\n    level: warning\n"
    )
    key = '"a (syntax)\\nINFO.yaml:1:1: [warning] b"'
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, f"---\n{key}: 1\n{key}: 2\n"
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == "0"
    assert records["schema-error-count"] != "0"
    assert "_not run: the file is not valid YAML_" not in summary
    assert "::warning file=INFO.yaml,line=3,col=1" in out


def test_multiline_warning_is_one_finding_and_does_not_fail(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A duplicated key holding a newline is printed decoded, across two lines.

    The record is one warning, so a schema-valid document with that
    duplication, and key-duplicates demoted to a warning, must pass
    rather than be refused as unreadable output.
    """
    (workspace / ".yamllint").write_text(
        "---\nextends: default\nrules:\n  key-duplicates:\n    level: warning\n"
    )
    text = (FIXTURES / "valid" / "INFO.yaml").read_text(encoding="utf-8")
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, text + '"a\\nb": 1\n"a\\nb": 2\n'
    )
    assert code == 0
    assert records["valid"] == "true"
    assert records["lint-error-count"] == "0"
    assert out.count("::warning ") == 1
    assert "key-duplicates" in summary


def test_parse_report_joins_lines_until_a_rule_closes_the_record() -> None:
    findings, unparsed = lint._parse_report(
        'a.yaml:3:1: [warning] duplication of key "a\nb" in mapping (key-duplicates)\n'
        "a.yaml:4:1: [error] too many spaces after colon (colons)\n"
    )
    assert [(f.line, f.level, f.rule) for f in findings] == [
        (3, "warning", "key-duplicates"),
        (4, "error", "colons"),
    ]
    assert findings[0].message == 'duplication of key "a\nb" in mapping'
    assert unparsed is None
    assert lint._parse_report("a.yaml:1:1: [warning] never closes\n")[1]


@pytest.mark.parametrize(
    "config",
    [
        pytest.param('---\nextends: "\\0"\n', id="extends"),
        pytest.param(
            '---\nextends: default\nignore-from-file: "\\0"\n', id="ignore-from-file"
        ),
        pytest.param(
            '---\nextends: default\nrules:\n  colons:\n    ignore-from-file: "\\0"\n',
            id="rule-ignore-from-file",
        ),
    ],
)
def test_nul_byte_in_a_config_path_is_reported_not_raised(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: str,
) -> None:
    """The preflight hands decoded YAML strings to the resolver.

    A NUL byte spelt as an escape raises ValueError from ``resolve()``
    before containment can judge it; that must surface as a reported
    failure with the outputs written, not a traceback.
    """
    (workspace / ".yamllint").write_text(config)
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "cannot be resolved" in out
    assert "Traceback" not in out


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(".yamllint", id="lint-config"),
        pytest.param("INFO.yaml", id="path"),
    ],
)
def test_symlink_loop_is_reported_not_raised(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """A self-referential link must fail the run with the outputs written.

    ``Path.resolve()`` raises RuntimeError for a loop on Python 3.10 to
    3.12 and returns the link unresolved on 3.13 and later; either way
    the run used to continue past containment with a traceback, or with
    the configuration quietly treated as absent.
    """
    if name != "INFO.yaml":
        (workspace / "INFO.yaml").write_text(LINT_FAILURE.read_text(encoding="utf-8"))
    (workspace / name).symlink_to(name)
    code, records, _, out = _run_main(workspace, monkeypatch, capsys, None)
    assert code == 1
    assert records["valid"] == "false"
    assert "cannot be resolved" in out
    assert "symbolic link" in out or "Symlink loop" in out


@pytest.mark.parametrize(
    ("config", "expect_error"),
    [
        pytest.param("", True, id="default-unix-rule-flags-crlf"),
        pytest.param(
            "---\nextends: default\nrules:\n  new-lines:\n    type: dos\n",
            False,
            id="dos-rule-accepts-crlf",
        ),
    ],
)
def test_crlf_line_endings_reach_yamllint_unchanged(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: str,
    expect_error: bool,
) -> None:
    """yamllint reads the file itself, so CRLF endings reach 'new-lines' as written."""
    if config:
        (workspace / ".yamllint").write_text(config)
    text = (FIXTURES / "valid" / "INFO.yaml").read_text(encoding="utf-8")
    crlf = text.replace("\n", "\r\n")
    # _run_main writes with text-mode translation, so write the bytes here
    # and hand it text it will rewrite identically.
    (workspace / "INFO.yaml").write_bytes(crlf.encode("utf-8"))
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: None)
    code, records, _, out = _run_main(workspace, monkeypatch, capsys, crlf)
    assert (workspace / "INFO.yaml").read_bytes() == crlf.encode("utf-8")
    if expect_error:
        assert code == 1
        assert records["lint-error-count"] == "1"
        assert "new-lines" in out
    else:
        assert code == 0
        assert records["lint-error-count"] == "0"
        assert records["schema-error-count"] == "0"


def test_syntax_error_survives_a_missing_project_source(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lint verdict must not be lost to a later prerequisite failing.

    With no 'project' input and no .gitreview, project resolution raises.
    That used to run before the syntax-error guard, so the run reported
    the missing project and published 'lint-error-count=0' for a file
    that does not parse.
    """
    code, records, summary, out = _run_main(
        workspace, monkeypatch, capsys, "---\nproject: [unclosed\n", project=""
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == "1"
    assert "::error file=INFO.yaml,line=" in out
    assert "no .gitreview file was found" not in out
    assert "_not run: the file is not valid YAML_" in summary


def test_syntax_error_survives_an_unreadable_schema(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace / "schema.yaml").write_text("---\n: [not a schema\n")
    code, records, _, out = _run_main(
        workspace,
        monkeypatch,
        capsys,
        "---\nproject: [unclosed\n",
        schema_path="schema.yaml",
    )
    assert code == 1
    assert records["lint-error-count"] == "1"
    assert "::error file=INFO.yaml,line=" in out
    assert "Schema" not in out


def test_document_the_yaml_reader_refuses_is_reported(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A NUL byte makes yamllint raise rather than report; the run must say so."""
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, "---\nproject: 'a\x00b'\n"
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == "0"
    assert "yamllint failed on 'INFO.yaml'" in out
    assert "unacceptable character" in out


def test_unparsable_rule_regex_in_config_is_reported(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """yamllint compiles 'ignored-keys' when the rule first runs, not on load."""
    (workspace / ".yamllint").write_text(
        "---\nextends: default\nrules:\n  key-ordering:\n    ignored-keys: ['[']\n"
    )
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "yamllint failed on 'INFO.yaml'" in out
    assert "unterminated character set" in out


KEY_ORDER_DOC = "---\nhair: 1\nhaïr: 2\nhais: 3\n"
KEY_ORDER_CONFIG = "---\nextends: default\nrules:\n  key-ordering: enable\n"


def _has_locale(name: str) -> bool:
    import locale

    previous = locale.setlocale(locale.LC_COLLATE)
    try:
        locale.setlocale(locale.LC_COLLATE, name)
    except locale.Error:
        return False
    finally:
        locale.setlocale(locale.LC_COLLATE, previous)
    return True


@pytest.mark.skipif(
    not _has_locale("en_US.UTF-8"), reason="en_US.UTF-8 locale not installed"
)
@pytest.mark.parametrize(
    ("locale_line", "errors"),
    [
        pytest.param("", "1", id="c-collation"),
        pytest.param("locale: en_US.UTF-8\n", "0", id="configured-locale"),
    ],
)
def test_configured_locale_drives_key_ordering(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    locale_line: str,
    errors: str,
) -> None:
    """The yamllint command applies the configuration's 'locale' itself.

    Under C collation 'haïr' sorts after 'hais' and key-ordering fires;
    under en_US.UTF-8 the three keys are in order. Running the command
    rather than the library is what makes the setting take effect.
    """
    (workspace / ".yamllint").write_text(KEY_ORDER_CONFIG + locale_line)
    _, records, _, _ = _run_main(workspace, monkeypatch, capsys, KEY_ORDER_DOC)
    assert records["lint-error-count"] == errors


def test_unsupported_locale_is_reported(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace / ".yamllint").write_text(
        "---\nextends: default\nlocale: xx_NOPE.UTF-8\n"
    )
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "yamllint failed on 'INFO.yaml'" in out
    assert "unsupported locale setting" in out


def test_missing_yamllint_command_is_reported(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command is installed by the action, not imported; its absence is an error."""
    empty = workspace.parent / "empty"
    empty.mkdir()
    monkeypatch.delenv("YAMLLINT_ENV_DIR", raising=False)
    monkeypatch.setattr(lint, "_interpreter_scripts_dir", lambda: empty)
    monkeypatch.setenv("PATH", str(empty))
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "'yamllint' is not on PATH" in out


def _fake_yamllint(directory: Path, marker: str) -> Path:
    """An executable that reports one warning naming where it came from."""
    script = directory / "yamllint"
    script.write_text(
        f"#!/bin/sh\nprintf 'INFO.yaml:1:1: [warning] {marker} (truthy)\\n'\n"
    )
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="shell-script stand-ins")
@pytest.mark.parametrize("sub", ["bin", "Scripts"])
def test_env_dir_executable_wins_over_the_checkout_and_path(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sub: str,
) -> None:
    """With YAMLLINT_ENV_DIR set, nothing on PATH or in the checkout is consulted.

    The validate step runs from the checkout under test. On Windows
    ``shutil.which`` searches the current directory, so a repository
    shipping a ``yamllint.exe`` could replace the pinned tool; the fix
    is to take the executable from the synced environment by absolute
    path, so the same test holds on every platform with a PATH entry
    standing in for the current-directory search. The environment
    keeps its executables under ``bin`` on POSIX and ``Scripts`` on
    Windows; both layouts are accepted.
    """
    env_dir = tmp_path / "lint"
    (env_dir / sub).mkdir(parents=True)
    _fake_yamllint(env_dir / sub, "from-env-dir")
    _fake_yamllint(workspace, "from-checkout")
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("YAMLLINT_ENV_DIR", str(env_dir))
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert "from-env-dir" in out
    assert "from-checkout" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="shell-script stand-ins")
def test_interpreter_executable_wins_over_the_checkout_and_path(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without YAMLLINT_ENV_DIR, the validator's own environment comes first.

    A pre-commit hook installs yamllint beside the validator and leaves
    YAMLLINT_ENV_DIR unset. The executable in the interpreter's scripts
    directory must win over one in the checkout, with a PATH entry
    standing in for the Windows current-directory search.
    """
    scripts = tmp_path / "hook-env" / "bin"
    scripts.mkdir(parents=True)
    _fake_yamllint(scripts, "from-hook-env")
    _fake_yamllint(workspace, "from-checkout")
    monkeypatch.delenv("YAMLLINT_ENV_DIR", raising=False)
    monkeypatch.setattr(lint, "_interpreter_scripts_dir", lambda: scripts)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{os.environ['PATH']}")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert "from-hook-env" in out
    assert "from-checkout" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="shell-script stand-ins")
def test_path_search_skips_the_current_directory(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The PATH fallback never runs an executable from the checkout.

    Neither a relative entry nor an absolute one naming the current
    directory, which is the checkout under test, is searched.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    _fake_yamllint(installed, "from-path")
    _fake_yamllint(workspace, "from-checkout")
    monkeypatch.delenv("YAMLLINT_ENV_DIR", raising=False)
    monkeypatch.setattr(lint, "_interpreter_scripts_dir", lambda: empty)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([".", "", str(workspace), str(installed)])
    )
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert "from-path" in out
    assert "from-checkout" not in out


def test_action_trims_its_path_and_schema_inputs(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The action input is trimmed; the shared resolver takes paths literally."""
    code, records, _, _ = _run_main(
        workspace,
        monkeypatch,
        capsys,
        LINT_FAILURE.read_text(encoding="utf-8"),
        path="  INFO.yaml  ",
        schema_path=" ",
        lint_yaml="false",
    )
    assert code == 0
    assert records["valid"] == "true"


def test_empty_env_dir_does_not_fall_back_to_path(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_dir = tmp_path / "lint"
    (env_dir / "bin").mkdir(parents=True)
    monkeypatch.setenv("YAMLLINT_ENV_DIR", str(env_dir))
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert "'yamllint' was not found in YAMLLINT_ENV_DIR" in out


def test_file_encoding_override_cannot_redirect_the_preflight(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    escape_target: Path,
) -> None:
    """yamllint must decode the configuration the way the preflight did.

    yamllint honours ``YAMLLINT_FILE_ENCODING`` for every file it opens.
    Under ``unicode_escape`` an 'extends' written as ``\\x2ftmp\\x2f...``
    names a benign workspace file to the preflight, which reads UTF-8,
    and a file outside the workspace to yamllint. The child must not
    inherit the variable.
    """
    escape_target.write_text("---\nrules:\n  no-such-rule: enable\n")
    escaped = str(escape_target).replace("/", "\\x2f")
    (workspace / escaped).write_text("---\nextends: relaxed\n")
    (workspace / ".yamllint").write_text(f"---\nextends: {escaped}\n")
    monkeypatch.setenv("YAMLLINT_FILE_ENCODING", "unicode_escape")
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 0
    assert records["valid"] == "true"
    assert "no-such-rule" not in out


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["yamllint"], returncode, stdout, stderr)


def test_crash_after_a_partial_report_is_not_a_verdict(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """yamllint prints findings as it goes, so a crash can follow some of them.

    The exit status then matches what an error-level finding produces,
    and the partial report would pass for a complete one. The traceback
    on stderr is what tells the two apart.
    """
    partial = _completed(
        1,
        stdout="INFO.yaml:8:10: [error] too many spaces after colon (colons)\n",
        stderr="Traceback (most recent call last):\n  ...\nre.error: boom\n",
    )
    monkeypatch.setattr(lint.subprocess, "run", lambda *a, **k: partial)
    code, records, _, out = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"
    assert records["lint-error-count"] == "0"
    assert "yamllint failed on 'INFO.yaml': re.error: boom" in out


@pytest.mark.parametrize(
    "completed",
    [
        pytest.param(_completed(0, stdout="not a finding\n"), id="unreadable-line"),
        pytest.param(_completed(1), id="error-status-without-errors"),
        pytest.param(
            _completed(0, stdout="INFO.yaml:1:1: [error] x (colons)\n"),
            id="clean-status-with-errors",
        ),
        pytest.param(_completed(2), id="unexpected-status"),
    ],
)
def test_report_and_status_must_agree(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Output the action cannot account for fails the run rather than a guess."""
    monkeypatch.setattr(lint.subprocess, "run", lambda *a, **k: completed)
    code, records, _, _ = _run_main(
        workspace, monkeypatch, capsys, LINT_FAILURE.read_text(encoding="utf-8")
    )
    assert code == 1
    assert records["valid"] == "false"


def test_syntax_finding_keeps_yamllint_wording_without_a_rule() -> None:
    """'(syntax)' is part of yamllint's description, not a rule identifier."""
    finding = lint._parse_finding(
        "INFO.yaml:2:7: [error] syntax error: expected ',' or ']' (syntax)"
    )
    assert finding == report.LintFinding(
        line=2,
        column=7,
        level="error",
        rule=None,
        message="syntax error: expected ',' or ']' (syntax)",
    )
    ruled = lint._parse_finding(
        "INFO.yaml:2:81: [error] line too long (98 > 80 characters) (line-length)"
    )
    assert ruled is not None
    assert (ruled.rule, ruled.message) == (
        "line-length",
        "line too long (98 > 80 characters)",
    )


def test_lint_errors_invalidate_the_result() -> None:
    error = report.LintFinding(
        line=1, column=1, level="error", rule="colons", message="x"
    )
    warning = report.LintFinding(
        line=1, column=1, level="warning", rule="truthy", message="x"
    )
    assert not report.Result(project="example", lint_findings=[error]).valid
    assert report.Result(project="example", lint_findings=[warning]).valid


def test_failure_outputs_publish_a_zero_lint_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    report.write_failure_outputs()
    assert "lint-error-count=0" in output.read_text()


def test_summary_renders_lint_findings_as_code_spans() -> None:
    finding = report.LintFinding(
        line=3, column=5, level="error", rule=None, message="[x](https://x.test)"
    )
    result = report.Result(project="example", lint_findings=[finding])
    rendered = report.render_summary(result, "INFO.yaml")
    assert "### YAML lint findings" in rendered
    assert "`3:5 [error] [x](https://x.test)`" in rendered


def test_lint_annotation_cannot_issue_a_workflow_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = report.LintFinding(
        line=1, column=1, level="error", rule="x", message="a\n::warning::forged"
    )
    core.emit(report.Result(project="example", lint_findings=[finding]), "INFO.yaml")
    commands = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.lstrip().startswith("::")
    ]
    assert commands
    for line in commands:
        assert line.startswith("::error file=INFO.yaml,line=1,col=1"), line
