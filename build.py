#!/usr/bin/env python3
"""Build an interactive, shareable website from deep-research corpus folders.

Usage:
    python3 build.py <research-folder> [<research-folder> ...] [-o dist]

Each folder is either:
  - a corpus with a manifest.json (title/subtitle/documents), or
  - a folder of numbered markdown chapters (00_*.md, 01_*.md, ...) — metadata
    is inferred from frontmatter / first headings.

Optional per-corpus figures: corpus-app/figures/<folder-name>/map.json lists
{"file", "after", "snippet"} entries; each snippet (an HTML/SVG fragment) is
injected into the chapter right after the matching heading line.

Output: a static site in dist/ — index.html (library) plus one self-contained
HTML reader per corpus. No server or internet needed to read it; share the
folder, or deploy it to GitHub Pages / Netlify for a URL.
"""

import argparse
import base64
import hashlib
import html
import json
import math
import mimetypes
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COVERS_DIR = HERE / "covers"  # optional per-corpus photo covers: covers/<slug>.<ext>
MARKED_JS = (HERE / "vendor" / "marked.min.js").read_text()

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect x='2' y='2' width='28' height='28' rx='7' fill='%23b3502f'/>"
           "<rect x='34' y='2' width='28' height='28' rx='7' fill='%23c9a227'/>"
           "<rect x='2' y='34' width='28' height='28' rx='7' fill='%232e5266'/>"
           "<rect x='34' y='34' width='28' height='28' rx='7' fill='%236b7d4f'/></svg>")

# Canonical public origin (GitHub Pages custom domain, see docs/CNAME). Used to
# build the ABSOLUTE og:image URL link-preview scrapers (iMessage, Slack, etc.)
# require — relative paths are ignored by them.
SITE_URL = "https://research.calvincollins.xyz"
OG_IMAGE = "aiforhumanities.png"  # source lives in corpus-app/; copied into out/ at build time

# Open Graph + Twitter card tags so a shared link renders a rich preview with an
# image. The result is passed as a VALUE into each template's .format() (never as
# part of the format string), so any braces in a title pass through untouched.
def og_tags(title, description, url, image):
    """Build a page's Open Graph + Twitter-card <meta> block (a string)."""
    esc = lambda s: html.escape(str(s), quote=True)
    return "\n".join([
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="research · calvincollins · xyz">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(description)}">',
        f'<meta property="og:url" content="{esc(url)}">',
        f'<meta property="og:image" content="{esc(image)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        f'<meta name="twitter:description" content="{esc(description)}">',
        f'<meta name="twitter:image" content="{esc(image)}">',
    ])


# Site-wide default (library index, Ghost, Fingerprint section fronts — anything
# that isn't a single corpus). Per-corpus reader pages build their own below.
OG_META = og_tags(
    "AI for HUMANities — Agentic Scholarship",
    "A library of deep research, plus The Ghost of Times and The Fingerprint.",
    f"{SITE_URL}/",
    f"{SITE_URL}/{OG_IMAGE}",
)

# trencadís tile palette (light-theme hexes; readers/library recolor via CSS vars)
TERRA, GOLD, BLUE, OLIVE, PLUM = "#b3502f", "#c9a227", "#2e5266", "#6b7d4f", "#8a5a7c"


# ---------------------------------------------------------------- loading

def parse_frontmatter(text):
    """Return (meta dict, body) from a markdown file with optional YAML-ish frontmatter."""
    meta = {}
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
                if kv:
                    val = kv.group(2).strip().strip('"').strip("'")
                    meta[kv.group(1).lower()] = val
            text = text[m.end():]
    return meta, text


def first_heading(body):
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def humanize(name):
    name = re.sub(r"-research$", "", name)
    return name.replace("-", " ").replace("_", " ").title()


def load_corpus(folder):
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    corpus = {
        "slug": re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-"),
        "title": humanize(folder.name),
        "subtitle": "",
        "author": "",
        "generated": "",
        "documents": [],
    }

    entries = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if "documents" in manifest:  # narrative-corpus schema
            for key in ("title", "subtitle", "author", "generated"):
                if manifest.get(key):
                    corpus[key] = manifest[key]
            entries = [
                (d.get("order", i), d["file"], d.get("title", ""), d.get("summary", ""))
                for i, d in enumerate(manifest.get("documents", []))
            ]
        elif "topic" in manifest:  # deep-research skill schema
            corpus["title"] = manifest["topic"].rstrip(" :")
            corpus["subtitle"] = manifest.get("sharpened_question", "")
            corpus["generated"] = (manifest.get("generated_at") or "")[:10]
            index = manifest.get("documents_index")
            if index:
                entries = [(i, d["file"], "", "") for i, d in enumerate(index)]
    if entries is None:
        files = sorted(p.name for p in folder.glob("[0-9][0-9]_*.md"))
        # deep-research corpora keep their forecast pillar in an unnumbered file
        if (folder / "Future_Trajectory.md").exists():
            files.append("Future_Trajectory.md")
        entries = [(i, f, "", "") for i, f in enumerate(files)]

    for order, fname, title, summary in entries:
        path = folder / fname
        if not path.exists():
            print(f"  ! missing {fname}, skipped", file=sys.stderr)
            continue
        meta, body = parse_frontmatter(path.read_text())
        corpus["documents"].append({
            "order": order,
            "file": fname,
            "title": title or meta.get("title") or first_heading(body) or humanize(fname[3:-3]),
            "summary": summary or meta.get("summary", ""),
            "body": body.strip(),
        })

    if not manifest_path.exists() and corpus["documents"]:
        # try to find a nicer corpus title from a README or plan file
        for candidate in ("README.md", "RESEARCH_PLAN.md"):
            p = folder / candidate
            if p.exists():
                h = first_heading(p.read_text())
                if h:
                    h = re.sub(r"research plan:?\s*", "", h, flags=re.I).strip().rstrip(" :")
                    corpus["title"] = h or corpus["title"]
                    break

    return corpus


def inject_figures(corpus, folder):
    """Splice HTML/SVG figure snippets into chapter bodies, per figures/<folder>/map.json."""
    figdir = HERE / "figures" / Path(folder).name
    map_path = figdir / "map.json"
    if not map_path.exists():
        return 0
    inserts = json.loads(map_path.read_text())
    by_file = {}
    for ins in inserts:
        by_file.setdefault(ins["file"], []).append(ins)
    count = 0
    for doc in corpus["documents"]:
        for ins in by_file.get(doc["file"], []):
            snippet_path = figdir / ins["snippet"]
            if not snippet_path.exists():
                print(f"  ! figure snippet missing: {ins['snippet']}", file=sys.stderr)
                continue
            # strip blank lines so the markdown renderer treats it as one raw HTML block
            snippet = "\n".join(l for l in snippet_path.read_text().splitlines() if l.strip())
            body = doc["body"]
            idx = body.find(ins["after"])
            if idx < 0:
                print(f"  ! anchor not found in {doc['file']}: {ins['after']!r}", file=sys.stderr)
                continue
            line_end = body.find("\n", idx)
            if line_end < 0:
                line_end = len(body)
            doc["body"] = body[:line_end] + "\n\n" + snippet + "\n" + body[line_end:]
            count += 1
    return count


# --------------------------------------------------------- ```viz chart fences
# Deep-research corpora embed small JSON chart specs in fenced ```viz blocks
# (bar / column / line / donut / range / timeline / flow). The standalone HTML
# volumes render these in deep-research/scripts/build_html.py; here we do the
# same at build time, rewriting each fence into an inline SVG/HTML figure that
# the client markdown renderer (marked.js) passes straight through as one raw
# HTML block — exactly like the corpus-fig snippets inject_figures splices in.
# Colors are the per-corpus theme classes (f-t1..f-t5 / t-muted / ln / ln-soft),
# so every chart recolors in light & dark and adopts each corpus's palette. Bad
# JSON degrades to a visible box rather than failing the build.

VIZ_FENCE_RE = re.compile(r"```viz[^\S\n]*\n(.*?)\n[ \t]*```", re.S)
_VIZ_NPAL = 5  # theme accent tokens --t1..--t5 the palette cycles through


def _viz_esc(s):
    return html.escape(str(s), quote=True)


def _viz_trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _viz_fmt_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _viz_esc(v)
    a = abs(f)
    if a >= 1e9:
        s = f"{f / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    elif a >= 1e6:
        s = f"{f / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    elif a >= 1e4:
        s = f"{f / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    elif f == int(f):
        s = f"{int(f):,}"
    else:
        s = f"{f:,.2f}".rstrip("0").rstrip(".")
    return s


def _viz_nice_max(v):
    """Smallest 'nice' number >= v, for axis tops; keeps quarter ticks clean."""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    frac = v / 10 ** exp
    for n in (1, 2, 3, 4, 6, 8, 10):
        if frac <= n:
            return n * 10 ** exp
    return 10 ** (exp + 1)


def _viz_svg_open(w, h):
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" preserveAspectRatio="xMidYMid meet">')


def _viz_legend(series):
    spans = "".join(
        f'<span><i style="background:var(--t{i % _VIZ_NPAL + 1})"></i>{_viz_esc(s)}</span>'
        for i, s in enumerate(series)
    )
    return f'<div class="viz-legend">{spans}</div>'


def _viz_bar(spec):
    """Horizontal bars — comparisons with longish labels."""
    items = spec["items"]
    if not items:
        raise ValueError("bar: items is empty")
    vals = [float(i["value"]) for i in items]
    vmax = _viz_nice_max(max(vals))
    unit = _viz_esc(spec.get("unit", ""))
    W, LP, RP, TP, bh, gap = 700, 185, 70, 8, 22, 12
    PW = W - LP - RP
    H = TP + len(items) * (bh + gap) + 28
    out = [_viz_svg_open(W, H)]
    for k in range(5):
        gx = LP + PW * k / 4
        out.append(f'<line x1="{gx:.1f}" y1="{TP}" x2="{gx:.1f}" y2="{H - 24}" class="ln-soft" stroke-width="1"/>')
        out.append(f'<text x="{gx:.1f}" y="{H - 8}" font-size="12" class="t-muted" text-anchor="middle">{_viz_fmt_num(vmax * k / 4)}</text>')
    y = TP
    for it in items:
        v = float(it["value"])
        bw = PW * v / vmax if vmax else 0
        out.append(f'<text x="{LP - 10}" y="{y + bh - 6}" font-size="13" text-anchor="end">{_viz_esc(_viz_trunc(it["label"], 27))}</text>')
        out.append(f'<rect x="{LP}" y="{y}" width="{bw:.1f}" height="{bh}" rx="3" class="f-t1"/>')
        out.append(f'<text x="{LP + bw + 8:.1f}" y="{y + bh - 6}" font-size="12.5" class="t-muted">{_viz_fmt_num(v)}{unit}</text>')
        y += bh + gap
    out.append("</svg>")
    return "".join(out)


def _viz_column(spec):
    """Vertical bars — short labels, e.g. years or quarters."""
    items = spec["items"]
    if not items:
        raise ValueError("column: items is empty")
    n = len(items)
    vals = [float(i["value"]) for i in items]
    vmax = _viz_nice_max(max(vals))
    unit = _viz_esc(spec.get("unit", ""))
    rotate = n > 7 or any(len(str(i["label"])) > 7 for i in items)
    W, H, LP, RP, TP = 700, 330, 60, 14, 16
    BP = 80 if rotate else 42
    PW, PH = W - LP - RP, H - TP - BP
    slot = PW / n
    cw = min(56, slot * 0.62)
    out = [_viz_svg_open(W, H)]
    for k in range(5):
        gy = TP + PH * (1 - k / 4)
        out.append(f'<line x1="{LP}" y1="{gy:.1f}" x2="{W - RP}" y2="{gy:.1f}" class="ln-soft" stroke-width="1"/>')
        out.append(f'<text x="{LP - 8}" y="{gy + 4:.1f}" font-size="12" class="t-muted" text-anchor="end">{_viz_fmt_num(vmax * k / 4)}</text>')
    for i, it in enumerate(items):
        v = float(it["value"])
        bh = PH * v / vmax if vmax else 0
        x = LP + slot * i + (slot - cw) / 2
        y = TP + PH - bh
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw:.1f}" height="{bh:.1f}" rx="3" class="f-t1"/>')
        out.append(f'<text x="{x + cw / 2:.1f}" y="{y - 6:.1f}" font-size="12" class="t-muted" text-anchor="middle">{_viz_fmt_num(v)}{unit}</text>')
        lx, ly = x + cw / 2, TP + PH + 18
        label = _viz_esc(_viz_trunc(it["label"], 14))
        if rotate:
            out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="12" text-anchor="end" transform="rotate(-35 {lx:.1f} {ly:.1f})">{label}</text>')
        else:
            out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="12.5" text-anchor="middle">{label}</text>')
    out.append("</svg>")
    return "".join(out)


def _viz_line(spec):
    """One or more series over an ordered x-axis (trends over time)."""
    xs = [str(x) for x in spec["x"]]
    series = spec["series"]
    if not xs or not series:
        raise ValueError("line: x and series are required")
    all_vals = [float(v) for s in series for v in s["values"] if v is not None]
    vmax = _viz_nice_max(max(all_vals))
    vmin = min(all_vals)
    vmin = -_viz_nice_max(-vmin) if vmin < 0 else 0.0
    W, H, LP, RP, TP, BP = 700, 330, 60, 18, 14, 44
    PW, PH = W - LP - RP, H - TP - BP

    def X(i):
        return LP + (PW * i / (len(xs) - 1) if len(xs) > 1 else PW / 2)

    def Y(v):
        return TP + PH * (1 - (v - vmin) / (vmax - vmin))

    out = [_viz_svg_open(W, H)]
    for k in range(5):
        val = vmin + (vmax - vmin) * k / 4
        gy = Y(val)
        out.append(f'<line x1="{LP}" y1="{gy:.1f}" x2="{W - RP}" y2="{gy:.1f}" class="ln-soft" stroke-width="1"/>')
        out.append(f'<text x="{LP - 8}" y="{gy + 4:.1f}" font-size="12" class="t-muted" text-anchor="end">{_viz_fmt_num(val)}</text>')
    step = max(1, math.ceil(len(xs) / 8))
    for i in range(0, len(xs), step):
        out.append(f'<text x="{X(i):.1f}" y="{H - 18}" font-size="12" class="t-muted" text-anchor="middle">{_viz_esc(_viz_trunc(xs[i], 10))}</text>')
    for si, s in enumerate(series):
        scls = f"s-t{si % _VIZ_NPAL + 1}"
        fcls = f"f-t{si % _VIZ_NPAL + 1}"
        pts = [(X(i), Y(float(v))) for i, v in enumerate(s["values"]) if v is not None]
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        out.append(f'<polyline points="{path}" fill="none" class="{scls}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        if len(pts) <= 30:
            for px, py in pts:
                out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" class="{fcls}"/>')
    out.append("</svg>")
    legend = _viz_legend([s["name"] for s in series]) if len(series) > 1 else ""
    return legend + "".join(out)


def _viz_donut(spec):
    """Composition / share-of-whole, with a legend."""
    items = spec["items"]
    total = sum(float(i["value"]) for i in items)
    if total <= 0:
        raise ValueError("donut: values must sum to > 0")
    cx = cy = 110
    r, stroke = 72, 42
    circ = 2 * math.pi * r
    out = [_viz_svg_open(220, 220)]
    offset = 0.0
    legend_rows = []
    for i, it in enumerate(items):
        frac = float(it["value"]) / total
        seg = circ * frac
        scls = f"s-t{i % _VIZ_NPAL + 1}"
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" class="{scls}" '
            f'stroke-width="{stroke}" stroke-dasharray="{seg:.2f} {circ - seg:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += seg
        legend_rows.append(
            f'<span><i style="background:var(--t{i % _VIZ_NPAL + 1})"></i>{_viz_esc(it["label"])} '
            f'<b>{frac * 100:.0f}%</b></span>'
        )
    out.append("</svg>")
    return (f'<div class="viz-donutwrap">{"".join(out)}'
            f'<div class="viz-legend viz-legend-col">{"".join(legend_rows)}</div></div>')


def _viz_range(spec):
    """Horizontal probability/interval ranges — built for forecast scenarios."""
    items = spec["items"]
    if not items:
        raise ValueError("range: items is empty")
    scale = 100 if all(float(i["high"]) <= 1 for i in items) else 1
    W, LP, RP, TP, bh, gap = 700, 185, 24, 8, 20, 16
    PW = W - LP - RP
    H = TP + len(items) * (bh + gap) + 28
    out = [_viz_svg_open(W, H)]
    for k in range(5):
        gx = LP + PW * k / 4
        out.append(f'<line x1="{gx:.1f}" y1="{TP}" x2="{gx:.1f}" y2="{H - 24}" class="ln-soft" stroke-width="1"/>')
        out.append(f'<text x="{gx:.1f}" y="{H - 8}" font-size="12" class="t-muted" text-anchor="middle">{k * 25}%</text>')
    y = TP
    for i, it in enumerate(items):
        lo, hi = float(it["low"]) * scale, float(it["high"]) * scale
        x1, x2 = LP + PW * lo / 100, LP + PW * hi / 100
        fcls = f"f-t{i % _VIZ_NPAL + 1}"
        out.append(f'<text x="{LP - 10}" y="{y + bh - 5}" font-size="13" text-anchor="end">{_viz_esc(_viz_trunc(it["label"], 27))}</text>')
        out.append(f'<line x1="{LP}" y1="{y + bh / 2:.1f}" x2="{W - RP}" y2="{y + bh / 2:.1f}" class="ln-soft" stroke-width="1"/>')
        out.append(f'<rect x="{x1:.1f}" y="{y}" width="{max(x2 - x1, 2):.1f}" height="{bh}" rx="10" class="{fcls}" fill-opacity="0.85"/>')
        out.append(f'<text x="{x2 + 8:.1f}" y="{y + bh - 5}" font-size="12.5" class="t-muted">{lo:.0f}–{hi:.0f}%</text>')
        y += bh + gap
    out.append("</svg>")
    return "".join(out)


def _viz_timeline(spec):
    """Vertical dated timeline — HTML, so long labels wrap cleanly."""
    events = spec["events"]
    if not events:
        raise ValueError("timeline: events is empty")
    rows = "".join(
        f'<div class="tlrow"><div class="tldate">{_viz_esc(e["date"])}</div>'
        f'<div class="tlbody">{_viz_esc(e["label"])}</div></div>'
        for e in events
    )
    return f'<div class="viz-timeline">{rows}</div>'


def _viz_flow(spec):
    """Linear process / causal-chain diagram — boxes joined by arrows."""
    steps = spec["steps"]
    if not steps:
        raise ValueError("flow: steps is empty")
    parts = []
    for i, s in enumerate(steps):
        if i:
            parts.append('<span class="flowarrow">→</span>')
        parts.append(f'<span class="flowstep">{_viz_esc(s)}</span>')
    return f'<div class="viz-flow">{"".join(parts)}</div>'


VIZ_RENDERERS = {
    "bar": _viz_bar, "column": _viz_column, "line": _viz_line, "donut": _viz_donut,
    "range": _viz_range, "timeline": _viz_timeline, "flow": _viz_flow,
}


def render_viz_fence(spec_text):
    """Render one ```viz JSON spec to a single-line <figure class="viz"> block.
    Single-line output (the renderers join with "") is required so marked.js
    treats it as one raw HTML block when it parses the chapter body client-side."""
    try:
        spec = json.loads(spec_text)
        vtype = spec.get("type")
        if vtype not in VIZ_RENDERERS:
            raise ValueError(f"unknown viz type {vtype!r} "
                             f"(known: {', '.join(sorted(VIZ_RENDERERS))})")
        body = VIZ_RENDERERS[vtype](spec)
    except Exception as e:  # degrade visibly, never break the build
        esc = html.escape(spec_text).replace("\n", "&#10;")
        return ('<div class="viz-error"><strong>Unrendered visualization</strong> '
                f'({_viz_esc(e)})<pre>{esc}</pre></div>')
    parts = ['<figure class="viz">']
    if spec.get("title"):
        parts.append(f'<div class="viz-title">{_viz_esc(spec["title"])}</div>')
    parts.append(body)
    cap, cred = spec.get("caption", ""), spec.get("credit", "")
    if cap or cred:
        credit = f' <span class="credit">Source: {_viz_esc(cred)}.</span>' if cred else ""
        parts.append(f"<figcaption>{_viz_esc(cap)}{credit}</figcaption>")
    parts.append("</figure>")
    return "".join(parts)


def transform_viz(md):
    """Rewrite every ```viz fence in a chapter body to an inline themed figure.
    Returns (new_markdown, count). Each figure is blank-line-isolated so the
    client markdown renderer starts a fresh raw-HTML block for it."""
    n = 0

    def repl(m):
        nonlocal n
        n += 1
        return "\n\n" + render_viz_fence(m.group(1)) + "\n\n"

    return VIZ_FENCE_RE.sub(repl, md), n


# ---------------------------------------------------------------- per-corpus theme
# A corpus opts into its own bespoke visual identity by dropping
# figures/<folder-name>/theme.json next to its map.json. When present, the
# reader page for that corpus gets an extra <style> block that overrides the
# design tokens (palette light+dark, fonts, ornament) — so each corpus reads as
# its own world. Absent → the default trencadís identity, byte-for-byte unchanged.
# Authored by the corpus-visuals skill.

# tokens a theme may override (CSS var name == key)
_THEME_TOKENS = ("bg", "panel", "text", "muted", "accent", "border", "mark",
                 "t1", "t2", "t3", "t4", "t5", "cover-bg")


def _safe_font(stack):
    """Reject web-font / external references — reader pages are fully offline."""
    if not stack:
        return ""
    return "" if ("url(" in stack or "http" in stack or "@import" in stack) else stack


def _theme_vars(palette, fonts=None, ornament=None):
    """Render the CSS custom-property declarations for one theme (light or dark)."""
    decls = [f"--{k}: {palette[k]};" for k in _THEME_TOKENS if k in palette]
    for css_var, key in (("--display", "display"), ("--serif", "body"), ("--mono", "mono")):
        val = _safe_font((fonts or {}).get(key))
        if val:
            decls.append(f"{css_var}: {val};")
    for css_var, key in (("--hr-glyph", "hr_glyph"), ("--fig-radius", "fig_radius")):
        val = (ornament or {}).get(key)
        if val:
            decls.append(f"{css_var}: {val};")
    return " ".join(decls)


def load_theme_spec(folder):
    """Return the parsed theme.json dict for a corpus, or None if it has none."""
    theme_path = HERE / "figures" / Path(folder).name / "theme.json"
    if not theme_path.exists():
        return None
    return json.loads(theme_path.read_text())


def render_theme_style(spec):
    """Build the per-corpus <style> override from a theme.json dict (or '')."""
    if not spec:
        return ""
    fonts, ornament = spec.get("fonts", {}), spec.get("ornament", {})
    rules = []
    light = _theme_vars(spec.get("light", {}), fonts, ornament)
    if light:
        rules.append(f":root {{ {light} }}")
    dark = _theme_vars(spec.get("dark", {}))
    if dark:
        rules.append(f'[data-theme="dark"] {{ {dark} }}')
    if _safe_font(fonts.get("mono")):
        rules.append("#content code, #content pre code { font-family: var(--mono); }")
    return "<style>" + " ".join(rules) + "</style>" if rules else ""


def theme_cover_palette(spec):
    """A 4-colour tuple for the generative card cover, drawn from the theme (or None)."""
    if not spec:
        return None
    if spec.get("cover_palette"):
        return tuple(spec["cover_palette"])[:4]
    light = spec.get("light", {})
    keys = [k for k in ("t1", "t2", "t3", "t4", "t5", "accent") if k in light]
    return tuple(light[k] for k in keys[:4]) if len(keys) >= 4 else None


# ---------------------------------------------------------------- cover art
# Deterministic generative covers: every corpus gets its own small piece of
# trencadís — a rose window, an arch arcade, a shard mosaic, or sun-over-hills.

def _rng(seed):
    state = (seed & 0x7FFFFFFF) or 1
    while True:
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        yield state / 0x7FFFFFFF


COVER_PALETTES = [
    (TERRA, GOLD, BLUE, OLIVE),
    (BLUE, TERRA, GOLD, PLUM),
    (OLIVE, BLUE, TERRA, GOLD),
    (PLUM, GOLD, BLUE, TERRA),
]


def _cover_rose(r, pal):
    cx, cy = 160, 74
    parts = []
    for i in range(14):
        ang = i * (360 / 14)
        c = pal[i % len(pal)]
        parts.append(f"<ellipse cx='{cx}' cy='{cy - 40}' rx='13' ry='34' fill='{c}' "
                     f"opacity='.9' transform='rotate({ang:.0f} {cx} {cy})'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='22' fill='{pal[1]}'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='10' fill='{pal[0]}'/>")
    for i in range(10):
        ang = i * 36
        parts.append(f"<circle cx='{cx}' cy='{cy - 18}' r='2.6' fill='{pal[2]}' "
                     f"transform='rotate({ang} {cx} {cy})'/>")
    # corner shards
    for _ in range(8):
        x, y = next(r) * 320, next(r) * 140
        if abs(x - cx) < 95 and abs(y - cy) < 85:
            continue
        s = 7 + next(r) * 9
        c = pal[int(next(r) * 4) % 4]
        parts.append(f"<rect x='{x:.0f}' y='{y:.0f}' width='{s:.0f}' height='{s:.0f}' rx='2' "
                     f"fill='{c}' opacity='.55' transform='rotate({next(r)*50-25:.0f} {x:.0f} {y:.0f})'/>")
    return parts


def _cover_arcade(r, pal):
    parts = [f"<circle cx='{52 + next(r)*40:.0f}' cy='30' r='17' fill='{pal[1]}' opacity='.9'/>"]
    w = 74
    for i in range(4):
        x0 = 12 + i * (w + 4)
        c = pal[i % len(pal)]
        parts.append(f"<path d='M{x0} 140 L{x0} 78 Q{x0 + w/2} 22 {x0 + w} 78 L{x0 + w} 140 Z' "
                     f"fill='{c}' opacity='.88'/>")
        parts.append(f"<path d='M{x0 + 14} 140 L{x0 + 14} 84 Q{x0 + w/2} 44 {x0 + w - 14} 84 "
                     f"L{x0 + w - 14} 140 Z' fill='var(--cover-bg, #f3ead8)'/>")
    for i in range(12):
        parts.append(f"<circle cx='{18 + next(r)*284:.0f}' cy='{12 + next(r)*22:.0f}' r='2.4' "
                     f"fill='{pal[int(next(r)*4)%4]}' opacity='.7'/>")
    return parts


def _cover_shards(r, pal):
    parts = []
    for row in range(4):
        for col in range(8):
            if next(r) < 0.16:
                continue
            x, y = col * 40 + 2, row * 35 + 2
            j = lambda: (next(r) - 0.5) * 12
            c = pal[int(next(r) * 4) % 4]
            parts.append(
                f"<polygon points='{x + j():.0f},{y + j():.0f} {x + 36 + j():.0f},{y + j():.0f} "
                f"{x + 36 + j():.0f},{y + 31 + j():.0f} {x + j():.0f},{y + 31 + j():.0f}' "
                f"fill='{c}' opacity='{0.55 + next(r) * 0.4:.2f}'/>")
    return parts


def _cover_hills(r, pal):
    sx = 80 + next(r) * 160
    parts = []
    for i in range(12):
        ang = i * 30
        parts.append(f"<rect x='{sx - 1.6:.0f}' y='8' width='3.2' height='13' rx='1.6' fill='{pal[1]}' "
                     f"transform='rotate({ang} {sx:.0f} 46)'/>")
    parts.append(f"<circle cx='{sx:.0f}' cy='46' r='19' fill='{pal[1]}'/>")
    parts.append(f"<path d='M0 96 Q80 {52 + next(r)*18:.0f} 170 92 T320 88 L320 140 L0 140 Z' "
                 f"fill='{pal[3]}' opacity='.85'/>")
    parts.append(f"<path d='M0 112 Q{90 + next(r)*60:.0f} {70 + next(r)*16:.0f} 210 108 T320 104 "
                 f"L320 140 L0 140 Z' fill='{pal[2]}' opacity='.8'/>")
    parts.append(f"<path d='M0 126 Q160 {100 + next(r)*12:.0f} 320 122 L320 140 L0 140 Z' "
                 f"fill='{pal[0]}' opacity='.85'/>")
    return parts


def cover_svg(slug, palette=None):
    seed = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16)
    r = _rng(seed)
    pal = tuple(palette) if palette and len(palette) >= 4 else COVER_PALETTES[(seed >> 3) % 4]
    variant = seed % 4
    parts = [_cover_rose, _cover_arcade, _cover_shards, _cover_hills][variant](r, pal)
    return ("<svg viewBox='0 0 320 140' preserveAspectRatio='xMidYMid slice' "
            "xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>" + "".join(parts) + "</svg>")


COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")


def find_cover_image(slug):
    """Return the path of a custom photo cover for this slug, or None.

    A corpus opts into a photo cover by dropping covers/<slug>.<ext> next to
    build.py. The image only appears on the library card — never in the reader.
    """
    if not COVERS_DIR.is_dir():
        return None
    for ext in COVER_EXTS:
        p = COVERS_DIR / f"{slug}{ext}"
        if p.exists():
            return p
    return None


def publish_cover_for_og(slug, out):
    """Copy a corpus's photo cover into out/covers/ and return its absolute URL.

    Link-preview scrapers need a real raster file at an absolute https URL — the
    library card embeds covers as base64 (no served file) and generative covers
    are SVG (which iMessage/most scrapers won't render), so a corpus without a
    photo cover returns None and the caller falls back to the site banner.
    """
    img = find_cover_image(slug)
    if img is None:
        return None
    dest_dir = out / "covers"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(img, dest_dir / img.name)
    return f"{SITE_URL}/covers/{img.name}"


def card_cover(slug, title="", palette=None):
    """The library-card cover: a custom photo if one exists, else generative art.

    The photo is base64-embedded so index.html stays self-contained (shareable as
    a single folder, works from file://). object-fit:cover in the CSS crops any
    aspect ratio to the card band, so the source image can be any size.
    """
    img = find_cover_image(slug)
    if img is None:
        return cover_svg(slug, palette)
    mime = mimetypes.guess_type(img.name)[0] or "image/png"
    b64 = base64.b64encode(img.read_bytes()).decode("ascii")
    alt = html.escape(title or slug)
    return f'<img class="cover-photo" src="data:{mime};base64,{b64}" alt="{alt}" decoding="async">'


def hero_svg():
    """The library hero: a trencadís rose-window sun over an arcade of catenary arches."""
    parts = []
    cx, cy = 300, 208
    for i in range(18):
        ang = -90 + i * (180 / 17)
        c = [TERRA, GOLD, BLUE, OLIVE][i % 4]
        parts.append(f"<ellipse cx='{cx}' cy='{cy - 96}' rx='17' ry='52' fill='{c}' opacity='.92' "
                     f"transform='rotate({ang:.1f} {cx} {cy})' class='tile-{i % 4}'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='62' class='tile-1' fill='{GOLD}'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='34' class='tile-0' fill='{TERRA}'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='13' class='hero-core' fill='#f6efe2'/>")
    for i in range(13):
        ang = -83 + i * (166 / 12)
        c = [BLUE, OLIVE, TERRA][i % 3]
        parts.append(f"<circle cx='{cx}' cy='{cy - 47}' r='4' fill='{c}' class='tile-{(i % 3) + 1 if i % 3 != 2 else 0}' "
                     f"transform='rotate({ang:.1f} {cx} {cy})'/>")
    # arcade silhouette along the horizon
    w = 92
    for i in range(7):
        x0 = -30 + i * (w + 6)
        c = [BLUE, OLIVE, TERRA, GOLD][i % 4]
        parts.append(f"<path d='M{x0} 232 L{x0} 196 Q{x0 + w/2} 138 {x0 + w} 196 L{x0 + w} 232 Z' "
                     f"fill='{c}' class='tile-{(i + 2) % 4}' opacity='.5'/>")
    parts.append("<rect x='-40' y='230' width='760' height='4' rx='2' class='hero-ground' fill='#1f1d1a' opacity='.25'/>")
    r = _rng(20260612)
    for _ in range(16):
        x, y = next(r) * 660, 12 + next(r) * 96
        if abs(x - cx) < 150:
            continue
        s = 6 + next(r) * 10
        c = [TERRA, GOLD, BLUE, OLIVE][int(next(r) * 4) % 4]
        parts.append(f"<rect x='{x:.0f}' y='{y:.0f}' width='{s:.0f}' height='{s:.0f}' rx='2' fill='{c}' "
                     f"opacity='.45' transform='rotate({next(r)*60-30:.0f} {x:.0f} {y:.0f})'/>")
    return ("<svg viewBox='0 0 660 236' xmlns='http://www.w3.org/2000/svg' role='img' "
            "aria-label='A mosaic sun rising over an arcade of arches'>" + "".join(parts) + "</svg>")


