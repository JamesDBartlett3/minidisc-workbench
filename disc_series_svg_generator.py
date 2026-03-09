"""
Disc-series SVG icon generator
------------------------------
Creates a set of SVG icons showing N overlapping discs where:
  • The "current" disc is a plain, solid circle with text only (no hub, hole, or highlights).
  • All other discs are stylized CDs (opaque base, rim, hub ring, transparent center hole,
    and radial highlights in UL & LR quadrants).
  • Z-order: non-current discs are stacked in index order (1 bottom -> ... -> N), then
    the current disc is drawn last (on top).
  • Adjacent discs overlap by a target fraction of *one disc’s area* (e.g., 0.75 = 75%).
Outputs: disc_<i>_of_<N>.svg and a zip bundle.

Requires: Python 3.x (no external network), writes plain SVG files.
"""

from __future__ import annotations
import math
from pathlib import Path
import zipfile

# ===================== CONFIGURABLE PARAMETERS =====================

TOTAL_DISCS      = 6          # <-- Set total N here (>= 2)
OVERLAP_FRACTION = 0.75       # <-- Neighbor overlap as fraction of *one disc’s area* (0..1)
RADIUS_PX        = 320        # <-- Disc radius in pixels
CANVAS_PADDING   = 80         # <-- Margin around the disc stack (px)

# Colors & styling (hex or CSS rgba()).
STYLE = {
    # Non-current (stylized CD)
    "cd_fill":        "#F0F0F0",
    "cd_outline":     "#323232",
    "cd_outline_w":   16,          # stroke width (px)
    "hub_ratio":      0.22,        # hub ring outer radius / disc radius
    "hub_stroke":     18,          # hub ring stroke (px)
    "hole_ratio":     0.12,        # center hole diameter / disc diameter

    # Radial highlights (two arcs in opposite quadrants)
    "hl_color":       "rgba(255,255,255,0.28)",
    "hl_outer_w":     36,
    "hl_inner_w":     28,
    # Angles (degrees, SVG: 0° at +x, counterclockwise positive)
    "highlight_arcs": [(110, 160), (290, 340)],   # UL and LR

    # Current (top) disc = solid only (no hub/hole/highlights)
    "current_fill":   "#000000",

    # Text
    "text_fill":      "#FFFFFF",
    "font_family":    "DejaVu Sans, Segoe UI, Arial, sans-serif",
    "font_weight":    "700",
    "font_size":      120,         # px
    # Choose 2-line label ("Disc i" / "of N") or 1-line "i/N"
    "label_mode":     "two-line",  # "two-line" | "fraction"
}

# Optional: output naming prefix
NAME_PREFIX = "disc_cd_param"

# ==================================================================

def _overlap_area_equal(r: float, d: float) -> float:
    """Overlap area of two equal circles (radius r) with center spacing d."""
    if d >= 2*r: return 0.0
    if d <= 0:   return math.pi*r*r
    return 2*r*r*math.acos(d/(2*r)) - 0.5*d*math.sqrt(max(0.0, 4*r*r - d*d))

def _solve_spacing_for_overlap(r: float, frac: float) -> float:
    """
    Find center spacing d such that the overlap area of two equal circles (radius r)
    equals 'frac' times the area of one circle.
    """
    frac = max(0.0, min(1.0, frac))
    A_target = frac * math.pi * r * r
    lo, hi = 0.0, 2*r
    for _ in range(80):  # binary search
        mid = (lo+hi)/2
        if _overlap_area_equal(r, mid) > A_target:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2

def _circle_path(cx: float, cy: float, R: float) -> str:
    """Full circle via two arcs (SVG path)."""
    x1, y1 = cx + R, cy
    x2, y2 = cx - R, cy
    return (
        f"M {x1:.3f},{y1:.3f} "
        f"A {R:.3f},{R:.3f} 0 1 0 {x2:.3f},{y2:.3f} "
        f"A {R:.3f},{R:.3f} 0 1 0 {x1:.3f},{y1:.3f} Z"
    )

def _donut_path(cx: float, cy: float, R_outer: float, R_inner: float) -> str:
    """Even-odd filled ring: outer circle + inner circle."""
    return _circle_path(cx, cy, R_outer) + " " + _circle_path(cx, cy, R_inner)

def _arc_path(cx: float, cy: float, R: float, start_deg: float, end_deg: float) -> str:
    """CCW arc path from start to end angles (degrees)."""
    start = math.radians(start_deg)
    end   = math.radians(end_deg)
    x0, y0 = cx + R*math.cos(start), cy + R*math.sin(start)
    x1, y1 = cx + R*math.cos(end),   cy + R*math.sin(end)
    delta = (end_deg - start_deg) % 360
    large_arc = 1 if delta > 180 else 0
    sweep = 1
    return f"M {x0:.3f},{y0:.3f} A {R:.3f},{R:.3f} 0 {large_arc} {sweep} {x1:.3f},{y1:.3f}"

def _svg_header(W: int, H: int, bg: str = "none") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="background:{bg}">\n'
    )

