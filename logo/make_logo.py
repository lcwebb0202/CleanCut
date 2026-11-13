"""Builds every CleanCut logo from one drawing: the C, drawn as a dashed cut line with scissors cutting
along it.

    python logo/make_logo.py

writes the icon and full-name logos in this folder (SVG; turn them into PNG with any SVG converter,
e.g. `rsvg-convert -w 512 -h 512 cleancut-icon.svg -o cleancut-icon.png`), the browser-tab icon
(cleancut/static/favicon.svg) and the C in the page header (cleancut/static/index.html, between the
"logo:start" and "logo:end" comments). The name's letters are turned into shapes so the full logo looks
the same on any computer; that needs Windows' Segoe UI Bold font (the page's font on Windows).
"""
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "cleancut", "static")
WHITE, DARK = "#f2f2f2", "#1d2026"

# ---------------------------------------------------------------- the C, in a 200 x 200 box

STROKE = 15                      # width of the dashed line
OUT_R, IN_R = 70, 42             # the two curves, centered on (100, 100)
END_DEG = 40                     # the C's opening: the curves stop 40 degrees above and below the right
DASH_RATIO = 27 / 7              # dash length : gap
SCISSORS = "translate(116,8) rotate(124) scale(2.3)"


def _pt(r: float, deg: float) -> tuple[float, float]:
    return 100 + r * math.cos(math.radians(deg)), 100 + r * math.sin(math.radians(deg))


def _arc_dashes(r: float, n: int) -> str:
    """n evenly spaced dashes along a curve, with a full gap before the end pieces."""
    length = r * math.radians(360 - 2 * END_DEG)
    half = STROKE / 2
    g = (length - 2 * half) / (n * DASH_RATIO + n + 1)
    d = g * DASH_RATIO
    parts = [0, half + g] + [d, g] * (n - 1) + [d, half + g]
    return " ".join(f"{v:.3f}" for v in parts)


def c_drawing(color: str, uid: str) -> str:
    """The C: dashes along the outer and inner curves, at each end a straight bar across the ring cut
    in two, and the scissors. The scissor handles are see-through (a mask cuts them out of the line),
    and a slight blur + sharp edge rounds the dashes' corners while keeping the exact color."""
    common = f'fill="none" stroke="{color}" stroke-width="{STROKE}" stroke-linejoin="round" stroke-linecap="butt"'
    (ox0, oy0), (ox1, oy1) = _pt(OUT_R, -END_DEG), _pt(OUT_R, END_DEG)
    (ix0, iy0), (ix1, iy1) = _pt(IN_R, END_DEG), _pt(IN_R, -END_DEG)
    outer_edge, inner_edge = OUT_R + STROKE / 2, IN_R - STROKE / 2
    gap = 7.0  # the break in the middle of each end bar
    piece = (outer_edge - inner_edge - gap) / 2
    ends = " ".join(f"M{_pt(outer_edge, a)[0]:.2f},{_pt(outer_edge, a)[1]:.2f} "
                    f"L{_pt(inner_edge, a)[0]:.2f},{_pt(inner_edge, a)[1]:.2f}" for a in (END_DEG, -END_DEG))
    return f'''<filter id="{uid}-soft" x="-20%" y="-20%" width="140%" height="140%" color-interpolation-filters="sRGB">
    <feGaussianBlur in="SourceAlpha" stdDeviation="2.2" result="blur"/>
    <feComponentTransfer in="blur" result="shape"><feFuncA type="linear" slope="6" intercept="-2.5"/></feComponentTransfer>
    <feFlood flood-color="{color}" result="ink"/>
    <feComposite in="ink" in2="shape" operator="in"/>
  </filter>
  <mask id="{uid}-cut" maskUnits="userSpaceOnUse" x="-100" y="-100" width="400" height="400">
    <rect x="-100" y="-100" width="400" height="400" fill="#fff"/>
    <g transform="{SCISSORS}"><circle cx="-18" cy="-9" r="6.5" fill="#000"/><circle cx="-18" cy="9" r="6.5" fill="#000"/></g>
  </mask>
  <g mask="url(#{uid}-cut)"><g filter="url(#{uid}-soft)">
    <path d="M{ox0:.1f},{oy0:.1f} A{OUT_R},{OUT_R} 0 1 0 {ox1:.1f},{oy1:.1f}" {common} stroke-dasharray="{_arc_dashes(OUT_R, 9)}"/>
    <path d="M{ix0:.1f},{iy0:.1f} A{IN_R},{IN_R} 0 1 1 {ix1:.1f},{iy1:.1f}" {common} stroke-dasharray="{_arc_dashes(IN_R, 5)}"/>
    <path d="{ends}" {common} stroke-dasharray="{piece:.2f} {gap:.2f} {piece:.2f} 100"/>
  </g></g>
  <g transform="{SCISSORS}">
    <circle cx="-18" cy="-9" r="6.5" fill="none" stroke="{color}" stroke-width="4"/>
    <circle cx="-18" cy="9" r="6.5" fill="none" stroke="{color}" stroke-width="4"/>
    <path d="M-12,-6 L21,8 L-6,0 Z M-12,6 L21,-8 L-6,0 Z" fill="{color}"/>
  </g>'''


