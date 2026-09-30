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
    from gerbonara.graphic_objects import Flash, Line, Arc
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

        if isinstance(aperture, ApertureMacroInstance) and getattr(aperture.macro, 'name', '').upper().startswith('THM_'):
            # Already a thermal-relief aperture from a previous rewrite; leave it alone so
            # re-running the tool on already-rewritten output is a no-op rather than an error.
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

    # embed rewritten SVG content as hidden data for the Three.js viewer
    safe_rewritten_svg = rewritten_svg.replace('</script>', '<\\/script>')

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
    parts.append('    .info { padding: 8px; font-size: 0.85rem; color: #333; border-top: 1px solid #eee; margin-top: 8px; }')
    parts.append('    .controls { text-align: center; padding: 8px; }')
    parts.append('    .compare-btn { display: inline-block; margin: 0 8px; padding: 8px 12px; background: #1976d2; color: white; border-radius: 6px; cursor: pointer; user-select: none; }')
    parts.append('    .three-d-canvas { width: 100%; height: 480px; display: block; touch-action: none; -webkit-user-select: none; user-select: none; cursor: grab; }')
    parts.append('    .three-d-unavailable { padding: 16px; font-size: 0.85rem; color: #a33; background: #fff5f5; border: 1px dashed #e0b4b4; border-radius: 4px; }')
    parts.append('  </style>')
    # include three.js and SVGLoader + OrbitControls from CDN as ES modules
    parts.append('</head>')
    parts.append('<body>')

    parts.append('  <div class="controls">')
    parts.append('    <span id="compareBtn" class="compare-btn">Visual Compare</span>')
    parts.append('  </div>')

    parts.append('  <div class="wrap">')

    parts.append('    <div class="pane" id="leftPane">')
    parts.append('      <h2>Original Gerber</h2>')
    parts.append('      <div class="svg-container" id="leftContainer">')
    parts.append(original_svg)
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

    # 3D pane
    parts.append('    <div class="pane" id="threeDPane">')
    parts.append('      <h2>Interactive 3D Preview</h2>')
    parts.append('      <canvas id="threeCanvas" class="three-d-canvas"></canvas>')
    parts.append('      <div class="info">Use mouse/touch to rotate and zoom. Copper is extruded; board outline is used for the PCB shape.</div>')
    parts.append('    </div>')

    parts.append('  </div>')

    # Hidden container with the rewritten SVG for the client-side loader
    parts.append(f'  <div id="svgData" style="display:none;">{safe_rewritten_svg}</div>')

    # Use local vendored ES module and helpers (for file:// compatibility)
    # Inline vendored UMD builds for Chrome file:// compatibility
    try:
        three_umd = pathlib.Path('scripts/three.umd.min.js').read_text(encoding='utf-8')
    except Exception:
        three_umd = None
    try:
        orbit_umd = pathlib.Path('scripts/OrbitControls.umd.js').read_text(encoding='utf-8')
    except Exception:
        orbit_umd = None
    try:
        svgloader_umd = pathlib.Path('scripts/SVGLoader.umd.js').read_text(encoding='utf-8')
    except Exception:
        svgloader_umd = None

    if three_umd:
        parts.append('  <script>')
        parts.append(three_umd)
        parts.append('  </script>')
    else:
        parts.append('  <script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>')

    if orbit_umd:
        parts.append('  <script>')
        parts.append(orbit_umd)
        parts.append('  </script>')
    else:
        parts.append('  <script src="https://unpkg.com/three@0.160.0/examples/js/controls/OrbitControls.js"></script>')

    if svgloader_umd:
        parts.append('  <script>')
        parts.append(svgloader_umd)
        parts.append('  </script>')
    else:
        parts.append('  <script src="https://unpkg.com/three@0.160.0/examples/js/loaders/SVGLoader.js"></script>')

    parts.append('  <script>')
    parts.append('    (function(){')
    parts.append('      const canvas = document.getElementById("threeCanvas");')
    parts.append('      function showUnavailable(message){')
    parts.append('        canvas.style.display = "none";')
    parts.append('        const msg = document.createElement("div");')
    parts.append('        msg.className = "three-d-unavailable";')
    parts.append('        msg.textContent = message;')
    parts.append('        canvas.parentNode.insertBefore(msg, canvas);')
    parts.append('      }')
    parts.append('      try {')
    parts.append('      const svgText = document.getElementById("svgData").innerHTML;')
    parts.append('      const parser = new DOMParser();')
    parts.append('      const doc = parser.parseFromString(svgText, "image/svg+xml");')
    parts.append('      const allPaths = Array.from(doc.querySelectorAll("path, rect, circle, ellipse, polygon, polyline"));')
    parts.append('      const copperElements = allPaths.filter(el => {')
    parts.append('        const fill = el.getAttribute("fill") || (el.style && el.style.fill) || null;')
    parts.append('        const stroke = el.getAttribute("stroke") || (el.style && el.style.stroke) || null;')
    parts.append('        function isBlackColor(v){ if(!v) return false; const s = String(v).trim().toLowerCase(); return s.indexOf("#000") === 0 || s.indexOf("black") === 0; }')
    parts.append('        return isBlackColor(fill) || isBlackColor(stroke) || (!fill && !stroke);')
    parts.append('      });')
    parts.append('      let boardPath = null;')
    parts.append('      try {')
    parts.append('        const profileCand = doc.querySelector("path[id*=Profile], path[class*=Profile], path[fill=none]");')
    parts.append('        boardPath = profileCand || doc.querySelector("path");')
    parts.append('      } catch (e) {')
    parts.append('        boardPath = doc.querySelector("path");')
    parts.append('      }')
    parts.append('      const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });')
    parts.append('    renderer.setPixelRatio(window.devicePixelRatio || 1);')
    parts.append('      const scene = new THREE.Scene();')
    parts.append('      const camera = new THREE.PerspectiveCamera(45, canvas.clientWidth / Math.max(canvas.clientHeight, 1), 0.1, 10000);')
    parts.append('      camera.position.set(0, -200, 300);')
    parts.append('      const controls = new THREE.OrbitControls(camera, renderer.domElement);')
    parts.append('      controls.enablePan = true; controls.enableDamping = true; controls.dampingFactor = 0.1;')
    parts.append('      const hemi = new THREE.HemisphereLight(0xffffff, 0x444444, 0.8); scene.add(hemi);')
    parts.append('      const dir = new THREE.DirectionalLight(0xffffff, 0.6); dir.position.set(0, 200, 200); scene.add(dir);')
    parts.append('      const loader = new THREE.SVGLoader();')
    parts.append('      function addShapesFromPaths(elements, material, depthMm){')
    parts.append('        elements.forEach(el => {')
    parts.append('          try {')
    parts.append('            const data = loader.parse(el.outerHTML);')
    parts.append('            data.paths.forEach(p => {')
    parts.append('              const shapes = p.toShapes(true);')
    parts.append('              shapes.forEach(s => {')
    parts.append('                const geom = new THREE.ExtrudeGeometry(s, { depth: depthMm, bevelEnabled: false });')
    parts.append('                const mesh = new THREE.Mesh(geom, material);')
    parts.append('                geom.computeBoundingBox();')
    parts.append('                const bb = geom.boundingBox;')
    parts.append('                const cx = (bb.max.x + bb.min.x) / 2; const cy = (bb.max.y + bb.min.y) / 2;')
    parts.append('                mesh.position.x -= cx; mesh.position.y -= cy;')
    parts.append('                scene.add(mesh);')
    parts.append('              });')
    parts.append('            });')
    parts.append('          } catch (e) { console.warn("SVG parse err", e); }')
    parts.append('        });')
    parts.append('      }')
    parts.append('      const copperMat = new THREE.MeshPhongMaterial({ color: 0xb87333, specular: 0x444444, shininess: 30 });')
    parts.append('      const boardMat = new THREE.MeshPhongMaterial({ color: 0x113322, side: THREE.DoubleSide });')
    parts.append('      const copperDepth = 0.035;')
    parts.append('      addShapesFromPaths(copperElements, copperMat, copperDepth);')
    parts.append('      if (boardPath) {')
    parts.append('        try {')
    parts.append('          const data = loader.parse(boardPath.outerHTML);')
    parts.append('          data.paths.forEach(p => {')
    parts.append('            const shapes = p.toShapes(true);')
    parts.append('            shapes.forEach(s => {')
    parts.append('              const geom = new THREE.ExtrudeGeometry(s, { depth: 1.6, bevelEnabled: false });')
    parts.append('              const mesh = new THREE.Mesh(geom, boardMat);')
    parts.append('              geom.computeBoundingBox();')
    parts.append('              const bb = geom.boundingBox;')
    parts.append('              const cx = (bb.max.x + bb.min.x) / 2; const cy = (bb.max.y + bb.min.y) / 2;')
    parts.append('              mesh.position.x -= cx; mesh.position.y -= cy;')
    parts.append('              scene.add(mesh);')
    parts.append('            });')
    parts.append('          });')
    parts.append('        } catch (e) { console.warn("board parse err", e); }')
    parts.append('      }')
    parts.append('      function fitCamera(){')
    parts.append('        const box = new THREE.Box3().setFromObject(scene);')
    parts.append('        const size = box.getSize(new THREE.Vector3());')
    parts.append('        const center = box.getCenter(new THREE.Vector3());')
    parts.append('        const maxDim = Math.max(size.x, size.y, size.z);')
    parts.append('        const fov = camera.fov * (Math.PI / 180);')
    parts.append('        const cameraZ = Math.abs(maxDim / 2 / Math.tan(fov / 2));')
    parts.append('        camera.position.set(center.x, center.y - cameraZ * 0.1, cameraZ * 1.2);')
    parts.append('        camera.lookAt(center);')
    parts.append('        controls.target.copy(center);')
    parts.append('      }')
    parts.append('      function onWindowResize(){')
    parts.append('        const w = canvas.clientWidth || 640; const h = canvas.clientHeight || 480;')
    parts.append('        camera.aspect = w / h; camera.updateProjectionMatrix(); renderer.setSize(w, h, false);')
    parts.append('      }')
    parts.append('      window.addEventListener("resize", onWindowResize);')
    parts.append('      function animate(){ requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); }')
    parts.append('      onWindowResize(); fitCamera(); animate();')
    parts.append('      } catch (e) {')
    parts.append('        console.error("3D preview unavailable:", e);')
    parts.append('        showUnavailable("3D preview unavailable: your browser could not create a WebGL context (" + (e && e.message ? e.message : String(e)) + "). Try enabling hardware acceleration in your browser settings, or view this preview in a different browser.");')
    parts.append('      }')
    parts.append('    })();')
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


