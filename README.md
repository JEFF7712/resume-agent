# resume-agent

A closed-loop harness that lets coding agents edit a LaTeX resume **without wrecking the
spacing**.

## The problem

Agents edit LaTeX blind. They cannot see the rendered page, so when a resume runs long or
short they start nudging the dozen hand-tuned `\vspace{-7pt}` values scattered through the
template. Each nudge is a guess, guesses compound, and you end up with headings crashing
into body text and dates that no longer line up.

A pass/fail check does not fix this. Telling an agent "this is 1.8in too short" still leaves
it guessing *which* of the magic numbers to change.

## The approach

**Take vertical spacing away from the agent and give it to a solver.**

1. All layout lives in `resumestyle.sty`, scaled by a single scalar: `\setresumedensity{...}`.
   Resume bodies contain content only.
2. `--autofit` bisects that scalar against the actually-compiled PDF until the page is
   exactly one page and comfortably full. Deterministic, no model in the loop.
3. Raw `\vspace` / `\vskip` / `\setlength` in a document body is a hard error.
4. When spacing genuinely cannot fix it, the harness says which way and by how much:
   *"cut roughly 4 body lines"* or *"add roughly 6 lines of real content"*.

The agent's job shrinks to what it is actually good at: writing good bullets.

## What it checks

| Check | Why it exists |
| --- | --- |
| Exactly 1 page | The usual hard requirement. |
| Bottom gap ≤ 0.85in | A one-page resume with a 2in empty tail is still a bad resume. |
| **No overlapping text** | Over-tight spacing makes headings print *on top of* body text. LaTeX reports no error for this, and page count and bottom gap are both blind to it. Detected geometrically from the PDF via `pdftotext -bbox`. |
| **No overfull `\hbox`** | Text running past the right margin, reported with `.tex` line numbers. A wording problem, never a spacing one. |
| No raw layout commands in a body | Stops the hand-tuning that causes all of the above. |

The overlap check is not theoretical. While building this, the solver happily "solved" a
case at density 1.76 that had section headings overlapping three separate body lines. Page
count said 1, bottom gap said 0.47in, and the page was garbage.

## Usage

```bash
nix develop                                              # TeX + poppler + imagemagick

python3 hooks/resume_layout.py example_resume.tex --autofit   # solve and write the density
python3 hooks/resume_layout.py example_resume.tex             # report fill / overlap / margins
python3 hooks/resume_layout.py example_resume.tex --preview   # PNG, so an agent can look
python3 hooks/resume_layout.py example_resume.tex --json      # machine-readable

nix run .                                                # compile + validate the example
nix run . -- all                                         # every *resume.tex in the repo
```

Example report:

```
example_resume.tex @ density 0.270: 1 page(s), bottom gap 0.44in
  PASS layout
  PASS overlap (no colliding text)
  PASS margins (no overfull boxes)
```

Example of the solver declining to paper over a content problem:

```
example_resume.tex only fits on 1 page at a density that makes text overlap
(3 collisions, e.g. "Minor in Statistics | GPA: 3.81" overlaps "Experience" by 7.2pt).
At the tightest legible density (1.32) it is 2 pages with 0.71in spilling over.
Spacing cannot fix this: cut roughly 4 body lines of content.
```

## Agent integration

`hooks/resume_after_edit.py` is one script wired into three harnesses. It rebuilds the
edited variant, blocks on failure, and tells the agent to run `--autofit` instead of
touching spacing.

| Harness | Config | Trigger |
| --- | --- | --- |
| Claude Code | `.claude/settings.json` | `PostToolUse` on `Edit`/`Write`/`MultiEdit` |
| Cursor | `.cursor/hooks.json` | `afterFileEdit`, `postToolUse`, `stop` follow-up |
| Codex | `.codex/hooks.json` (+ `hooks = true` in `.codex/config.toml`) | `PostToolUse` on `apply_patch`/`Edit`/`Write` |

Status is written to `build/.resume-check.json`.

## Adopting it for your own resume

1. Copy `resumestyle.sty`, `hooks/`, and the harness config directory you use.
2. Name your source `resume.tex` or `<variant>_resume.tex` (that is how the hook recognizes it).
3. Start from `example_resume.tex`: keep the preamble, replace the content.
4. Run `--autofit` once. Re-run it whenever you add or cut content.

Writing a section uses `\resumesection[<gap in pt>]{Title}`, never `\vspace{...}\section{...}`.

## Density, briefly

`\resumedensity` scales every negative gap in `resumestyle.sty`. `1.0` is the original
hand-tuned template. Higher tightens, lower loosens, and negative values turn the gaps
positive, which is the only way to fill a genuinely thin page without inventing experience.
Bottom gap increases monotonically with density, which is what makes the bisection valid.

The solver bounds this on both sides: it will not go tighter than the point where text
starts overlapping, and it reports a content problem rather than returning an ugly page.

## Requirements

`latexmk` (TeX Live), `pdfinfo` / `pdftoppm` / `pdftotext` (poppler-utils), and ImageMagick.
The flake provides all of them.

## Tests

```bash
uv run pytest          # unit + integration
uv run ruff check .
```

## License

MIT. The layout in `resumestyle.sty` derives from
[jakegut/resume](https://github.com/jakegut/resume), based on
[sb2nov/resume](https://github.com/sb2nov/resume), both MIT.
