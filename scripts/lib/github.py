"""Minimal read-only GitHub REST client for the prepare step.

Standard library only (urllib). The client never decides *what* to fetch from
untrusted data: repository, PR number and SHAs are validated before they are
interpolated into URLs, and every response body is size-capped.

The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

Transport = Callable[[str, str, dict], tuple[int, dict, bytes]]

USER_AGENT = "ai-cross-pr-review-prepare"
API_VERSION = "2022-11-28"

_REPO_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_PR_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
MAX_PR_NUMBER = 2**31 - 1


class GitHubError(Exception):
    """Transport or API failure. ``status`` is 0 for transport-level errors."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class ValidationError(ValueError):
    """Untrusted input failed validation. Deterministic stop."""


def validate_pr_number(value: str | int) -> int:
    text = str(value).strip() if not isinstance(value, bool) else ""
    if not _PR_NUMBER.fullmatch(text):
        raise ValidationError("pr_number must be a positive integer")
    number = int(text)
    if number > MAX_PR_NUMBER:
        raise ValidationError("pr_number is out of range")
    return number


def validate_repository(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("/") != 1:
        raise ValidationError("repository must be 'owner/name'")
    owner, name = value.split("/")
    if name.endswith(".git"):
        raise ValidationError("repository name must not end with .git")
    for part in (owner, name):
        if not _REPO_SEGMENT.fullmatch(part) or part in (".", ".."):
            raise ValidationError("repository contains invalid characters")
    return owner, name


def validate_sha(value: object, what: str = "sha") -> str:
    if not isinstance(value, str) or not _SHA40.fullmatch(value):
        raise ValidationError(f"{what} is not a full lowercase 40-hex SHA")
    return value


def validate_branch_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValidationError("branch name is empty or too long")
    if value.startswith("-") or value.startswith("/") or value.endswith("/"):
        raise ValidationError("branch name has invalid leading/trailing character")
    if value.endswith(".lock") or value.endswith(".") or _BRANCH_FORBIDDEN.search(value):
        raise ValidationError("branch name contains forbidden characters")
    return value


def validate_https_url(value: str, what: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValidationError(f"{what} must be an https URL without query/fragment")
    if "@" in parsed.netloc:
        raise ValidationError(f"{what} must not embed credentials")
    return value.rstrip("/")


def _urllib_transport_factory(max_bytes: int) -> Transport:
    def transport(method: str, url: str, headers: dict) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https enforced by caller)
                body = response.read(max_bytes + 1)
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as err:
            body = err.read(max_bytes + 1) if err.fp is not None else b""
            return err.code, dict(err.headers or {}), body
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            # Never include the URL's headers here; the URL itself carries no secret.
            raise GitHubError(f"GitHub request failed: {err.__class__.__name__}") from None

    return transport


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token: str | None,
        *,
        transport: Transport | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
    ):
        self.api_url = validate_https_url(api_url, "api_url")
        self._token = token or None
        self._max_bytes = max_response_bytes
        self._transport = transport or _urllib_transport_factory(max_response_bytes)

    # -- low level --------------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(self, path: str, params: dict | None = None, *, allow_404: bool = False):
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, _headers, body = self._transport("GET", url, self._headers())
        if len(body) > self._max_bytes:
            raise GitHubError(f"GitHub response too large for {path}", status)
        if status == 404 and allow_404:
            return None
        if status != 200:
            raise GitHubError(f"GitHub API returned HTTP {status} for {path}", status)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GitHubError(f"GitHub API returned invalid JSON for {path}", status) from None

    # -- endpoints --------------------------------------------------------

    def get_pull(self, owner: str, name: str, number: int) -> dict | None:
        """Return PR metadata or None when the PR does not exist."""
        data = self._get_json(f"/repos/{owner}/{name}/pulls/{number}", allow_404=True)
        if data is not None and not isinstance(data, dict):
            raise GitHubError("pull request payload is not an object")
        return data

    def get_branch_head_sha(self, owner: str, name: str, branch: str) -> str:
        branch = validate_branch_name(branch)
        quoted = urllib.parse.quote(branch, safe="")
        data = self._get_json(f"/repos/{owner}/{name}/branches/{quoted}")
        try:
            return validate_sha(data["commit"]["sha"], "branch head sha")
        except (KeyError, TypeError):
            raise GitHubError("branch payload missing commit sha") from None

    def get_merge_base_sha(self, owner: str, name: str, base_sha: str, head_sha: str) -> str | None:
        """Merge-base between two fixed SHAs via the compare endpoint.

        Both arguments are SHAs (never branch names) so the answer is a pure
        function of the recorded snapshot. Returns None when GitHub reports no
        common history.
        """
        base_sha = validate_sha(base_sha, "base sha")
        head_sha = validate_sha(head_sha, "head sha")
        data = self._get_json(
            f"/repos/{owner}/{name}/compare/{base_sha}...{head_sha}",
            {"per_page": 1, "page": 1},
            allow_404=True,
        )
        if data is None:
            return None
        try:
            return validate_sha(data["merge_base_commit"]["sha"], "merge-base sha")
        except (KeyError, TypeError):
            raise GitHubError("compare payload missing merge_base_commit") from None

    def get_file_content(
        self, owner: str, name: str, path: str, ref: str, max_bytes: int
    ) -> bytes | None:
        """Return raw bytes of a regular file at ``ref`` (a SHA), or None if absent.

        Raises ValidationError when the path is not a regular file or exceeds
        ``max_bytes`` (checked before decoding).
        """
        ref = validate_sha(ref, "content ref")
        quoted = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
        data = self._get_json(f"/repos/{owner}/{name}/contents/{quoted}", {"ref": ref}, allow_404=True)
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("type") != "file":
            raise ValidationError(f"{path} is not a regular file")
        size = data.get("size")
        if not isinstance(size, int) or size < 0:
            raise GitHubError("content payload has invalid size")
        if size > max_bytes:
            raise ValidationError(f"{path} exceeds limit: {size} > {max_bytes}")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubError("content payload is not base64")
        try:
            raw = base64.b64decode(data["content"], validate=False)
        except ValueError:
            raise GitHubError("content payload has invalid base64") from None
        if len(raw) > max_bytes:
            raise ValidationError(f"{path} exceeds limit after decoding")
        return raw
