#!/usr/bin/env python3
"""Rewrite a Gerber file by adding thermal-relief macros for GND pads.

This script uses gerbonara to parse the input Gerber file and identify pad-like
flash primitives associated with the GND net. It then generates new aperture
definitions for the corresponding original D codes, injects macro definitions
from a separate macros.gbr file, rewrites the GND flash selections to the new
thermal aperture D codes, and writes a modified Gerber file.

The output can also include a side-by-side HTML preview that renders the original
and rewritten Gerber content as SVG.
"""

from __future__ import annotations
import argparse
import base64
import pathlib
import re
import sys
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cairosvg
from PIL import Image, ImageFilter, ImageOps, ImageChops, ImageColor
import webbrowser
import math

try:
    from gerbonara.apertures import ApertureMacroInstance, CircleAperture, ObroundAperture, RectangleAperture
    from gerbonara.graphic_objects import Flash
    from gerbonara.rs274x import GerberFile
except Exception as exc:
    raise SystemExit(
        "gerbonara is required for this script. Install it in the project virtualenv "
        "and run this script with that interpreter."
    ) from exc


MACRO_NAME_RE = re.compile(r"^%AM([A-Za-z0-9_]+)\*$")
D_SELECTION_RE = re.compile(r"^D(\d{2,})\*$")
FLASH_LINE_RE = re.compile(r"D03\*$")


def extract_aper_function(attrs: Any) -> Optional[Any]:
    if not attrs:
        return None
    if isinstance(attrs, dict):
        return attrs.get('.AperFunction')
    if isinstance(attrs, (list, tuple)):
        for item in attrs:
            if isinstance(item, tuple) and len(item) == 2 and item[0] == '.AperFunction':
                return item[1]
    return None


def normalize_aper_function(aper_func: Any) -> Optional[str]:
    if aper_func is None:
        return None
    if isinstance(aper_func, (list, tuple)):
        return ",".join(str(item) for item in aper_func)
    return str(aper_func)


def is_gnd_net(attrs: Any) -> bool:
    if not attrs:
        return False
    net = attrs.get('.N') if isinstance(attrs, dict) else None
    if net is None:
        if isinstance(attrs, (list, tuple)):
            for item in attrs:
                if isinstance(item, tuple) and len(item) == 2 and item[0] == '.N':
                    net = item[1]
                    break
    if net is None:
        return False
    if isinstance(net, (list, tuple)):
        return any('GND' in str(item).upper() for item in net if item is not None)
    return 'GND' in str(net).upper()


def extract_macro_blocks(src_text: str) -> List[str]:
    lines = src_text.splitlines()
    blocks: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        if line.startswith('%AM') and line.endswith('*'):
            block_lines = [line]
            i += 1
            while i < len(lines):
                block_lines.append(lines[i].rstrip('\n'))
                if lines[i].rstrip('\n').endswith('*%'):
                    break
                i += 1
            blocks.append('\n'.join(block_lines))
            i += 1
            continue
        i += 1
    return blocks


def macro_name_from_block(block: str) -> Optional[str]:
    first_line = block.splitlines()[0].strip()
    match = MACRO_NAME_RE.match(first_line)
    return match.group(1) if match else None


def insert_macro_blocks(lines: List[str], macro_blocks: Sequence[str]) -> List[str]:
    existing_names = set()
    for line in lines:
        if line.startswith('%AM') and line.endswith('*'):
            match = MACRO_NAME_RE.match(line.strip())
            if match:
                existing_names.add(match.group(1))

    ready_blocks = []
    for block in macro_blocks:
        name = macro_name_from_block(block)
        if name is None:
            continue
        if name in existing_names:
            continue
        ready_blocks.append(block)

    if not ready_blocks:
        return lines

    insert_at = None
    for idx, line in enumerate(lines):
        if line.strip() == 'G04 Aperture macros list end*':
            insert_at = idx
            break

    if insert_at is None:
        raise ValueError("Could not find 'G04 Aperture macros list end*' in input file")

    insertion_lines = [
        'G04 Additional macros merged from macros.gbr*',
        *ready_blocks,
        'G04 Additional macros merged from macros.gbr end*',
    ]
    return lines[:insert_at] + insertion_lines + lines[insert_at:]


def format_number(value: float) -> str:
    return f"{value:.6f}".rstrip('0').rstrip('.') if '.' in f"{value:.6f}" else f"{value:.6f}"


def roundrect_dimensions(aper: ApertureMacroInstance) -> Tuple[float, float]:
    params = getattr(aper, 'parameters', None)
    if not params or len(params) < 9:
        raise ValueError('RoundRect aperture definition has no numeric parameter list')

    radius = float(params[0])
    coords = params[1:9]
    if len(coords) < 8:
        raise ValueError('Unexpected RoundRect parameter list length')

    xs = [float(coords[i]) for i in range(0, 8, 2)]
    ys = [float(coords[i]) for i in range(1, 8, 2)]
    width = max(xs) - min(xs) + 2.0 * radius
    height = max(ys) - min(ys) + 2.0 * radius
    return width, height


