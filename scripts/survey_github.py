# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch root INFO.yaml files through the GitHub REST API for the survey.

``GitHubFetcher`` makes authenticated requests and waits out rate
limits; ``CachingFetcher`` keeps what it fetched on disk so an
interrupted survey resumes where it stopped.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, Protocol

API_ROOT = "https://api.github.com"
INFO_PATH = "INFO.yaml"

# GitHub limits organisation and repository names to these characters,
# which also keeps a name from steering a cache path outside its directory.
_NAME = re.compile(r"[A-Za-z0-9._-]+")


class SurveyError(Exception):
    """A problem that stops the survey from running at all."""


class Fetcher(Protocol):
    """The source of repositories and their INFO.yaml files."""

    def list_repositories(self, org: str) -> list[str]:
        """Return the public, non-archived, non-fork repositories of ``org``."""
        ...

    def fetch_info_yaml(self, org: str, repository: str) -> bytes | None:
        """Return the root INFO.yaml of a repository, or ``None`` if absent."""
        ...


def _integer_header(headers: Message, name: str) -> float | None:
    value = (headers.get(name) or "").strip()
    return float(value) if value.isdigit() else None


def wait_seconds(status: int, headers: Message, now: float) -> float | None:
    """Return how long to wait before retrying a response, or ``None``.

    ``None`` means the response is not a rate-limit response and must not
    be retried. GitHub signals a secondary limit with ``Retry-After``, and
    an exhausted primary limit with ``X-RateLimit-Remaining: 0`` and the
    epoch second the window resets at in ``X-RateLimit-Reset``. A limit
    response carrying neither waits a minute, as GitHub advises.
    """
    if status not in (403, 429):
        return None
    retry_after = _integer_header(headers, "Retry-After")
    if retry_after is not None:
        return retry_after
    if headers.get("X-RateLimit-Remaining") == "0":
        reset = _integer_header(headers, "X-RateLimit-Reset")
        return 60.0 if reset is None else max(reset - now, 0.0) + 1.0
    return 60.0 if status == 429 else None


class GitHubFetcher:
    """Read repositories and files through the GitHub REST API."""

    def __init__(
        self,
        token: str,
        *,
        opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 5,
    ) -> None:
        self._token = token
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._resume_at = 0.0

    def _get(self, url: str, accept: str) -> tuple[int, bytes]:
        """GET a URL, waiting out rate limits; return the status and body."""
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "info-yaml-validate-action-survey",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        for _ in range(self._max_attempts):
            delay = self._resume_at - self._clock()
            if delay > 0:
                self._sleep(delay)
            try:
                with self._opener(request) as response:
                    status, body, headers = (
                        response.status,
                        response.read(),
                        response.headers,
                    )
            except urllib.error.HTTPError as exc:
                status, body, headers = exc.code, exc.read(), exc.headers
            except urllib.error.URLError as exc:
                raise SurveyError(f"GET {url} failed: {exc.reason}") from None
            # A response that spent the last of the primary allowance
            # paces the next request, rather than a request failing to
            # learn the same thing.
            reset = _integer_header(headers, "X-RateLimit-Reset")
            if headers.get("X-RateLimit-Remaining") == "0" and reset is not None:
                self._resume_at = reset + 1.0
            wait = wait_seconds(status, headers, self._clock())
            if wait is None:
                return status, body
            self._resume_at = max(self._resume_at, self._clock() + wait)
        raise SurveyError(
            f"GET {url} still rate limited after {self._max_attempts} attempts"
        )

    def list_repositories(self, org: str) -> list[str]:
        """Return the public, non-archived, non-fork repositories of ``org``."""
        names: list[str] = []
        page = 1
        while True:
            url = (
                f"{API_ROOT}/orgs/{urllib.parse.quote(org, safe='')}/repos"
                f"?type=public&per_page=100&page={page}"
            )
            status, body = self._get(url, "application/vnd.github+json")
            if status != 200:
                raise SurveyError(f"listing the repositories of {org}: HTTP {status}")
            batch = json.loads(body)
            if not batch:
                return sorted(names, key=str.lower)
            names.extend(
                entry["name"]
                for entry in batch
                if not entry.get("archived")
                and not entry.get("fork")
                and not entry.get("private")
            )
            page += 1

    def fetch_info_yaml(self, org: str, repository: str) -> bytes | None:
        """Return the root INFO.yaml of a repository, or ``None`` if absent."""
        url = (
            f"{API_ROOT}/repos/{urllib.parse.quote(org, safe='')}/"
            f"{urllib.parse.quote(repository, safe='')}/contents/{INFO_PATH}"
        )
        status, body = self._get(url, "application/vnd.github.raw+json")
        if status == 200:
            return body
        # The API answers 404 for an absent file and for an empty
        # repository alike; neither has an INFO.yaml to survey.
        if status == 404:
            return None
        raise SurveyError(f"fetching {org}/{repository}/{INFO_PATH}: HTTP {status}")


class CachingFetcher:
    """Keep fetched files on disk, so an interrupted survey resumes cheaply.

    Each repository gets ``<org>/<repository>.yaml`` holding its file, or
    ``<org>/<repository>.missing`` recording that it has none. Repository
    lists are not cached: they change, and cost one request per hundred
    repositories.
    """

    def __init__(self, inner: Fetcher, directory: Path) -> None:
        self._inner = inner
        self._directory = directory

    def list_repositories(self, org: str) -> list[str]:
        """Return the repositories of ``org`` from the wrapped fetcher."""
        return self._inner.list_repositories(org)

    def fetch_info_yaml(self, org: str, repository: str) -> bytes | None:
        """Return the cached file, fetching and caching it on first use."""
        for name in (org, repository):
            if not _NAME.fullmatch(name) or name in (".", ".."):
                raise SurveyError(f"refusing to cache under the name {name!r}")
        directory = self._directory / org
        found = directory / f"{repository}.yaml"
        missing = directory / f"{repository}.missing"
        if found.is_file():
            return found.read_bytes()
        if missing.is_file():
            return None
        content = self._inner.fetch_info_yaml(org, repository)
        directory.mkdir(parents=True, exist_ok=True)
        # Write through a temporary name, so an interrupted run never
        # leaves a truncated file for the next run to trust.
        target = missing if content is None else found
        partial = directory / f"{target.name}.partial"
        partial.write_bytes(content or b"")
        partial.replace(target)
        return content
