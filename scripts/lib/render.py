"""Deterministic Markdown rendering for the publish step.

The publisher never interprets the review: it renders a fixed template from an
already validated result document. Every string that originates from the model
passes through :func:`escape_markdown`, which removes the Markdown and HTML
constructs that could reach outside the comment (raw HTML, images, links,
@mentions, issue cross-references).

The first line is the publisher marker. It carries the snapshot ID, so the same
snapshot updates its own comment while a different snapshot gets a new one.

Standard library only.
"""

from __future__ import annotations

import re

from . import limits as limits_mod

MARKER_VERSION = "v1"
MARKER_PREFIX = f"<!-- ai-cross-pr-review:{MARKER_VERSION} snapshot="
MARKER_RE = re.compile(rf"^<!-- ai-cross-pr-review:{MARKER_VERSION} snapshot=([0-9a-f]{{64}}) -->$")

TITLE = "## AI Review (Claude)"
FOOTER = (
    "_このコメントは決定論的publisherが生成した。AIはPR番号・投稿先・SHA・merge可否を決定しない。_"
)

# Zero-width space. Breaks @mention and #123 autolinking without dropping text.
_ZWSP = "​"

# Markdown constructs that can change the structure of the comment or emit HTML.
_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>~|#])")
_AUTOLINK_RE = re.compile(r"([@#])(?=[A-Za-z0-9_])")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|mailto):|(?<![@\w])www\.")
# A line of only dashes or equals turns the previous line into a heading.
_SETEXT_RE = re.compile(r"^([-=]+)[ \t]*$", re.MULTILINE)


class RenderError(Exception):
    """The result cannot be rendered within the comment limit."""


def marker(snapshot_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id or ""):
        raise RenderError("snapshot id is not a 64-hex digest")
    return f"{MARKER_PREFIX}{snapshot_id} -->"


def read_marker(body: object) -> str | None:
    """Return the snapshot ID when ``body`` starts with our marker, else None.

    Only the first line is considered, so a marker quoted anywhere else in a
    comment written by someone else does not make that comment a publish target.
    """
    if not isinstance(body, str):
        return None
    match = MARKER_RE.match(body.split("\n", 1)[0].rstrip("\r"))
    return match.group(1) if match else None


def escape_markdown(text: object) -> str:
    """Neutralize model-derived text: no HTML, no links, no mentions."""
    if not isinstance(text, str):
        return ""
    escaped = _ESCAPE_RE.sub(r"\\\1", text)
    escaped = _SETEXT_RE.sub(r"\\\1", escaped)
    escaped = _AUTOLINK_RE.sub(rf"\1{_ZWSP}", escaped)

    def break_url(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.lower() == "www.":
            return f"{value[:-1]}{_ZWSP}."
        return f"{value}{_ZWSP}"

    return _URL_RE.sub(break_url, escaped)


def inline(text: object) -> str:
    """Escape text that must stay on one line (headings, bullets)."""
    return " ".join(escape_markdown(text).split("\n"))


def code(text: object) -> str:
    """Render a short value as a code span, falling back to escaped text."""
    value = text if isinstance(text, str) else ""
    if not value:
        return "`-`"
    if "`" in value or "\n" in value:
        return escape_markdown(value)
    return f"`{value}`"


def _location(finding: dict) -> str:
    path = finding["path"]
    line = finding.get("line")
    return code(f"{path}:{line}" if isinstance(line, int) else path)


def _finding_block(index: int, finding: dict) -> list[str]:
    return [
        f"#### {index}. {inline(finding['title'])}",
        "",
        "- severity: {s} / confidence: {c} / category: {g}".format(
            s=code(finding["severity"]), c=code(finding["confidence"]), g=code(finding["category"])
        ),
        f"- location: {_location(finding)}",
        "",
        escape_markdown(finding["detail"]),
        "",
    ]


def _header(document: dict) -> list[str]:
    snapshot = document["snapshot"]
    run = document["run"]
    lines = [
        marker(snapshot["snapshot_id"]),
        TITLE,
        "",
        f"Reviewed commit: {code(snapshot['head_sha'])}",
        "",
        f"- Base: {code(snapshot['base_sha'])} / Merge-base: {code(snapshot['merge_base_sha'])}",
        f"- Diff SHA-256: {code(snapshot['diff_sha256'])}",
        f"- Review policy commit: {code(snapshot.get('policy_commit_sha'))}",
        "- Provider: {p} / model: {m} / effort: {e}".format(
            p=code(run.get("provider")), m=code(run.get("model_requested")), e=code(run.get("effort"))
        ),
        f"- CLI: {code(run.get('cli_version'))} / run ID: {code(run.get('run_id'))}"
        f" / framework: {code(document.get('framework_version'))}",
        "",
    ]
    return lines


def _notes(document: dict, omitted: int) -> list[str]:
    normalization = document.get("normalization") or {}
    notes = []
    dropped = normalization.get("dropped_findings")
    if isinstance(dropped, list) and dropped:
        notes.append(f"- schema検証で破棄したfinding: {len(dropped)}件")
    for key, label in (("excluded_files", "diffから除外したファイル"), ("redactions", "出力から除去したcredentialらしき値")):
        value = normalization.get(key)
        if isinstance(value, int) and value > 0:
            notes.append(f"- {label}: {value}件")
    if omitted > 0:
        notes.append(f"- コメント長の上限により省略したfinding: {omitted}件")
    if not notes:
        return []
    return ["### Notes", "", *notes, ""]


def render_comment(document: dict, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS) -> str:
    """Render the comment body, dropping the least severe findings if needed.

    Findings arrive sorted by severity, so truncation always keeps the most
    severe ones and states how many were omitted.
    """
    findings = document["review"]["findings"]
    for keep in range(len(findings), -1, -1):
        body = _render(document, findings[:keep], omitted=len(findings) - keep)
        if len(body) <= limits.max_comment_chars:
            return body
    raise RenderError("rendered comment exceeds the limit even without findings")


def _render(document: dict, findings: list[dict], *, omitted: int) -> str:
    review = document["review"]
    lines = _header(document)

    lines += ["### Summary", "", escape_markdown(review["summary"]), ""]

    total = len(findings) + omitted
    lines += [f"### Findings ({total})", ""]
    if not findings:
        lines += ["レビュー対象のdiffに報告すべきfindingはなかった。" if total == 0 else "_すべてのfindingが省略された。_", ""]
    for index, finding in enumerate(findings, start=1):
        lines += _finding_block(index, finding)

    limitations = review.get("limitations") or []
    if limitations:
        lines += ["### Limitations", ""]
        lines += [f"- {inline(item)}" for item in limitations]
        lines += [""]

    lines += _notes(document, omitted)
    lines += ["---", "", FOOTER]
    return "\n".join(lines) + "\n"