def build_thermal_aperture_definitions(gerber_file: GerberFile) -> Dict[int, Dict[str, Any]]:
    mapping: Dict[int, Dict[str, Any]] = {}
    for obj in gerber_file.objects:
        if not isinstance(obj, Flash):
            continue
        if not is_gnd_net(obj.attrs):
            continue
        aperture = getattr(obj, 'aperture', None)
        original_number = getattr(aperture, 'original_number', None)
        if original_number is None:
            continue
        if original_number in mapping:
            continue

        aper_func = normalize_aper_function(extract_aper_function(getattr(aperture, 'attrs', None)))
        if aper_func is None and isinstance(aperture, ApertureMacroInstance):
            aper_func = normalize_aper_function(extract_aper_function(aperture.attrs))

        new_number = original_number * 10 + 1
        if isinstance(aperture, CircleAperture):
            diameter = float(aperture.diameter)
            add_line = f"%ADD{new_number}THM_4SEG,{format_number(diameter)}X2X0*%"
        elif isinstance(aperture, ApertureMacroInstance) and getattr(aperture.macro, 'name', '').lower() == 'roundrect':
            width, height = roundrect_dimensions(aperture)
            add_line = f"%ADD{new_number}THM_RECT,{format_number(width)}X{format_number(height)}X0.5*%"
        elif isinstance(aperture, (ObroundAperture, RectangleAperture)):
            width = float(getattr(aperture, 'w', 0.0))
            height = float(getattr(aperture, 'h', 0.0))
            if width <= 0 or height <= 0:
                raise ValueError(
                    f"Cannot generate thermal aperture for original D{original_number}: invalid dimensions {width}x{height}"
                )
            add_line = f"%ADD{new_number}THM_RECT,{format_number(width)}X{format_number(height)}X0.5*%"
        else:
            raise ValueError(
                f"Cannot generate thermal aperture for original D{original_number}: unsupported aperture type {type(aperture).__name__}"
            )

        mapping[original_number] = {
            'original_number': original_number,
            'new_number': new_number,
            'aper_func': aper_func,
            'add_line': add_line,
        }
    return mapping


def insert_aperture_definitions(lines: List[str], aperture_defs: Sequence[Dict[str, Any]]) -> List[str]:
    insert_at = None
    for idx, line in enumerate(lines):
        if line.strip() == 'G04 APERTURE END LIST*':
            insert_at = idx
            break
    if insert_at is None:
        raise ValueError("Could not find 'G04 APERTURE END LIST*' in input file")

    insertion_lines: List[str] = []
    for aperture_def in aperture_defs:
        func_line = aperture_def.get('func_line')
        if func_line:
            insertion_lines.append(func_line)
        insertion_lines.append(aperture_def['add_line'])
        insertion_lines.append('%TD*%')

    return lines[:insert_at] + insertion_lines + lines[insert_at:]


def rewrite_body_lines(lines: List[str], gnd_mapping: Dict[int, Dict[str, Any]], flash_objects: Sequence[Flash]) -> List[str]:
    output_lines: List[str] = []
    current_original_d: Optional[int] = None
    current_output_d: Optional[int] = None
    flash_index = 0

    for line in lines:
        stripped = line.strip()
        match = D_SELECTION_RE.match(stripped)
        if match:
            current_original_d = int(match.group(1))
            current_output_d = current_original_d
            output_lines.append(line)
            continue

        if FLASH_LINE_RE.search(stripped):
            if flash_index >= len(flash_objects):
                raise ValueError('Flash line count does not match gerbonara flash objects')
            flash_obj = flash_objects[flash_index]
            flash_index += 1
            if current_original_d is None:
                raise ValueError('Flash line encountered before any D selection')

            desired_d = current_original_d
            if is_gnd_net(flash_obj.attrs) and current_original_d in gnd_mapping:
                desired_d = gnd_mapping[current_original_d]['new_number']

            if desired_d != current_output_d:
                output_lines.append(f'D{desired_d}*')
                current_output_d = desired_d
            output_lines.append(line)
            continue

        output_lines.append(line)

    if flash_index != len(flash_objects):
        raise ValueError('Flash object count mismatch after body rewrite')
    return output_lines


def normalize_svg_markup(svg_markup: str) -> str:
    if not svg_markup:
        return svg_markup
    if svg_markup.startswith('<?xml'):
        lines = svg_markup.splitlines()
        if len(lines) > 1:
            return '\n'.join(lines[1:])
        return ''
    return svg_markup


def render_svg_to_png(svg_markup: str) -> bytes:
    return cairosvg.svg2png(bytestring=svg_markup.encode('utf-8'))