def hero_art():
    """The library hero. Uses aiforhumanities.<ext> if present (base64-embedded so
    index.html stays self-contained), else falls back to the generative mosaic SVG."""
    for ext in COVER_EXTS:
        img = HERE / f"aiforhumanities{ext}"
        if img.exists():
            mime = mimetypes.guess_type(img.name)[0] or "image/png"
            b64 = base64.b64encode(img.read_bytes()).decode("ascii")
            return (f"<img class='hero-img' src='data:{mime};base64,{b64}' "
                    "alt='AI for HUMANities' decoding='async'>")
    return hero_svg()


# ---------------------------------------------------------------- templates

READER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
{theme_style}
</head>
<body>
<div id="reader-progress" aria-hidden="true"></div>
<button id="menu-btn" title="Chapters">☰</button>
<aside id="sidebar">
  <a class="back" href="index.html">← Library</a>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <h1>{title}</h1>
  <p class="subtitle">{subtitle}</p>
  <input id="search" type="search" placeholder="Search the corpus…" autocomplete="off">
  <div id="search-results"></div>
  <nav id="toc"></nav>
  <div id="reader-controls">
    <button id="listen-btn" title="Listen to this chapter">▶ Listen</button>
    <button id="type-btn" title="Text size &amp; width" aria-haspopup="true">Aa</button>
    <button id="share-btn" title="Share this chapter">Share ↗</button>
    <button id="theme-btn">◐ Theme</button>
  </div>
</aside>
<main id="main">
  <article id="content"></article>
  <div id="pager">
    <button id="prev">←</button>
    <span id="pager-label"></span>
    <button id="next">→</button>
  </div>
  <section id="related"></section>
