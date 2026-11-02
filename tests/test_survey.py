# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline tests for the estate survey script."""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from info_yaml.validate import BUNDLED_SCHEMA, load_yaml
from scripts import survey, survey_github

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name / "INFO.yaml").read_bytes()


class StubFetcher:
    """Serve repositories and files from memory, recording each fetch."""

    def __init__(self, estate: dict[str, dict[str, bytes | None]]) -> None:
        self.estate = estate
        self.fetched: list[tuple[str, str]] = []

    def list_repositories(self, org: str) -> list[str]:
        return list(self.estate[org])

    def fetch_info_yaml(self, org: str, repository: str) -> bytes | None:
        self.fetched.append((org, repository))
        return self.estate[org][repository]


ESTATE: dict[str, dict[str, bytes | None]] = {
    "example": {
        "valid": _fixture("valid"),
        "invalid-uri": _fixture("invalid-uri"),
        "missing-keys": _fixture("missing-issue-tracking-keys"),
        "incomplete": _fixture("invalid-schema"),
        "not-yaml": b"project: [\n",
        "not-utf8": b"\xff\xfe",
        "no-info": None,
    },
}


@pytest.fixture(scope="module")
def schemas() -> tuple[Any, Any]:
    return (
        load_yaml(survey.LEGACY_SCHEMA, label="Old schema"),
        load_yaml(BUNDLED_SCHEMA, label="New schema"),
    )


def test_verdict_matrix(schemas: tuple[Any, Any]) -> None:
    results = survey.survey(StubFetcher(ESTATE), ["example"], *schemas)
    verdicts = {item.repository: (item.old, item.new) for item in results["example"]}
    assert verdicts == {
        "valid": ("pass", "pass"),
        "invalid-uri": ("pass", "fail"),
        "missing-keys": ("pass", "fail"),
        "incomplete": ("fail", "fail"),
        "not-yaml": ("fail", "fail"),
        "not-utf8": ("fail", "fail"),
        "no-info": ("missing", "missing"),
    }
    assert [item.repository for item in results["example"] if item.regressed] == [
        "invalid-uri",
        "missing-keys",
    ]


def test_fail_to_pass_is_not_a_regression() -> None:
    outcome = survey.assess(
        "fixed",
        b"name: 1\n",
        {"properties": {"name": {"type": "string"}}},
        {"properties": {"name": {"type": "integer"}}},
    )
    assert (outcome.old, outcome.new, outcome.regressed) == ("fail", "pass", False)


def test_legacy_ignores_uri_format(schemas: tuple[Any, Any]) -> None:
    outcome = survey.assess("uri", _fixture("invalid-uri"), *schemas)
    assert outcome.old_findings == []
    assert outcome.new_findings == [
        "issue_tracking.url: 'not a valid uri' is not a 'uri'"
    ]


def test_legacy_rejects_an_unusable_schema() -> None:
    with pytest.raises(survey_github.SurveyError, match="could not apply"):
        survey.legacy_validate_schema({"a": 1}, {"properties": {"a": {"type": 7}}})


def test_unusable_old_schema_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = tmp_path / "schema.yaml"
    schema.write_text("properties:\n  project:\n    type: 7\n")
    code = survey.main(
        ["--org", "example", "--old-schema", str(schema)], fetcher=StubFetcher(ESTATE)
    )
    assert code == survey.EXIT_ERROR
    assert "could not apply the old schema" in capsys.readouterr().err


def test_unwritable_report_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "absent" / "survey.md"
    code = survey.main(
        ["--org", "example", "--output", str(output)], fetcher=StubFetcher(ESTATE)
    )
    assert code == survey.EXIT_ERROR
    assert capsys.readouterr().err.splitlines()[-1].startswith("error: ")


