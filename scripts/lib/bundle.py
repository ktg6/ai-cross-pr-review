"""Shared helpers for the review step: bundle verification and output hygiene.

The prepare step writes a bundle whose ``manifest.json`` hashes every input
file. Both the review adapter and the normalizer re-verify those hashes before
using the bundle, so a tampered or truncated artifact stops the run instead of
being reviewed and reported as "no issues".

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import limits as limits_mod

MANIFEST_NAME = "manifest.json"
REDACTED = "[REDACTED]"

# Control characters, keeping tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Credential-shaped values. Matching is on shape only; no value is ever logged.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
)


class BundleError(Exception):
    """The bundle is missing, malformed, or does not match its manifest."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_text(value: object, max_chars: int) -> str:
    """Strip control characters, normalize newlines, and clamp the length."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    return text[:max_chars]


def redact_secrets(text: str, extra_values: tuple[str, ...] = ()) -> tuple[str, int]:
    """Replace credential-shaped substrings. Returns the text and a hit count.

    ``extra_values`` holds literal values that must never appear in output, such
    as the token handed to the review step. Values shorter than 8 characters are
    ignored so that a misconfigured empty secret cannot blank the whole text.
    """
    count = 0
    for value in extra_values:
        if isinstance(value, str) and len(value) >= 8 and value in text:
            count += text.count(value)
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text, hits = pattern.subn(REDACTED, text)
        count += hits
    return text, count


@dataclass(frozen=True)
class ChangedFileEntry:
    path: str
    status: str
    excluded: str | None
    binary: bool
    patch_bytes: int

    @property
    def reviewable(self) -> bool:
        return self.excluded is None and self.patch_bytes > 0


@dataclass(frozen=True)
class Bundle:
    directory: Path
    manifest: dict
    pr_metadata: dict
    files: list[ChangedFileEntry]
    diff: bytes
    policy: bytes | None

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    def file_index(self) -> dict[str, ChangedFileEntry]:
        return {entry.path: entry for entry in self.files}


def _read_json(path: Path, max_bytes: int) -> object:
    try:
        data = path.read_bytes()
    except OSError as err:
        raise BundleError(f"cannot read {path.name}: {err.__class__.__name__}") from None
    if len(data) > max_bytes:
        raise BundleError(f"{path.name} exceeds limit: {len(data)} > {max_bytes}")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BundleError(f"{path.name} is not valid JSON") from None


def load_bundle(directory: Path, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS) -> Bundle:
    """Load a bundle after verifying every manifest hash.

    Any missing file, size overrun, hash mismatch, or unexpected extra file
    stops the run. ``run.json`` is generated alongside the manifest as trusted
    run metadata but is intentionally not included in the input-file hashes.
    """
    directory = Path(directory)
    manifest = _read_json(directory / MANIFEST_NAME, limits.max_api_response_bytes)
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json is not an object")
    if manifest.get("bundle_schema_version") != limits_mod.BUNDLE_SCHEMA_VERSION:
        raise BundleError("unsupported bundle schema version")
    for key in ("repository", "pr_number", "base_sha", "head_sha", "merge_base_sha", "snapshot_id", "diff", "policy"):
        if key not in manifest:
            raise BundleError(f"manifest.json is missing {key}")

    hashed = manifest.get("files")
    if not isinstance(hashed, dict) or not hashed:
        raise BundleError("manifest.json has no file hashes")
    allowed_names = set(hashed) | {MANIFEST_NAME, "run.json"}
    try:
        actual_names = {entry.name for entry in directory.iterdir()}
    except OSError as err:
        raise BundleError(f"cannot list bundle directory: {err.__class__.__name__}") from None
    unexpected = actual_names - allowed_names
    if unexpected:
        raise BundleError(f"bundle has unexpected files: {', '.join(sorted(unexpected))}")
    for name, info in sorted(hashed.items()):
        if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
            raise BundleError("manifest.json references an unsafe file name")
        if not isinstance(info, dict) or not isinstance(info.get("sha256"), str):
            raise BundleError(f"manifest.json has no digest for {name}")
        target = directory / name
        if not target.is_file():
            raise BundleError(f"bundle file is missing: {name}")
        if target.is_symlink():
            raise BundleError(f"bundle file must not be a symlink: {name}")
        data = target.read_bytes()
        if len(data) != info.get("bytes"):
            raise BundleError(f"bundle file size does not match the manifest: {name}")
        if sha256_hex(data) != info["sha256"]:
            raise BundleError(f"bundle file digest does not match the manifest: {name}")

    pr_metadata = _read_json(directory / "pr-metadata.json", limits.max_metadata_bytes)
    if not isinstance(pr_metadata, dict):
        raise BundleError("pr-metadata.json is not an object")

    raw_files = _read_json(directory / "files.json", limits.max_api_response_bytes)
    if not isinstance(raw_files, list):
        raise BundleError("files.json is not an array")
    files: list[ChangedFileEntry] = []
    for item in raw_files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BundleError("files.json contains an invalid entry")
        files.append(
            ChangedFileEntry(
                path=item["path"],
                status=str(item.get("status", "")),
                excluded=item["excluded"] if isinstance(item.get("excluded"), str) else None,
                binary=bool(item.get("binary")),
                patch_bytes=int(item.get("patch_bytes") or 0),
            )
        )
    if not files:
        raise BundleError("files.json is empty")

    diff = (directory / "diff.patch").read_bytes()
    limits_mod.check_limit("bundle diff size", len(diff), limits.max_diff_total_bytes)

    policy: bytes | None = None
    policy_info = manifest["policy"]
    if isinstance(policy_info, dict) and policy_info.get("present"):
        policy = (directory / "policy.md").read_bytes()
        limits_mod.check_limit("bundle policy size", len(policy), limits.max_policy_bytes)

    return Bundle(directory=directory, manifest=manifest, pr_metadata=pr_metadata, files=files, diff=diff, policy=policy)