def build_side_by_side_png(original_svg: str, rewritten_svg: str) -> bytes:
    original_bytes = render_svg_to_png(original_svg)
    rewritten_bytes = render_svg_to_png(rewritten_svg)
    original_img = Image.open(BytesIO(original_bytes)).convert('RGBA')
    rewritten_img = Image.open(BytesIO(rewritten_bytes)).convert('RGBA')
    width = original_img.width + rewritten_img.width + 20
    height = max(original_img.height, rewritten_img.height)
    composite = Image.new('RGBA', (width, height), (255, 255, 255, 255))
    composite.paste(original_img, (0, 0), original_img)
    composite.paste(rewritten_img, (original_img.width + 20, 0), rewritten_img)
    output = BytesIO()
    composite.save(output, format='PNG')
    return output.getvalue()


def compute_stats(gerber_file):
    from collections import Counter
    from gerbonara.graphic_objects import Flash
    total = 0
    gnd = 0
    counter = Counter()
    for obj in gerber_file.objects:
        if not isinstance(obj, Flash):
            continue
        aper = getattr(obj, 'aperture', None)
        aper_func = normalize_aper_function(extract_aper_function(getattr(aper, 'attrs', None))) if aper is not None else None
        attrs = getattr(obj, 'attrs', {}) or {}
        if aper_func is None and not attrs.get('.P') and not attrs.get('.N'):
            continue
        total += 1
        if not is_gnd_net(attrs):
            continue
        gnd += 1
        key = aper_func or type(aper).__name__
        key = str(key).split(',')[0]
        counter[key] += 1
    return {'total': total, 'gnd': gnd, 'breakdown': dict(counter)}


