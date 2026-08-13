from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest
import resume_after_edit
from resume_validate import CheckResult

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "example_resume.tex"


def _run_main(monkeypatch: pytest.MonkeyPatch, payload: dict) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    cap = io.StringIO()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", cap)
    monkeypatch.setattr(sys, "stderr", err)
    rc = resume_after_edit.main()
    return rc, cap.getvalue(), err.getvalue()


def test_detect_harness_cursor_claude_codex() -> None:
    assert resume_after_edit.detect_harness({"hook_event_name": "afterFileEdit"}) == (
        "cursor_after_edit"
    )
    assert resume_after_edit.detect_harness({"hook_event_name": "stop", "loop_count": 0}) == (
        "cursor_stop"
    )
    assert resume_after_edit.detect_harness({"hook_event_name": "postToolUse"}) == (
        "cursor_post_tool"
    )
    assert (
        resume_after_edit.detect_harness(
            {
                "hook_event_name": "PostToolUse",
                "transcript_path": "/tmp/t.jsonl",
                "tool_name": "Edit",
            }
        )
        == "claude"
    )
    assert (
        resume_after_edit.detect_harness(
            {"hook_event_name": "PostToolUse", "turn_id": "abc", "tool_name": "Write"}
        )
        == "codex"
    )


def test_paths_from_apply_patch() -> None:
    command = (
        "*** Begin Patch\n"
        "*** Update File: example_resume.tex\n"
        "+foo\n"
        "*** Add File: swe_resume.tex\n"
        "+bar\n"
    )
    assert resume_after_edit.paths_from_apply_patch(command) == [
        "example_resume.tex",
        "swe_resume.tex",
    ]


def test_extract_paths_from_cursor_and_codex() -> None:
    cursor = resume_after_edit.extract_paths(
        {"file_path": "example_resume.tex"}, "cursor_after_edit"
    )
    assert cursor == ["example_resume.tex"]
    codex = resume_after_edit.extract_paths(
        {
            "tool_input": {
                "command": "*** Update File: example_resume.tex\n+x\n",
            }
        },
        "codex",
    )
    assert "example_resume.tex" in codex


def test_resume_paths_ignores_non_resume() -> None:
    found = resume_after_edit.resume_paths(["README.md", str(EXAMPLE)])
    assert found == [EXAMPLE]


def test_main_ignores_non_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, out, _ = _run_main(
        monkeypatch,
        {
            "hook_event_name": "postToolUse",
            "tool_input": {"file_path": "README.md"},
        },
    )
    assert rc == 0
    assert out.strip() == "{}"


def test_main_claude_blocks_and_points_at_autofit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resume_after_edit,
        "validate_tex",
        lambda path: CheckResult(
            ok=False,
            tex=str(path),
            pdf=None,
            pages=2,
            bottom_gap_in=None,
            max_bottom_gap_in=0.85,
            message="2 pages",
        ),
    )
    monkeypatch.setattr(resume_after_edit, "write_status", lambda *a, **k: None)
    rc, out, err = _run_main(
        monkeypatch,
        {
            "hook_event_name": "PostToolUse",
            "transcript_path": "/tmp/t.jsonl",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(EXAMPLE)},
        },
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "--autofit" in payload["reason"]
    assert "2 pages" in err


def test_cursor_stop_emits_followup_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resume_after_edit,
        "read_status",
        lambda: {
            "ok": False,
            "updated_at": time.time(),
            "followup_sent": False,
            "results": [{"ok": False, "tex": "example_resume.tex", "message": "under-filled"}],
        },
    )
    sent: list[dict] = []
    monkeypatch.setattr(resume_after_edit, "mark_followup_sent", lambda status: sent.append(status))
    rc, out, _ = _run_main(
        monkeypatch,
        {"hook_event_name": "stop", "status": "completed", "loop_count": 0},
    )
    assert rc == 0
    payload = json.loads(out)
    assert "followup_message" in payload
    assert "example_resume.tex" in payload["followup_message"]
    assert sent
