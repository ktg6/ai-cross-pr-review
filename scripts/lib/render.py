"""Deterministic Markdown rendering for the report and publish steps.

Neither step interprets the review: both render a fixed template from an
already validated final document. Every string that originates from a model
passes through :func:`escape_markdown`, which removes the Markdown and HTML
constructs that could reach outside the comment (raw HTML, images, links,
@mentions, issue cross-references).

The comment's first line is the publisher marker. It carries the snapshot ID, so
the same snapshot updates its own comment while a different snapshot gets a new
one. The job summary carries no marker: it is never posted anywhere.

Standard library only.
"""

from __future__ import annotations

import re

from . import limits as limits_mod

MARKER_VERSION = "v1"
MARKER_PREFIX = f"<!-- ai-cross-pr-review:{MARKER_VERSION} snapshot="
MARKER_RE = re.compile(rf"^<!-- ai-cross-pr-review:{MARKER_VERSION} snapshot=([0-9a-f]{{64}}) -->$")

TITLE = "## AI Cross Review (Claude → Codex)"
FOOTER = (
    "_このコメントは決定論的publisherが生成した。AIはPR番号・投稿先・SHA・merge可否を決定しない。_"
)
NOT_PUBLISHABLE_NOTE = (
    "**この実行は完了しなかった。指摘がないことは、問題がないことを意味しない。**"
)

# Bucket display order and headings. The order is also the retention order when
# a comment must be shortened: later buckets are dropped first.
BUCKETS: tuple[tuple[str, str], ...] = (
    ("adopted", "Codexが採用したClaudeの指摘"),
    ("added", "Codexが追加した指摘"),
    ("deferred", "判断保留"),
    ("rejected", "不採用となったClaudeの指摘"),
    ("duplicates", "重複と判定されたClaudeの指摘"),
)
_BUCKET_ORDER = {name: index for index, (name, _label) in enumerate(BUCKETS)}
_SEVERITY_ORDER = {name: index for index, name in enumerate(limits_mod.SEVERITIES)}

# Zero-width space. Breaks @mention and #123 autolinking without dropping text.
_ZWSP = "​"

# Markdown constructs that can change the structure of the comment or emit HTML.
_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>~|#])")
_AUTOLINK_RE = re.compile(r"([@#])(?=[A-Za-z0-9_])")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|mailto):|(?<![@\w])www\.")
# A line of only dashes or equals turns the previous line into a heading.
_SETEXT_RE = re.compile(r"^([-=]+)[ \t]*$", re.MULTILINE)


class RenderError(Exception):
    """The result cannot be rendered within the size limit."""


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


def _location(entry: dict) -> str:
    path = entry["path"]
    line = entry.get("line")
    return code(f"{path}:{line}" if isinstance(line, int) else path)


def _entry_block(index: int, entry: dict) -> list[str]:
    lines = [
        f"#### {index}. {inline(entry['title'])}",
        "",
        "- severity: {s} / confidence: {c} / category: {g}".format(
            s=code(entry["severity"]), c=code(entry["confidence"]), g=code(entry["category"])
        ),
        f"- location: {_location(entry)}",
        f"- 指摘元: {code(entry['origin'])}",
    ]
    if isinstance(entry.get("claude_index"), int):
        lines.append(f"- 一次レビューindex: {code(str(entry['claude_index']))}")
    if isinstance(entry.get("duplicate_of"), int):
        lines.append(f"- 重複元index: {code(str(entry['duplicate_of']))}")
    lines += ["", escape_markdown(entry["detail"]), ""]
    if entry.get("rationale"):
        lines += ["**検証の根拠**", "", escape_markdown(entry["rationale"]), ""]
    if entry.get("suggested_fix"):
        lines += ["**修正案**", "", escape_markdown(entry["suggested_fix"]), ""]
    return lines


def _flag(value: object) -> str:
    return "`ok`" if value is True else "`ng`"