def build_preview_html_v2(original_svg: str, rewritten_svg: str, orig_stats: dict, rew_stats: dict, tint_css: str = 'rgba(255,0,0,0.10)', fg_color_css: Optional[str] = None, preview_3d_src: Optional[str] = None) -> str:
    def breakdown_html(breakdown):
        items = ''.join([f"<li>{k}: {v}</li>" for k, v in sorted(breakdown.items(), key=lambda x: -x[1])])
        return f"<ul>{items}</ul>"

    orig_stats_html = f"<p>Total pads: {orig_stats['total']}<br/>GND pads: {orig_stats['gnd']}</p>" + breakdown_html(orig_stats['breakdown'])
    rew_stats_html = f"<p>Total pads: {rew_stats['total']}<br/>GND pads: {rew_stats['gnd']}</p>" + breakdown_html(rew_stats['breakdown'])

    parts = []
    parts.append('<!DOCTYPE html>')
    parts.append('<html lang="en">')
    parts.append('<head>')
    parts.append('  <meta charset="UTF-8">')
    parts.append('  <title>Gerber Rewrite Preview</title>')
    parts.append('  <style>')
    parts.append('    body { margin: 0; padding: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; background: #f5f5f5; }')
    parts.append('    .wrap { display: flex; flex-wrap: wrap; gap: 12px; padding: 12px; box-sizing: border-box; align-items: stretch; }')
    parts.append('    .pane { flex: 1 1 320px; background: white; border: 1px solid #ccc; padding: 8px; box-sizing: border-box; position: relative; min-height: 0; }')
    parts.append('    .pane h2 { margin: 4px 8px; font-size: 0.95rem; }')
    parts.append('    .svg-container { width: 100%; min-height: 320px; overflow: auto; border: 1px dashed #ddd; position: relative; background: #fff; display: flex; align-items: center; justify-content: center; }')
    parts.append('    .svg-container svg, .svg-container img { width: 100%; height: auto; display: block; }')
    parts.append('    .overlay-svg { position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: none; pointer-events: none; }')
    parts.append('    .overlay-svg svg { position: relative; z-index: 1; }')
    parts.append(f'    .overlay-svg .tint {{ position: absolute; left: 0; top: 0; right: 0; bottom: 0; background: {tint_css}; pointer-events: none; mix-blend-mode: multiply; z-index: 2; }}')
    parts.append('    .info { padding: 8px; font-size: 0.85rem; color: #333; border-top: 1px solid #eee; margin-top: 8px; }')
    parts.append('    .controls { text-align: center; padding: 8px; }')
    parts.append('    .compare-btn, .reset-3d-btn { display: inline-block; margin: 0 8px; padding: 8px 12px; background: #1976d2; color: white; border-radius: 6px; cursor: pointer; user-select: none; }')
    parts.append('    .compare-btn.locked { background: #004ba0; }')
    parts.append('    .three-d-container { perspective: 1200px; touch-action: none; overflow: hidden; }')
    parts.append('    .three-d-image { transform-origin: center center; transform: perspective(1200px) rotateX(20deg) rotateY(-20deg); transition: transform 0.15s ease; cursor: grab; user-select: none; -webkit-user-drag: none; -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; touch-action: none; }')
    parts.append('    .three-d-image:active { cursor: grabbing; }')
    parts.append('    .divider { height: 100%; width: 1px; background: #eee; }')
    parts.append('  </style>')
    parts.append('</head>')
    parts.append('<body>')
    parts.append('  <div class="controls">')
    parts.append('    <span id="compareBtn" class="compare-btn">Visual Compare</span>')
    if preview_3d_src:
        parts.append('    <span id="reset3dBtn" class="reset-3d-btn">Reset 3D View</span>')
    parts.append('  </div>')
    parts.append('  <div class="wrap">')

    parts.append('    <div class="pane" id="leftPane">')
    parts.append('      <h2>Original Gerber</h2>')
    parts.append('      <div class="svg-container" id="leftContainer">')
    parts.append(original_svg)
    parts.append('        <div id="leftOverlay" class="overlay-svg">')
    parts.append(rewritten_svg)
    parts.append('          <div class="tint"></div>')
    parts.append('        </div>')
    parts.append('      </div>')
    parts.append('      <div class="info">' + orig_stats_html + '</div>')
    parts.append('    </div>')

    parts.append('    <div class="pane" id="rightPane">')
    parts.append('      <h2>Rewritten Gerber</h2>')
    parts.append('      <div class="svg-container" id="rightContainer">')
    parts.append(rewritten_svg)
    parts.append('      </div>')
    parts.append('      <div class="info">' + rew_stats_html + '</div>')
    parts.append('    </div>')

    if preview_3d_src:
        parts.append('    <div class="pane" id="threeDPane">')
        parts.append('      <h2>3D Preview</h2>')
        parts.append('      <div class="svg-container three-d-container" id="threeDContainer">')
        parts.append(f'        <img id="preview3dImage" class="three-d-image" src="{preview_3d_src}" alt="3D Preview">')
        parts.append('      </div>')
        parts.append('      <div class="info">Drag the 3D preview to adjust its angle.</div>')
        parts.append('    </div>')

    parts.append('  </div>')

    parts.append('  <script>')
    parts.append('    const btn = document.getElementById("compareBtn");')
    parts.append('    const leftOverlay = document.getElementById("leftOverlay");')
    parts.append('    const leftContainer = document.getElementById("leftContainer");')
    parts.append('    const preview3dImage = document.getElementById("preview3dImage");')
    parts.append('    const reset3dBtn = document.getElementById("reset3dBtn");')
    parts.append('    let locked = false;')
    parts.append('    let dragStart = null;')
    parts.append('    let currentX = -20;')
    parts.append('    let currentY = 20;')
    parts.append('    let zoom = 1.0;')
    parts.append('    function showOverlay(){ leftOverlay.style.display = "block"; const svgs = leftContainer.querySelectorAll("svg"); if(svgs[0]) { svgs[0].style.opacity = "0"; svgs[0].style.visibility = "hidden"; } }')
    parts.append('    function hideOverlay(){ leftOverlay.style.display = "none"; const svgs = leftContainer.querySelectorAll("svg"); if(svgs[0]) { svgs[0].style.opacity = "1"; svgs[0].style.visibility = "visible"; } }')
    parts.append('    function update3DTransform(){ if(!preview3dImage) return; preview3dImage.style.transform = `perspective(1200px) scale(${zoom}) rotateX(${currentY}deg) rotateY(${currentX}deg)`; }')
    parts.append('    function reset3DView(){ currentX = -20; currentY = 20; zoom = 1.0; update3DTransform(); }')
    parts.append('    if(btn){ btn.addEventListener("mousedown", (e)=>{ if(!locked) showOverlay(); });')
    parts.append('    window.addEventListener("mouseup", (e)=>{ if(!locked) hideOverlay(); if(dragStart) { dragStart = null; } });')
    parts.append('    btn.addEventListener("click", (e)=>{ locked = !locked; if(locked){ showOverlay(); btn.classList.add("locked"); } else { hideOverlay(); btn.classList.remove("locked"); } });')
    parts.append('    btn.addEventListener("touchstart", (e)=>{ if(!locked) showOverlay(); });')
    parts.append('    btn.addEventListener("touchend", (e)=>{ if(!locked) hideOverlay(); });')
    parts.append('    }')
    parts.append('    if(preview3dImage){ preview3dImage.addEventListener("pointerdown", (event)=>{ dragStart = { x: event.clientX, y: event.clientY, x0: currentX, y0: currentY }; preview3dImage.setPointerCapture(event.pointerId); });')
    parts.append('    preview3dImage.addEventListener("pointermove", (event)=>{ if(!dragStart) return; const dx = event.clientX - dragStart.x; const dy = event.clientY - dragStart.y; currentX = dragStart.x0 + dx * 0.2; currentY = Math.min(60, Math.max(-60, dragStart.y0 - dy * 0.2)); update3DTransform(); });')
    parts.append('    preview3dImage.addEventListener("pointerup", (event)=>{ dragStart = null; preview3dImage.releasePointerCapture(event.pointerId); });')
    parts.append('    preview3dImage.addEventListener("pointercancel", ()=>{ dragStart = null; });')
    parts.append('    preview3dImage.addEventListener("wheel", (event)=>{ if(!preview3dImage) return; event.preventDefault(); zoom = Math.min(2.0, Math.max(0.4, zoom - event.deltaY * 0.001)); update3DTransform(); });')
    parts.append('    }')
    parts.append('    if(reset3dBtn){ reset3dBtn.addEventListener("click", reset3DView); }')
    parts.append('    window.addEventListener("resize", ()=>{ if(preview3dImage){ update3DTransform(); } });')
    parts.append('    reset3DView();')
    parts.append('  </script>')

    parts.append('</body>')
    parts.append('</html>')

    return '\n'.join(parts)





