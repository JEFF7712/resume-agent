import re
import shutil
import subprocess
from pathlib import Path

import pytest
import resume_layout
from resume_layout import (
    DENSITY_MAX,
    DENSITY_MIN,
    DENSITY_RE,
    Measurement,
    autofit,
    lines_equivalent,
    lint_source,
    overfull_boxes,
    read_density,
    report,
    text_collisions,
    write_density,
)
from resume_validate import validate_tex

REPO_ROOT = Path(__file__).resolve().parents[1]
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


PUBLIC_MACRO_RE = re.compile(r"\\newcommand\{\\(resume[A-Za-z]+|classesList|resumesep)\}")
INTERNAL_MACROS = {"resumedensity"}


def test_example_resume_exercises_each_public_macro() -> None:
    sty = (REPO_ROOT / "resumestyle.sty").read_text(encoding="utf-8")
    example = (REPO_ROOT / "example_resume.tex").read_text(encoding="utf-8")
    macros = [name for name in PUBLIC_MACRO_RE.findall(sty) if name not in INTERNAL_MACROS]
    assert macros, "expected public macros in resumestyle.sty"
    missing = [name for name in macros if f"\\{name}" not in example]
    assert missing == [], f"example_resume.tex does not use: {missing}"


def _stub_tex(tmp_path: Path) -> Path:
    tex = tmp_path / "fit_resume.tex"
    tex.write_text("\\setresumedensity{1.0}\n", encoding="utf-8")
    return tex


def test_autofit_solves_and_writes_density(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tex = _stub_tex(tmp_path)

    def fake_measure(_tex: Path, density: float) -> Measurement:
        span = DENSITY_MAX - DENSITY_MIN
        gap = 0.20 + 0.50 * ((density - DENSITY_MIN) / span)
        return Measurement(
            density=density,
            pages=1,
            gap_in=gap,
            log="",
            collisions=[],
            overflow_in=0.0,
        )

    monkeypatch.setattr(resume_layout, "measure", fake_measure)
    result = autofit(tex)
    assert result["ok"]
    assert result["reason"] == "solved"
    assert result["density"] is not None
    assert read_density(tex) == pytest.approx(result["density"])


def test_autofit_reports_content_too_long(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tex = _stub_tex(tmp_path)

    def fake_measure(_tex: Path, density: float) -> Measurement:
        return Measurement(
            density=density,
            pages=2,
            gap_in=0.0,
            log="",
            collisions=[],
            overflow_in=0.80,
        )

    monkeypatch.setattr(resume_layout, "measure", fake_measure)
    result = autofit(tex)
    assert not result["ok"]
    assert result["reason"] == "content_too_long"
    assert "cut roughly" in result["message"]
    assert read_density(tex) == pytest.approx(1.0)


def test_autofit_reports_content_too_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tex = _stub_tex(tmp_path)

    def fake_measure(_tex: Path, density: float) -> Measurement:
        return Measurement(
            density=density,
            pages=1,
            gap_in=1.40,
            log="",
            collisions=[],
            overflow_in=0.0,
        )

    monkeypatch.setattr(resume_layout, "measure", fake_measure)
    result = autofit(tex)
    assert not result["ok"]
    assert result["reason"] == "content_too_short"
    assert "add roughly" in result["message"]
    assert "Do not invent experience" in result["message"]


def test_autofit_rejects_overlap_at_the_only_fitting_density(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tex = _stub_tex(tmp_path)

    def fake_measure(_tex: Path, density: float) -> Measurement:
        if density < 1.0:
            return Measurement(
                density=density,
                pages=2,
                gap_in=0.0,
                log="",
                collisions=[],
                overflow_in=0.60,
            )
        return Measurement(
            density=density,
            pages=1,
            gap_in=0.30,
            log="",
            collisions=['"Experience" overlaps "Skills" by 7.2pt'],
            overflow_in=0.0,
        )

    monkeypatch.setattr(resume_layout, "measure", fake_measure)
    result = autofit(tex)
    assert not result["ok"]
    assert result["reason"] == "content_too_long"
    assert "overlap" in result["message"]


def _minimal_resume(tmp_path: Path, body: str) -> Path:
    tex = tmp_path / "mini_resume.tex"
    tex.write_text(
        "\\documentclass[letterpaper,11pt]{article}\n"
        "\\usepackage{resumestyle}\n"
        "\\setresumedensity{1.0}\n"
        "\\begin{document}\n"
        f"{body}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return tex


@pytest.mark.integration
def test_autofit_thin_page_is_content_too_short(tmp_path: Path) -> None:
    if not shutil.which("latexmk"):
        pytest.skip("latexmk unavailable")
    tex = _minimal_resume(
        tmp_path,
        "\\resumeheader{Pat Lee}{Austin, TX}{\\resumecontact{pat@example.com}}\n"
        "\\resumesection{Skills}\n"
        "\\resumeSkillsListStart\n"
        "\\resumeItem{Python}\n"
        "\\resumeSkillsListEnd\n",
    )
    result = autofit(tex)
    assert not result["ok"]
    assert result["reason"] == "content_too_short"


@pytest.mark.integration
def test_example_resume_layout_report_passes() -> None:
    if not shutil.which("latexmk"):
        pytest.skip("latexmk unavailable")
    result = report(REPO_ROOT / "example_resume.tex")
    assert result["ok"], result["message"]
    assert result["pages"] == 1
    assert not result["collisions"]
    assert not result["overfull"]
    assert not result["lint"]


@pytest.mark.integration
def test_collision_detector_catches_overlapping_lines(tmp_path: Path) -> None:
    """A negative gap larger than the line height prints text on top of text.

    LaTeX reports no error for this, so the geometric check is the only signal.
    """
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
        if not shutil.which("latexmk"):
            pytest.skip("latexmk unavailable")
        result = validate_tex(REPO_ROOT / name)
        assert result.ok, result.message
        assert result.pdf is not None
        pdf = Path(result.pdf)
    assert text_collisions(pdf) == []