def c_bounds(scissors: bool = True) -> tuple[float, float, float, float]:
    """Where the C's drawing reaches in its box (ring with its stroke, end bars, and the scissors)."""
    pts = [_pt(OUT_R + STROKE / 2, a) for a in range(END_DEG, 361 - END_DEG)]
    for a in (END_DEG, -END_DEG):  # the end bars' outer corners
        for r in (OUT_R + STROKE / 2, IN_R - STROKE / 2):
            x, y = _pt(r, a)
            nx, ny = -math.sin(math.radians(a)), math.cos(math.radians(a))
            pts += [(x + nx * STROKE / 2, y + ny * STROKE / 2), (x - nx * STROKE / 2, y - ny * STROKE / 2)]
    if not scissors:
        return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)
    rot = math.radians(124)

    def sc(x, y):
        x, y = x * 2.3, y * 2.3
        return 116 + x * math.cos(rot) - y * math.sin(rot), 8 + x * math.sin(rot) + y * math.cos(rot)
    for cy in (-9, 9):
        pts += [sc(-18 + 8.5 * math.cos(t / 36 * math.pi), cy + 8.5 * math.sin(t / 36 * math.pi)) for t in range(72)]
    pts += [sc(x, y) for x, y in [(21, 8), (21, -8), (-12, -6), (-12, 6)]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


X0, Y0, X1, Y1 = c_bounds()


def icon_box() -> str:
    """A square around the C: centered halfway between the ring and the whole drawing (the scissors
    reach far up), so the C sits near the middle without the scissors touching the top edge."""
    rx0, ry0, rx1, ry1 = c_bounds(scissors=False)
    cx = ((rx0 + rx1) / 2 + (X0 + X1) / 2) / 2
    cy = ((ry0 + ry1) / 2 + (Y0 + Y1) / 2) / 2
    half = max(cy - Y0, Y1 - cy, cx - X0, X1 - cx) + 6
    return f"{cx - half:.1f} {cy - half:.1f} {2 * half:.1f} {2 * half:.1f}"


def icon(color: str, name: str) -> None:
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{icon_box()}">
  <title>CleanCut</title>
  {c_drawing(color, "c")}
</svg>
'''
    open(os.path.join(HERE, f"cleancut-icon{name}.svg"), "w", encoding="utf-8").write(svg)


def favicon() -> None:
    """The browser-tab icon: dark C on light browser themes, white C on dark ones."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{icon_box()}" style="color:{DARK}">
  <style>@media (prefers-color-scheme: dark) {{ svg {{ color: {WHITE} !important }} }}</style>
  {c_drawing("currentColor", "c")}
</svg>
'''
    open(os.path.join(STATIC, "favicon.svg"), "w", encoding="utf-8").write(svg)


def page_header() -> None:
    """The C in the page header (its size and spacing next to "leanCut" come from style.css)."""
    path = os.path.join(STATIC, "index.html")
    html = open(path, encoding="utf-8").read()
    block = f'''<!-- logo:start (made by logo/make_logo.py) -->
    <svg class="logo" viewBox="0 0 200 200" role="img" aria-label="C">
  {c_drawing("currentColor", "hdr")}
    </svg><!-- logo:end -->'''
    html, n = re.subn(r"<!-- logo:start.*?<!-- logo:end -->", lambda m: block, html, flags=re.S)
    assert n == 1, "logo markers not found in index.html"
    open(path, "w", encoding="utf-8").write(html)


# ---------------------------------------------------------------- the full name

def wordmark(color: str, name: str) -> None:
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.boundsPen import BoundsPen

    font = TTFont(r"C:\Windows\Fonts\segoeuib.ttf")
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    kern = {}
    for t in font["kern"].kernTables if "kern" in font else []:
        kern.update(getattr(t, "kernTable", {}))
    # Same layout as the page header (style.css .brand / .logo): 52 px text, the C 0.95 em big,
    # sitting 0.112 em below the baseline, the text starting 0.1 em inside the C's box.
    size = 52.0
    c_size, c_drop = 0.95 * size, 0.112 * size
    s = size / font["head"].unitsPerEm
    pen, bounds = SVGPathPen(glyphs), BoundsPen(glyphs)
    x, prev = 0.0, None
    for ch in "leanCut":
        g = cmap[ord(ch)]
        x += kern.get((prev, g), 0) if prev else 0
        t = (s, 0, 0, -s, c_size - 0.1 * size + x * s, 0)
        glyphs[g].draw(TransformPen(pen, t))
        glyphs[g].draw(TransformPen(bounds, t))
        x += font["hmtx"][g][0]
        prev = g
    tx0, ty0, tx1, ty1 = bounds.bounds
    k = c_size / 200
    c_top = c_drop - c_size
    pad = 3
    vx0, vy0 = X0 * k - pad, min(ty0, c_top + Y0 * k) - pad
    vx1, vy1 = tx1 + pad, max(ty1, c_top + Y1 * k) + pad
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx0:.2f} {vy0:.2f} {vx1 - vx0:.2f} {vy1 - vy0:.2f}">
  <title>CleanCut</title>
  <g transform="translate(0,{c_top:.3f}) scale({k:.5f})">
  {c_drawing(color, "c")}
  </g>
  <path d="{pen.getCommands()}" fill="{color}"/>
</svg>
'''
    open(os.path.join(HERE, f"cleancut-logo-{name}.svg"), "w", encoding="utf-8").write(svg)


if __name__ == "__main__":
    icon(WHITE, "")          # white C, see-through (dark dashboards)
    icon(DARK, "-dark")      # dark C, see-through (light dashboards)
    favicon()
    page_header()
    wordmark(WHITE, "white")
    wordmark(DARK, "dark")
    print("C reaches x %.1f..%.1f, y %.1f..%.1f; icon box %s" % (X0, X1, Y0, Y1, icon_box()))