def run_rewrite(input_path: pathlib.Path, macros_path: pathlib.Path, output_path: pathlib.Path, preview_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    original_text = input_path.read_text(encoding='utf-8', errors='replace')
    macro_text = macros_path.read_text(encoding='utf-8', errors='replace')
    macro_blocks = extract_macro_blocks(macro_text)
    if not macro_blocks:
        raise ValueError(f'No macro definitions found in {macros_path}')

    original_lines = original_text.splitlines()
    merged_macro_lines = insert_macro_blocks(original_lines, macro_blocks)

    gerber_file = GerberFile.from_string('\n'.join(merged_macro_lines), enable_include_dir=None)
    gnd_mapping = build_thermal_aperture_definitions(gerber_file)
    if not gnd_mapping:
        raise ValueError('No GND pad apertures were found to rewrite')

    aperture_defs = []
    for original_number, info in sorted(gnd_mapping.items(), key=lambda item: item[0]):
        aper_func = info.get('aper_func')
        func_line = f"%TA.AperFunction,{aper_func}*%" if aper_func else None
        aperture_defs.append({'func_line': func_line, 'add_line': info['add_line']})

    merged_with_apertures = insert_aperture_definitions(merged_macro_lines, aperture_defs)

    body_start = next(
        (idx for idx, line in enumerate(merged_with_apertures) if line.strip() == 'G04 APERTURE END LIST*'),
        None,
    )
    if body_start is None:
        raise ValueError("Could not find 'G04 APERTURE END LIST*' after macro insertion")
    body_start += 1
    header_lines = merged_with_apertures[:body_start]
    body_lines = merged_with_apertures[body_start:]

    flash_objects = [obj for obj in gerber_file.objects if isinstance(obj, Flash)]
    rewritten_body = rewrite_body_lines(body_lines, gnd_mapping, flash_objects)
    output_lines = header_lines + rewritten_body
    rewritten_text = '\n'.join(output_lines) + '\n'

    output_path.write_text(rewritten_text, encoding='utf-8')

    result: Dict[str, Any] = {
        'input_path': str(input_path),
        'output_path': str(output_path),
        'gnd_rewrites': [
            {
                'original_d': int(original_number),
                'new_d': int(info['new_number']),
                'aper_function': info.get('aper_func'),
                'definition': info['add_line'],
            }
            for original_number, info in sorted(gnd_mapping.items())
        ],
    }
    if preview_path is not None:
        original_svg_raw = str(gerber_file.to_svg(margin=0.5))
        rewritten_file = GerberFile.from_string(rewritten_text, enable_include_dir=None)
        rewritten_svg_raw = str(rewritten_file.to_svg(margin=0.5))

        original_svg_path = preview_path.with_name(preview_path.stem + '.original.svg')
        rewritten_svg_path = preview_path.with_name(preview_path.stem + '.rewritten.svg')
        original_svg_path.write_text(original_svg_raw, encoding='utf-8')
        rewritten_svg_path.write_text(rewritten_svg_raw, encoding='utf-8')

        original_svg = normalize_svg_markup(original_svg_raw)
        rewritten_svg = normalize_svg_markup(rewritten_svg_raw)
        orig_stats = compute_stats(gerber_file)
        rew_stats = compute_stats(rewritten_file)
        composite_png_bytes = build_side_by_side_png(original_svg, rewritten_svg)
        preview_image_path = preview_path.with_suffix('.png')
        preview_image_path.write_bytes(composite_png_bytes)
        preview_html = build_preview_html_v2(original_svg, rewritten_svg, orig_stats, rew_stats)
        preview_path.write_text(preview_html, encoding='utf-8')
        result['preview_path'] = str(preview_path)
        result['preview_image_path'] = str(preview_image_path)
        result['original_svg_path'] = str(original_svg_path)
        result['rewritten_svg_path'] = str(rewritten_svg_path)
        # include stats for caller use
        result['orig_stats'] = orig_stats
        result['rew_stats'] = rew_stats

    return result


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='thermalate — rewrite Gerber files by adding thermal macro apertures and generate previews.'
    )
    parser.add_argument('input', help='Source gerber file to rewrite')
    parser.add_argument('macros', help='Gerber file containing macro definitions (macros.gbr)')
    parser.add_argument('output', help='Output rewritten gerber file path')
    parser.add_argument('--preview', help='Write side-by-side HTML preview to this path')
    parser.add_argument('--open', dest='open_browser', action='store_true', help='Open the generated preview.html in the default web browser')
    parser.add_argument('--tint-color', dest='tint_color', help='Tint color used for the overlay (CSS color or hex), e.g. "#ff0000" or "rgba(255,0,0,0.1)"')
    parser.add_argument('--fg-color', dest='fg_color', help='Replace black fill/stroke in rendered SVGs with this color (CSS color or hex)')
    parser.add_argument('--render-3d', dest='render_3d', action='store_true', help='Generate a 3D-like PNG of the rewritten PCB')
    parser.add_argument('--pcb-width-mm', dest='pcb_width_mm', type=float, help='Target PCB width in mm for the 3D render (defaults to image width + 10mm)')
    parser.add_argument('--pcb-height-mm', dest='pcb_height_mm', type=float, help='Target PCB height in mm for the 3D render (defaults to image height + 10mm)')
    parser.add_argument('--corner-radius-mm', dest='corner_radius_mm', type=float, help='Corner radius in mm for the PCB background (defaults to 10%% of larger dimension)')
    return parser.parse_args(argv)


