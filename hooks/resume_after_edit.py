#!/usr/bin/env python3
"""Cross-harness post-edit hook: rebuild + validate resume .tex after agent edits.

Supports:
  - Cursor: afterFileEdit, postToolUse (Write/StrReplace), stop
  - Claude Code: PostToolUse (Edit|Write|MultiEdit)
  - Codex: PostToolUse (apply_patch|Edit|Write)
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from resume_validate import (
    REPO_ROOT,
    STATUS_PATH,
    agent_message,
    is_resume_tex,
    mark_followup_sent,
    read_status,
    resolve_tex,
    validate_tex,
    write_status,
)

PATCH_FILE_RE = re.compile(r"(?m)^(?:\*\*\* (?:Update|Add|Delete) File: |diff --git a/\S+ b/)(.+)$")


def load_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def detect_harness(payload: dict) -> str:
    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    if event in {"afterFileEdit", "afterTabFileEdit"}:
        return "cursor_after_edit"
    if event == "stop" or ("status" in payload and "loop_count" in payload):
        return "cursor_stop"
    if event == "postToolUse":
        return "cursor_post_tool"
    if event == "PostToolUse" or "tool_name" in payload:
        if "transcript_path" in payload:
            return "claude"
        if "turn_id" in payload:
            return "codex"
        # Cursor postToolUse often omits hook_event_name but includes tool_output.
        if "tool_output" in payload:
            return "cursor_post_tool"
        tool_input = payload.get("tool_input") or {}
        if (
            isinstance(tool_input, dict)
            and "command" in tool_input
            and "file_path" not in tool_input
        ):
            return "codex"
        tool_name = payload.get("tool_name") or ""
        # Ambiguous without transcript/turn markers; prefer Cursor when names match.
        if (
            tool_name in {"Write", "StrReplace", "EditNotebook", "Shell", "Read", "Task", "Delete"}
            and "cwd" in payload
            and "permission_mode" not in payload
        ):
            return "cursor_post_tool"
        return "claude"
    if "file_path" in payload and "edits" in payload:
        return "cursor_after_edit"
    return "unknown"


def paths_from_apply_patch(command: str) -> list[str]:
    return [m.strip() for m in PATCH_FILE_RE.findall(command or "") if m.strip()]


def extract_paths(payload: dict, harness: str) -> list[str]:
    paths: list[str] = []

    if harness == "cursor_after_edit":
        fp = payload.get("file_path")
        if fp:
            paths.append(fp)
        return paths

    if harness == "cursor_stop":
        return paths

    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {}

    if not isinstance(tool_input, dict):
        tool_input = {}

    for key in ("file_path", "path", "target_notebook"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            paths.append(val)

    command = tool_input.get("command")
    if isinstance(command, str) and (
        "Begin Patch" in command or "*** Update File:" in command or "apply_patch" in command
    ):
        paths.extend(paths_from_apply_patch(command))

    top = payload.get("file_path")
    if isinstance(top, str) and top:
        paths.append(top)

    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resume_paths(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        if not is_resume_tex(raw):
            continue
        path = resolve_tex(raw)
        if path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


def emit_cursor_after_edit() -> None:
    # afterFileEdit has no agent-facing output contract; status file + stop hook handle feedback.
    print("{}")


def emit_cursor_post_tool(msg: str) -> None:
    print(json.dumps({"additional_context": msg}))


def emit_cursor_stop() -> None:
    status = read_status()
    if not status or status.get("ok", True) or status.get("followup_sent"):
        print("{}")
        return
    updated_at = float(status.get("updated_at") or 0)
    # Ignore stale failures from earlier sessions/edits.
    if updated_at and (time.time() - updated_at) > 30 * 60:
        print("{}")
        return
    results = status.get("results") or []
    failures = [r for r in results if not r.get("ok", True)]
    if not failures:
        print("{}")
        return
    lines = [
        "The last resume edit failed the layout check. Keep editing the resume .tex until it passes:",
        "exactly 1 page, and bottom gap ≤ 0.85in (page should look full, not short).",
    ]
    for r in failures:
        lines.append(f"- {Path(r.get('tex', '?')).name}: {r.get('message', 'failed')}")
    lines.append(f"Status file: {STATUS_PATH}")
    mark_followup_sent(status)
    print(json.dumps({"followup_message": "\n".join(lines)}))


def emit_claude_or_codex(msg: str, ok: bool) -> None:
    # Prefer JSON additionalContext so both Claude Code and Codex see the result.
    # Exit 2 would discard stdout JSON on Claude Code, so keep exit 0 and use
    # decision/reason for actionable failures.
    payload: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }
    if not ok:
        payload["decision"] = "block"
        payload["reason"] = msg
        print(msg, file=sys.stderr)
    print(json.dumps(payload))


def guidance(paths: list[Path], ok: bool) -> tuple[str, bool]:
    """Turn a pass/fail into an instruction, so the agent stops guessing at spacing.

    Returns the extra message text and whether the source itself is clean; raw
    layout commands in a body are a hard fail even when the page still measures OK.
    """
    from resume_layout import lint_source, overfull_boxes

    lines: list[str] = []
    source_clean = True
    for path in paths:
        for problem in lint_source(path):
            source_clean = False
            lines.append(f"- {problem}")
        log = REPO_ROOT / "build" / f"{path.stem}.log"
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            for box in overfull_boxes(text):
                pt = f"{box['pt']:.2f}pt" if box["pt"] is not None else "?"
                lines.append(
                    f"- {path.name}:{box['lines']} overfull \\hbox by {pt} "
                    "(text past the right margin; shorten the wording on those lines)"
                )
    if not ok:
        names = " ".join(p.name for p in paths)
        lines.append(
            "- Do NOT hand-tune \\vspace to fix page fill. Run "
            f"`python3 hooks/resume_layout.py {names} --autofit` first; it solves "
            "\\setresumedensity for you. Only change content if autofit reports "
            "content_too_short or content_too_long."
        )
    if not lines:
        return "", source_clean
    return "\n" + "\n".join(lines), source_clean


def main() -> int:
    payload = load_stdin()
    harness = detect_harness(payload)

    if harness == "cursor_stop":
        emit_cursor_stop()
        return 0

    paths = resume_paths(extract_paths(payload, harness))
    if not paths:
        print("{}")
        return 0

    results = [validate_tex(path) for path in paths]
    write_status(results)
    ok = all(r.ok for r in results)
    extra, source_clean = guidance(paths, ok)
    msg = agent_message(results) + extra
    ok = ok and source_clean

    if harness == "cursor_after_edit":
        emit_cursor_after_edit()
        return 0
    if harness == "cursor_post_tool":
        emit_cursor_post_tool(msg)
        return 0
    if harness in {"claude", "codex"}:
        emit_claude_or_codex(msg, ok)
        return 0

    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
