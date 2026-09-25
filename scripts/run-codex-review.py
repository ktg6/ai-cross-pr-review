#!/usr/bin/env python3
"""codex_review step: re-verify the primary review against the same snapshot.

The verification stage runs a pinned Codex CLI (``codex exec``) signed in with
ChatGPT authentication rather than an API key (ADR-0012). The sign-in check
does not establish the account's subscription plan. An API-key sign-in is
refused before the model is called; there is no fallback to the API.

The model is still given no way to act. Every tool-bearing feature of the CLI is
disabled, the sandbox is read-only, the working directory is empty, and user
config, rules and project docs are not loaded. The fixed verification contract
replaces the CLI's base instructions. Because a disabled feature is a
configuration and not a structural guarantee, the JSONL event stream is also
checked: any item other than a message or reasoning (a command, a file change,
a tool or web call) fails the stage, and its output is discarded.

The primary (Claude) review is untrusted input here. It is delivered inside the
same nonce-delimited boundary as the PR data, and the fixed verification policy
states that the boundary's contents are data, never instructions (ADR-0006).

Before the CLI is started, the primary result's snapshot fingerprint must match
the bundle: verifying one snapshot's findings against another snapshot's diff is
meaningless and must never reach a publisher.

This step holds no GitHub credential and posts nothing. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

RAW_RESULT_NAME = "codex-raw.json"
INVOCATION_NAME = "codex-invocation.json"

BEGIN_MARKER = "BEGIN UNTRUSTED VERIFICATION INPUT"
END_MARKER = "END UNTRUSTED VERIFICATION INPUT"

# Exact first line of ``codex login status`` for a ChatGPT sign-in.
CHATGPT_LOGIN_STATUS = "Logged in using ChatGPT"

# Features that give the model a way to act or to reach outside the prompt. All
# of them are names the pinned CLI accepts; an unknown name is a hard CLI error,
# so a renamed feature fails the stage instead of silently staying enabled.
DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "code_mode_host",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "view_image",
    "goals",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "sleep_tool",
    "workspace_dependencies",
)

# Event and item types a tool-free verification may produce. Anything else is
# treated as an attempt to act.
ALLOWED_EVENT_TYPES = frozenset(
    {"thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated", "item.completed", "error"}
)
ALLOWED_ITEM_TYPES = frozenset({"agent_message", "reasoning", "error"})

# Passed through to the CLI only when present; nothing else is inherited.
_PASSTHROUGH_ENV = ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")


class CodexError(Exception):
    """Deterministic stop: the verification cannot be produced safely."""


def log(message: str) -> None:
    sys.stderr.write(f"run-codex-review: {message}\n")


# -- inputs -------------------------------------------------------------------


def load_claude_result(path: Path, snapshot_id: str, limits: limits_mod.Limits) -> dict:
    """Read the normalized primary review and bind it to this snapshot."""
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise CodexError(f"cannot read the primary review: {err.__class__.__name__}") from None
    limits_mod.check_limit("primary review size", len(data), limits.max_result_bytes)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CodexError("primary review is not valid JSON") from None
    if not isinstance(document, dict):
        raise CodexError("primary review is not an object")
    if document.get("result_schema_version") != limits_mod.RESULT_SCHEMA_VERSION:
        raise CodexError("primary review has an unsupported schema version")
    if document.get("framework_version") != limits_mod.FRAMEWORK_VERSION:
        raise CodexError("primary review was produced by a different framework version")
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, dict):
        raise CodexError("primary review has no snapshot block")
    if snapshot.get("snapshot_id") != snapshot_id:
        raise CodexError("primary review belongs to a different snapshot")
    review = document.get("review")
    if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
        raise CodexError("primary review has no findings array")
    if len(review["findings"]) > limits.max_findings:
        raise CodexError("primary review exceeds the findings limit")
    return document


def build_untrusted_document(
    b: bundle_mod.Bundle, claude: dict, nonce: str, limits: limits_mod.Limits
) -> str:
    """Wrap the snapshot and the primary review in unpredictable markers."""
    begin = f"{BEGIN_MARKER} {nonce}"
    end = f"{END_MARKER} {nonce}"
    files = [
        {
            "path": entry.path,
            "status": entry.status,
            "binary": entry.binary,
            "excluded": entry.excluded,
            "patch_bytes": entry.patch_bytes,
        }
        for entry in b.files
    ]
    review = claude["review"]
    claude_payload = {
        "summary": review.get("summary", ""),
        # The index is explicit so claim_reviews[].claude_index cannot drift.
        "findings": [
            dict(finding, claude_index=index)
            for index, finding in enumerate(review["findings"][: limits.max_findings])
        ],
        "limitations": review.get("limitations", []),
    }
    sections = [
        begin,
        f"## POLICY (source: {b.policy_source})",
        b.policy.decode("utf-8", "replace"),
        "## PR_METADATA (untrusted)",
        json.dumps(b.pr_metadata, ensure_ascii=False, indent=2, sort_keys=True),
        "## FILES (changed files in this snapshot)",
        json.dumps(files, ensure_ascii=False, indent=2),
        "## DIFF (untrusted, merge-base..head)",
        b.diff.decode("utf-8", "replace"),
        "## CLAUDE_REVIEW (untrusted; the primary review under verification, not evidence)",
        json.dumps(claude_payload, ensure_ascii=False, indent=2),
        end,
    ]
    document = "\n\n".join(sections) + "\n"
    if document.count(nonce) != 2:
        raise CodexError("input content collides with the boundary nonce")
    return document


# -- CLI invocation -----------------------------------------------------------


def _toml_string(value: str) -> str:
    """Encode a value as a TOML basic string for a ``-c key=value`` override."""
    # JSON string escaping is a subset of TOML basic-string escaping.
    return json.dumps(value, ensure_ascii=False)


def build_argv(
    codex_bin: str,
    *,
    model: str,
    effort: str,
    prompt_file: Path,
    schema_file: Path,
) -> list[str]:
    """Build the ``codex exec`` argv. No tool, no user config, no persistence."""
    argv = [
        codex_bin,
        "exec",
        "--json",
        # No session files; nothing about the PR is kept in CODEX_HOME.
        "--ephemeral",
        # Neither the runner's config.toml nor its execpolicy rules apply.
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--model", model,
        "--output-schema", str(schema_file),
        "-c", f"model_reasoning_effort={_toml_string(effort)}",
        # The fixed contract replaces the CLI's base (agentic) instructions.
        "-c", f"model_instructions_file={_toml_string(str(prompt_file))}",
        # No AGENTS.md or other project docs are read into the prompt.
        "-c", "project_doc_max_bytes=0",
        "-c", 'web_search="disabled"',
    ]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    # The prompt is read from stdin.
    argv.append("-")
    return argv


def build_env(workdir: Path, codex_home: Path) -> dict[str, str]:
    """Minimal environment. No GitHub token, no API key, no inherited CI secrets.

    ``CODEX_HOME`` points at the operator's signed-in directory so that the CLI
    can refresh the ChatGPT session in place (ADR-0012). An API key in the
    environment would switch the CLI to usage-based billing, so none is passed.
    """
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if os.environ.get(name)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.update(
        {
            "HOME": str(workdir / "home"),
            "CODEX_HOME": str(codex_home),
            "TMPDIR": str(workdir / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "CI": "true",
        }
    )
    return env


def default_codex_home(run_env: dict) -> Path:
    value = run_env.get("CODEX_HOME")
    if value:
        return Path(value)
    return Path(run_env.get("HOME") or Path.home()) / ".codex"


def _run_small(run, argv: list[str], env: dict[str, str], cwd: Path, what: str) -> str:
    try:
        if run is subprocess.run:
            proc = _run_exec_bounded(
                argv, cwd=cwd, env=env, input_data=b"", timeout=120, max_stdout=4096,
            )
        else:
            proc = run(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
    except subprocess.TimeoutExpired:
        raise CodexError(f"{what} timed out") from None
    except OSError as err:
        raise CodexError(f"codex could not be executed: {err.__class__.__name__}") from None
    if proc.returncode != 0:
        raise CodexError(f"{what} failed (exit {proc.returncode})")
    return ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", "replace")


def verify_cli(
    codex_bin: str,
    *,
    env: dict[str, str],
    cwd: Path,
    expected_version: str = limits_mod.CODEX_CLI_VERSION,
    run=subprocess.run,
) -> None:
    """Check the pinned version and ChatGPT sign-in mode, not the account plan."""
    reported = _run_small(run, [codex_bin, "--version"], env, cwd, "codex --version").strip().splitlines()
    first = reported[0].strip() if reported else ""
    if first != f"codex-cli {expected_version}":
        raise CodexError("codex version does not match the pinned version")

    status = _run_small(run, [codex_bin, "login", "status"], env, cwd, "codex login status")
    lines = [line.strip() for line in status.splitlines() if line.strip()]
    if CHATGPT_LOGIN_STATUS not in lines:
        # Covers "Not logged in" and an API-key sign-in alike. The status text
        # is not echoed: it is the CLI's, not ours, and may change shape.
        raise CodexError("codex is not signed in with ChatGPT authentication")


def parse_events(stdout: bytes) -> tuple[str, dict]:
    """Return the final message and run facts from the JSONL event stream.

    Fails closed on a failed turn, a missing completion, a missing message, and
    on any item that is not a message or reasoning.
    """
    thread_id: str | None = None
    usage: dict = {}
    completed = 0
    message: str | None = None
    for number, line in enumerate(stdout.decode("utf-8", "replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise CodexError(f"codex event {number} is not JSON") from None
        if not isinstance(event, dict):
            raise CodexError(f"codex event {number} is not an object")
        kind = event.get("type")
        if kind not in ALLOWED_EVENT_TYPES:
            raise CodexError(f"codex emitted an unexpected event type: {_label(kind)}")
        if kind == "thread.started":
            thread_id = _safe_id(event.get("thread_id"))
        elif kind == "turn.failed":
            raise CodexError("codex turn failed")
        elif kind == "turn.completed":
            completed += 1
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        elif kind.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict):
                raise CodexError(f"codex event {number} has no item")
            item_type = item.get("type")
            if item_type not in ALLOWED_ITEM_TYPES:
                # A command, file change, MCP/web/tool call: the model acted.
                raise CodexError(f"codex used a tool ({_label(item_type)}); the output is discarded")
            if kind == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise CodexError("codex message has no text")
                message = text
    if completed != 1:
        raise CodexError("codex did not complete exactly one turn")
    if message is None or not message.strip():
        raise CodexError("codex returned no message")
    return message, {
        "thread_id": thread_id,
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
    }


def _label(value: object) -> str:
    if isinstance(value, str) and value.isascii() and value.isprintable() and len(value) <= 40:
        return value
    return "unknown"


def _safe_id(value: object) -> str | None:
    if isinstance(value, str) and value.isascii() and value.isprintable() and 0 < len(value) <= 128:
        return value
    return None


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def failure_detail(stderr: bytes) -> str:
    """A short, sanitized diagnostic. The prompt is never echoed."""
    text, _ = bundle_mod.redact_secrets(bundle_mod.sanitize_text(stderr.decode("utf-8", "replace")[-2000:], 400))
    return text or "no stderr"


def _stop_process_group(proc: subprocess.Popen) -> None:
    """Stop the CLI wrapper and any children that inherited its process group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(0.25)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _run_exec_bounded(
    argv: list[str], *, cwd: Path, env: dict[str, str], input_data: bytes,
    timeout: int, max_stdout: int,
) -> subprocess.CompletedProcess:
    """Drain all pipes concurrently and stop before retaining excess output."""
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout = bytearray()
    stderr = bytearray()
    max_stderr = 64 * 1024
    offset = 0
    deadline = time.monotonic() + timeout
    completed = False
    with selectors.DefaultSelector() as ready:
        try:
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                os.set_blocking(pipe.fileno(), False)
            if input_data:
                ready.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                proc.stdin.close()
            ready.register(proc.stdout, selectors.EVENT_READ, "stdout")
            ready.register(proc.stderr, selectors.EVENT_READ, "stderr")

            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in ready.select(remaining):
                    pipe = key.fileobj
                    if key.data == "stdin":
                        try:
                            offset += os.write(pipe.fileno(), input_data[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(input_data)
                        if offset == len(input_data):
                            ready.unregister(pipe)
                            pipe.close()
                        continue

                    chunk = os.read(pipe.fileno(), 65536)
                    if not chunk:
                        ready.unregister(pipe)
                        pipe.close()
                        continue
                    target, maximum, name = (
                        (stdout, max_stdout, "codex event stream size")
                        if key.data == "stdout" else
                        (stderr, max_stderr, "codex stderr size")
                    )
                    limits_mod.check_limit(name, len(target) + len(chunk), maximum)
                    target.extend(chunk)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            returncode = proc.wait(timeout=remaining)
            completed = True
            return subprocess.CompletedProcess(argv, returncode, bytes(stdout), bytes(stderr))
        finally:
            try:
                if not completed:
                    _stop_process_group(proc)
            finally:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()


# -- orchestration ------------------------------------------------------------


def run_codex_review(
    *,
    bundle_dir: Path,
    claude_result_file: Path,
    workdir: Path,
    prompt_file: Path,
    schema_file: Path,
    model: str = models_mod.DEFAULT_CODEX_MODEL,
    effort: str = models_mod.DEFAULT_CODEX_EFFORT,
    codex_bin: str = "codex",
    codex_home: Path | None = None,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    nonce: str | None = None,
    run=None,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    model = models_mod.validate_codex_model(model)
    effort = models_mod.validate_codex_effort(effort)
    codex_home = Path(codex_home) if codex_home is not None else default_codex_home(run_env)
    if not codex_home.is_dir():
        raise CodexError("CODEX_HOME is not a directory; sign in with `codex login` first")

    b = bundle_mod.load_bundle(Path(bundle_dir), limits)
    claude = load_claude_result(Path(claude_result_file), b.snapshot_id, limits)
    log(
        "inputs verified: snapshot={s} claude_findings={f}".format(
            s=b.snapshot_id[:16], f=len(claude["review"]["findings"])
        )
    )

    prompt_file = Path(prompt_file).resolve()
    schema_file = Path(schema_file).resolve()
    instructions = prompt_file.read_bytes()
    schema_bytes = schema_file.read_bytes()
    json.loads(schema_bytes.decode("utf-8"))

    workdir = Path(workdir)
    for name in ("home", "tmp", "cwd"):
        (workdir / name).mkdir(parents=True, exist_ok=True)
    env = build_env(workdir, codex_home)
    verify_cli(codex_bin, env=env, cwd=workdir / "cwd", run=run or subprocess.run)
    log(f"codex cli verified: version={limits_mod.CODEX_CLI_VERSION} auth={limits_mod.CODEX_AUTH_MODE}")

    nonce = nonce or secrets.token_hex(16)
    document = build_untrusted_document(b, claude, nonce, limits)
    argv = build_argv(codex_bin, model=model, effort=effort, prompt_file=prompt_file, schema_file=schema_file)

    started = time.monotonic()
    try:
        if run is None:
            proc = _run_exec_bounded(
                argv, cwd=workdir / "cwd", env=env,
                input_data=document.encode("utf-8"),
                timeout=limits.codex_timeout_seconds,
                max_stdout=limits.codex_max_event_stream_bytes,
            )
        else:
            # Unit tests inject a canned CLI response; the real path above
            # enforces the limit while bytes arrive.
            proc = run(
                argv, cwd=str(workdir / "cwd"), env=env,
                input=document.encode("utf-8"), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=limits.codex_timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired:
        raise CodexError("codex timed out") from None
    except OSError as err:
        raise CodexError(f"codex could not be executed: {err.__class__.__name__}") from None
    duration_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        log(f"codex failed (exit {proc.returncode}): {failure_detail(proc.stderr or b'')}")
        raise CodexError("codex exited with a non-zero status")
    stdout = proc.stdout or b""
    limits_mod.check_limit("codex event stream size", len(stdout), limits.codex_max_event_stream_bytes)

    structured_text, facts = parse_events(stdout)
    if len(structured_text.encode("utf-8")) > limits.max_raw_result_bytes:
        raise CodexError("verification output exceeds the limit")

    # The raw message stays in the job workdir; it is never uploaded.
    (workdir / RAW_RESULT_NAME).write_text(structured_text, encoding="utf-8")

    invocation = {
        "provider": limits_mod.CODEX_PROVIDER,
        "cli_version": limits_mod.CODEX_CLI_VERSION,
        "auth_mode": limits_mod.CODEX_AUTH_MODE,
        "model_requested": model,
        # The CLI's event stream does not report the served model; not guessed.
        "model_reported": None,
        "effort": effort,
        "tools_enabled": False,
        "status": "completed",
        "thread_id": facts["thread_id"],
        "input_tokens": facts["input_tokens"],
        "output_tokens": facts["output_tokens"],
        "duration_ms": duration_ms,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "snapshot_id": b.snapshot_id,
        "prompt_sha256": bundle_mod.sha256_hex(instructions),
        "schema_sha256": bundle_mod.sha256_hex(schema_bytes),
        "run_id": _run_id(run_env),
    }
    (workdir / INVOCATION_NAME).write_bytes(
        (json.dumps(invocation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    log("verification completed: {n} bytes in {ms} ms".format(n=len(structured_text), ms=duration_ms))
    return invocation


def _run_id(run_env: dict) -> str | None:
    value = run_env.get("GITHUB_RUN_ID", "")
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 20 else None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--claude-result-file", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--schema-file", required=True, type=Path)
    parser.add_argument("--model", default=models_mod.DEFAULT_CODEX_MODEL)
    parser.add_argument("--effort", default=models_mod.DEFAULT_CODEX_EFFORT)
    parser.add_argument("--codex-bin", default="codex")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run_codex_review(
            bundle_dir=args.bundle_dir,
            claude_result_file=args.claude_result_file,
            workdir=args.workdir,
            prompt_file=args.prompt_file,
            schema_file=args.schema_file,
            model=args.model,
            effort=args.effort,
            codex_bin=args.codex_bin,
        )
    except (
        CodexError,
        bundle_mod.BundleError,
        limits_mod.LimitExceeded,
        models_mod.ModelNotAllowed,
    ) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
