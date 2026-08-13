import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from resume_layout import (
    DENSITY_RE,
    lines_equivalent,
    lint_source,
    overfull_boxes,
    read_density,
    text_collisions,
    write_density,
)

CANONICAL = tuple(sorted(p.name for p in REPO_ROOT.glob("*resume.tex")))


@pytest.mark.parametrize("name", CANONICAL)
def test_canonical_resumes_use_shared_style_and_declare_density(name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert "\\usepackage{resumestyle}" in text
    assert len(DENSITY_RE.findall(text)) == 1


@pytest.mark.parametrize("name", CANONICAL)
def test_canonical_resumes_have_no_raw_layout_commands(name: str) -> None:
    assert lint_source(REPO_ROOT / name) == []


def test_lint_flags_raw_spacing_in_body(tmp_path: Path) -> None:
    tex = tmp_path / "x_resume.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{resumestyle}\n"
        "\\setresumedensity{1.0}\n"
        "\\begin{document}\n"
        "\\resumesection{Skills}\n"
        "\\vspace{-9pt}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    problems = lint_source(tex)
    assert len(problems) == 1
    assert "x_resume.tex:6" in problems[0]
    assert "\\vspace" in problems[0]


def test_lint_flags_missing_shared_style(tmp_path: Path) -> None:
    tex = tmp_path / "y_resume.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n", encoding="utf-8"
    )
    assert any("resumestyle" in p for p in lint_source(tex))


def test_preamble_spacing_is_not_flagged(tmp_path: Path) -> None:
    """Layout commands are legal in a preamble; only the body is off limits."""
    tex = tmp_path / "z_resume.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{resumestyle}\n"
        "\\setlength{\\parindent}{0pt}\n"
        "\\setresumedensity{1.0}\n"
        "\\begin{document}\n"
        "text\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    assert lint_source(tex) == []


def test_density_roundtrip(tmp_path: Path) -> None:
    tex = tmp_path / "d_resume.tex"
    tex.write_text("\\setresumedensity{1.0}\n", encoding="utf-8")
    assert read_density(tex) == 1.0
    write_density(tex, 0.4237)
    assert read_density(tex) == pytest.approx(0.424)


def test_overfull_boxes_parses_log_lines() -> None:
    log = (
        "Overfull \\hbox (0.55968pt too wide) in paragraph at lines 155--157\n"
        "Underfull \\hbox (badness 10000) in paragraph at lines 10--11\n"
        "Overfull \\hbox (12.5pt too wide) in paragraph at lines 20--22\n"
    )
    boxes = overfull_boxes(log)
    assert [b["lines"] for b in boxes] == ["155--157", "20--22"]
    assert boxes[1]["pt"] == pytest.approx(12.5)


def test_lines_equivalent_uses_body_leading() -> None:
    assert lines_equivalent(1.0) == pytest.approx(6.0)  # 72pt / 12pt leading


@pytest.mark.integration
def test_collision_detector_catches_overlapping_lines(tmp_path: Path) -> None:
    """A negative gap larger than the line height prints text on top of text.

    LaTeX reports no error for this, so the geometric check is the only signal.
    """
    import shutil
    import subprocess

    if not shutil.which("latexmk"):
        pytest.skip("latexmk unavailable")
    tex = tmp_path / "collide.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        # Default leading is 12pt, so -8pt drops the second baseline just 4pt
        # below the first: the two lines print through each other.
        "\\noindent First line of body text here\\par\n"
        "\\vspace{-8pt}\n"
        "\\noindent Second line of body text here\\par\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", tex.name],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    pdf = tmp_path / "collide.pdf"
    if not pdf.exists():
        pytest.skip("latexmk produced no PDF")
    collisions = text_collisions(pdf)
    assert collisions, "overlapping lines were not detected"
    assert "overlaps" in collisions[0]


@pytest.mark.integration
@pytest.mark.parametrize("name", CANONICAL)
def test_built_resumes_have_no_overlapping_text(name: str) -> None:
    pdf = REPO_ROOT / "build" / f"{Path(name).stem}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf} not built")
    assert text_collisions(pdf) == []
