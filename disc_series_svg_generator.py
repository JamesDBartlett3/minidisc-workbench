#!/usr/bin/env python3
"""
Disc-series SVG icon generator (CLI)
------------------------------------
Creates a set of SVG icons showing N overlapping discs where:
  • The "current" disc (i) is a plain, solid circle with text only.
  • All other discs are stylized CDs (opaque base, rim, hub ring,
    transparent center hole, radial highlights in two quadrants).
  • Z-order (non-current) uses the rule:
        z(j) = j                    if j < i
        z(j) = (j - i) * -1         if j > i
    Discs are rendered in ascending z(j) (bottom → top), then the current disc is drawn last.
  • Adjacent discs overlap by a specified fraction of one disc’s area.

Outputs: <prefix>_<i>_of_<N>.svg for i = 1..N, and a ZIP bundle.
"""

from __future__ import annotations
import argparse
import math
from pathlib import Path
import re
import zipfile
from typing import List, Tuple

# --------------------------- Defaults ---------------------------

DEFAULTS = dict(
    total_discs      = 6,       # N >= 2
    overlap_fraction = 0.75,    # 0..1 or "NN%" via CLI
    radius_px        = 320,
    padding_px       = 80,
    prefix           = "disc_cd_param",

    # Non-current (stylized CD)
    cd_fill          = "#F0F0F0",
    cd_outline       = "#323232",
    cd_outline_w     = 16,
    hub_ratio        = 0.22,    # hub ring outer radius / disc radius
    hub_stroke       = 18,
    hole_ratio       = 0.12,    # center hole diameter / disc diameter

    # Highlights: two opposite quadrants, each drawn at two radii
    hl_color         = "rgba(255,255,255,0.28)",
    hl_outer_w       = 36,
    hl_inner_w       = 28,
    highlight_arcs   = [(110, 160), (290, 340)],  # UL and LR

    # Current (top) disc = solid only
    current_fill     = "#000000",

    # Text
    text_fill        = "#FFFFFF",
    font_family      = "DejaVu Sans, Segoe UI, Arial, sans-serif",
    font_weight      = "700",
    font_size        = 120,
    label_mode       = "two-line",  # "two-line" | "fraction"
)

# --------------------------- Geometry ---------------------------

def overlap_area_equal(r: float, d: float) -> float:
    """Overlap area of two equal circles (radius r) with center spacing d."""
    if d >= 2*r: return 0.0
    if d <= 0:   return math.pi*r*r
    return 2*r*r*math.acos(d/(2*r)) - 0.5*d*math.sqrt(max(0.0, 4*r*r - d*d))

def solve_spacing_for_overlap(r: float, frac: float) -> float:
    """
    Find center spacing d such that the overlap area of two equal circles (radius r)
    equals 'frac' times the area of one circle.
    """
    frac = max(0.0, min(1.0, frac))
    target = frac * math.pi * r * r
    lo, hi = 0.0, 2*r
    for _ in range(80):  # binary search
        mid = (lo + hi) / 2
        if overlap_area_equal(r, mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

# --------------------------- SVG helpers ---------------------------

def circle_path(cx: float, cy: float, R: float) -> str:
    """Full circle via two arcs (SVG path)."""
    x1, y1 = cx + R, cy
    x2, y2 = cx - R, cy
    return (
        f"M {x1:.3f},{y1:.3f} "
        f"A {R:.3f},{R:.3f} 0 1 0 {x2:.3f},{y2:.3f} "
        f"A {R:.3f},{R:.3f} 0 1 0 {x1:.3f},{y1:.3f} Z"
    )

def donut_path(cx: float, cy: float, R_outer: float, R_inner: float) -> str:
    """Even-odd filled ring: outer circle + inner circle."""
    return circle_path(cx, cy, R_outer) + " " + circle_path(cx, cy, R_inner)

def arc_path(cx: float, cy: float, R: float, start_deg: float, end_deg: float) -> str:
    """CCW arc path from start to end angles (degrees)."""
    start = math.radians(start_deg)
    end   = math.radians(end_deg)
    x0, y0 = cx + R*math.cos(start), cy + R*math.sin(start)
    x1, y1 = cx + R*math.cos(end),   cy + R*math.sin(end)
    delta = (end_deg - start_deg) % 360
    large_arc = 1 if delta > 180 else 0
    sweep = 1
    return f"M {x0:.3f},{y0:.3f} A {R:.3f},{R:.3f} 0 {large_arc} {sweep} {x1:.3f},{y1:.3f}"

def svg_header(W: int, H: int, bg: str = "none") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="background:{bg}">\n'
    )