</main>
<script id="corpus-data" type="application/json">{data_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

CSS = """
:root {
  --bg: #faf8f4; --panel: #f1ede5; --text: #1f1d1a; --muted: #6e6a62;
  --accent: #8c4a2f; --border: #ddd6c9; --mark: #f3dfa0;
  --t1: #b3502f; --t2: #c9a227; --t3: #2e5266; --t4: #6b7d4f; --t5: #8a5a7c;
  --cover-bg: #f3ead8;
  --serif: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
  --display: 'Iowan Old Style', Palatino, Georgia, serif;
  --sans: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
}
[data-theme="dark"] {
  --bg: #161513; --panel: #1f1d1a; --text: #e8e4db; --muted: #968f82;
  --accent: #d98f5f; --border: #35322c; --mark: #5c4a1e;
  --t1: #d4795a; --t2: #d8b545; --t3: #6f9bb3; --t4: #93a86f; --t5: #b07a9e;
  --cover-bg: #232019;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--serif); }
#sidebar {
  position: fixed; top: 0; left: 0; bottom: 0; width: 320px; overflow-y: auto;
  background: var(--panel); border-right: 1px solid var(--border); padding: 1.2rem 1.2rem 2rem;
}
.tiles { display: flex; gap: 5px; margin: .9rem 0 .2rem; }
.tiles span { width: 13px; height: 13px; }
.tiles span:nth-child(1) { background: var(--t1); border-radius: 3px 8px 4px 7px; transform: rotate(-6deg); }
.tiles span:nth-child(2) { background: var(--t2); border-radius: 7px 3px 8px 4px; transform: rotate(8deg); }
.tiles span:nth-child(3) { background: var(--t3); border-radius: 4px 7px 3px 8px; transform: rotate(-4deg); }
.tiles span:nth-child(4) { background: var(--t4); border-radius: 8px 4px 7px 3px; transform: rotate(5deg); }
#sidebar h1 { font-family: var(--display); font-size: 1.22rem; line-height: 1.28; margin: .5rem 0 .25rem; }
#sidebar .subtitle { font-size: .8rem; color: var(--muted); margin: 0 0 1rem; font-family: var(--sans); }
.back { font-family: var(--sans); font-size: .78rem; color: var(--muted); text-decoration: none;
  text-transform: uppercase; letter-spacing: .08em; }
.back:hover { color: var(--accent); }
#search { width: 100%; padding: .5rem .7rem; font-size: .85rem; border: 1px solid var(--border);
  border-radius: 10px; background: var(--bg); color: var(--text); font-family: var(--sans); }
#search:focus { outline: none; border-color: var(--accent); }
#toc { margin-top: .8rem; }
#toc a { display: block; padding: .45rem .55rem; margin: .1rem 0; border-radius: 8px;
  color: var(--text); text-decoration: none; font-family: var(--sans); font-size: .84rem; line-height: 1.35;
  border-left: 2px solid transparent; }
#toc a .num { color: var(--muted); font-size: .72rem; margin-right: .4rem; }
#toc a:hover { background: var(--bg); }
#toc a.active { background: var(--bg); color: var(--accent); font-weight: 600; border-left-color: var(--accent); }
#search-results { font-family: var(--sans); font-size: .8rem; }
#search-results .hit { padding: .5rem .55rem; border-bottom: 1px solid var(--border); cursor: pointer; border-radius: 8px; }
#search-results .hit:hover { background: var(--bg); }
#search-results .hit b { color: var(--accent); display: block; margin-bottom: .15rem; }
#search-results mark { background: var(--mark); color: inherit; border-radius: 2px; }
#search-results .none { color: var(--muted); padding: .5rem .55rem; }
#reader-controls { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1.2rem; }
#reader-controls button { margin: 0; font-family: var(--sans); font-size: .76rem; color: var(--muted);
  background: none; border: 1px solid var(--border); border-radius: 10px; padding: .35rem .7rem; cursor: pointer; }
#reader-controls button:hover { color: var(--accent); border-color: var(--accent); }
#listen-btn.on { color: var(--bg); background: var(--accent); border-color: var(--accent); }
/* floating Listen bar (Web Speech narration) */
#listen-bar { position: fixed; left: 50%; bottom: 1rem; transform: translateX(-50%); z-index: 40; display: none;
  align-items: center; gap: .7rem; flex-wrap: wrap; max-width: 92vw; background: var(--panel);
  border: 1px solid var(--border); border-radius: 14px; padding: .5rem .8rem; box-shadow: 0 8px 30px rgba(0,0,0,.18); }
#listen-bar.show { display: flex; }
#listen-bar button { font-family: var(--sans); font-size: 1rem; background: none; border: 1px solid var(--border);
  border-radius: 9px; width: 34px; height: 30px; color: var(--text); cursor: pointer; }
#listen-bar button:hover { border-color: var(--accent); color: var(--accent); }
#lb-title { font-family: var(--display); font-size: .9rem; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#listen-bar .lb-ctl { font-family: var(--sans); font-size: .66rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: .06em; display: flex; align-items: center; gap: .3rem; }
#listen-bar select { font-family: var(--sans); font-size: .72rem; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px; padding: .15rem .3rem; }
@media (max-width: 560px) { #lb-title { display: none; } }
#main { margin-left: 320px; display: flex; flex-direction: column; min-height: 100vh; }
#content { max-width: var(--reader-measure, 740px); width: 100%; margin: 0 auto; padding: 3rem 2rem 2rem;
  font-size: var(--reader-fs, 1.04rem); line-height: 1.72; flex: 1; }
/* top reading-progress bar */
#reader-progress { position: fixed; top: 0; left: 0; height: 3px; width: 0; z-index: 30;
  background: linear-gradient(90deg, var(--t1), var(--t2), var(--t3), var(--t4)); transition: width .08s linear; }
/* text size / measure popover (toggled by the Aa button) */
#type-panel { position: fixed; z-index: 35; background: var(--bg); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 16px 44px rgba(0,0,0,.28); padding: .8rem; width: 220px; display: none; }
#type-panel.open { display: block; }
#type-panel .tp-row { display: flex; align-items: center; justify-content: space-between; gap: .6rem; margin-bottom: .6rem; }
#type-panel .tp-row:last-child { margin-bottom: 0; }
#type-panel .tp-lab { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
#type-panel .tp-grp { display: flex; gap: .3rem; }
#type-panel .tp-grp button { font-family: var(--sans); font-size: .8rem; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-radius: 8px; padding: .3rem .55rem; cursor: pointer; min-width: 2rem; }
#type-panel .tp-grp button:hover { border-color: var(--accent); color: var(--accent); }
#type-panel .tp-grp button.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
#content h1 { font-family: var(--display); font-size: 2rem; line-height: 1.22; margin-top: 0; }
#content h1::after { content: ""; display: block; height: 4px; width: 110px; margin-top: .55rem;
  border-radius: 2px; background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
#content h2 { font-family: var(--display); font-size: 1.38rem; margin-top: 2.4rem; line-height: 1.3; }
#content h3 { font-size: 1.1rem; }
#content a { color: var(--accent); }
#content blockquote { margin: 1.4rem 0; padding: .7rem 1.3rem; border-left: 3px solid var(--accent);
  background: var(--panel); border-radius: 0 14px 14px 0; color: var(--muted); font-style: italic; }
#content code { background: var(--panel); padding: .1em .35em; border-radius: 4px; font-size: .88em; }
#content pre { background: var(--panel); padding: 1rem; border-radius: 10px; overflow-x: auto; }
#content pre code { background: none; padding: 0; }
#content table { border-collapse: collapse; font-family: var(--sans); font-size: .85rem; width: 100%; margin: 1.2rem 0; }
#content th, #content td { border: 1px solid var(--border); padding: .45rem .6rem; text-align: left; vertical-align: top; }
#content th { background: var(--panel); }
#content img { max-width: 100%; }
#content hr { border: none; margin: 2.6rem 0; text-align: center; height: 1em; }
#content hr::before { content: var(--hr-glyph, "◆ ◆ ◆"); color: var(--accent); opacity: .45; font-size: .7rem; letter-spacing: 1.1em;
  padding-left: 1.1em; }
figure.corpus-fig { margin: 2.2rem 0; padding: 1.1rem 1.1rem .9rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--fig-radius, 16px); }
figure.corpus-fig svg { width: 100%; height: auto; display: block; }
figure.corpus-fig figcaption { font-family: var(--sans); font-size: .78rem; color: var(--muted);
  text-align: center; margin-top: .7rem; line-height: 1.45; }
figure.corpus-fig figcaption strong { color: var(--accent); }
.corpus-fig svg text { font-family: var(--sans); fill: var(--text); }
.corpus-fig svg .t-muted { fill: var(--muted); }
.corpus-fig svg .t-acc { fill: var(--accent); }
.corpus-fig svg .t-serif { font-family: var(--display); }
.corpus-fig svg .t-mono { font-family: var(--mono, ui-monospace, 'SF Mono', Menlo, Consolas, monospace); }
.corpus-fig svg .t-inv { fill: #faf8f4; }
.corpus-fig svg .ln { stroke: var(--muted); }
.corpus-fig svg .ln-soft { stroke: var(--border); }
.corpus-fig svg .f-t1 { fill: var(--t1); } .corpus-fig svg .f-t2 { fill: var(--t2); }
.corpus-fig svg .f-t3 { fill: var(--t3); } .corpus-fig svg .f-t4 { fill: var(--t4); }
.corpus-fig svg .f-t5 { fill: var(--t5); }
.corpus-fig svg .s-t1 { stroke: var(--t1); } .corpus-fig svg .s-t2 { stroke: var(--t2); }
.corpus-fig svg .s-t3 { stroke: var(--t3); } .corpus-fig svg .s-t4 { stroke: var(--t4); }
.corpus-fig svg .s-t5 { stroke: var(--t5); }
.corpus-fig svg .f-bg { fill: var(--bg); }
.corpus-fig svg .f-panel { fill: var(--panel); }
/* viz chart fences (build-time SVG/HTML), themed with the same palette tokens
   as corpus-fig so they recolor in light & dark and per corpus. */
figure.viz { margin: 2.2rem 0; padding: 1.1rem 1.2rem .95rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--fig-radius, 16px); }
figure.viz .viz-title { font-family: var(--sans); font-size: .95rem; font-weight: 700;
  color: var(--text); margin: 0 0 .9rem; line-height: 1.3; }
figure.viz svg { width: 100%; height: auto; display: block; }
figure.viz svg text { font-family: var(--sans); fill: var(--text); }
figure.viz figcaption { font-family: var(--sans); font-size: .78rem; color: var(--muted);
  margin-top: .8rem; line-height: 1.5; }
figure.viz figcaption .credit { font-style: italic; }
.viz svg .t-muted { fill: var(--muted); }
.viz svg .ln { stroke: var(--muted); }
.viz svg .ln-soft { stroke: var(--border); }
.viz svg .f-t1 { fill: var(--t1); } .viz svg .f-t2 { fill: var(--t2); }
.viz svg .f-t3 { fill: var(--t3); } .viz svg .f-t4 { fill: var(--t4); }
.viz svg .f-t5 { fill: var(--t5); }
.viz svg .s-t1 { stroke: var(--t1); } .viz svg .s-t2 { stroke: var(--t2); }
.viz svg .s-t3 { stroke: var(--t3); } .viz svg .s-t4 { stroke: var(--t4); }
.viz svg .s-t5 { stroke: var(--t5); }
.viz-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 12px;
  font-family: var(--sans); font-size: .8rem; color: var(--text); }
.viz-legend span { display: inline-flex; align-items: center; gap: 7px; }
.viz-legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.viz-legend b { color: var(--muted); font-weight: 600; }
.viz-donutwrap { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
figure.viz .viz-donutwrap svg { width: 200px; flex: 0 0 auto; }
.viz-legend-col { flex-direction: column; gap: 8px; margin: 0; }
.viz-timeline { font-family: var(--sans); font-size: .9rem; }
.viz-timeline .tlrow { display: grid; grid-template-columns: 104px 1fr; gap: 0 18px; }
.viz-timeline .tldate { text-align: right; font-weight: 700; color: var(--accent);
  font-size: .82rem; padding: 2px 0 18px; }
.viz-timeline .tlbody { position: relative; border-left: 2px solid var(--border);
  padding: 0 0 18px 18px; line-height: 1.5; }
.viz-timeline .tlbody::before { content: ""; position: absolute; left: -6px; top: 5px;
  width: 10px; height: 10px; border-radius: 50%; background: var(--accent); }
.viz-timeline .tlrow:last-child .tldate, .viz-timeline .tlrow:last-child .tlbody { padding-bottom: 2px; }
.viz-flow { display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  font-family: var(--sans); font-size: .85rem; }
.viz-flow .flowstep { background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 14px; line-height: 1.35; max-width: 230px; }
.viz-flow .flowarrow { color: var(--accent); font-weight: 700; font-size: 18px; }
.viz-error { margin: 1.4rem 0; padding: 14px 18px; border: 1px dashed var(--accent);
  border-radius: 8px; background: var(--panel); font-family: var(--sans); font-size: .8rem; color: var(--text); }
.viz-error pre { margin: 10px 0 0; font-size: .72rem; white-space: pre-wrap; }
#pager { max-width: 740px; width: 100%; margin: 0 auto; padding: 1rem 2rem 3rem;
  display: flex; align-items: center; justify-content: space-between; font-family: var(--sans); }
#pager button { font-size: 1rem; padding: .45rem 1.2rem; border: 1px solid var(--border);
  border-radius: 16px 16px 8px 8px; background: var(--panel); color: var(--text); cursor: pointer; }
#pager button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
#pager button:disabled { opacity: .3; cursor: default; }
#pager-label { font-size: .78rem; color: var(--muted); }
#menu-btn { display: none; position: fixed; top: .7rem; left: .7rem; z-index: 20; font-size: 1.1rem;
  background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 10px;
  padding: .3rem .6rem; cursor: pointer; }
@media (max-width: 860px) {
  #sidebar { transform: translateX(-100%); transition: transform .2s; z-index: 10; width: 300px; }
  body.menu-open #sidebar { transform: none; box-shadow: 0 0 40px rgba(0,0,0,.3); }
  #menu-btn { display: block; }
  #main { margin-left: 0; }
  #content { padding-top: 3.6rem; }
  figure.corpus-fig { margin-left: -.6rem; margin-right: -.6rem; padding: .7rem .6rem .7rem; }
}
/* a deep-linked passage flashes briefly when the reader scrolls to it */
@keyframes passageFlash { 0% { background: var(--mark); } 100% { background: transparent; } }
.passage-flash { animation: passageFlash 2.2s ease; border-radius: 4px; box-decoration-break: clone; }
/* related reading at the foot of the reading column */
#related { max-width: 740px; width: 100%; margin: 0 auto; padding: 0 2rem 3rem; }
.related-h { font-family: var(--display); font-size: 1.08rem; margin: 0 0 .9rem; padding-top: 1.5rem; border-top: 1px solid var(--border); }
.related-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .8rem; }
.related-card { display: block; background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: .8rem .9rem; text-decoration: none; color: var(--text); transition: border-color .15s ease, transform .15s ease; }
.related-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.related-cat { display: block; font-family: var(--sans); font-size: .62rem; text-transform: uppercase;
  letter-spacing: .12em; color: var(--accent); margin: 0 0 .3rem; }
.related-t { display: block; font-family: var(--display); font-size: 1rem; line-height: 1.22; }
/* distraction-free focus mode (desktop only — leaves the mobile drawer alone) */
@media (min-width: 861px) {
  #sidebar { transition: transform .22s ease; }
  #main { transition: margin-left .22s ease; }
  body.focus-mode #sidebar { transform: translateX(-100%); }
  body.focus-mode #main { margin-left: 0; }
  body.focus-mode #content { max-width: 760px; }
}
/* keyboard-shortcuts help (toggled with ?) */
#kbd-help { position: fixed; inset: 0; z-index: 90; background: rgba(20,18,15,.5); display: none;
  align-items: center; justify-content: center; }
#kbd-help.open { display: flex; }
#kbd-help .kh { background: var(--bg); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem 1.4rem; min-width: 250px; }
#kbd-help h3 { font-family: var(--display); margin: 0 0 .8rem; font-size: 1.05rem; }
#kbd-help dl { display: grid; grid-template-columns: auto 1fr; gap: .45rem 1rem; margin: 0; font-family: var(--sans); font-size: .82rem; }
#kbd-help dt { color: var(--accent); font-weight: 600; white-space: nowrap; }
#kbd-help dd { margin: 0; color: var(--muted); }
"""

APP_JS = r"""
const corpus = JSON.parse(document.getElementById('corpus-data').textContent);
const docs = corpus.documents;
const toc = document.getElementById('toc');
const content = document.getElementById('content');
const searchBox = document.getElementById('search');
const results = document.getElementById('search-results');
const key = 'corpus:' + corpus.slug;
let current = 0;

// theme
const themePref = localStorage.getItem('corpus-theme');
if (themePref) document.documentElement.dataset.theme = themePref;
else if (matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.dataset.theme = 'dark';
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

// table of contents
docs.forEach((d, i) => {
  const a = document.createElement('a');
  a.href = '#ch-' + i;
  a.innerHTML = '<span class="num">' + String(i).padStart(2, '0') + '</span>' + d.title;
  a.title = d.summary || '';
  a.onclick = (e) => { e.preventDefault(); show(i); closeMenu(); };
  toc.appendChild(a);
});

let firstShow = true;
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
function show(i, anchorText) {
  if (listening && !autoNext) stopListen();  // manual nav stops narration; auto-advance keeps it
  const apply = () => {
  current = Math.max(0, Math.min(docs.length - 1, i));
  content.innerHTML = marked.parse(docs[current].body);
  // rewrite chapter-to-chapter .md links into in-app navigation
  content.querySelectorAll('a[href$=".md"]').forEach(a => {
    const target = docs.findIndex(d => a.getAttribute('href').endsWith(d.file));
    if (target >= 0) { a.href = '#ch-' + target; a.onclick = (e) => { e.preventDefault(); show(target); }; }
  });
  content.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  toc.querySelectorAll('a').forEach((a, j) => a.classList.toggle('active', j === current));
  document.getElementById('prev').disabled = current === 0;
  document.getElementById('next').disabled = current === docs.length - 1;
  document.getElementById('pager-label').textContent = (current + 1) + ' / ' + docs.length + ' · ' + docs[current].title
    + ' · ' + Math.max(1, Math.round(docs[current].body.split(/\s+/).length / 220)) + ' min';
  history.replaceState(null, '', '#ch-' + current);
  localStorage.setItem(key, current);
  // feed the cross-page library shell: per-corpus read set + global recents
  try {
    const rk = 'read:' + corpus.slug;
    const readArr = JSON.parse(localStorage.getItem(rk) || '[]');
    if (readArr.indexOf(current) < 0) { readArr.push(current); localStorage.setItem(rk, JSON.stringify(readArr)); }
    const rec = (JSON.parse(localStorage.getItem('library-recents') || '[]') || []).filter(r => r.slug !== corpus.slug);
    rec.unshift({ slug: corpus.slug, title: corpus.title, ch: current, chTitle: docs[current].title, ts: Date.now() });
    localStorage.setItem('library-recents', JSON.stringify(rec.slice(0, 20)));
    // quiet reading streak: bump once per calendar day a chapter is opened
    const today = new Date().toISOString().slice(0, 10);
    const st = JSON.parse(localStorage.getItem('reading-streak') || '{}');
    if (st.last !== today) {
      const yest = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
      st.count = (st.last === yest ? (st.count || 0) : 0) + 1;
      st.last = today;
      localStorage.setItem('reading-streak', JSON.stringify(st));
    }
  } catch (e) {}
  if (anchorText) {
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    let node; while ((node = walker.nextNode())) {
      const idx = node.textContent.toLowerCase().indexOf(anchorText.toLowerCase());
      if (idx >= 0) {
        const host = node.parentElement;
        host.scrollIntoView({ block: 'center' });
        if (!reduceMotion) { host.classList.add('passage-flash'); setTimeout(() => host.classList.remove('passage-flash'), 2200); }
        return;
      }
    }
  }
  document.getElementById('main').scrollIntoView();
  window.scrollTo(0, 0);
  };
  // cross-fade chapter swaps where supported (skips first paint + reduced motion)
  if (document.startViewTransition && !reduceMotion && !firstShow) document.startViewTransition(apply);
  else apply();
  firstShow = false;
}

document.getElementById('prev').onclick = () => show(current - 1);
document.getElementById('next').onclick = () => show(current + 1);

let chordKey = null, chordTimer;
document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const back = document.getElementById('cmdk-back');
  if (back && back.classList.contains('open')) return;  // palette owns the keys when open
  const help = document.getElementById('kbd-help');
  if (help && help.classList.contains('open')) { if (e.key === 'Escape' || e.key === '?') toggleHelp(); return; }
  if (chordKey === 'g') { clearTimeout(chordTimer); chordKey = null;
    if (e.key === 'g') { show(0); return; }
    if (e.key === 'e') { show(docs.length - 1); return; } }
  if (e.key === 'g') { chordKey = 'g'; chordTimer = setTimeout(() => { chordKey = null; }, 700); return; }
  if (e.key === 'ArrowLeft' || e.key === 'k') show(current - 1);
  else if (e.key === 'ArrowRight' || e.key === 'j') show(current + 1);
  else if (e.key === 'z' || e.key === 'Z') toggleFocus();
  else if (e.key === '?') toggleHelp();
});

function toggleFocus() {
  const on = document.body.classList.toggle('focus-mode');
  localStorage.setItem('reader-focus', on ? '1' : '0');
}
if (localStorage.getItem('reader-focus') === '1') document.body.classList.add('focus-mode');

function toggleHelp() {
  let h = document.getElementById('kbd-help');
  if (!h) {
    h = document.createElement('div'); h.id = 'kbd-help';
    h.innerHTML = '<div class="kh"><h3>Keyboard</h3><dl>'
      + '<dt>⌘K</dt><dd>command palette</dd>'
      + '<dt>← / k</dt><dd>previous chapter</dd>'
      + '<dt>→ / j</dt><dd>next chapter</dd>'
      + '<dt>g g</dt><dd>first chapter</dd>'
      + '<dt>g e</dt><dd>last chapter</dd>'
      + '<dt>Z</dt><dd>focus mode</dd>'
      + '<dt>?</dt><dd>this help</dd></dl></div>';
    h.addEventListener('click', (ev) => { if (ev.target === h) toggleHelp(); });
    document.body.appendChild(h);
  }
  h.classList.toggle('open');
}

// search
let timer;
searchBox.addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(runSearch, 150);
});
function runSearch() {
  const q = searchBox.value.trim().toLowerCase();
  results.innerHTML = '';
  toc.style.display = q ? 'none' : '';
  if (!q || q.length < 3) { toc.style.display = ''; return; }
  let hits = 0;
  docs.forEach((d, i) => {
    const text = d.body.toLowerCase();
    let pos = text.indexOf(q), shown = 0;
    while (pos >= 0 && shown < 3 && hits < 40) {
      const start = Math.max(0, pos - 60), end = Math.min(d.body.length, pos + q.length + 90);
      const raw = d.body.slice(start, end).replace(/[#*_>\[\]]/g, '');
      const safe = raw.replace(/&/g, '&amp;').replace(/</g, '&lt;');
      const snippet = safe.replace(new RegExp(q.replace(/[.*+?^${}()|\\]/g, '\\$&'), 'ig'), m => '<mark>' + m + '</mark>');
      const div = document.createElement('div');
      div.className = 'hit';
      div.innerHTML = '<b>' + d.title + '</b>…' + snippet + '…';
      const exact = d.body.slice(pos, pos + q.length);
      div.onclick = () => { show(i, exact); searchBox.value = ''; runSearch(); closeMenu(); };
      results.appendChild(div);
      hits++; shown++;
      pos = text.indexOf(q, pos + q.length);
    }
  });
  if (!hits) results.innerHTML = '<div class="none">No matches.</div>';
}

// mobile menu
const menuBtn = document.getElementById('menu-btn');
menuBtn.onclick = () => document.body.classList.toggle('menu-open');
function closeMenu() { document.body.classList.remove('menu-open'); }

// ---- Listen mode (browser Web Speech API) + share-this-chapter ----
const synth = window.speechSynthesis;
let listening = false, paused = false, autoNext = false, listenIdx = 0, listenRate = 1,
    listenAuto = true, sleepMode = 'none', sleepTimer = null, lchunks = [], lidx = 0, lbar = null;
function chapterText(i) {
  let t = docs[i] ? docs[i].body : '';
  return t.replace(/```[\s\S]*?```/g, ' ').replace(/<[^>]+>/g, ' ')
          .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
          .replace(/[#>*_`~|]/g, ' ').replace(/\s+/g, ' ').trim();
}
function chunkText(t) {
  const raw = t.match(/[^.!?]+[.!?]+["')\]”’]*|\S[^.!?]*$/g) || [t];
  const out = []; let buf = '';
  raw.forEach(s => { s = s.trim(); if (!s) return;
    if ((buf + ' ' + s).length > 240 && buf) { out.push(buf); buf = s; } else buf = buf ? buf + ' ' + s : s; });
  if (buf) out.push(buf);
  return out;
}
function pickVoice() { const vs = synth.getVoices();
  return vs.find(v => /^en/i.test(v.lang) && v.localService) || vs.find(v => /^en/i.test(v.lang)) || vs[0]; }
function speakChunk() {
  if (!listening) return;
  if (lidx >= lchunks.length) {
    if (sleepMode === 'chapter') { stopListen(); return; }
    if (listenAuto && listenIdx < docs.length - 1) {
      listenIdx++; autoNext = true; show(listenIdx); autoNext = false;
      lchunks = chunkText(chapterText(listenIdx)); lidx = 0; updateBar(); speakChunk();
    } else stopListen();
    return;
  }
  const u = new SpeechSynthesisUtterance(lchunks[lidx]);
  u.rate = listenRate; const vo = pickVoice(); if (vo) u.voice = vo;
  u.onend = () => { if (listening && !paused) { lidx++; speakChunk(); } };
  u.onerror = () => { if (listening && !paused) { lidx++; speakChunk(); } };
  synth.speak(u);
}
function startListen() {
  if (!synth) return;
  synth.cancel(); listening = true; paused = false; listenIdx = current;
  lchunks = chunkText(chapterText(listenIdx)); lidx = 0; buildBar(); lbar.classList.add('show'); updateBar();
  const lb = document.getElementById('listen-btn'); if (lb) lb.classList.add('on');
  speakChunk();
}
function stopListen() {
  listening = false; paused = false; if (synth) synth.cancel();
  if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; }
  if (lbar) lbar.classList.remove('show');
  const lb = document.getElementById('listen-btn'); if (lb) lb.classList.remove('on');
}
function togglePause() { if (!listening) return;
  if (paused) { synth.resume(); paused = false; } else { synth.pause(); paused = true; } updateBar(); }
function setSleep(sec) {
  if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; }
  sleepMode = sec === 1 ? 'chapter' : 'none';
  if (sec > 1) sleepTimer = setTimeout(stopListen, sec * 1000);
}
function buildBar() {
  if (lbar) return;
  lbar = document.createElement('div'); lbar.id = 'listen-bar';
  lbar.innerHTML = '<button id="lb-toggle" title="Pause">⏸</button><button id="lb-stop" title="Stop">⏹</button>'
    + '<span id="lb-title"></span>'
    + '<label class="lb-ctl">Speed <select id="lb-rate"><option value="0.8">0.8×</option><option value="1" selected>1×</option><option value="1.2">1.2×</option><option value="1.5">1.5×</option></select></label>'
    + '<label class="lb-ctl">Sleep <select id="lb-sleep"><option value="0" selected>Off</option><option value="1">End of chapter</option><option value="900">15 min</option><option value="1800">30 min</option></select></label>';
  document.body.appendChild(lbar);
  lbar.querySelector('#lb-toggle').onclick = togglePause;
  lbar.querySelector('#lb-stop').onclick = stopListen;
  lbar.querySelector('#lb-rate').onchange = function () { listenRate = +this.value; };
  lbar.querySelector('#lb-sleep').onchange = function () { setSleep(+this.value); };
}
function updateBar() {
  if (!lbar) return;
  const t = lbar.querySelector('#lb-title'); if (t) t.textContent = docs[listenIdx] ? docs[listenIdx].title : '';
  const tg = lbar.querySelector('#lb-toggle'); if (tg) tg.textContent = paused ? '▶' : '⏸';
}
const listenBtn = document.getElementById('listen-btn');
if (listenBtn) {
  if (!synth) listenBtn.style.display = 'none';
  else listenBtn.onclick = () => { listening ? stopListen() : startListen(); };
}
const shareBtn = document.getElementById('share-btn');
if (shareBtn) shareBtn.onclick = () => {
  if (!window.CorpusShare) return;
  window.CorpusShare.open({ kicker: corpus.title, title: docs[current].title,
    source: corpus.subtitle || corpus.title,
    url: location.origin + location.pathname + '#ch-' + current, filename: 'chapter' });
};
window.addEventListener('pagehide', () => { if (synth) synth.cancel(); });

// initial chapter: hash > saved progress > 0; ?q= deep-links to a passage (Today's Passage, share cards)
const hash = location.hash.match(/^#ch-(\d+)$/);
const anchorQ = new URLSearchParams(location.search).get('q');
show(hash ? +hash[1] : +(localStorage.getItem(key) || 0), anchorQ || undefined);

// respond to in-page hash changes: browser back/forward, and cross-page command
// palette jumps that land on a chapter of the corpus already open (no reload).
window.addEventListener('hashchange', () => {
  const h = location.hash.match(/^#ch-(\d+)$/);
  if (h && +h[1] !== current) show(+h[1]);
});

// Related reading at the foot of the column — corpus-level, from the inlined
// library manifest's precomputed similarity graph (no fetch). Deferred to
// DOMContentLoaded because the shell (which carries #library-manifest) is
// injected into the page AFTER this script.
function renderRelated() {
  const relEl = document.getElementById('related');
  const mEl = document.getElementById('library-manifest');
  if (!relEl || !mEl) return;
  let LIB; try { LIB = JSON.parse(mEl.textContent); } catch (e) { return; }
  const me = LIB.find(x => x.slug === corpus.slug);
  const rel = (me && me.related) || [];
  if (!rel.length) return;
  const esc = s => { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
  relEl.innerHTML = '<h3 class="related-h">Related reading</h3><div class="related-grid">'
    + rel.map(r => '<a class="related-card" href="' + esc(r.slug) + '.html">'
        + '<span class="related-cat">' + esc(r.category || '') + '</span>'
        + '<span class="related-t">' + esc(r.title) + '</span></a>').join('')
    + '</div>';
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderRelated);
else renderRelated();

// top reading-progress bar
(function () {
  var bar = document.getElementById('reader-progress'); if (!bar) return;
  function onScroll() { var h = document.documentElement, max = h.scrollHeight - h.clientHeight;
    bar.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + '%'; }
  document.addEventListener('scroll', onScroll, { passive: true }); onScroll();
})();

// reader type controls — text size + measure, persisted across the whole library
(function () {
  var contentEl = document.getElementById('content'), btn = document.getElementById('type-btn');
  if (!contentEl || !btn) return;
  var SIZES = { s: '0.95rem', m: '1.04rem', l: '1.16rem', xl: '1.28rem' };
  var MEAS = { narrow: '640px', normal: '740px', wide: '880px' };
  var fs = SIZES[localStorage.getItem('reader-fs')] ? localStorage.getItem('reader-fs') : 'm';
  var meas = MEAS[localStorage.getItem('reader-measure')] ? localStorage.getItem('reader-measure') : 'normal';
  function applyType() {
    contentEl.style.setProperty('--reader-fs', SIZES[fs]);
    contentEl.style.setProperty('--reader-measure', MEAS[meas]);
    var pg = document.getElementById('pager'); if (pg) pg.style.maxWidth = MEAS[meas];
    var rl = document.getElementById('related'); if (rl) rl.style.maxWidth = MEAS[meas];
  }
  applyType();
  var panel;
  function mark() {
    if (!panel) return;
    [].forEach.call(panel.querySelectorAll('#tp-size button'), function (b) { b.classList.toggle('on', b.getAttribute('data-v') === fs); });
    [].forEach.call(panel.querySelectorAll('#tp-meas button'), function (b) { b.classList.toggle('on', b.getAttribute('data-v') === meas); });
  }
  function build() {
    panel = document.createElement('div'); panel.id = 'type-panel';
    panel.innerHTML = '<div class="tp-row"><span class="tp-lab">Text size</span><span class="tp-grp" id="tp-size">'
      + '<button data-v="s" aria-label="Smaller">A−</button><button data-v="m">A</button>'
      + '<button data-v="l">A+</button><button data-v="xl" aria-label="Largest">A++</button></span></div>'
      + '<div class="tp-row"><span class="tp-lab">Width</span><span class="tp-grp" id="tp-meas">'
      + '<button data-v="narrow">Narrow</button><button data-v="normal">Normal</button><button data-v="wide">Wide</button></span></div>';
    document.body.appendChild(panel);
    panel.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var v = b.getAttribute('data-v');
      if (b.parentNode.id === 'tp-size') { fs = v; localStorage.setItem('reader-fs', v); }
      else { meas = v; localStorage.setItem('reader-measure', v); }
      applyType(); mark();
    });
  }
  btn.onclick = function (e) {
    e.stopPropagation();
    if (!panel) build();
    if (panel.classList.toggle('open')) {
      mark();
      var r = btn.getBoundingClientRect();
      panel.style.left = Math.min(r.left, window.innerWidth - panel.offsetWidth - 8) + 'px';
      var top = r.top - panel.offsetHeight - 8;
      panel.style.top = (top < 8 ? r.bottom + 8 : top) + 'px';
    }
  };
  document.addEventListener('click', function (e) {
    if (panel && panel.classList.contains('open') && !panel.contains(e.target) && e.target !== btn) panel.classList.remove('open');
  });
})();
"""

# ---------------------------------------------------------------- the connective shell
# A thin cross-page layer injected into EVERY page (library, readers, Ghost and
# Fingerprint section fronts + editions). It carries two things:
#   1. a baked `library-manifest` (every corpus + its chapter titles, plus the
#      Ghost/Fingerprint sections and their editions) — inlined, so it works
#      fully offline, no fetch;
#   2. SHELL_JS, which turns that manifest into a global ⌘/Ctrl-K command palette
#      (fuzzy-jump to any corpus, chapter, or edition from anywhere) with a
#      continue-reading list, and — on the library index only — promotes each
#      corpus's saved reading progress into a card progress ring + "completed"
#      trencadís seal, plus a Resume-reading band.
# Navigation hrefs in the manifest are stored relative to docs/ root; SHELL_BASE
# ('' for root pages, '../' for edition pages in docs/<section>/) prefixes them.

SHELL_CSS = """
@view-transition { navigation: auto; }
@media (prefers-reduced-motion: reduce) {
  ::view-transition-group(*), ::view-transition-old(*), ::view-transition-new(*) { animation: none !important; }
}
#cmdk-fab { position: fixed; left: .9rem; bottom: .9rem; z-index: 60; display: inline-flex; align-items: center; gap: .45rem;
  font-family: var(--sans); font-size: .72rem; letter-spacing: .03em; color: var(--muted);
  background: var(--panel); border: 1px solid var(--border); border-radius: 11px; padding: .4rem .7rem; cursor: pointer; }
#cmdk-fab:hover { color: var(--accent); border-color: var(--accent); }
#cmdk-fab kbd { font-family: var(--sans); font-size: .7rem; background: var(--bg); border: 1px solid var(--border);
  border-radius: 5px; padding: .02rem .32rem; color: inherit; }
#cmdk-back { position: fixed; inset: 0; z-index: 80; background: rgba(20,18,15,.42); -webkit-backdrop-filter: blur(3px);
  backdrop-filter: blur(3px); display: none; align-items: flex-start; justify-content: center; padding: 12vh 1rem 1rem; }
#cmdk-back.open { display: flex; }
#cmdk { width: 100%; max-width: 560px; background: var(--bg); border: 1px solid var(--border); border-radius: 16px; overflow: hidden;
  box-shadow: 0 30px 80px rgba(0,0,0,.34); }
#cmdk-in { display: flex; align-items: center; gap: .6rem; padding: .85rem 1rem; border-bottom: 1px solid var(--border); }
#cmdk-in svg { width: 18px; height: 18px; flex: none; color: var(--muted); }
#cmdk-in input { flex: 1; border: none; background: none; outline: none; color: var(--text); font-family: var(--serif); font-size: 1.05rem; }
#cmdk-in .hint { font-family: var(--sans); font-size: .6rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted);
  border: 1px solid var(--border); border-radius: 5px; padding: .12rem .4rem; }
#cmdk-res { max-height: 52vh; overflow-y: auto; padding: .4rem; }
.cmdk-grp { font-family: var(--sans); font-size: .6rem; text-transform: uppercase; letter-spacing: .16em; color: var(--muted); padding: .6rem .6rem .25rem; }
.cmdk-row { display: flex; align-items: center; gap: .7rem; padding: .5rem .6rem; border-radius: 9px; cursor: pointer;
  text-decoration: none; color: var(--text); }
.cmdk-row.sel { background: var(--panel); }
.cmdk-ic { width: 1.3rem; text-align: center; color: var(--accent); flex: none; font-family: var(--sans); font-size: .9rem; }
.cmdk-t { font-family: var(--display); font-size: .98rem; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cmdk-t mark { background: var(--mark, #f3dfa0); color: inherit; border-radius: 2px; padding: 0 1px; }
.cmdk-m { font-family: var(--sans); font-size: .68rem; color: var(--muted); white-space: nowrap; letter-spacing: .02em; }
.cmdk-none { font-family: var(--sans); font-size: .85rem; color: var(--muted); text-align: center; padding: 1.4rem .6rem; }
/* reading progress lives on the meta line (bottom-right of the card body), never over the cover image */
.meta.has-prog { display: flex; align-items: center; justify-content: space-between; gap: .6rem; }
.card-prog { display: inline-flex; align-items: center; gap: .34rem; font-family: var(--sans); font-size: .68rem;
  letter-spacing: .02em; text-transform: none; color: var(--muted); white-space: nowrap; }
.card-prog svg { width: 14px; height: 14px; flex: none; }
.card-prog .pbg { stroke: var(--border); }
.card-prog.done { color: var(--accent); }
#resume { display: none; }
#resume.on { display: block; max-width: 1080px; margin: 1.2rem auto 0; padding: 0 2rem; }
#resume a { display: flex; align-items: center; gap: 1rem; text-decoration: none; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 0 14px 14px 0; padding: 1rem 1.3rem; transition: border-color .15s ease; }
#resume a:hover { border-color: var(--accent); }
#resume .rcol { min-width: 0; }
#resume .rk { display: block; font-family: var(--sans); font-size: .62rem; text-transform: uppercase; letter-spacing: .16em; color: var(--accent); margin: 0 0 .25rem; }
#resume .rt { display: block; font-family: var(--display); font-size: 1.12rem; line-height: 1.2; }
#resume .rs { display: block; font-family: var(--sans); font-size: .76rem; color: var(--muted); margin: .2rem 0 0; }
#resume .rcta { margin-left: auto; font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--bg); background: var(--accent); border-radius: 11px; padding: .5rem .9rem; white-space: nowrap; }
@media (max-width: 560px) { #cmdk-fab { font-size: .66rem; } .cmdk-m { display: none; } }
/* shareable-card preview modal */
#share-back { position: fixed; inset: 0; z-index: 85; background: rgba(20,18,15,.5); display: none; align-items: center; justify-content: center; padding: 1rem; }
#share-back.open { display: flex; }
#share-box { background: var(--bg); border: 1px solid var(--border); border-radius: 16px; padding: 1rem; max-width: 380px; width: 100%; }
#share-img { width: 100%; border-radius: 10px; display: block; border: 1px solid var(--border); }
#share-actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .8rem; }
#share-actions button { font-family: var(--sans); font-size: .8rem; padding: .5rem .8rem; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); color: var(--text); cursor: pointer; }
#share-actions button:hover { border-color: var(--accent); color: var(--accent); }
#share-actions #share-dl { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.share-toast { position: fixed; bottom: 1.3rem; left: 50%; transform: translateX(-50%); background: var(--accent); color: var(--bg); font-family: var(--sans); font-size: .8rem; padding: .5rem .9rem; border-radius: 10px; z-index: 95; }
"""

SHELL_JS = r"""
(function () {
  var base = window.SHELL_BASE || '';
  var mEl = document.getElementById('library-manifest');
  var LIB = mEl ? JSON.parse(mEl.textContent) : [];

  var ENTRIES = [];
  // searchable keyword surface — folds in the slug so evocative display titles
  // ("A God in the Psyche") still match the obvious term ("jung", "ipv6").
  function kw() { return Array.prototype.join.call(arguments, ' ').toLowerCase(); }
  LIB.forEach(function (it) {
    var slugWords = (it.slug || '').replace(/-/g, ' ').replace(/ research$/, '');
    if (it.kind === 'corpus') {
      ENTRIES.push({ t: it.title, m: it.category || 'Corpus', grp: 'Corpora', icon: '◆', href: it.href, k: kw(it.title, slugWords, it.category) });
      (it.chapters || []).forEach(function (ch, i) {
        ENTRIES.push({ t: ch, m: it.title, grp: 'Chapters', icon: '·', href: it.href + '#ch-' + i, k: kw(ch, it.title, slugWords) });
      });
    } else if (it.kind === 'collection') {
      ENTRIES.push({ t: it.title, m: 'Collection · ' + (it.meta || ''), grp: 'Collections', icon: '❖', href: it.href, k: kw(it.title, it.meta) });
    } else if (it.kind === 'section') {
      ENTRIES.push({ t: it.title, m: it.meta || 'Section', grp: 'Sections', icon: '§', href: it.href, k: kw(it.title, it.meta) });
    } else if (it.kind === 'edition') {
      ENTRIES.push({ t: it.title, m: (it.category || '') + (it.meta ? ' · ' + it.meta : ''), grp: 'Editions', icon: '▤', href: it.href, k: kw(it.title, it.category, it.meta) });
    }
  });
  var GRP_ORDER = ['Continue', 'Collections', 'Corpora', 'Sections', 'Chapters', 'In the text', 'Editions'];

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function fuzzy(q, s) { q = q.toLowerCase(); s = s.toLowerCase(); var i = 0, j = 0; while (i < q.length && j < s.length) { if (q[i] === s[j]) i++; j++; } return i === q.length; }
  function score(q, e) {
    q = q.toLowerCase();
    var t = e.t.toLowerCase(), p = t.indexOf(q);
    if (p === 0) return 0;
    if (p > 0) return 1;
    if ((e.k || '').indexOf(q) >= 0) return 2;
    if (fuzzy(q, e.k || t)) return 5;  // loose subsequence — fallback rank only
    return 9;
  }
  function hl(t, q) {
    if (!q) return esc(t);
    var i = t.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return esc(t);
    return esc(t.slice(0, i)) + '<mark>' + esc(t.slice(i, i + q.length)) + '</mark>' + esc(t.slice(i + q.length));
  }
  function readRecents() {
    try { return JSON.parse(localStorage.getItem('library-recents') || '[]') || []; } catch (e) { return []; }
  }
  function recentEntries() {
    return readRecents().slice(0, 4).map(function (r) {
      return { t: r.title, m: 'Ch ' + (r.ch + 1) + (r.chTitle ? ' · ' + r.chTitle : ''), grp: 'Continue', icon: '▸', href: r.slug + '.html#ch-' + r.ch };
    });
  }

  // global "in the text" search — search-index.json is fetched lazily on the
  // first body-length query, so it never blocks first paint (and degrades to
  // title search if the fetch fails, e.g. opened from file://).
  var SI = null, siState = 'idle';
  function loadSI() {
    if (siState !== 'idle') return;
    siState = 'loading';
    fetch(base + 'search-index.json').then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (j) { SI = j; siState = 'ready'; if (isOpen() && input && input.value.trim().length >= 2) render(); })
      .catch(function () { siState = 'failed'; });
  }
  function bodyMatches(q) {
    if (siState !== 'ready' || !SI || q.length < 2) return [];
    var out = [];
    for (var c = 0; c < SI.length && out.length < 16; c++) {
      var corp = SI[c], chs = corp.chapters || [];
      for (var i = 0; i < chs.length && out.length < 16; i++) {
        var text = chs[i].text || '', pos = text.toLowerCase().indexOf(q);
        if (pos < 0) continue;
        var s = Math.max(0, pos - 32), e = Math.min(text.length, pos + q.length + 54);
        var snip = (s > 0 ? '…' : '') + text.slice(s, e).trim() + (e < text.length ? '…' : '');
        out.push({ t: snip, m: corp.title + ' · ' + (chs[i].title || ('Ch ' + (i + 1))), grp: 'In the text',
                   icon: '¶', href: corp.slug + '.html#ch-' + chs[i].i });
      }
    }
    return out;
  }

  var back, input, resEl, rows = [], sel = 0, built = false;
  function build() {
    if (built) return;
    back = document.createElement('div'); back.id = 'cmdk-back';
    back.innerHTML = '<div id="cmdk" role="dialog" aria-label="Command palette">'
      + '<div id="cmdk-in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.65" y2="16.65"/></svg>'
      + '<input id="cmdk-q" placeholder="Jump to any corpus, chapter, or edition…" autocomplete="off" spellcheck="false" aria-label="Search the library">'
      + '<span class="hint">esc</span></div><div id="cmdk-res"></div></div>';
    document.body.appendChild(back);
    input = back.querySelector('#cmdk-q'); resEl = back.querySelector('#cmdk-res');
    input.addEventListener('input', render);
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    built = true;
  }
  function results(q) {
    if (!q) return recentEntries().concat(ENTRIES.filter(function (e) { return e.grp === 'Collections' || e.grp === 'Corpora' || e.grp === 'Sections'; }));
    var ql = q.toLowerCase();
    if (ql.length >= 3) loadSI();
    var subs = [], fuz = [];
    ENTRIES.forEach(function (e) { var s = score(ql, e); if (s < 5) subs.push({ e: e, s: s }); else if (s === 5) fuz.push(e); });
    var head;
    if (subs.length) { subs.sort(function (a, b) { return a.s - b.s; }); head = subs.slice(0, 10).map(function (x) { return x.e; }); }
    else head = fuz.slice(0, 8);
    return head.concat(bodyMatches(ql));  // title/chapter hits first, then in-the-text hits
  }
  function render() {
    var q = input.value.trim();
    var list = results(q);
    if (!list.length) {
      rows = [];
      resEl.innerHTML = (siState === 'loading')
        ? '<div class="cmdk-none">Searching inside the chapters…</div>'
        : '<div class="cmdk-none">No matches. Try a title, a chapter, or “ghost”.</div>';
      return;
    }
    var ordered = [];
    GRP_ORDER.forEach(function (g) { list.forEach(function (e) { if (e.grp === g) ordered.push(e); }); });
    list.forEach(function (e) { if (GRP_ORDER.indexOf(e.grp) < 0) ordered.push(e); });
    rows = ordered; sel = 0;
    var html = '', lastG = null;
    ordered.forEach(function (e, i) {
      if (e.grp !== lastG) { html += '<div class="cmdk-grp">' + esc(e.grp) + '</div>'; lastG = e.grp; }
      html += '<a class="cmdk-row' + (i === 0 ? ' sel' : '') + '" data-i="' + i + '" href="' + esc(base + e.href) + '">'
        + '<span class="cmdk-ic">' + e.icon + '</span><span class="cmdk-t">' + hl(e.t, q) + '</span><span class="cmdk-m">' + esc(e.m) + '</span></a>';
    });
    resEl.innerHTML = html;
    [].forEach.call(resEl.querySelectorAll('.cmdk-row'), function (el) {
      el.addEventListener('mouseenter', function () { sel = +el.getAttribute('data-i'); paint(); });
      el.addEventListener('click', function (ev) { ev.preventDefault(); go(rows[+el.getAttribute('data-i')]); });
    });
  }
  function paint() {
    [].forEach.call(resEl.querySelectorAll('.cmdk-row'), function (el, i) {
      var on = i === sel; el.classList.toggle('sel', on); if (on) el.scrollIntoView({ block: 'nearest' });
    });
  }
  function go(e) { if (!e) return; close(); window.location.href = base + e.href; }
  function open() { build(); back.classList.add('open'); input.value = ''; render(); setTimeout(function () { input.focus(); }, 10); }
  function close() { if (back) back.classList.remove('open'); }
  function isOpen() { return back && back.classList.contains('open'); }

  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); isOpen() ? close() : open(); return; }
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(rows.length - 1, sel + 1); paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); paint(); }
    else if (e.key === 'Enter') { e.preventDefault(); go(rows[sel]); }
  });

  var mac = /Mac|iPhone|iPad/.test(navigator.platform);
  var fab = document.createElement('button'); fab.id = 'cmdk-fab'; fab.type = 'button';
  fab.setAttribute('aria-label', 'Search the library');
  fab.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.65" y2="16.65"/></svg> <kbd>' + (mac ? '⌘' : 'Ctrl') + 'K</kbd>';
  fab.addEventListener('click', open);
  document.body.appendChild(fab);

  [].forEach.call(document.querySelectorAll('.card[data-slug], .coll-card[data-slug]'), function (card) {
    var slug = card.getAttribute('data-slug');
    var total = +card.getAttribute('data-total') || 0;
    var accent = card.getAttribute('data-accent') || '--accent';
    var meta = card.querySelector('.meta, .coll-meta');
    if (!meta || !total) return;
    var read = 0;
    try { read = (JSON.parse(localStorage.getItem('read:' + slug) || '[]') || []).filter(function (x) { return x < total; }).length; } catch (e) {}
    if (!read) return;  // keep the meta line clean until there's progress
    var done = read >= total;
    var prog = document.createElement('span');
    prog.className = 'card-prog' + (done ? ' done' : '');
    if (done) {
      card.classList.add('is-complete');
      prog.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="var(' + accent + ')" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="13 4 6.5 12 3 8.5"/></svg>Read';
    } else {
      var r = 6, cir = 2 * Math.PI * r, off = cir * (1 - read / total);
      prog.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle class="pbg" cx="8" cy="8" r="' + r + '" fill="none" stroke-width="2.4"/>'
        + '<circle cx="8" cy="8" r="' + r + '" fill="none" stroke="var(' + accent + ')" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="' + cir.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '" transform="rotate(-90 8 8)"/></svg>' + read + '/' + total;
    }
    meta.classList.add('has-prog');
    meta.appendChild(prog);
  });

  var resume = document.getElementById('resume');
  if (resume) {
    var rec = readRecents();
    if (rec.length) {
      var r0 = rec[0];
      resume.innerHTML = '<a href="' + esc(base + r0.slug + '.html#ch-' + r0.ch) + '">'
        + '<span class="rcol"><span class="rk">Resume reading</span>'
        + '<span class="rt">' + esc(r0.title) + '</span>'
        + '<span class="rs">Chapter ' + (r0.ch + 1) + (r0.chTitle ? ' — ' + esc(r0.chTitle) : '') + '</span></span>'
        + '<span class="rcta">Continue →</span></a>';
      resume.classList.add('on');
    }
  }

  // quiet reading streak — a small folio stat folded into the library stats line
  try {
    var st = JSON.parse(localStorage.getItem('reading-streak') || '{}');
    var stats = document.querySelector('.stats');
    if (stats && st && (st.count || 0) >= 2 && !/streak/.test(stats.textContent)) {
      stats.textContent = stats.textContent + ' · ' + st.count + '-day reading streak';
    }
  } catch (e) {}
})();
"""


# window.CorpusShare — generate a shareable PNG "card" entirely client-side from
# the live theme tokens (so it matches light/dark + any per-corpus theme), with a
# preview modal offering Download / Copy link / native Share. Draws only vector +
# system-font text, so the canvas never taints and toBlob/toDataURL stay allowed.
SHARE_JS = r"""
(function () {
  function v(name, fb) { var x = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); return x || fb; }
  function rr(x, a, b, w, h, r) { x.beginPath(); x.moveTo(a + r, b); x.arcTo(a + w, b, a + w, b + h, r);
    x.arcTo(a + w, b + h, a, b + h, r); x.arcTo(a, b + h, a, b, r); x.arcTo(a, b, a + w, b, r); x.closePath(); }
  function wrap(ctx, text, maxW) {
    var words = String(text).split(/\s+/), lines = [], line = '';
    for (var i = 0; i < words.length; i++) {
      var t = line ? line + ' ' + words[i] : words[i];
      if (ctx.measureText(t).width > maxW && line) { lines.push(line); line = words[i]; } else line = t;
    }
    if (line) lines.push(line); return lines;
  }
  function draw(o) {
    var S = 2, W = 540 * S, H = 675 * S, pad = 54 * S;
    var c = document.createElement('canvas'); c.width = W; c.height = H;
    var x = c.getContext('2d');
    var bg = v('--bg', '#faf8f4'), text = v('--text', '#1f1d1a'), accent = v('--accent', '#8c4a2f'),
        muted = v('--muted', '#6e6a62'), border = v('--border', '#ddd6c9');
    var tiles = [v('--t1', '#b3502f'), v('--t2', '#c9a227'), v('--t3', '#2e5266'), v('--t4', '#6b7d4f'), v('--t5', '#8a5a7c')];
    var SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif";
    var SERIF = "'Iowan Old Style', Palatino, Georgia, serif";
    x.fillStyle = bg; x.fillRect(0, 0, W, H);
    x.strokeStyle = border; x.lineWidth = 2 * S; x.strokeRect(20 * S, 20 * S, W - 40 * S, H - 40 * S);
    x.textBaseline = 'top';
    x.fillStyle = accent; x.font = (14 * S) + 'px ' + SANS;
    x.fillText((o.kicker || 'research · calvincollins · xyz').toUpperCase(), pad, pad);
    var ty = pad + 32 * S;
    for (var i = 0; i < 5; i++) { x.save(); x.translate(pad + i * (25 * S) + 8 * S, ty + 8 * S);
      x.rotate((i % 2 ? 6 : -6) * Math.PI / 180); x.fillStyle = tiles[i % tiles.length];
      rr(x, -8 * S, -8 * S, 16 * S, 16 * S, 4 * S); x.fill(); x.restore(); }
    var qy = ty + 56 * S, maxW = W - pad * 2, body = o.quote || o.title || '';
    var fs = o.quote ? 38 * S : 46 * S;
    function setf() { x.font = (o.quote ? 'italic ' : '') + fs + 'px ' + SERIF; }
    setf(); var lines = wrap(x, body, maxW), lh = fs * 1.3;
    while (lines.length * lh > H - qy - 150 * S && fs > 22 * S) { fs -= 2 * S; setf(); lines = wrap(x, body, maxW); lh = fs * 1.3; }
    var oy = 0;
    if (o.quote) { x.fillStyle = accent; x.font = (84 * S) + 'px ' + SERIF; x.fillText('“', pad - 4 * S, qy - 24 * S); oy = 42 * S; setf(); }
    x.fillStyle = text; setf();
    for (var j = 0; j < lines.length; j++) x.fillText(lines[j], pad, qy + oy + j * lh);
    var fy = H - pad - 64 * S;
    if (o.source) { x.fillStyle = muted; x.font = (16 * S) + 'px ' + SANS;
      var sl = wrap(x, o.source, maxW); for (var k = 0; k < Math.min(2, sl.length); k++) x.fillText(sl[k], pad, fy + k * 21 * S); }
    x.fillStyle = accent; x.font = (14 * S) + 'px ' + SANS;
    x.fillText('research.calvincollins.xyz', pad, H - pad - 16 * S);
    return c;
  }
  var modal;
  function toast(m) { var t = document.createElement('div'); t.className = 'share-toast'; t.textContent = m;
    document.body.appendChild(t); setTimeout(function () { t.remove(); }, 1600); }
  function open(o) {
    var canvas = draw(o), link = o.url || location.href;
    if (!modal) {
      modal = document.createElement('div'); modal.id = 'share-back';
      modal.innerHTML = '<div id="share-box"><img id="share-img" alt="Shareable card preview">'
        + '<div id="share-actions"><button id="share-dl">Download</button>'
        + '<button id="share-copy">Copy link</button><button id="share-go">Share</button>'
        + '<button id="share-x">Close</button></div></div>';
      document.body.appendChild(modal);
      modal.addEventListener('click', function (e) { if (e.target === modal) modal.classList.remove('open'); });
      document.getElementById('share-x').onclick = function () { modal.classList.remove('open'); };
    }
    document.getElementById('share-img').src = canvas.toDataURL('image/png');
    document.getElementById('share-copy').onclick = function () {
      if (navigator.clipboard) navigator.clipboard.writeText(link).then(function () { toast('Link copied'); }, function () { prompt('Copy link:', link); });
      else prompt('Copy link:', link);
    };
    document.getElementById('share-dl').onclick = function () {
      canvas.toBlob(function (b) { var a = document.createElement('a'); a.href = URL.createObjectURL(b);
        a.download = (o.filename || 'research-card') + '.png'; a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000); });
    };
    var go = document.getElementById('share-go');
    go.style.display = navigator.share ? '' : 'none';
    go.onclick = function () {
      canvas.toBlob(function (b) {
        var file = new File([b], (o.filename || 'research-card') + '.png', { type: 'image/png' });
        if (navigator.canShare && navigator.canShare({ files: [file] })) navigator.share({ files: [file], text: o.shareText || o.title || '', url: link }).catch(function () {});
        else navigator.share({ text: o.shareText || o.title || '', url: link }).catch(function () {});
      });
    };
    modal.classList.add('open');
  }
  window.CorpusShare = { open: open };
})();
"""


# ============================================================================
# The Atlas — a second cross-page surface (sibling to the ⌘K palette): a
# pannable world map where every corpus's cover photo is clipped into the
# region its author or story belongs to. The heavy geometry is baked once by
# scripts/build_atlas_geo.py into atlas/geo.json (projected, simplified SVG
# paths); build-time we only group the corpora onto regions and write a small
# atlas.json that the client renders + makes interactive on first open.
# ============================================================================

ATLAS_TILES = [TERRA, GOLD, BLUE, OLIVE, PLUM]

# Default placement of each corpus onto a region in atlas/geo.json. A corpus is
# pinned where its AUTHOR or STORY belongs (Carlyle → Scotland, Whitman → New
# York, Jesus/John → the Holy Land …). `cell` steers which slice a corpus gets
# when several share one region; `z:"base"` draws a nation underneath the state
# insets that sit on top of it. Overridable wholesale via build.config.json
# "atlas": {"places": {...}, "grow": {...}}.
ATLAS_PLACEMENTS = {
    "carlyle-research":                    {"region": "GB-SCT", "cell": "n", "label": "Ecclefechan & Edinburgh, Scotland"},
    "carlyle-french-revolution-research":  {"region": "GB-SCT", "cell": "c", "label": "Scotland — by the sage of Chelsea"},
    "carlyle-friedrich-research":          {"region": "GB-SCT", "cell": "s", "label": "Scotland & London"},
    "deutsch-good-explanations-research":  {"region": "GB-ENG", "label": "Oxford, England"},
    "ancestry-research":                   {"region": "IE", "label": "Munster, Ireland"},
    "civil-war-religion-whitman-research": {"region": "US-NY", "label": "Brooklyn & New York"},
    "dickinson-research":                  {"region": "US-MA", "label": "Amherst, Massachusetts"},
    "uap-research":                        {"region": "US-NM", "label": "Roswell, New Mexico"},
    "ipv4-ipv6-ctv-research":              {"region": "US-CA", "label": "Silicon Valley, California"},
    "us-geopolitics-research":             {"region": "US", "z": "base", "label": "The United States"},
    "jung-research":                       {"region": "CH", "cell": "e", "label": "Zürich, Switzerland"},
    "piaget-research":                     {"region": "CH", "cell": "w", "label": "Geneva & Neuchâtel, Switzerland"},
    "phenomenology-pragmatism-research":   {"region": "DE", "label": "Freiburg, Germany"},
    "mcluhan-research":                    {"region": "CA-ON", "label": "Toronto, Ontario"},
    "jesus-research":                      {"region": "IL", "cell": "n", "label": "Galilee"},
    "john-the-baptist-research":           {"region": "IL", "cell": "s", "label": "The Jordan & the Judean wilderness"},
}
# When a small region hosts >1 corpus, grow the footprint to a roomier one so no
# slice ends up too small (Calvin's rule: several authors in Massachusetts →
# expand to New England; the two Holy-Land corpora → the wider Levant).
ATLAS_GROW = {"US-MA": "US-NEWENGLAND", "IL": "LEVANT"}


def load_atlas_geo():
    """Read the baked geometry (atlas/geo.json). Returns None if not authored."""
    p = HERE / "atlas" / "geo.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _cell_order(hint, axis):
    """Map a compass/index `cell` hint to a sort position along the split axis."""
    if hint is None:
        return 1.0
    if isinstance(hint, (int, float)):
        return float(hint)
    h = str(hint).strip().lower()
    xmap = {"w": 0, "west": 0, "nw": 0, "sw": 0, "l": 0, "left": 0,
            "c": 1, "center": 1, "centre": 1, "mid": 1,
            "e": 2, "east": 2, "ne": 2, "se": 2, "r": 2, "right": 2}
    ymap = {"n": 0, "north": 0, "nw": 0, "ne": 0, "top": 0,
            "c": 1, "center": 1, "centre": 1, "mid": 1,
            "s": 2, "south": 2, "sw": 2, "se": 2, "bottom": 2}
    return float((xmap if axis == "v" else ymap).get(h, 1))


def compute_atlas(geo, corpora, placements=None, grow=None):
    """Group placed corpora onto regions and lay out clipped image cells.

    `corpora` is {slug: {title, href, accent, img, chapters:[{t,href}]}}. Returns
    the small dict written to atlas.json (viewBox + backdrop + the regions in use
    + per-corpus placement rectangles), or None if nothing places.
    """
    placements = placements or ATLAS_PLACEMENTS
    grow = grow or ATLAS_GROW
    regions = geo.get("regions", {})

    groups = {}
    for slug, pl in placements.items():
        if slug not in corpora:
            continue
        rid = (pl or {}).get("region")
        if not rid or rid not in regions:
            if rid:
                print(f"  ! atlas: region {rid!r} for {slug} not in geo.json", file=sys.stderr)
            continue
        groups.setdefault(rid, []).append((slug, pl))

    used, places = {}, []
    for rid, members in groups.items():
        footprint = rid
        if len(members) > 1 and rid in grow and grow[rid] in regions:
            footprint = grow[rid]
        reg = regions[footprint]
        bx0, by0, bx1, by1 = reg["bbox"]
        bw, bh = bx1 - bx0, by1 - by0
        axis = "v" if bw >= bh else "h"          # split wide regions L→R, tall ones T→B
        members.sort(key=lambda m: _cell_order((m[1] or {}).get("cell"), axis))
        k = len(members)
        used.setdefault(footprint, {"d": reg["d"], "bbox": reg["bbox"], "name": reg["name"], "dividers": []})
        for i, (slug, pl) in enumerate(members):
            if axis == "v":
                cx0, cx1 = bx0 + bw * i / k, bx0 + bw * (i + 1) / k
                cy0, cy1 = by0, by1
                if i:
                    used[footprint]["dividers"].append([round(cx0, 1), round(by0, 1), round(cx0, 1), round(by1, 1)])
            else:
                cy0, cy1 = by0 + bh * i / k, by0 + bh * (i + 1) / k
                cx0, cx1 = bx0, bx1
                if i:
                    used[footprint]["dividers"].append([round(bx0, 1), round(cy0, 1), round(bx1, 1), round(cy0, 1)])
            c = corpora[slug]
            places.append({
                "slug": slug, "title": c["title"], "href": c["href"], "accent": c["accent"],
                "img": c.get("img"), "region": footprint,
                "cell": [round(cx0, 1), round(cy0, 1), round(cx1 - cx0, 1), round(cy1 - cy0, 1)],
                "label": (pl or {}).get("label") or reg["name"],
                "z": 0 if (pl or {}).get("z") == "base" else 1,
                "chapters": c.get("chapters", []),
            })
    if not places:
        return None
    # Base layers first, then larger cells before smaller, so small insets paint
    # last (on top) and win hit-testing where they overlap a national base layer.
    places.sort(key=lambda p: (p["z"], -(p["cell"][2] * p["cell"][3])))
    return {
        "viewBox": geo["viewBox"], "w": geo["w"], "h": geo["h"],
        "graticule": geo.get("graticule", []), "backdrop": geo.get("backdrop", []),
        "regions": used, "places": places,
    }


ATLAS_CSS = """
#atlas-dock { position: fixed; left: .9rem; bottom: .9rem; z-index: 60; display: flex; gap: .5rem; align-items: center; }
#atlas-dock #cmdk-fab { position: static; left: auto; bottom: auto; }
#atlas-fab { display: inline-flex; align-items: center; gap: .4rem; font-family: var(--sans); font-size: .72rem;
  letter-spacing: .03em; color: var(--muted); background: var(--panel); border: 1px solid var(--border);
  border-radius: 11px; padding: .4rem .7rem; cursor: pointer; }
#atlas-fab:hover { color: var(--accent); border-color: var(--accent); }
#atlas-fab svg { flex: none; }
#atlas-back { position: fixed; inset: 0; z-index: 90; background: rgba(20,18,15,.55); -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; padding: 3vh 2vw; }
#atlas-back.open { display: flex; }
#atlas-panel { position: relative; width: 100%; height: 94vh; max-width: 1500px; background: var(--bg);
  border: 1px solid var(--border); border-radius: 16px; overflow: hidden; box-shadow: 0 30px 90px rgba(0,0,0,.45);
  display: flex; flex-direction: column; }
#atlas-bar { display: flex; align-items: baseline; gap: .8rem; padding: .7rem 1rem; border-bottom: 1px solid var(--border); flex: none; }
#atlas-title { font-family: var(--display, var(--serif)); font-size: 1.05rem; color: var(--text); white-space: nowrap; }
#atlas-sub { font-family: var(--sans); font-size: .72rem; color: var(--muted); flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#atlas-tools { display: flex; gap: .35rem; flex: none; }
#atlas-tools button { font-family: var(--sans); font-size: .74rem; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-radius: 8px; padding: .25rem .55rem; cursor: pointer; min-width: 1.9rem; }
#atlas-tools button:hover { border-color: var(--accent); color: var(--accent); }
#atlas-stage { position: relative; flex: 1; overflow: hidden; cursor: grab; background: var(--bg); touch-action: none; }
#atlas-stage.grab { cursor: grabbing; }
#atlas-world { position: absolute; top: 0; left: 0; transform-origin: 0 0; will-change: transform; }
#atlas-svg { display: block; }
#atlas-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-family: var(--sans); font-size: .9rem; color: var(--muted); pointer-events: none; text-align: center; padding: 2rem; }
#atlas-grat path { fill: none; stroke: var(--border); stroke-width: .6; opacity: .45; }
#atlas-bd path { fill: var(--panel); stroke: var(--border); stroke-width: .7; opacity: .7; }
[data-theme="dark"] #atlas-bd path { fill: #26241f; opacity: .85; }
.atlas-img { cursor: pointer; transition: opacity .18s ease; }
.atlas-place.active .atlas-img { opacity: .42; }
.atlas-ring { opacity: 0; transition: opacity .18s ease; pointer-events: none; }
.atlas-place.active .atlas-ring { opacity: 1; }
.atlas-rgn { fill: none; stroke: var(--text); stroke-width: 1.1; opacity: .45; pointer-events: none; }
.atlas-div { stroke: var(--bg); stroke-width: 1.6; opacity: .65; pointer-events: none; }
#atlas-card { position: absolute; left: 0; top: 0; z-index: 5; width: 250px; max-width: 76vw; background: var(--bg);
  border: 1px solid var(--border); border-radius: 12px; padding: .7rem .8rem; box-shadow: 0 14px 40px rgba(0,0,0,.3); display: none; }
#atlas-card.show { display: block; }
#atlas-card .ac-title { display: block; font-family: var(--display, var(--serif)); font-size: 1.02rem; line-height: 1.2;
  text-decoration: none; margin-bottom: .15rem; }
#atlas-card .ac-title:hover { text-decoration: underline; }
#atlas-card .ac-place { font-family: var(--sans); font-size: .66rem; text-transform: uppercase; letter-spacing: .12em;
  color: var(--muted); margin-bottom: .55rem; }
#atlas-card .ac-sel { width: 100%; font-family: var(--sans); font-size: .8rem; padding: .42rem .5rem;
  border: 1px solid var(--border); border-radius: 8px; background: var(--panel); color: var(--text); cursor: pointer; }
@media (max-width: 640px) { #atlas-sub { display: none; } #atlas-panel { height: 96vh; } #atlas-fab span { display: none; } }
"""

ATLAS_JS = r"""
(function () {
  var base = window.SHELL_BASE || '';
  var DATA = null, loaded = false, loading = false;
  var back, stage, world, card, msg;
  var view = { s: 1, x: 0, y: 0 };
  var dragging = false, moved = false, lastX = 0, lastY = 0;
  var active = null, hideT = null, bySlug = {};

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  // The Atlas FAB, docked beside the ⌘K button (which SHELL_JS already added).
  var fab = document.createElement('button');
  fab.id = 'atlas-fab'; fab.type = 'button'; fab.setAttribute('aria-label', 'Open the Atlas');
  fab.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c3.2 3.4 3.2 14.6 0 18M12 3c-3.2 3.4-3.2 14.6 0 18"/></svg> <span>Atlas</span>';
  fab.addEventListener('click', open);
  function dock() {
    var c = document.getElementById('cmdk-fab');
    var d = document.createElement('div'); d.id = 'atlas-dock';
    var parent = (c && c.parentNode) || document.body;
    parent.insertBefore(d, c || null);
    if (c) d.appendChild(c);
    d.appendChild(fab);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', dock);
  else dock();

  function open() { buildShell(); back.classList.add('open'); document.body.style.overflow = 'hidden'; if (!loaded) load(); }
  function close() { if (back) { back.classList.remove('open'); document.body.style.overflow = ''; } hideCard(); }

  function buildShell() {
    if (back) return;
    back = document.createElement('div'); back.id = 'atlas-back';
    back.innerHTML =
      '<div id="atlas-panel">'
      + '<div id="atlas-bar"><span id="atlas-title">The Atlas</span>'
      + '<span id="atlas-sub">Every corpus, pinned where its author or story belongs — drag to roam, scroll to zoom.</span>'
      + '<span id="atlas-tools"><button id="atlas-zin" aria-label="Zoom in">+</button>'
      + '<button id="atlas-zout" aria-label="Zoom out">−</button>'
      + '<button id="atlas-fit" aria-label="Reset the view">Fit</button>'
      + '<button id="atlas-x" aria-label="Close the Atlas">Esc</button></span></div>'
      + '<div id="atlas-stage"><div id="atlas-world"></div>'
      + '<div id="atlas-card"></div><div id="atlas-msg">Unrolling the map…</div></div></div>';
    document.body.appendChild(back);
    stage = back.querySelector('#atlas-stage'); world = back.querySelector('#atlas-world');
    card = back.querySelector('#atlas-card'); msg = back.querySelector('#atlas-msg');
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    back.querySelector('#atlas-x').onclick = close;
    back.querySelector('#atlas-zin').onclick = function () { zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1.3); };
    back.querySelector('#atlas-zout').onclick = function () { zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1 / 1.3); };
    back.querySelector('#atlas-fit').onclick = fit;
    stage.addEventListener('pointerdown', function (e) {
      if (e.target.closest('#atlas-card')) return;
      dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
      try { stage.setPointerCapture(e.pointerId); } catch (x) {} stage.classList.add('grab');
    });
    stage.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      var dx = e.clientX - lastX, dy = e.clientY - lastY;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      view.x += dx; view.y += dy; lastX = e.clientX; lastY = e.clientY; apply();
    });
    function end() { if (dragging && moved) save(); dragging = false; stage.classList.remove('grab'); }
    stage.addEventListener('pointerup', end); stage.addEventListener('pointercancel', end);
    stage.addEventListener('wheel', function (e) {
      e.preventDefault(); var r = stage.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });
    stage.addEventListener('dblclick', function (e) {
      if (e.target.closest('#atlas-card')) return;
      var r = stage.getBoundingClientRect(); zoomAt(e.clientX - r.left, e.clientY - r.top, 1.5);
    });
    card.addEventListener('pointerenter', function () { if (hideT) { clearTimeout(hideT); hideT = null; } });
    card.addEventListener('pointerleave', schedHide);
    document.addEventListener('keydown', function (e) {
      if (!back || !back.classList.contains('open')) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === '+' || e.key === '=') zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1.3);
      else if (e.key === '-' || e.key === '_') zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1 / 1.3);
      else if (e.key === '0') fit();
    });
    window.addEventListener('resize', function () {
      if (back && back.classList.contains('open') && DATA && DATA._b && !enoughVisible()) fit();
    });
  }

  function apply() { world.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.s + ')'; }
  // Persist where the reader roamed to, so the Atlas reopens exactly as they left it.
  var SAVE_KEY = 'atlas-view', saveT = null;
  function save() { if (saveT) clearTimeout(saveT); saveT = setTimeout(function () {
    try { localStorage.setItem(SAVE_KEY, JSON.stringify({ s: view.s, x: view.x, y: view.y })); } catch (e) {} }, 180); }
  function restore() {
    try { var v = JSON.parse(localStorage.getItem(SAVE_KEY) || 'null');
      if (v && isFinite(v.s) && isFinite(v.x) && isFinite(v.y) && v.s > 0) { view.s = v.s; view.x = v.x; view.y = v.y; return true; }
    } catch (e) {}
    return false;
  }
  // Is enough of the map on-screen? Guards a saved view that lands off-stage when
  // the Atlas is reopened (or resized) at a very different viewport size.
  function enoughVisible() {
    if (!DATA || !DATA._b) return true;
    var b = DATA._b, sw = stage.clientWidth, sh = stage.clientHeight;
    var L = view.x + b.x * view.s, T = view.y + b.y * view.s,
        Rr = view.x + (b.x + b.w) * view.s, B = view.y + (b.y + b.h) * view.s;
    return view.s > 0 && (Math.min(Rr, sw) - Math.max(L, 0) >= 60) && (Math.min(B, sh) - Math.max(T, 0) >= 60);
  }
  function zoomAt(px, py, f) {
    var ns = Math.max(0.22, Math.min(9, view.s * f)), k = ns / view.s;
    view.x = px - (px - view.x) * k; view.y = py - (py - view.y) * k; view.s = ns; apply(); save();
  }
  function fit() {
    if (!DATA || !DATA._b) return;
    var b = DATA._b, sw = stage.clientWidth, sh = stage.clientHeight, pad = 46;
    var s = Math.max(0.22, Math.min(9, Math.min((sw - pad * 2) / b.w, (sh - pad * 2) / b.h)));
    view.s = s; view.x = (sw - (2 * b.x + b.w) * s) / 2; view.y = (sh - (2 * b.y + b.h) * s) / 2; apply(); save();
  }

  function load() {
    if (loading) return; loading = true;
    fetch(base + 'atlas.json').then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (j) { DATA = j; render(j); loaded = true; if (msg) msg.remove(); })
      .catch(function () { if (msg) msg.textContent = 'The Atlas needs to load its map data — open it on the live site (research.calvincollins.xyz).'; });
  }

  function render(j) {
    var s = '<svg id="atlas-svg" viewBox="' + j.viewBox + '" width="' + j.w + '" height="' + j.h
      + '" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">';
    s += '<defs>';
    for (var rid in j.regions) s += '<clipPath id="ar-' + rid + '"><path d="' + j.regions[rid].d + '"/></clipPath>';
    j.places.forEach(function (p) {
      var c = p.cell;
      s += '<clipPath id="ac-' + p.slug + '"><rect x="' + c[0] + '" y="' + c[1] + '" width="' + c[2] + '" height="' + c[3] + '"/></clipPath>';
    });
    s += '</defs>';
    s += '<g id="atlas-grat">'; (j.graticule || []).forEach(function (d) { s += '<path d="' + d + '"/>'; }); s += '</g>';
    s += '<g id="atlas-bd">'; (j.backdrop || []).forEach(function (d) { s += '<path d="' + d + '"/>'; }); s += '</g>';
    s += '<g id="atlas-places">';
    j.places.forEach(function (p) {
      var c = p.cell;
      s += '<g class="atlas-place" data-slug="' + esc(p.slug) + '"><g clip-path="url(#ar-' + p.region + ')">';
      if (p.img) {
        var href = esc(base + p.img);
        s += '<image class="atlas-img" href="' + href + '" xlink:href="' + href + '" x="' + c[0] + '" y="' + c[1]
          + '" width="' + c[2] + '" height="' + c[3] + '" preserveAspectRatio="xMidYMid slice" clip-path="url(#ac-' + p.slug + ')"/>';
      }
      s += '<rect class="atlas-ring" x="' + c[0] + '" y="' + c[1] + '" width="' + c[2] + '" height="' + c[3]
        + '" clip-path="url(#ac-' + p.slug + ')" fill="none" stroke="' + esc(p.accent) + '" stroke-width="3"/>';
      s += '</g></g>';
    });
    s += '</g>';
    s += '<g id="atlas-lines">';
    for (var r2 in j.regions) {
      var R = j.regions[r2];
      s += '<path class="atlas-rgn" d="' + R.d + '"/>';
      (R.dividers || []).forEach(function (d) {
        s += '<line class="atlas-div" x1="' + d[0] + '" y1="' + d[1] + '" x2="' + d[2] + '" y2="' + d[3] + '" clip-path="url(#ar-' + r2 + ')"/>';
      });
    }
    s += '</g></svg>';
    world.innerHTML = s;

    var X0 = 1e9, Y0 = 1e9, X1 = -1e9, Y1 = -1e9;
    j.places.forEach(function (p) {
      bySlug[p.slug] = p; var c = p.cell;
      X0 = Math.min(X0, c[0]); Y0 = Math.min(Y0, c[1]); X1 = Math.max(X1, c[0] + c[2]); Y1 = Math.max(Y1, c[1] + c[3]);
    });
    DATA._b = { x: X0 - 40, y: Y0 - 40, w: (X1 - X0) + 80, h: (Y1 - Y0) + 80 };

    [].forEach.call(world.querySelectorAll('.atlas-place'), function (el) {
      var p = bySlug[el.getAttribute('data-slug')];
      el.addEventListener('pointerenter', function () { if (!dragging) activate(p, el); });
      el.addEventListener('pointermove', function () { if (!dragging && active !== p) activate(p, el); });
      el.addEventListener('pointerleave', schedHide);
      el.addEventListener('click', function (e) {
        if (moved || e.target.closest('#atlas-card')) return;
        window.location.href = base + p.href;
      });
    });
    if (restore()) { apply(); if (!enoughVisible()) fit(); }   // reopen where they left off — unless it's now off-screen
    else fit();
  }

  function activate(p, el) {
    if (active && active._el && active._el !== el) active._el.classList.remove('active');
    active = p; p._el = el; el.classList.add('active');
    var opts = '<option value="" disabled selected>Jump to a chapter…</option>';
    (p.chapters || []).forEach(function (ch) { opts += '<option value="' + esc(base + ch.href) + '">' + esc(ch.t) + '</option>'; });
    card.innerHTML = '<a class="ac-title" href="' + esc(base + p.href) + '" style="color:' + esc(p.accent) + '">' + esc(p.title) + '</a>'
      + '<div class="ac-place">' + esc(p.label) + '</div>'
      + (p.chapters && p.chapters.length ? '<select class="ac-sel" aria-label="Jump to a chapter">' + opts + '</select>' : '');
    var sel = card.querySelector('.ac-sel');
    if (sel) sel.onchange = function () { if (this.value) window.location.href = this.value; };
    card.classList.add('show');
    var r = el.getBoundingClientRect(), sr = stage.getBoundingClientRect();
    card.style.left = '0px'; card.style.top = '0px';
    var cw = card.offsetWidth, chh = card.offsetHeight;
    var cx = (r.left + r.right) / 2 - sr.left, cyTop = r.top - sr.top;
    var left = Math.max(8, Math.min(sr.width - cw - 8, cx - cw / 2));
    var top = cyTop - chh - 12; if (top < 8) top = (r.bottom - sr.top) + 12;
    top = Math.max(8, Math.min(sr.height - chh - 8, top));
    card.style.left = left + 'px'; card.style.top = top + 'px';
    if (hideT) { clearTimeout(hideT); hideT = null; }
  }
  function schedHide() { if (hideT) clearTimeout(hideT); hideT = setTimeout(hideCard, 240); }
  function hideCard() {
    if (card) card.classList.remove('show');
    if (active && active._el) active._el.classList.remove('active');
    active = null;
  }
})();
"""


def shell_html(manifest_json, base):
    """The cross-page connective shell, injected verbatim into every page template.

    `base` ('' for root pages, '../' for ghost/fingerprint edition pages in
    subdirs) prefixes every navigation href the palette emits. The manifest is
    inlined (not fetched) so the whole thing works offline / from file://.
    """
    return (
        f"<style>{SHELL_CSS}</style>"
        f'<script id="library-manifest" type="application/json">{manifest_json}</script>'
        f"<script>window.SHELL_BASE={json.dumps(base)};</script>"
        f"<script>{SHELL_JS}</script>"
        f"<script>{SHARE_JS}</script>"
        f"<style>{ATLAS_CSS}</style>"
        f"<script>{ATLAS_JS}</script>"
    )


LIBRARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{site_title}</title>
<meta name="description" content="{site_subtitle}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
{overture_head}
</head>
<body>
{overture}
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="ghost.html">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
    <a href="connections.html">Connections</a>
    <a href="wrapped.html">Wrapped</a>
    <a href="#library">The Research</a>
  </nav>
</div>
<header>
  <div class="hero-text">
    <p class="kicker">A library of deep research</p>
    <h1>{site_title}</h1>
    <p class="tagline">{site_subtitle}</p>
    <p class="stats">{stats}</p>
  </div>
  <div class="hero-art">{hero}</div>
</header>
{daily_passage}
{resume}
{foryou}
{ghost_band}
{fingerprint_band}
<h2 class="section-title" id="library">The Research Library</h2>
<main class="library">
{cards}
</main>
{collections}
<footer>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <p class="epigraph">“The medium is the message.” — Marshall McLuhan</p>
  <p class="colophon">Every corpus reads anywhere — no server, no tracking, light or dark.</p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

LIBRARY_THEME_JS = """
const pref = localStorage.getItem('corpus-theme');
if (pref) document.documentElement.dataset.theme = pref;
else if (matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.dataset.theme = 'dark';
"""

# Live category filter + text search for the library index. Category pills and the
# search box combine: a card shows only if it matches the active category AND the
# search term. Empty category sections hide their heading; a message shows if nothing matches.
LIBRARY_FILTER_JS = """
(function () {
  var search = document.getElementById('lib-search');
  var empty = document.getElementById('lib-empty');
  var pills = Array.prototype.slice.call(document.querySelectorAll('.cat-pill'));
  var sections = Array.prototype.slice.call(document.querySelectorAll('.cat-section'));
  if (!search) return;
  var activeCat = 'all';

  function apply() {
    var term = search.value.trim().toLowerCase();
    var anyVisible = false;
    sections.forEach(function (sec) {
      var catOk = activeCat === 'all' || sec.dataset.cat === activeCat;
      var shown = 0;
      sec.querySelectorAll('.card').forEach(function (card) {
        var textOk = !term || (card.dataset.search || '').indexOf(term) !== -1;
        var show = catOk && textOk;
        card.style.display = show ? '' : 'none';
        if (show) shown++;
      });
      sec.hidden = shown === 0;
      if (shown) anyVisible = true;
    });
    if (empty) empty.hidden = anyVisible;
    // The "For You" rows are a browse-what-to-read surface; hide them while a
    // category/search filter is active so they don't contradict the filtered grid.
    var fy = document.getElementById('foryou');
    if (fy && !fy.hidden) fy.style.display = (activeCat !== 'all' || term) ? 'none' : '';
  }

  pills.forEach(function (pill) {
    pill.addEventListener('click', function () {
      activeCat = pill.dataset.cat;
      pills.forEach(function (p) { p.classList.toggle('active', p === pill); });
      apply();
    });
  });
  search.addEventListener('input', apply);
})();
"""

# Today's Passage — a date-seeded pull-quote pinned atop the index, preferring
# corpora the visitor hasn't opened. Reads the inlined #passages-data and links
# to the exact chapter with ?q=<snippet> so the reader scrolls to (and flashes)
# the line. Pure client-side: date math + localStorage history, no fetch.
DAILY_PASSAGE_JS = r"""
(function () {
  var el = document.getElementById('daily-passage'), dataEl = document.getElementById('passages-data');
  if (!el || !dataEl) return;
  var P; try { P = JSON.parse(dataEl.textContent || '[]'); } catch (e) { return; }
  if (!P.length) return;
  var opened = {};
  try { (JSON.parse(localStorage.getItem('library-recents') || '[]') || []).forEach(function (r) { opened[r.slug] = 1; }); } catch (e) {}
  P.forEach(function (p) { try { if ((JSON.parse(localStorage.getItem('read:' + p.slug) || '[]') || []).length) opened[p.slug] = 1; } catch (e) {} });
  var fresh = P.filter(function (p) { return !opened[p.slug]; });
  var pool = fresh.length ? fresh : P;
  var day = Math.floor(Date.now() / 864e5);
  var p = pool[day % pool.length];
  var esc = function (s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  var href = esc(p.slug) + '.html?q=' + encodeURIComponent(p.text.slice(0, 42)) + '#ch-' + p.chapter;
  el.innerHTML = '<a class="dp-quote" href="' + href + '">'
    + '<span class="dp-kicker">Today’s Passage</span>'
    + '<span class="dp-mark" aria-hidden="true">“</span>'
    + '<span class="dp-text">' + esc(p.text) + '</span>'
    + '<span class="dp-src">— ' + esc(p.title) + ' · ' + esc(p.chapterTitle) + '</span>'
    + '<span class="dp-cta">Read in context →</span>'
    + '<button class="dp-share" type="button" aria-label="Share this passage">Share ↗</button></a>';
  el.hidden = false;
  var sb = el.querySelector('.dp-share');
  if (sb) sb.onclick = function (ev) { ev.preventDefault(); ev.stopPropagation();
    if (!window.CorpusShare) return;  // the shell (which defines CorpusShare) loads after this script
    window.CorpusShare.open({ kicker: 'Today’s Passage', quote: p.text, source: p.title + ' · ' + p.chapterTitle,
      url: new URL(href, location.href).href, filename: 'passage' }); };
})();
"""

# "For You" — a living, personalized home surface. Runs on the index AFTER the
# shell has decorated the grid cards (setTimeout defers it past the shell script),
# then CLONES the relevant already-built cards into discovery rows — so covers,
# progress rings and themes come along for free. Returning readers get Keep
# reading / Because you read X / More in <top subject>; newcomers get Start here.
# Reads localStorage history + the inlined manifest's similarity graph. Static.
HOME_JS = r"""
setTimeout(function () {
  var fy = document.getElementById('foryou');
  var mEl = document.getElementById('library-manifest');
  if (!fy || !mEl) return;
  var LIB; try { LIB = JSON.parse(mEl.textContent); } catch (e) { return; }
  var corpora = LIB.filter(function (x) { return x.kind === 'corpus'; });
  var bySlug = {}; corpora.forEach(function (c) { bySlug[c.slug] = c; });
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  var recents = []; try { recents = JSON.parse(localStorage.getItem('library-recents') || '[]') || []; } catch (e) {}
  function readCount(slug) { try { return (JSON.parse(localStorage.getItem('read:' + slug) || '[]') || []).length; } catch (e) { return 0; } }
  function total(slug) { var c = bySlug[slug]; return c && c.chapters ? c.chapters.length : 0; }
  function finished(slug) { var t = total(slug); return t > 0 && readCount(slug) >= t; }
  var used = {};
  function fresh(list) { var out = []; list.forEach(function (s) { if (bySlug[s] && !used[s]) { used[s] = 1; out.push(s); } }); return out; }
  function cardClone(slug, href) { var o = document.querySelector('.card[data-slug="' + slug + '"]'); if (!o) return null; var c = o.cloneNode(true); if (href) c.setAttribute('href', href); return c; }
  function row(titleHTML, slugs, hrefMap) {
    if (!slugs.length) return null;
    var g = document.createElement('div'); g.className = 'fy-group';
    var h = document.createElement('h3'); h.className = 'fy-h'; h.innerHTML = titleHTML; g.appendChild(h);
    var strip = document.createElement('div'); strip.className = 'fy-row'; var any = false;
    slugs.forEach(function (s) { var cc = cardClone(s, hrefMap && hrefMap[s]); if (cc) { strip.appendChild(cc); any = true; } });
    if (!any) return null;
    g.appendChild(strip); return g;
  }
  var groups = [], kicker, note = '';
  if (recents.length) {
    kicker = 'For you';
    var last = recents[0]; used[last.slug] = 1;
    var keep = [], hrefMap = {};
    recents.slice(1, 7).forEach(function (r) { if (bySlug[r.slug] && !finished(r.slug)) { keep.push(r.slug); hrefMap[r.slug] = r.slug + '.html#ch-' + r.ch; } });
    var g0 = row('Keep reading', fresh(keep), hrefMap); if (g0) groups.push(g0);
    var rel = (bySlug[last.slug] && bySlug[last.slug].related) || [];
    var relSlugs = fresh(rel.map(function (r) { return r.slug; }).filter(function (s) { return !finished(s); }));
    var g1 = row('Because you read <em>' + esc(last.title) + '</em>', relSlugs); if (g1) groups.push(g1);
    var catCount = {};
    corpora.forEach(function (c) { var n = readCount(c.slug); if (n) catCount[c.category] = (catCount[c.category] || 0) + n; });
    var topCat = null, mx = 0; for (var cat in catCount) { if (catCount[cat] > mx) { mx = catCount[cat]; topCat = cat; } }
    if (topCat) {
      var inCat = corpora.filter(function (c) { return c.category === topCat && !finished(c.slug); }).map(function (c) { return c.slug; });
      var g2 = row('More in <em>' + esc(topCat) + '</em>', fresh(inCat).slice(0, 8)); if (g2) groups.push(g2);
    }
  } else {
    kicker = 'Welcome';
    note = 'Open anything and this space becomes yours — what to read next, drawn from what you’ve read.';
    var g = row('Start here', fresh(corpora.slice(0, 7).map(function (c) { return c.slug; }))); if (g) groups.push(g);
  }
  if (!groups.length) return;
  var k = document.createElement('p'); k.className = 'fy-kicker'; k.textContent = kicker; fy.appendChild(k);
  if (note) { var n = document.createElement('p'); n.className = 'fy-note'; n.textContent = note; fy.appendChild(n); }
  groups.forEach(function (g) { fy.appendChild(g); });
  fy.hidden = false;
}, 0);
"""

# First-visit overture — a once-only, flash-free welcome on the home page,
# assembled entirely from the existing brand (site title/subtitle + the three
# section names) plus the trencadís mosaic. A tiny synchronous <head> script adds
# html.show-overture BEFORE first paint when 'seen-overture' is unset, so there's
# no flash of the library behind it; the overlay is pre-rendered in HTML so it
# paints instantly. Instantly skippable (Enter / Esc / backdrop) and fully
# disabled under prefers-reduced-motion (shown without animation).
OVERTURE_HEAD = (
    "<script>try{if(!localStorage.getItem('seen-overture'))"
    "document.documentElement.classList.add('show-overture');}catch(e){}</script>"
)

OVERTURE_CSS = """
#overture { display: none; }
html.show-overture { overflow: hidden; }
html.show-overture #overture { display: flex; position: fixed; inset: 0; z-index: 200; background: var(--bg);
  align-items: center; justify-content: center; text-align: center; padding: 2rem; animation: ovFade .55s ease both; }
#overture.closing { animation: ovOut .5s ease forwards; }
.ov-inner { max-width: 640px; }
.ov-tiles { display: flex; gap: 8px; justify-content: center; margin: 0 0 1.7rem; }
.ov-tiles span { width: 22px; height: 22px; }
.ov-tiles span:nth-child(1) { background: var(--t1); border-radius: 4px 10px 5px 9px; transform: rotate(-7deg); }
.ov-tiles span:nth-child(2) { background: var(--t2); border-radius: 9px 4px 10px 5px; transform: rotate(9deg); }
.ov-tiles span:nth-child(3) { background: var(--t3); border-radius: 5px 9px 4px 10px; transform: rotate(-5deg); }
.ov-tiles span:nth-child(4) { background: var(--t4); border-radius: 10px 5px 9px 4px; transform: rotate(6deg); }
.ov-tiles span:nth-child(5) { background: var(--t5); border-radius: 6px 9px 5px 10px; transform: rotate(-9deg); }
.ov-brand { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .2em; color: var(--accent); margin: 0 0 1rem; }
.ov-title { font-family: var(--display); font-size: clamp(2.4rem, 7vw, 4.2rem); line-height: 1.04; letter-spacing: -.01em; margin: 0 0 .6rem; }
.ov-sub { font-family: var(--serif); font-style: italic; font-size: 1.25rem; color: var(--muted); margin: 0 0 1.8rem; }
.ov-sections { display: flex; flex-wrap: wrap; gap: .55rem 1.7rem; justify-content: center; font-family: var(--sans);
  font-size: .72rem; text-transform: uppercase; letter-spacing: .13em; color: var(--muted); margin: 0 0 2.2rem; }
.ov-sections span { position: relative; }
.ov-sections span:not(:last-child)::after { content: "·"; position: absolute; right: -1rem; color: var(--border); }
#ov-enter { font-family: var(--sans); font-size: .82rem; text-transform: uppercase; letter-spacing: .08em; color: var(--bg);
  background: var(--accent); border: none; border-radius: 14px; padding: .8rem 1.8rem; cursor: pointer; transition: transform .15s ease; }
#ov-enter:hover { transform: translateY(-2px); }
.ov-skip { font-family: var(--sans); font-size: .68rem; color: var(--muted); margin: 1rem 0 0; }
html.show-overture .ov-inner > * { animation: ovRise .7s cubic-bezier(.2,.7,.3,1) both; }
html.show-overture .ov-tiles { animation-delay: .05s; }
html.show-overture .ov-brand { animation-delay: .14s; }
html.show-overture .ov-title { animation-delay: .22s; }
html.show-overture .ov-sub { animation-delay: .34s; }
html.show-overture .ov-sections { animation-delay: .46s; }
html.show-overture #ov-enter { animation-delay: .58s; }
html.show-overture .ov-skip { animation-delay: .7s; }
@keyframes ovFade { from { opacity: 0; } to { opacity: 1; } }
@keyframes ovOut { from { opacity: 1; } to { opacity: 0; visibility: hidden; } }
@keyframes ovRise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  html.show-overture #overture, html.show-overture .ov-inner > * { animation: none !important; }
}
"""

OVERTURE_JS = r"""
(function () {
  var ov = document.getElementById('overture');
  if (!ov) return;
  if (!document.documentElement.classList.contains('show-overture')) { ov.remove(); return; }
  var enter = document.getElementById('ov-enter');
  function dismiss() {
    if (ov.classList.contains('closing')) return;
    try { localStorage.setItem('seen-overture', '1'); } catch (e) {}
    ov.classList.add('closing');
    // honour reduced motion: skip the fade-out dead-time (overlay + scroll-lock go at once)
    var quick = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    setTimeout(function () { document.documentElement.classList.remove('show-overture'); if (ov.parentNode) ov.remove(); }, quick ? 0 : 520);
  }
  if (enter) enter.addEventListener('click', dismiss);
  ov.addEventListener('click', function (e) { if (e.target === ov) dismiss(); });
  document.addEventListener('keydown', function (e) {
    if (!document.getElementById('overture')) return;  // after dismissal this handler no-ops site-wide
    if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); dismiss(); }
    else if (e.key === 'Tab') { e.preventDefault(); if (enter) enter.focus(); }  // trap focus on the single control
  });
  if (enter) try { enter.focus(); } catch (e) {}  // move focus into the dialog on open
})();
"""

LIBRARY_CSS = """
:root {
  --bg: #faf8f4; --panel: #f1ede5; --text: #1f1d1a; --muted: #6e6a62;
  --accent: #8c4a2f; --border: #ddd6c9; --cover-bg: #f3ead8;
  --t1: #b3502f; --t2: #c9a227; --t3: #2e5266; --t4: #6b7d4f;
  --serif: Georgia, 'Iowan Old Style', serif;
  --display: 'Iowan Old Style', Palatino, Georgia, serif;
  --sans: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
}
[data-theme="dark"] {
  --bg: #161513; --panel: #1f1d1a; --text: #e8e4db; --muted: #968f82;
  --accent: #d98f5f; --border: #35322c; --cover-bg: #232019;
  --t1: #d4795a; --t2: #d8b545; --t3: #6f9bb3; --t4: #93a86f;
}
* { box-sizing: border-box; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--serif); }
.masthead { max-width: 1080px; margin: 0 auto; padding: .8rem 2rem; display: flex;
  justify-content: space-between; align-items: center; font-family: var(--sans); font-size: .68rem;
  color: var(--muted); text-transform: uppercase; letter-spacing: .14em; border-bottom: 1px solid var(--border); }
.mh-nav { display: flex; gap: 1.3rem; }
.mh-nav a { color: var(--muted); text-decoration: none; border-bottom: 1px solid transparent; padding-bottom: 2px; }
.mh-nav a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.section-title { max-width: 1080px; margin: 1.6rem auto .2rem; padding: 0 2rem; font-family: var(--display);
  font-size: 1.5rem; scroll-margin-top: 1rem; }
.section-title::after { content: ""; display: block; height: 4px; width: 96px; margin-top: .5rem; border-radius: 2px;
  background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
/* Collections — the flagship: curated cross-corpus reading arcs, shown as posters. */
.collections { max-width: 1080px; margin: 1.9rem auto 0; padding: 0 2rem; }
.coll-h { font-family: var(--display); font-size: 1.5rem; margin: 0; }
.coll-h::after { content: ""; display: block; height: 4px; width: 96px; margin-top: .5rem; border-radius: 2px;
  background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
.coll-intro { font-family: var(--sans); font-size: .9rem; color: var(--muted); margin: .7rem 0 1.2rem; max-width: 42rem; line-height: 1.5; }
.coll-shelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(264px, 1fr)); gap: 1.4rem; }
.coll-card { display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden; text-decoration: none; color: var(--text);
  transition: transform .18s cubic-bezier(.34,1.56,.64,1), box-shadow .16s ease, border-color .16s ease; }
.coll-card:hover { transform: translateY(-3px); border-color: var(--accent); box-shadow: 0 10px 30px rgba(0,0,0,.1); }
.coll-poster { position: relative; background: var(--cover-bg); border-bottom: 1px solid var(--border); }
.coll-poster svg { width: 100%; height: 138px; display: block; }
.coll-poster::after { content: ""; position: absolute; inset: 0; background: linear-gradient(180deg, transparent 38%, rgba(20,18,15,.55) 100%); }
.coll-poster-title { position: absolute; left: 15px; right: 15px; bottom: 12px; z-index: 1; font-family: var(--display);
  font-weight: 700; color: #f6efe2; font-size: 1.42rem; line-height: 1.08; text-shadow: 0 1px 8px rgba(0,0,0,.5); }
.coll-body { padding: 1rem 1.2rem 1.15rem; display: flex; flex-direction: column; flex: 1; }
.coll-note { font-family: var(--sans); font-size: .82rem; color: var(--muted); line-height: 1.5; margin: 0 0 .9rem; flex: 1;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.coll-meta { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .07em;
  color: var(--accent); margin: 0; display: flex; align-items: center; justify-content: space-between; gap: .6rem; }
/* Today's Passage — the daily pull-quote hook pinned atop the index. */
#daily-passage { max-width: 1080px; margin: 1.6rem auto 0; padding: 0 2rem; }
.dp-quote { display: block; position: relative; overflow: hidden; text-decoration: none; color: var(--text);
  background: var(--panel); border: 1px solid var(--border); border-radius: 18px; padding: 1.6rem 1.9rem 1.4rem;
  transition: transform .16s cubic-bezier(.34,1.56,.64,1), border-color .16s ease; }
.dp-quote:hover { transform: translateY(-2px); border-color: var(--accent); }
.dp-kicker { display: block; font-family: var(--sans); font-size: .68rem; text-transform: uppercase;
  letter-spacing: .18em; color: var(--accent); margin: 0 0 .55rem; }
.dp-mark { position: absolute; top: .2rem; right: 1.2rem; font-family: var(--display); font-size: 5rem;
  line-height: 1; color: var(--accent); opacity: .12; }
.dp-text { display: block; font-family: var(--display); font-style: italic; font-size: clamp(1.2rem, 2.5vw, 1.6rem);
  line-height: 1.36; margin: 0 0 .8rem; max-width: 46rem; }
.dp-src { display: block; font-family: var(--sans); font-size: .8rem; color: var(--muted); margin: 0 0 1rem; }
.dp-cta { font-family: var(--sans); font-size: .74rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--accent); border-bottom: 1.5px solid var(--accent); padding-bottom: 2px; }
.dp-share { position: absolute; right: 1.5rem; bottom: 1.25rem; font-family: var(--sans); font-size: .7rem;
  text-transform: uppercase; letter-spacing: .05em; color: var(--muted); background: var(--bg);
  border: 1px solid var(--border); border-radius: 9px; padding: .35rem .7rem; cursor: pointer; }
.dp-share:hover { color: var(--accent); border-color: var(--accent); }
/* "For You" — the living, personalized discovery zone (rows of cloned cards). */
#foryou { max-width: 1080px; margin: 1.4rem auto 0; padding: 0 2rem; }
#foryou[hidden] { display: none; }
.fy-kicker { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .18em; color: var(--accent); margin: 0 0 .15rem; }
.fy-note { font-family: var(--sans); font-size: .82rem; color: var(--muted); margin: .2rem 0 .4rem; max-width: 40rem; line-height: 1.5; }
.fy-group { margin: 0 0 1.1rem; }
.fy-h { font-family: var(--display); font-weight: normal; font-size: 1.12rem; margin: .9rem 0 .7rem; }
.fy-h em { font-style: italic; color: var(--accent); }
.fy-row { display: flex; gap: 1.1rem; overflow-x: auto; padding: .2rem .1rem 1rem; scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.fy-row::-webkit-scrollbar { height: 8px; }
.fy-row::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.fy-row::-webkit-scrollbar-track { background: transparent; }
.fy-row .card { width: 264px; flex: 0 0 264px; scroll-snap-align: start; }
@media (max-width: 560px) { .fy-row .card { width: 78vw; flex-basis: 78vw; } #foryou { padding: 0 1.2rem; } }
/* The Ghost of Times feature band on the home page — inky newspaper contrast to the mosaic library. */
.ghost-band { max-width: 1080px; margin: 2rem auto 0; padding: 0 2rem; }
.ghost-band a { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: #f3ead8; background: #1b1a17; border: 1px solid #322f29; border-radius: 18px;
  padding: 1.5rem 1.8rem; transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.ghost-band a:hover { transform: translateY(-3px); box-shadow: 0 14px 36px rgba(0,0,0,.28); border-color: var(--t2); }
.ghost-band .gb-flag { font-family: var(--display); font-size: 2.1rem; line-height: 1; color: #f3ead8;
  border-right: 1px solid #46423a; padding-right: 1.6rem; }
.ghost-band .gb-flag small { display: block; font-family: var(--sans); font-size: .58rem; letter-spacing: .22em;
  text-transform: uppercase; color: var(--t2); margin-top: .5rem; }
.ghost-band .gb-mid .gb-kicker { font-family: var(--sans); font-size: .68rem; text-transform: uppercase;
  letter-spacing: .16em; color: var(--t2); margin: 0 0 .35rem; }
.ghost-band .gb-mid .gb-lead { font-family: var(--display); font-size: 1.18rem; line-height: 1.28; margin: 0 0 .3rem; color: #f6efe2; }
.ghost-band .gb-mid .gb-sub { font-family: var(--sans); font-size: .82rem; line-height: 1.45; color: #b9b2a4; margin: 0; }
.ghost-band .gb-cta { font-family: var(--sans); font-size: .76rem; text-transform: uppercase; letter-spacing: .1em;
  color: #1b1a17; background: var(--t2); border-radius: 12px; padding: .55rem 1rem; white-space: nowrap; }
@media (max-width: 680px) {
  .ghost-band a { grid-template-columns: 1fr; gap: .9rem; }
  .ghost-band .gb-flag { border-right: none; border-bottom: 1px solid #46423a; padding: 0 0 .9rem; }
  .masthead { flex-direction: column; align-items: flex-start; gap: .5rem; letter-spacing: .09em; }
  .mh-nav { flex-wrap: wrap; gap: .4rem 1.1rem; }
}
header { max-width: 1080px; margin: 0 auto; padding: 2.6rem 2rem 1rem; display: flex;
  align-items: center; gap: 2.5rem; }
.hero-text { flex: 1.1; }
.hero-art { flex: 1; min-width: 0; }
.hero-art svg { width: 100%; height: auto; display: block; }
.hero-art .hero-img { width: 100%; height: auto; display: block; border-radius: 10px; }
[data-theme="dark"] .hero-art .hero-core { fill: #161513; }
[data-theme="dark"] .hero-art .hero-ground { fill: #e8e4db; }
.kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .18em; color: var(--accent); margin: 0 0 .6rem; }
header h1 { font-family: var(--display); font-size: clamp(2.3rem, 4.6vw, 3.3rem);
  line-height: 1.08; margin: 0 0 .7rem; letter-spacing: -.01em; }
.tagline { color: var(--muted); margin: 0 0 1.1rem; font-family: var(--sans); font-size: .95rem;
  line-height: 1.5; max-width: 34rem; }
.stats { font-family: var(--sans); font-size: .74rem; color: var(--accent); margin: 0;
  text-transform: uppercase; letter-spacing: .1em; }
.library { max-width: 1080px; margin: 0 auto; padding: 0 0 3rem; }
/* Library toolbar — search box + category filter pills. */
.lib-toolbar { padding: 1.2rem 2rem .4rem; display: flex; flex-wrap: wrap; align-items: center;
  gap: .9rem 1.4rem; }
.lib-search { flex: 1 1 240px; min-width: 0; max-width: 360px; font-family: var(--sans); font-size: .9rem;
  color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: .55rem .85rem; -webkit-appearance: none; appearance: none; }
.lib-search:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(140,74,47,.14); }
.lib-search::placeholder { color: var(--muted); }
.cat-pills { display: flex; flex-wrap: wrap; gap: .5rem; }
.cat-pill { font-family: var(--sans); font-size: .76rem; letter-spacing: .02em; color: var(--muted);
  background: transparent; border: 1px solid var(--border); border-radius: 999px; padding: .4rem .85rem;
  cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease; }
.cat-pill:hover { color: var(--accent); border-color: var(--accent); }
.cat-pill.active { color: var(--bg); background: var(--accent); border-color: var(--accent); }
.cat-pill .cat-count { opacity: .65; font-size: .9em; }
.cat-section { padding-top: .6rem; }
.cat-section[hidden] { display: none; }
.cat-heading { max-width: 100%; margin: 1.2rem 0 0; padding: 0 2rem; font-family: var(--display);
  font-size: 1.18rem; color: var(--text); display: flex; align-items: baseline; gap: .5rem; }
.cat-heading .cat-count { font-family: var(--sans); font-size: .72rem; color: var(--muted);
  font-weight: normal; letter-spacing: .06em; }
.lib-empty { font-family: var(--sans); color: var(--muted); text-align: center; padding: 2.5rem 2rem; margin: 0; }
.grid { padding: 1.1rem 2rem 1.4rem;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.4rem; }
.card { display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden; text-decoration: none; color: var(--text);
  transition: transform .2s cubic-bezier(.34,1.56,.64,1), box-shadow .15s ease, border-color .15s ease; }
.card:hover { transform: translateY(-3px); box-shadow: 0 10px 30px rgba(0,0,0,.1); border-color: var(--accent); }
.card:active { transform: translateY(-1px) scale(.992); }
.cover { background: var(--cover-bg); border-bottom: 1px solid var(--border); }
.cover svg { width: 100%; height: 124px; display: block; }
/* Photo covers crop to the card band at any source size/aspect ratio. */
.cover .cover-photo { width: 100%; height: 124px; object-fit: cover; object-position: center; display: block; }
.card-body { padding: 1.05rem 1.2rem 1.15rem; display: flex; flex-direction: column; flex: 1; }
.card h2 { font-family: var(--display); font-size: 1.13rem; line-height: 1.3; margin: 0 0 .4rem; }
.card .sub { color: var(--muted); font-size: .8rem; font-family: var(--sans);
  margin: 0 0 .9rem; line-height: 1.45; flex: 1;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card .meta { color: var(--accent); font-size: .7rem; font-family: var(--sans);
  text-transform: uppercase; letter-spacing: .08em; margin: 0; }
footer { max-width: 1080px; margin: 0 auto; padding: 1rem 2rem 3rem; border-top: 1px solid var(--border); }
.tiles { display: flex; gap: 5px; margin: 1.2rem 0 .9rem; }
.tiles span { width: 13px; height: 13px; }
.tiles span:nth-child(1) { background: var(--t1); border-radius: 3px 8px 4px 7px; transform: rotate(-6deg); }
.tiles span:nth-child(2) { background: var(--t2); border-radius: 7px 3px 8px 4px; transform: rotate(8deg); }
.tiles span:nth-child(3) { background: var(--t3); border-radius: 4px 7px 3px 8px; transform: rotate(-4deg); }
.tiles span:nth-child(4) { background: var(--t4); border-radius: 8px 4px 7px 3px; transform: rotate(5deg); }
.epigraph { font-style: italic; color: var(--muted); margin: 0 0 .3rem; font-size: .92rem; }
.colophon { font-family: var(--sans); font-size: .74rem; color: var(--muted); margin: 0; }
@media (max-width: 760px) {
  header { flex-direction: column-reverse; gap: 1.2rem; padding-top: 1.6rem; }
  .hero-art { width: 100%; }
}
"""


# ---------------------------------------------------------------- ghost of times
# A second section of the site: "The Ghost of Times" — a daily paper of
# writer-voiced op-eds, produced by the ghost_of_times skill. Each published
# edition is a self-contained HTML file dropped into docs/ghost/; this builder
# reads docs/ghost/manifest.json and renders the section index (ghost.html)
# plus a feature band on the home page. The edition files themselves are NOT
# regenerated here — they are authored by the skill and only listed.

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _weekday(date_str):
    try:
        import datetime
        return WEEKDAYS[datetime.date.fromisoformat(date_str[:10]).weekday()]
    except Exception:
        return ""


def read_ghost_manifest(out_dir):
    """Load docs/ghost/manifest.json → list of editions, newest first. Missing → []."""
    path = Path(out_dir) / "ghost" / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, treating as no editions", file=sys.stderr)
        return []
    eds = data.get("editions", data) if isinstance(data, dict) else data
    eds = [e for e in eds if isinstance(e, dict) and e.get("date")]
    eds.sort(key=lambda e: (e.get("edition_number", 0), e.get("date", "")), reverse=True)
    return eds


def _writers_line(ed):
    names = ed.get("writers") or []
    if not names:
        return ""
    n = len(names)
    return f"{n} writer{'s' if n != 1 else ''} · " + " · ".join(html.escape(x) for x in names)


def ghost_band_html(editions, ghost_cfg):
    """The Ghost of Times feature band for the home page. Always links to ghost.html."""
    motto = html.escape(ghost_cfg.get("motto", ""))
    blurb = html.escape(ghost_cfg.get("blurb", ""))
    flag = (f'<div class="gb-flag">The Ghost<br>of Times'
            f'<small>{motto}</small></div>')
    if editions:
        latest = editions[0]
        no = latest.get("edition_number")
        kicker = "The Ghost of Times" + (f" · No. {no:02d}" if isinstance(no, int) else "")
        when = _weekday(latest.get("date", "")) or ""
        when = f"{when} · {latest['date']}" if when else latest.get("date", "")
        lead = html.escape(latest.get("lead_headline") or "Latest edition")
        sub = html.escape(when)
        cta = "Read the latest →"
    else:
        kicker = "A new section"
        lead = motto or "The Ghost of Times"
        sub = blurb
        cta = "Coming soon →"
    mid = (f'<div class="gb-mid"><p class="gb-kicker">{kicker}</p>'
           f'<p class="gb-lead">{lead}</p><p class="gb-sub">{sub}</p></div>')
    return (f'<div class="ghost-band"><a href="ghost.html">{flag}{mid}'
            f'<span class="gb-cta">{cta}</span></a></div>')


GHOST_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Ghost of Times — research · calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="index.html">The Research</a>
    <a href="ghost.html" class="active">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
  </nav>
</div>
<header class="ghost-plate">
  {engraving}
  <p class="gp-kicker">A paper of writer-voiced op-eds</p>
  <h1 class="gp-name">The Ghost of Times</h1>
  <p class="gp-motto">“{motto}”</p>
  <div class="gp-folio">
    <span>Vol. 1</span>
    <span class="gp-folio-c">{stats}</span>
    <span>Published irregularly</span>
  </div>
</header>
<main class="ged-wrap">
{editions}
</main>
<footer class="ghost-foot">
  <p class="epigraph">{blurb}</p>
  <p class="colophon"><a href="index.html">← Back to the Research Library</a></p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

GHOST_PAGE_CSS = """
/* The Ghost of Times — a newspaper section front, sharing the editions'
   warm-paper / serif / double-rule / terracotta language. */
.ghost-plate { display: block; max-width: 820px; margin: 1.6rem auto 0; padding: 0 2rem; text-align: center; }
.gp-engraving { display: block; width: 100%; max-width: 560px; height: auto; margin: .4rem auto 1.4rem;
  border-radius: 14px; border: 1px solid var(--border); }
[data-theme="dark"] .gp-engraving { filter: brightness(.92) contrast(1.02); }
.gp-kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .2em; color: var(--accent); margin: 0 0 .5rem; }
.gp-name { font-family: var(--display); font-weight: 800; font-size: clamp(2.6rem, 6.5vw, 4.4rem);
  line-height: .98; letter-spacing: -.02em; margin: 0 0 .55rem; }
.gp-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.gp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .14em; color: var(--text); }
.gp-folio .gp-folio-c { color: var(--accent); font-weight: 700; }

/* featured (latest) edition — reads like a section lead */
.ged-feature { max-width: 720px; margin: 2.1rem auto 0; padding: 0 2rem; }
.ged-feature a { display: block; text-decoration: none; color: var(--text); }
.gedf-meta { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .12em;
  color: var(--accent); margin: 0 0 .7rem; }
.gedf-head { font-family: var(--display); font-weight: 800; font-size: clamp(1.9rem, 4.2vw, 2.7rem);
  line-height: 1.07; letter-spacing: -.01em; margin: 0 0 .7rem; transition: color .15s ease; }
.ged-feature a:hover .gedf-head { color: var(--accent); }
.gedf-dek { font-family: var(--serif); font-style: italic; font-size: 1.2rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 .9rem; }
.gedf-writers { font-family: var(--sans); font-size: .74rem; letter-spacing: .04em; color: var(--accent); margin: 0 0 1.1rem; }
.gedf-cta { font-family: var(--sans); font-size: .78rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--accent); border-bottom: 1.5px solid var(--accent); padding-bottom: 2px; }

/* back issues — newspaper archive rows */
.ged-issues { max-width: 720px; margin: 2.8rem auto 0; padding: 0 2rem 1rem; }
.ged-issues-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .3rem; }
.ged-row { display: grid; grid-template-columns: auto 1fr auto; gap: 1.2rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .95rem 0; border-bottom: 1px solid var(--border); }
.ged-row-no { font-family: var(--display); font-size: 1.05rem; color: var(--accent); white-space: nowrap; }
.ged-row-body { min-width: 0; }
.ged-row-head { font-family: var(--display); font-size: 1.18rem; line-height: 1.18; display: block; transition: color .15s ease; }
.ged-row:hover .ged-row-head { color: var(--accent); }
.ged-row-meta { font-family: var(--sans); font-size: .7rem; letter-spacing: .03em; color: var(--muted); display: block; margin-top: .25rem; }
.ged-row-date { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); white-space: nowrap; }

.ged-empty { max-width: 720px; margin: 2.1rem auto 0; padding: 2rem; text-align: center;
  color: var(--muted); font-family: var(--sans); font-size: .9rem; border-top: 3px double var(--border); }

.ghost-foot { max-width: 720px; margin: 3rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.ghost-foot .epigraph { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .95rem; margin: 0 0 .6rem; }
.ghost-foot .colophon { font-family: var(--sans); font-size: .74rem; margin: 0; }
.ghost-foot .colophon a { color: var(--accent); text-decoration: none; }
.ghost-foot .colophon a:hover { text-decoration: underline; }

@media (max-width: 560px) {
  .gp-folio { font-size: .58rem; letter-spacing: .08em; }
  .ged-row { grid-template-columns: auto 1fr; gap: .8rem; }
  .ged-row-date { display: none; }
}
"""


MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def _long_date(date_str):
    """'2026-05-29' → 'Friday, May 29, 2026'. Falls back to the raw string."""
    try:
        import datetime
        d = datetime.date.fromisoformat(date_str[:10])
        return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}, {d.year}"
    except Exception:
        return date_str


