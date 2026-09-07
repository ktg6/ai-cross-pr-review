"""Bare-git snapshot fetch and per-file diff generation.

Security model (ADR-0002):

* A fresh bare repository is created per run; nothing is checked out and no
  hook, external diff, textconv, submodule or LFS filter can execute.
* Only the two commits the review needs (merge-base and PR head) are fetched,
  with ``--depth=1`` and explicit refspecs. The fetched head is verified against
  the head SHA recorded from the API, so a moving PR head aborts the run.
* Every git invocation is an argv list with ``--literal-pathspecs``; untrusted
  filenames are passed after ``--`` and never touch a shell.
* Credentials reach git only through ``GIT_ASKPASS`` and a private environment
  variable of the fetch subprocess; they never appear in argv or on disk.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import limits as limits_mod
from .limits import Limits, forbidden_path_reason

GIT = "git"
HEAD_REF = "refs/ai-review/head"
MERGE_BASE_REF = "refs/ai-review/merge-base"
ASKPASS_PASSWORD_ENV = "AI_REVIEW_GIT_PASSWORD"

_ASKPASS_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import os, sys\n"
    f"sys.stdout.write(os.environ.get({ASKPASS_PASSWORD_ENV!r}, '') + '\\n')\n"
)

MODE_SUBMODULE = "160000"
MODE_SYMLINK = "120000"


class GitError(Exception):
    """A git command failed or produced output that could not be trusted."""


def _hardening_config(allowed_protocols: tuple[str, ...]) -> list[str]:
    settings = [
        "core.hooksPath=" + os.devnull,
        "credential.helper=",
        "fetch.recurseSubmodules=no",
        "submodule.recurse=false",
        "core.fsmonitor=false",
        "color.ui=never",
        "diff.noprefix=false",
        "diff.mnemonicPrefix=false",
        "diff.relative=false",
        "diff.renames=true",
        "protocol.allow=never",
    ]
    for proto in allowed_protocols:
        settings.append(f"protocol.{proto}.allow=always")
    args: list[str] = []
    for item in settings:
        args.extend(["-c", item])
    return args


@dataclass
class RawEntry:
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    status: str  # single letter; score stripped
    path: bytes
    previous_path: bytes | None = None


@dataclass
class ChangedFile:
    status: str
    path: str
    previous_path: str | None
    old_mode: str
    new_mode: str
    kind: str  # file | symlink | submodule
    binary: bool = False
    additions: int | None = None
    deletions: int | None = None
    excluded: str | None = None
    patch: bytes | None = field(default=None, repr=False)

    @property
    def patch_bytes(self) -> int:
        return len(self.patch) if self.patch is not None else 0

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "path": self.path,
            "previous_path": self.previous_path,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "kind": self.kind,
            "binary": self.binary,
            "additions": self.additions,
            "deletions": self.deletions,
            "excluded": self.excluded,
            "patch_bytes": self.patch_bytes,
        }


def parse_raw_z(data: bytes) -> list[RawEntry]:
    """Parse ``git diff --raw -z`` output."""
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[RawEntry] = []
    i = 0
    while i < len(tokens):
        header = tokens[i]
        if not header.startswith(b":"):
            raise GitError("unexpected token in raw diff output")
        fields = header[1:].split(b" ")
        if len(fields) != 5:
            raise GitError("malformed raw diff header")
        old_mode, new_mode, old_sha, new_sha, status_field = (f.decode("ascii") for f in fields)
        status = status_field[:1]
        if status in ("R", "C"):
            if i + 2 >= len(tokens):
                raise GitError("truncated rename entry in raw diff output")
            entries.append(RawEntry(old_mode, new_mode, old_sha, new_sha, status, tokens[i + 2], tokens[i + 1]))
            i += 3
        else:
            if i + 1 >= len(tokens):
                raise GitError("truncated entry in raw diff output")
            entries.append(RawEntry(old_mode, new_mode, old_sha, new_sha, status, tokens[i + 1]))
            i += 2
    return entries


def parse_numstat_z(data: bytes) -> dict[bytes, tuple[int | None, int | None]]:
    """Parse ``git diff --numstat -z`` into {new_path: (added, deleted)}.

    ``None`` counts mean git reported ``-`` (binary).
    """
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    result: dict[bytes, tuple[int | None, int | None]] = {}
    i = 0

    def _count(raw: bytes) -> int | None:
        if raw == b"-":
            return None
        try:
            return int(raw)
        except ValueError:
            raise GitError("malformed numstat count") from None

    while i < len(tokens):
        parts = tokens[i].split(b"\t", 2)
        if len(parts) != 3:
            raise GitError("malformed numstat entry")
        added, deleted, path = _count(parts[0]), _count(parts[1]), parts[2]
        if path == b"":
            if i + 2 >= len(tokens):
                raise GitError("truncated numstat rename entry")
            path = tokens[i + 2]
            i += 3
        else:
            i += 1
        result[path] = (added, deleted)
    return result


class GitRunner:
    """Runs hardened git commands against a private bare repository."""

    def __init__(
        self,
        workdir: Path,
        limits: Limits = limits_mod.DEFAULT_LIMITS,
        *,
        allowed_protocols: tuple[str, ...] = ("https",),
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.workdir = Path(workdir)
        self.repo_dir = self.workdir / "repo.git"
        self.home_dir = self.workdir / "home"
        self.askpass_path = self.workdir / "askpass.py"
        self.limits = limits
        self._config_args = _hardening_config(allowed_protocols)
        self._run = run

    # -- process management ----------------------------------------------

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home_dir),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            # Do not let diff/rev-parse lazily download blobs omitted by the
            # fetch filter. Missing oversized blobs must stop the run.
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
            "LC_ALL": "C",
        }
        if extra:
            env.update(extra)
        return env

    def git(
        self,
        args: list,
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> bytes:
        argv = [GIT, "--literal-pathspecs", *self._config_args, *args]
        try:
            proc = self._run(
                argv,
                cwd=str(cwd or self.repo_dir),
                env=self._env(env_extra),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.limits.git_command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        except OSError as err:
            raise GitError(f"git could not be executed: {err.__class__.__name__}") from None
        if proc.returncode != 0:
            detail = (proc.stderr or b"")[:2048].decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} failed (exit {proc.returncode}): {detail}")
        return proc.stdout or b""

    # -- repository lifecycle --------------------------------------------

    def init_bare(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.home_dir.mkdir(exist_ok=True)
        self.repo_dir.mkdir(exist_ok=False)
        # --template= (empty) prevents copying hook samples or any template config.
        self.git(["init", "--quiet", "--bare", "--template=", "--", str(self.repo_dir)], cwd=self.workdir)

    def _write_askpass(self) -> None:
        self.askpass_path.write_text(_ASKPASS_SOURCE, encoding="utf-8")
        self.askpass_path.chmod(stat.S_IRWXU)

    def fetch_snapshot(self, remote_url: str, head_refspec_source: str, merge_base_sha: str, password: str | None) -> None:
        """Fetch exactly the head and merge-base commits with depth 1."""
        refspecs = [
            f"+{head_refspec_source}:{HEAD_REF}",
            f"+{merge_base_sha}:{MERGE_BASE_REF}",
        ]
        env_extra: dict[str, str] = {}
        if password:
            self._write_askpass()
            env_extra["GIT_ASKPASS"] = str(self.askpass_path)
            env_extra[ASKPASS_PASSWORD_ENV] = password
        self.git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--no-auto-gc",
                "--depth=1",
                f"--filter=blob:limit={self.limits.max_blob_bytes}",
                "--",
                remote_url,
                *refspecs,
            ],
            timeout=self.limits.git_fetch_timeout_seconds,
            env_extra=env_extra,
        )

    def resolve_commit(self, ref: str) -> str:
        out = self.git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
        sha = out.decode("ascii", "replace").strip()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise GitError(f"could not resolve {ref} to a commit")
        return sha

    def version(self) -> str:
        return self.git(["--version"], cwd=self.workdir).decode("utf-8", "replace").strip()

    # -- diff primitives --------------------------------------------------

    _DIFF_FLAGS = ["--no-ext-diff", "--no-textconv", "--no-color", "--find-renames", "--full-index"]

    def raw_listing(self, old: str, new: str) -> list[RawEntry]:
        out = self.git(["diff", *self._DIFF_FLAGS, "--raw", "-z", "--no-abbrev", old, new, "--"])
        return parse_raw_z(out)

    def numstat(self, old: str, new: str) -> dict[bytes, tuple[int | None, int | None]]:
        out = self.git(["diff", *self._DIFF_FLAGS, "--numstat", "-z", old, new, "--"])
        return parse_numstat_z(out)

    def patch(self, old: str, new: str, paths: list[bytes]) -> bytes:
        return self.git(["diff", *self._DIFF_FLAGS, "--unified=3", old, new, "--", *paths])


# -- change collection -----------------------------------------------------


def _decode_path(raw: bytes) -> tuple[str, bool]:
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return raw.decode("utf-8", "backslashreplace"), False


def collect_changes(runner: GitRunner, merge_base_sha: str, head_sha: str, limits: Limits) -> list[ChangedFile]:
    """Build the per-file change list with patches, enforcing every limit."""
    entries = runner.raw_listing(merge_base_sha, head_sha)
    limits_mod.check_limit("changed files (git)", len(entries), limits.max_changed_files)
    entries.sort(key=lambda e: e.path)
    counts = runner.numstat(merge_base_sha, head_sha)

    files: list[ChangedFile] = []
    total = 0
    for entry in entries:
        path, path_ok = _decode_path(entry.path)
        prev, prev_ok = (None, True)
        if entry.previous_path is not None:
            prev, prev_ok = _decode_path(entry.previous_path)
        if entry.new_mode == MODE_SUBMODULE or entry.old_mode == MODE_SUBMODULE:
            kind = "submodule"
        elif entry.new_mode == MODE_SYMLINK or entry.old_mode == MODE_SYMLINK:
            kind = "symlink"
        else:
            kind = "file"
        added, deleted = counts.get(entry.path, (None, None))
        changed = ChangedFile(
            status=entry.status,
            path=path,
            previous_path=prev,
            old_mode=entry.old_mode,
            new_mode=entry.new_mode,
            kind=kind,
            binary=(added is None and deleted is None),
            additions=added,
            deletions=deleted,
        )

        if entry.status not in ("A", "M", "D", "R", "C", "T"):
            raise GitError(f"unsupported change status {entry.status!r} for {path}")
        if not path_ok or not prev_ok:
            changed.excluded = "non_utf8_path"
        else:
            reason = forbidden_path_reason(path) or (forbidden_path_reason(prev) if prev else None)
            if reason:
                changed.excluded = reason
            elif kind == "submodule":
                changed.excluded = "submodule"

        if changed.excluded is None and not changed.binary:
            pathspec = [entry.path] if entry.previous_path is None else [entry.previous_path, entry.path]
            patch = runner.patch(merge_base_sha, head_sha, pathspec)
            if not patch.startswith(b"diff --git "):
                raise GitError(f"unexpected patch output for {path}")
            limits_mod.check_limit(f"diff size of {path}", len(patch), limits.max_file_diff_bytes)
            total += len(patch)
            limits_mod.check_limit("total diff size", total, limits.max_diff_total_bytes)
            changed.patch = patch
        files.append(changed)
    return files


def assemble_patch(files: list[ChangedFile]) -> bytes:
    return b"".join(f.patch for f in files if f.patch is not None)