def _sample_arc_points(arc_obj: Arc, segments: int = 20) -> List[Tuple[float, float]]:
    # gerbonara Arc may provide a 'center' attribute (absolute coords) or cx/cy which can be offsets.
    center = getattr(arc_obj, 'center', None)
    if center is None:
        cx = getattr(arc_obj, 'cx', 0.0)
        cy = getattr(arc_obj, 'cy', 0.0)
    else:
        cx, cy = center

    radius = math.hypot(arc_obj.x1 - cx, arc_obj.y1 - cy)
    start_angle = math.atan2(arc_obj.y1 - cy, arc_obj.x1 - cx)
    end_angle = math.atan2(arc_obj.y2 - cy, arc_obj.x2 - cx)
    delta = end_angle - start_angle
    if arc_obj.clockwise:
        if delta >= 0:
            delta -= 2 * math.pi
    else:
        if delta <= 0:
            delta += 2 * math.pi
    num_steps = max(4, int(math.ceil(abs(delta) / (math.pi / 16))))
    points = []
    for step in range(num_steps + 1):
        angle = start_angle + delta * (step / num_steps)
        x = cx + math.cos(angle) * radius
        y = cy + math.sin(angle) * radius
        points.append((x, y))
    return points


def _build_edgecuts_outline(edgecuts_path: pathlib.Path) -> Optional[Tuple[List[Tuple[float, float]], Tuple[float, float, float, float]]]:
    try:
        raw_text = edgecuts_path.read_text(encoding='utf-8', errors='replace')
        edge_gerber = GerberFile.from_string(raw_text, enable_include_dir=None)
    except Exception:
        return None

    points: List[Tuple[float, float]] = []
    for idx, obj in enumerate(edge_gerber.objects):
        if idx == 0:
            points.append((obj.x1, obj.y1))
        if isinstance(obj, Line):
            points.append((obj.x2, obj.y2))
        elif isinstance(obj, Arc):
            arc_points = _sample_arc_points(obj)
            if arc_points:
                if points and abs(points[-1][0] - arc_points[0][0]) < 1e-6 and abs(points[-1][1] - arc_points[0][1]) < 1e-6:
                    arc_points = arc_points[1:]
                points.extend(arc_points)
        else:
            continue

    if not points:
        return None
    if abs(points[0][0] - points[-1][0]) > 1e-6 or abs(points[0][1] - points[-1][1]) > 1e-6:
        points.append(points[0])

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    return points, bbox