def _ed_href(ed):
    return html.escape(ed.get("file") or f"ghost/{ed.get('date','')}-ghost-of-times.html", quote=True)


def _no_label(ed):
    no = ed.get("edition_number")
    return f"Nº {no:02d}" if isinstance(no, int) else "Nº —"


def ghost_feature_html(ed):
    """The latest edition, rendered like a newspaper section lead."""
    meta = " · ".join(x for x in ["Latest edition", _no_label(ed), _long_date(ed.get("date", ""))] if x)
    headline = html.escape(ed.get("lead_headline") or f"Edition of {ed.get('date','')}")
    dek = html.escape(ed.get("lead_dek") or "")
    dek_html = f'<p class="gedf-dek">{dek}</p>' if dek else ""
    writers = _writers_line(ed)
    writers_html = f'<p class="gedf-writers">{writers}</p>' if writers else ""
    return (f'<article class="ged-feature"><a href="{_ed_href(ed)}">'
            f'<p class="gedf-meta">{html.escape(meta)}</p>'
            f'<h2 class="gedf-head">{headline}</h2>{dek_html}{writers_html}'
            f'<span class="gedf-cta">Read the edition →</span></a></article>')


def ghost_row_html(ed):
    """A back-issue row in the archive list."""
    when = _weekday(ed.get("date", ""))
    when_s = f"{when} · {ed['date']}" if when else ed.get("date", "")
    headline = html.escape(ed.get("lead_headline") or f"Edition of {ed.get('date','')}")
    writers = _writers_line(ed)
    writers_html = f'<span class="ged-row-meta">{writers}</span>' if writers else ""
    return (f'<a class="ged-row" href="{_ed_href(ed)}">'
            f'<span class="ged-row-no">{_no_label(ed)}</span>'
            f'<span class="ged-row-body"><span class="ged-row-head">{headline}</span>{writers_html}</span>'
            f'<span class="ged-row-date">{html.escape(when_s)}</span></a>')