def _css_rgba_from_color(color_str: Optional[str], alpha: float) -> str:
    if not color_str:
        return f'rgba(255,0,0,{alpha})'
    try:
        if color_str.strip().startswith('rgba') or color_str.strip().startswith('rgb'):
            return color_str
        rgb = ImageColor.getrgb(color_str)
        return f'rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha})'
    except Exception:
        return f'rgba(255,0,0,{alpha})'


def _replace_black_in_svg(svg_text: str, fg_color: Optional[str]) -> str:
    if not fg_color:
        return svg_text
    # Simple textual replace for common black fills/strokes
    new = svg_text.replace('fill="black"', f'fill="{fg_color}"')
    new = new.replace("fill='black'", f"fill='{fg_color}'")
    new = new.replace('stroke="black"', f'stroke="{fg_color}"')
    new = new.replace("stroke='black'", f"stroke='{fg_color}'")
    return new


def render_3d_png_from_svg(svg_markup: str, out_path: pathlib.Path, fg_color: str, tint_color: str, pcb_w_mm: Optional[float], pcb_h_mm: Optional[float], corner_radius_mm: Optional[float]):
    """Render a simple 3D-like PNG from an SVG.

    This function creates a PCB-like image by recoloring black areas to fg_color (copper),
    applying a blurred tint overlay for conformal coating, and producing a rounded rectangular
    board background sized according to provided mm values (or derived from the SVG).
    """
    # Render SVG to PNG bytes
    png_bytes = cairosvg.svg2png(bytestring=svg_markup.encode('utf-8'))
    img = Image.open(BytesIO(png_bytes)).convert('RGBA')

    # Extract SVG physical size in mm if present
    def extract_mm_size(svg_text: str):
        m = re.search(r'width="([0-9.]+)mm"\s+height="([0-9.]+)mm"', svg_markup)
        if m:
            return float(m.group(1)), float(m.group(2))
        # fallback: use image pixels and assume 3.78 px/mm
        return img.width / 3.78, img.height / 3.78

    svg_w_mm, svg_h_mm = extract_mm_size(svg_markup)
    if pcb_w_mm is None:
        pcb_w_mm = svg_w_mm + 20.0
    if pcb_h_mm is None:
        pcb_h_mm = svg_h_mm + 20.0
    if corner_radius_mm is None:
        corner_radius_mm = 0.1 * max(pcb_w_mm, pcb_h_mm)

    px_per_mm = img.width / svg_w_mm if svg_w_mm > 0 else img.width / 25.4
    board_w_px = int(round(pcb_w_mm * px_per_mm))
    board_h_px = int(round(pcb_h_mm * px_per_mm))
    corner_px = int(round(corner_radius_mm * px_per_mm))

    # create a 3D board surface with thickness and edge shading
    tint_rgb = ImageColor.getrgb(tint_color) if tint_color else (255, 0, 0)
    board_color = tuple(max(0, min(255, int(c * 0.25))) for c in tint_rgb)
    side_color = tuple(max(0, c - 30) for c in board_color)

    from PIL import ImageDraw

    mask = Image.new('L', (board_w_px, board_h_px), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, board_w_px, board_h_px], radius=corner_px, fill=255)

    board_top = Image.new('RGBA', (board_w_px, board_h_px), board_color + (255,))
    board_top.putalpha(mask)

    # thickness: 1.6mm (user requested)
    thickness_px = max(1, int(round(px_per_mm * 1.6)))

    # create side plane with rounded ends matching board rounding
    side_plane = Image.new('RGBA', (board_w_px, thickness_px), (0, 0, 0, 0))
    sd = ImageDraw.Draw(side_plane)
    # rounded ends for the side plane
    sd.rounded_rectangle([0, 0, board_w_px, thickness_px], radius=max(1, corner_px // 2), fill=side_color + (255,))
    # apply vertical alpha gradient to side plane
    side_alpha = Image.new('L', (board_w_px, thickness_px), 0)
    sad = ImageDraw.Draw(side_alpha)
    sad.rounded_rectangle([0, 0, board_w_px, thickness_px], radius=max(1, corner_px // 2), fill=255)
    side_alpha = side_alpha.filter(ImageFilter.GaussianBlur(max(1, int(px_per_mm * 0.6))))
    side_plane.putalpha(side_alpha)

    def find_perspective_coeffs(src_pts, dst_pts):
        matrix = []
        for (x_src, y_src), (x_dst, y_dst) in zip(src_pts, dst_pts):
            matrix.append([x_src, y_src, 1, 0, 0, 0, -x_dst * x_src, -x_dst * y_src])
            matrix.append([0, 0, 0, x_src, y_src, 1, -y_dst * x_src, -y_dst * y_src])
        A = matrix
        B = [c for p in dst_pts for c in p]
        import numpy as _np
        A_np = _np.array(A)
        B_np = _np.array(B)
        try:
            res = _np.linalg.lstsq(A_np, B_np, rcond=None)[0]
            return tuple(res.tolist())
        except Exception:
            return None

    # derive a copper mask and layer from the original SVG render, matching drawn pixels and alpha
    alpha = img.split()[3]
    gray = img.convert('L')
    copper_mask = gray.point(lambda p: 255 if p < 240 else 0)
    copper_mask = ImageChops.multiply(copper_mask, alpha)

    fg_rgb = ImageColor.getrgb(fg_color) if fg_color else (184, 115, 51)
    copper_fill = Image.new('RGBA', img.size, fg_rgb + (255,))
    copper_fill.putalpha(copper_mask)

    glow = copper_mask.filter(ImageFilter.GaussianBlur(radius=max(1, px_per_mm * 0.5)))
    glow_color = tuple(min(255, int(c * 1.4)) for c in fg_rgb)
    copper_glow = Image.new('RGBA', img.size, glow_color + (90,))
    copper_glow.putalpha(glow)
    copper_layer = Image.alpha_composite(copper_fill, copper_glow)

    # place copper on board top — scale to fit within the board while preserving aspect and center it
    scale = min(board_w_px / max(1, img.width), board_h_px / max(1, img.height))
    target_w = max(1, int(round(img.width * scale)))
    target_h = max(1, int(round(img.height * scale)))
    copper_resized = copper_layer.resize((target_w, target_h), resample=Image.LANCZOS)
    composite_top = board_top.copy()
    paste_x = (board_w_px - target_w) // 2
    paste_y = (board_h_px - target_h) // 2
    composite_top.paste(copper_resized, (paste_x, paste_y), copper_resized)

    board_offset_y = int(board_h_px * 0.18)
    board_top_shrink = 0.75
    board_dst_top_w = int(board_w_px * board_top_shrink)
    board_dx = (board_w_px - board_dst_top_w) // 2
    board_output_h = board_h_px + board_offset_y + thickness_px + int(px_per_mm * 2)
    board_dst_pts = [
        (board_dx, board_offset_y),
        (board_dx + board_dst_top_w, board_offset_y),
        (board_w_px, board_h_px),
        (0, board_h_px),
    ]
    src_board_pts = [(0, 0), (board_w_px, 0), (board_w_px, board_h_px), (0, board_h_px)]
    board_coeffs = find_perspective_coeffs(src_board_pts, board_dst_pts)
    if board_coeffs:
        board_top_tilted = composite_top.transform((board_w_px, board_output_h), Image.PERSPECTIVE, board_coeffs, Image.BICUBIC)
    else:
        board_top_tilted = composite_top

    side_dst_pts = [
        (0, board_h_px),
        (board_w_px, board_h_px),
        (board_w_px - int(board_w_px * 0.08), board_h_px + thickness_px),
        (int(board_w_px * 0.08), board_h_px + thickness_px),
    ]
    src_side_pts = [(0, 0), (board_w_px, 0), (board_w_px, thickness_px), (0, thickness_px)]
    side_coeffs = find_perspective_coeffs(src_side_pts, side_dst_pts)
    if side_coeffs:
        side_tilted = side_plane.transform((board_w_px, board_output_h), Image.PERSPECTIVE, side_coeffs, Image.BICUBIC)
    else:
        side_tilted = side_plane

    shadow_shape = Image.new('L', (board_w_px, board_output_h), 0)
    sd = ImageDraw.Draw(shadow_shape)
    sd.rounded_rectangle([0, board_offset_y, board_w_px, board_h_px], radius=corner_px, fill=255)
    shadow = shadow_shape.filter(ImageFilter.GaussianBlur(radius=px_per_mm * 1.5))
    shadow_img = Image.new('RGBA', (board_w_px, board_output_h), (0, 0, 0, 0))
    shadow_img.paste(Image.new('RGBA', (board_w_px, board_output_h), (0, 0, 0, 120)), (0, 0), shadow)

    margin_px = max(1, int(px_per_mm * 6.0))
    canvas_w_px = board_w_px + margin_px * 2
    canvas_h_px = board_output_h + margin_px * 2

    composed = Image.new('RGBA', (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
    composed.paste(shadow_img, (margin_px, margin_px), shadow_img)
    composed.paste(side_tilted, (margin_px, margin_px), side_tilted)
    composed.paste(board_top_tilted, (margin_px, margin_px), board_top_tilted)

    # add a light coating over the board for a soft conformal finish
    coating_color = tuple(max(0, min(255, int(c * 0.4))) for c in tint_rgb)
    coating = Image.new('RGBA', (canvas_w_px, canvas_h_px), coating_color + (100,))
    coating = coating.filter(ImageFilter.GaussianBlur(radius=px_per_mm * 0.4))
    composed = Image.alpha_composite(composed, coating)

    # build a final mask from the transformed board top and side alpha channels so
    # the visible result is clipped to the board silhouette (this guarantees all
    # four corners are rounded according to the original rounded_rectangle)
    top_alpha = board_top_tilted.split()[3]
    side_alpha = side_tilted.split()[3]
    # combine top and side alphas (per-pixel max)
    combined_alpha = ImageChops.lighter(top_alpha, side_alpha)
    # place combined alpha into a full-canvas mask at the margin offset
    full_mask = Image.new('L', (canvas_w_px, canvas_h_px), 0)
    full_mask.paste(combined_alpha, (margin_px, margin_px))

    # apply mask so only board-shaped pixels remain
    final = Image.new('RGBA', (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
    final.paste(composed, (0, 0), full_mask)

    final.save(out_path, format='PNG')


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    input_path = pathlib.Path(args.input)
    macros_path = pathlib.Path(args.macros)
    output_path = pathlib.Path(args.output)
    preview_path = pathlib.Path(args.preview) if args.preview else None

    result = run_rewrite(input_path, macros_path, output_path, preview_path)
    print(f"Rewrote '{result['input_path']}' to '{result['output_path']}'")
    print(f"Created {len(result['gnd_rewrites'])} new thermal aperture definitions")
    for rewrite in result['gnd_rewrites']:
        print(
            f"  D{rewrite['original_d']} -> D{rewrite['new_d']} ({rewrite['aper_function']})"
        )
    if preview_path is not None:
        print(f"Preview written to '{result['preview_path']}'")
        print(f"Preview image written to '{result['preview_image_path']}'")
        print(f"Original SVG written to '{result['original_svg_path']}'")
        print(f"Rewritten SVG written to '{result['rewritten_svg_path']}'")

    # post-processing: handle color overrides and open browser / 3D render
    if preview_path is not None:
        orig_svg_path = pathlib.Path(result['original_svg_path'])
        rew_svg_path = pathlib.Path(result['rewritten_svg_path'])
        orig_svg = orig_svg_path.read_text(encoding='utf-8')
        rew_svg = rew_svg_path.read_text(encoding='utf-8')

        # replace black fills/strokes if requested
        if getattr(args, 'fg_color', None):
            orig_svg = _replace_black_in_svg(orig_svg, args.fg_color)
            rew_svg = _replace_black_in_svg(rew_svg, args.fg_color)
            orig_svg_path.write_text(orig_svg, encoding='utf-8')
            rew_svg_path.write_text(rew_svg, encoding='utf-8')

        # build preview HTML with tint/fg
        tint_css = _css_rgba_from_color(getattr(args, 'tint_color', None), 0.10)
        orig_stats = result.get('orig_stats') or compute_stats(GerberFile.from_string(input_path.read_text(encoding='utf-8', errors='replace'), enable_include_dir=None))
        rew_stats = result.get('rew_stats') or compute_stats(GerberFile.from_string(output_path.read_text(encoding='utf-8', errors='replace'), enable_include_dir=None))
        preview_html = build_preview_html_v2(orig_svg, rew_svg, orig_stats, rew_stats, tint_css=tint_css, fg_color_css=args.fg_color)
        preview_path.write_text(preview_html, encoding='utf-8')

        # render 3D if requested
        preview_3d_src = None
        if getattr(args, 'render_3d', False):
            out3 = preview_path.with_name(preview_path.stem + '.3d.png')
            render_3d_png_from_svg(rew_svg, out3, args.fg_color or '#b87333', args.tint_color or '#ff0000', args.pcb_width_mm, args.pcb_height_mm, args.corner_radius_mm)
            print(f"3D render written to '{out3}'")
            result['preview_3d_path'] = str(out3)
            preview_3d_src = out3.name

        # rebuild preview HTML if a 3D preview image exists
        if preview_path is not None and preview_3d_src:
            preview_html = build_preview_html_v2(orig_svg, rew_svg, orig_stats, rew_stats, tint_css=tint_css, fg_color_css=args.fg_color, preview_3d_src=preview_3d_src)
            preview_path.write_text(preview_html, encoding='utf-8')

        # open browser if requested
        if getattr(args, 'open_browser', False):
            webbrowser.open(preview_path.resolve().as_uri())

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