def draw_stylized_cd(cx: float, cy: float, r: float, S: dict) -> str:
    """Opaque stylized CD with rim, hub ring, UL/LR highlights, and transparent center hole."""
    parts = []
    path_ring = donut_path(cx, cy, r, r * S["hole_ratio"])
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
            f'<path d="{arc_path(cx, cy, r*0.86, a0, a1)}" '
            f'stroke="{S["hl_color"]}" stroke-width="{S["hl_outer_w"]}" '
            f'fill="none" stroke-linecap="round" />'
        )
        parts.append(
            f'<path d="{arc_path(cx, cy, r*0.70, a0, a1)}" '
            f'stroke="{S["hl_color"]}" stroke-width="{S["hl_inner_w"]}" '
            f'fill="none" stroke-linecap="round" />'
        )
    return "\n".join(parts)

def draw_current_disc(cx: float, cy: float, r: float, S: dict) -> str:
    return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" fill="{S["current_fill"]}" />'

def draw_label(cx: float, cy: float, i: int, n: int, S: dict) -> str:
    if S.get("label_mode", "two-line") == "fraction":
        return (
            f'<text x="{cx:.3f}" y="{cy:.3f}" text-anchor="middle" '
            f'font-family="{S["font_family"]}" font-weight="{S["font_weight"]}" '
            f'font-size="{S["font_size"]}" fill="{S["text_fill"]}">'
            f'{i}/{n}</text>'
        )
    return (
        f'<text x="{cx:.3f}" y="{cy:.3f}" text-anchor="middle" '
        f'font-family="{S["font_family"]}" font-weight="{S["font_weight"]}" '
        f'font-size="{S["font_size"]}" fill="{S["text_fill"]}">\n'
        f'  <tspan x="{cx:.3f}" dy="-0.25em">Disc {i}</tspan>\n'
        f'  <tspan x="{cx:.3f}" dy="1.3em">of {n}</tspan>\n'
        f'</text>'
    )

# --------------------------- CLI parsing ---------------------------

import re
PERCENT_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*%?\s*$")

def parse_fraction_or_percent(s: str) -> float:
    """
    Accepts '0.75' or '75%' (or '75').
    Returns a float in [0,1].
    """
    m = PERCENT_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(f"Invalid fraction/percent: {s}")
    val = float(m.group(1))
    if val > 1.0:
        val = val / 100.0
    if not (0.0 <= val <= 1.0):
        raise argparse.ArgumentTypeError("Overlap must be in [0,1] or 0–100%.")
    return val

