# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the INFO.yaml validator."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

import info_yaml_validate as iyv
from info_yaml import report

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
    return yaml.safe_load(iyv.BUNDLED_SCHEMA.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_valid_document_produces_no_findings(schema: Any) -> None:
    assert iyv.validate_schema(VALID_INFO, schema) == []


def test_missing_required_key_is_reported(schema: Any) -> None:
    document = {key: value for key, value in VALID_INFO.items() if key != "committers"}
    findings = iyv.validate_schema(document, schema)
    assert any("committers" in finding for finding in findings)


def test_all_findings_are_collected_not_just_the_first(schema: Any) -> None:
    """A caller fixing one error per run turns one correction into many."""
    document = {
        key: value
        for key, value in VALID_INFO.items()
        if key not in {"committers", "tsc", "repositories"}
    }
    assert len(iyv.validate_schema(document, schema)) >= 3


def test_invalid_lifecycle_state_is_reported(schema: Any) -> None:
    document = dict(VALID_INFO, lifecycle_state="Flourishing")
    findings = iyv.validate_schema(document, schema)
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
        assert iyv.validate_schema(document, schema) == [], state


def test_unknown_key_in_user_object_is_reported(schema: Any) -> None:
    """The user object sets additionalProperties: false."""
    lead = dict(VALID_INFO["project_lead"], nickname="ada")
    document = dict(VALID_INFO, project_lead=lead)
    findings = iyv.validate_schema(document, schema)
    assert any("project_lead" in finding for finding in findings)


def test_unknown_type_in_schema_is_reported_against_the_schema() -> None:
    """A broken caller schema must not read as a document finding."""
    with pytest.raises(iyv.InfoYamlError, match="unknown type"):
        iyv.validate_schema(VALID_INFO, {"type": "not-a-real-type"})


def test_unparsable_pattern_is_reported_against_the_schema() -> None:
    """'pattern' reaches re.search directly; re.error is not a TypeError."""
    schema = {"type": "object", "properties": {"project": {"pattern": "["}}}
    with pytest.raises(iyv.InfoYamlError, match="could not be applied"):
        iyv.validate_schema({"project": "example"}, schema)


def test_unparsable_pattern_properties_is_reported() -> None:
    schema = {"type": "object", "patternProperties": {"(": {"type": "string"}}}
    with pytest.raises(iyv.InfoYamlError, match="could not be applied"):
        iyv.validate_schema({"project": "example"}, schema)


def test_bundled_schema_is_not_strictly_draft4_valid(schema: Any) -> None:
    """Guards the reason validate_schema does not call check_schema.

    The canonical schema carries Draft 3 spellings. If upstream ever
    corrects them this test fails, at which point check_schema can be
    reinstated and this test deleted.
    """
    with pytest.raises(jsonschema.SchemaError):
        jsonschema.Draft4Validator.check_schema(schema)


def test_bundled_schema_still_validates_a_good_document(schema: Any) -> None:
    """Non-conformance must stay harmless in practice, not just in theory."""
    assert iyv.validate_schema(VALID_INFO, schema) == []


def test_findings_are_ordered_deterministically(schema: Any) -> None:
    """Unstable ordering makes a diff between two runs unreadable."""
    document = {
        key: value
        for key, value in VALID_INFO.items()
        if key not in {"committers", "tsc"}
    }
    assert iyv.validate_schema(document, schema) == iyv.validate_schema(
        document, schema
    )


# --------------------------------------------------------------------------
# Error path formatting
# --------------------------------------------------------------------------


def test_error_path_renders_nested_keys_and_indices(schema: Any) -> None:
    committers = [dict(VALID_INFO["committers"][0], nickname="ada")]
    document = dict(VALID_INFO, committers=committers)
    findings = iyv.validate_schema(document, schema)
    assert any(finding.startswith("committers[0]:") for finding in findings)


def test_root_level_error_reports_document_root() -> None:
    findings = iyv.validate_schema([], {"type": "object"})
    assert findings and findings[0].startswith("(document root):")


# --------------------------------------------------------------------------
# Repository check
# --------------------------------------------------------------------------


def test_exact_repository_match_passes() -> None:
    assert iyv.check_repositories(["example"], "example") == []


def test_ancestor_repository_is_permitted() -> None:
    """A subproject may carry an INFO.yaml naming its parent project."""
    assert iyv.check_repositories(["integration"], "integration/distribution") == []


def test_unrelated_repository_is_rejected() -> None:
    findings = iyv.check_repositories(["something-else"], "example")
    assert len(findings) == 1
    assert "something-else" in findings[0]


def test_partial_name_is_not_treated_as_an_ancestor() -> None:
    """'integ' is a string prefix of 'integration' but not a path ancestor."""
    findings = iyv.check_repositories(["integ"], "integration")
    assert len(findings) == 1


def test_descendant_repository_is_rejected() -> None:
    """The rule permits ancestors, not children."""
    findings = iyv.check_repositories(["integration/distribution"], "integration")
    assert len(findings) == 1


def test_every_offending_entry_is_reported() -> None:
    findings = iyv.check_repositories(["bad-one", "example", "bad-two"], "example")
    assert len(findings) == 2


def test_empty_repository_list_is_a_finding() -> None:
    """Silence here would let a file declaring nothing pass the check."""
    findings = iyv.check_repositories([], "example")
    assert len(findings) == 1
    assert "no repositories" in findings[0]


def test_extract_repositories_preserves_non_string_entries() -> None:
    """The schema constrains nothing here, so nothing else reports them."""
    entries, structural = iyv.extract_repositories({"repositories": ["example", 42]})
    assert entries == ["example", 42]
    assert structural == []


def test_non_string_entry_is_reported() -> None:
    """Discarding it would let repositories: ['example', 42] pass clean."""
    findings = iyv.check_repositories(["example", 42], "example")
    assert len(findings) == 1
    assert "not a string" in findings[0]


def test_non_string_entry_survives_the_bundled_schema(schema: Any) -> None:
    """Proves the repository check is the only thing that can catch it."""
    document = dict(VALID_INFO, repositories=["example", 42])
    assert iyv.validate_schema(document, schema) == []


def test_missing_repositories_key_is_a_structural_finding() -> None:
    entries, structural = iyv.extract_repositories({"project": "example"})
    assert entries == []
    assert len(structural) == 1
    assert "no 'repositories' key" in structural[0]


def test_repositories_of_the_wrong_type_is_a_structural_finding() -> None:
    entries, structural = iyv.extract_repositories({"repositories": "example"})
    assert entries == []
    assert "must be a list" in structural[0]


def test_non_mapping_document_is_a_structural_finding() -> None:
    entries, structural = iyv.extract_repositories([])
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
    entries, structural = iyv.extract_repositories(document)
    assert len(structural) == 1
    assert entries == []
    # The guard in main() picks one or the other, never both.
    assert "no repositories are declared" not in structural[0]


# --------------------------------------------------------------------------
# Project resolution
# --------------------------------------------------------------------------


def test_explicit_project_wins(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=other.git\n")
    assert iyv.resolve_project("explicit", tmp_path) == "explicit"


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
    assert iyv.resolve_project("", tmp_path) == "integration/distribution"


def test_gitreview_without_git_suffix_is_used_verbatim(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=example\n")
    assert iyv.resolve_project("", tmp_path) == "example"


def test_missing_gitreview_fails_loudly(tmp_path: Path) -> None:
    """Guessing would make the check pass against an unnamed project."""
    with pytest.raises(iyv.InfoYamlError, match="no .gitreview"):
        iyv.resolve_project("", tmp_path)


def test_gitreview_without_project_key_fails(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("[gerrit]\nhost=gerrit.example.org\n")
    with pytest.raises(iyv.InfoYamlError, match="no 'project' key"):
        iyv.resolve_project("", tmp_path)


def test_unparseable_gitreview_fails(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text("this is not ini\n[[[\n")
    with pytest.raises(iyv.InfoYamlError):
        iyv.resolve_project("", tmp_path)


def test_percent_in_project_is_read_verbatim(tmp_path: Path) -> None:
    """ConfigParser interpolation would otherwise raise on a literal %."""
    (tmp_path / ".gitreview").write_text("[gerrit]\nproject=odd%name.git\n")
    assert iyv.resolve_project("", tmp_path) == "odd%name"


def test_interpolation_sequence_is_not_expanded(tmp_path: Path) -> None:
    (tmp_path / ".gitreview").write_text(
        "[gerrit]\nhost=example.org\nproject=%(host)s/thing.git\n"
    )
    assert iyv.resolve_project("", tmp_path) == "%(host)s/thing"


# --------------------------------------------------------------------------
# Path containment
# --------------------------------------------------------------------------


def test_info_path_defaults_to_repository_root(tmp_path: Path) -> None:
    assert iyv.resolve_info_path("", tmp_path) == (tmp_path / "INFO.yaml").resolve()


def test_info_path_escaping_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(iyv.InfoYamlError, match="outside the workspace"):
        iyv.resolve_info_path("../secret.yaml", workspace)


def test_schema_path_escaping_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(iyv.InfoYamlError, match="outside the workspace"):
        iyv.resolve_schema_path("../evil-schema.yaml", workspace)


def test_empty_schema_path_selects_the_bundled_schema(tmp_path: Path) -> None:
    assert iyv.resolve_schema_path("", tmp_path) == iyv.BUNDLED_SCHEMA


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
    with pytest.raises(iyv.InfoYamlError, match="outside the workspace"):
        iyv.resolve_info_path("", workspace)


def test_symlinked_schema_path_cannot_escape(
    tmp_path: Path, escape_target: Path
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "schema.yaml").symlink_to(escape_target)
    with pytest.raises(iyv.InfoYamlError, match="outside the workspace"):
        iyv.resolve_schema_path("schema.yaml", workspace)


def test_symlinked_gitreview_cannot_escape(tmp_path: Path, escape_target: Path) -> None:
    """No workflow input names .gitreview, but the repository owns it."""
    workspace = _workspace(tmp_path)
    (workspace / ".gitreview").symlink_to(escape_target)
    with pytest.raises(iyv.InfoYamlError, match="outside the workspace"):
        iyv.resolve_project("", workspace)


def test_symlink_within_the_workspace_still_works(tmp_path: Path) -> None:
    """Containment must reject escapes, not links as such."""
    workspace = _workspace(tmp_path)
    (workspace / "real.yaml").write_text("project: example\n")
    (workspace / "INFO.yaml").symlink_to(workspace / "real.yaml")
    assert iyv.resolve_info_path("", workspace) == (workspace / "real.yaml").resolve()


def test_bundled_schema_exists_and_parses() -> None:
    """The action ships the schema; a missing file breaks every consumer."""
    assert iyv.BUNDLED_SCHEMA.is_file()
    assert isinstance(
        yaml.safe_load(iyv.BUNDLED_SCHEMA.read_text(encoding="utf-8")), dict
    )


# --------------------------------------------------------------------------
# YAML loading
# --------------------------------------------------------------------------


def test_missing_file_reports_the_path(tmp_path: Path) -> None:
    with pytest.raises(iyv.InfoYamlError, match="not found"):
        iyv.load_yaml(tmp_path / "absent.yaml", label="INFO.yaml")


def test_malformed_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "INFO.yaml"
    path.write_text("project: [unclosed\n")
    with pytest.raises(iyv.InfoYamlError, match="not valid YAML"):
        iyv.load_yaml(path, label="INFO.yaml")


def test_non_utf8_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "INFO.yaml"
    path.write_bytes(b"project: \xff\xfe\n")
    with pytest.raises(iyv.InfoYamlError, match="not valid UTF-8"):
        iyv.load_yaml(path, label="INFO.yaml")


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
    with pytest.raises(iyv.InfoYamlError, match="must be a boolean"):
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
    with pytest.raises(iyv.InfoYamlError, match="external resource"):
        iyv.validate_schema({"project": "example"}, schema)


def test_external_file_reference_is_refused() -> None:
    """A file: ref would escape the workspace containment on schema-path."""
    schema = {
        "type": "object",
        "properties": {"project": {"$ref": "file:///etc/passwd"}},
    }
    with pytest.raises(iyv.InfoYamlError, match="external resource"):
        iyv.validate_schema({"project": "example"}, schema)


def test_internal_reference_still_resolves() -> None:
    """Refusing external refs must not break self-contained schemas."""
    schema = {
        "definitions": {"name": {"type": "string"}},
        "type": "object",
        "properties": {"project": {"$ref": "#/definitions/name"}},
    }
    assert iyv.validate_schema({"project": "example"}, schema) == []
    assert iyv.validate_schema({"project": 42}, schema) != []


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
    iyv.emit(result, "INFO.yaml")

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
