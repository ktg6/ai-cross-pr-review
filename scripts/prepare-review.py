#!/usr/bin/env python3
"""prepare step: build a reproducible, AI-free review bundle for a fixed PR snapshot.

Reads PR metadata from the GitHub API, pins base/head/merge-base SHAs, fetches
only those commits into a private bare repository, generates size-limited
per-file diffs, fetches the default-branch review policy, and writes a bundle
whose manifest hashes every input. Any inconsistency or limit breach stops the
run without a bundle (exit 2).

Standard library only. No PR code is checked out or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import diff as diff_mod  # noqa: E402
from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_SERVER_URL = "https://github.com"
DEFAULT_POLICY_PATH = ".github/ai-review.md"
TOKEN_ENV = "GITHUB_TOKEN"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PrepareError(Exception):
    """Deterministic stop: the snapshot cannot be reviewed safely."""


def log(message: str) -> None:
    sys.stderr.write(f"prepare-review: {message}\n")


# -- helpers ------------------------------------------------------------------


def sanitize_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    return text[:max_chars]


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", "ignore"), True


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


# -- PR snapshot ------------------------------------------------------------------


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    repository: str
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    default_branch: str
    head_repository: str
    is_fork: bool
    draft: bool
    changed_files: int


def _nested(data: dict, *keys: str):
    node: object = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise PrepareError(f"pull request payload missing {'.'.join(keys)}")
        node = node[key]
    return node


def extract_snapshot(pr: dict, repository: str, number: int) -> PullSnapshot:
    if not isinstance(pr, dict):
        raise PrepareError("pull request payload is not an object")
    if _nested(pr, "number") != number:
        raise PrepareError("pull request number mismatch")
    if _nested(pr, "state") != "open" or pr.get("merged") is True:
        raise PrepareError("pull request is not open")
    base_full = _nested(pr, "base", "repo", "full_name")
    if not isinstance(base_full, str) or base_full.lower() != repository.lower():
        raise PrepareError("pull request base repository does not match the requested repository")
    head_repo = _nested(pr, "head", "repo")
    if not isinstance(head_repo, dict):
        raise PrepareError("pull request head repository is unavailable (deleted fork?)")
    head_full = head_repo.get("full_name")
    if not isinstance(head_full, str) or not head_full:
        raise PrepareError("pull request head repository name is missing")
    changed = _nested(pr, "changed_files")
    if not isinstance(changed, int) or isinstance(changed, bool) or changed < 0:
        raise PrepareError("pull request changed_files is invalid")
    return PullSnapshot(
        number=number,
        repository=base_full,
        base_sha=gh.validate_sha(_nested(pr, "base", "sha"), "base sha"),
        head_sha=gh.validate_sha(_nested(pr, "head", "sha"), "head sha"),
        base_ref=sanitize_text(_nested(pr, "base", "ref"), 255),
        head_ref=sanitize_text(_nested(pr, "head", "ref"), 255),
        default_branch=gh.validate_branch_name(_nested(pr, "base", "repo", "default_branch")),
        head_repository=head_full,
        is_fork=head_full.lower() != base_full.lower(),
        draft=bool(pr.get("draft", False)),
        changed_files=changed,
    )


def build_pr_metadata(pr: dict, snap: PullSnapshot, limits: limits_mod.Limits) -> dict:
    """Untrusted PR metadata, size-capped, for the reviewer prompt."""
    body, truncated = truncate_utf8(sanitize_text(pr.get("body"), 10 * limits.max_body_bytes), limits.max_body_bytes)
    labels = []
    for item in (pr.get("labels") or [])[: limits.max_labels]:
        if isinstance(item, dict):
            labels.append(sanitize_text(item.get("name"), 100))
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    meta = {
        "trust": "untrusted",
        "number": snap.number,
        "title": sanitize_text(pr.get("title"), limits.max_title_chars),
        "body": body,
        "body_truncated": truncated,
        "author": sanitize_text(user.get("login"), 100),
        "base_ref": snap.base_ref,
        "head_ref": snap.head_ref,
        "head_repository": sanitize_text(snap.head_repository, 200),
        "is_fork": snap.is_fork,
        "draft": snap.draft,
        "labels": labels,
        "changed_files": snap.changed_files,
    }
    limits_mod.check_limit("pr metadata size", len(json_bytes(meta)), limits.max_metadata_bytes)
    return meta


def load_policy(client: gh.GitHubClient, owner: str, name: str, snap: PullSnapshot, policy_path: str, limits: limits_mod.Limits) -> tuple[str, bytes | None]:
    """Fetch the repository review policy from the default branch's pinned SHA."""
    commit_sha = client.get_branch_head_sha(owner, name, snap.default_branch)
    raw = client.get_file_content(owner, name, policy_path, commit_sha, limits.max_policy_bytes)
    if raw is None:
        return commit_sha, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PrepareError("review policy is not valid UTF-8") from None
    if "\x00" in text:
        raise PrepareError("review policy contains NUL bytes")
    return commit_sha, raw