def build_ghost_page(out_dir, editions, ghost_cfg, shell=""):
    """Render docs/ghost.html — the section front: a featured latest edition + back issues."""
    out = Path(out_dir)
    if editions:
        n = len(editions)
        stats = f"{n} edition{'s' if n != 1 else ''}"
        body = ghost_feature_html(editions[0])
        rest = editions[1:]
        if rest:
            rows = "\n".join(ghost_row_html(e) for e in rest)
            body += f'<section class="ged-issues"><h2 class="ged-issues-h">Back issues</h2>{rows}</section>'
    else:
        body = ('<p class="ged-empty">No editions published yet. Run the Ghost of Times '
                'skill and publish an edition to see it here.</p>')
        stats = "No editions yet"
    engraving = ""
    if (out / "ghost" / "masthead.jpg").exists():
        engraving = ('<img class="gp-engraving" src="ghost/masthead.jpg" '
                     'alt="The Ghost of Times — masthead engraving">')
    page = GHOST_PAGE_TEMPLATE.format(
        css=LIBRARY_CSS + GHOST_PAGE_CSS,
        favicon=FAVICON, og_meta=OG_META,
        engraving=engraving,
        motto=html.escape(ghost_cfg.get("motto", "")),
        blurb=html.escape(ghost_cfg.get("blurb", "")),
        stats=stats,
        editions=body,
        theme_js=LIBRARY_THEME_JS,
        shell=shell,
    )
    (out / "ghost.html").write_text(page)
    print(f"  ✓ The Ghost of Times  ({len(editions)} editions) → ghost.html")


# ---------------------------------------------------------------- ghost editions
# Each published edition is rendered HERE, natively, from its structured content
# (docs/ghost/data/{date}.json — deposited by the ghost_of_times skill's publish
# step) rather than shipped as a self-contained print artifact. That means every
# edition inherits the site's design system — warm-paper tokens, Iowan/Georgia,
# terracotta, dark mode, the masthead nav — for free, and stays a few KB instead
# of multiple megabytes of embedded fonts. The op-ed bodies are markdown, parsed
# client-side by the same marked.js the corpus reader uses.
#
# Design register: a site-native reading column (the corpus reader's measure and
# serif) wearing newspaper chrome — a "Ghost of Times" nameplate with the folio
# double-rule that echoes ghost.html, the site's 4-colour gradient bar under the
# lead headline, small-caps bylines, italic deks, drop-cap leads, and a paneled
# "The Facts" ledger. Different from a corpus chapter, but unmistakably the same
# site.

GHOST_EDITION_CSS = """
/* A single Ghost of Times edition — the corpus reader's reading column in
   newspaper dress. Shares every colour/font token with the rest of the site. */
.gh-edition { max-width: 760px; margin: 0 auto; padding: 2.2rem 2rem 1rem; }

/* nameplate — a compact echo of ghost.html's section plate. `display:block`
   overrides the generic `header{display:flex}` rule from LIBRARY_CSS so the
   kicker / name / folio stack vertically like the section front. */
.gh-nameplate { display: block; text-align: center; max-width: 640px; margin: 0 auto 2.6rem; padding: 0; }
.gh-kicker { font-family: var(--sans); font-size: .68rem; text-transform: uppercase;
  letter-spacing: .2em; color: var(--accent); margin: 0 0 .45rem; }
.gh-name { display: inline-block; font-family: var(--display); font-weight: 800;
  font-size: clamp(2.1rem, 5.2vw, 3.1rem); line-height: .98; letter-spacing: -.02em;
  color: var(--text); text-decoration: none; margin: 0 0 .9rem; }
.gh-name:hover { color: var(--accent); }
.gh-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .5rem 0;
  font-family: var(--sans); font-size: .66rem; text-transform: uppercase; letter-spacing: .14em; }
.gh-folio .gh-folio-c { color: var(--accent); font-weight: 700; }

/* contents — every story + author, clickable to its piece */
.gh-contents { display: block; margin: 0 0 2.8rem; }
.gh-contents-h { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .2em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .1rem; }
.gh-toc-item { display: grid; grid-template-columns: 3.2rem 1fr; gap: .8rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .7rem .2rem; border-bottom: 1px solid var(--border); }
.gh-toc-item:hover { background: var(--panel); }
.gh-toc-no { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--accent); white-space: nowrap; padding-top: .15rem; }
.gh-toc-item.is-lead .gh-toc-no { font-weight: 700; }
.gh-toc-head { font-family: var(--display); font-size: 1.12rem; line-height: 1.2; display: block;
  transition: color .15s ease; }
.gh-toc-item:hover .gh-toc-head { color: var(--accent); }
.gh-toc-by { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--muted); display: block; margin-top: .25rem; }

/* the op-ed stack */
.gh-piece { margin: 0; scroll-margin-top: 1.2rem; }
.gh-head { font-family: var(--display); font-weight: 800; letter-spacing: -.01em;
  line-height: 1.1; margin: 0 0 .55rem; }
.gh-piece .gh-head { font-size: clamp(1.55rem, 3.2vw, 2rem); }
.gh-lead .gh-head { font-size: clamp(2rem, 4.6vw, 2.9rem); line-height: 1.06; }
.gh-lead .gh-head::after { content: ""; display: block; height: 4px; width: 110px; margin-top: .6rem;
  border-radius: 2px; background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
.gh-dek { font-family: var(--serif); font-style: italic; color: var(--muted);
  font-size: 1.16rem; line-height: 1.42; margin: 0 0 .9rem; }
.gh-lead .gh-dek { font-size: 1.28rem; }
.gh-byline { font-family: var(--sans); font-size: .72rem; letter-spacing: .14em; color: var(--accent);
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  padding: .4rem 0; margin: 0 0 1.3rem; }
/* objective-voice abstract — neutral framing of what the piece is about, set apart from the body */
.gh-summary { margin: 0 0 1.5rem; padding: .85rem 1.1rem; background: var(--panel);
  border-left: 3px solid var(--accent); border-radius: 0 12px 12px 0; }
.gh-summary-label { display: block; font-family: var(--sans); font-size: .64rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .18em; color: var(--muted); margin: 0 0 .4rem; }
.gh-summary-text { font-family: var(--sans); font-size: .9rem; line-height: 1.55; color: var(--text); margin: 0; }
.gh-lead .gh-summary-text { font-size: .96rem; }
.gh-body { font-size: 1.05rem; line-height: 1.74; }
.gh-body p { margin: 0 0 1.15rem; }
.gh-body a { color: var(--accent); }
.gh-body em { font-style: italic; }
.gh-body blockquote { margin: 1.4rem 0; padding: .6rem 1.3rem; border-left: 3px solid var(--accent);
  background: var(--panel); border-radius: 0 14px 14px 0; color: var(--muted); font-style: italic; }
/* drop cap opens each piece */
.gh-body > p:first-of-type::first-letter { font-family: var(--display); font-weight: 800;
  float: left; font-size: 3.1em; line-height: .72; padding: .06em .1em 0 0; color: var(--accent); }
.gh-sources { font-family: var(--sans); font-size: .76rem; line-height: 1.6; color: var(--muted);
  margin: 1.2rem 0 0; }
.gh-sources a { color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; }
.gh-sources a:hover { border-bottom-color: var(--accent); }
.gh-sources .filed { text-transform: uppercase; letter-spacing: .08em; font-size: .7rem; }
.gh-sep { border: none; height: 1em; margin: 2.6rem 0; text-align: center; }
.gh-sep::before { content: "◆ ◆ ◆"; color: var(--accent); opacity: .4; font-size: .68rem; letter-spacing: 1em; padding-left: 1em; }

/* "The Facts" — the unfiltered wire ledger, as a paneled artifact */
.gh-facts { margin: 3rem auto 0; padding: 1.5rem 1.6rem 1.2rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: 16px; }
.gh-facts-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .2em;
  color: var(--accent); margin: 0 0 1.1rem; padding-bottom: .55rem; border-bottom: 2px solid var(--text); }
.gh-facts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.3rem 1.8rem; }
.gh-fact-head { font-family: var(--display); font-size: 1rem; line-height: 1.25; margin: 0 0 .25rem; }
.gh-fact-by { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); margin: 0 0 .5rem; }
.gh-fact ul { margin: 0; padding-left: 1.05rem; }
.gh-fact li { font-family: var(--sans); font-size: .82rem; line-height: 1.5; color: var(--text); margin: 0 0 .35rem; }
.gh-fact li::marker { color: var(--accent); }

/* foot */
.gh-foot { max-width: 760px; margin: 2.6rem auto 0; padding: 1.5rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.gh-watch { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .98rem; margin: 0 0 .7rem; }
.gh-colophon { font-family: var(--sans); font-size: .74rem; color: var(--muted); margin: 0; }
.gh-colophon a { color: var(--accent); text-decoration: none; }
.gh-colophon a:hover { text-decoration: underline; }

/* floating theme toggle, same affordance as the corpus reader */
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: .4rem .7rem; cursor: pointer; }
#theme-btn:hover { color: var(--accent); border-color: var(--accent); }

@media (max-width: 620px) {
  .gh-edition { padding: 1.6rem 1.3rem 1rem; }
  .gh-folio { font-size: .58rem; letter-spacing: .08em; }
  .gh-facts-grid { grid-template-columns: 1fr; gap: 1.1rem; }
}
"""

GHOST_EDITION_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="index.html">The Research</a>
    <a href="ghost.html" class="active">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
  </nav>
</div>
<main class="gh-edition">
  <header class="gh-nameplate">
    <p class="gh-kicker">A paper of writer-voiced op-eds</p>
    <a class="gh-name" href="ghost.html">The Ghost of Times</a>
    <div class="gh-folio">
      <span>{folio_no}</span>
      <span class="gh-folio-c">{folio_date}</span>
      <span>{folio_writers}</span>
    </div>
  </header>
  <nav id="gh-contents"></nav>
  <div id="gh-pieces"></div>
  <section id="gh-facts"></section>
  <footer class="gh-foot">
    {watch}
    <p class="gh-colophon"><a href="ghost.html">← All editions</a> &nbsp;·&nbsp; <a href="index.html">The Research Library</a></p>
  </footer>