def test_unwritable_cache_is_an_error(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.write_text("not a directory\n")
    code = survey.main(
        ["--org", "example", "--cache-dir", str(cache)], fetcher=StubFetcher(ESTATE)
    )
    assert code == survey.EXIT_ERROR


def test_regression_exits_one_and_reports(tmp_path: Path) -> None:
    output = tmp_path / "survey.md"
    code = survey.main(
        ["--org", "example", "--output", str(output)], fetcher=StubFetcher(ESTATE)
    )
    assert code == survey.EXIT_REGRESSION
    report = output.read_text(encoding="utf-8")
    lines = report.splitlines()
    assert lines[0] == "# INFO.yaml survey"
    assert "## example" in lines
    assert "| Repository | Old | New | New findings |" in lines
    assert "| `valid` | pass | pass |  |" in lines
    assert (
        "| `invalid-uri` | pass | fail | `issue_tracking.url: 'not a valid uri' "
        "is not a 'uri'` |"
    ) in lines
    assert (
        "| `missing-keys` | pass | fail | `issue_tracking: 'type' is a required "
        "property`<br>`issue_tracking: 'url' is a required property` |"
    ) in lines
    # Findings are listed only where the verdicts differ.
    assert "| `incomplete` | fail | fail |  |" in lines
    assert "Without INFO.yaml: `no-info`" in lines
    assert lines[-1] == (
        "**Totals:** 7 repositories, 6 with INFO.yaml, 1 without; "
        "pass → pass 1, fail → fail 3, fail → pass 0, **pass → fail 2**"
    )


def test_no_regression_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    estate: dict[str, dict[str, bytes | None]] = {
        "first": {"valid": _fixture("valid"), "none": None},
        "second": {"broken": b"project: [\n"},
    }
    fetcher = StubFetcher(estate)
    code = survey.main(["--org", "first", "--org", "second"], fetcher=fetcher)
    assert code == survey.EXIT_OK
    report = capsys.readouterr().out
    assert "## first" in report
    assert "## second" in report
    assert fetcher.fetched == [
        ("first", "valid"),
        ("first", "none"),
        ("second", "broken"),
    ]


def test_report_survives_a_non_utf8_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    monkeypatch.setattr("sys.stdout", stdout)
    fetcher = StubFetcher({"example": {"valid": _fixture("valid")}})
    code = survey.main(["--org", "example"], fetcher=fetcher)
    stdout.flush()
    assert code == survey.EXIT_OK
    report = raw.getvalue().decode("cp1252")
    assert "## example" in report
    assert "pass ? pass 1" in report


def test_default_organisations() -> None:
    assert survey.parse_args([]).orgs is None
    assert "onap" in survey.DEFAULT_ORGS


def test_missing_token_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SURVEY_TOKEN", raising=False)
    code = survey.main(["--token-env", "SURVEY_TOKEN"])
    assert code == survey.EXIT_ERROR
    assert "no token in $SURVEY_TOKEN" in capsys.readouterr().err


def test_defective_new_schema_is_an_error(tmp_path: Path) -> None:
    schema = tmp_path / "schema.yaml"
    schema.write_text("type: 7\n")
    code = survey.main(
        ["--org", "example", "--new-schema", str(schema)], fetcher=StubFetcher(ESTATE)
    )
    assert code == survey.EXIT_ERROR


@pytest.mark.parametrize(
    "estate",
    [{"example": {"no-info": None}}, {"example": {"not-yaml": b"project: [\n"}}],
)
@pytest.mark.parametrize(
    ("option", "text", "message"),
    [
        ("--new-schema", "type: 7\n", "the schema itself is not valid"),
        ("--old-schema", "type: 7\n", "could not apply the old schema"),
    ],
)
def test_defective_schema_is_an_error_with_nothing_to_validate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    estate: dict[str, dict[str, bytes | None]],
    option: str,
    text: str,
    message: str,
) -> None:
    schema = tmp_path / "schema.yaml"
    schema.write_text(text)
    code = survey.main(
        ["--org", "example", option, str(schema)], fetcher=StubFetcher(estate)
    )
    assert code == survey.EXIT_ERROR
    assert message in capsys.readouterr().err


def test_cells_cannot_break_the_table() -> None:
    assert survey._cell("a | b\nc <br>") == "a \\| b c &lt;br&gt;"


def test_markdown_in_a_finding_reaches_the_report_as_text(
    schemas: tuple[Any, Any],
) -> None:
    probe = "![probe](https://example.test/x.png)"
    content = _fixture("invalid-uri").replace(
        b'"not a valid uri"', f'"{probe}"'.encode()
    )
    results = survey.survey(
        StubFetcher({"example": {"probe": content}}), ["example"], *schemas
    )
    (outcome,) = results["example"]
    assert outcome.regressed
    row = next(line for line in survey.render(results).splitlines() if "probe" in line)
    assert row == (
        f"| `probe` | pass | fail | `issue_tracking.url: '{probe}' is not a 'uri'` |"
    )


