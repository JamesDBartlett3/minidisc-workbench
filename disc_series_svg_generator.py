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