def render_3d_png_from_svg(svg_markup: str, out_path: pathlib.Path, fg_color: str, tint_color: str, pcb_w_mm: Optional[float], pcb_h_mm: Optional[float], corner_radius_mm: Optional[float], gerber_path: Optional[pathlib.Path] = None):
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
    # initial estimate of px/mm from the rendered SVG
    px_per_mm_est = img.width / svg_w_mm if svg_w_mm > 0 else img.width / 25.4

    # parse the SVG viewBox for coordinate mapping
    vb_match = re.search(r'viewBox="([\d\.\-\s]+)"', svg_markup)
    if vb_match:
        vb_vals = [float(v) for v in vb_match.group(1).split()]
        if len(vb_vals) == 4:
            vb_minx, vb_miny, vb_w, vb_h = vb_vals
        else:
            vb_minx, vb_miny, vb_w, vb_h = 0.0, 0.0, img.width, img.height
    else:
        vb_minx, vb_miny, vb_w, vb_h = 0.0, 0.0, img.width, img.height

    edgecuts_path = pathlib.Path('edgecuts.gbr')
    edge_outline = _build_edgecuts_outline(edgecuts_path) if edgecuts_path.exists() else None
    if edge_outline:
        edge_points, edge_bbox = edge_outline
        edge_minx, edge_miny, edge_maxx, edge_maxy = edge_bbox
        edge_board_w_mm = edge_maxx - edge_minx
        edge_board_h_mm = edge_maxy - edge_miny
    else:
        edge_minx = vb_minx
        edge_miny = vb_miny
        edge_board_w_mm = None
        edge_board_h_mm = None

    if pcb_w_mm is None:
        pcb_w_mm = edge_board_w_mm if edge_board_w_mm is not None else svg_w_mm + 20.0
    else:
        if edge_board_w_mm is not None:
            pcb_w_mm = max(pcb_w_mm, edge_board_w_mm)
    if pcb_h_mm is None:
        pcb_h_mm = edge_board_h_mm if edge_board_h_mm is not None else svg_h_mm + 20.0
    else:
        if edge_board_h_mm is not None:
            pcb_h_mm = max(pcb_h_mm, edge_board_h_mm)

    # choose a common origin for board coordinate mapping: use the minimum of the SVG viewBox origin
    # and the edge-cuts origin so both copper and edge polygons map into the same coordinate space.
    if edge_board_w_mm is not None:
        global_minx = min(vb_minx, edge_minx)
        global_miny = min(vb_miny, edge_miny)
    else:
        global_minx = vb_minx
        global_miny = vb_miny
    board_origin_x = global_minx
    board_origin_y = global_miny

    # derive a copper mask from the rendered SVG so we can size/position the board
    alpha = img.split()[3]
    gray = img.convert('L')
    copper_mask = gray.point(lambda p: 255 if p < 240 else 0)
    copper_mask = ImageChops.multiply(copper_mask, alpha)
    bbox = copper_mask.getbbox()

    # Auto-expand board to include copper extents if needed
    expand_margin_mm = 10.0
    rotated_for_fit = False
    if bbox:
        left, upper, right, lower = bbox
        copper_w_px = right - left
        copper_h_px = lower - upper
        copper_w_mm = copper_w_px / px_per_mm_est
        copper_h_mm = copper_h_px / px_per_mm_est

        # if copper doesn't fit but a 90° rotation would fit better, rotate the raster
        def fits(w_mm, h_mm, board_w, board_h):
            return (w_mm <= board_w - expand_margin_mm and h_mm <= board_h - expand_margin_mm)

        if not fits(copper_w_mm, copper_h_mm, pcb_w_mm, pcb_h_mm):
            # try rotated dimensions
            if fits(copper_h_mm, copper_w_mm, pcb_w_mm, pcb_h_mm):
                # rotate the rendered raster and associated masks by 90 degrees
                img = img.rotate(90, expand=True)
                copper_mask = copper_mask.rotate(90, expand=True)
                # copper_layer needs to be recreated to match new mask size below
                # signal rotation so viewBox width/height can be swapped later
                rotated_for_fit = True
                # recompute pixel/mm estimate after rotation
                px_per_mm_est = img.width / svg_w_mm if svg_w_mm > 0 else img.width / 25.4
                # recompute copper pixel dims after rotation
                left, upper, right, lower = copper_mask.getbbox()
                copper_w_px = right - left
                copper_h_px = lower - upper
                copper_w_mm = copper_w_px / px_per_mm_est
                copper_h_mm = copper_h_px / px_per_mm_est

        if copper_w_mm > pcb_w_mm - expand_margin_mm:
            pcb_w_mm = copper_w_mm + expand_margin_mm
        if copper_h_mm > pcb_h_mm - expand_margin_mm:
            pcb_h_mm = copper_h_mm + expand_margin_mm

    if corner_radius_mm is None:
        corner_radius_mm = 0.1 * max(pcb_w_mm, pcb_h_mm)

    px_per_mm = px_per_mm_est
    board_w_px = int(round(pcb_w_mm * px_per_mm))
    board_h_px = int(round(pcb_h_mm * px_per_mm))
    corner_px = int(round(corner_radius_mm * px_per_mm))

    # create a 3D board surface with thickness and edge shading
    tint_rgb = ImageColor.getrgb(tint_color) if tint_color else (255, 0, 0)
    board_color = tuple(max(0, min(255, int(c * 0.25))) for c in tint_rgb)
    side_color = tuple(max(0, c - 30) for c in board_color)

    from PIL import ImageDraw

    mask = None
    edgecuts_path = pathlib.Path('edgecuts.gbr')
    if edgecuts_path.exists():
        edge_outline = _build_edgecuts_outline(edgecuts_path)
        if edge_outline:
            edge_points, edge_bbox = edge_outline
            mask = Image.new('L', (board_w_px, board_h_px), 0)
            md = ImageDraw.Draw(mask)
            board_points = [
                (
                    int(round((x - board_origin_x) * px_per_mm)),
                    int(round((y - board_origin_y) * px_per_mm)),
                )
                for x, y in edge_points
            ]
            md.polygon(board_points, fill=255)
    if mask is None:
        mask = Image.new('L', (board_w_px, board_h_px), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([0, 0, board_w_px, board_h_px], radius=corner_px, fill=255)

    board_top = Image.new('RGBA', (board_w_px, board_h_px), board_color + (255,))
    board_top.putalpha(mask)

    # thickness: 1.6mm (user requested)
    thickness_px = max(1, int(round(px_per_mm * 1.6)))

    # create side plane with rounded ends matching board rounding — will be omitted from final composite per user request
    side_plane = Image.new('RGBA', (board_w_px, thickness_px), (0, 0, 0, 0))
    sd = ImageDraw.Draw(side_plane)
    sd.rounded_rectangle([0, 0, board_w_px, thickness_px], radius=max(1, corner_px // 2), fill=side_color + (255,))
    # side plane alpha (kept for potential use) but we will not paste the side or shadow into the final image
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

    # build copper layer — prefer a vector-based rendering from the Gerber when available
    fg_rgb = ImageColor.getrgb(fg_color) if fg_color else (184, 115, 51)
    copper_layer = None
    vector_copper_used = False
    if gerber_path is not None and pathlib.Path(gerber_path).exists():
        try:
            gtext = pathlib.Path(gerber_path).read_text(encoding='utf-8', errors='replace')
            gfile = GerberFile.from_string(gtext, enable_include_dir=None)
            # draw vector copper directly in board pixel coordinates
            copper_img = Image.new('RGBA', (board_w_px, board_h_px), (0, 0, 0, 0))
            from PIL import ImageDraw
            cdraw = ImageDraw.Draw(copper_img)
            from gerbonara.graphic_objects import Flash, Line, Arc
            for obj in gfile.objects:
                # Flash objects -> pads
                if isinstance(obj, Flash):
                    aper = getattr(obj, 'aperture', None)
                    x_mm = getattr(obj, 'x', None)
                    y_mm = getattr(obj, 'y', None)
                    # some Flash objects use center
                    if x_mm is None or y_mm is None:
                        center = getattr(obj, 'center', None)
                        if center:
                            x_mm, y_mm = center
                    if x_mm is None or y_mm is None:
                        continue
                    x_px = int(round((x_mm - board_origin_x) * px_per_mm))
                    y_px = int(round((y_mm - board_origin_y) * px_per_mm))
                    if isinstance(aper, CircleAperture):
                        d_mm = getattr(aper, 'diameter', None)
                        if d_mm:
                            r_px = max(1, int(round((d_mm / 2.0) * px_per_mm)))
                            bbox_xy = [x_px - r_px, y_px - r_px, x_px + r_px, y_px + r_px]
                            cdraw.ellipse(bbox_xy, fill=fg_rgb + (255,))
                    elif isinstance(aper, RectangleAperture):
                        w_mm = getattr(aper, 'w', None) or getattr(aper, 'width', None)
                        h_mm = getattr(aper, 'h', None) or getattr(aper, 'height', None)
                        if w_mm and h_mm:
                            hw = int(round((w_mm / 2.0) * px_per_mm))
                            hh = int(round((h_mm / 2.0) * px_per_mm))
                            bbox_xy = [x_px - hw, y_px - hh, x_px + hw, y_px + hh]
                            cdraw.rectangle(bbox_xy, fill=fg_rgb + (255,))
                    elif isinstance(aper, ObroundAperture):
                        w_mm = getattr(aper, 'w', None)
                        h_mm = getattr(aper, 'h', None)
                        if w_mm and h_mm:
                            hw = int(round((w_mm / 2.0) * px_per_mm))
                            hh = int(round((h_mm / 2.0) * px_per_mm))
                            radius_px = max(1, min(hw, hh))
                            cdraw.rounded_rectangle([x_px - hw, y_px - hh, x_px + hw, y_px + hh], radius=radius_px, fill=fg_rgb + (255,))
                # Draw traces (lines and arcs) as stroked paths using aperture diameter
                if isinstance(obj, Line):
                    x1_px = int(round((obj.x1 - board_origin_x) * px_per_mm))
                    y1_px = int(round((obj.y1 - board_origin_y) * px_per_mm))
                    x2_px = int(round((obj.x2 - board_origin_x) * px_per_mm))
                    y2_px = int(round((obj.y2 - board_origin_y) * px_per_mm))
                    ap = getattr(obj, 'aperture', None)
                    # aperture.diameter is in mm; default stroke width is 0.2 mm
                    stroke_mm = getattr(ap, 'diameter', None) if ap is not None else 0.2
                    stroke_px = max(1, int(round(stroke_mm * px_per_mm)))
                    cdraw.line([x1_px, y1_px, x2_px, y2_px], fill=fg_rgb + (255,), width=stroke_px)
                if isinstance(obj, Arc):
                    pts = _sample_arc_points(obj)
                    if pts and len(pts) > 1:
                        px_pts = [(int(round((x - board_origin_x) * px_per_mm)), int(round((y - board_origin_y) * px_per_mm))) for x, y in pts]
                        ap = getattr(obj, 'aperture', None)
                        stroke_mm = getattr(ap, 'diameter', None) if ap is not None else 0.2
                        stroke_px = max(1, int(round(stroke_mm * px_per_mm)))
                        cdraw.line(px_pts, fill=fg_rgb + (255,), width=stroke_px)
            # use raw copper_img as the copper layer (no glow)
            copper_layer = copper_img
            vector_copper_used = True
        except Exception:
            copper_layer = None
    # fallback: raster-driven copper (legacy path)
    if copper_layer is None:
        # if we rotated earlier, re-create copper_fill/glow to match img/copper_mask sizes
        copper_fill = Image.new('RGBA', img.size, fg_rgb + (255,))
        copper_fill.putalpha(copper_mask)
        # small dilation to help preserve thin pads/traces after transformations
        dilation_px = max(1, int(round(px_per_mm * 0.05)))
        try:
            copper_mask = copper_mask.filter(ImageFilter.MaxFilter(3)) if dilation_px >= 1 else copper_mask
        except Exception:
            pass
        # no glow: use a crisp copper fill from the raster mask
        copper_layer = copper_fill
    # if we rotated the raster to fit, swap the viewBox width/height so unit mapping remains correct
    if 'rotated_for_fit' in locals() and rotated_for_fit:
        vb_w, vb_h = vb_h, vb_w

    if vector_copper_used and copper_layer is not None:
        # copper_layer is already rendered in board pixel coordinates (board_w_px x board_h_px)
        composite_top = board_top.copy()
        composite_top.paste(copper_layer, (0, 0), copper_layer)
    else:
        # crop copper to its bbox in pixel space to avoid processing empty areas
        bbox = copper_mask.getbbox()
        if bbox:
            left_px, upper_px, right_px, lower_px = bbox
            copper_crop = copper_layer.crop(bbox)
            copper_w_px = right_px - left_px
            copper_h_px = lower_px - upper_px

            # map pixel bbox to viewBox units (usually mm when gerbonara produced SVG uses mm)
            copper_left_mm = vb_minx + left_px * (vb_w / img.width)
            copper_top_mm = vb_miny + upper_px * (vb_h / img.height)
            copper_w_vb = copper_w_px * (vb_w / img.width)
            copper_h_vb = copper_h_px * (vb_h / img.height)

            # compute target size in board pixels so copper keeps its physical size
            target_w_px = max(1, int(round(copper_w_vb * px_per_mm)))
            target_h_px = max(1, int(round(copper_h_vb * px_per_mm)))
            copper_resized = copper_crop.resize((target_w_px, target_h_px), resample=Image.LANCZOS)

            # compute offset so copper top-left maps to the board origin offset
            paste_x_local = int(round((copper_left_mm - board_origin_x) * px_per_mm))
            paste_y_local = int(round((copper_top_mm - board_origin_y) * px_per_mm))
        else:
            # fallback: scale full copper layer to fit the board
            scale = min(board_w_px / max(1, img.width), board_h_px / max(1, img.height))
            target_w = max(1, int(round(img.width * scale)))
            target_h = max(1, int(round(img.height * scale)))
            copper_resized = copper_layer.resize((target_w, target_h), resample=Image.LANCZOS)
            paste_x_local = (board_w_px - target_w) // 2
            paste_y_local = (board_h_px - target_h) // 2

        # ensure copper fits within board; if not, scale down proportionally
        paste_x_local = max(0, paste_x_local)
        paste_y_local = max(0, paste_y_local)
        available_w = board_w_px - paste_x_local
        available_h = board_h_px - paste_y_local
        if copper_resized.width > available_w or copper_resized.height > available_h:
            scale_w = available_w / copper_resized.width if copper_resized.width > 0 else 1.0
            scale_h = available_h / copper_resized.height if copper_resized.height > 0 else 1.0
            scale = min(scale_w, scale_h, 1.0)
            if scale < 1.0:
                new_w = max(1, int(round(copper_resized.width * scale)))
                new_h = max(1, int(round(copper_resized.height * scale)))
                copper_resized = copper_resized.resize((new_w, new_h), resample=Image.LANCZOS)

        composite_top = board_top.copy()
        composite_top.paste(copper_resized, (paste_x_local, paste_y_local), copper_resized)

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
    # paste only the board top (no side plane or shadow) so traces lie on the same plane
    composed.paste(board_top_tilted, (margin_px, margin_px), board_top_tilted)

    # add a light coating over the board for a soft conformal finish
    coating_color = tuple(max(0, min(255, int(c * 0.4))) for c in tint_rgb)
    coating = Image.new('RGBA', (canvas_w_px, canvas_h_px), coating_color + (100,))
    coating = coating.filter(ImageFilter.GaussianBlur(radius=px_per_mm * 0.4))
    composed = Image.alpha_composite(composed, coating)

    # build a final mask from the transformed board top and side by transforming
    # the original edge polygon (vector) through the same perspective mapping.
    # This creates a precise silhouette mask that preserves rounded corners and
    # avoids raster distortions from transforming an existing raster mask.
    def map_perspective_point(coeffs, x, y):
        # coeffs are (a,b,c,d,e,f,g,h)
        a, b, c, d, e, f, g, h = coeffs
        denom = (g * x + h * y + 1.0)
        if denom == 0:
            return (0.0, 0.0)
        x_dst = (a * x + b * y + c) / denom
        y_dst = (d * x + e * y + f) / denom
        return (x_dst, y_dst)

    vector_mask = Image.new('L', (canvas_w_px, canvas_h_px), 0)
    vd = ImageDraw.Draw(vector_mask)

    # transform board polygon (edge_points) through board_coeffs
    if 'edge_points' in locals() and board_coeffs:
        transformed_pts = []
        for x_mm, y_mm in edge_points:
            # convert mm to source px coordinates
            x_src = (x_mm - board_origin_x) * px_per_mm
            y_src = (y_mm - board_origin_y) * px_per_mm
            x_t, y_t = map_perspective_point(board_coeffs, x_src, y_src)
            # offset by margin_px for final canvas placement
            transformed_pts.append((int(round(x_t + margin_px)), int(round(y_t + margin_px))))
        if transformed_pts:
            vd.polygon(transformed_pts, fill=255)

    # transform side rectangle through side_coeffs and add to mask
    if side_coeffs:
        # source side rect corners
        src_side = [(0, 0), (board_w_px, 0), (board_w_px, thickness_px), (0, thickness_px)]
        transformed_side = []
        for (x_src, y_src) in src_side:
            x_t, y_t = map_perspective_point(side_coeffs, x_src, y_src)
            transformed_side.append((int(round(x_t + margin_px)), int(round(y_t + margin_px))))
        # nudge side mask slightly downward to avoid a visible seam with the top surface
        offset_y = max(1, int(round(px_per_mm * 0.03)))
        if offset_y:
            transformed_side = [(x, y + offset_y) for (x, y) in transformed_side]
        vd.polygon(transformed_side, fill=255)
    # final mask is vector_mask
    final = Image.new('RGBA', (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
    final.paste(composed, (0, 0), vector_mask)

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
            render_3d_png_from_svg(rew_svg, out3, args.fg_color or '#b87333', args.tint_color or '#ff0000', args.pcb_width_mm, args.pcb_height_mm, args.corner_radius_mm, gerber_path=output_path)
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