def build_remote_url(server_url: str, owner: str, name: str, with_username: bool) -> str:
    parts = gh.validate_https_url(server_url, "server_url")
    scheme, netloc, path, _q, _f = urllib.parse.urlsplit(parts)
    if with_username:
        netloc = "x-access-token@" + netloc
    return f"{scheme}://{netloc}{path.rstrip('/')}/{owner}/{name}.git"


# -- bundle -------------------------------------------------------------------------


def _ensure_empty_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise PrepareError(f"output directory is not empty: {path}")
    else:
        path.mkdir(parents=True)


def write_bundle(
    output_dir: Path,
    *,
    snap: PullSnapshot,
    merge_base_sha: str,
    policy_path: str,
    policy_commit_sha: str,
    policy: bytes | None,
    pr_metadata: dict,
    files: list[diff_mod.ChangedFile],
    limits: limits_mod.Limits,
    run_info: dict,
) -> dict:
    _ensure_empty_dir(output_dir)
    patch = diff_mod.assemble_patch(files)
    contents: dict[str, bytes] = {
        "pr-metadata.json": json_bytes(pr_metadata),
        "files.json": json_bytes([f.to_json() for f in files]),
        "diff.patch": patch,
    }
    if policy is not None:
        contents["policy.md"] = policy

    diff_sha = sha256_hex(patch)
    policy_blob = git_blob_sha(policy) if policy is not None else None
    snapshot_id = sha256_hex(
        "\n".join(
            [
                "ai-review-snapshot-v1",
                snap.repository.lower(),
                str(snap.number),
                snap.base_sha,
                snap.head_sha,
                merge_base_sha,
                policy_commit_sha,
                policy_blob or "-",
                diff_sha,
            ]
        ).encode("utf-8")
    )
    manifest = {
        "bundle_schema_version": limits_mod.BUNDLE_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "repository": snap.repository,
        "pr_number": snap.number,
        "base_sha": snap.base_sha,
        "head_sha": snap.head_sha,
        "merge_base_sha": merge_base_sha,
        "is_fork": snap.is_fork,
        "policy": {
            "path": policy_path,
            "source": "default_branch",
            "commit_sha": policy_commit_sha,
            "blob_sha": policy_blob,
            "present": policy is not None,
            "bytes": len(policy) if policy is not None else 0,
        },
        "diff": {
            "sha256": diff_sha,
            "bytes": len(patch),
            "file_count": len(files),
            "included": sum(1 for f in files if f.patch is not None),
            "excluded": sum(1 for f in files if f.excluded is not None),
            "binary": sum(1 for f in files if f.binary and f.excluded is None),
        },
        "limits": limits.as_dict(),
        "snapshot_id": snapshot_id,
        "files": {
            name: {"sha256": sha256_hex(data), "bytes": len(data)} for name, data in sorted(contents.items())
        },
    }
    contents["manifest.json"] = json_bytes(manifest)
    contents["run.json"] = json_bytes(run_info)
    for name, data in contents.items():
        (output_dir / name).write_bytes(data)
    return manifest