def test_literal_cells_cannot_break_the_table() -> None:
    assert survey._literal("a | `b`\nc <br>") == "``a \\| `b` c <br>``"


def test_cache_serves_repeat_fetches(tmp_path: Path) -> None:
    inner = StubFetcher({"org": {"has": b"project: x\n", "lacks": None}})
    cached = survey_github.CachingFetcher(inner, tmp_path)
    for _ in range(2):
        assert cached.fetch_info_yaml("org", "has") == b"project: x\n"
        assert cached.fetch_info_yaml("org", "lacks") is None
    assert inner.fetched == [("org", "has"), ("org", "lacks")]
    assert sorted(path.name for path in (tmp_path / "org").iterdir()) == [
        "has.yaml",
        "lacks.missing",
    ]


@pytest.mark.parametrize("name", ["..", "a/b", ""])
def test_cache_refuses_unsafe_names(tmp_path: Path, name: str) -> None:
    cached = survey_github.CachingFetcher(StubFetcher({}), tmp_path)
    with pytest.raises(survey_github.SurveyError):
        cached.fetch_info_yaml("org", name)


def _headers(**values: str) -> Message:
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (200, {}, None),
        (404, {}, None),
        (403, {}, None),
        (403, {"Retry_After": "30"}, 30.0),
        (429, {"Retry_After": "5"}, 5.0),
        (429, {}, 60.0),
        (403, {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "1100"}, 101.0),
        (403, {"X_RateLimit_Remaining": "0"}, 60.0),
    ],
)
def test_wait_seconds(
    status: int, headers: dict[str, str], expected: float | None
) -> None:
    assert (
        survey_github.wait_seconds(status, _headers(**headers), now=1000.0) == expected
    )


SECONDARY_LIMIT = (
    b'{"message": "You have exceeded a secondary rate limit. Please wait a '
    b'few minutes before you try again."}'
)
FORBIDDEN = b'{"message": "Resource not accessible by personal access token"}'


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (SECONDARY_LIMIT, 60.0),
        (b'{"message": "You have triggered an abuse detection mechanism."}', 60.0),
        (FORBIDDEN, None),
    ],
)
def test_wait_seconds_reads_a_secondary_limit_body(
    body: bytes, expected: float | None
) -> None:
    headers = _headers(X_RateLimit_Remaining="4000")
    assert survey_github.wait_seconds(403, headers, 1000.0, body) == expected


class _Response:
    def __init__(
        self, body: bytes, headers: Message, *, truncated: bool = False
    ) -> None:
        self.status = 200
        self.headers = headers
        self._body = body
        self._truncated = truncated

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        if self._truncated:
            return _TruncatedStream(self._body).read()
        return self._body


class _TruncatedStream(io.BytesIO):
    """A body whose connection drops before the declared length arrives."""

    def read(self, size: int | None = -1) -> bytes:
        raise http.client.IncompleteRead(self.getvalue(), 10)


class StubOpener:
    """Answer requests from a queue of (status, body, headers) replies."""

    def __init__(
        self,
        replies: list[tuple[int, bytes, dict[str, str]]],
        *,
        truncated: bool = False,
    ) -> None:
        self.replies = replies
        self.requests: list[urllib.request.Request] = []
        self.truncated = truncated

    def __call__(self, request: urllib.request.Request) -> _Response:
        self.requests.append(request)
        status, body, headers = self.replies.pop(0)
        message = _headers(**headers)
        if status != 200:
            stream = (_TruncatedStream if self.truncated else io.BytesIO)(body)
            raise urllib.error.HTTPError(
                request.full_url, status, "error", message, stream
            )
        return _Response(body, message, truncated=self.truncated)


def _fetcher(opener: StubOpener, sleeps: list[float]) -> survey_github.GitHubFetcher:
    return survey_github.GitHubFetcher(
        "secret", opener=opener, sleep=sleeps.append, clock=lambda: 1000.0
    )


