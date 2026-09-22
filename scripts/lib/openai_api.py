"""Minimal OpenAI Responses API client for the verification stage (ADR-0007).

Standard library only (urllib). The client sends exactly one kind of request:
a tool-less, non-stored, strict-structured-output response creation. It never
receives a GitHub token and never decides what to review.

Retries are deliberately narrow. Creating a response is not idempotent and
costs money, so only HTTP 429 (refused before the model ran) and transport
failures that produced no HTTP status are repeated. A 5xx may mean the model
already ran, so it is surfaced as a failure instead of being paid for twice.

The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import limits as limits_mod

# (method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, "bytes | None"], tuple[int, dict, bytes]]

USER_AGENT = "ai-cross-pr-review-verify"


class OpenAIError(Exception):
    """Transport or API failure. ``status`` is 0 for transport-level errors."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _urllib_transport_factory(max_bytes: int, timeout: float) -> Transport:
    def transport(method: str, url: str, headers: dict, body: bytes | None = None):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https enforced by caller)
                return response.status, dict(response.headers), response.read(max_bytes + 1)
        except urllib.error.HTTPError as err:
            data = err.read(max_bytes + 1) if err.fp is not None else b""
            return err.code, dict(err.headers or {}), data
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            # Never echo the request: it carries the review bundle.
            raise OpenAIError(f"OpenAI request failed: {err.__class__.__name__}") from None

    return transport


def validate_https_url(value: str, what: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise OpenAIError(f"{what} must be an https URL without query/fragment")
    if "@" in parsed.netloc:
        raise OpenAIError(f"{what} must not embed credentials")
    return value.rstrip("/")


class ResponsesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        transport: Transport | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
        timeout_seconds: float = 600.0,
        retry_attempts: int = 1,
        retry_delay_seconds: float = 2.0,
        sleep=time.sleep,
    ):
        self.base_url = validate_https_url(base_url, "openai base url")
        if not api_key:
            raise OpenAIError("OPENAI_API_KEY is not set")
        self._api_key = api_key
        self._max_bytes = max_response_bytes
        self._transport = transport or _urllib_transport_factory(max_response_bytes, timeout_seconds)
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_delay = max(0.0, float(retry_delay_seconds))
        self._sleep = sleep

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def create_response(self, payload: dict) -> dict:
        """POST /v1/responses and return the parsed response object."""
        url = self.base_url + limits_mod.CODEX_API_PATH
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > self._max_bytes:
            raise OpenAIError("verification request body is too large")

        status = 0
        data = b""
        for attempt in range(1, self._retry_attempts + 1):
            last = attempt == self._retry_attempts
            try:
                status, _headers, data = self._transport("POST", url, self._headers(), body)
            except OpenAIError:
                if last:
                    raise
            else:
                if status != 429 or last:
                    break
            self._sleep(self._retry_delay * attempt)

        if len(data) > self._max_bytes:
            raise OpenAIError("OpenAI response is too large", status)
        if status != 200:
            # The body may quote the request; report the status only.
            raise OpenAIError(f"OpenAI API returned HTTP {status}", status)
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OpenAIError("OpenAI API returned invalid JSON", status) from None
        if not isinstance(parsed, dict):
            raise OpenAIError("OpenAI response is not an object", status)
        return parsed


def extract_structured_text(response: dict) -> str:
    """Return the assistant's structured output text, or fail closed.

    Every abnormal outcome (incomplete generation, refusal, missing message)
    raises. None of them may be turned into an empty, successful review.
    """
    status = response.get("status")
    if status == "incomplete":
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        raise OpenAIError(f"verification response is incomplete: {_safe(reason)}")
    if status != "completed":
        raise OpenAIError(f"verification response did not complete: {_safe(status)}")

    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIError("verification response has no output array")

    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "refusal":
                raise OpenAIError("verification was refused by the model")
            if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    if not chunks:
        raise OpenAIError("verification response contains no output text")
    return "".join(chunks)


def reported_model(response: dict) -> str | None:
    value = response.get("model")
    return value if isinstance(value, str) and value else None


def usage_tokens(response: dict) -> tuple[int | None, int | None]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None, None

    def count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return count("input_tokens"), count("output_tokens")


def _safe(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return text if text.isascii() and text.isprintable() and len(text) <= 80 else "unknown"
