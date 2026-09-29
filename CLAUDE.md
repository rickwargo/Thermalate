# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Thermalate rewrites KiCad/Gerber copper-layer files so that GND-net pads get thermal-relief
apertures (spoked pads) instead of solid flashes, using custom aperture macros. It also
generates an HTML/SVG/PNG preview (including a pseudo-3D render) comparing the original and
rewritten Gerber layers.

`thermalate.py` is the current, actively developed entry point — it is a single ~1200-line
module containing the whole pipeline (Gerber parsing/rewriting, SVG preview, and 3D PNG
rendering). `merge_macros.py` is an earlier, standalone script that only does the macro-block
merge step (no aperture remapping); treat it as a reference/superseded predecessor of the macro
insertion logic in `thermalate.py`. `main.py`, `gerber_gnd_finder.py`, and
`gerber_gnd_thermal_rewrite.py` are empty stub files — no logic lives there yet.

## Environment & commands

The project uses a local venv at `.venv` (Python 3.14). Activate it or invoke its interpreter
directly:

```bash
source .venv/bin/activate
# or
.venv/bin/python thermalate.py ...
```

Dependencies (`requirements.txt`): `gerbonara` (Gerber parsing/rewriting/SVG export),
`pygerber` (installed from the Argmaster GitHub repo), `gerber_writer`, `cairosvg` (SVG->PNG),
plus `Pillow`/`numpy` as transitive deps for the 3D preview raster work.

```bash
pip install -r requirements.txt
```

There is no test suite, linter, or build step configured in this repo.

### Running the rewrite

```bash
.venv/bin/python thermalate.py <input.gbr> macros.gbr <output.gbr> \
    --preview preview.html --open \
    --fg-color "#b87333" --tint-color "#ff0000" \
    --render-3d --pcb-width-mm 100 --pcb-height-mm 80 --corner-radius-mm 3
```

- `input` — source copper-layer Gerber (e.g. `copper.gbr`).
- `macros` — Gerber file holding the `%AM...*%` thermal macro definitions to merge in
  (`macros.gbr`, which defines `THM_3SEG`, `THM_4SEG`, `THM_RECT`).
- `output` — path to write the rewritten Gerber.
- `--preview` — writes a side-by-side HTML preview; also produces
  `<preview-stem>.original.svg`, `<preview-stem>.rewritten.svg`, and `<preview-stem>.png`
  (composite raster) alongside it.
- `--render-3d` — additionally writes `<preview-stem>.3d.png`, a pseudo-3D PCB render, and
  embeds it in the preview HTML. It looks for an optional `edgecuts.gbr` in the cwd to derive
  a precise board outline (falls back to a rounded-rectangle bounding box otherwise).
- `--open` opens the generated preview in the default browser (`webbrowser.open`).

`merge_macros.py` can be run standalone for the simpler "just merge macro blocks" case:

```bash
.venv/bin/python merge_macros.py <input.gbr> <macros.gbr> <output.gbr>
```

## Architecture (thermalate.py pipeline)

The pipeline in `run_rewrite()` proceeds as text-level Gerber surgery interleaved with
`gerbonara` parsing, not a full AST rewrite:

1. **Macro merge** (`extract_macro_blocks`, `insert_macro_blocks`): parse `%AM...*%` blocks out
   of `macros.gbr` and splice any not already present into the input file's line list, right
   before the `G04 Aperture macros list end*` marker.
2. **Parse with gerbonara** (`GerberFile.from_string`) on the macro-merged text to get typed
   `Flash`/`Line`/`Arc` objects and their apertures.
3. **Identify GND pads** (`is_gnd_net`): a `Flash` is GND if its `.N` (net) attribute contains
   `"GND"` (case-insensitive). This relies on net-name attributes (`%TO.N,GND*%`-style) being
   present in the source Gerber.
4. **Generate thermal apertures** (`build_thermal_aperture_definitions`): for each distinct
   original D-code used by a GND flash, synthesize a new D-code as `original*10 + 1` and emit an
   `%ADD<new>THM_4SEG,...*%` / `THM_RECT,...*%` aperture-definition line sized to match the
   original aperture's geometry:
   - `CircleAperture` → `THM_4SEG` sized by diameter.
   - `RoundRect` macro aperture → `THM_RECT` sized from its parameter list
     (`roundrect_dimensions`).
   - `ObroundAperture`/`RectangleAperture` → `THM_RECT` sized by width/height.
   - Anything else raises `ValueError` (unsupported aperture type) — the script deliberately
     does not silently skip unhandled pad shapes.
5. **Insert new aperture definitions** (`insert_aperture_definitions`) before
   `G04 APERTURE END LIST*`, each preceded by its `%TA.AperFunction,...*%` attribute line so the
   new D-code inherits the original's function metadata.
6. **Rewrite the body** (`rewrite_body_lines`): walk the Gerber body line-by-line, tracking the
   currently-selected `D<n>*` aperture. On each `D03*` (flash) line, cross-reference against the
   parsed `flash_objects` (by position, in file order) to decide whether this flash is GND and
   should be redirected to the new thermal D-code, inserting an extra `D<new>*` selection line
   only when the aperture actually needs to change. This keeps the rewritten file a
   line-for-line-compatible Gerber, not a re-serialization from the gerbonara object model — it
   assumes the flash line order exactly matches the order gerbonara parsed them in, and raises
   `ValueError` on any count mismatch.
7. **Preview generation** (optional, `preview_path` given): re-parses both the original and
   rewritten Gerber text via gerbonara's `to_svg()`, writes the two SVGs, computes per-net pad
   stats (`compute_stats`), composites a side-by-side PNG (`build_side_by_side_png`, via
   `cairosvg`), and builds an HTML page (`build_preview_html_v2`) with a 2D side-by-side view
   plus a WebGL 3D viewer built from vendored Three.js modules in `scripts/` (`three.umd.min.js`,
   `OrbitControls.umd.js`, `SVGLoader.umd.js` — vendored, not CDN-loaded, so the preview works
   over `file://`). The 3D viewer extrudes SVG paths client-side in the browser.
8. **3D PNG render** (optional, `--render-3d`, `render_3d_png_from_svg`): a separate,
   server-side (PIL/cairosvg) pseudo-3D board render — distinct from the in-browser Three.js
   viewer in the preview HTML. It rasterizes the rewritten SVG, derives a copper mask (or draws
   copper vector-accurately by re-walking `gerbonara` `Flash`/`Line`/`Arc` objects when the
   source `.gbr` path is available), builds a rounded/edge-cut-shaped board silhouette, and
   applies a perspective transform (`find_perspective_coeffs`, a manual homography solve via
   `numpy.linalg.lstsq`) to fake a tilted 3D view with a soft shadow and conformal-coating tint.

## Key invariant to preserve when editing `thermalate.py`

The text-splicing approach means several functions rely on exact Gerber marker strings existing
verbatim in the input file: `G04 Aperture macros list end*`, `G04 APERTURE END LIST*`. If you
change how input files are generated (e.g. a different KiCad export settings), these anchors
must still be present or `run_rewrite` raises `ValueError` immediately rather than degrading
silently. Similarly, `rewrite_body_lines` assumes `D03*` (flash) lines appear in the body in the
same order gerbonara enumerates `Flash` objects — anything that reorders or filters objects
during parsing (e.g. upgrading gerbonara) needs re-validation against this assumption.
