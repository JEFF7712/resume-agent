# Repository Guidelines

`resume-agent` is a closed-loop LaTeX resume harness. See `README.md` for the rationale.

## Project Structure

- `resumestyle.sty`: shared layout. **Every** vertical spacing constant lives here.
- `example_resume.tex`: fictional example that exercises each macro.
- `hooks/resume_layout.py`: the density solver, layout report, source lint, preview renderer.
- `hooks/resume_validate.py`: compile + 1-page + bottom-fill gate. Writes `build/.resume-check.json`.
- `hooks/resume_after_edit.py`: cross-harness post-edit hook (Claude Code, Cursor, Codex).
- `tests/`: pytest suite. Integration tests are marked and skip without a TeX toolchain.

## Resume Editing

**Edit content only. You do not control spacing.**

```bash
python3 hooks/resume_layout.py <file.tex> --autofit   # solves \setresumedensity
python3 hooks/resume_layout.py <file.tex>             # report
python3 hooks/resume_layout.py <file.tex> --preview   # build/<stem>-preview.png
```

Raw `\vspace`, `\vskip`, `\setlength` and friends in a document **body** are rejected by the
hook. Use `\resumesection[<gap in pt>]{Title}` to open a section.

Every edit must end with:

1. Exactly 1 page.
2. Bottom gap ≤ `0.85in` (override with `RESUME_MAX_BOTTOM_GAP_IN`).
3. No overlapping text.
4. No overfull `\hbox`.

When a check fails:

1. **Run `--autofit` first.** Most fill failures are spacing, and it fixes them without
   touching content.
2. Only edit content when autofit says spacing cannot fix it. It reports which:
   `content_too_long` (cut roughly N body lines) or `content_too_short` (add roughly N lines
   of real content). Never invent experience to fill a page.
3. Overfull `\hbox` findings are wording problems. Shorten the flagged line.

## Build and Test

- `nix develop`: TeX Live, poppler-utils, ImageMagick, Python.
- `nix run .` / `nix run . -- all`: compile and validate.
- `uv run pytest`: full suite. `uv run ruff check .` and `uv run pyright` before submitting.

## Coding Style

Python targets 3.11+, four-space indent, type hints, `ruff` line length 100. Add comments
only where the reason behind the code is not obvious. LaTeX keeps two-space indent in nested
blocks and reuses the `\resume...` macros.

## Commits

One logical change per commit, imperative subject. Do not commit build artifacts, PDFs, or
personal resume content.