def _draw_stylized_cd(cx: float, cy: float, r: float, S: dict) -> str:
    """Opaque stylized CD with rim, hub ring, UL/LR highlights, and transparent center hole."""
    parts = []
    path_ring = _donut_path(cx, cy, r, r * S["hole_ratio"])
    parts.append(
        f'<path d="{path_ring}" fill="{S["cd_fill"]}" fill-rule="evenodd" '
        f'stroke="{S["cd_outline"]}" stroke-width="{S["cd_outline_w"]}" />'
    )
    parts.append(
        f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r*S["hub_ratio"]:.3f}" '
        f'fill="none" stroke="{S["cd_outline"]}" stroke-width="{S["hub_stroke"]}" />'
    )
    for (a0, a1) in S["highlight_arcs"]:
        parts.append(
            f'<path d="{_arc_path(cx, cy, r*0.86, a0, a1)}" '
            f'stroke="{S["hl_color"]}" stroke-width="{S["hl_outer_w"]}" '
            f'fill="none" stroke-linecap="round" />'
        )
        parts.append(
            f'<path d="{_arc_path(cx, cy, r*0.70, a0, a1)}" '
            f'stroke="{S["hl_color"]}" stroke-width="{S["hl_inner_w"]}" '
            f'fill="none" stroke-linecap="round" />'
        )
    return "\n".join(parts)

def _draw_current_disc(cx: float, cy: float, r: float, S: dict) -> str:
    return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" fill="{S["current_fill"]}" />'

def _draw_label(cx: float, cy: float, i: int, n: int, S: dict) -> str:
    if S.get("label_mode", "two-line") == "fraction":
        return (
            f'<text x="{cx:.3f}" y="{cy:.3f}" text-anchor="middle" '
            f'font-family="{S["font_family"]}" font-weight="{S["font_weight"]}" '
            f'font-size="{S["font_size"]}" fill="{S["text_fill"]}">'
            f'{i}/{n}</text>'
        )
    # default: two-line ("Disc i" / "of n")
    return (
        f'<text x="{cx:.3f}" y="{cy:.3f}" text-anchor="middle" '
        f'font-family="{S["font_family"]}" font-weight="{S["font_weight"]}" '
        f'font-size="{S["font_size"]}" fill="{S["text_fill"]}">\n'
        f'  <tspan x="{cx:.3f}" dy="-0.25em">Disc {i}</tspan>\n'
        f'  <tspan x="{cx:.3f}" dy="1.3em">of {n}</tspan>\n'
        f'</text>'
    )

def _build_svg_for_index(i: int, n: int, r: float, spacing: float, pad: int, S: dict) -> str:
    """
    Build the SVG for "Disc i of n".
    - Non-current discs 1..n (except i) are drawn in index order (1 -> n) so earlier indices are below.
    - Current disc i is drawn last (top).
    """
    required_width = (n - 1) * spacing + 2 * r
    W = int(required_width + 2 * pad)
    H = int(2 * r + 2 * pad)
    cx0 = pad + r
    cy  = pad + r
    centers = [(cx0 + k * spacing, cy) for k in range(n)]

    parts = [_svg_header(W, H, "none")]
    # draw non-current stylized CDs in index order
    for idx in range(1, n + 1):
        if idx == i:
            continue
        cx, cy = centers[idx - 1]
        parts.append(f'<g id="disc{idx}_stylized">')
        parts.append(_draw_stylized_cd(cx, cy, r, S))
        parts.append('</g>')
    # draw current plain disc on top
    cx, cy = centers[i - 1]
    parts.append(f'<g id="disc{i}_current_top">')
    parts.append(_draw_current_disc(cx, cy, r, S))
    parts.append(_draw_label(cx, cy, i, n, S))
    parts.append('</g>')
    parts.append('</svg>')
    return "\n".join(parts), W, H

def generate_all(
    n: int = TOTAL_DISCS,
    overlap_fraction: float = OVERLAP_FRACTION,
    radius_px: float = RADIUS_PX,
    pad_px: int = CANVAS_PADDING,
    style: dict = STYLE,
    prefix: str = NAME_PREFIX,
) -> list[str]:
    """Generate SVGs for i=1..n and return file paths."""
    assert n >= 2, "TOTAL_DISCS must be >= 2"
    r = float(radius_px)
    spacing = _solve_spacing_for_overlap(r, float(overlap_fraction))
    outputs = []
    for i in range(1, n + 1):
        svg_text, W, H = _build_svg_for_index(i, n, r, spacing, int(pad_px), style)
        fname = f"{prefix}_{i}_of_{n}.svg"
        Path(fname).write_text(svg_text, encoding="utf-8")
        outputs.append(fname)
    # bundle as a zip
    zip_name = f"{prefix}_set_{n}_discs.zip"
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in outputs:
            zf.write(f)
    outputs.append(zip_name)
    return outputs

if __name__ == "__main__":
    files = generate_all()
    print("Created:", files)