def parse_highlight_arcs(s: str):
    """
    Parse --highlight-arcs like: "110,160;290,340"
    Returns list of (start_deg, end_deg) tuples.
    """
    arcs = []
    for chunk in s.split(";"):
        if not chunk.strip():
            continue
        parts = chunk.split(",")
        if len(parts) != 2:
            raise argparse.ArgumentTypeError("Highlight arcs must be 'a0,a1; b0,b1; ...'")
        a0, a1 = float(parts[0]), float(parts[1])
        arcs.append((a0, a1))
    return arcs

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate SVG disc-series icons.")
    d = DEFAULTS

    p.add_argument("--total-discs", type=int, default=d["total_discs"])
    p.add_argument("--overlap", type=parse_fraction_or_percent, default=d["overlap_fraction"],
                   help="Neighbor overlap as fraction (0..1) or percent (e.g., 75%%).")
    p.add_argument("--radius", type=float, default=d["radius_px"])
    p.add_argument("--padding", type=int, default=d["padding_px"])
    p.add_argument("--prefix", default=d["prefix"])

    # Colors & strokes
    p.add_argument("--cd-fill", default=d["cd_fill"])
    p.add_argument("--cd-outline", default=d["cd_outline"])
    p.add_argument("--cd-outline-w", type=float, default=d["cd_outline_w"])
    p.add_argument("--hub-ratio", type=float, default=d["hub_ratio"])
    p.add_argument("--hub-stroke", type=float, default=d["hub_stroke"])
    p.add_argument("--hole-ratio", type=float, default=d["hole_ratio"])

    p.add_argument("--hl-color", default=d["hl_color"])
    p.add_argument("--hl-outer-w", type=float, default=d["hl_outer_w"])
    p.add_argument("--hl-inner-w", type=float, default=d["hl_inner_w"])
    p.add_argument("--highlight-arcs", type=parse_highlight_arcs, default=d["highlight_arcs"],
                   help='Quadrant arcs like "110,160;290,340"')

    p.add_argument("--current-fill", default=d["current_fill"])

    # Text
    p.add_argument("--text-fill", default=d["text_fill"])
    p.add_argument("--font-family", default=d["font_family"])
    p.add_argument("--font-weight", default=d["font_weight"])
    p.add_argument("--font-size", type=float, default=d["font_size"])
    p.add_argument("--label-mode", choices=["two-line", "fraction"], default=d["label_mode"])

    return p

# --------------------------- Main render ---------------------------

def main():
    ap = build_arg_parser()
    args = ap.parse_args()

    n = args.total_discs
    if n < 2:
        raise SystemExit("ERROR: --total-discs must be >= 2.")

    r = float(args.radius)
    pad = int(args.padding)
    overlap_fraction = float(args.overlap)
    spacing = solve_spacing_for_overlap(r, overlap_fraction)

    # Compose style dict from args
    S = {
        "cd_fill":      args.cd_fill,
        "cd_outline":   args.cd_outline,
        "cd_outline_w": float(args.cd_outline_w),
        "hub_ratio":    float(args.hub_ratio),
        "hub_stroke":   float(args.hub_stroke),
        "hole_ratio":   float(args.hole_ratio),
        "hl_color":     args.hl_color,
        "hl_outer_w":   float(args.hl_outer_w),
        "hl_inner_w":   float(args.hl_inner_w),
        "highlight_arcs": args.highlight_arcs,
        "current_fill": args.current_fill,
        "text_fill":    args.text_fill,
        "font_family":  args.font_family,
        "font_weight":  str(args.font_weight),
        "font_size":    float(args.font_size),
        "label_mode":   args.label_mode,
    }

    # Canvas and centers
    required_width = (n - 1) * spacing + 2 * r
    W = int(required_width + 2 * pad)
    H = int(2 * r + 2 * pad)
    cx0 = pad + r
    cy  = pad + r
    centers = [(cx0 + k * spacing, cy) for k in range(n)]

    outputs = []
    for i in range(1, n + 1):
        svg = [svg_header(W, H, "none")]

        # ---- z-order for non-current discs (bottom → top) ----
        def z_weight(j: int) -> float:
            if j < i:
                return j
            elif j > i:
                return (j - i) * -1
            else:
                return float('inf')  # current handled separately

        order = sorted([j for j in range(1, n + 1) if j != i], key=z_weight)

        # Draw non-current discs in ascending z
        for j in order:
            cx, cy = centers[j - 1]
            svg.append(f'<g id="disc{j}_stylized">')
            svg.append(draw_stylized_cd(cx, cy, r, S))
            svg.append('</g>')

        # Draw current disc last (top)
        cx, cy = centers[i - 1]
        svg.append(f'<g id="disc{i}_current_top">')
        svg.append(draw_current_disc(cx, cy, r, S))
        svg.append(draw_label(cx, cy, i, n, S))
        svg.append('</g>')

        svg.append('</svg>')

        fname = f"{args.prefix}_{i}_of_{n}.svg"
        Path(fname).write_text("\n".join(svg), encoding="utf-8")
        outputs.append(fname)

    # Bundle ZIP
    zip_name = f"{args.prefix}_set_{n}_discs.zip"
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in outputs:
            zf.write(f)

    print("Created:", outputs + [zip_name])

if __name__ == "__main__":
    main()