def _header(document: dict) -> list[str]:
    snapshot = document["snapshot"]
    request = document["request"]
    stages = document["stages"]
    verification = document["verification"]
    policy = "{p} (source: {s}, repository policy present: {e})".format(
        p=code(request["policy_path"]),
        s=code(snapshot["policy_source"]),
        e=code("true" if snapshot["policy_present"] else "false"),
    )
    lines = [
        TITLE,
        "",
        f"Reviewed commit: {code(snapshot['head_sha'])}",
        "",
        "- Repository: {r} / PR: {n} / fork: {f}".format(
            r=code(snapshot["repository"]),
            n=code(f"#{snapshot['pr_number']}"),
            f=code("true" if snapshot["is_fork"] else "false"),
        ),
        f"- Base: {code(snapshot['base_sha'])} / Merge-base: {code(snapshot['merge_base_sha'])}",
        f"- Diff SHA-256: {code(snapshot['diff_sha256'])}",
        f"- Review policy: {policy}",
        f"- Review policy commit: {code(snapshot.get('policy_commit_sha'))}",
        "- 一次レビュー(Claude): status {s} / 要求model {a} / 実使用model {b} / effort {e}".format(
            s=code(stages["claude"]["status"]),
            a=code(stages["claude"]["model_requested"]),
            b=code(stages["claude"]["model_reported"]),
            e=code(request["claude_effort"]),
        ),
        "- 再検証(Codex): status {s} / 要求model {a} / 実使用model {b} / effort {e}".format(
            s=code(stages["codex"]["status"]),
            a=code(stages["codex"]["model_requested"]),
            b=code(stages["codex"]["model_reported"]),
            e=code(request["codex_effort"]),
        ),
        "- 検証: schema {a} / snapshot {b} / fingerprint claude {c} codex {d} / 参照整合 {e} / 整形 {f}".format(
            a=_flag(verification["schema_valid"]),
            b=_flag(verification["snapshot_match"]),
            c=_flag(verification["claude_fingerprint_match"]),
            d=_flag(verification["codex_fingerprint_match"]),
            e=_flag(verification["reference_consistency"]),
            f=_flag(verification["finalized"]),
        ),
        "- 出力モード: {m} / 投稿可否: {p} / framework: {f}".format(
            m=code(request["output_mode"]),
            p=code("publishable" if document["publishable"] else "not-publishable"),
            f=code(document.get("framework_version")),
        ),
        "",
    ]
    if not document["publishable"]:
        lines += [NOT_PUBLISHABLE_NOTE, ""]
    return lines


def _selection_key(bucket: str, entry: dict) -> tuple[int, int]:
    return (_BUCKET_ORDER[bucket], _SEVERITY_ORDER.get(entry["severity"], len(_SEVERITY_ORDER)))


def _ordered_entries(document: dict) -> list[tuple[str, dict]]:
    """All entries, most important first, for size-driven truncation."""
    pairs = [(bucket, entry) for bucket, _label in BUCKETS for entry in document["review"][bucket]]
    return sorted(pairs, key=lambda item: _selection_key(item[0], item[1]))


def _notes(document: dict, omitted: int) -> list[str]:
    review = document["review"]
    notes = []
    if review.get("dropped"):
        notes.append(f"- schema検証・参照整合で破棄した項目: {review['dropped']}件")
    if review.get("redactions"):
        notes.append(f"- 出力から除去したcredentialらしき値: {review['redactions']}件")
    if omitted > 0:
        notes.append(f"- 長さの上限により省略した項目: {omitted}件")
    for name in ("claude", "codex"):
        detail = document["stages"][name].get("detail")
        if detail:
            notes.append(f"- {name} stage: {inline(detail)}")
    if not notes:
        return []
    return ["### Notes", "", *notes, ""]


def _body(document: dict, selected: list[tuple[str, dict]], *, omitted: int) -> list[str]:
    review = document["review"]
    lines = _header(document)
    lines += ["### Summary", "", escape_markdown(review["summary"]), ""]

    chosen: dict[str, list[dict]] = {bucket: [] for bucket, _ in BUCKETS}
    for bucket, entry in selected:
        chosen[bucket].append(entry)

    for bucket, label in BUCKETS:
        total = len(review[bucket])
        lines += [f"### {label} ({total})", ""]
        if total == 0:
            lines += ["_該当なし。_", ""]
            continue
        shown = chosen[bucket]
        if not shown:
            lines += ["_長さの上限により省略した。_", ""]
            continue
        for index, entry in enumerate(shown, start=1):
            lines += _entry_block(index, entry)
        if len(shown) < total:
            lines += [f"_このセクションで省略した項目: {total - len(shown)}件_", ""]

    if review["insufficient_context"]:
        lines += ["### 情報不足（判断に必要だがsnapshotに無かったもの）", ""]
        lines += [f"- {inline(item)}" for item in review["insufficient_context"]]
        lines += [""]

    if review["limitations"]:
        lines += ["### Limitations", ""]
        lines += [f"- {inline(item)}" for item in review["limitations"]]
        lines += [""]

    lines += _notes(document, omitted)
    lines += ["---", "", FOOTER]
    return lines


def _render(document: dict, max_chars: int, prefix: list[str]) -> str:
    """Render, dropping the least important entries until it fits."""
    ordered = _ordered_entries(document)
    for keep in range(len(ordered), -1, -1):
        body = "\n".join(prefix + _body(document, ordered[:keep], omitted=len(ordered) - keep)) + "\n"
        if len(body) <= max_chars:
            return body
    raise RenderError("rendered output exceeds the limit even without entries")


def render_summary(document: dict, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS) -> str:
    """Render the job summary. No marker: this text is never posted."""
    return _render(document, limits.max_job_summary_chars, [])


def render_comment(document: dict, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS) -> str:
    """Render the PR comment body, including the publisher marker."""
    return _render(document, limits.max_comment_chars, [marker(document["snapshot"]["snapshot_id"])])
