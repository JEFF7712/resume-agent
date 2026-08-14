#!/usr/bin/env python3
"""Layout controller for resume .tex sources.

The older `resume_validate.py` is a *gate*: it answers pass/fail. That leaves an
agent guessing which of a dozen hand-tuned \\vspace values to nudge, which is how
spacing gets mangled. This module is a *controller*:

  * `--autofit` solves for the single \\setresumedensity scalar that makes the
    page exactly 1 page and comfortably full. No model in the loop, no guessing.
  * `report` converts the residual into actionable units: "add ~3 body lines",
    plus the overfull \\hbox list (with .tex line numbers) that explains ragged
    right-hand dates and text running past the margin.
  * `--preview` renders a PNG so an agent can actually look at the page.

Density semantics (see resumestyle.sty): every vertical gap is a negative length
scaled by \\resumedensity. Higher density => more negative => tighter => larger
bottom gap. So bottom gap increases monotonically with density, which is what
makes the bisection below valid.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from resume_validate import (
    DEFAULT_MAX_BOTTOM_GAP_IN,
    INK_THRESHOLD,
    REPO_ROOT,
    bottom_gap_inches,
    pdf_page_count,
    resolve_tex,
)

DENSITY_RE = re.compile(r"(\\setresumedensity\{)(-?[\d.]+)(\})")
OVERFULL_RE = re.compile(
    r"^(Overfull|Underfull) \\hbox \(([^)]*)\)(?: in paragraph| detected)? at lines (\d+)--(\d+)",
    re.MULTILINE,
)

# \small in an 11pt article: 10pt font on a 12pt baseline.
BODY_LINE_PT = 12.0
BODY_LINE_IN = BODY_LINE_PT / 72.0

# Bounds on \resumedensity. 1.0 is the template's hand-tuned look. 0.0 zeroes
# every negative gap (maximum height); negative values turn them into positive
# space, which is the only way to fill a genuinely thin page without inventing
# content.
DENSITY_MIN = -1.0
DENSITY_MAX = 2.5

# Aim a little under the hard ceiling so a later one-word edit does not tip the
# page over into 2 pages.
DEFAULT_TARGET_GAP_IN = 0.45


WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>'
)

# A tall line (\Huge name, a superscript, a \raisebox) legitimately reaches into
# its neighbour's band by a point or two. A real collision is much deeper.
COLLISION_ABS_PT = 2.5
COLLISION_FRAC = 0.35

# Bands narrower and shorter than this are superscripts/subscripts, not lines.
FRAGMENT_MAX_WIDTH_PT = 15.0
FRAGMENT_HEIGHT_FRAC = 0.7
COLLISION_LOOKAHEAD = 3

# Consecutive bullets at the same indent should share an item gap. A last line
# that fills the measure can wrap a trailing space onto a blank extra line,
# which shows up as ~12pt of extra hole before the next bullet. Section breaks
# and job headings sit between lists and are ignored.
BULLET_MARKERS = frozenset({"•", "●", "‣"})
ITEM_X_TOL_PT = 8.0
HEADING_INSET_PT = 8.0
ITEM_GAP_ABS_PT = 12.0
ITEM_GAP_RATIO = 2.0


@dataclass
class Measurement:
    density: float
    pages: int
    gap_in: float
    log: str
    collisions: list[str]
    overflow_in: float
    internal_gaps: list[str] = field(default_factory=list)


class LayoutError(RuntimeError):
    pass


def read_density(tex: Path) -> float:
    m = DENSITY_RE.search(tex.read_text(encoding="utf-8"))
    if not m:
        raise LayoutError(
            f"{tex.name} has no \\setresumedensity{{...}} line; it is not on the shared "
            "resumestyle.sty layout. Add \\usepackage{resumestyle} and \\setresumedensity{1.0}."
        )
    return float(m.group(2))


def write_density(tex: Path, density: float) -> None:
    text = tex.read_text(encoding="utf-8")
    new, n = DENSITY_RE.subn(lambda m: f"{m.group(1)}{density:.3f}{m.group(3)}", text)
    if n != 1:
        raise LayoutError(f"{tex.name}: expected exactly 1 \\setresumedensity line, found {n}")
    tex.write_text(new, encoding="utf-8")


def _latexmk() -> str:
    exe = shutil.which("latexmk")
    if not exe:
        raise LayoutError("latexmk not on PATH; run inside `nix develop`")
    return exe


def _texinputs() -> str:
    """Let a .tex anywhere in the repo find resumestyle.sty at the repo root."""
    return f"{REPO_ROOT}:"


def measure(tex: Path, density: float) -> Measurement:
    """Compile a copy of `tex` at `density` in a scratch dir; never touches build/."""
    source = tex.read_text(encoding="utf-8")
    patched, n = DENSITY_RE.subn(lambda m: f"{m.group(1)}{density:.4f}{m.group(3)}", source)
    if n != 1:
        raise LayoutError(f"{tex.name}: expected exactly 1 \\setresumedensity line, found {n}")

    with tempfile.TemporaryDirectory(prefix="resume-autofit-") as td:
        work = Path(td)
        (work / tex.name).write_text(patched, encoding="utf-8")
        proc = subprocess.run(
            [_latexmk(), "-pdf", "-interaction=nonstopmode", tex.name],
            cwd=work,
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "TEXINPUTS": _texinputs()},
        )
        pdf = work / f"{tex.stem}.pdf"
        log_path = work / f"{tex.stem}.log"
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if not pdf.exists():
            tail = (proc.stdout or "") + (proc.stderr or "")
            raise LayoutError(f"latexmk failed at density {density:.3f}:\n{tail[-3000:]}")
        pages = pdf_page_count(pdf)
        return Measurement(
            density=density,
            pages=pages,
            gap_in=bottom_gap_inches(pdf),
            log=log,
            collisions=text_collisions(pdf),
            overflow_in=overflow_height_inches(pdf) if pages > 1 else 0.0,
            internal_gaps=uneven_item_gaps(pdf),
        )


def overflow_height_inches(pdf: Path) -> float:
    """Vertical extent of ink on pages 2+ -- i.e. how much content does not fit."""
    total = 0.0
    pages = pdf_page_count(pdf)
    dpi = 100
    for page in range(2, pages + 1):
        with tempfile.TemporaryDirectory(prefix="resume-overflow-") as td:
            prefix = Path(td) / "p"
            subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    str(dpi),
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    str(pdf),
                    str(prefix),
                ],
                check=False,
                capture_output=True,
            )
            png = Path(str(prefix) + ".png")
            if not png.exists():
                continue
            raw = subprocess.check_output(["magick", str(png), "-colorspace", "Gray", "pgm:-"])
            rows = _pgm_ink_rows(raw)
            if rows:
                total += (rows[-1] - rows[0] + 1) / float(dpi)
    return total


def _pgm_ink_rows(raw: bytes) -> list[int]:
    """Row indices containing ink, from a P5 PGM byte stream."""
    if not raw.startswith(b"P5"):
        return []
    i = 2

    def token() -> bytes:
        nonlocal i
        while i < len(raw) and raw[i] in b" \t\r\n":
            i += 1
        if i < len(raw) and raw[i : i + 1] == b"#":
            while i < len(raw) and raw[i] != 10:
                i += 1
            return token()
        j = i
        while j < len(raw) and raw[j] not in b" \t\r\n":
            j += 1
        tok = raw[i:j]
        i = j
        return tok

    width, height = int(token()), int(token())
    token()  # maxval
    while i < len(raw) and raw[i] in b" \t\r":
        i += 1
    if i < len(raw) and raw[i] == 10:
        i += 1
    data = raw[i:]
    if len(data) < width * height:
        return []
    return [
        y
        for y in range(height)
        if any(b < INK_THRESHOLD for b in data[y * width : (y + 1) * width])
    ]


def _pdf_page_chunks(pdf: Path) -> list[str]:
    proc = subprocess.run(
        ["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return []
    return proc.stdout.split("<page ")[1:]


def _page_lines(chunk: str) -> list[dict]:
    """Cluster pdftotext -bbox words into visual lines, dropping superscripts."""
    words = [
        (float(x0), float(y0), float(x1), float(y1), txt)
        for x0, y0, x1, y1, txt in WORD_RE.findall(chunk)
    ]
    if not words:
        return []

    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[dict] = []
    for x0, y0, x1, y1, txt in words:
        mid = (y0 + y1) / 2
        if lines and abs(mid - lines[-1]["mid"]) <= 2.0:
            ln = lines[-1]
            ln["y0"] = min(ln["y0"], y0)
            ln["y1"] = max(ln["y1"], y1)
            ln["x0"] = min(ln["x0"], x0)
            ln["x1"] = max(ln["x1"], x1)
            ln["words"].append(txt)
            ln["mid"] = (ln["y0"] + ln["y1"]) / 2
        else:
            lines.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1, "mid": mid, "words": [txt]})

    heights = sorted(ln["y1"] - ln["y0"] for ln in lines)
    median_h = heights[len(heights) // 2] if heights else 0.0
    return [
        ln
        for ln in lines
        if not (
            (ln["x1"] - ln["x0"]) < FRAGMENT_MAX_WIDTH_PT
            and (ln["y1"] - ln["y0"]) < FRAGMENT_HEIGHT_FRAC * median_h
        )
    ]


def text_collisions(pdf: Path) -> list[str]:
    """Find text lines that vertically overlap each other.

    Over-tight negative spacing does not raise a LaTeX error: headings simply
    print on top of body text. Page count and bottom gap are both blind to it,
    so the geometry has to be checked directly.
    """
    out: list[str] = []
    for chunk in _pdf_page_chunks(pdf):
        out.extend(_collisions_in_lines(_page_lines(chunk)))
    return out


def _collisions_in_lines(lines: list[dict]) -> list[str]:
    out: list[str] = []
    for i, a in enumerate(lines):
        for b in lines[i + 1 : i + 1 + COLLISION_LOOKAHEAD]:
            overlap = a["y1"] - b["y0"]
            if overlap <= 0:
                continue
            if a["x1"] <= b["x0"] or b["x1"] <= a["x0"]:
                continue  # side by side, not stacked
            shorter = min(a["y1"] - a["y0"], b["y1"] - b["y0"])
            if overlap < max(COLLISION_ABS_PT, COLLISION_FRAC * shorter):
                continue
            first = " ".join(a["words"])[:38]
            second = " ".join(b["words"])[:38]
            out.append(f'"{first}" overlaps "{second}" by {overlap:.1f}pt')
    return out


def _is_bullet(ln: dict) -> bool:
    if not ln["words"]:
        return False
    first = ln["words"][0]
    return first in BULLET_MARKERS or any(first.startswith(m) for m in BULLET_MARKERS)


def uneven_item_gaps(pdf: Path) -> list[str]:
    """Find sibling bullets whose gap is much larger than the typical item gap.

    Page count and bottom-gap checks miss a hole in the middle of a list. The
    usual cause is a last line that fills the measure, wrapping a trailing space
    onto a blank extra line before the next \\item.
    """
    out: list[str] = []
    for chunk in _pdf_page_chunks(pdf):
        out.extend(uneven_item_gaps_in_lines(_page_lines(chunk)))
    return out


def uneven_item_gaps_in_lines(lines: list[dict]) -> list[str]:
    bullets = [i for i, ln in enumerate(lines) if _is_bullet(ln)]
    sibling: list[tuple[float, str, str]] = []
    for a_i, b_i in itertools.pairwise(bullets):
        a, b = lines[a_i], lines[b_i]
        if abs(a["x0"] - b["x0"]) > ITEM_X_TOL_PT:
            continue
        between = lines[a_i + 1 : b_i]
        inset = min(a["x0"], b["x0"])
        if any(ln["x0"] < inset - HEADING_INSET_PT for ln in between):
            continue
        prev = lines[b_i - 1]
        gap = b["y0"] - prev["y1"]
        sibling.append((gap, " ".join(a["words"]), " ".join(b["words"])))
    if len(sibling) < 2:
        return []
    med = statistics.median(g for g, _, _ in sibling)
    limit = max(ITEM_GAP_ABS_PT, ITEM_GAP_RATIO * med)
    out: list[str] = []
    for gap, prev_t, next_t in sibling:
        if gap <= limit:
            continue
        out.append(
            f'{gap:.0f}pt between "{prev_t[:40]}" and "{next_t[:40]}" '
            f"(typical item gap {med:.0f}pt). Shorten the previous bullet so its "
            "last line is not flush to the margin."
        )
    return out


def overfull_boxes(log: str, *, min_pt: float = 0.1) -> list[dict]:
    out = []
    for kind, detail, start, end in OVERFULL_RE.findall(log):
        pt = None
        m = re.search(r"([\d.]+)pt too wide", detail)
        if m:
            pt = float(m.group(1))
        if kind == "Overfull" and pt is not None and pt < min_pt:
            continue
        if kind == "Underfull":
            continue
        out.append({"kind": kind, "detail": detail, "lines": f"{start}--{end}", "pt": pt})
    return out


def lines_equivalent(inches: float) -> float:
    return inches / BODY_LINE_IN


def autofit(
    tex: Path,
    *,
    target_gap_in: float = DEFAULT_TARGET_GAP_IN,
    max_gap_in: float = DEFAULT_MAX_BOTTOM_GAP_IN,
    tol_in: float = 0.03,
    max_iters: int = 11,
) -> dict:
    """Bisect \\resumedensity so the page is 1 page with bottom gap ~= target."""
    trace: list[dict] = []
    cache: dict[float, Measurement] = {}

    def probe(d: float) -> Measurement:
        key = round(d, 4)
        if key not in cache:
            m = measure(tex, key)
            cache[key] = m
            trace.append(
                {
                    "density": key,
                    "pages": m.pages,
                    "gap_in": round(m.gap_in, 3),
                    "collisions": len(m.collisions),
                }
            )
        return cache[key]

    # Feasible band: pages == 1 (needs density >= d_lo) and no overlapping text
    # (needs density <= d_hi). Both properties are monotone in density.
    loosest = probe(DENSITY_MIN)

    d_lo = DENSITY_MIN
    if loosest.pages > 1:
        lo, hi = DENSITY_MIN, DENSITY_MAX
        if probe(DENSITY_MAX).pages > 1:
            tight = probe(DENSITY_MAX)
            over = lines_equivalent(tight.overflow_in)
            return {
                "ok": False,
                "reason": "content_too_long",
                "density": None,
                "message": (
                    f"Even at maximum density ({DENSITY_MAX}) {tex.name} is {tight.pages} pages "
                    f"with {tight.overflow_in:.2f}in spilling over. Spacing cannot fix this: "
                    f"cut roughly {over:.0f} body lines of content."
                ),
                "trace": trace,
            }
        for _ in range(max_iters):
            mid = (lo + hi) / 2.0
            if probe(mid).pages > 1:
                lo = mid
            else:
                hi = mid
            if hi - lo < 0.02:
                break
        d_lo = hi

    at_lo = probe(d_lo)
    if at_lo.collisions:
        # The page only "fits" by printing text on top of itself. Measure how much
        # content actually has to go at the tightest density that stays legible.
        lo, hi = DENSITY_MIN, d_lo
        for _ in range(max_iters):
            mid = (lo + hi) / 2.0
            if probe(mid).collisions:
                hi = mid
            else:
                lo = mid
            if hi - lo < 0.02:
                break
        safe = probe(lo)
        excess = lines_equivalent(safe.overflow_in)
        return {
            "ok": False,
            "reason": "content_too_long",
            "density": None,
            "collisions": at_lo.collisions,
            "message": (
                f"{tex.name} only fits on 1 page at a density that makes text overlap "
                f"({len(at_lo.collisions)} collisions, e.g. {at_lo.collisions[0]}). "
                f"At the tightest legible density ({lo:.2f}) it is {safe.pages} pages with "
                f"{safe.overflow_in:.2f}in spilling over. Spacing cannot fix this: "
                f"cut roughly {excess:.0f} body lines of content."
            ),
            "trace": trace,
        }

    if at_lo.gap_in > max_gap_in:
        deficit = lines_equivalent(at_lo.gap_in - target_gap_in)
        return {
            "ok": False,
            "reason": "content_too_short",
            "density": round(d_lo, 3),
            "measured_gap_in": round(at_lo.gap_in, 3),
            "message": (
                f"At the fullest layout that still fits (density {d_lo:.2f}) the bottom gap is "
                f"{at_lo.gap_in:.2f}in (want <= {max_gap_in:.2f}in). Spacing cannot fix this: "
                f"add roughly {deficit:.0f} more body lines of real content "
                f"(a bullet is 1-2 lines). Do not invent experience."
            ),
            "trace": trace,
        }

    # gap(density) increases with density; bisect within the feasible band.
    lo, hi = d_lo, DENSITY_MAX
    best = at_lo
    for _ in range(max_iters):
        mid = (lo + hi) / 2.0
        m = probe(mid)
        if m.pages > 1 or m.collisions:
            hi = mid  # too tight: overlapping or spilling
            continue
        if m.gap_in <= max_gap_in:
            best = m
        if abs(m.gap_in - target_gap_in) <= tol_in:
            break
        if m.gap_in > target_gap_in:
            hi = mid  # too much empty space: loosen
        else:
            lo = mid  # room to tighten

    write_density(tex, best.density)
    return {
        "ok": best.gap_in <= max_gap_in,
        "reason": "solved",
        "density": round(best.density, 3),
        "pages": best.pages,
        "measured_gap_in": round(best.gap_in, 3),
        "overfull": overfull_boxes(best.log),
        "collisions": best.collisions,
        "message": (
            f"Set \\setresumedensity{{{best.density:.3f}}} in {tex.name}: 1 page, "
            f"bottom gap {best.gap_in:.2f}in (target {target_gap_in:.2f}in) "
            f"after {len(trace)} compiles."
        ),
        "trace": trace,
    }


def report(tex: Path, *, max_gap_in: float = DEFAULT_MAX_BOTTOM_GAP_IN) -> dict:
    density = read_density(tex)
    m = measure(tex, density)
    boxes = overfull_boxes(m.log)
    problems = lint_source(tex)
    lines = [f"{tex.name} @ density {density:.3f}: {m.pages} page(s), bottom gap {m.gap_in:.2f}in"]

    for problem in problems:
        lines.append(f"  FAIL source: {problem}")

    if m.pages != 1:
        lines.append(
            "  FAIL pages: must be exactly 1. Run `--autofit`; if that reports "
            "content_too_long, cut content."
        )
    elif m.gap_in > max_gap_in:
        lines.append(
            f"  FAIL fill: {lines_equivalent(m.gap_in - DEFAULT_TARGET_GAP_IN):.0f} body lines "
            f"short of a full page. Run `--autofit` first; only add content if it says "
            f"content_too_short."
        )
    else:
        lines.append("  PASS layout")

    if m.collisions:
        lines.append(f"  FAIL overlap: {len(m.collisions)} pairs of text lines print on top")
        for c in m.collisions:
            lines.append(f"    {c}")
        lines.append("    Spacing is too tight. Run `--autofit`; do not hand-edit gaps.")
    else:
        lines.append("  PASS overlap (no colliding text)")

    if m.internal_gaps:
        lines.append(f"  FAIL item gap: {len(m.internal_gaps)} uneven hole(s) between bullets")
        for g in m.internal_gaps:
            lines.append(f"    {g}")
        lines.append(
            "    Density cannot fix this. Shorten the previous bullet so the last "
            "line is not flush to the margin."
        )
    else:
        lines.append("  PASS item gap (even spacing between bullets)")

    if boxes:
        lines.append(f"  {len(boxes)} overfull \\hbox (text past the right margin):")
        for b in boxes:
            pt = f"{b['pt']:.2f}pt" if b["pt"] is not None else "?"
            lines.append(f"    {tex.name}:{b['lines']} overfull by {pt}")
        lines.append("    Fix by shortening the wording on those lines, not by changing spacing.")
    else:
        lines.append("  PASS margins (no overfull boxes)")

    return {
        "tex": str(tex),
        "density": density,
        "pages": m.pages,
        "gap_in": round(m.gap_in, 3),
        "overfull": boxes,
        "collisions": m.collisions,
        "internal_gaps": m.internal_gaps,
        "lint": problems,
        "ok": (
            m.pages == 1
            and m.gap_in <= max_gap_in
            and not boxes
            and not problems
            and not m.collisions
            and not m.internal_gaps
        ),
        "message": "\n".join(lines),
    }


RAW_LAYOUT_RE = re.compile(
    r"^(?P<indent>[^%\n]*?)(?P<cmd>\\(?:vspace\*?|vskip|smallskip|medskip|bigskip|setlength|addtolength|baselineskip))",
    re.MULTILINE,
)


def lint_source(tex: Path) -> list[str]:
    """Reject raw layout commands in the document body.

    Spacing is the solver's job. An agent that hand-tunes \\vspace defeats the
    density model and produces exactly the misaligned output this harness exists
    to prevent.
    """
    text = tex.read_text(encoding="utf-8")
    problems: list[str] = []

    if "\\usepackage{resumestyle}" not in text:
        problems.append(
            f"{tex.name}: missing \\usepackage{{resumestyle}}. Layout must come from the "
            "shared style file, not a per-file preamble."
        )

    split = text.split("\\begin{document}", 1)
    if len(split) != 2:
        return problems
    offset = text.index("\\begin{document}")
    body = split[1]
    base_line = text[:offset].count("\n") + 1

    for m in RAW_LAYOUT_RE.finditer(body):
        line = base_line + body[: m.start()].count("\n")
        problems.append(
            f"{tex.name}:{line}: raw `{m.group('cmd')}` in the document body. "
            "Vertical spacing is owned by \\setresumedensity + resumestyle.sty; "
            "run `--autofit` instead of hand-tuning."
        )
    return problems


def preview(tex: Path, dpi: int = 110) -> Path:
    pdf = REPO_ROOT / "build" / f"{tex.stem}.pdf"
    if not pdf.exists():
        raise LayoutError(f"{pdf} missing; compile first")
    out = REPO_ROOT / "build" / f"{tex.stem}-preview"
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), "-singlefile", str(pdf), str(out)],
        check=True,
        capture_output=True,
    )
    return out.with_suffix(".png")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("tex", nargs="+")
    p.add_argument("--autofit", action="store_true", help="Solve and write \\setresumedensity")
    p.add_argument("--preview", action="store_true", help="Render build/<stem>-preview.png")
    p.add_argument("--target-gap", type=float, default=DEFAULT_TARGET_GAP_IN)
    p.add_argument("--max-gap", type=float, default=DEFAULT_MAX_BOTTOM_GAP_IN)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    results = []
    ok = True
    for raw in args.tex:
        tex = resolve_tex(raw)
        try:
            if args.autofit:
                res = autofit(tex, target_gap_in=args.target_gap, max_gap_in=args.max_gap)
            else:
                res = report(tex, max_gap_in=args.max_gap)
            if args.preview:
                res["preview"] = str(preview(tex))
        except LayoutError as exc:
            res = {"tex": str(tex), "ok": False, "message": str(exc)}
        results.append(res)
        ok = ok and bool(res.get("ok"))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(r.get("message", ""))
            if r.get("preview"):
                print(f"  preview: {r['preview']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
