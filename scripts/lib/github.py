"""Minimal GitHub REST client for the prepare and publish steps.

Standard library only (urllib). The client never decides *what* to fetch or
where to post from untrusted data: repository, PR number, SHAs and comment IDs
are validated before they are interpolated into URLs, and every request and
response body is size-capped.

Reads are retried a bounded number of times when the client is constructed with
``retry_attempts > 1``. Writes are retried only on 429 (the request was refused,
so it cannot have been applied); a 5xx on a POST could already have created a
comment, so it is never repeated.

The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

# (method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, "bytes | None"], tuple[int, dict, bytes]]

USER_AGENT = "ai-cross-pr-review-prepare"
API_VERSION = "2022-11-28"

_REPO_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_PR_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_COMMENT_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
MAX_PR_NUMBER = 2**31 - 1
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


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


def validate_comment_id(value: str | int) -> int:
    text = str(value).strip() if not isinstance(value, bool) else ""
    if not _COMMENT_ID.fullmatch(text):
        raise ValidationError("comment id must be a positive integer")
    return int(text)


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
    def transport(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
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
        retry_attempts: int = 1,
        retry_delay_seconds: float = 2.0,
        sleep=time.sleep,
    ):
        self.api_url = validate_https_url(api_url, "api_url")
        self._token = token or None
        self._max_bytes = max_response_bytes
        self._transport = transport or _urllib_transport_factory(max_response_bytes)
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_delay = max(0.0, float(retry_delay_seconds))
        self._sleep = sleep

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

    def _send(self, method: str, url: str, headers: dict, body: bytes | None, *, retry_5xx: bool):
        """Send one request, retrying a bounded number of times when allowed."""
        for attempt in range(1, self._retry_attempts + 1):
            last = attempt == self._retry_attempts
            try:
                status, _headers, data = self._transport(method, url, headers, body)
            except GitHubError:
                # Transport-level failure: safe to repeat only for idempotent calls.
                if last or not retry_5xx:
                    raise
            else:
                if status == 429:
                    # Refused before any state change, so a repeat is always safe.
                    if last:
                        return status, data
                elif not (retry_5xx and status in RETRY_STATUSES) or last:
                    return status, data
            self._sleep(self._retry_delay * attempt)
        raise GitHubError("GitHub request exhausted its retries")  # pragma: no cover - loop always returns

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        allow_404: bool = False,
        retry_5xx: bool = True,
    ):
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = self._headers()
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(body) > self._max_bytes:
                raise GitHubError(f"request body too large for {path}")
            headers["Content-Type"] = "application/json"
        status, data = self._send(method, url, headers, body, retry_5xx=retry_5xx)
        if len(data) > self._max_bytes:
            raise GitHubError(f"GitHub response too large for {path}", status)
        if status == 404 and allow_404:
            return None
        if status not in (200, 201):
            raise GitHubError(f"GitHub API returned HTTP {status} for {path}", status)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GitHubError(f"GitHub API returned invalid JSON for {path}", status) from None

    def _get_json(self, path: str, params: dict | None = None, *, allow_404: bool = False):
        return self._request_json("GET", path, params=params, allow_404=allow_404)

    # -- endpoints --------------------------------------------------------

    def get_pull(self, owner: str, name: str, number: int) -> dict | None:
        """Return PR metadata or None when the PR does not exist."""
        data = self._get_json(f"/repos/{owner}/{name}/pulls/{number}", allow_404=True)
        if data is not None and not isinstance(data, dict):
            raise GitHubError("pull request payload is not an object")
        return data

    def get_authenticated_login(self) -> str:
        """Return the login of the account represented by the current token."""
        data = self._get_json("/user")
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise GitHubError("authenticated user payload missing login")
        return login

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

    # -- issue comments (publish step) ------------------------------------

    def list_issue_comments(
        self, owner: str, name: str, number: int, *, per_page: int = 100, max_pages: int = 10
    ) -> list[dict]:
        """Return every comment on a PR conversation, or fail when there are too many.

        Failing closed is deliberate: if the publisher cannot see the whole list
        it cannot tell whether its own marker is already present, and posting a
        duplicate is worse than not posting.
        """
        comments: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get_json(
                f"/repos/{owner}/{name}/issues/{number}/comments",
                {"per_page": per_page, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubError("issue comments payload is not an array")
            comments.extend(item for item in data if isinstance(item, dict))
            if len(data) < per_page:
                return comments
        raise GitHubError(f"issue comment list exceeds {max_pages} pages")

    def create_issue_comment(self, owner: str, name: str, number: int, body: str) -> dict:
        """Create a comment. Never retried on 5xx: the comment may already exist."""
        data = self._request_json(
            "POST",
            f"/repos/{owner}/{name}/issues/{number}/comments",
            payload={"body": body},
            retry_5xx=False,
        )
        if not isinstance(data, dict):
            raise GitHubError("created comment payload is not an object")
        return data

    def update_issue_comment(self, owner: str, name: str, comment_id: int, body: str) -> dict:
        comment_id = validate_comment_id(comment_id)
        data = self._request_json(
            "PATCH",
            f"/repos/{owner}/{name}/issues/comments/{comment_id}",
            payload={"body": body},
        )
        if not isinstance(data, dict):
            raise GitHubError("updated comment payload is not an object")
        return data