</main>
<button id="theme-btn" title="Light / dark">◐ Theme</button>
<script id="ghost-edition-data" type="application/json">{data_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

GHOST_EDITION_JS = r"""
// theme (shares the 'corpus-theme' preference with readers + library)
const pref = localStorage.getItem('corpus-theme');
if (pref) document.documentElement.dataset.theme = pref;
else if (matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.dataset.theme = 'dark';
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

const ed = JSON.parse(document.getElementById('ghost-edition-data').textContent);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls;
  if (html != null) n.innerHTML = html; return n; };

// op-eds: lead first, then each section's stories, in render order
const pieces = [ed.lead].concat((ed.sections || []).flatMap(s => s.stories || [])).filter(Boolean);
const stack = document.getElementById('gh-pieces');
pieces.forEach((p, i) => {
  const isLead = i === 0;
  const art = el('article', 'gh-piece' + (isLead ? ' gh-lead' : ''));
  art.id = 'piece-' + i;
  art.appendChild(el(isLead ? 'h1' : 'h2', 'gh-head', escapeText(p.headline || '')));
  if (p.dek) art.appendChild(el('p', 'gh-dek', escapeText(p.dek)));
  if (p.author_byline) art.appendChild(el('p', 'gh-byline', escapeText(p.author_byline)));
  if (p.factual_summary) {
    const sum = el('aside', 'gh-summary');
    sum.appendChild(el('span', 'gh-summary-label', 'What this piece is about'));
    sum.appendChild(el('p', 'gh-summary-text', escapeText(p.factual_summary)));
    art.appendChild(sum);
  }
  const body = el('div', 'gh-body', marked.parse(p.body || ''));
  body.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  art.appendChild(body);
  if (p.byline_html) art.appendChild(el('p', 'gh-sources', p.byline_html));
  stack.appendChild(art);
  if (i < pieces.length - 1) stack.appendChild(el('hr', 'gh-sep'));
});

// Contents — every story + author at the top, each jumping to its piece.
const contents = document.getElementById('gh-contents');
if (pieces.length > 1) {
  contents.appendChild(el('p', 'gh-contents-h', 'In this edition'));
  pieces.forEach((p, i) => {
    const a = el('a', 'gh-toc-item' + (i === 0 ? ' is-lead' : ''));
    a.href = '#piece-' + i;
    a.appendChild(el('span', 'gh-toc-no', i === 0 ? 'Lead' : String(i + 1).padStart(2, '0')));
    const body = el('span', 'gh-toc-body');
    body.appendChild(el('span', 'gh-toc-head', escapeText(p.headline || '')));
    if (p.author_byline) body.appendChild(el('span', 'gh-toc-by', escapeText(p.author_byline)));
    a.appendChild(body);
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const t = document.getElementById('piece-' + i);
      if (t) { t.scrollIntoView({ behavior: 'smooth', block: 'start' });
               history.replaceState(null, '', '#piece-' + i); }
    });
    contents.appendChild(a);
  });
}

// "The Facts" ledger
const files = (ed.story_files && ed.story_files.files) || [];
const facts = document.getElementById('gh-facts');
if (files.length) {
  facts.className = 'gh-facts';
  facts.appendChild(el('h2', 'gh-facts-h', 'The Facts'));
  const grid = el('div', 'gh-facts-grid');
  files.forEach(f => {
    const d = el('div', 'gh-fact');
    d.appendChild(el('h3', 'gh-fact-head', escapeText(f.wire_headline || '')));
    if (f.writer_display_name) d.appendChild(el('p', 'gh-fact-by', 'As filed by ' + escapeText(f.writer_display_name)));
    const ul = document.createElement('ul');
    (f.bullets || []).forEach(b => ul.appendChild(el('li', null, escapeText(b.text || ''))));
    d.appendChild(ul);
    grid.appendChild(d);
  });
  facts.appendChild(grid);
}

function escapeText(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
"""


def _clean_byline_name(byline):
    """'BY G.K. CHESTERTON' -> 'G.K. Chesterton' for counting/labels (display untouched elsewhere)."""
    name = re.sub(r"^\s*by\s+", "", byline or "", flags=re.I).strip()
    if name and name.isupper():
        name = re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), name.lower())
    return name


def read_ghost_edition_data(out_dir, date):
    """Load a single edition's structured content from docs/ghost/data/{date}.json. Missing → None."""
    path = Path(out_dir) / "ghost" / "data" / f"{date}-ghost-of-times.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read edition data {path}", file=sys.stderr)
        return None


def build_ghost_edition(out_dir, ed, shell=""):
    """Render one edition page (docs/ghost/{date}-ghost-of-times.html) from its data file.

    Returns True if rendered, False if the data file is absent (page left untouched)."""
    out = Path(out_dir)
    date = ed.get("date", "")
    data = read_ghost_edition_data(out, date)
    if data is None:
        return False

    no = ed.get("edition_number")
    folio_no = f"No. {no:02d}" if isinstance(no, int) else "No. —"
    folio_date = _long_date(date)
    # writer count: prefer the manifest roster, else count the pieces
    writers = ed.get("writers") or []
    if not writers:
        names = [_clean_byline_name(data.get("lead", {}).get("author_byline", ""))]
        for sec in data.get("sections", []):
            for st in sec.get("stories", []):
                names.append(_clean_byline_name(st.get("author_byline", "")))
        writers = [w for w in names if w]
    nw = len(writers)
    folio_writers = f"{nw} writer{'s' if nw != 1 else ''}"

    watch = data.get("what_to_watch") or ""
    watch_html = f'<p class="gh-watch">{html.escape(watch)}</p>' if watch else ""

    lead_head = data.get("lead", {}).get("headline", "") or f"Edition of {date}"
    page = GHOST_EDITION_TEMPLATE.format(
        title=html.escape(f"{lead_head} — The Ghost of Times"),
        description=html.escape(data.get("lead", {}).get("dek", "") or "The Ghost of Times"),
        favicon=FAVICON, og_meta=OG_META,
        css=LIBRARY_CSS + GHOST_EDITION_CSS,
        folio_no=html.escape(folio_no),
        folio_date=html.escape(folio_date),
        folio_writers=html.escape(folio_writers),
        watch=watch_html,
        data_json=json_for_html(data),
        marked_js=MARKED_JS,
        app_js=GHOST_EDITION_JS,
        shell=shell,
    )
    (out / "ghost" / f"{date}-ghost-of-times.html").write_text(page)
    return True


# ---------------------------------------------------------------- the fingerprint
# A third top-level section: "The Fingerprint" — a daily, company-agnostic market
# paper on the global programmatic & Advanced TV (CTV) industry, produced by the
# fingerprint skill. Like the Ghost, each edition is rendered NATIVELY here from
# structured content (docs/fingerprint/data/{date}-fingerprint.json) so it inherits
# the site's design system — warm-paper tokens, Iowan/Georgia, dark mode, the
# masthead nav — rather than being shipped as a multi-megabyte print PDF.
#
# Design register: the Fingerprint's print identity is "Concrete Signal" — each
# editorial beat carries one saturated accent and one elementary-geometry sigil.
# The web edition keeps that signal vocabulary but is internet-native: a centred
# reading column in the site's measure, a sticky beat-navigator that tracks the
# scroll, mono datelines, accent-ruled story cards, inline SVG charts, and op-ed
# panels with engraved portraits. Unmistakably the same site as the corpus reader
# and the Ghost — but wearing a market-wire's coat instead of a newspaper's.

# Per-beat accent (light, dark) + sigil. The sigils are the skill's own
# "Concrete Signal" marks, re-rendered here with currentColor so the beat accent
# tints them and dark mode just works.
FP_BEATS = {
    "platform-watch":       ("#0d5b68", "#62aab8"),
    "performance-ctv":      ("#9c7414", "#d2ad3f"),
    "partnership-signals":  ("#b3502f", "#d98f5f"),
    "competitor-moves":     ("#2f6340", "#74ab84"),
    "regulatory-wire":      ("#7a2230", "#c77380"),
    "campaign-wire":        ("#2e4a78", "#7b9bd0"),
    "global-desk":          ("#4a3570", "#9a82c4"),
    "watercooler":          ("#8a5a2a", "#c79a63"),
    "wire-opinion-mcluhan": ("#355a6b", "#79a7ba"),
    "wire-opinion-ogilvy":  ("#6b4a2a", "#c39a6b"),
}

FP_MARKS = {
    "platform-watch":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
        '<circle cx="12" cy="14" r="1.4" fill="currentColor" stroke="none"/>'
        '<path d="M 6 14 A 6 6 0 0 1 18 14"/><path d="M 3 14 A 9 9 0 0 1 21 14"/>'
        '<line x1="12" y1="17" x2="12" y2="22"/></g></svg>',
    "performance-ctv":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="3,18 9,12 13,15 21,6"/><polyline points="16,6 21,6 21,11"/></g></svg>',
    "partnership-signals":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4">'
        '<circle cx="8" cy="12" r="4.2"/><circle cx="16" cy="12" r="4.2"/>'
        '<line x1="8" y1="12" x2="16" y2="12" stroke-width="1.6"/>'
        '<circle cx="8" cy="12" r="1.2" fill="currentColor" stroke="none"/>'
        '<circle cx="16" cy="12" r="1.2" fill="currentColor" stroke="none"/></g></svg>',
    "competitor-moves":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="3,5 11,12 3,19"/><polyline points="21,5 13,12 21,19"/>'
        '<line x1="11.5" y1="12" x2="12.5" y2="12" stroke-width="2"/></g></svg>',
    "regulatory-wire":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">'
        '<path d="M 6 4 L 4 4 L 4 20 L 6 20"/><path d="M 18 4 L 20 4 L 20 20 L 18 20"/>'
        '<line x1="9" y1="10" x2="15" y2="10"/><line x1="9" y1="14" x2="15" y2="14"/></g></svg>',
    "campaign-wire":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">'
        '<rect x="3.5" y="9" width="17" height="11" rx="0.6"/>'
        '<line x1="8" y1="9" x2="8" y2="6.5"/><line x1="16" y1="9" x2="16" y2="6.5"/>'
        '<line x1="7" y1="6.5" x2="17" y2="6.5" stroke-width="1.6"/>'
        '<polygon points="12,11 13.05,13.2 15.5,13.5 13.7,15.2 14.15,17.6 12,16.45 9.85,17.6 10.3,15.2 8.5,13.5 10.95,13.2" '
        'fill="currentColor" stroke="none"/></g></svg>',
    "global-desk":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.3">'
        '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4.5" ry="9"/>'
        '<line x1="3" y1="12" x2="21" y2="12"/>'
        '<path d="M 4.5 7 Q 12 9 19.5 7" stroke-width="1.1"/>'
        '<path d="M 4.5 17 Q 12 15 19.5 17" stroke-width="1.1"/></g></svg>',
    "watercooler":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="2"/>'
        '<circle cx="19" cy="12" r="1.3"/></g>'
        '<g fill="none" stroke="currentColor" stroke-width="0.9" opacity="0.55">'
        '<path d="M 6.5 12 Q 9 8 11 12"/><path d="M 13 12 Q 15.5 8 17.5 12"/></g></svg>',
    "wire-opinion-mcluhan":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4">'
        '<rect x="3" y="6" width="18" height="12" rx="1.2"/>'
        '<line x1="9" y1="20.5" x2="15" y2="20.5" stroke-width="1.6"/>'
        '<line x1="12" y1="18" x2="12" y2="20.5" stroke-width="1.6"/>'
        '<circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/></g></svg>',
    "wire-opinion-ogilvy":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M 5 19 L 5 7 L 12 7 L 14 9 L 19 9 L 19 19 Z"/>'
        '<line x1="5" y1="12" x2="19" y2="12"/></g></svg>',
}


def _fp_beat_css():
    """Emit each beat's --sec accent: light default, then dark-mode override.
    Targets the section block, the op-ed panel, and the jump-nav chip alike."""
    def sel(beat, prefix=""):
        return (f'{prefix}.fp-sec[data-beat="{beat}"],'
                f'{prefix}.fp-oped[data-beat="{beat}"],'
                f'{prefix}.fp-fp-sec[data-beat="{beat}"],'
                f'{prefix}.fp-jump a[data-beat="{beat}"]')
    dark_prefix = '[data-theme="dark"] '
    light = "\n".join(f'{sel(b)}{{--sec:{l};}}' for b, (l, d) in FP_BEATS.items())
    dark = "\n".join(f'{sel(b, dark_prefix)}{{--sec:{d};}}' for b, (l, d) in FP_BEATS.items())
    return light + "\n" + dark


def read_fingerprint_manifest(out_dir):
    """Load docs/fingerprint/manifest.json → list of editions, newest first."""
    path = Path(out_dir) / "fingerprint" / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, treating as no editions", file=sys.stderr)
        return []
    eds = data.get("editions", data) if isinstance(data, dict) else data
    eds = [e for e in eds if isinstance(e, dict) and e.get("date")]
    eds.sort(key=lambda e: (e.get("edition_number", 0), e.get("date", "")), reverse=True)
    return eds


def _fp_ed_href(ed):
    return html.escape(ed.get("file") or f"fingerprint/{ed.get('date','')}-fingerprint.html", quote=True)


def _fp_no_label(ed):
    no = ed.get("edition_number")
    return f"Nº {no:02d}" if isinstance(no, int) else "Nº —"


def fingerprint_band_html(editions, cfg):
    """The Fingerprint feature band for the home page — a market-wire sibling to
    the Ghost band, signed in petrol teal with a mono dispatch ticker."""
    motto = html.escape(cfg.get("motto", "All the news that's fit to Fingerprint"))
    blurb = html.escape(cfg.get("blurb", ""))
    flag = (f'<div class="fp-band-flag">The<br>Fingerprint'
            f'<small>{motto}</small></div>')
    if editions:
        latest = editions[0]
        no = latest.get("edition_number")
        kicker = "The Fingerprint" + (f" · Nº {no:02d}" if isinstance(no, int) else "")
        when = _weekday(latest.get("date", ""))
        when = f"{when} · {latest['date']}" if when else latest.get("date", "")
        lead = html.escape(latest.get("lead_headline") or "Latest edition")
        beats = latest.get("beats") or []
        ticker = " · ".join(html.escape(b) for b in beats[:6]) if beats else html.escape(when)
        sub = html.escape(when)
        cta = "Read the wire →"
    else:
        kicker = "A new section"
        lead = motto
        sub = blurb
        ticker = blurb
        cta = "Coming soon →"
    mid = (f'<div class="fp-band-mid"><p class="fp-band-kicker">{kicker}</p>'
           f'<p class="fp-band-lead">{lead}</p>'
           f'<p class="fp-band-ticker">{ticker}</p></div>')
    return (f'<div class="fp-band"><a href="fingerprint.html">{flag}{mid}'
            f'<span class="fp-band-cta">{cta}</span></a></div>')


# Home-page band CSS (added to LIBRARY_CSS at build time).
FINGERPRINT_BAND_CSS = """
/* The Fingerprint feature band — a market-wire card. Warm panel, petrol-teal
   signal accent, a mono dispatch ticker; a sibling to the inky Ghost band. */
.fp-band { max-width: 1080px; margin: 1.1rem auto 0; padding: 0 2rem; }
.fp-band a { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-left: 4px solid #0d5b68; border-radius: 18px;
  padding: 1.4rem 1.7rem; transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.fp-band a:hover { transform: translateY(-3px); box-shadow: 0 14px 36px rgba(0,0,0,.14); border-left-color: #11808f; }
[data-theme="dark"] .fp-band a { border-left-color: #62aab8; }
.fp-band .fp-band-flag { font-family: var(--display); font-weight: 800; font-size: 1.85rem; line-height: 1;
  letter-spacing: -.01em; color: var(--text); border-right: 1px solid var(--border); padding-right: 1.5rem; }
.fp-band .fp-band-flag small { display: block; font-family: var(--sans); font-size: .55rem; font-weight: 400;
  letter-spacing: .2em; text-transform: uppercase; color: #0d5b68; margin-top: .5rem; }
[data-theme="dark"] .fp-band .fp-band-flag small { color: #62aab8; }
.fp-band .fp-band-mid { min-width: 0; }
.fp-band .fp-band-kicker { font-family: var(--sans); font-size: .66rem; text-transform: uppercase;
  letter-spacing: .16em; color: #0d5b68; margin: 0 0 .35rem; }
[data-theme="dark"] .fp-band .fp-band-kicker { color: #62aab8; }
.fp-band .fp-band-lead { font-family: var(--display); font-size: 1.18rem; line-height: 1.28; margin: 0 0 .4rem; color: var(--text); }
.fp-band .fp-band-ticker { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .68rem;
  letter-spacing: .04em; color: var(--muted); margin: 0; text-transform: uppercase;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fp-band .fp-band-cta { font-family: var(--sans); font-size: .74rem; text-transform: uppercase; letter-spacing: .1em;
  color: #f3ead8; background: #0d5b68; border-radius: 12px; padding: .55rem 1rem; white-space: nowrap; }
[data-theme="dark"] .fp-band .fp-band-cta { background: #1d6f7d; }
@media (max-width: 680px) {
  .fp-band a { grid-template-columns: 1fr; gap: .8rem; }
  .fp-band .fp-band-flag { border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 .8rem; }
}
"""


# ---- the section front: fingerprint.html ----

FINGERPRINT_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Fingerprint — research · calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="index.html">The Research</a>
    <a href="ghost.html">The Ghost of Times</a>
    <a href="fingerprint.html" class="active">The Fingerprint</a>
  </nav>
</div>
<header class="fpp-plate">
  <p class="fpp-kicker">{kicker}</p>
  <h1 class="fpp-name">The Fingerprint</h1>
  <div class="fpp-ridges" aria-hidden="true">{ridges}</div>
  <p class="fpp-motto">“{motto}”</p>
  <div class="fpp-folio">
    <span>Vol. 1</span>
    <span class="fpp-folio-c">{stats}</span>
    <span>Filed daily</span>
  </div>
</header>
<main class="fped-wrap">
{editions}
</main>
<footer class="fpp-foot">
  <p class="epigraph">{blurb}</p>
  <p class="colophon"><a href="index.html">← Back to the Research Library</a></p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

# A concentric-arc "broadcast ridge" ornament — the platform sigil scaled up, the
# nearest thing the Concrete Signal vocabulary has to a fingerprint's ridges.
FP_RIDGES = (
    '<svg viewBox="0 0 240 60" xmlns="http://www.w3.org/2000/svg" class="fpp-ridge-svg">'
    '<g fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round">'
    '<circle cx="120" cy="54" r="2" fill="currentColor" stroke="none"/>'
    '<path d="M 104 54 A 16 16 0 0 1 136 54"/>'
    '<path d="M 92 54 A 28 28 0 0 1 148 54"/>'
    '<path d="M 80 54 A 40 40 0 0 1 160 54"/>'
    '<path d="M 68 54 A 52 52 0 0 1 172 54"/>'
    '<path d="M 56 54 A 64 64 0 0 1 184 54"/>'
    '</g></svg>'
)

FINGERPRINT_PAGE_CSS = """
/* The Fingerprint section front — a market-paper nameplate that shares the
   site's warm-paper/serif language, signed in petrol teal. */
.fpp-plate { display: block; max-width: 820px; margin: 1.8rem auto 0; padding: 0 2rem; text-align: center; }
.fpp-kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .2em; color: #0d5b68; margin: 0 0 .5rem; }
[data-theme="dark"] .fpp-kicker { color: #62aab8; }
.fpp-name { font-family: var(--display); font-weight: 800; font-size: clamp(2.6rem, 6.8vw, 4.6rem);
  line-height: .98; letter-spacing: -.02em; margin: 0 0 .3rem; }
.fpp-ridges { color: #0d5b68; opacity: .8; margin: 0 0 .6rem; }
[data-theme="dark"] .fpp-ridges { color: #62aab8; }
.fpp-ridge-svg { width: 200px; height: 50px; }
.fpp-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.fpp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .14em; color: var(--text); }
.fpp-folio .fpp-folio-c { color: #0d5b68; font-weight: 700; }
[data-theme="dark"] .fpp-folio .fpp-folio-c { color: #62aab8; }

/* featured (latest) edition — a section lead */
.fped-feature { max-width: 720px; margin: 2.1rem auto 0; padding: 0 2rem; }
.fped-feature a { display: block; text-decoration: none; color: var(--text); }
.fpedf-meta { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .72rem; letter-spacing: .04em;
  text-transform: uppercase; color: #0d5b68; margin: 0 0 .7rem; }
[data-theme="dark"] .fpedf-meta { color: #62aab8; }
.fpedf-head { font-family: var(--display); font-weight: 800; font-size: clamp(1.9rem, 4.2vw, 2.7rem);
  line-height: 1.07; letter-spacing: -.01em; margin: 0 0 .7rem; transition: color .15s ease; }
.fped-feature a:hover .fpedf-head { color: #0d5b68; }
[data-theme="dark"] .fped-feature a:hover .fpedf-head { color: #62aab8; }
.fpedf-dek { font-family: var(--serif); font-style: italic; font-size: 1.2rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 .9rem; }
.fpedf-beats { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .7rem; letter-spacing: .02em;
  text-transform: uppercase; color: var(--muted); margin: 0 0 1.1rem; }
.fpedf-cta { font-family: var(--sans); font-size: .78rem; text-transform: uppercase; letter-spacing: .1em;
  color: #0d5b68; border-bottom: 1.5px solid #0d5b68; padding-bottom: 2px; }
[data-theme="dark"] .fpedf-cta { color: #62aab8; border-bottom-color: #62aab8; }

/* back issues */
.fped-issues { max-width: 720px; margin: 2.8rem auto 0; padding: 0 2rem 1rem; }
.fped-issues-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .3rem; }
.fped-row { display: grid; grid-template-columns: auto 1fr auto; gap: 1.2rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .95rem 0; border-bottom: 1px solid var(--border); }
.fped-row-no { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .9rem; color: #0d5b68; white-space: nowrap; }
[data-theme="dark"] .fped-row-no { color: #62aab8; }
.fped-row-body { min-width: 0; }
.fped-row-head { font-family: var(--display); font-size: 1.18rem; line-height: 1.18; display: block; transition: color .15s ease; }
.fped-row:hover .fped-row-head { color: #0d5b68; }
[data-theme="dark"] .fped-row:hover .fped-row-head { color: #62aab8; }
.fped-row-meta { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .66rem; letter-spacing: .02em;
  text-transform: uppercase; color: var(--muted); display: block; margin-top: .25rem; }
.fped-row-date { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); white-space: nowrap; }
.fped-empty { max-width: 720px; margin: 2.1rem auto 0; padding: 2rem; text-align: center;
  color: var(--muted); font-family: var(--sans); font-size: .9rem; border-top: 3px double var(--border); }

.fpp-foot { max-width: 720px; margin: 3rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.fpp-foot .epigraph { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .95rem; margin: 0 0 .6rem; }
.fpp-foot .colophon { font-family: var(--sans); font-size: .74rem; margin: 0; }
.fpp-foot .colophon a { color: var(--accent); text-decoration: none; }
.fpp-foot .colophon a:hover { text-decoration: underline; }

@media (max-width: 560px) {
  .fpp-folio { font-size: .58rem; letter-spacing: .08em; }
  .fped-row { grid-template-columns: auto 1fr; gap: .8rem; }
  .fped-row-date { display: none; }
}
"""


def _fp_beats_line(ed):
    beats = ed.get("beats") or []
    return " · ".join(html.escape(b) for b in beats) if beats else ""


def fingerprint_feature_html(ed):
    """The latest edition, rendered like a section lead on fingerprint.html."""
    meta = " · ".join(x for x in ["Latest edition", _fp_no_label(ed), _long_date(ed.get("date", ""))] if x)
    headline = html.escape(ed.get("lead_headline") or f"Edition of {ed.get('date','')}")
    dek = html.escape(ed.get("lead_dek") or "")
    dek_html = f'<p class="fpedf-dek">{dek}</p>' if dek else ""
    beats = _fp_beats_line(ed)
    beats_html = f'<p class="fpedf-beats">{beats}</p>' if beats else ""
    return (f'<article class="fped-feature"><a href="{_fp_ed_href(ed)}">'
            f'<p class="fpedf-meta">{html.escape(meta)}</p>'
            f'<h2 class="fpedf-head">{headline}</h2>{dek_html}{beats_html}'
            f'<span class="fpedf-cta">Read the edition →</span></a></article>')


def fingerprint_row_html(ed):
    """A back-issue row in the Fingerprint archive list."""
    when = _weekday(ed.get("date", ""))
    when_s = f"{when} · {ed['date']}" if when else ed.get("date", "")
    headline = html.escape(ed.get("lead_headline") or f"Edition of {ed.get('date','')}")
    dispatches = ed.get("dispatches")
    meta = f"{dispatches} dispatches" if isinstance(dispatches, int) else _fp_beats_line(ed)
    meta_html = f'<span class="fped-row-meta">{html.escape(meta)}</span>' if meta else ""
    return (f'<a class="fped-row" href="{_fp_ed_href(ed)}">'
            f'<span class="fped-row-no">{_fp_no_label(ed)}</span>'
            f'<span class="fped-row-body"><span class="fped-row-head">{headline}</span>{meta_html}</span>'
            f'<span class="fped-row-date">{html.escape(when_s)}</span></a>')


def build_fingerprint_page(out_dir, editions, cfg, shell=""):
    """Render docs/fingerprint.html — the section front."""
    out = Path(out_dir)
    if editions:
        n = len(editions)
        stats = f"{n} edition{'s' if n != 1 else ''}"
        body = fingerprint_feature_html(editions[0])
        rest = editions[1:]
        if rest:
            rows = "\n".join(fingerprint_row_html(e) for e in rest)
            body += f'<section class="fped-issues"><h2 class="fped-issues-h">Back issues</h2>{rows}</section>'
    else:
        body = ('<p class="fped-empty">No editions published yet. Run the Fingerprint '
                'skill and publish an edition to see it here.</p>')
        stats = "No editions yet"
    page = FINGERPRINT_PAGE_TEMPLATE.format(
        css=LIBRARY_CSS + FINGERPRINT_BAND_CSS + FINGERPRINT_PAGE_CSS,
        favicon=FAVICON, og_meta=OG_META,
        kicker=html.escape(cfg.get("kicker", "The global programmatic & Advanced TV market paper")),
        ridges=FP_RIDGES,
        motto=html.escape(cfg.get("motto", "All the news that's fit to Fingerprint")),
        blurb=html.escape(cfg.get("blurb", "")),
        stats=stats,
        editions=body,
        theme_js=LIBRARY_THEME_JS,
        shell=shell,
    )
    (out / "fingerprint.html").write_text(page)
    print(f"  ✓ The Fingerprint  ({len(editions)} editions) → fingerprint.html")


# ---- a single edition page, rendered natively (internet-native wire) ----

FINGERPRINT_EDITION_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div id="fp-progress" aria-hidden="true"></div>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="../index.html">The Research</a>
    <a href="../ghost.html">The Ghost of Times</a>
    <a href="../fingerprint.html" class="active">The Fingerprint</a>
  </nav>
</div>
<main class="fp-edition">
  <header class="fp-nameplate">
    <p class="fp-np-kicker">The global programmatic &amp; Advanced TV market paper</p>
    <a class="fp-np-name" href="../fingerprint.html">The Fingerprint</a>
    <div class="fp-folio">
      <span>{folio_no}</span>
      <span class="fp-folio-c">{folio_date}</span>
      <span>{folio_meta}</span>
    </div>
  </header>
  <nav class="fp-jump" id="fp-jump" aria-label="Sections"></nav>
  <section id="fp-frontpage" class="fp-frontpage" aria-label="In this edition"></section>
  <section id="fp-lead" class="fp-lead-wrap"></section>
  <div id="fp-body"></div>
  <footer class="fp-foot">
    {watch}
    <p class="fp-colophon"><a href="../fingerprint.html">← All editions</a> &nbsp;·&nbsp; <a href="../index.html">The Research Library</a></p>
  </footer>