def write_github_output(path: str, values: dict[str, str]) -> None:
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value or "\r" in value:
            raise PrepareError(f"refusing to write unsafe GITHUB_OUTPUT entry {key}")
        lines.append(f"{key}={value}\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


# -- orchestration -----------------------------------------------------------------


def prepare(
    *,
    repository: str,
    pr_number: str | int,
    output_dir: Path,
    workdir: Path,
    token: str | None,
    api_url: str = DEFAULT_API_URL,
    server_url: str = DEFAULT_SERVER_URL,
    policy_path: str = DEFAULT_POLICY_PATH,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    transport: gh.Transport | None = None,
    runner_factory=None,
    remote_url: str | None = None,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    owner, name = gh.validate_repository(repository)
    number = gh.validate_pr_number(pr_number)
    if not policy_path or limits_mod.forbidden_path_reason(policy_path):
        raise PrepareError("policy path is invalid")

    client = gh.GitHubClient(api_url, token, transport=transport, max_response_bytes=limits.max_api_response_bytes)

    # 1-3. PR metadata, SHA pinning.
    pr = client.get_pull(owner, name, number)
    if pr is None:
        raise PrepareError(f"pull request #{number} does not exist in {owner}/{name}")
    snap = extract_snapshot(pr, f"{owner}/{name}", number)
    limits_mod.check_limit("changed files (api)", snap.changed_files, limits.max_changed_files)
    if snap.changed_files == 0:
        raise PrepareError("pull request has no changed files")
    log(f"pr #{number} head={snap.head_sha} base={snap.base_sha} fork={snap.is_fork}")
    pr_metadata = build_pr_metadata(pr, snap, limits)

    # 4. Policy from the default branch's pinned commit (never the PR head).
    policy_commit_sha, policy = load_policy(client, owner, name, snap, policy_path, limits)
    log(f"policy commit={policy_commit_sha} present={policy is not None}")

    # 3 (cont.) merge-base between fixed SHAs.
    merge_base_sha = client.get_merge_base_sha(owner, name, snap.base_sha, snap.head_sha)
    if merge_base_sha is None:
        raise PrepareError("no common history between base and head (merge-base failed)")
    if merge_base_sha == snap.head_sha:
        raise PrepareError("head is already contained in base; nothing to review")
    log(f"merge-base={merge_base_sha}")

    # 5-6. Fetch exactly two commits and diff them.
    if runner_factory is None:
        runner = diff_mod.GitRunner(workdir, limits)
    else:
        runner = runner_factory(workdir, limits)
    runner.init_bare()
    url = remote_url or build_remote_url(server_url, owner, name, with_username=bool(token))
    runner.fetch_snapshot(url, f"refs/pull/{number}/head", merge_base_sha, token)
    fetched_head = runner.resolve_commit(diff_mod.HEAD_REF)
    if fetched_head != snap.head_sha:
        raise PrepareError("pull request head moved during fetch; snapshot is stale")
    if runner.resolve_commit(diff_mod.MERGE_BASE_REF) != merge_base_sha:
        raise PrepareError("fetched merge-base does not match the recorded SHA")
    files = diff_mod.collect_changes(runner, merge_base_sha, snap.head_sha, limits)
    if not files:
        raise PrepareError("diff between merge-base and head is empty")

    # Re-verify the snapshot after the expensive steps.
    latest = client.get_pull(owner, name, number)
    if latest is None:
        raise PrepareError("pull request disappeared during prepare")
    latest_snap = extract_snapshot(latest, f"{owner}/{name}", number)
    if latest_snap.head_sha != snap.head_sha:
        raise PrepareError("pull request head changed during prepare; snapshot is stale")
    if latest_snap.base_sha != snap.base_sha:
        raise PrepareError("pull request base changed during prepare; snapshot is stale")

    # 7-8. Bundle.
    run_id = run_env.get("GITHUB_RUN_ID", "")
    run_info = {
        "run_id": run_id if re.fullmatch(r"[0-9]{1,20}", run_id or "") else None,
        "git_version": runner.version(),
        "python_version": platform.python_version(),
    }
    manifest = write_bundle(
        output_dir,
        snap=snap,
        merge_base_sha=merge_base_sha,
        policy_path=policy_path,
        policy_commit_sha=policy_commit_sha,
        policy=policy,
        pr_metadata=pr_metadata,
        files=files,
        limits=limits,
        run_info=run_info,
    )
    log(
        "bundle written: files={file_count} included={included} excluded={excluded} binary={binary} diff_bytes={bytes}".format(
            **manifest["diff"]
        )
    )

    github_output = run_env.get("GITHUB_OUTPUT")
    if github_output:
        write_github_output(
            github_output,
            {
                "head_sha": snap.head_sha,
                "base_sha": snap.base_sha,
                "merge_base_sha": merge_base_sha,
                "policy_sha": policy_commit_sha,
                "diff_sha256": manifest["diff"]["sha256"],
                "snapshot_id": manifest["snapshot_id"],
                "bundle_dir": str(output_dir),
            },
        )
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True, help="owner/name of the base repository")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path, help="private scratch directory for the bare repository")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--policy-path", default=DEFAULT_POLICY_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    token = os.environ.get(TOKEN_ENV) or None
    try:
        prepare(
            repository=args.repository,
            pr_number=args.pr_number,
            output_dir=args.output_dir,
            workdir=args.workdir,
            token=token,
            api_url=args.api_url,
            server_url=args.server_url,
            policy_path=args.policy_path,
        )
    except (PrepareError, gh.ValidationError, gh.GitHubError, diff_mod.GitError, limits_mod.LimitExceeded) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}: {err}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