def test_fetcher_honours_retry_after() -> None:
    opener = StubOpener([(429, b"", {"Retry_After": "7"}), (200, b"project: x\n", {})])
    sleeps: list[float] = []
    assert _fetcher(opener, sleeps).fetch_info_yaml("org", "repo") == b"project: x\n"
    assert sleeps == [7.0]
    request = opener.requests[0]
    assert (
        request.full_url == "https://api.github.com/repos/org/repo/contents/INFO.yaml"
    )
    assert request.get_header("Authorization") == "Bearer secret"


def test_fetcher_waits_for_an_exhausted_primary_limit() -> None:
    spent = {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "1010"}
    opener = StubOpener([(200, b"a: 1\n", spent), (404, b"", {})])
    sleeps: list[float] = []
    fetcher = _fetcher(opener, sleeps)
    assert fetcher.fetch_info_yaml("org", "one") == b"a: 1\n"
    assert sleeps == []
    assert fetcher.fetch_info_yaml("org", "two") is None
    assert sleeps == [11.0]


def test_fetcher_gives_up_after_repeated_limits() -> None:
    opener = StubOpener([(429, b"", {"Retry_After": "1"})] * 5)
    with pytest.raises(survey_github.SurveyError, match="still rate limited"):
        _fetcher(opener, []).fetch_info_yaml("org", "repo")


def test_fetcher_retries_a_secondary_limit_403() -> None:
    opener = StubOpener(
        [(403, SECONDARY_LIMIT, {"X_RateLimit_Remaining": "4000"}), (404, b"", {})]
    )
    sleeps: list[float] = []
    assert _fetcher(opener, sleeps).fetch_info_yaml("org", "repo") is None
    assert sleeps == [60.0]


def test_fetcher_does_not_retry_a_permission_403() -> None:
    opener = StubOpener([(403, FORBIDDEN, {"X_RateLimit_Remaining": "4000"})])
    sleeps: list[float] = []
    with pytest.raises(survey_github.SurveyError, match="HTTP 403"):
        _fetcher(opener, sleeps).fetch_info_yaml("org", "repo")
    assert sleeps == []


def test_fetcher_reports_other_failures() -> None:
    opener = StubOpener([(500, b"", {})])
    with pytest.raises(survey_github.SurveyError, match="HTTP 500"):
        _fetcher(opener, []).fetch_info_yaml("org", "repo")


@pytest.mark.parametrize("status", [200, 500])
def test_fetcher_reports_a_truncated_response(status: int) -> None:
    opener = StubOpener([(status, b"proj", {})], truncated=True)
    with pytest.raises(survey_github.SurveyError, match="IncompleteRead"):
        _fetcher(opener, []).fetch_info_yaml("org", "repo")


def test_truncated_response_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = StubOpener([(200, b"[", {})], truncated=True)
    monkeypatch.setenv("SURVEY_TOKEN", "secret")
    monkeypatch.setattr(
        survey,
        "GitHubFetcher",
        lambda token: survey_github.GitHubFetcher(token, opener=opener),
    )
    code = survey.main(["--org", "example", "--token-env", "SURVEY_TOKEN"])
    assert code == survey.EXIT_ERROR
    assert "IncompleteRead" in capsys.readouterr().err


@pytest.mark.parametrize("body", [b"[", b'{"message": "x"}', b'[{"id": 1}]'])
def test_listing_rejects_an_unexpected_body(body: bytes) -> None:
    opener = StubOpener([(200, body, {})])
    with pytest.raises(survey_github.SurveyError, match="unexpected response body"):
        _fetcher(opener, []).list_repositories("org")


def test_listing_pages_and_filters() -> None:
    first = [
        {"name": "zeta"},
        {"name": "old", "archived": True},
        {"name": "copy", "fork": True},
        {"name": "Alpha"},
    ]
    opener = StubOpener(
        [
            (200, json.dumps(first).encode(), {}),
            (200, json.dumps([{"name": "beta"}]).encode(), {}),
            (200, b"[]", {}),
        ]
    )
    assert _fetcher(opener, []).list_repositories("org") == ["Alpha", "beta", "zeta"]
    assert [request.full_url for request in opener.requests] == [
        f"https://api.github.com/orgs/org/repos?type=public&per_page=100&page={page}"
        for page in (1, 2, 3)
    ]
