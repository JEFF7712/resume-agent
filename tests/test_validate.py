import shutil
from pathlib import Path

import pytest
from resume_validate import (
    CheckResult,
    agent_message,
    is_resume_tex,
    validate_tex,
    write_status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_is_resume_tex_matches_canonical_names() -> None:
    assert is_resume_tex("resume.tex")
    assert is_resume_tex("example_resume.tex")
    assert is_resume_tex("path/to/swe_resume.tex")
    assert not is_resume_tex("notes.tex")
    assert not is_resume_tex("resume.txt")
    assert not is_resume_tex("resume.tex.bak")


def test_validate_missing_source(tmp_path: Path) -> None:
    result = validate_tex(tmp_path / "missing_resume.tex")
    assert not result.ok
    assert "not found" in result.message


def test_validate_reports_missing_pdf_without_compile(tmp_path: Path) -> None:
    tex = tmp_path / "ghost_resume.tex"
    tex.write_text("% empty\n", encoding="utf-8")
    result = validate_tex(tex, compile=False)
    assert not result.ok
    assert "PDF missing" in result.message


def test_agent_message_instructs_autofit_on_failure() -> None:
    results = [
        CheckResult(
            ok=False,
            tex="example_resume.tex",
            pdf=None,
            pages=2,
            bottom_gap_in=None,
            max_bottom_gap_in=0.85,
            message="example_resume.pdf has 2 pages; resumes must be exactly 1 page.",
        )
    ]
    text = agent_message(results)
    assert "[FAIL] example_resume.tex" in text
    assert "resume_validate.py" in text


def test_write_status_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_path = tmp_path / ".resume-check.json"
    monkeypatch.setattr("resume_validate.BUILD_DIR", tmp_path)
    monkeypatch.setattr("resume_validate.STATUS_PATH", status_path)
    write_status(
        [
            CheckResult(
                ok=True,
                tex="example_resume.tex",
                pdf="build/example_resume.pdf",
                pages=1,
                bottom_gap_in=0.44,
                max_bottom_gap_in=0.85,
                message="OK",
            )
        ]
    )
    payload = status_path.read_text(encoding="utf-8")
    assert '"ok": true' in payload
    assert "example_resume.tex" in payload


@pytest.mark.integration
def test_example_resume_passes_validate_and_is_clean() -> None:
    if not shutil.which("latexmk"):
        pytest.skip("latexmk unavailable")
    result = validate_tex(REPO_ROOT / "example_resume.tex")
    assert result.ok, result.message
    assert result.pages == 1
    assert result.bottom_gap_in is not None
    assert result.bottom_gap_in <= 0.85

    log = (REPO_ROOT / "build" / "example_resume.log").read_text(encoding="utf-8", errors="replace")
    assert "LaTeX Font Warning" not in log
    assert "footskip is too small" not in log