</main>
<button id="theme-btn" title="Light / dark">◐ Theme</button>
<script id="fp-edition-data" type="application/json">{data_json}</script>
<script id="fp-marks" type="application/json">{marks_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

FINGERPRINT_EDITION_CSS = """
/* A single Fingerprint edition — the site's reading column wearing a market-wire's
   coat. Every colour/font token is shared with the rest of the site; each beat adds
   its own --sec accent. */
.fp-edition { max-width: 880px; margin: 0 auto; padding: 1.4rem 2rem 1rem; }

/* reading-progress bar + theme button — same affordances as the corpus reader */
#fp-progress { position: fixed; top: 0; left: 0; height: 3px; width: 0;
  background: linear-gradient(90deg, var(--t1), var(--t2), var(--t3), var(--t4)); z-index: 50; transition: width .1s linear; }
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: .4rem .7rem; cursor: pointer; }
#theme-btn:hover { color: var(--accent); border-color: var(--accent); }

/* nameplate */
.fp-nameplate { display: block; text-align: center; margin: .6rem 0 1.4rem; }
.fp-np-kicker { font-family: var(--sans); font-size: .66rem; text-transform: uppercase;
  letter-spacing: .2em; color: #0d5b68; margin: 0 0 .4rem; }
[data-theme="dark"] .fp-np-kicker { color: #62aab8; }
.fp-np-name { display: inline-block; font-family: var(--display); font-weight: 800;
  font-size: clamp(2.1rem, 5.4vw, 3.2rem); line-height: .98; letter-spacing: -.02em;
  color: var(--text); text-decoration: none; margin: 0 0 .9rem; }
.fp-np-name:hover { color: #0d5b68; }
[data-theme="dark"] .fp-np-name:hover { color: #62aab8; }
.fp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .5rem 0;
  font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .64rem; text-transform: uppercase; letter-spacing: .1em; }
.fp-folio .fp-folio-c { color: #0d5b68; font-weight: 700; }
[data-theme="dark"] .fp-folio .fp-folio-c { color: #62aab8; }

/* sticky beat-navigator — internet-native wayfinding */
.fp-jump { position: sticky; top: 0; z-index: 15; display: flex; gap: .3rem; overflow-x: auto;
  margin: 0 -2rem 2rem; padding: .55rem 2rem; background: color-mix(in srgb, var(--bg) 86%, transparent);
  backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); scrollbar-width: none; }
.fp-jump::-webkit-scrollbar { display: none; }
.fp-jump a { display: inline-flex; align-items: center; gap: .42rem; white-space: nowrap; text-decoration: none;
  font-family: var(--sans); font-size: .7rem; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); padding: .32rem .6rem; border-radius: 8px; transition: color .15s, background .15s; }
.fp-jump a::before { content: ""; width: 8px; height: 8px; border-radius: 2px; background: var(--sec, var(--muted)); flex: none; }
.fp-jump a:hover { color: var(--text); background: var(--panel); }
.fp-jump a.active { color: var(--text); background: var(--panel); box-shadow: inset 0 0 0 1px var(--border); }

/* front-page index — every article listed under its section, as a newspaper
   contents box. Flows in columns; each block tinted by its beat accent. */
.fp-frontpage { margin: 0 0 2.6rem; }
.fp-fp-h { font-family: var(--sans); font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .22em;
  color: var(--muted); text-align: center; border-top: 1px solid var(--text); border-bottom: 1px solid var(--text);
  padding: .5rem 0; margin: 0 0 1.5rem; }
.fp-fp-grid { columns: 2; column-gap: 2.4rem; }
.fp-fp-sec { break-inside: avoid; -webkit-column-break-inside: avoid; margin: 0 0 1.3rem; }
.fp-fp-head { display: flex; align-items: center; gap: .45rem; margin: 0 0 .5rem; padding-bottom: .3rem;
  border-bottom: 1px solid var(--sec); }
.fp-fp-sigil { width: 15px; height: 15px; color: var(--sec); flex: none; }
.fp-fp-sigil svg { width: 100%; height: 100%; display: block; }
.fp-fp-title { font-family: var(--sans); font-size: .67rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .11em; color: var(--sec); }
.fp-fp-list { list-style: none; margin: 0; padding: 0; }
.fp-fp-list li { margin: 0 0 .4rem; line-height: 1.28; }
.fp-fp-list a { font-family: var(--display); font-size: .98rem; color: var(--text); text-decoration: none;
  border-bottom: 1px solid transparent; transition: color .12s ease; }
.fp-fp-list a:hover { color: var(--sec); border-bottom-color: var(--sec); }
.fp-fp-by { font-family: var(--sans); font-size: .68rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }

/* the Above-the-Fold lead */
.fp-lead { margin: 0 0 1rem; }
.fp-kicker { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .68rem; text-transform: uppercase;
  letter-spacing: .12em; color: #0d5b68; margin: 0 0 .55rem; }
[data-theme="dark"] .fp-kicker { color: #62aab8; }
.fp-lead-head { font-family: var(--display); font-weight: 800; letter-spacing: -.015em; line-height: 1.05;
  font-size: clamp(2rem, 5vw, 3rem); margin: 0 0 .6rem; }
.fp-lead-head::after { content: ""; display: block; height: 4px; width: 120px; margin-top: .7rem; border-radius: 2px;
  background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
.fp-lead-dek { font-family: var(--serif); font-style: italic; font-size: 1.3rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 1rem; }
.fp-lead-body { font-size: 1.08rem; line-height: 1.75; }
.fp-lead-body > p:first-of-type::first-letter { font-family: var(--display); font-weight: 800;
  float: left; font-size: 3.1em; line-height: .72; padding: .06em .1em 0 0; color: #0d5b68; }
[data-theme="dark"] .fp-lead-body > p:first-of-type::first-letter { color: #62aab8; }
.fp-lead-wrap { border-bottom: 3px double var(--border); padding-bottom: 1.6rem; margin-bottom: 2.4rem; }

/* a beat section */
.fp-sec { margin: 2.6rem 0 0; scroll-margin-top: 4rem; }
.fp-sec-head { display: flex; align-items: center; gap: .6rem; margin: 0 0 1.1rem;
  border-bottom: 2px solid var(--sec); padding-bottom: .5rem; }
.fp-sec-sigil { width: 22px; height: 22px; color: var(--sec); flex: none; }
.fp-sec-sigil svg { width: 100%; height: 100%; display: block; }
.fp-sec-title { font-family: var(--sans); font-size: .8rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; color: var(--sec); margin: 0; }

/* a story card */
.fp-story { border-left: 3px solid var(--sec); padding: 0 0 0 1.1rem; margin: 0 0 1.7rem; scroll-margin-top: 4rem; }
.fp-story:last-child { margin-bottom: 0; }
.fp-story-head { font-family: var(--display); font-weight: 700; font-size: 1.3rem; line-height: 1.22;
  letter-spacing: -.01em; margin: 0 0 .35rem; }
.fp-story-dek { font-family: var(--serif); font-style: italic; font-size: 1.02rem; line-height: 1.42;
  color: var(--muted); margin: 0 0 .55rem; }
.fp-story-body { font-size: 1.0rem; line-height: 1.68; margin: 0 0 .5rem; }
.fp-story-body p { margin: 0 0 .7rem; }
.fp-src { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .68rem; letter-spacing: .02em;
  color: var(--muted); margin: .2rem 0 0; }
.fp-src a { color: var(--sec); text-decoration: none; border-bottom: 1px solid transparent; }
.fp-src a:hover { border-bottom-color: var(--sec); }
.fp-src .fp-filed { text-transform: uppercase; }

/* inline newspaper-style chart */
.fp-chart { margin: .8rem 0 .9rem; padding: .9rem 1rem; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; }
.fp-chart-title { font-family: var(--sans); font-size: .76rem; font-weight: 700; color: var(--text); margin: 0 0 .5rem; }
.fp-chart svg { width: 100%; height: auto; display: block; }
.fp-chart-src { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .62rem; color: var(--muted); margin: .4rem 0 0; }
.fp-chart text { font-family: var(--sans); fill: var(--muted); }
.fp-chart .fp-stat-num { font-family: var(--display); font-weight: 800; fill: var(--sec); }

/* op-ed panel */
.fp-oped { display: grid; grid-template-columns: 132px 1fr; gap: 1.4rem; align-items: start;
  background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--sec);
  border-radius: 16px; padding: 1.5rem 1.6rem; margin: 2.4rem 0 0; scroll-margin-top: 4rem; }
.fp-oped-portrait { text-align: center; }
.fp-oped-portrait img { width: 112px; height: 112px; object-fit: cover; border-radius: 50%;
  border: 2px solid var(--sec); display: block; margin: 0 auto .5rem; filter: grayscale(.15); }
.fp-oped-cap { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.fp-oped-kicker { font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; color: var(--sec); margin: 0 0 .4rem; }
.fp-oped-head { font-family: var(--display); font-weight: 800; font-size: 1.5rem; line-height: 1.15; margin: 0 0 .45rem; }
.fp-oped-dek { font-family: var(--serif); font-style: italic; font-size: 1.08rem; line-height: 1.4; color: var(--muted); margin: 0 0 .8rem; }
.fp-oped-body { font-size: 1.02rem; line-height: 1.72; }
.fp-oped-body p { margin: 0 0 .9rem; }
.fp-oped-body em { font-style: italic; }
.fp-oped-body p:last-child { color: var(--muted); font-style: italic; }

/* foot */
.fp-foot { margin: 3rem 0 0; padding: 1.5rem 0 3rem; border-top: 1px solid var(--border); text-align: center; }
.fp-watch { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: 1.02rem; margin: 0 auto .8rem; max-width: 40rem; }
.fp-watch b { font-family: var(--sans); font-style: normal; font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; color: #0d5b68; display: block; margin-bottom: .35rem; }
[data-theme="dark"] .fp-watch b { color: #62aab8; }
.fp-colophon { font-family: var(--sans); font-size: .74rem; color: var(--muted); margin: 0; }
.fp-colophon a { color: var(--accent); text-decoration: none; }
.fp-colophon a:hover { text-decoration: underline; }

@media (max-width: 620px) {
  .fp-edition { padding: 1.2rem 1.2rem 1rem; }
  .fp-fp-grid { columns: 1; }
  .fp-jump { margin: 0 -1.2rem 1.6rem; padding: .5rem 1.2rem; }
  .fp-folio { font-size: .56rem; letter-spacing: .06em; }
  .fp-oped { grid-template-columns: 1fr; gap: .9rem; }
  .fp-oped-portrait img { width: 84px; height: 84px; }
}
__FP_BEAT_CSS__
"""

FINGERPRINT_EDITION_JS = r"""
// theme (shares the 'corpus-theme' preference with readers + library)
const pref = localStorage.getItem('corpus-theme');
if (pref) document.documentElement.dataset.theme = pref;
else if (matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.dataset.theme = 'dark';
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

const ed = JSON.parse(document.getElementById('fp-edition-data').textContent);
const MARKS = JSON.parse(document.getElementById('fp-marks').textContent);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls;
  if (html != null) n.innerHTML = html; return n; };
const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
const openExternal = (root) => root.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });

// ---- source / dateline line ----
function srcLine(s, beat) {
  if (!s.source_publication && !s.source_url) return '';
  const name = esc(s.source_publication || s.source_url);
  const link = s.source_url ? `<a href="${esc(s.source_url)}">${name}</a>` : name;
  const filed = s.filed_date ? ` · <span class="fp-filed">Filed ${esc(s.filed_date)}</span>` : '';
  return `<p class="fp-src">${link}${filed}</p>`;
}

// ---- charts (bar | line | donut | stat) ----
function renderChart(c) {
  if (!c || !c.type) return null;
  const wrap = el('div', 'fp-chart');
  if (c.title) wrap.appendChild(el('p', 'fp-chart-title', esc(c.title)));
  let svg = '';
  const W = 520;
  if (c.type === 'stat') {
    svg = `<svg viewBox="0 0 ${W} 120"><text class="fp-stat-num" x="12" y="74" font-size="58">${esc(c.value||'')}</text>`
        + `<text x="14" y="104" font-size="15">${esc(c.caption||'')}</text></svg>`;
  } else if (c.type === 'bar') {
    const data = c.data || []; const max = Math.max(1, ...data.map(d => +d.value || 0));
    const rh = 30, gap = 12, H = data.length * (rh + gap) + 10;
    const rows = data.map((d, i) => {
      const y = 8 + i * (rh + gap), w = (((+d.value||0) / max) * (W - 180));
      return `<text x="0" y="${y+20}" font-size="13">${esc(d.label||'')}</text>`
           + `<rect x="150" y="${y}" width="${w.toFixed(1)}" height="${rh}" rx="3" fill="var(--sec)" opacity="0.85"/>`
           + `<text x="${(150+w+6).toFixed(1)}" y="${y+20}" font-size="13" fill="var(--text)">${esc((d.value)+(c.unit||''))}</text>`;
    }).join('');
    svg = `<svg viewBox="0 0 ${W} ${H}">${rows}</svg>`;
  } else if (c.type === 'line') {
    const data = c.data || []; const ys = data.map(d => +d.y || 0);
    const max = Math.max(1, ...ys), min = Math.min(0, ...ys), H = 200, pad = 32;
    const x = i => pad + i * ((W - pad*2) / Math.max(1, data.length - 1));
    const y = v => (H - pad) - ((v - min) / (max - min || 1)) * (H - pad*2);
    const pts = data.map((d, i) => `${x(i).toFixed(1)},${y(+d.y||0).toFixed(1)}`).join(' ');
    const dots = data.map((d, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(+d.y||0).toFixed(1)}" r="3.2" fill="var(--sec)"/>`
      + `<text x="${x(i).toFixed(1)}" y="${H-8}" font-size="11" text-anchor="middle">${esc(d.x||'')}</text>`).join('');
    svg = `<svg viewBox="0 0 ${W} ${H}"><polyline points="${pts}" fill="none" stroke="var(--sec)" stroke-width="2.2" stroke-linejoin="round"/>${dots}</svg>`;
  } else if (c.type === 'donut') {
    const data = c.data || []; const total = data.reduce((a, d) => a + (+d.value || 0), 0) || 1;
    const cx = 100, cy = 100, r = 70, sw = 30; let a0 = -Math.PI / 2; const segs = [];
    const palette = ['var(--sec)', 'var(--t2)', 'var(--t3)', 'var(--t4)', 'var(--muted)'];
    data.forEach((d, i) => {
      const frac = (+d.value || 0) / total, a1 = a0 + frac * 2 * Math.PI;
      const large = frac > 0.5 ? 1 : 0;
      const x0 = cx + r*Math.cos(a0), y0 = cy + r*Math.sin(a0), x1 = cx + r*Math.cos(a1), y1 = cy + r*Math.sin(a1);
      segs.push(`<path d="M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}" fill="none" stroke="${palette[i%palette.length]}" stroke-width="${sw}"/>`);
      a0 = a1;
    });
    const legend = data.map((d, i) => `<text x="210" y="${40 + i*22}" font-size="13"><tspan fill="${palette[i%palette.length]}">■</tspan> ${esc(d.label||'')} — ${esc((d.value)+(c.unit||''))}</text>`).join('');
    svg = `<svg viewBox="0 0 ${W} 200">${segs.join('')}${legend}</svg>`;
  }
  wrap.innerHTML += svg;
  if (c.source) wrap.appendChild(el('p', 'fp-chart-src', 'Source: ' + esc(c.source)));
  return wrap;
}

// ---- lead ----
(function renderLead() {
  const p = ed.lead; if (!p) return;
  const host = document.getElementById('fp-lead');
  const art = el('article', 'fp-lead');
  art.appendChild(el('p', 'fp-kicker', esc(p.kicker || 'Above the Fold')));
  art.appendChild(el('h1', 'fp-lead-head', esc(p.headline || '')));
  if (p.dek) art.appendChild(el('p', 'fp-lead-dek', esc(p.dek)));
  const body = el('div', 'fp-lead-body', marked.parse(p.body || '')); openExternal(body);
  art.appendChild(body);
  if (p.chart) { const ch = renderChart(p.chart); if (ch) art.appendChild(ch); }
  art.insertAdjacentHTML('beforeend', srcLine(p));
  host.appendChild(art);
})();

// ---- group sections for the jump-nav (collapse Global Desk + Opinion) ----
function navGroup(sec) {
  const c = sec.section_class || '';
  if (c === 'global-desk') return { key: 'global-desk', label: 'Global Desk', beat: 'global-desk' };
  if (c.indexOf('wire-opinion') === 0) return { key: 'opinion', label: 'Opinion', beat: 'wire-opinion-mcluhan' };
  return { key: c || sec.section_title, label: sec.section_title, beat: c };
}

const sections = ed.sections || [];
const body = document.getElementById('fp-body');
const navItems = []; const seen = {};

sections.forEach((sec, i) => {
  const beat = sec.section_class || '';
  const isOped = beat.indexOf('wire-opinion') === 0;
  const id = 'sec-' + i;
  const g = navGroup(sec);
  if (!seen[g.key]) { seen[g.key] = true; navItems.push({ id, label: g.label, beat: g.beat }); }

  if (isOped) {
    const s = (sec.stories || [])[0] || {};
    const art = el('article', 'fp-oped'); art.id = id; art.dataset.beat = beat;
    const portrait = s.portrait ? `assets/${esc(s.portrait)}.png` : '';
    const pho = portrait
      ? `<div class="fp-oped-portrait"><img src="${portrait}" alt="${esc(s.portrait_caption||'')}"><div class="fp-oped-cap">${esc(s.portrait_caption||'')}</div></div>`
      : '';
    const txt = el('div', 'fp-oped-text');
    txt.appendChild(el('p', 'fp-oped-kicker', esc(sec.section_title || 'Wire Opinion')));
    txt.appendChild(el('h2', 'fp-oped-head', esc(s.headline || '')));
    if (s.dek) txt.appendChild(el('p', 'fp-oped-dek', esc(s.dek)));
    const ob = el('div', 'fp-oped-body', marked.parse(s.body || '')); openExternal(ob);
    txt.appendChild(ob);
    art.innerHTML = pho; art.appendChild(txt);
    body.appendChild(art);
    return;
  }

  const sec_el = el('section', 'fp-sec'); sec_el.id = id; sec_el.dataset.beat = beat;
  const head = el('div', 'fp-sec-head');
  head.appendChild(el('span', 'fp-sec-sigil', MARKS[beat] || ''));
  head.appendChild(el('h2', 'fp-sec-title', esc(sec.section_title || '')));
  sec_el.appendChild(head);
  (sec.stories || []).forEach((s, j) => {
    const st = el('article', 'fp-story'); st.id = 'art-' + i + '-' + j;
    st.appendChild(el('h3', 'fp-story-head', esc(s.headline || '')));
    if (s.dek) st.appendChild(el('p', 'fp-story-dek', esc(s.dek)));
    const sb = el('div', 'fp-story-body', marked.parse(s.body || '')); openExternal(sb);
    st.appendChild(sb);
    if (s.chart) { const ch = renderChart(s.chart); if (ch) st.appendChild(ch); }
    st.insertAdjacentHTML('beforeend', srcLine(s, beat));
    sec_el.appendChild(st);
  });
  body.appendChild(sec_el);
});

// ---- front-page index: every article listed under its section ----
(function buildFrontPage() {
  const host = document.getElementById('fp-frontpage');
  if (!host) return;
  const blocks = [];
  if (ed.lead) blocks.push({ beat: 'partnership-signals', title: 'Above the Fold',
    items: [{ headline: ed.lead.headline || '', href: '#fp-lead' }] });
  sections.forEach((sec, i) => {
    const beat = sec.section_class || '';
    const isOped = beat.indexOf('wire-opinion') === 0;
    const items = (sec.stories || []).map((s, j) => ({
      headline: s.headline || '',
      by: isOped ? (s.portrait_caption || '') : '',
      href: isOped ? ('#sec-' + i) : ('#art-' + i + '-' + j),
    }));
    blocks.push({ beat, title: sec.section_title || '', items });
  });
  host.appendChild(el('h2', 'fp-fp-h', 'In This Edition'));
  const grid = el('div', 'fp-fp-grid');
  blocks.forEach(b => {
    const block = el('div', 'fp-fp-sec'); block.dataset.beat = b.beat;
    const head = el('div', 'fp-fp-head');
    head.appendChild(el('span', 'fp-fp-sigil', MARKS[b.beat] || ''));
    head.appendChild(el('span', 'fp-fp-title', esc(b.title)));
    block.appendChild(head);
    const ul = el('ul', 'fp-fp-list');
    b.items.forEach(it => {
      const li = document.createElement('li');
      const by = it.by ? ` <span class="fp-fp-by">— ${esc(it.by)}</span>` : '';
      const a = el('a', null, esc(it.headline) + by);
      a.href = it.href;
      a.onclick = (e) => { e.preventDefault();
        const t = document.querySelector(it.href); if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
      li.appendChild(a); ul.appendChild(li);
    });
    block.appendChild(ul);
    grid.appendChild(block);
  });
  host.appendChild(grid);
})();

// ---- build the jump-nav (Above the Fold first) ----
(function buildNav() {
  const nav = document.getElementById('fp-jump');
  const items = [{ id: 'fp-lead', label: 'Above the Fold', beat: 'partnership-signals' }].concat(navItems);
  items.forEach(it => {
    const a = el('a', null, esc(it.label));
    a.href = '#' + it.id; a.dataset.target = it.id; if (it.beat) a.dataset.beat = it.beat;
    a.onclick = (e) => { e.preventDefault(); document.getElementById(it.id).scrollIntoView({ behavior: 'smooth', block: 'start' }); };
    nav.appendChild(a);
  });
  // active-section tracking
  const links = [...nav.querySelectorAll('a')];
  const byId = {}; links.forEach(a => byId[a.dataset.target] = a);
  const targets = items.map(it => document.getElementById(it.id)).filter(Boolean);
  const io = new IntersectionObserver((entries) => {
    entries.forEach(en => {
      if (en.isIntersecting) {
        links.forEach(a => a.classList.remove('active'));
        const a = byId[en.target.id]; if (a) { a.classList.add('active'); a.scrollIntoView({ block: 'nearest', inline: 'center' }); }
      }
    });
  }, { rootMargin: '-20% 0px -70% 0px', threshold: 0 });
  targets.forEach(t => io.observe(t));
})();

// ---- reading-progress bar ----
const bar = document.getElementById('fp-progress');
const onScroll = () => {
  const h = document.documentElement; const max = h.scrollHeight - h.clientHeight;
  bar.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + '%';
};
document.addEventListener('scroll', onScroll, { passive: true }); onScroll();

// honor a deep-link hash (#sec-N / #fp-lead) now that the sections exist
if (location.hash.length > 1) {
  const t = document.getElementById(location.hash.slice(1));
  if (t) requestAnimationFrame(() => t.scrollIntoView());
}
"""


def read_fingerprint_edition_data(out_dir, date):
    """Load one edition's structured content from docs/fingerprint/data/{date}-fingerprint.json."""
    path = Path(out_dir) / "fingerprint" / "data" / f"{date}-fingerprint.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read edition data {path}", file=sys.stderr)
        return None


def _fp_dispatch_count(data):
    """Lead + every non-opinion story; opinion columns counted separately."""
    n = 1 if data.get("lead") else 0
    cols = 0
    for sec in data.get("sections", []):
        if str(sec.get("section_class", "")).startswith("wire-opinion"):
            cols += len(sec.get("stories", []))
        else:
            n += len(sec.get("stories", []))
    return n, cols


def build_fingerprint_edition(out_dir, ed, shell=""):
    """Render one edition page (docs/fingerprint/{date}-fingerprint.html) from its data file.

    Returns True if rendered, False if the data file is absent."""
    out = Path(out_dir)
    date = ed.get("date", "")
    data = read_fingerprint_edition_data(out, date)
    if data is None:
        return False

    no = ed.get("edition_number", data.get("edition_number"))
    folio_no = f"Nº {no:02d}" if isinstance(no, int) else "Nº —"
    folio_date = _long_date(date)
    dispatches, columns = _fp_dispatch_count(data)
    meta_bits = [f"{dispatches} dispatches"]
    if columns:
        meta_bits.append(f"{columns} columns")
    folio_meta = " · ".join(meta_bits)

    watch = data.get("what_to_watch") or ""
    watch_html = f'<p class="fp-watch"><b>What to watch</b>{html.escape(watch)}</p>' if watch else ""

    lead_head = data.get("lead", {}).get("headline", "") or f"Edition of {date}"
    css = (LIBRARY_CSS + FINGERPRINT_EDITION_CSS).replace("__FP_BEAT_CSS__", _fp_beat_css())
    page = FINGERPRINT_EDITION_TEMPLATE.format(
        title=html.escape(f"{lead_head} — The Fingerprint"),
        description=html.escape(data.get("lead", {}).get("dek", "") or "The Fingerprint"),
        favicon=FAVICON, og_meta=OG_META,
        css=css,
        folio_no=html.escape(folio_no),
        folio_date=html.escape(folio_date),
        folio_meta=html.escape(folio_meta),
        watch=watch_html,
        data_json=json_for_html(data),
        marks_json=json_for_html(FP_MARKS),
        marked_js=MARKED_JS,
        app_js=FINGERPRINT_EDITION_JS,
        shell=shell,
    )
    (out / "fingerprint" / f"{date}-fingerprint.html").write_text(page)
    return True


# ---------------------------------------------------------------- connections
# A "map of ideas": an interactive SVG knowledge graph of how the corpora relate,
# laid out at build time from the same similarity graph that powers related-reading.
# Categories sit in a ring; nodes cluster by category; edges are strong similarities.
# Hover/focus a node to light its links and neighbours; click to open the corpus.

CONNECTIONS_CSS = """
.cx-wrap { max-width: 1120px; margin: 0 auto; padding: 1.2rem 2rem 3rem; }
.cx-head { text-align: center; margin: 1.4rem 0 .2rem; }
.cx-head .kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em; color: var(--accent); margin: 0 0 .5rem; }
.cx-head h1 { font-family: var(--display); font-size: clamp(2rem, 5vw, 3rem); line-height: 1.05; margin: 0; }
.cx-head p { font-family: var(--sans); color: var(--muted); font-size: .9rem; line-height: 1.5; margin: .55rem auto 0; max-width: 38rem; }
.cx-legend { display: flex; flex-wrap: wrap; gap: .5rem 1.1rem; justify-content: center; margin: 1.1rem 0 1rem;
  font-family: var(--sans); font-size: .74rem; color: var(--muted); }
.cx-leg { display: inline-flex; align-items: center; gap: .4rem; }
.cx-leg i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.cx-stage { position: relative; background: var(--panel); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; }
#cx-svg { width: 100%; height: auto; display: block; }
.cx-edge { stroke: var(--border); stroke-width: 1.2; opacity: .55; transition: opacity .15s ease, stroke .15s ease; }
.cx-edge.lit { stroke: var(--accent); opacity: .95; stroke-width: 1.9; }
#cx-svg.dimmed .cx-edge:not(.lit) { opacity: .1; }
.cx-node { cursor: pointer; }
.cx-node circle { stroke: var(--panel); stroke-width: 2.5; transition: opacity .15s ease; }
.cx-node:focus { outline: none; }
.cx-node:focus circle, .cx-node.hot circle { stroke: var(--accent); stroke-width: 3; }
.cx-label { font-family: var(--sans); font-size: 10.5px; fill: var(--muted); pointer-events: none; transition: opacity .15s ease, fill .15s ease; }
.cx-node.hot .cx-label, .cx-node.adj .cx-label { fill: var(--text); }
#cx-svg.dimmed .cx-node:not(.hot):not(.adj) { opacity: .26; }
#cx-svg.dimmed .cx-node:not(.hot):not(.adj) .cx-label { opacity: 0; }
#cx-info { position: absolute; left: 1rem; bottom: 1rem; max-width: min(360px, 72%); background: var(--bg);
  border: 1px solid var(--border); border-radius: 12px; padding: .8rem 1rem; box-shadow: 0 12px 30px rgba(0,0,0,.16);
  opacity: 0; transform: translateY(8px); transition: opacity .15s ease, transform .15s ease; pointer-events: none; }
#cx-info.show { opacity: 1; transform: none; }
#cx-info .cx-i-t { display: block; font-family: var(--display); font-size: 1.12rem; line-height: 1.2; }
#cx-info .cx-i-n { display: block; font-family: var(--sans); font-size: .76rem; color: var(--muted); margin: .35rem 0 .45rem; line-height: 1.45; }
#cx-info .cx-i-cta { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color: var(--accent); }
.cx-hint { text-align: center; font-family: var(--sans); font-size: .74rem; color: var(--muted); margin: .9rem 0 0; }
@media (max-width: 600px) { .cx-label { font-size: 9px; } #cx-info { left: .6rem; bottom: .6rem; } }
"""

CONNECTIONS_JS = r"""
(function () {
  var svg = document.getElementById('cx-svg'); if (!svg) return;
  var base = window.SHELL_BASE || '';
  var dEl = document.getElementById('cx-data');
  var titles = (dEl && JSON.parse(dEl.textContent || '{}').titles) || {};
  var info = document.getElementById('cx-info');
  var nodes = [].slice.call(svg.querySelectorAll('.cx-node'));
  var edges = [].slice.call(svg.querySelectorAll('.cx-edge'));
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function clear() { svg.classList.remove('dimmed'); nodes.forEach(function (n) { n.classList.remove('hot', 'adj'); });
    edges.forEach(function (e) { e.classList.remove('lit'); }); if (info) info.classList.remove('show'); }
  function focusNode(n) {
    var slug = n.getAttribute('data-slug'), nbr = (n.getAttribute('data-nbr') || '').split(' ').filter(Boolean);
    svg.classList.add('dimmed');
    nodes.forEach(function (m) { m.classList.remove('hot', 'adj'); });
    n.classList.add('hot');
    nodes.forEach(function (m) { if (nbr.indexOf(m.getAttribute('data-slug')) >= 0) m.classList.add('adj'); });
    edges.forEach(function (e) { var a = e.getAttribute('data-a'), b = e.getAttribute('data-b'); e.classList.toggle('lit', a === slug || b === slug); });
    if (info) {
      var links = nbr.map(function (s) { return titles[s] || s; });
      info.innerHTML = '<span class="cx-i-t">' + esc(titles[slug] || slug) + '</span>'
        + '<span class="cx-i-n">' + (links.length ? 'Connects to ' + esc(links.join(' · ')) : 'No strong links yet') + '</span>'
        + '<span class="cx-i-cta">Open corpus →</span>';
      info.classList.add('show');
    }
  }
  function go(n) { window.location.href = base + n.getAttribute('data-slug') + '.html'; }
  nodes.forEach(function (n) {
    n.addEventListener('mouseenter', function () { focusNode(n); });
    n.addEventListener('focus', function () { focusNode(n); });
    n.addEventListener('click', function () { go(n); });
    n.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(n); } });
  });
  svg.addEventListener('mouseleave', clear);
})();
"""

CONNECTIONS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connections — research · calvincollins · xyz</title>
<meta name="description" content="A map of ideas: how the research corpora relate by theme.">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="index.html">The Research</a>
    <a href="ghost.html">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
    <a href="connections.html" class="active">Connections</a>
  </nav>
</div>
<main class="cx-wrap">
  <header class="cx-head">
    <p class="kicker">A map of ideas</p>
    <h1>Connections</h1>
    <p>How the corpora speak to one another — clustered by subject, linked where they share the most ground. Hover a work to light its threads; open it with a click.</p>
  </header>
  <div class="cx-legend">{legend}</div>
  <div class="cx-stage">{svg}<div id="cx-info"></div></div>
  <p class="cx-hint">Lines connect works that share the most thematic vocabulary. Tab through the map by keyboard, or open any work to read it.</p>
</main>
<footer class="cx-foot" style="max-width:1120px;margin:2rem auto 0;padding:1.4rem 2rem 3rem;border-top:1px solid var(--border);text-align:center">
  <p class="colophon" style="font-family:var(--sans);font-size:.74rem;color:var(--muted);margin:0"><a href="index.html" style="color:var(--accent);text-decoration:none">← Back to the Research Library</a></p>
