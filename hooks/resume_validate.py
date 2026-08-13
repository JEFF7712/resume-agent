#!/usr/bin/env python3
"""Compile a resume .tex and enforce exactly-one-page + bottom-fill constraints."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which

# The project being checked. Defaults to this repo, but when the harness is
# vendored (git submodule, etc.) the consuming project sets RESUME_PROJECT_ROOT
# so that build/, resumestyle.sty and the resume sources resolve against it
# rather than against the vendored copy.
REPO_ROOT = Path(os.environ.get("RESUME_PROJECT_ROOT") or Path(__file__).resolve().parents[1])
REPO_ROOT = REPO_ROOT.expanduser().resolve()
BUILD_DIR = REPO_ROOT / "build"
STATUS_PATH = BUILD_DIR / ".resume-check.json"

# Letter page is 11in; allow a normal bottom margin, but reject large empty tails.
DEFAULT_MAX_BOTTOM_GAP_IN = 0.85
DEFAULT_DPI = 120
INK_THRESHOLD = 250

# A file is treated as a resume if it is `resume.tex` or ends in `_resume.tex`
# (e.g. `swe_resume.tex`), which is how variants are named.
RESUME_BASENAMES = frozenset({"resume.tex"})


@dataclass
class CheckResult:
    ok: bool
    tex: str
    pdf: str | None
    pages: int | None
    bottom_gap_in: float | None
    max_bottom_gap_in: float
    message: str
    compile_log_tail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def is_resume_tex(path: str | Path) -> bool:
    p = Path(path)
    name = p.name
    if name in RESUME_BASENAMES:
        return True
    return name.endswith("_resume.tex")


def resolve_tex(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    return p


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def _latexmk_cmd() -> list[str]:
    latexmk = which("latexmk")
    if latexmk:
        return [latexmk]
    if which("nix"):
        return ["nix", "develop", str(REPO_ROOT), "-c", "latexmk"]
    raise RuntimeError("latexmk not on PATH and nix is unavailable")


def output_dir_for(tex: Path) -> Path:
    """Root-level sources build into build/; nested ones build beside the source."""
    if tex.parent == REPO_ROOT:
        return BUILD_DIR
    return tex.parent


def compile_tex(tex: Path) -> tuple[Path, str]:
    out_dir = output_dir_for(tex)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = tex.parent
    cmd = [
        *_latexmk_cmd(),
        "-g",
        "-pdf",
        "-interaction=nonstopmode",
        f"-output-directory={out_dir}",
        tex.name,
    ]
    # Nested .tex files still need to find resumestyle.sty at the repo root.
    env = dict(os.environ, TEXINPUTS=f"{REPO_ROOT}:{os.environ.get('TEXINPUTS', '')}")
    proc = subprocess.run(cmd, cwd=work_dir, text=True, capture_output=True, check=False, env=env)
    pdf = out_dir / f"{tex.stem}.pdf"
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"latexmk failed for {tex.name} (exit {proc.returncode})\n{log[-4000:]}")
    return pdf, log


def pdf_page_count(pdf: Path) -> int:
    proc = _run(["pdfinfo", str(pdf)])
    if proc.returncode != 0:
        raise RuntimeError(f"pdfinfo failed:\n{proc.stderr or proc.stdout}")
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise RuntimeError(f"Could not parse Pages from pdfinfo for {pdf}")


def bottom_gap_inches(pdf: Path, *, dpi: int = DEFAULT_DPI) -> float:
    pdftoppm = which("pdftoppm")
    magick = which("magick") or which("convert")
    if not pdftoppm or not magick:
        raise RuntimeError("Need pdftoppm and ImageMagick (magick/convert) for fill checks")

    with tempfile.TemporaryDirectory(prefix="resume-fill-") as td:
        prefix = Path(td) / "page"
        proc = _run([pdftoppm, "-png", "-r", str(dpi), "-singlefile", str(pdf), str(prefix)])
        if proc.returncode != 0:
            raise RuntimeError(f"pdftoppm failed:\n{proc.stderr or proc.stdout}")
        png = Path(str(prefix) + ".png")
        raw = subprocess.check_output([magick, str(png), "-colorspace", "Gray", "pgm:-"])

    if not raw.startswith(b"P5"):
        raise RuntimeError("Expected PGM P5 from ImageMagick")

    i = 2

    def next_token() -> bytes:
        nonlocal i
        while i < len(raw) and raw[i] in b" \t\r\n":
            i += 1
        if i < len(raw) and raw[i : i + 1] == b"#":
            while i < len(raw) and raw[i] != 10:
                i += 1
            return next_token()
        j = i
        while j < len(raw) and raw[j] not in b" \t\r\n":
            j += 1
        tok = raw[i:j]
        i = j
        return tok

    width = int(next_token())
    height = int(next_token())
    _maxval = int(next_token())
    while i < len(raw) and raw[i] in b" \t\r":
        i += 1
    if i < len(raw) and raw[i] == 10:
        i += 1
    data = raw[i:]
    if len(data) < width * height:
        raise RuntimeError("Truncated PGM pixel data")

    last = 0
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        if any(b < INK_THRESHOLD for b in row):
            last = y
    return (height - 1 - last) / float(dpi)


def validate_tex(
    tex_path: str | Path,
    *,
    max_bottom_gap_in: float | None = None,
    compile: bool = True,
) -> CheckResult:
    max_gap = (
        max_bottom_gap_in
        if max_bottom_gap_in is not None
        else float(os.environ.get("RESUME_MAX_BOTTOM_GAP_IN", DEFAULT_MAX_BOTTOM_GAP_IN))
    )
    tex = resolve_tex(tex_path)
    if not tex.exists():
        return CheckResult(
            ok=False,
            tex=str(tex),
            pdf=None,
            pages=None,
            bottom_gap_in=None,
            max_bottom_gap_in=max_gap,
            message=f"Resume source not found: {tex}",
        )

    compile_log_tail = None
    try:
        if compile:
            pdf, log = compile_tex(tex)
            compile_log_tail = log[-2000:] if log else None
        else:
            pdf = output_dir_for(tex) / f"{tex.stem}.pdf"
            if not pdf.exists():
                return CheckResult(
                    ok=False,
                    tex=str(tex),
                    pdf=None,
                    pages=None,
                    bottom_gap_in=None,
                    max_bottom_gap_in=max_gap,
                    message=f"PDF missing for {tex.name}; compile first",
                )

        pages = pdf_page_count(pdf)
        if pages != 1:
            return CheckResult(
                ok=False,
                tex=str(tex),
                pdf=str(pdf),
                pages=pages,
                bottom_gap_in=None,
                max_bottom_gap_in=max_gap,
                message=(
                    f"{pdf.name} has {pages} pages; resumes must be exactly 1 page. "
                    "Run `--autofit` first; if that reports content_too_long, cut content."
                ),
                compile_log_tail=compile_log_tail,
            )

        gap = bottom_gap_inches(pdf)
        if gap > max_gap:
            return CheckResult(
                ok=False,
                tex=str(tex),
                pdf=str(pdf),
                pages=pages,
                bottom_gap_in=round(gap, 3),
                max_bottom_gap_in=max_gap,
                message=(
                    f"{pdf.name} is 1 page but under-filled: bottom gap {gap:.2f}in "
                    f"(max {max_gap:.2f}in). Run `--autofit` first; only add real content "
                    "if it reports content_too_short."
                ),
                compile_log_tail=compile_log_tail,
            )

        return CheckResult(
            ok=True,
            tex=str(tex),
            pdf=str(pdf),
            pages=pages,
            bottom_gap_in=round(gap, 3),
            max_bottom_gap_in=max_gap,
            message=(
                f"OK: {pdf.name} is exactly 1 page with bottom gap {gap:.2f}in "
                f"(max {max_gap:.2f}in)."
            ),
            compile_log_tail=compile_log_tail,
        )
    except Exception as exc:  # noqa: BLE001 - surface toolchain failures to agents
        return CheckResult(
            ok=False,
            tex=str(tex),
            pdf=None,
            pages=None,
            bottom_gap_in=None,
            max_bottom_gap_in=max_gap,
            message=f"Resume check failed for {tex.name}: {exc}",
            compile_log_tail=compile_log_tail,
        )


def write_status(results: list[CheckResult], *, followup_sent: bool = False) -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": all(r.ok for r in results) if results else True,
        "updated_at": time.time(),
        "followup_sent": followup_sent,
        "results": [r.to_dict() for r in results],
    }
    STATUS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_status() -> dict | None:
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def mark_followup_sent(status: dict) -> None:
    status = dict(status)
    status["followup_sent"] = True
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def agent_message(results: list[CheckResult]) -> str:
    if not results:
        return ""
    lines = ["Resume layout check:"]
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        lines.append(f"- [{status}] {Path(r.tex).name}: {r.message}")
    if any(not r.ok for r in results):
        lines.append(
            "Fix the .tex source, then rely on the post-edit hook (or run "
            "`python3 hooks/resume_validate.py <file.tex>`) until PASS."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tex", nargs="+", help="Resume .tex path(s) to validate")
    parser.add_argument(
        "--max-bottom-gap",
        type=float,
        default=None,
        help=f"Max allowed bottom whitespace in inches (default {DEFAULT_MAX_BOTTOM_GAP_IN})",
    )
    parser.add_argument(
        "--no-compile",
        action="store_true",
        help="Skip latexmk and only inspect an existing PDF",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    args = parser.parse_args(argv)

    results = [
        validate_tex(path, max_bottom_gap_in=args.max_bottom_gap, compile=not args.no_compile)
        for path in args.tex
    ]
    write_status(results)
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(agent_message(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