</footer>
<script id="cx-data" type="application/json">{data_json}</script>
<script>{theme_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""


def build_connections_page(out_dir, corpora, category_order, shell=""):
    """Render docs/connections.html — an interactive theme-graph of the corpora."""
    out = Path(out_dir)
    nodes = [c for c in corpora if c.get("kind") == "corpus"]
    if len(nodes) < 2:
        return False
    by = {n["slug"]: n for n in nodes}
    seen = []
    for c in (category_order or []):
        if any(n["category"] == c for n in nodes) and c not in seen:
            seen.append(c)
    for n in nodes:
        if n["category"] not in seen:
            seen.append(n["category"])
    cat_color = {c: [TERRA, GOLD, BLUE, OLIVE, PLUM][i % 5] for i, c in enumerate(seen)}

    W, H, cx, cy, R = 1040, 760, 520, 380, 248
    pos, C = {}, max(1, len(seen))
    for i, cat in enumerate(seen):
        members = [n for n in nodes if n["category"] == cat]
        ang = -math.pi / 2 + 2 * math.pi * i / C
        ax, ay = cx + R * math.cos(ang), cy + R * math.sin(ang)
        k = len(members)
        spread = 30 + 15 * k
        for j, m in enumerate(members):
            if k == 1:
                mx, my = ax, ay
            else:
                a2 = ang + 2 * math.pi * j / k
                mx, my = ax + spread * math.cos(a2) * 0.6, ay + spread * math.sin(a2) * 0.6
            pos[m["slug"]] = (mx, my)

    edges = set()
    for n in nodes:
        for r in n.get("related", []):
            if r.get("slug") in by:
                edges.add(tuple(sorted((n["slug"], r["slug"]))))
    nbr = {n["slug"]: set() for n in nodes}
    for a, b in edges:
        nbr[a].add(b); nbr[b].add(a)

    parts = [f'<svg id="cx-svg" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="A graph of how the research corpora relate by theme"><g id="cx-edges">']
    for a, b in sorted(edges):
        ax, ay = pos[a]; bx, by = pos[b]
        parts.append(f'<line class="cx-edge" data-a="{html.escape(a, quote=True)}" data-b="{html.escape(b, quote=True)}" '
                     f'x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/>')
    parts.append('</g><g id="cx-nodes">')
    for n in nodes:
        x, y = pos[n["slug"]]
        rad = 9 + min(15, len(n.get("chapters", [])) * 0.7)
        parts.append(
            f'<g class="cx-node" data-slug="{html.escape(n["slug"], quote=True)}" '
            f'data-nbr="{html.escape(" ".join(sorted(nbr[n["slug"]])), quote=True)}" '
            f'tabindex="0" role="link" aria-label="{html.escape(n["title"])}">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{cat_color[n["category"]]}"/>'
            f'<text class="cx-label" x="{x:.1f}" y="{y + rad + 12:.1f}" text-anchor="middle">{html.escape(n["title"])}</text>'
            f'</g>'
        )
    parts.append('</g></svg>')
    legend = "".join(f'<span class="cx-leg"><i style="background:{cat_color[c]}"></i>{html.escape(c)}</span>' for c in seen)
    titles = {n["slug"]: n["title"] for n in nodes}
    page = CONNECTIONS_TEMPLATE.format(
        css=LIBRARY_CSS + CONNECTIONS_CSS, favicon=FAVICON, og_meta=OG_META,
        svg="".join(parts), legend=legend,
        data_json=json_for_html({"titles": titles}),
        theme_js=LIBRARY_THEME_JS, app_js=CONNECTIONS_JS, shell=shell,
    )
    (out / "connections.html").write_text(page)
    print(f"  ✓ Connections  ({len(nodes)} nodes, {len(edges)} links) → connections.html")
    return True


# ---------------------------------------------------------------- build

def strip_md(body, cap=6000):
    """Plain-text excerpt of a chapter body for the global search index.

    Drops injected figure HTML/SVG, code fences, markdown punctuation and URLs,
    collapses whitespace, and caps length so search-index.json stays lean.
    """
    t = re.sub(r"```.*?```", " ", body, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[#>*_`~\[\]()|]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:cap]


# ---------------------------------------------------------------- entertainment-layer data (build-time)
# Two precomputed artifacts the runtime leans on, both static and offline:
#   * pull-quotes per corpus  -> Today's Passage + (later) shareable passage cards
#   * a corpus-to-corpus similarity graph -> related-reading + (later) recommendations
# Zero ML: similarity is a category bonus + Jaccard keyword overlap.

_STOPWORDS = set((
    "the a an and or but of to in on for with from at by as is are was were be been being this that "
    "these those it its his her their our your they them we you i he she him not no nor so than then "
    "thus into over under about between across after before during against within without research "
    "chapter how what why who when where which whom whose into onto upon"
).split())


def _keywords(text):
    """Lowercased content-word set for the similarity graph (stopwords/short words dropped)."""
    return {t for t in re.findall(r"[a-z][a-z'\-]{3,}", text.lower()) if t not in _STOPWORDS}


def _clean_passage(text, max_len=260):
    """Trim a pull-quote to <= max_len WITHOUT cutting a word: prefer ending at a
    sentence, then a clause boundary, else a word boundary + an ellipsis."""
    text = text.strip().strip('"“”‘’\'').strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    half = max_len * 0.5
    s = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if s >= half:
        return cut[:s + 1]
    c = max(cut.rfind("; "), cut.rfind(", "), cut.rfind(": "), cut.rfind("— "))
    if c >= half:
        return cut[:c].rstrip(" ,;:—") + "…"
    w = cut.rfind(" ")
    return (cut[:w] if w > 0 else cut).rstrip(" ,;:—") + "…"


READING_WPM = 220  # average adult prose reading speed, for time-to-read estimates


def reading_time(words):
    """Human reading-time label for a word count: '7 min', '1h 12m', '3h'."""
    m = max(1, round(words / READING_WPM))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h}h {mm}m" if mm else f"{h}h"


def extract_passages(corpus, limit=5):
    """A few striking pull-quotes per corpus: epigraph/blockquotes first, then chapter lead sentences.

    Each passage's text exists verbatim in the rendered prose, so Today's Passage
    can deep-link to it and the reader's TreeWalker can scroll to (and flash) it.
    """
    quotes, leads = [], []
    for di, d in enumerate(corpus["documents"]):
        body = d["body"]
        for m in re.finditer(r"(?:^>.*(?:\n|$))+", body, re.MULTILINE):
            raw = strip_md(re.sub(r"(?m)^>\s?", "", m.group(0)), cap=600).strip()
            if len(raw) < 45 or "http" in raw.lower():
                continue
            q = _clean_passage(raw, 260)
            if len(q) >= 45:
                quotes.append({"chapter": di, "chapterTitle": d["title"], "text": q, "kind": "epigraph"})
        prose = strip_md(re.sub(r"(?m)^#.*$", "", body), cap=600)
        ms = re.search(r"([A-Z][^.!?]{45,180}[.!?])", prose)
        if ms:
            leads.append({"chapter": di, "chapterTitle": d["title"], "text": _clean_passage(ms.group(1).strip(), 240), "kind": "lead"})
    picked, used_ch = [], set()
    for pool in (quotes, leads):
        for p in pool:
            if len(picked) >= limit:
                break
            if p["chapter"] in used_ch:
                continue
            picked.append(p)
            used_ch.add(p["chapter"])
    for p in quotes + leads:  # backfill if a corpus has few distinct chapters
        if len(picked) >= limit:
            break
        if p not in picked:
            picked.append(p)
    return picked[:limit]


def build_similarity(corpus_meta, top_k=3):
    """corpus_meta: list of {slug, title, category, keywords}. Returns {slug: [related entries]}."""
    out = {}
    for a in corpus_meta:
        scored = []
        for b in corpus_meta:
            if b["slug"] == a["slug"]:
                continue
            inter = len(a["keywords"] & b["keywords"])
            union = len(a["keywords"] | b["keywords"]) or 1
            score = (inter / union) * 100
            if a["category"] == b["category"] and a["category"] != "Other":
                score += 35
            scored.append((score, b))
        scored.sort(key=lambda x: x[0], reverse=True)
        out[a["slug"]] = [
            {"slug": b["slug"], "title": b["title"], "category": b["category"]}
            for s, b in scored[:top_k] if s > 0
        ]
    return out


# ---------------------------------------------------------------- collections (flagship)
# A Collection is a hand-authored reading ARC that threads chapters from several
# corpora into one continuous argument. It reuses the corpus reader wholesale:
# we assemble a synthetic "corpus" whose documents are the chosen chapters (each
# carrying a back-link to its source corpus), then render it through
# READER_TEMPLATE — so the merged arc inherits the TOC, search, pager, keyboard
# nav, themes, and the command-palette shell for free. Authored in
# build.config.json under "collections": [{id,title,essay,palette?,chapters:[{slug,chapter}]}].

def resolve_collection(col, corpora_by_slug, idx):
    """Assemble a collection's merged reader corpus + shelf meta, or None if empty."""
    cid = col.get("id") or re.sub(r"[^a-z0-9]+", "-", col.get("title", "").lower()).strip("-")
    slug = "collection-" + cid
    docs, used = [], []
    for step in col.get("chapters", []):
        src = corpora_by_slug.get(step.get("slug"))
        if not src:
            print(f"  ! collection {cid}: unknown corpus {step.get('slug')!r}", file=sys.stderr)
            continue
        ci = step.get("chapter", 0)
        if not (0 <= ci < len(src["documents"])):
            print(f"  ! collection {cid}: {step.get('slug')} has no chapter {ci}", file=sys.stderr)
            continue
        d = src["documents"][ci]
        # rewrite the chapter's intra-corpus .md links to point at the full source corpus
        f2i = {doc["file"]: j for j, doc in enumerate(src["documents"])}
        def _rw(m, _slug=step["slug"], _f2i=f2i):
            j = _f2i.get(m.group(2))
            return f'[{m.group(1)}]({_slug}.html#ch-{j})' if j is not None else m.group(1)
        body = re.sub(r"\[([^\]]+)\]\(([^)]+\.md)\)", _rw, d["body"])
        attrib = f'*From [{src["title"]}]({step["slug"]}.html#ch-{ci}) — chapter {ci + 1}*'
        docs.append({"order": len(docs), "file": f"{slug}-{len(docs)}.md",
                     "title": d["title"], "summary": src["title"], "body": attrib + "\n\n" + body})
        used.append(step["slug"])
    if not docs:
        print(f"  ! collection {cid}: no valid chapters, skipped", file=sys.stderr)
        return None
    # count prose words only — strip injected figure HTML/SVG so it doesn't inflate the estimate
    words = sum(len(re.sub(r"<[^>]+>", " ", x["body"]).split()) for x in docs)
    mins = max(1, round(words / 220))
    reading = f"~{mins} min" if mins < 90 else f"~{mins / 60:.1f} hr"
    col_corpus = {"slug": slug, "title": col.get("title", cid), "subtitle": col.get("essay", "")[:160],
                  "author": "", "generated": "", "documents": docs}
    meta = {"i": idx, "id": cid, "slug": slug, "title": col.get("title", cid), "essay": col.get("essay", ""),
            "n_ch": len(docs), "n_corpora": len(set(used)), "reading": reading,
            "palette": col.get("palette")}
    return col_corpus, meta


def collection_card_html(meta):
    """One Collection poster card for the index shelf."""
    poster = cover_svg(meta["slug"], meta.get("palette"))
    accent = ["--t1", "--t2", "--t3", "--t4", "--t5"][meta["i"] % 5]
    return (
        f'<a class="coll-card" href="{meta["slug"]}.html" data-slug="{meta["slug"]}" '
        f'data-total="{meta["n_ch"]}" data-accent="{accent}">'
        f'<div class="coll-poster">{poster}<span class="coll-poster-title">{html.escape(meta["title"])}</span></div>'
        f'<div class="coll-body"><p class="coll-note">{html.escape(meta["essay"])}</p>'
        f'<p class="coll-meta">{meta["n_corpora"]} corpora · {meta["n_ch"]} chapters · {meta["reading"]}</p>'
        f'</div></a>'
    )


def collections_section_html(metas):
    """The Collections shelf for the library index (empty string if none)."""
    if not metas:
        return ""
    cards = "".join(collection_card_html(m) for m in metas)
    return ('<section class="collections" id="collections">'
            '<h2 class="coll-h">Collections</h2>'
            '<p class="coll-intro">Curated reading arcs that thread chapters from several corpora '
            'into one continuous argument.</p>'
            f'<div class="coll-shelf">{cards}</div></section>')


def json_for_html(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


# ---------------------------------------------------------------- research wrapped
# A private "year in reading" computed entirely client-side from localStorage
# (read:{slug} sets, reading-streak) against an inlined per-corpus stats table.
# Nothing leaves the device; a brand-new visitor gets an empty-state invite.

WRAPPED_CSS = """
.wr-plate { max-width: 820px; margin: 1.8rem auto 0; padding: 0 2rem; text-align: center; }
.wr-kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .2em; color: var(--accent); margin: 0 0 .5rem; }
.wr-name { font-family: var(--display); font-weight: 800; font-size: clamp(2.4rem, 6vw, 4rem); line-height: .98; letter-spacing: -.02em; margin: 0 0 .55rem; }
.wr-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0; }
#wrapped-body { max-width: 820px; margin: 1.8rem auto 0; padding: 0 2rem 2rem; }
.wr-identity { font-family: var(--display); font-size: 1.35rem; text-align: center; margin: 0 0 1.6rem; }
.wr-identity strong { color: var(--accent); }
.wr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1rem; }
.wr-card { background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 1.3rem 1.4rem; display: flex; flex-direction: column; }
.wr-val { font-family: var(--display); font-weight: 800; font-size: 2.1rem; color: var(--accent); line-height: 1; }
.wr-label { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-top: .55rem; }
.wr-sub { font-family: var(--sans); font-size: .78rem; color: var(--text); margin-top: .2rem; }
.wr-share { display: block; margin: 1.9rem auto 0; font-family: var(--sans); font-size: .82rem; text-transform: uppercase; letter-spacing: .08em; color: var(--bg); background: var(--accent); border: none; border-radius: 12px; padding: .7rem 1.4rem; cursor: pointer; }
.wr-empty { max-width: 540px; margin: 2.5rem auto; text-align: center; font-family: var(--sans); color: var(--muted); line-height: 1.65; }
.wr-foot { max-width: 820px; margin: 2.5rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.wr-foot a { color: var(--accent); text-decoration: none; font-family: var(--sans); font-size: .74rem; }
"""

WRAPPED_JS = r"""
(function () {
  var stats = [];
  try { stats = JSON.parse(document.getElementById('wrapped-stats').textContent || '[]'); } catch (e) {}
  function readCount(slug) { try { return (JSON.parse(localStorage.getItem('read:' + slug) || '[]') || []).length; } catch (e) { return 0; } }
  var chaptersRead = 0, wordsRead = 0, started = 0, completed = 0, catCount = {}, topCorpus = null, topN = 0;
  stats.forEach(function (s) {
    var r = Math.min(readCount(s.slug), s.chapters);
    if (r <= 0) return;
    started++; chaptersRead += r;
    wordsRead += Math.round((r / Math.max(1, s.chapters)) * s.words);
    catCount[s.category] = (catCount[s.category] || 0) + r;
    if (r > topN) { topN = r; topCorpus = s; }
    if (r >= s.chapters) completed++;
  });
  var topCat = Object.keys(catCount).sort(function (a, b) { return catCount[b] - catCount[a]; })[0] || '—';
  var streak = 0; try { streak = (JSON.parse(localStorage.getItem('reading-streak') || '{}') || {}).count || 0; } catch (e) {}
  var identity = chaptersRead < 1 ? 'The Newcomer' : chaptersRead < 5 ? 'The Browser'
    : chaptersRead < 15 ? 'The Regular' : chaptersRead < 40 ? 'a Constant Reader' : 'The Omnivore';
  var host = document.getElementById('wrapped-body');
  var esc = function (s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
  if (chaptersRead === 0) {
    host.innerHTML = '<p class="wr-empty">Your Wrapped fills in as you read. Open any corpus — your chapters, words, streak, and reading identity gather here, computed on this device and stored nowhere else.</p>';
    return;
  }
  var fmt = function (n) { return n.toLocaleString(); };
  function card(label, value, sub) {
    return '<div class="wr-card"><span class="wr-val">' + esc(value) + '</span>'
      + '<span class="wr-label">' + esc(label) + '</span>'
      + (sub ? '<span class="wr-sub">' + esc(sub) + '</span>' : '') + '</div>';
  }
  host.innerHTML = '<p class="wr-identity">This season, you are <strong>' + esc(identity) + '</strong></p>'
    + '<div class="wr-grid">'
    + card('chapters read', fmt(chaptersRead))
    + card('words read', fmt(wordsRead))
    + card('corpora started', started + ' of ' + stats.length)
    + card('corpora completed', String(completed))
    + card('day streak', String(streak))
    + card('favorite subject', topCat)
    + (topCorpus ? card('most read', topCorpus.title, topN + ' chapters') : '')
    + '</div>'
    + '<button id="wr-share" class="wr-share">Share your Wrapped ↗</button>';
  var sh = document.getElementById('wr-share');
  if (sh) sh.onclick = function () {
    if (!window.CorpusShare) return;  // the shell (which defines CorpusShare) loads after this script
    window.CorpusShare.open({ kicker: 'My Research Wrapped', title: 'I read like ' + identity,
      source: fmt(chaptersRead) + ' chapters · ' + fmt(wordsRead) + ' words · ' + streak + '-day streak · favorite subject: ' + topCat,
      url: location.origin + location.pathname, filename: 'research-wrapped' });
  };
})();
"""

WRAPPED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Wrapped — research · calvincollins · xyz</title>
<meta name="description" content="Your private year in reading.">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="index.html">The Research</a>
    <a href="ghost.html">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
    <a href="wrapped.html" class="active">Wrapped</a>
  </nav>
</div>
<header class="wr-plate">
  <p class="wr-kicker">A private year in reading</p>
  <h1 class="wr-name">Research Wrapped</h1>
  <p class="wr-motto">Yours alone — computed on this device, stored nowhere else.</p>
</header>
<main id="wrapped-body"></main>
<footer class="wr-foot"><a href="index.html">← Back to the Research Library</a></footer>
<script id="wrapped-stats" type="application/json">{stats_json}</script>
<script>{theme_js}</script>
<script>{wrapped_js}</script>
{shell}
</body>
</html>
"""


def build_wrapped_page(out_dir, wrapped_stats, shell=""):
    """Render docs/wrapped.html — a client-side 'year in reading' from localStorage."""
    out = Path(out_dir)
    page = WRAPPED_TEMPLATE.format(
        favicon=FAVICON, og_meta=OG_META,
        css=LIBRARY_CSS + WRAPPED_CSS,
        stats_json=json_for_html(wrapped_stats),
        theme_js=LIBRARY_THEME_JS,
        wrapped_js=WRAPPED_JS,
        shell=shell,
    )
    (out / "wrapped.html").write_text(page)
    print("  ✓ Research Wrapped → wrapped.html")


def build(folders, out_dir, site_title, site_subtitle, ghost_cfg=None, descriptions=None,
          fingerprint_cfg=None, titles=None, category_order=None, collections=None, atlas_cfg=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Copy the link-preview image into the served output so the absolute
    # og:image URL (SITE_URL/OG_IMAGE) resolves on the live site.
    og_src = HERE / OG_IMAGE
    if og_src.exists():
        shutil.copy(og_src, out / OG_IMAGE)
    ghost_cfg = ghost_cfg or {}
    fingerprint_cfg = fingerprint_cfg or {}
    descriptions = descriptions or {}
    titles = titles or {}
    category_order = category_order or []
    cards = []
    manifest = []          # cross-page command-palette index (corpora + sections + editions)
    pages = []             # reader renders deferred until the manifest below is complete
    search_entries = []    # trimmed chapter text for the palette's "in the text" search
    corpus_meta = []       # {slug,title,category,keywords} for the similarity graph
    all_passages = []      # pull-quotes across every corpus, for Today's Passage
    corpora_by_slug = {}   # full corpus objects, retained for assembling collections
    atlas_corpora = {}     # {slug: {title,href,accent,img,chapters}} for the Atlas map
    wrapped_stats = []     # per-corpus {slug,title,category,chapters,words} for Research Wrapped
    total_chapters = 0
    total_words = 0

    for n_corpus, folder in enumerate(folders, 1):
        corpus = load_corpus(folder)
        if not corpus["documents"]:
            print(f"  ! {folder}: no chapters found, skipped", file=sys.stderr)
            continue
        # A punchy display title/tagline can be set per corpus (keyed by slug) via
        # build.config.json "titles" — this overrides the manifest's research
        # `topic`/`title` for display only, leaving the corpus metadata untouched.
        override = titles.get(corpus["slug"], {})
        if override.get("title"):
            corpus["title"] = override["title"]
        if override.get("subtitle"):
            corpus["subtitle"] = override["subtitle"]
        figs = inject_figures(corpus, folder)
        vizn = 0
        for _doc in corpus["documents"]:
            _doc["body"], _c = transform_viz(_doc["body"])
            vizn += _c
        theme = load_theme_spec(folder)
        # Per-corpus link preview: this corpus's photo cover (a real served file)
        # if it has one, else the site banner. Description prefers the configured
        # card blurb, then the subtitle, then the title.
        cover_url = publish_cover_for_og(corpus["slug"], out)
        reader_og = og_tags(
            corpus["title"],
            descriptions.get(corpus["slug"]) or corpus["subtitle"] or corpus["title"],
            f"{SITE_URL}/{corpus['slug']}.html",
            cover_url or f"{SITE_URL}/{OG_IMAGE}",
        )
        # Defer the reader render until the full library manifest exists, so the
        # shared command-palette shell (injected below) can see every corpus.
        pages.append({
            "slug": corpus["slug"],
            "title": corpus["title"],
            "subtitle": corpus["subtitle"],
            "theme_style": render_theme_style(theme),
            "reader_og": reader_og,
            "data_json": json_for_html(corpus),
        })
        n = len(corpus["documents"])
        words = sum(len(d["body"].split()) for d in corpus["documents"])
        total_chapters += n
        total_words += words
        meta_bits = [f"Nº {n_corpus:02d}", f"{n} chapters", reading_time(words)]
        if corpus["generated"]:
            meta_bits.append(corpus["generated"])
        # An index card's blurb can be overridden per corpus (keyed by slug) via
        # build.config.json "descriptions" — useful when a corpus has no manifest
        # subtitle, or its manifest carries a long sharpened_question / boilerplate.
        card_sub = descriptions.get(corpus["slug"], corpus["subtitle"] or "")
        category = override.get("category") or "Other"
        # A lowercase haystack the index search box matches against (title, blurb, category).
        search_text = html.escape(
            " ".join([corpus["title"], card_sub, category]).lower(), quote=True
        )
        # Tile accent for this card's progress ring (cycles the trencadís palette).
        accent_var = ["--t1", "--t2", "--t3", "--t4", "--t5"][(n_corpus - 1) % 5]
        card_html = (
            f'<a class="card" href="{corpus["slug"]}.html" data-cat="{html.escape(category, quote=True)}" '
            f'data-slug="{html.escape(corpus["slug"], quote=True)}" data-total="{n}" data-accent="{accent_var}" '
            f'data-search="{search_text}">'
            f'<div class="cover">{card_cover(corpus["slug"], corpus["title"], theme_cover_palette(theme))}</div>'
            f'<div class="card-body"><h2>{html.escape(corpus["title"])}</h2>'
            f'<p class="sub">{html.escape(card_sub)}</p>'
            f'<p class="meta">{" · ".join(meta_bits)}</p></div></a>'
        )
        cards.append({"category": category, "html": card_html})
        manifest.append({
            "slug": corpus["slug"],
            "title": corpus["title"],
            "category": category,
            "kind": "corpus",
            "href": f"{corpus['slug']}.html",
            "chapters": [d["title"] for d in corpus["documents"]],
        })
        search_entries.append({
            "slug": corpus["slug"], "title": corpus["title"],
            "chapters": [
                {"i": di, "title": d["title"], "text": strip_md(d["body"])}
                for di, d in enumerate(corpus["documents"])
            ],
        })
        corpus_meta.append({
            "slug": corpus["slug"], "title": corpus["title"], "category": category,
            "keywords": _keywords(corpus["title"] + " " + corpus["slug"].replace("-", " ") + " "
                                  + " ".join(d["title"] for d in corpus["documents"])),
        })
        all_passages += [dict(p, slug=corpus["slug"], title=corpus["title"])
                         for p in extract_passages(corpus)]
        corpora_by_slug[corpus["slug"]] = corpus  # retained for collections
        _atlas_img = find_cover_image(corpus["slug"])
        atlas_corpora[corpus["slug"]] = {
            "title": corpus["title"],
            "href": f"{corpus['slug']}.html",
            "accent": ATLAS_TILES[(n_corpus - 1) % len(ATLAS_TILES)],
            "img": f"covers/{_atlas_img.name}" if _atlas_img else None,
            "chapters": [{"t": d["title"], "href": f"{corpus['slug']}.html#ch-{di}"}
                         for di, d in enumerate(corpus["documents"])],
        }
        wrapped_stats.append({"slug": corpus["slug"], "title": corpus["title"], "category": category,
                              "chapters": n,
                              "words": sum(len(re.sub(r"<[^>]+>", " ", d["body"]).split()) for d in corpus["documents"])})
        fig_note = f", {figs} figures" if figs else ""
        viz_note = f", {vizn} charts" if vizn else ""
        print(f"  ✓ {corpus['title']}  ({n} chapters{fig_note}{viz_note})")

    # Bake the corpus-to-corpus similarity graph into each corpus manifest entry,
    # so the reader can render "Related reading" with no fetch (it already has the
    # inlined manifest via the shell).
    related_map = build_similarity(corpus_meta)
    for entry in manifest:
        if entry.get("kind") == "corpus":
            entry["related"] = related_map.get(entry["slug"], [])

    # Resolve Collections (cross-corpus arcs) and register them in the palette
    # manifest before it is serialized into the shared shell.
    resolved_collections = []
    for i, col in enumerate(collections or []):
        r = resolve_collection(col, corpora_by_slug, i)
        if r:
            resolved_collections.append(r)
    for _cc, _meta in resolved_collections:
        manifest.append({"title": _meta["title"], "kind": "collection", "category": "Collection",
                         "href": _meta["slug"] + ".html", "meta": f'{_meta["n_ch"]} chapters'})

    # Read the Ghost + Fingerprint edition lists up front so their section fronts
    # and individual editions can join the command-palette manifest before any
    # page (each of which embeds that manifest via the shared shell) is written.
    editions = read_ghost_manifest(out) if ghost_cfg.get("enabled", True) else []
    fp_editions = read_fingerprint_manifest(out) if fingerprint_cfg.get("enabled", True) else []

    if ghost_cfg.get("enabled", True):
        manifest.append({"title": "The Ghost of Times", "kind": "section",
                         "category": "Daily paper", "href": "ghost.html",
                         "meta": "writer-voiced op-eds"})
        for ed in editions:
            manifest.append({
                "title": ed.get("lead_headline") or f"Ghost — {ed.get('date', '')}",
                "kind": "edition", "category": "The Ghost of Times",
                "href": ed.get("file") or f"ghost/{ed.get('date', '')}-ghost-of-times.html",
                "meta": ed.get("date", ""),
            })
    if fingerprint_cfg.get("enabled", True):
        manifest.append({"title": "The Fingerprint", "kind": "section",
                         "category": "Market wire", "href": "fingerprint.html",
                         "meta": "CTV market paper"})
        for ed in fp_editions:
            manifest.append({
                "title": ed.get("lead_headline") or f"Fingerprint — {ed.get('date', '')}",
                "kind": "edition", "category": "The Fingerprint",
                "href": ed.get("file") or f"fingerprint/{ed.get('date', '')}-fingerprint.html",
                "meta": ed.get("date", ""),
            })

    manifest.append({"title": "Research Wrapped", "kind": "section", "category": "You",
                     "href": "wrapped.html", "meta": "your year in reading"})
    manifest.append({"title": "Connections", "kind": "section",
                     "category": "The map of ideas", "href": "connections.html",
                     "meta": "how the corpora relate"})

    manifest_json = json_for_html(manifest)
    shell_root = shell_html(manifest_json, "")      # pages at docs/ root
    shell_sub = shell_html(manifest_json, "../")    # edition pages in docs/<section>/

    # Lazy global-search payload — the palette fetches this only on an "in the
    # text" query, so it never weighs down first paint. Bodies are trimmed.
    (out / "search-index.json").write_text(json.dumps(search_entries, ensure_ascii=False))
    # Pull-quotes for Today's Passage (also written as a file for later reuse).
    (out / "passages.json").write_text(json.dumps(all_passages, ensure_ascii=False))

    # The Atlas — lay each corpus's cover onto its region and write atlas.json,
    # which the shared shell's map surface fetches on first open. Skipped (with a
    # note) if the geometry hasn't been authored (scripts/build_atlas_geo.py).
    atlas_cfg = atlas_cfg or {}
    atlas_geo = load_atlas_geo()
    if atlas_geo:
        atlas_data = compute_atlas(
            atlas_geo, atlas_corpora,
            placements=atlas_cfg.get("places") or ATLAS_PLACEMENTS,
            grow=atlas_cfg.get("grow") or ATLAS_GROW,
        )
        if atlas_data:
            (out / "atlas.json").write_text(json.dumps(atlas_data, separators=(",", ":"), ensure_ascii=False))
            print(f"  ✓ Atlas: {len(atlas_data['places'])} corpora across {len(atlas_data['regions'])} regions → atlas.json")
        else:
            print("  ! Atlas: no corpora placed (check the 'atlas' placements)", file=sys.stderr)
    else:
        print("  ! Atlas: atlas/geo.json not found — run scripts/build_atlas_geo.py", file=sys.stderr)

    # Now write the reader pages, each carrying the shared shell.
    for p in pages:
        (out / f"{p['slug']}.html").write_text(READER_TEMPLATE.format(
            title=html.escape(p["title"]),
            subtitle=html.escape(p["subtitle"]),
            css=CSS,
            theme_style=p["theme_style"],
            favicon=FAVICON, og_meta=p["reader_og"],
            data_json=p["data_json"],
            marked_js=MARKED_JS,
            app_js=APP_JS,
            shell=shell_root,
        ))

    # Collection pages — merged cross-corpus readers, rendered through the same
    # reader template so they inherit the TOC, search, pager, keyboard nav + shell.
    for col_corpus, meta in resolved_collections:
        col_og = og_tags(meta["title"], (meta["essay"][:200] or meta["title"]),
                         f"{SITE_URL}/{meta['slug']}.html", f"{SITE_URL}/{OG_IMAGE}")
        (out / f"{meta['slug']}.html").write_text(READER_TEMPLATE.format(
            title=html.escape(meta["title"]), subtitle=html.escape(col_corpus["subtitle"]),
            css=CSS, theme_style="", favicon=FAVICON, og_meta=col_og,
            data_json=json_for_html(col_corpus), marked_js=MARKED_JS, app_js=APP_JS, shell=shell_root))
    if resolved_collections:
        print(f"  ✓ Rendered {len(resolved_collections)} collection(s)")

    # The Connections page — interactive theme-graph of the corpora.
    build_connections_page(out, [e for e in manifest if e.get("kind") == "corpus"], category_order, shell=shell_root)

    # The Ghost of Times section (second top-level section of the site).
    build_ghost_page(out, editions, ghost_cfg, shell=shell_root)
    ghost_band = ghost_band_html(editions, ghost_cfg)
    # Render each edition page natively from its deposited data (skips any that
    # predate the data-driven renderer and so have no docs/ghost/data/*.json).
    rendered = sum(build_ghost_edition(out, ed, shell=shell_sub) for ed in editions)
    if editions:
        print(f"  ✓ Rendered {rendered}/{len(editions)} edition page(s) from data")

    # The Fingerprint section (third top-level section of the site).
    build_fingerprint_page(out, fp_editions, fingerprint_cfg, shell=shell_root)
    fingerprint_band = fingerprint_band_html(fp_editions, fingerprint_cfg)
    fp_rendered = sum(build_fingerprint_edition(out, ed, shell=shell_sub) for ed in fp_editions)
    if fp_editions:
        print(f"  ✓ Rendered {fp_rendered}/{len(fp_editions)} Fingerprint edition page(s) from data")

    stats = f"{len(cards)} corpora · {total_chapters} chapters · {round(total_words / 1000)}k words"

    # Group the cards into category sections. Categories appear in the configured
    # category_order; any category not listed (and the "Other" catch-all) follows
    # in first-seen order so a corpus is never silently dropped from the library.
    seen_order = []
    for card in cards:
        if card["category"] not in seen_order:
            seen_order.append(card["category"])
    ordered_cats = [c for c in category_order if c in seen_order]
    ordered_cats += [c for c in seen_order if c not in ordered_cats]

    pills = ['<button class="cat-pill active" data-cat="all">All</button>']
    sections = []
    for cat in ordered_cats:
        cat_cards = [c["html"] for c in cards if c["category"] == cat]
        pills.append(
            f'<button class="cat-pill" data-cat="{html.escape(cat, quote=True)}">'
            f'{html.escape(cat)} <span class="cat-count">{len(cat_cards)}</span></button>'
        )
        sections.append(
            f'<section class="cat-section" data-cat="{html.escape(cat, quote=True)}">'
            f'<h3 class="cat-heading">{html.escape(cat)} '
            f'<span class="cat-count">{len(cat_cards)}</span></h3>'
            f'<div class="grid">{"".join(cat_cards)}</div></section>'
        )
    library_body = (
        '<div class="lib-toolbar">'
        '<input id="lib-search" class="lib-search" type="search" '
        'placeholder="Search the library…" aria-label="Search the research library" autocomplete="off">'
        f'<div class="cat-pills">{"".join(pills)}</div>'
        '</div>'
        + "\n".join(sections)
        + '<p class="lib-empty" id="lib-empty" hidden>No corpora match your search.</p>'
    )

    # The Fingerprint band shares the library CSS, so fold its rules in once.
    library_css = LIBRARY_CSS + FINGERPRINT_BAND_CSS + OVERTURE_CSS
    # First-visit overture markup — built from the existing brand only (no new copy).
    overture_html = (
        '<div id="overture" role="dialog" aria-modal="true" aria-label="Welcome to the library"><div class="ov-inner">'
        '<div class="ov-tiles" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div>'
        '<p class="ov-brand">research · calvincollins · xyz</p>'
        f'<h1 class="ov-title">{html.escape(site_title)}</h1>'
        f'<p class="ov-sub">{html.escape(site_subtitle)}</p>'
        '<div class="ov-sections"><span>The Research</span><span>The Ghost of Times</span><span>The Fingerprint</span></div>'
        '<button id="ov-enter" type="button">Enter the library →</button>'
        '<p class="ov-skip">Press Esc to skip</p>'
        '</div></div>'
    )
    daily_passage = (
        '<section id="daily-passage" hidden></section>'
        f'<script id="passages-data" type="application/json">{json_for_html(all_passages)}</script>'
    )
    collections_html = collections_section_html([m for _cc, m in resolved_collections])
    (out / "index.html").write_text(LIBRARY_TEMPLATE.format(
        site_title=html.escape(site_title),
        site_subtitle=html.escape(site_subtitle),
        css=library_css,
        favicon=FAVICON, og_meta=OG_META,
        stats=stats,
        hero=hero_art(),
        overture_head=OVERTURE_HEAD,
        overture=overture_html,
        daily_passage=daily_passage,
        ghost_band=ghost_band,
        fingerprint_band=fingerprint_band,
        resume='<div id="resume"></div>',
        foryou='<section id="foryou" hidden></section>',
        collections=collections_html,
        cards=library_body,
        theme_js=LIBRARY_THEME_JS + LIBRARY_FILTER_JS + DAILY_PASSAGE_JS + HOME_JS + OVERTURE_JS,
        shell=shell_root,
    ))
    build_wrapped_page(out, wrapped_stats, shell=shell_root)
    print(f"\nBuilt {len(cards)} corpora + {len(editions)} ghost + {len(fp_editions)} fingerprint editions ({stats}) → {out}/index.html")


def load_config(path):
    """Load build.config.json. Corpus paths are resolved relative to the config file."""
    cfg_path = Path(path)
    cfg = json.loads(cfg_path.read_text())
    base = cfg_path.resolve().parent
    folders = [str((base / c).resolve()) for c in cfg.get("corpora", [])]
    out = cfg.get("out", "dist")
    if not Path(out).is_absolute():
        out = str(base / out)
    return {
        "folders": folders,
        "out": out,
        "title": cfg.get("title", "Research Library"),
        "subtitle": cfg.get("subtitle", "Deep-research corpora, readable and searchable."),
        "ghost": cfg.get("ghost", {}),
        "fingerprint": cfg.get("fingerprint", {}),
        "descriptions": cfg.get("descriptions", {}),
        "titles": cfg.get("titles", {}),
        "category_order": cfg.get("category_order", []),
        "collections": cfg.get("collections", []),
        "atlas": cfg.get("atlas", {}),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="*", help="research corpus folders (optional if --config is given)")
    ap.add_argument("-c", "--config", help="path to build.config.json (title/subtitle/out/corpora/ghost)")
    ap.add_argument("-o", "--out", default=None, help="output directory (default: dist, or config's out)")
    ap.add_argument("--title", default=None, help="library page title")
    ap.add_argument("--subtitle", default=None, help="library page subtitle")
    args = ap.parse_args()

    # Default to the sibling build.config.json when neither folders nor --config are given.
    default_cfg = HERE / "build.config.json"
    if not args.folders and not args.config and default_cfg.exists():
        args.config = str(default_cfg)

    if args.config:
        cfg = load_config(args.config)
        folders = args.folders or cfg["folders"]
        out = args.out or cfg["out"]
        title = args.title or cfg["title"]
        subtitle = args.subtitle or cfg["subtitle"]
        ghost_cfg = cfg["ghost"]
        fingerprint_cfg = cfg["fingerprint"]
        descriptions = cfg["descriptions"]
        titles = cfg["titles"]
        category_order = cfg["category_order"]
        collections = cfg["collections"]
        atlas_cfg = cfg["atlas"]
    elif args.folders:
        folders = args.folders
        out = args.out or "dist"
        title = args.title or "Research Library"
        subtitle = args.subtitle or "Deep-research corpora, readable and searchable."
        ghost_cfg = {}
        fingerprint_cfg = {}
        descriptions = {}
        titles = {}
        category_order = []
        collections = []
        atlas_cfg = {}
    else:
        ap.error("no corpus folders and no --config / build.config.json found")

    build(folders, out, title, subtitle, ghost_cfg=ghost_cfg, descriptions=descriptions,
          fingerprint_cfg=fingerprint_cfg, titles=titles, category_order=category_order,
          collections=collections, atlas_cfg=atlas_cfg)
