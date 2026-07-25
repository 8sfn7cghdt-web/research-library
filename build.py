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
import io
import json
import math
import mimetypes
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COVERS_DIR = HERE / "covers"  # optional per-corpus photo covers: covers/<slug>.<ext>
FIGURES_DIR = HERE / "figures"
SCENE_ASSET_DIRNAME = "research-scenes"
MARKED_JS = (HERE / "vendor" / "marked.min.js").read_text()
# GFM strikethrough fires on a SINGLE tilde, so prose tildes meaning
# "approximately" (~$22B, ~50%, ~12-month-old) get rendered crossed-out — the
# struck span also swallows any **bold**/*italic* markers caught between the two
# tildes. Override the `del` tokenizer to require DOUBLE tildes; a lone tilde is
# left as a literal character. Returning `undefined` (not `false`) is load-
# bearing: `false` makes marked fall back to its default single-tilde tokenizer.
# Code spans/fences are tokenized before `del`, so tildes inside code are safe.
MARKED_JS += r"""
;(function(){
  if (typeof marked === 'undefined') return;
  marked.use({ tokenizer: { del: function(src){
    var cap = /^~~(?=[^\s~])((?:\\.|[^\\])*?[^\s~])~~(?=[^~]|$)/.exec(src);
    if (cap) return { type: 'del', raw: cap[0], text: cap[1],
                      tokens: this.lexer.inlineTokens(cap[1]) };
    return undefined;
  } } });
})();
"""

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect x='4' y='4' width='26' height='26' fill='%239a2c1a'/>"
           "<rect x='34' y='4' width='26' height='26' fill='%23a3771c'/>"
           "<rect x='4' y='34' width='26' height='26' fill='%23274d68'/>"
           "<rect x='34' y='34' width='26' height='26' fill='%234a6350'/></svg>")

# Canonical public origin (GitHub Pages custom domain, see docs/CNAME). Used to
# build the ABSOLUTE og:image URL link-preview scrapers (iMessage, Slack, etc.)
# require — relative paths are ignored by them.
SITE_URL = "https://calvincollins.xyz"
OG_IMAGE = "machine-humanities-homepage.png"  # source lives in corpus-app/; copied into out/ at build time
TOP_HEADER_IMAGE = OG_IMAGE
HERO_IMAGE = "divine-hero-agent-logo-v3.png"
RESEARCH_HERO_IMAGE = "research-hero-agent-logo-v1.png"
ADTECH_HERO_IMAGE = "adtech-hero-agent-logo-v1.png"
SITE_IMAGE_ASSETS = (OG_IMAGE, HERO_IMAGE, RESEARCH_HERO_IMAGE, ADTECH_HERO_IMAGE)
USE_TOP_HEADER_IMAGE = True
USE_HERO_IMAGE = True  # Use the Divine Hero Agent mascot; flip to False for the engraved lintel SVG.

# Open Graph + Twitter card tags so a shared link renders a rich preview with an
# image. The result is passed as a VALUE into each template's .format() (never as
# part of the format string), so any braces in a title pass through untouched.
def og_tags(title, description, url, image):
    """Build a page's Open Graph + Twitter-card <meta> block (a string)."""
    esc = lambda s: html.escape(str(s), quote=True)
    return "\n".join([
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="calvincollins · xyz">',
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
    "Machine Humanities — Agentic Scholarship",
    "A library of deep research, plus The Ghost of Times and The Fingerprint.",
    f"{SITE_URL}/",
    f"{SITE_URL}/{OG_IMAGE}",
)

# trencadís tile palette (light-theme hexes; readers/library recolor via CSS vars)
TERRA, GOLD, BLUE, OLIVE, PLUM = "#9a2c1a", "#a3771c", "#274d68", "#4a6350", "#64405a"


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


def load_glossary(folder):
    """Reader glossary for a corpus — a list of {term, aliases[], def}. The site
    turns each first in-text occurrence into a hover/tap definition chip. Source of
    truth is `manifest.json`'s optional `glossary`; a standalone `glossary.json` in
    the corpus folder overrides it (hand-authoring / backfill). Returns [] if none.
    Definitions are reader-facing, so they must be plain-language and carry no
    pipeline vocabulary (same house rule as the prose)."""
    folder = Path(folder)
    raw = []
    gp = folder / "glossary.json"
    if gp.exists():
        try:
            raw = json.loads(gp.read_text())
        except Exception:
            raw = []
    else:
        mp = folder / "manifest.json"
        if mp.exists():
            try:
                raw = json.loads(mp.read_text()).get("glossary", []) or []
            except Exception:
                raw = []
    if isinstance(raw, dict):  # tolerate {term: def} shorthand
        raw = [{"term": k, "def": v} for k, v in raw.items()]
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        term = (e.get("term") or "").strip()
        definition = (e.get("def") or e.get("definition") or "").strip()
        if not term or not definition:
            continue
        aliases = [a.strip() for a in (e.get("aliases") or []) if isinstance(a, str) and a.strip()]
        out.append({"term": term, "aliases": aliases, "def": definition})
    return out


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
    for bi, it in enumerate(items):
        v = float(it["value"])
        bw = PW * v / vmax if vmax else 0
        out.append(f'<text x="{LP - 10}" y="{y + bh - 6}" font-size="13" text-anchor="end">{_viz_esc(_viz_trunc(it["label"], 27))}</text>')
        out.append(f'<rect x="{LP}" y="{y}" width="{bw:.1f}" height="{bh}" rx="3" class="f-t1 cv-grow-right" style="--d:{bi * 0.05:.2f}s"/>')
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
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw:.1f}" height="{bh:.1f}" rx="3" class="f-t1 cv-grow-up" style="--d:{i * 0.05:.2f}s"/>')
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
        out.append(f'<polyline points="{path}" fill="none" pathLength="1" class="{scls} cv-draw" style="--d:{si * 0.18:.2f}s" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        if len(pts) <= 30:
            for di, (px, py) in enumerate(pts):
                out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" class="{fcls} cv-fade" style="--d:{si * 0.18 + 0.5 + di * 0.02:.2f}s"/>')
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
    out = [_viz_svg_open(220, 220), '<g class="cv-spin">']
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
    out.append("</g></svg>")
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
        out.append(f'<rect x="{x1:.1f}" y="{y}" width="{max(x2 - x1, 2):.1f}" height="{bh}" rx="10" class="{fcls} cv-grow-right" style="--d:{i * 0.06:.2f}s" fill-opacity="0.85"/>')
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
        f'<div class="tlrow cv-rise" style="--d:{i * 0.07:.2f}s"><div class="tldate">{_viz_esc(e["date"])}</div>'
        f'<div class="tlbody">{_viz_esc(e["label"])}</div></div>'
        for i, e in enumerate(events)
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
            parts.append(f'<span class="flowarrow cv-fade" style="--d:{(2 * i - 1) * 0.08:.2f}s">→</span>')
        parts.append(f'<span class="flowstep cv-fade" style="--d:{2 * i * 0.08:.2f}s">{_viz_esc(s)}</span>')
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
# Deterministic generative covers: every corpus gets its own engraved bookplate
# — a monogram plate, a hatch band, a spine label, or a stroke-only figure plate.
# All variants sit on baked plate paper (#f6f2e7) and carry a category seal.

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


def _cover_monogram(r, pal, slug):
    """Double frame, the slug's two leading initials in engraved serif, corner dots."""
    tokens = [w for w in slug.split("-") if w][:2]
    initials = "".join(t[0] for t in tokens).upper() or slug[:1].upper()
    if len(initials) < 2 and tokens and len(tokens[0]) >= 2:
        initials = tokens[0][:2].upper()
    parts = [
        f"<rect x='7' y='7' width='306' height='126' fill='none' stroke='{pal[0]}' stroke-width='1.5'/>",
        f"<rect x='12' y='12' width='296' height='116' fill='none' stroke='{pal[0]}' stroke-width='1'/>",
        f"<text x='160' y='70' text-anchor='middle' dominant-baseline='central' "
        f"font-family='Georgia, serif' font-size='58' letter-spacing='6' fill='{pal[0]}'>{initials}</text>",
    ]
    for cx, cy in ((9.5, 9.5), (310.5, 9.5), (9.5, 130.5), (310.5, 130.5)):
        parts.append(f"<circle cx='{cx}' cy='{cy}' r='2.5' fill='{pal[1]}'/>")
    return parts


def _cover_hatch(r, pal, slug):
    """Single frame, a horizon rule, 45-degree hatching beneath, a roundel above."""
    horizon = int(88 + next(r) * 10)
    parts = [
        f"<rect x='8' y='8' width='304' height='124' fill='none' stroke='{pal[0]}' stroke-width='1'/>",
        f"<line x1='8' y1='{horizon}' x2='312' y2='{horizon}' stroke='{pal[0]}' stroke-width='1'/>",
        f"<clipPath id='hz-{slug[:24]}'><rect x='8' y='{horizon}' width='304' height='{132 - horizon}'/></clipPath>",
    ]
    hatch = []
    span = 132 - horizon
    for x in range(8 - span, 312, 4):
        hatch.append(f"<line x1='{x}' y1='132' x2='{x + span}' y2='{horizon}' "
                     f"stroke='{pal[0]}' stroke-width='1.2' opacity='.5'/>")
    parts.append(f"<g clip-path='url(#hz-{slug[:24]})'>" + "".join(hatch) + "</g>")
    rcx, rcy = int(266 + next(r) * 24), int(28 + next(r) * 12)
    for rr in (4, 8, 12):
        parts.append(f"<circle cx='{rcx}' cy='{rcy}' r='{rr}' fill='none' stroke='{pal[2]}' stroke-width='1'/>")
    return parts


def _cover_spine(r, pal, slug):
    """A field wash under a centered spine-label box with the short title."""
    label = " ".join(w for w in slug.split("-") if w and w != "research").upper()[:18].strip()
    return [
        f"<rect width='320' height='140' fill='{pal[0]}' fill-opacity='.08'/>",
        f"<rect x='65' y='38' width='190' height='64' fill='#f6f2e7' stroke='{pal[0]}' stroke-width='1'/>",
        f"<rect x='69' y='42' width='182' height='56' fill='none' stroke='{pal[0]}' stroke-width='1'/>",
        f"<text x='160' y='70' text-anchor='middle' dominant-baseline='central' "
        f"font-family='Helvetica, Arial, sans-serif' font-size='12' letter-spacing='2' "
        f"fill='{pal[0]}'>{html.escape(label)}</text>",
    ]


def _cover_figure(r, pal, slug):
    """A seeded stroke-only engraving: a fluted column, nested arches, or an orrery."""
    parts = [f"<rect x='8' y='8' width='304' height='124' fill='none' stroke='{pal[0]}' stroke-width='1'/>"]
    kind = int(next(r) * 3) % 3
    if kind == 0:  # fluted column
        cx = 120 + next(r) * 80
        for i, dx in enumerate((-14, -7, 0, 7, 14)):
            parts.append(f"<line x1='{cx + dx:.0f}' y1='44' x2='{cx + dx:.0f}' y2='118' "
                         f"stroke='{pal[i % 4]}' stroke-width='1.5'/>")
        parts.append(f"<path d='M{cx - 26:.0f} 44 L{cx + 26:.0f} 44 M{cx - 20:.0f} 38 L{cx + 20:.0f} 38 "
                     f"M{cx - 26:.0f} 118 L{cx + 26:.0f} 118 M{cx - 32:.0f} 124 L{cx + 32:.0f} 124' "
                     f"stroke='{pal[0]}' stroke-width='1.5' fill='none'/>")
    elif kind == 1:  # nested arches
        for i, w in enumerate((220, 160, 100)):
            x0, x1 = 160 - w / 2, 160 + w / 2
            parts.append(f"<path d='M{x0:.0f} 124 Q160 {26 + i * 22:.0f} {x1:.0f} 124' "
                         f"fill='none' stroke='{pal[i % 4]}' stroke-width='1.5'/>")
        parts.append(f"<line x1='40' y1='124' x2='280' y2='124' stroke='{pal[0]}' stroke-width='1.5'/>")
    else:  # orrery
        cx, cy = 160, 70
        for i, rr in enumerate((18, 32, 46)):
            parts.append(f"<ellipse cx='{cx}' cy='{cy}' rx='{rr * 2}' ry='{rr}' fill='none' "
                         f"stroke='{pal[i % 4]}' stroke-width='1.5'/>")
            a = next(r) * 6.28318
            px, py = cx + rr * 2 * math.cos(a), cy + rr * math.sin(a)
            parts.append(f"<circle cx='{px:.0f}' cy='{py:.0f}' r='3' fill='none' "
                         f"stroke='{pal[(i + 1) % 4]}' stroke-width='1.5'/>")
        parts.append(f"<circle cx='{cx}' cy='{cy}' r='4' fill='none' stroke='{pal[0]}' stroke-width='1.5'/>")
    return parts


_SEAL_SHAPES = {
    "Faith & Religion": "circle",
    "Mind & Philosophy": "ellipse",
    "Platforms & Deals": "lozenge",
    "Programmatic Infrastructure": "lozenge",
    "Identity & Addressing": "lozenge",
    "Trust, Politics & Society": "lozenge",
    "Global Markets": "lozenge",
    "Media Theory": "lozenge",
    "The Modern World": "square",
    "Thomas Carlyle": "triangle",
    "Heritage": "rings",
}


def _cover_seal(cat, pal):
    """Category seal: 14px stroke-only mark, lower-left inside the frame."""
    kind = _SEAL_SHAPES.get(cat, "rings")
    s, cx, cy = pal[0], 24, 116
    if kind == "circle":
        return f"<circle cx='{cx}' cy='{cy}' r='7' fill='none' stroke='{s}' stroke-width='1.5'/>"
    if kind == "ellipse":
        return f"<ellipse cx='{cx}' cy='{cy}' rx='8' ry='5.5' fill='none' stroke='{s}' stroke-width='1.5'/>"
    if kind == "lozenge":
        return (f"<rect x='{cx - 5}' y='{cy - 5}' width='10' height='10' fill='none' stroke='{s}' "
                f"stroke-width='1.5' transform='rotate(45 {cx} {cy})'/>")
    if kind == "square":
        return f"<rect x='{cx - 6}' y='{cy - 6}' width='12' height='12' fill='none' stroke='{s}' stroke-width='1.5'/>"
    if kind == "triangle":
        return f"<path d='M{cx} {cy - 7} L{cx + 7} {cy + 6} L{cx - 7} {cy + 6} Z' fill='none' stroke='{s}' stroke-width='1.5'/>"
    return (f"<circle cx='{cx}' cy='{cy}' r='7' fill='none' stroke='{s}' stroke-width='1.5'/>"
            f"<circle cx='{cx}' cy='{cy}' r='3.5' fill='none' stroke='{s}' stroke-width='1.5'/>")


def cover_svg(slug, palette=None, cat=None):
    seed = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16)
    r = _rng(seed)
    pal = tuple(palette) if palette and len(palette) >= 4 else COVER_PALETTES[(seed >> 3) % 4]
    variant = seed % 4
    parts = ["<rect width='320' height='140' fill='#f6f2e7'/>"]
    parts += [_cover_monogram, _cover_hatch, _cover_spine, _cover_figure][variant](r, pal, slug)
    if cat is not None:
        parts.append(_cover_seal(cat, pal))
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


def card_cover(slug, title="", palette=None, cat=None):
    """The library-card cover: a custom photo if one exists, else generative art.

    The photo is base64-embedded so index.html stays self-contained (shareable as
    a single folder, works from file://). object-fit:cover in the CSS crops any
    aspect ratio to the card band, so the source image can be any size.
    """
    img = find_cover_image(slug)
    if img is None:
        return cover_svg(slug, palette, cat)
    # Cards are ~260px wide; embedding the 1400px source 36 times is what made
    # the home page weigh 20MB. Cap at 2x the display width.
    mime, data = _fit_image_bytes(img, 560, 78)
    b64 = base64.b64encode(data).decode("ascii")
    alt = html.escape(title or slug)
    return (f'<img class="cover-photo" src="data:{mime};base64,{b64}" alt="{alt}" '
            f'decoding="async" loading="lazy">')


def hero_svg():
    """The library hero: an engraved reading-room arch with a hanging reading lamp.
    Stroke-only in currentColor (CSS sets .hero-art { color: var(--text) }) so themes
    re-ink it for free; the lamp's flame is the one baked accent. No tiles, no other fills."""
    # 45deg tympanum hatch, 5px pitch, clipped under the arch (opacity/stroke set on the group)
    hatch = "".join(f"<path d='M{a} 226 l 90 -90'/>" for a in range(90, 566, 5))
    return (
        "<svg viewBox='0 0 660 236' xmlns='http://www.w3.org/2000/svg' role='img' "
        "aria-label='An engraved reading-room arch and lamp'>"
        "<defs><clipPath id='hero-arch'>"
        "<path d='M90 226 C 90 10, 570 10, 570 226 Z'/>"
        "</clipPath></defs>"
        # baseline rule
        "<rect class='hero-ground' fill='#1e1b16' opacity='.85' x='0' y='226' width='660' height='2'/>"
        # hatching under the arch shoulders
        "<g clip-path='url(#hero-arch)' stroke='currentColor' stroke-width='1' opacity='.3'>"
        + hatch +
        "</g>"
        # the catenary arch (inner 2.5px) + double outer line (offset 6px, 1px), and the lamp (2px)
        "<g fill='none' stroke='currentColor'>"
        "<path stroke-width='2.5' d='M90 226 C 90 10, 570 10, 570 226'/>"
        "<path stroke-width='1' d='M84 226 C 84 4, 576 4, 576 226'/>"
        "<line x1='330' y1='64' x2='330' y2='120' stroke-width='2'/>"
        "<path stroke-width='2' d='M311 150 L322 120 L338 120 L349 150 Z'/>"
        "</g>"
        # the flame — the one color
        "<circle class='hero-core' cx='330' cy='163' r='6' fill='#9a2c1a'/>"
        "</svg>"
    )


_EMBED_CACHE = {}


def _fit_image_bytes(path, max_px, quality):
    """Downscale a source image to the size it is actually displayed at and
    re-encode it, so a 30px logo doesn't ship as 750KB of base64. Returns
    (mime, bytes). Falls back to the untouched file if Pillow isn't available
    or the image is already small enough."""
    key = (str(path), max_px, quality)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if max_px:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
                if max(im.size) > max_px:
                    im.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                if has_alpha:
                    im.convert("RGBA").save(buf, format="PNG", optimize=True)
                    mime = "image/png"
                else:
                    im.convert("RGB").save(buf, format="JPEG", quality=quality,
                                           optimize=True, progressive=True)
                    mime = "image/jpeg"
                if buf.tell() < len(raw):
                    raw = buf.getvalue()
        except Exception:
            pass  # keep the original bytes; a heavy page beats a broken one
    _EMBED_CACHE[key] = (mime, raw)
    return mime, raw


_URI_RE = re.compile(r"data:image/(png|jpe?g|webp);base64,([A-Za-z0-9+/]+={0,2})")
_URI_CACHE = {}


def shrink_data_uris(text, max_px=1600, quality=80, min_bytes=400_000):
    """Cap the size of base64 images embedded in corpus prose.

    The scene illustrations are baked into the chapter markdown, and the current
    ones are already sized for the reader column (~1100px), so this is a guard
    rather than a routine pass: anything under `min_bytes` is skipped without
    being decoded. Source files are never touched."""
    if not text:
        return text
    try:
        from PIL import Image
    except Exception:
        return text

    def repl(m):
        b64 = m.group(2)
        if len(b64) * 3 // 4 < min_bytes:
            return m.group(0)
        key = hashlib.sha1(b64.encode("ascii")).hexdigest()
        if key in _URI_CACHE:
            return _URI_CACHE[key]
        try:
            raw = base64.b64decode(b64)
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                if max(im.size) > max_px:
                    im.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=quality,
                                       optimize=True, progressive=True)
            if buf.tell() >= len(raw):
                out = m.group(0)
            else:
                out = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            out = m.group(0)
        _URI_CACHE[key] = out
        return out

    return _URI_RE.sub(repl, text)


def embedded_image(filename, class_name, alt, max_px=None, quality=82, sizing=""):
    """Embed a project image as a data URI so the page stays self-contained.

    `max_px` caps the embedded pixel dimensions — pass the largest size the
    image is ever *displayed* at (times 2 for retina). `sizing` supplies the
    intrinsic width/height attributes that stop the image from reflowing the
    page as it decodes."""
    img = HERE / filename
    if not img.exists():
        return ""
    mime, data = _fit_image_bytes(img, max_px, quality)
    b64 = base64.b64encode(data).decode("ascii")
    return (f"<img class='{html.escape(class_name, quote=True)}' "
            f"src='data:{mime};base64,{b64}' "
            f"alt='{html.escape(alt, quote=True)}' decoding='async'{sizing}>")


def top_header_art():
    """The full-width Machine Humanities visual that opens the home page."""
    if not USE_TOP_HEADER_IMAGE:
        return ""
    img = embedded_image(TOP_HEADER_IMAGE, "top-header-img", "Machine Humanities reading room",
                         max_px=1800, quality=80, sizing=" width='1800' height='693'")
    return f'<div class="top-header-art">{img}</div>' if img else ""


def brand_logo_art():
    # Rendered at 30px in the masthead — embed at 2x, not at the source's 640px.
    return embedded_image(HERO_IMAGE, "mh-logo", "Divine Hero Agent",
                          max_px=72, sizing=" width='30' height='30'")


def section_agent_art(kind="library", compact=False):
    """Reuse the homepage mascot with section-specific framing so the agents feel like siblings."""
    # Namespaced accent class: a bare "library" collides with the site-wide
    # .library container rule and silently pads the chip.
    accent = "agent-library" if kind == "library" else "agent-adtech"
    label = "Research agent" if kind == "library" else "Ad Tech agent"
    filename = RESEARCH_HERO_IMAGE if kind == "library" else ADTECH_HERO_IMAGE
    wrap = "agent-chip compact" if compact else "agent-chip"
    # 96px in the mirror spread, 285px on a desk front — embed at 2x either way.
    box = 96 if compact else 285
    img = embedded_image(filename, f"hero-img mascot-img agent-portrait {accent}", label,
                         max_px=box * 2, sizing=f" width='{box}' height='{box}'")
    if not img:
        return hero_svg()
    return (
        f'<div class="{wrap} {accent}">'
        f'{img}'
        f'<span class="agent-chip-label">{html.escape(label)}</span>'
        '</div>'
    )


def hero_cta_html(hub_desk=None):
    """The home hero's entry points. The fold already carries the banner art and
    the mascot; a third decorative plate here just pushed the library further
    down, so this row sends people into the shelves instead."""
    links = [('research.html', 'Enter the library', 'primary')]
    if hub_desk:
        links.append((hub_desk["href"], hub_desk["title"], ''))
    links.append(('forecast.html', 'The Forecast Desk', ''))
    out = []
    for href, label, kind in links:
        cls = "hero-cta-btn" + (" primary" if kind == 'primary' else "")
        arrow = ' <span aria-hidden="true">→</span>' if kind == 'primary' else ''
        out.append(f'<a class="{cls}" href="{html.escape(href, quote=True)}">'
                   f'{html.escape(label)}{arrow}</a>')
    return f'<div class="hero-cta">{"".join(out)}</div>'


def hero_art():
    """The home-page hero. When USE_HERO_IMAGE is on, embeds the Divine mascot image;
    otherwise uses the engraved lintel SVG."""
    if USE_HERO_IMAGE:
        return embedded_image(HERO_IMAGE, "hero-img mascot-img", "Divine Hero Agent",
                              max_px=570, sizing=" width='285' height='285'")
    return hero_svg()


def desk_hero_art(slug=""):
    """Section-front hero art for detached desks."""
    if slug == "research":
        return section_agent_art("library")
    if slug == "adtech":
        return section_agent_art("adtech")
    return hero_svg()


# ---------------------------------------------------------------- templates

# The site-wide header nav — one canonical link set rendered identically on
# every page (index, sections, desks, editions, detail pages), so no page ever
# shows a subset of the others. `prefix` is "" at the docs/ root or "../" for
# pages one directory down; `active` is the bare filename of the current
# section (e.g. "adtech.html") to underline, or None.
MAIN_NAV_ITEMS = [
    ("The Research", "research.html"),
    ("The Ghost of Times", "ghost.html"),
    ("Ad Tech", "adtech.html"),
    ("The Pamphlets", "pamphlets.html"),
    ("The Forecast Desk", "forecast.html"),
    ("Connections", "connections.html"),
    ("Glossary", "glossary.html"),
    ("Quiz", "quiz.html"),
    ("Wrapped", "wrapped.html"),
]


def main_nav_html(prefix="", active=None):
    lines = []
    for label, href in MAIN_NAV_ITEMS:
        cls = ' class="active"' if href == active else ''
        lines.append(f'    <a href="{prefix}{href}"{cls}>{label}</a>')
    return "\n".join(lines)


READER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
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
  <a class="back" href="{back_href}">← {back_label}</a>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <h1>{title}</h1>
  <p class="subtitle">{subtitle}</p>
  <p class="mh-stamp" aria-hidden="true">Machine generated</p>
  <div id="reader-controls">
    <button id="listen-btn" title="Listen to this chapter">▶ Listen</button>
    <button id="type-btn" title="Text size &amp; width" aria-haspopup="true">Aa</button>
    <button id="terms-btn" title="Glossary of terms" aria-haspopup="true" hidden>❔ Terms</button>
    <a id="quiz-btn" href="quiz.html" title="Test yourself on this corpus">✏ Quiz</a>
    <button id="share-btn" title="Share this chapter">Share ↗</button>
    <button id="theme-btn">◐ Theme</button>
  </div>
  <input id="search" type="search" placeholder="Search the corpus…" autocomplete="off">
  <div id="search-results"></div>
  <nav id="toc"></nav>
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
<template id="reader-scene-template">{scene}</template>
<script id="corpus-data" type="application/json">{data_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

CSS = """
:root {
  /* paper + ink */
  --bg: #fcfbf7;            /* warm paper white — the room */
  --panel: #f4f1e8;         /* mat board — sidebar, code, input wells, atlas landmass, figure plates ONLY */
  --text: #1e1b16;          /* letterpress ink */
  --muted: #6e6759;         /* pencil annotation */
  --accent: #9a2c1a;        /* collection-stamp red */
  --border: #d8d3c4;        /* the engraved hairline — load-bearing site-wide */
  --mark: #f4e7ad;          /* reading-slip highlight */
  --cover-bg: #f6f2e7;      /* plate paper */
  /* plate inks (the old trencadís slots): carmine, ochre, Prussian, verdigris, aubergine */
  --t1: #9a2c1a; --t2: #a3771c; --t3: #274d68; --t4: #4a6350; --t5: #64405a;
  /* elevation: flat by decree; only floating overlays get shadow-2/-3 */
  --shadow-1: 0 1px 0 rgba(30,27,22,.06);
  --shadow-2: 0 2px 10px rgba(30,27,22,.08);
  --shadow-3: 0 18px 44px rgba(30,27,22,.16);
  /* motion: one curve, no springs (token kept for consumers; bounce removed) */
  --ease: cubic-bezier(.25,.1,.25,1);
  --ease-spring: cubic-bezier(.25,.1,.25,1);
  /* focus: double ring — visible on any themed ground */
  --ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent);
  /* type */
  --display: Baskerville, 'Hoefler Text', 'Palatino Linotype', 'Book Antiqua', Cambria, Georgia, serif;
  --serif: Charter, 'Bitstream Charter', 'Iowan Old Style', Georgia, 'Times New Roman', serif;
  --sans: system-ui, -apple-system, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  --mono: ui-monospace, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
  --hr-glyph: "· · ·";      /* quiet asterism; themes override freely */
  --fig-radius: 2px;        /* was 16px; themed corpora that set fig_radius keep theirs */
}
[data-theme="dark"] {
  /* the reading room after hours — lamplit ink on slate, flat, no glow */
  --bg: #181512;
  --panel: #211e19;
  --text: #eae6db;
  --muted: #9b9384;
  --accent: #d98055;        /* beside the #d98f5f the 30 dark theme blocks were tuned against */
  --border: #3a362f;
  --mark: #5a4a1d;
  --cover-bg: #232019;
  --t1: #d0704f; --t2: #cfa93e; --t3: #6e98b1; --t4: #8ba576; --t5: #a97b9c;
  --shadow-1: 0 1px 0 rgba(0,0,0,.35);
  --shadow-2: 0 2px 12px rgba(0,0,0,.45);
  --shadow-3: 0 18px 48px rgba(0,0,0,.60);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--serif);
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; text-rendering: optimizeLegibility; }
#sidebar {
  position: fixed; top: 0; left: 0; bottom: 0; width: 320px; overflow-y: auto;
  background: var(--panel); border-right: 1px solid var(--border); padding: 1.2rem 1.2rem 2rem;
}
.tiles { display: flex; gap: 2px; margin: .9rem 0 .2rem; }
.tiles span { width: 7px; height: 7px; border-radius: 0; transform: none; box-shadow: none; }
.tiles span:nth-child(1) { background: var(--t1); }
.tiles span:nth-child(2) { background: var(--t2); }
.tiles span:nth-child(3) { background: var(--t3); }
.tiles span:nth-child(4) { background: var(--t4); }
#sidebar h1 { font-family: var(--display); font-weight: 600; font-size: 1.28rem; line-height: 1.28; margin: .5rem 0 .25rem; }
#sidebar .subtitle { font-size: .8rem; color: var(--muted); margin: 0 0 1rem; font-family: var(--sans); }
#sidebar .mh-stamp { display: inline-block; padding: .3em .65em; border: 1.5px solid var(--accent);
  color: var(--accent); font-family: var(--mono); font-size: .68rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase; transform: rotate(-1.5deg);
  opacity: .92; border-radius: 1px; margin: .2rem 0 1rem; }
.back { font-family: var(--sans); font-size: .68rem; font-weight: 600; color: var(--muted); text-decoration: none;
  text-transform: uppercase; letter-spacing: .14em;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-size: 0% 1px; background-repeat: no-repeat; background-position: 0 100%;
  transition: background-size .18s var(--ease), color .14s var(--ease); }
.back:hover, .back:focus-visible { background-size: 100% 1px; color: var(--accent); }
#search { width: 100%; padding: .5rem .7rem; font-size: .85rem; border: 1px solid var(--border);
  border-radius: 2px; background: var(--bg); color: var(--text); font-family: var(--sans); }
#search:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
#toc { margin-top: .8rem; }
#toc a { display: block; padding: .45rem .55rem; margin: .1rem 0; border-radius: 0;
  color: var(--text); text-decoration: none; font-family: var(--sans); font-size: .84rem; line-height: 1.35;
  border-left: 2px solid transparent;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-repeat: no-repeat; background-size: 3px 0%; background-position: 0 0;
  transition: background-size .18s var(--ease), background-color .15s var(--ease),
              border-color .15s var(--ease), color .15s var(--ease); }
#toc a .num { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin-right: .4rem; }
#toc a:hover, #toc a:focus-visible { background-color: var(--bg); background-size: 3px 100%; }
#toc a.active { background: transparent; color: var(--accent); font-weight: 600; border-left-color: var(--accent); }
#search-results { font-family: var(--sans); font-size: .8rem; }
#search-results .hit { padding: .5rem .55rem; border-bottom: 1px solid var(--border); cursor: pointer; border-radius: 0; }
#search-results .hit:hover { background: var(--bg); }
#search-results .hit b { color: var(--accent); display: block; margin-bottom: .15rem; }
#search-results mark { background: var(--mark); color: inherit; border-radius: 2px; }
#search-results .none { color: var(--muted); padding: .5rem .55rem; }
#reader-controls { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1.2rem; }
#reader-controls button, #reader-controls a { margin: 0; font-family: var(--sans); font-size: .66rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .08em; color: var(--muted);
  background: transparent; border: 1px solid var(--border); border-radius: 2px; padding: .35rem .7rem; cursor: pointer;
  text-decoration: none; display: inline-block; }
#reader-controls button:hover, #reader-controls a:hover { color: var(--accent); border-color: var(--text); }
#listen-btn.on { color: var(--bg); background: var(--accent); border-color: var(--accent); }
/* floating Listen bar (Web Speech narration) */
#listen-bar { position: fixed; left: 50%; bottom: 1rem; transform: translateX(-50%); z-index: 40; display: none;
  align-items: center; gap: .7rem; flex-wrap: wrap; max-width: 92vw; background: var(--panel);
  border: 1px solid var(--border); border-radius: 2px; padding: .5rem .8rem; box-shadow: var(--shadow-2); }
#listen-bar.show { display: flex; }
#listen-bar button { font-family: var(--sans); font-size: 1rem; background: none; border: 1px solid var(--border);
  border-radius: 2px; width: 34px; height: 30px; color: var(--text); cursor: pointer; }
#listen-bar button:hover { border-color: var(--accent); color: var(--accent); }
#lb-title { font-family: var(--display); font-size: .9rem; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#listen-bar .lb-ctl { font-family: var(--mono); font-size: .68rem; font-weight: 500; color: var(--muted); text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; display: flex; align-items: center; gap: .3rem; }
#listen-bar select { font-family: var(--sans); font-size: .72rem; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 2px; padding: .15rem .3rem; }
#lb-voice { max-width: 160px; }
@media (max-width: 560px) { #lb-title { display: none; } #lb-voice { max-width: 110px; } }
#main { margin-left: 320px; display: flex; flex-direction: column; min-height: 100vh; }
#content { max-width: var(--reader-measure, 700px); width: 100%; margin: 0 auto; padding: 3.5rem 2rem 4rem;
  font-size: var(--reader-fs, 1.04rem); line-height: 1.75; flex: 1; }
/* top reading-progress bar */
#reader-progress { position: fixed; top: 0; left: 0; height: 3px; width: 0; z-index: 30;
  background: var(--accent); transition: width .08s linear; }
/* text size / measure popover (toggled by the Aa button) */
#type-panel { position: fixed; z-index: 35; background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .8rem; width: 220px; display: none; }
#type-panel.open { display: block; }
#type-panel .tp-row { display: flex; align-items: center; justify-content: space-between; gap: .6rem; margin-bottom: .6rem; }
#type-panel .tp-row:last-child { margin-bottom: 0; }
#type-panel .tp-lab { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--muted); }
#type-panel .tp-grp { display: flex; gap: .3rem; }
#type-panel .tp-grp button { font-family: var(--sans); font-size: .8rem; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-radius: 2px; padding: .3rem .55rem; cursor: pointer; min-width: 2rem; }
#type-panel .tp-grp button:hover { border-color: var(--accent); color: var(--accent); }
#type-panel .tp-grp button.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
/* glossary — inline term chip + hover/tap definition popover + terms panel */
.gloss { display: inline; appearance: none; -webkit-appearance: none; font: inherit; line-height: inherit;
  color: inherit; background: none; border: none; padding: 0; margin: 0; cursor: help;
  border-bottom: 1px dashed color-mix(in srgb, var(--accent) 55%, transparent);
  text-decoration: none; -webkit-tap-highlight-color: transparent; }
.gloss:hover, .gloss:focus-visible { color: var(--accent); border-bottom-color: var(--accent); outline: none; }
.gloss:focus-visible { box-shadow: var(--ring); border-radius: 2px; }
.gloss::after { content: ""; }
#gloss-pop { position: fixed; z-index: 90; max-width: min(300px, calc(100vw - 16px)); width: max-content; background: var(--bg);
  border: 1px solid var(--border); border-radius: 2px; box-shadow: var(--shadow-2); padding: .7rem .8rem;
  font-family: var(--sans); font-size: .82rem; line-height: 1.5; color: var(--text); display: none;
  animation: glossIn .14s var(--ease) both; }
#gloss-pop.open { display: block; }
#gloss-pop .gp-term { font-family: var(--display); font-style: italic; font-weight: 600; color: var(--accent); font-size: .9rem; margin-bottom: .2rem; }
@keyframes glossIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { #gloss-pop { animation: none; } }
#gloss-panel { position: fixed; z-index: 36; background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .5rem; width: 320px; max-width: 88vw;
  max-height: 60vh; overflow-y: auto; display: none; }
#gloss-panel.open { display: block; }
#gloss-panel h4 { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em;
  color: var(--muted); margin: .3rem .5rem .5rem; }
#gloss-panel dl { margin: 0; }
#gloss-panel dt { font-family: var(--display); font-weight: 600; font-size: .9rem; color: var(--accent);
  padding: .5rem .5rem 0; }
#gloss-panel dd { font-family: var(--sans); font-size: .8rem; line-height: 1.5; color: var(--text);
  margin: .12rem 0 .3rem; padding: 0 .5rem .45rem; border-bottom: 1px solid var(--border); }
#gloss-panel dd:last-child { border-bottom: none; }
#content h1 { font-family: var(--display); font-weight: 600; font-size: 2.1rem; line-height: 1.15; margin-top: 0; }
#content h1::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
#content h2 { font-family: var(--display); font-weight: 600; font-size: 1.45rem; margin-top: 2.6rem; line-height: 1.3; }
#content h3 { font-family: var(--display); font-size: 1.15rem; font-weight: 600; }
#content a { color: var(--accent); text-underline-offset: .18em; overflow-wrap: anywhere; }
#content a:hover { text-decoration-thickness: 1.5px; }
#content em, #content strong { overflow-wrap: anywhere; }
/* injected chapter feature well: ruled mono byline + drop cap on the opening paragraph */
#content .ch-byline { font-family: var(--mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums;
  color: var(--muted); border-bottom: 1px solid var(--border);
  padding: .15rem 0 .8rem; margin: 1.1rem 0 1.6rem; }
#content .ch-byline + p::first-letter { font-family: var(--display); font-weight: 600;
  float: left; font-size: 3.1em; line-height: .74; padding: .05em .09em 0 0; color: var(--accent); }
#content blockquote { margin: 1.6rem 0; padding: .15rem 0 .15rem 1.2rem; border-left: 2px solid var(--accent);
  background: transparent; border-radius: 0; color: var(--muted); font-style: italic; }
#content code { background: var(--panel); padding: .1em .35em; border-radius: 2px; font-size: .88em; overflow-wrap: anywhere; }
#content pre { background: var(--panel); padding: 1rem; border-radius: 2px; overflow-x: auto; }
#content pre code { background: none; padding: 0; }
#content table { border-collapse: collapse; font-family: var(--sans); font-size: .85rem; width: 100%; table-layout: fixed; margin: 1.2rem 0; }
#content th, #content td { border: 1px solid var(--border); padding: .45rem .6rem; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
#content th { background: var(--panel); }
#content img { max-width: 100%; }
#content hr { border: none; margin: 2.6rem 0; text-align: center; height: 1em; }
#content hr::before { content: var(--hr-glyph, "· · ·"); color: var(--accent); opacity: .45; font-size: .7rem; letter-spacing: 1.1em;
  padding-left: 1.1em; }
figure.corpus-fig { margin: 2.2rem 0; padding: 1.1rem 1.1rem .9rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--fig-radius, 2px); box-shadow: none; }
figure.corpus-fig svg { width: 100%; height: auto; display: block; }
figure.corpus-fig figcaption { font-family: var(--sans); font-size: .74rem; color: var(--muted);
  text-align: left; margin-top: .7rem; line-height: 1.45; }
figure.corpus-fig figcaption strong { font-family: var(--sans); font-size: .66rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .14em; color: var(--accent); }
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
  border: 1px solid var(--border); border-radius: var(--fig-radius, 2px); box-shadow: none; }
figure.viz .viz-title { font-family: var(--sans); font-size: .95rem; font-weight: 700;
  color: var(--text); margin: 0 0 .9rem; line-height: 1.3; }
figure.viz svg { width: 100%; height: auto; display: block; }
figure.viz svg text { font-family: var(--sans); fill: var(--text); }
figure.viz figcaption { font-family: var(--sans); font-size: .74rem; color: var(--muted);
  text-align: left; margin-top: .8rem; line-height: 1.5; }
figure.viz figcaption .credit { font-family: var(--mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; }
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
.viz-legend i { width: 12px; height: 12px; border-radius: 0; display: inline-block; }
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
  border-radius: 2px; padding: 8px 14px; line-height: 1.35; max-width: 230px; }
.viz-flow .flowarrow { color: var(--accent); font-weight: 700; font-size: 18px; }
.viz-error { margin: 1.4rem 0; padding: 14px 18px; border: 1px dashed var(--accent);
  border-radius: 0; background: var(--panel); font-family: var(--sans); font-size: .8rem; color: var(--text); }
.viz-error pre { margin: 10px 0 0; font-size: .72rem; white-space: pre-wrap; }
/* ── animated reveal on scroll (corpus-visuals) ──────────────────────────────
   Figures and their marks start hidden ONLY after the page JS adds .reveal-armed
   (it skips this under prefers-reduced-motion), so with no-JS or reduced motion
   everything is fully visible by default — motion is purely additive. When a
   figure scrolls into view the observer adds .is-revealed and CSS does the rest.
   The cv-* classes are a shared reveal vocabulary used by BOTH the build-time viz
   charts and hand-authored corpus-fig figures; the per-mark `--d` var staggers
   them. transform-box:fill-box anchors each SVG mark's scale to its own geometry. */
figure.viz, figure.corpus-fig { --cv-ease: cubic-bezier(.25,.1,.25,1); }
.reveal-armed.viz, .reveal-armed.corpus-fig { opacity: 0; transform: translateY(6px);
  transition: opacity .35s var(--cv-ease), transform .35s var(--cv-ease); will-change: opacity, transform; }
.reveal-armed.is-revealed { opacity: 1; transform: none; }
.reveal-armed .cv-fade { opacity: 0; transition: opacity .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-fade { opacity: 1; }
.reveal-armed .cv-rise { opacity: 0; transform: translateY(6px);
  transition: opacity .35s var(--cv-ease) var(--d, 0s), transform .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-rise { opacity: 1; transform: none; }
.reveal-armed .cv-grow-up { transform: scaleY(0); transform-box: fill-box; transform-origin: bottom;
  transition: transform .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-grow-up { transform: scaleY(1); }
.reveal-armed .cv-grow-right { transform: scaleX(0); transform-box: fill-box; transform-origin: left;
  transition: transform .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-grow-right { transform: scaleX(1); }
.reveal-armed .cv-draw { stroke-dasharray: 1; stroke-dashoffset: 1;
  transition: stroke-dashoffset .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-draw { stroke-dashoffset: 0; }
.reveal-armed .cv-spin { opacity: 0; transform: rotate(-55deg) scale(.72); transform-box: fill-box;
  transform-origin: center; transition: transform .35s var(--cv-ease) var(--d, 0s), opacity .35s var(--cv-ease) var(--d, 0s); }
.reveal-armed.is-revealed .cv-spin { opacity: 1; transform: none; }
@media (prefers-reduced-motion: reduce) {
  .reveal-armed.viz, .reveal-armed.corpus-fig, .reveal-armed .cv-fade, .reveal-armed .cv-rise,
  .reveal-armed .cv-grow-up, .reveal-armed .cv-grow-right, .reveal-armed .cv-draw, .reveal-armed .cv-spin {
    opacity: 1 !important; transform: none !important; stroke-dashoffset: 0 !important; transition: none !important; }
}
#pager { max-width: 700px; width: 100%; margin: 0 auto; padding: 1.1rem 2rem 3rem;
  display: flex; align-items: center; justify-content: space-between; font-family: var(--sans);
  border-top: 1px solid var(--border); }
#pager button { background: none; border: 0; border-radius: 0; padding: .2rem 0;
  font-family: var(--sans); font-size: .68rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .12em; color: var(--accent); cursor: pointer; max-width: 40%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: color .15s var(--ease), transform .16s var(--ease); }
#pager button:hover:not(:disabled) { color: var(--text); }
#prev:hover:not(:disabled) { transform: translateX(-3px); }
#next:hover:not(:disabled) { transform: translateX(3px); }
#pager button:disabled { opacity: .35; cursor: default; }
#pager-label { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); }
#menu-btn { display: none; position: fixed; top: .7rem; left: .7rem; z-index: 20; font-size: 1.1rem;
  background: var(--bg); color: var(--muted); border: 1px solid var(--border); border-radius: 2px;
  box-shadow: var(--shadow-2); padding: .3rem .6rem; cursor: pointer; }
#menu-btn:hover { color: var(--accent); border-color: var(--text); }
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
#related { max-width: 700px; width: 100%; margin: 0 auto; padding: 0 2rem 3rem; }
.related-h { font-family: var(--display); font-weight: 600; font-size: 1.08rem; margin: 0 0 .9rem; padding-top: 1.5rem; border-top: 1px solid var(--border); }
.related-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .8rem; }
.related-card { display: block; background: transparent; border: 1px solid var(--border); border-radius: 0;
  padding: .8rem .9rem; text-decoration: none; color: var(--text); box-shadow: none;
  position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.related-card:hover, .related-card:focus-visible { border-color: var(--text);
  transform: translateY(-2px); box-shadow: var(--shadow-2); z-index: 1; }
.related-card:hover .related-t { color: var(--accent); }
.related-cat { display: block; font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .3rem; }
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
#kbd-help { position: fixed; inset: 0; z-index: 90; background: rgba(24,22,18,.5); display: none;
  align-items: center; justify-content: center; }
#kbd-help.open { display: flex; }
#kbd-help .kh { background: var(--bg); border: 1px solid var(--border); border-radius: 2px; box-shadow: var(--shadow-3); padding: 1.2rem 1.4rem; min-width: 250px; }
#kbd-help h3 { font-family: var(--display); margin: 0 0 .8rem; font-size: 1.05rem; }
#kbd-help dl { display: grid; grid-template-columns: auto 1fr; gap: .45rem 1rem; margin: 0; font-family: var(--sans); font-size: .82rem; }
#kbd-help dt { color: var(--accent); font-weight: 600; white-space: nowrap; }
#kbd-help dd { margin: 0; color: var(--muted); }
/* Punch Pass reduced-motion kill block (A.4) — enumerates every selector this
   constant transforms or draws; state changes stay, travel dies. */
@media (prefers-reduced-motion: reduce) {
  #toc a, #toc a:hover, #toc a:focus-visible,
  #prev, #prev:hover:not(:disabled),
  #next, #next:hover:not(:disabled),
  .related-card, .related-card:hover, .related-card:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

APP_JS = r"""
const corpus = JSON.parse(document.getElementById('corpus-data').textContent);
const docs = corpus.documents;
const toc = document.getElementById('toc');
const content = document.getElementById('content');
// sidebar Quiz link → the Test Yourself page with this corpus preselected
(function () { var qb = document.getElementById('quiz-btn');
  if (qb && corpus.slug) qb.href = 'quiz.html?on=' + encodeURIComponent(corpus.slug); })();
// glossary matchers — built up front so the first chapter's render can wrap chips
// (applyGlossary + the popover/panel wiring live near the end of this script)
var GLOSS = (corpus.glossary || []).filter(function (e) { return e && e.term && e.def; });
var GLOSS_M = (function () {
  if (!GLOSS.length) return [];
  var items = [];
  GLOSS.forEach(function (e) {
    [e.term].concat(e.aliases || []).forEach(function (s) {
      if (!s) return;
      var cs = /[A-Z]/.test(s);                                   // acronyms match case-sensitively
      var q = s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      var re = new RegExp('(^|[^A-Za-z0-9-])(' + q + ')(?![A-Za-z0-9-])', cs ? '' : 'i');
      items.push({ len: s.length, re: re, def: e.def, term: e.term });
    });
  });
  items.sort(function (a, b) { return b.len - a.len; });          // longest phrase wins
  return items;
})();
const searchBox = document.getElementById('search');
const results = document.getElementById('search-results');
const key = 'corpus:' + corpus.slug;
let current = 0;

// theme toggle — the theme itself is applied pre-paint by the <head> boot
// script (dark unless localStorage 'corpus-theme' says 'light')
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
// Animated reveal-on-scroll for charts & figures. Figures stay fully visible
// unless armed here, so this is purely additive (no-JS / reduced-motion = static).
// Correctness rule: a figure must NEVER stay hidden. IntersectionObserver only
// *times* the reveal to scroll; an immediate in-view pass and a backstop timeout
// guarantee every armed figure reveals even if IO is throttled or unsupported.
let revealObserver = null, revealBackstop = null;
function armReveals(root) {
  if (revealObserver) { revealObserver.disconnect(); revealObserver = null; }
  if (revealBackstop) { clearTimeout(revealBackstop); revealBackstop = null; }
  if (reduceMotion) return;
  const figs = [...root.querySelectorAll('figure.viz, figure.corpus-fig')];
  if (!figs.length) return;
  figs.forEach(f => f.classList.add('reveal-armed'));
  const reveal = f => f.classList.add('is-revealed');
  if ('IntersectionObserver' in window) {
    revealObserver = new IntersectionObserver((entries, obs) => {
      entries.forEach(en => { if (en.isIntersecting) { reveal(en.target); obs.unobserve(en.target); } });
    }, { threshold: 0.15, rootMargin: '0px 0px -8% 0px' });
    figs.forEach(f => revealObserver.observe(f));
  }
  // reveal anything already on screen once layout settles — uses setTimeout (not
  // rAF) so above-the-fold figures still reveal where frames/IO are throttled
  setTimeout(() => {
    const vh = window.innerHeight || 800;
    figs.forEach(f => { const r = f.getBoundingClientRect(); if (r.top < vh * 0.92 && r.bottom > 0) reveal(f); });
  }, 60);
  // safety net: never leave a figure invisible, even if IO never fires
  revealBackstop = setTimeout(() => figs.forEach(reveal), 2600);
}
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
  applyGlossary(content);
  // feature well — injected AFTER the glossary pass so its text is never chip-annotated;
  // content.innerHTML is rebuilt per chapter, so the remove() is belt-and-braces.
  var oldWell = content.querySelector('.ch-byline'); if (oldWell) oldWell.remove();
  var well = document.createElement('p'); well.className = 'ch-byline';
  var mins = Math.max(1, Math.round(docs[current].body.split(/\s+/).length / 220));
  well.textContent = 'Chapter ' + (current + 1) + ' of ' + docs.length + ' · ' + mins + ' min read · Machine Humanities';
  var h1 = content.querySelector('h1');
  if (h1) h1.insertAdjacentElement('afterend', well); else content.insertAdjacentElement('afterbegin', well);
  var sceneTpl = document.getElementById('reader-scene-template');
  if (sceneTpl && h1 && sceneTpl.content && sceneTpl.content.firstElementChild) {
    well.insertAdjacentElement('afterend', sceneTpl.content.firstElementChild.cloneNode(true));
  }
  armReveals(content);
  toc.querySelectorAll('a').forEach((a, j) => a.classList.toggle('active', j === current));
  document.getElementById('prev').disabled = current === 0;
  document.getElementById('next').disabled = current === docs.length - 1;
  document.getElementById('prev').textContent = current > 0 ? '← From: ' + docs[current - 1].title : '←';
  document.getElementById('next').textContent = current < docs.length - 1 ? 'Continued: ' + docs[current + 1].title + ' →' : '→';
  document.getElementById('pager-label').textContent = (current + 1) + ' / ' + docs.length;
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
function pickVoice() {
  const sel = lbar && lbar.querySelector('#lb-voice');
  const name = sel && sel.value;
  const vs = synth.getVoices();
  if (name) { const v = vs.find(v => v.name === name); if (v) return v; }
  return vs.find(v => /^en/i.test(v.lang) && /enhanced|premium/i.test(v.name) && v.localService)
    || vs.find(v => /^en/i.test(v.lang) && v.localService)
    || vs.find(v => /^en/i.test(v.lang)) || vs[0];
}
function populateVoices() {
  const sel = lbar && lbar.querySelector('#lb-voice');
  if (!sel) return;
  const vs = synth.getVoices().filter(v => /^en/i.test(v.lang));
  if (!vs.length) return;
  vs.sort((a, b) => {
    const rank = v => /enhanced|premium/i.test(v.name) ? 0 : v.localService ? 1 : 2;
    return rank(a) - rank(b);
  });
  const prev = sel.value;
  sel.innerHTML = vs.map(v => '<option value="' + v.name + '">' + v.name + '</option>').join('');
  if (prev && vs.find(v => v.name === prev)) sel.value = prev;
  if (!localStorage.getItem('listen-voice-tip') && !vs.some(v => /enhanced|premium/i.test(v.name))) {
    localStorage.setItem('listen-voice-tip', '1');
    const tip = document.createElement('div');
    tip.style.cssText = 'position:fixed;bottom:5rem;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:.65rem 1.1rem;font-family:var(--sans);font-size:.78rem;color:var(--muted);z-index:50;max-width:340px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.18);line-height:1.5';
    tip.textContent = 'Tip: download Enhanced or Premium voices in your OS accessibility settings for much richer audio.';
    document.body.appendChild(tip); setTimeout(() => tip.remove(), 8000);
  }
}
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
    + '<label class="lb-ctl">Voice <select id="lb-voice"><option>Loading…</option></select></label>'
    + '<label class="lb-ctl">Speed <select id="lb-rate"><option value="0.8">0.8×</option><option value="1" selected>1×</option><option value="1.2">1.2×</option><option value="1.5">1.5×</option></select></label>'
    + '<label class="lb-ctl">Sleep <select id="lb-sleep"><option value="0" selected>Off</option><option value="1">End of chapter</option><option value="900">15 min</option><option value="1800">30 min</option></select></label>';
  document.body.appendChild(lbar);
  lbar.querySelector('#lb-toggle').onclick = togglePause;
  lbar.querySelector('#lb-stop').onclick = stopListen;
  lbar.querySelector('#lb-rate').onchange = function () { listenRate = +this.value; };
  lbar.querySelector('#lb-sleep').onchange = function () { setSleep(+this.value); };
  populateVoices();
  if (synth.onvoiceschanged !== undefined) synth.addEventListener('voiceschanged', populateVoices);
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
  var MEAS = { narrow: '620px', normal: '700px', wide: '820px' };
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

// ---- glossary: inline definition chips + hover/tap popover + terms panel ----
// The corpus carries a `glossary` ([{term, aliases[], def}]); the first in-text
// occurrence of each term (per chapter) becomes a chip that reveals a plain
// definition on hover (desktop) or tap (touch), with a full terms list behind
// the sidebar "Terms" button. Definitions never distract from the prose.
// (GLOSS / GLOSS_M are defined up top so the first render can wrap chips.)
function applyGlossary(root) {
  if (!GLOSS_M || !GLOSS_M.length) return;
  var used = {};
  var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: function (n) {
      if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      for (var p = n.parentNode; p && p !== root; p = p.parentNode) {
        var t = p.nodeName;
        if (t === 'A' || t === 'CODE' || t === 'PRE' || /^H[1-4]$/.test(t)) return NodeFilter.FILTER_REJECT;
        if (p.classList && (p.classList.contains('gloss') || p.classList.contains('corpus-fig'))) return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  var nodes = []; for (var nd; (nd = walker.nextNode());) nodes.push(nd);
  nodes.forEach(function (node) {
    for (var i = 0; i < GLOSS_M.length; i++) {
      var m = GLOSS_M[i]; if (used[m.term]) continue;
      var mt = m.re.exec(node.nodeValue); if (!mt) continue;
      var pre = mt[1] || '', word = mt[2], start = mt.index + pre.length;
      var after = node.splitText(start);
      after.nodeValue = after.nodeValue.slice(word.length);
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'gloss'; b.textContent = word;
      b.setAttribute('data-term', m.term); b.setAttribute('data-def', m.def);
      b.setAttribute('aria-label', m.term + ': ' + m.def);
      node.parentNode.insertBefore(b, after);
      used[m.term] = 1; break;                                    // one chip per text node
    }
  });
}

(function setupGlossary() {
  if (!GLOSS.length) return;
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  var pop = document.createElement('div'); pop.id = 'gloss-pop'; pop.setAttribute('role', 'tooltip');
  document.body.appendChild(pop);
  var hoverCap = !(window.matchMedia && window.matchMedia('(hover: none)').matches);
  var openFor = null, cEl = document.getElementById('content');
  function open(btn) {
    pop.innerHTML = '<div class="gp-term"></div><div class="gp-def"></div>';
    pop.querySelector('.gp-term').textContent = btn.getAttribute('data-term');
    pop.querySelector('.gp-def').textContent = btn.getAttribute('data-def');
    pop.classList.add('open');
    var r = btn.getBoundingClientRect(), pw = pop.offsetWidth, ph = pop.offsetHeight;
    var left = Math.max(8, Math.min(r.left + r.width / 2 - pw / 2, window.innerWidth - pw - 8));
    var top = r.top - ph - 8; if (top < 8) top = r.bottom + 8;
    pop.style.left = left + 'px'; pop.style.top = top + 'px'; openFor = btn;
  }
  function close() { pop.classList.remove('open'); openFor = null; }
  if (hoverCap) {
    cEl.addEventListener('pointerover', function (e) { var b = e.target.closest && e.target.closest('.gloss'); if (b) open(b); });
    cEl.addEventListener('pointerout', function (e) { var b = e.target.closest && e.target.closest('.gloss'); if (b && openFor === b) close(); });
    cEl.addEventListener('focusin', function (e) { var b = e.target.closest && e.target.closest('.gloss'); if (b) open(b); });
    cEl.addEventListener('focusout', function () { close(); });
  }
  cEl.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.gloss'); if (!b) return;
    e.preventDefault(); if (openFor === b) close(); else open(b);
  });
  document.addEventListener('click', function (e) {
    if (openFor && !(e.target.closest && (e.target.closest('.gloss') || e.target.closest('#gloss-pop')))) close();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  window.addEventListener('scroll', function () { if (openFor) close(); }, { passive: true });

  var tbtn = document.getElementById('terms-btn'); if (!tbtn) return;
  tbtn.hidden = false; var panel = null;
  tbtn.addEventListener('click', function () {
    if (!panel) {
      panel = document.createElement('div'); panel.id = 'gloss-panel';
      var sorted = GLOSS.slice().sort(function (a, b) { return a.term.localeCompare(b.term); });
      var h = '<h4>' + sorted.length + ' terms in this corpus</h4><dl>';
      sorted.forEach(function (e) { h += '<dt>' + esc(e.term) + '</dt><dd>' + esc(e.def) + '</dd>'; });
      panel.innerHTML = h + '</dl>'; document.body.appendChild(panel);
    }
    var opened = panel.classList.toggle('open');
    if (opened) {
      var r = tbtn.getBoundingClientRect();
      panel.style.left = Math.min(r.left, window.innerWidth - panel.offsetWidth - 8) + 'px';
      panel.style.top = (r.bottom + 8) + 'px';
    }
  });
  document.addEventListener('click', function (e) {
    if (panel && panel.classList.contains('open') && !panel.contains(e.target) && e.target !== tbtn) panel.classList.remove('open');
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
/* unified, accessible keyboard focus ring across every interactive surface */
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible,
textarea:focus-visible, [tabindex]:focus-visible, .card:focus-visible, .coll-card:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
:focus:not(:focus-visible) { outline: none; }
/* branded text selection across the whole site */
::selection { background: color-mix(in srgb, var(--accent) 26%, transparent); }
/* themed, unobtrusive scrollbars (overlay on macOS; visible elsewhere) */
* { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
::-webkit-scrollbar { width: 12px; height: 12px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 0; border: 3px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
::-webkit-scrollbar-track { background: transparent; }
#cmdk-fab { position: fixed; left: .9rem; bottom: .9rem; z-index: 60; display: inline-flex; align-items: center; gap: .45rem;
  font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--muted);
  background: var(--bg); border: 1px solid var(--border); border-radius: 2px; padding: .4rem .7rem; cursor: pointer;
  box-shadow: var(--shadow-2); transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#cmdk-fab:hover, #cmdk-fab:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }
#cmdk-fab kbd { font-family: var(--mono); font-size: .7rem; background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; padding: .02rem .32rem; color: inherit; }
#cmdk-back { position: fixed; inset: 0; z-index: 80; background: rgba(24,22,18,.45); -webkit-backdrop-filter: blur(3px);
  backdrop-filter: blur(3px); display: none; align-items: flex-start; justify-content: center; padding: 12vh 1rem 1rem; }
#cmdk-back.open { display: flex; }
#cmdk { width: 100%; max-width: 560px; background: var(--bg); border: 1px solid var(--border); border-radius: 2px; overflow: hidden;
  box-shadow: var(--shadow-3); animation: cmdkIn .16s var(--ease) both; }
#cmdk::before { content: "Call slip"; display: block; padding: .55rem 1rem 0;
  font-family: var(--sans); font-size: .6rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .16em; color: var(--muted); }
@keyframes cmdkIn { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { #cmdk { animation: none; } }
#cmdk-in { display: flex; align-items: center; gap: .6rem; padding: .85rem 1rem; border-bottom: 1px solid var(--border); }
#cmdk-in svg { width: 18px; height: 18px; flex: none; color: var(--muted); }
#cmdk-in input { flex: 1; border: none; background: none; outline: none; color: var(--text); font-family: var(--serif); font-size: 1.05rem; }
#cmdk-in .hint { font-family: var(--sans); font-size: .6rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted);
  border: 1px solid var(--border); border-radius: 2px; padding: .12rem .4rem; }
#cmdk-res { max-height: 52vh; overflow-y: auto; padding: .4rem; }
.cmdk-grp { font-family: var(--sans); font-size: .68rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--muted);
  border-bottom: 1px solid var(--border); padding: .6rem 0 .3rem; margin: 0 .6rem; }
.cmdk-row { display: flex; align-items: center; gap: .7rem; padding: .5rem .6rem; border-radius: 0; cursor: pointer;
  text-decoration: none; color: var(--text); }
.cmdk-row:hover { background: var(--panel); }
.cmdk-row.sel { background: var(--panel); box-shadow: inset 2px 0 0 var(--accent); }
.cmdk-ic { width: 1.3rem; text-align: center; color: var(--accent); flex: none; font-family: var(--sans); font-size: .9rem; }
.cmdk-t { font-family: var(--display); font-size: .98rem; flex: 0 1 auto; max-width: 60%; min-width: 9ch; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cmdk-t mark { background: var(--mark, #f4e7ad); color: inherit; border-radius: 2px; padding: 0 1px; }
.cmdk-m { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase; letter-spacing: .06em;
  font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap;
  display: flex; align-items: baseline; gap: .55rem; flex: 1; justify-content: flex-end; }
.cmdk-m::before { content: ""; flex: 1; min-width: 1.5rem; height: .7em;
  background-image: radial-gradient(circle, var(--border) 1px, transparent 1.2px);
  background-size: 6px 2px; background-repeat: repeat-x; background-position: 0 60%; }
.cmdk-none { font-family: var(--sans); font-size: .85rem; color: var(--muted); text-align: center; padding: 1.4rem .6rem; }
/* reading progress lives on the meta line (bottom-right of the card body), never over the cover image */
.meta.has-prog { display: flex; align-items: center; justify-content: space-between; gap: .6rem; }
.card-prog { display: inline-flex; align-items: center; gap: .34rem; font-family: var(--mono); font-size: .64rem;
  font-variant-numeric: tabular-nums; letter-spacing: .02em; text-transform: none; color: var(--muted); white-space: nowrap; }
.card-prog svg { width: 14px; height: 14px; flex: none; }
.card-prog .pbg { stroke: var(--border); }
.card-prog.done { color: var(--accent); }
#resume { display: none; }
#resume.on { display: block; max-width: 1120px; margin: 1.2rem auto 0; padding: 0 2rem; }
#resume a { display: flex; align-items: center; gap: 1rem; text-decoration: none; color: var(--text); background: transparent;
  border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 0; padding: 1rem 1.3rem; position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
#resume a:hover, #resume a:focus-visible { border-color: var(--text); border-left-color: var(--accent);
  transform: translateY(-2px); box-shadow: var(--shadow-2); z-index: 1; }
#resume .rcol { min-width: 0; }
#resume .rk { display: block; font-family: var(--sans); font-size: .68rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .25rem; }
#resume .rt { display: block; font-family: var(--display); font-size: 1.12rem; line-height: 1.2; }
#resume .rs { display: block; font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: .2rem 0 0; }
#resume .rcta { margin-left: auto; display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
#resume a:hover .rcta, #resume a:focus-visible .rcta { background: var(--text); border-color: var(--text); color: var(--bg); transform: translateX(3px); }
@media (max-width: 560px) { #cmdk-fab { font-size: .66rem; } .cmdk-m { display: none; } .cmdk-m::before { display: none; } }
/* shareable-card preview modal */
#share-back { position: fixed; inset: 0; z-index: 85; background: rgba(24,22,18,.5); display: none; align-items: center; justify-content: center; padding: 1rem; }
#share-back.open { display: flex; }
#share-box { background: var(--bg); border: 1px solid var(--border); border-radius: 2px; padding: 1rem; max-width: 380px; width: 100%; box-shadow: var(--shadow-3); }
#share-img { width: 100%; border-radius: 0; display: block; border: 1px solid var(--border); }
#share-actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .8rem; }
#share-actions button { font-family: var(--sans); font-size: .8rem; padding: .5rem .8rem; border: 1px solid var(--border); border-radius: 2px; background: transparent; color: var(--text); cursor: pointer; }
#share-actions button:hover { border-color: var(--text); color: var(--accent); }
#share-actions #share-dl { background: var(--text); color: var(--bg); border: 1px solid var(--text); border-radius: 2px;
  font-family: var(--sans); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; cursor: pointer;
  box-shadow: none; transition: background-color .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
#share-actions #share-dl:hover, #share-actions #share-dl:focus-visible { background: var(--accent); border-color: var(--accent); color: var(--bg);
  transform: translateY(-1px); box-shadow: var(--shadow-2); }
#share-actions #share-dl:active { transform: translateY(0); box-shadow: none; }
.share-toast { position: fixed; bottom: 1.3rem; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); font-family: var(--sans); font-size: .8rem; padding: .5rem .9rem; border-radius: 2px; z-index: 95; }
/* Punch Pass reduced-motion kill block (A.4) — enumerates every selector this constant transforms */
@media (prefers-reduced-motion: reduce) {
  #cmdk-fab, #cmdk-fab:hover, #cmdk-fab:focus-visible,
  #resume a, #resume a:hover, #resume a:focus-visible,
  #resume .rcta, #resume a:hover .rcta, #resume a:focus-visible .rcta,
  #share-actions #share-dl, #share-actions #share-dl:hover, #share-actions #share-dl:focus-visible, #share-actions #share-dl:active {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
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
        + '<span class="cmdk-ic">' + e.icon + '</span><span class="cmdk-t">' + hl(e.t, q) + '</span><span class="cmdk-m">' + esc(e.m.length > 44 ? e.m.slice(0, 43) + '…' : e.m) + '</span></a>';
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
    var bg = v('--bg', '#fcfbf7'), text = v('--text', '#1e1b16'), accent = v('--accent', '#9a2c1a'),
        muted = v('--muted', '#6e6759'), border = v('--border', '#d8d3c4');
    var tiles = [v('--t1', '#9a2c1a'), v('--t2', '#a3771c'), v('--t3', '#274d68'), v('--t4', '#4a6350'), v('--t5', '#64405a')];
    var SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif";
    var SERIF = "'Iowan Old Style', Palatino, Georgia, serif";
    x.fillStyle = bg; x.fillRect(0, 0, W, H);
    x.strokeStyle = border; x.lineWidth = 2 * S; x.strokeRect(20 * S, 20 * S, W - 40 * S, H - 40 * S);
    x.textBaseline = 'top';
    x.fillStyle = accent; x.font = (14 * S) + 'px ' + SANS;
    x.fillText((o.kicker || 'calvincollins · xyz').toUpperCase(), pad, pad);
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
    x.fillText('calvincollins.xyz', pad, H - pad - 16 * S);
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
# pannable world map where every corpus is one UNIFORM STAMP (cover + caption,
# identical size for all), grouped into named callout plates whose leader
# lines point at the true anchor dots — vintage-atlas-inset style. The world
# backdrop was baked once by scripts/build_atlas_geo.py into atlas/geo.json;
# build-time we project the lon/lat pins, lay out the plates, and write a
# small atlas.json that the client renders + makes interactive on first open.
# ============================================================================

ATLAS_TILES = [TERRA, GOLD, BLUE, OLIVE, PLUM]

# Every corpus is one uniform STAMP (same size for all — no research is ever
# bigger than another), pinned by lon/lat where its AUTHOR or STORY belongs and
# grouped into a named callout PLATE (a vintage-atlas inset) whose leader line
# points at the true anchor dots on the map. Adding a new corpus = one line
# here (lon/lat + label + group). Overridable wholesale via build.config.json
# "atlas": {"places": {...}, "groups": {...}}.
ATLAS_PLACEMENTS = {
    # — The Pacific Coast —
    "ipv4-ipv6-ctv-research":              {"lon": -122.14, "lat": 37.44, "group": "pacific", "label": "Silicon Valley, California"},
    "ctv-identity-signals-research":       {"lon": -122.40, "lat": 37.79, "group": "pacific", "label": "San Francisco — the identity stack"},
    "agentic-advertising-protocols-research": {"lon": -122.42, "lat": 37.77, "group": "pacific", "label": "San Francisco — where the protocols are drafted"},
    "social-ctv-retargeting-research":     {"lon": -122.04, "lat": 37.37, "group": "pacific", "label": "Sunnyvale, California — LinkedIn's handoff"},
    "fox-roku-research":                   {"lon": -118.24, "lat": 34.05, "group": "pacific", "label": "Century City, Los Angeles"},
    # — the American interior —
    "uap-research":                        {"lon": -104.52, "lat": 33.39, "group": "newmexico", "label": "Roswell, New Mexico"},
    "walmart-vibe-research":               {"lon": -94.21,  "lat": 36.37, "group": "bentonville", "label": "Bentonville, Arkansas"},
    # — Washington —
    "us-geopolitics-research":             {"lon": -77.04,  "lat": 38.90, "group": "washington", "label": "Washington, D.C."},
    "political-ctv-research":              {"lon": -77.01,  "lat": 38.89, "group": "washington", "label": "Washington, D.C. — the measured screen"},
    # — New York —
    "civil-war-religion-whitman-research": {"lon": -73.99,  "lat": 40.69, "group": "newyork", "label": "Brooklyn & New York"},
    "us-economy-financial-system-research": {"lon": -74.01, "lat": 40.71, "group": "newyork", "label": "Wall Street & the Treasury"},
    "containerized-bidding-research":      {"lon": -74.00,  "lat": 40.72, "group": "newyork", "label": "IAB Tech Lab & the programmatic exchanges, New York"},
    "ctv-dsp-ssp-research":                {"lon": -73.99,  "lat": 40.73, "group": "newyork", "label": "The exchanges, New York"},
    "ctv-brand-safety-research":           {"lon": -73.97,  "lat": 40.76, "group": "newyork", "label": "Madison Avenue, New York"},
    # — New England —
    "dickinson-research":                  {"lon": -72.52,  "lat": 42.37, "group": "newengland", "label": "Amherst, Massachusetts"},
    "emerson-research":                    {"lon": -71.35,  "lat": 42.46, "group": "newengland", "label": "Concord, Massachusetts"},
    "american-pragmatism-research":        {"lon": -71.11,  "lat": 42.37, "group": "newengland", "label": "Cambridge, Massachusetts"},
    "great-awakenings-research":           {"lon": -72.63,  "lat": 42.32, "group": "newengland", "label": "Northampton, Massachusetts — Edwards's revival"},
    # — Canada —
    "mcluhan-research":                    {"lon": -79.38,  "lat": 43.65, "group": "toronto", "label": "Toronto, Ontario"},
    # — Britain & Ireland —
    "carlyle-research":                    {"lon": -3.19,   "lat": 55.95, "group": "britain", "label": "Ecclefechan & Edinburgh, Scotland"},
    "carlyle-french-revolution-research":  {"lon": -0.17,   "lat": 51.48, "group": "britain", "label": "Cheyne Row, Chelsea — where the manuscript burned"},
    "deutsch-good-explanations-research":  {"lon": -1.26,   "lat": 51.75, "group": "britain", "label": "Oxford, England"},
    "miq-research":                        {"lon": -0.09,   "lat": 51.52, "group": "britain", "label": "London — MiQ's home office"},
    "ancestry-research":                   {"lon": -8.47,   "lat": 51.90, "group": "munster", "label": "Munster, Ireland"},
    # — the Continent —
    "jung-research":                       {"lon": 8.54,    "lat": 47.37, "group": "alps", "label": "Zürich, Switzerland"},
    "piaget-research":                     {"lon": 6.14,    "lat": 46.20, "group": "alps", "label": "Geneva & Neuchâtel, Switzerland"},
    "pareto-research":                     {"lon": 6.63,    "lat": 46.52, "group": "alps", "label": "Lausanne & Céligny, Switzerland"},
    "phenomenology-pragmatism-research":   {"lon": 7.85,    "lat": 47.99, "group": "alps", "label": "Freiburg, Germany"},
    "democratic-socialism-research":       {"lon": 13.40,   "lat": 52.52, "group": "berlin", "label": "Berlin & the Second International"},
    "carlyle-friedrich-research":          {"lon": 13.06,   "lat": 52.39, "group": "berlin", "label": "Potsdam — Friedrich's Prussia"},
    "roman-catholicism-research":          {"lon": 12.45,   "lat": 41.90, "group": "rome", "label": "Rome & the Vatican"},
    # — the Levant —
    "jesus-research":                      {"lon": 35.57,   "lat": 32.88, "group": "levant", "label": "Galilee"},
    "john-the-baptist-research":           {"lon": 35.55,   "lat": 31.84, "group": "levant", "label": "The Jordan & the Judean wilderness"},
    # — the wider world —
    "india-advertising-research":          {"lon": 72.88,   "lat": 19.08, "group": "mumbai", "label": "Mumbai — a billion screens"},
    "latam-ctv-research":                  {"lon": -46.63,  "lat": -23.55, "group": "saopaulo", "label": "São Paulo — reach ahead of revenue"},
}
# The callout plates: display name + where the plate itself sits (lon/lat of its
# CENTER — parked over open ocean or empty land near its anchors) + optional
# column count for the stamp grid inside.
ATLAS_GROUPS = {
    "pacific":     {"name": "The Pacific Coast", "plate": [-117.0, 13.0], "cols": 3},
    "newmexico":   {"name": "New Mexico",        "plate": [-104.5, 38.5]},
    "bentonville": {"name": "Bentonville",       "plate": [-91.0, 28.5]},
    "washington":  {"name": "Washington",        "plate": [-73.0, 26.0]},
    "newyork":     {"name": "New York",          "plate": [-51.0, 35.0], "cols": 3},
    "newengland":  {"name": "New England",       "plate": [-78.0, 55.0], "cols": 2},
    "toronto":     {"name": "Toronto",           "plate": [-96.0, 52.0]},
    "britain":     {"name": "Britain",           "plate": [-18.0, 58.5], "cols": 2},
    "munster":     {"name": "Munster",           "plate": [-16.0, 42.5]},
    "alps":        {"name": "The Alps",          "plate": [-1.0, 36.0], "cols": 2},
    "berlin":      {"name": "Berlin & Potsdam",  "plate": [22.0, 58.5], "cols": 2},
    "rome":        {"name": "Rome",              "plate": [16.0, 36.5]},
    "levant":      {"name": "The Levant",        "plate": [26.0, 22.0], "cols": 2},
    "mumbai":      {"name": "Mumbai",            "plate": [66.0, 11.0]},
    "saopaulo":    {"name": "São Paulo",         "plate": [-37.0, -29.0]},
}


def load_atlas_geo():
    """Read the baked geometry (atlas/geo.json). Returns None if not authored."""
    p = HERE / "atlas" / "geo.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# Stamp geometry, in map units (the geo.json viewBox is 3600×1938). Every
# corpus gets exactly this footprint — the whole point of the redesign.
ATLAS_STAMP_W = 84      # cover width
ATLAS_STAMP_H = 112     # cover height (3:4 portrait, like the library cards)
ATLAS_CAP_H = 32        # caption strip under the cover (≤2 title lines)
ATLAS_GAP = 10          # gutter between stamps in a plate
ATLAS_PAD = 14          # plate inner padding
ATLAS_HEAD = 38         # plate header band (place name)


def _atlas_caption(title, max_chars=14, max_lines=2):
    """Wrap a corpus title into ≤2 short caption lines for under its stamp."""
    words, lines, cur = (title or "").split(), [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # ellipsize if we ran out of room
    used = " ".join(lines)
    if len(used) < len(" ".join(words)):
        last = lines[-1]
        lines[-1] = (last[:max_chars - 1].rstrip(" ,;:&/-") + "…") if len(last) >= max_chars - 1 else last + "…"
    return [ln[:max_chars + 2] for ln in lines]


def _plate_cols(k, hint=None):
    """Column count for a plate's stamp grid: explicit hint, else compact."""
    if hint:
        return max(1, int(hint))
    return {1: 1, 2: 2, 3: 3, 4: 2, 5: 3, 6: 3}.get(k, 3)


def compute_atlas(geo, corpora, placements=None, groups=None):
    """Lay every placed corpus out as a uniform stamp inside its group's plate.

    `corpora` is {slug: {title, href, accent, img, chapters:[{t,href}]}}. Returns
    the dict written to atlas.json: viewBox + backdrop + plates (callout boxes
    with name/leader) + places (one equal-size stamp per corpus, each carrying
    its true anchor point), or None if nothing places. Warns on unpinned
    corpora and on plate-vs-plate / plate-vs-anchor collisions so new pins are
    honest about where they land.
    """
    placements = placements or ATLAS_PLACEMENTS
    groups = groups or ATLAS_GROUPS
    proj = _atlas_projector(geo)

    for slug in corpora:
        if slug not in placements:
            print(f"  ! atlas: no pin for {slug} — add it to ATLAS_PLACEMENTS", file=sys.stderr)

    by_group = {}
    for slug, pl in placements.items():
        if slug not in corpora:
            continue
        gid = (pl or {}).get("group")
        if not gid or gid not in groups:
            print(f"  ! atlas: {slug} has unknown group {gid!r}", file=sys.stderr)
            continue
        by_group.setdefault(gid, []).append((slug, pl))

    plates, places = [], []
    cell_h = ATLAS_STAMP_H + ATLAS_CAP_H
    for gid, g in groups.items():
        members = by_group.get(gid)
        if not members:
            continue
        members.sort(key=lambda m: m[0])
        k = len(members)
        cols = _plate_cols(k, g.get("cols"))
        rows = math.ceil(k / cols)
        pw = ATLAS_PAD * 2 + cols * ATLAS_STAMP_W + (cols - 1) * ATLAS_GAP
        # a narrow plate still has to fit its place name (17px caps, ~11 units/char)
        pw = max(pw, min(240, 11 * len(g["name"]) + 26))
        ph = ATLAS_HEAD + rows * cell_h + (rows - 1) * ATLAS_GAP + ATLAS_PAD
        pcx, pcy = proj(g["plate"][0], g["plate"][1])
        px, py = pcx - pw / 2, pcy - ph / 2

        anchors = []
        for i, (slug, pl) in enumerate(members):
            r, cidx = divmod(i, cols)
            in_row = min(cols, k - r * cols)             # center a short last row
            row_w = in_row * ATLAS_STAMP_W + (in_row - 1) * ATLAS_GAP
            sx = px + (pw - row_w) / 2 + cidx * (ATLAS_STAMP_W + ATLAS_GAP)
            sy = py + ATLAS_HEAD + r * (cell_h + ATLAS_GAP)
            ax, ay = proj(pl["lon"], pl["lat"])
            anchors.append((ax, ay))
            c = corpora[slug]
            places.append({
                "slug": slug, "title": c["title"], "href": c["href"], "accent": c["accent"],
                "img": c.get("img"), "plate": gid,
                "x": round(sx, 1), "y": round(sy, 1), "w": ATLAS_STAMP_W, "h": ATLAS_STAMP_H,
                "ax": ax, "ay": ay,
                "cap": _atlas_caption(c["title"]),
                "label": (pl or {}).get("label") or g["name"],
                "chapters": c.get("chapters", []),
            })

        # Leader: from the plate's rim toward the anchors' centroid; the client
        # draws thin spokes from that centroid to each anchor dot.
        cx = sum(a[0] for a in anchors) / k
        cy = sum(a[1] for a in anchors) / k
        leader = None
        if not (px <= cx <= px + pw and py <= cy <= py + ph):
            dx, dy = cx - pcx, cy - pcy
            tx = (pw / 2) / abs(dx) if dx else float("inf")
            ty = (ph / 2) / abs(dy) if dy else float("inf")
            t = min(tx, ty)
            leader = [round(pcx + dx * t, 1), round(pcy + dy * t, 1), round(cx, 1), round(cy, 1)]
        plates.append({
            "id": gid, "name": g["name"],
            "x": round(px, 1), "y": round(py, 1), "w": pw, "h": ph,
            "cx": round(cx, 1), "cy": round(cy, 1), "leader": leader,
        })

    if not places:
        return None

    # Honesty checks: overlapping plates, plates covering someone's anchor, and
    # plates escaping the map sheet all print as build warnings for re-tuning.
    def _overlap(a, b, pad=6):
        return not (a["x"] + a["w"] + pad < b["x"] or b["x"] + b["w"] + pad < a["x"]
                    or a["y"] + a["h"] + pad < b["y"] or b["y"] + b["h"] + pad < a["y"])
    for i, a in enumerate(plates):
        for b in plates[i + 1:]:
            if _overlap(a, b):
                print(f"  ! atlas: plates {a['id']!r} and {b['id']!r} overlap — move one in ATLAS_GROUPS", file=sys.stderr)
        if a["x"] < 4 or a["y"] < 4 or a["x"] + a["w"] > geo["w"] - 4 or a["y"] + a["h"] > geo["h"] - 4:
            print(f"  ! atlas: plate {a['id']!r} runs off the map sheet", file=sys.stderr)
        for p in places:
            if p["plate"] != a["id"] and a["x"] - 8 <= p["ax"] <= a["x"] + a["w"] + 8 and a["y"] - 8 <= p["ay"] <= a["y"] + a["h"] + 8:
                print(f"  ! atlas: plate {a['id']!r} covers the anchor of {p['slug']}", file=sys.stderr)

    return {
        "viewBox": geo["viewBox"], "w": geo["w"], "h": geo["h"],
        "graticule": geo.get("graticule", []), "backdrop": geo.get("backdrop", []),
        "plates": plates, "places": places,
    }


def _atlas_projector(geo):
    """Rebuild the Web-Mercator projection geo.json was baked at, so arbitrary
    lon/lat points (the per-corpus intellectual-world nodes) land in the SAME
    coordinate space as the regions and backdrop. Mirrors scripts/build_atlas_geo.py;
    reads the window/width straight off geo.json so the two can never drift."""
    win = geo.get("window") or {"W": -130.0, "E": 160.0, "N": 74.0, "S": -40.0}
    w = geo.get("w") or 3600.0

    def _mx(lon):
        return math.radians(lon)

    def _my(lat):
        lat = max(min(lat, 84.0), -84.0)
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    x0, x1 = _mx(win["W"]), _mx(win["E"])
    y0 = _my(win["N"])
    sx = w / (x1 - x0) if x1 != x0 else 1.0

    def proj(lon, lat):
        return [round((_mx(lon) - x0) * sx, 1), round((y0 - _my(lat)) * sx, 1)]

    return proj


def load_atlas_connections():
    """Read the authored intellectual-world data (atlas/connections.json).

    Shape: {slug: {home:{name,place,lon,lat,note}, nodes:[{name,place,lon,lat,
    relation,kind}]}} — WGS84 lon/lat that the build projects. {} if not authored."""
    p = HERE / "atlas" / "connections.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"  ! atlas connections.json unreadable: {e}", file=sys.stderr)
        return {}


def compute_atlas_connections(geo, corpora, conn_raw):
    """Project each corpus's intellectual world (home + geographic nodes) into the
    atlas coordinate space, joining the corpus's own cover/accent/link onto its
    home anchor. Returns {slug: {home, nodes}} for slugs that have both a corpus
    entry and authored connections; skips the rest with a note."""
    if not conn_raw:
        return {}
    proj = _atlas_projector(geo)
    out = {}
    for slug, world in conn_raw.items():
        if slug not in corpora or not isinstance(world, dict):
            if slug not in corpora:
                print(f"  ! atlas connections: {slug!r} has no corpus entry", file=sys.stderr)
            continue
        home = world.get("home") or {}
        nodes_in = world.get("nodes") or []
        if "lon" not in home or "lat" not in home or not nodes_in:
            print(f"  ! atlas connections: {slug!r} missing home coords or nodes", file=sys.stderr)
            continue
        c = corpora[slug]
        hx, hy = proj(home["lon"], home["lat"])
        nodes = []
        for nd in nodes_in:
            if "lon" not in nd or "lat" not in nd:
                continue
            nx, ny = proj(nd["lon"], nd["lat"])
            nodes.append({
                "x": nx, "y": ny,
                "name": nd.get("name", ""), "place": nd.get("place", ""),
                "relation": nd.get("relation", ""), "kind": nd.get("kind", "influence"),
            })
        if not nodes:
            continue
        out[slug] = {
            "home": {
                "x": hx, "y": hy,
                "name": home.get("name", c["title"]), "place": home.get("place", ""),
                "note": home.get("note", ""),
                "title": c["title"], "href": c["href"], "accent": c["accent"], "img": c.get("img"),
            },
            "nodes": nodes,
        }
    return out


ATLAS_CSS = """
#atlas-dock { position: fixed; left: .9rem; bottom: .9rem; z-index: 60; display: flex; gap: .5rem; align-items: center; }
#atlas-dock #cmdk-fab { position: static; left: auto; bottom: auto; }
#atlas-fab { display: inline-flex; align-items: center; gap: .4rem; font-family: var(--sans); font-size: .66rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--muted); background: var(--bg);
  border: 1px solid var(--border); border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#atlas-fab:hover, #atlas-fab:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }
#atlas-fab svg { flex: none; }
#atlas-back { position: fixed; inset: 0; z-index: 90; background: rgba(24,22,18,.5); -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; padding: 3vh 2vw; }
#atlas-back.open { display: flex; }
#atlas-panel { position: relative; width: 100%; height: 94vh; max-width: 1500px; background: var(--bg);
  border: 1px solid var(--border); border-radius: 2px; overflow: hidden; box-shadow: var(--shadow-3);
  display: flex; flex-direction: column; }
#atlas-bar { display: flex; align-items: baseline; gap: .8rem; padding: .7rem 1rem; border-bottom: 1px solid var(--border); flex: none; }
#atlas-title { font-family: var(--display, var(--serif)); font-size: 1.05rem; color: var(--text); white-space: nowrap; }
#atlas-sub { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#atlas-tools { display: flex; gap: .35rem; flex: none; }
#atlas-tools button { font-family: var(--sans); font-size: .74rem; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-radius: 2px; padding: .25rem .55rem; cursor: pointer; min-width: 1.9rem; }
#atlas-tools button:hover { border-color: var(--text); color: var(--accent); }
#atlas-stage { position: relative; flex: 1; overflow: hidden; cursor: grab; background: var(--bg); touch-action: none; }
#atlas-stage.grab { cursor: grabbing; }
#atlas-world { position: absolute; top: 0; left: 0; transform-origin: 0 0; will-change: transform; }
#atlas-svg { display: block; overflow: visible; }
#atlas-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-family: var(--sans); font-size: .9rem; color: var(--muted); pointer-events: none; text-align: center; padding: 2rem; }
#atlas-grat path { fill: none; stroke: var(--border); stroke-width: .6; opacity: .45; }
#atlas-bd path { fill: var(--panel); stroke: var(--border); stroke-width: .7; opacity: .7; }
[data-theme="dark"] #atlas-bd path { fill: #221f1b; opacity: .85; }
/* the callout plates — every corpus one uniform stamp, grouped under a place name */
.atlas-plbg { fill: var(--bg); stroke: var(--border); stroke-width: 1.2;
  filter: drop-shadow(0 3px 9px rgba(24,22,18,.16)); }
[data-theme="dark"] .atlas-plbg { filter: drop-shadow(0 3px 9px rgba(0,0,0,.5)); }
.atlas-plname { font-family: var(--sans); font-size: calc(17px * var(--als, 1)); font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; fill: var(--muted); cursor: pointer;
  paint-order: stroke; stroke: var(--bg); stroke-width: 4px; stroke-linejoin: round; }
.atlas-plname:hover { fill: var(--accent); }
.atlas-plrule { stroke: var(--border); stroke-width: 1; }
.atlas-leader { fill: none; stroke: var(--muted); stroke-width: 1.6; opacity: .55; pointer-events: none; }
.atlas-spoke { stroke: var(--muted); stroke-width: 1.1; stroke-dasharray: 3 4; opacity: .5; pointer-events: none; }
.atlas-anchor { stroke: var(--bg); stroke-width: 2; pointer-events: none; }
.atlas-img { cursor: pointer; }
.atlas-fallback { cursor: pointer; opacity: .22; }
.atlas-initial { font-family: var(--display, var(--serif)); font-size: 52px; pointer-events: none; }
.atlas-ring { fill: none; opacity: .6; transition: opacity .15s ease, stroke-width .15s ease; pointer-events: none; }
.atlas-place.active .atlas-ring { opacity: 1; stroke-width: 5; }
.atlas-cap { font-family: var(--sans); font-size: 12.5px; fill: var(--text); opacity: .85; pointer-events: none; }
#atlas-card { position: absolute; left: 0; top: 0; z-index: 5; width: 250px; max-width: 76vw; background: var(--bg);
  border: 1px solid var(--border); border-radius: 0; padding: .7rem .8rem; box-shadow: var(--shadow-2); display: none; }
#atlas-card.show { display: block; }
#atlas-card .ac-title { display: block; font-family: var(--display, var(--serif)); font-size: 1.02rem; line-height: 1.2;
  text-decoration: none; margin-bottom: .15rem; }
#atlas-card .ac-title:hover { text-decoration: underline; }
#atlas-card .ac-place { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--muted); margin-bottom: .55rem; }
#atlas-card .ac-sel { width: 100%; font-family: var(--sans); font-size: .8rem; padding: .42rem .5rem;
  border: 1px solid var(--border); border-radius: 2px; background: var(--panel); color: var(--text); cursor: pointer; }
/* the research picker — turns the Atlas from a mosaic into a per-corpus explorer */
#atlas-pick { font-family: var(--sans); font-size: .76rem; color: var(--text); background: var(--panel);
  border: 1px solid var(--border); border-radius: 2px; padding: .34rem 1.6rem .34rem .55rem; cursor: pointer;
  flex: none; max-width: 240px; -webkit-appearance: none; appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='7' viewBox='0 0 10 7'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23999' stroke-width='1.5'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right .55rem center; }
#atlas-pick:hover, #atlas-pick:focus { border-color: var(--accent); color: var(--accent); outline: none; }
/* per-corpus intellectual world: home badge, radiating threads, and place nodes */
.atlas-thread { fill: none; stroke-width: 2.4; opacity: .4; transition: opacity .15s ease, stroke-width .15s ease; pointer-events: none; }
.atlas-thread.lit { opacity: .96; stroke-width: 4.2; }
.atlas-cnode { cursor: pointer; }
.atlas-cdot { stroke: var(--bg); stroke-width: 3; transition: opacity .15s ease; }
.atlas-cnode.active .atlas-cdot { r: 13; }
.atlas-chalo { fill: none; stroke-width: 2.2; opacity: 0; transition: opacity .15s ease; pointer-events: none; }
.atlas-cnode.active .atlas-chalo { opacity: .85; }
.atlas-clabel { font-family: var(--sans); font-size: 23px; fill: var(--text); paint-order: stroke; stroke: var(--bg);
  stroke-width: 5px; stroke-linejoin: round; pointer-events: none; opacity: .92; transition: opacity .15s ease; }
.atlas-cnode.active .atlas-clabel { opacity: 1; font-weight: 600; }
.atlas-home { cursor: pointer; }
.atlas-hlabel { font-family: var(--display, var(--serif)); font-size: 34px; font-weight: 600; paint-order: stroke;
  stroke: var(--bg); stroke-width: 7px; stroke-linejoin: round; }
#atlas-card .ac-kind { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--muted); margin-bottom: .25rem; }
#atlas-card .ac-rel { font-family: var(--serif, var(--display)); font-size: .85rem; line-height: 1.42; color: var(--text); margin-top: .15rem; }
#atlas-card .ac-cta { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; margin-top: .55rem; }
@media (max-width: 640px) { #atlas-sub { display: none; } #atlas-panel { height: 96vh; } #atlas-fab span { display: none; }
  #atlas-pick { max-width: 44vw; } }
@media (prefers-reduced-motion: reduce) {
  #atlas-fab, #atlas-fab:hover, #atlas-fab:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

ATLAS_JS = r"""
(function () {
  var base = window.SHELL_BASE || '';
  var DATA = null, loaded = false, loading = false;
  var back, stage, world, card, msg, pick;
  var mode = '';   // '' = the all-pins mosaic; otherwise a slug -> that corpus's intellectual world
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
      + '<span id="atlas-sub">Every research is one equal stamp, pinned to its place. Click a place name to zoom in — or pick a research above to trace its intellectual world.</span>'
      + '<select id="atlas-pick" aria-label="Choose a research to map its intellectual world"></select>'
      + '<span id="atlas-tools"><button id="atlas-zin" aria-label="Zoom in">+</button>'
      + '<button id="atlas-zout" aria-label="Zoom out">−</button>'
      + '<button id="atlas-fit" aria-label="Reset the view">Fit</button>'
      + '<button id="atlas-x" aria-label="Close the Atlas">Esc</button></span></div>'
      + '<div id="atlas-stage"><div id="atlas-world"></div>'
      + '<div id="atlas-card"></div><div id="atlas-msg">Unrolling the map…</div></div></div>';
    document.body.appendChild(back);
    stage = back.querySelector('#atlas-stage'); world = back.querySelector('#atlas-world');
    card = back.querySelector('#atlas-card'); msg = back.querySelector('#atlas-msg');
    pick = back.querySelector('#atlas-pick');
    pick.addEventListener('change', function () { setMode(pick.value); });
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    back.querySelector('#atlas-x').onclick = close;
    back.querySelector('#atlas-zin').onclick = function () { zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1.3); };
    back.querySelector('#atlas-zout').onclick = function () { zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1 / 1.3); };
    back.querySelector('#atlas-fit').onclick = fit;
    var ptrs = {};   // active pointers, so two fingers pinch-zoom on touch
    stage.addEventListener('pointerdown', function (e) {
      if (e.target.closest('#atlas-card')) return;
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
      try { stage.setPointerCapture(e.pointerId); } catch (x) {} stage.classList.add('grab');
    });
    stage.addEventListener('pointermove', function (e) {
      if (!(e.pointerId in ptrs)) { if (!dragging) return; }
      var ids = Object.keys(ptrs);
      if (ids.length === 2 && (e.pointerId in ptrs)) {
        var a = ptrs[ids[0]], b = ptrs[ids[1]];
        var d0 = Math.hypot(a.x - b.x, a.y - b.y);
        ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
        a = ptrs[ids[0]]; b = ptrs[ids[1]];
        var d1 = Math.hypot(a.x - b.x, a.y - b.y);
        var r = stage.getBoundingClientRect();
        if (d0 > 0 && d1 > 0) zoomAt((a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, d1 / d0);
        moved = true; return;
      }
      if (!dragging) return;
      if (e.pointerId in ptrs) ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      var dx = e.clientX - lastX, dy = e.clientY - lastY;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      view.x += dx; view.y += dy; lastX = e.clientX; lastY = e.clientY; apply();
    });
    function end(e) {
      delete ptrs[e.pointerId];
      var ids = Object.keys(ptrs);
      if (ids.length === 1) { lastX = ptrs[ids[0]].x; lastY = ptrs[ids[0]].y; }
      if (!ids.length) { dragging = false; stage.classList.remove('grab'); }
    }
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

  function apply() {
    world.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.s + ')';
    // Counter-scale the plate names so the navigation labels stay readable
    // (~12 screen px) however far out the sheet is zoomed.
    world.style.setProperty('--als', Math.min(2.6, Math.max(1, 12 / (17 * view.s))).toFixed(3));
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
  // Min zoom follows the fitted scale, so small (mobile) stages can still show
  // the whole sheet; you can never zoom out past "everything visible".
  function minS() { return view.fitS ? Math.min(0.22, view.fitS) : 0.22; }
  function zoomAt(px, py, f) {
    var ns = Math.max(minS(), Math.min(9, view.s * f)), k = ns / view.s;
    view.x = px - (px - view.x) * k; view.y = py - (py - view.y) * k; view.s = ns; apply();
  }
  function fit() {
    if (!DATA || !DATA._b) return;
    var b = DATA._b, sw = stage.clientWidth, sh = stage.clientHeight, pad = 46;
    if (sw < 60 || sh < 60) { requestAnimationFrame(fit); return; }   // stage not laid out yet
    var s = Math.min(9, Math.min((sw - pad * 2) / b.w, (sh - pad * 2) / b.h));
    if (!isFinite(s) || s <= 0) return;
    view.fitS = s;
    view.s = s; view.x = (sw - (2 * b.x + b.w) * s) / 2; view.y = (sh - (2 * b.y + b.h) * s) / 2; apply();
  }
  function zoomToRect(x, y, w, h) {
    var sw = stage.clientWidth, sh = stage.clientHeight;
    var s = Math.max(minS(), Math.min(4.5, Math.min(sw / w, sh / h)));
    view.s = s; view.x = (sw - (2 * x + w) * s) / 2; view.y = (sh - (2 * y + h) * s) / 2; apply();
  }

  function load() {
    if (loading) return; loading = true;
    fetch(base + 'atlas.json').then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (j) {
        DATA = j; buildPick();
        var sm = ''; try { sm = localStorage.getItem('atlas-pick') || ''; } catch (e) {}
        if (sm && j.connections && j.connections[sm]) mode = sm;
        paint(true); loaded = true; if (msg) msg.remove(); syncPick(); updateSub();
      })
      .catch(function () { if (msg) msg.textContent = 'The Atlas needs to load its map data — open it on the live site (calvincollins.xyz).'; });
  }

  // The research picker turns the Atlas from a single mosaic into a per-corpus
  // explorer: pick a work and the map redraws as that corpus's intellectual world.
  function buildPick() {
    if (!pick || !DATA) return;
    var conn = DATA.connections || {}, seen = {}, opts = '<option value="">✦ All researches — the whole map</option>';
    (DATA.places || []).filter(function (p) { if (seen[p.slug] || !conn[p.slug]) return false; seen[p.slug] = 1; return true; })
      .map(function (p) { return { slug: p.slug, title: p.title }; })
      .sort(function (x, y) { return x.title.localeCompare(y.title); })
      .forEach(function (it) { opts += '<option value="' + esc(it.slug) + '">' + esc(it.title) + '</option>'; });
    pick.innerHTML = opts;
  }
  function syncPick() { if (pick) pick.value = mode; }
  function setMode(m) {
    if (!DATA) return;
    m = m || ''; if (m && !(DATA.connections && DATA.connections[m])) m = '';
    if (m === mode) { syncPick(); return; }
    mode = m; try { localStorage.setItem('atlas-pick', mode); } catch (e) {}
    hideCard(); paint(true); syncPick(); updateSub();
  }
  function updateSub() {
    var sub = back && back.querySelector('#atlas-sub'); if (!sub) return;
    var C = (mode && DATA && DATA.connections && DATA.connections[mode]) ? DATA.connections[mode] : null;
    if (C) sub.textContent = C.home.name + '’s intellectual world — ' + C.nodes.length + ' geographic connections. Hover a place to trace its thread.';
    else sub.textContent = 'Every research is one equal stamp, pinned to its place. Click a place name to zoom in — or pick a research above to trace its intellectual world.';
  }

  // Repaint the whole surface for the active mode. Cheap enough to rebuild wholesale.
  function paint(doFit) {
    if (!DATA) return;
    var j = DATA, C = (mode && j.connections && j.connections[mode]) ? j.connections[mode] : null;
    var s = '<svg id="atlas-svg" viewBox="' + j.viewBox + '" width="' + j.w + '" height="' + j.h
      + '" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">';
    s += '<defs>';
    if (!C) j.places.forEach(function (p) {
      s += '<clipPath id="ac-' + p.slug + '"><rect x="' + p.x + '" y="' + p.y + '" width="' + p.w + '" height="' + p.h + '" rx="7"/></clipPath>'; });
    else s += '<clipPath id="ahome"><circle cx="' + C.home.x + '" cy="' + C.home.y + '" r="58"/></clipPath>';
    s += '</defs>';
    s += '<g id="atlas-grat">'; (j.graticule || []).forEach(function (d) { s += '<path d="' + d + '"/>'; }); s += '</g>';
    s += '<g id="atlas-bd">'; (j.backdrop || []).forEach(function (d) { s += '<path d="' + d + '"/>'; }); s += '</g>';
    s += C ? connLayer(C) : placesLayer(j);
    s += '</svg>';
    world.innerHTML = s;
    bySlug = {};
    computeBounds(C);
    if (C) wireConn(C); else wirePlaces(j);
    if (doFit === false) { apply(); return; }
    fit();
  }

  // The stamp mosaic: leader lines + anchor dots first, then the callout
  // plates, then every corpus as one equal-size stamp with a short caption.
  function placesLayer(j) {
    var plateById = {};
    (j.plates || []).forEach(function (pl) { plateById[pl.id] = pl; });
    var s = '<g id="atlas-lead">';
    (j.plates || []).forEach(function (pl) {
      if (pl.leader) s += '<path class="atlas-leader" d="M' + pl.leader[0] + ' ' + pl.leader[1] + 'L' + pl.leader[2] + ' ' + pl.leader[3] + '"/>';
    });
    j.places.forEach(function (p) {
      var pl = plateById[p.plate];
      if (pl && (Math.abs(p.ax - pl.cx) > 2 || Math.abs(p.ay - pl.cy) > 2))
        s += '<line class="atlas-spoke" x1="' + pl.cx + '" y1="' + pl.cy + '" x2="' + p.ax + '" y2="' + p.ay + '"/>';
      s += '<circle class="atlas-anchor" cx="' + p.ax + '" cy="' + p.ay + '" r="5" fill="' + esc(p.accent) + '"/>';
    });
    s += '</g><g id="atlas-plates">';
    (j.plates || []).forEach(function (pl) {
      s += '<g class="atlas-plate" data-plate="' + esc(pl.id) + '">'
        + '<rect class="atlas-plbg" x="' + pl.x + '" y="' + pl.y + '" width="' + pl.w + '" height="' + pl.h + '" rx="10"/>'
        + '<line class="atlas-plrule" x1="' + (pl.x + 12) + '" y1="' + (pl.y + 33) + '" x2="' + (pl.x + pl.w - 12) + '" y2="' + (pl.y + 33) + '"/>'
        + '</g>';
    });
    // names in their own layer ABOVE every plate box, so a counter-scaled
    // label never hides under a neighbouring plate
    s += '</g><g id="atlas-plnames">';
    (j.plates || []).forEach(function (pl) {
      s += '<text class="atlas-plname" data-plate="' + esc(pl.id) + '" x="' + (pl.x + pl.w / 2) + '" y="' + (pl.y + 25) + '" text-anchor="middle">' + esc(pl.name) + '</text>';
    });
    s += '</g><g id="atlas-stamps">';
    j.places.forEach(function (p) {
      s += '<g class="atlas-place" data-slug="' + esc(p.slug) + '">';
      if (p.img) { var href = esc(base + p.img);
        s += '<image class="atlas-img" href="' + href + '" xlink:href="' + href + '" x="' + p.x + '" y="' + p.y
          + '" width="' + p.w + '" height="' + p.h + '" preserveAspectRatio="xMidYMid slice" clip-path="url(#ac-' + p.slug + ')"/>';
      } else {
        s += '<rect class="atlas-fallback" x="' + p.x + '" y="' + p.y + '" width="' + p.w + '" height="' + p.h + '" rx="7" fill="' + esc(p.accent) + '"/>'
          + '<text class="atlas-initial" x="' + (p.x + p.w / 2) + '" y="' + (p.y + p.h / 2 + 18) + '" text-anchor="middle" fill="' + esc(p.accent) + '">' + esc((p.title || '?').charAt(0)) + '</text>';
      }
      s += '<rect class="atlas-ring" x="' + p.x + '" y="' + p.y + '" width="' + p.w + '" height="' + p.h + '" rx="7" stroke="' + esc(p.accent) + '" stroke-width="2.5"/>';
      (p.cap || []).forEach(function (ln, li) {
        s += '<text class="atlas-cap" x="' + (p.x + p.w / 2) + '" y="' + (p.y + p.h + 14 + li * 14) + '" text-anchor="middle">' + esc(ln) + '</text>';
      });
      s += '</g>';
    });
    s += '</g>';
    return s;
  }

  function arcPath(x1, y1, x2, y2) {
    var mx = (x1 + x2) / 2, my = (y1 + y2) / 2, dx = x2 - x1, dy = y2 - y1, len = Math.sqrt(dx * dx + dy * dy) || 1;
    var off = Math.min(170, len * 0.16), cx = mx - dy / len * off, cy = my + dx / len * off;
    return 'M' + x1.toFixed(1) + ' ' + y1.toFixed(1) + ' Q' + cx.toFixed(1) + ' ' + cy.toFixed(1) + ' ' + x2.toFixed(1) + ' ' + y2.toFixed(1);
  }
  function clipText(t, n) { t = t || ''; return t.length > n ? t.slice(0, n - 1).replace(/[\s,;:&/-]+$/, '') + '…' : t; }
  function connLayer(C) {
    var ac = esc(C.home.accent || '#b3502f'), H = C.home, R = 58;
    var s = '<g id="atlas-threads" stroke="' + ac + '">';
    C.nodes.forEach(function (n, i) { s += '<path class="atlas-thread" data-i="' + i + '" d="' + arcPath(H.x, H.y, n.x, n.y) + '"/>'; });
    s += '</g>';
    // home badge drawn UNDER the place nodes, so their dots + labels are never masked
    s += '<g class="atlas-home">';
    s += '<circle cx="' + H.x + '" cy="' + H.y + '" r="' + (R + 6) + '" fill="var(--bg)"/>';
    if (H.img) { var href = esc(base + H.img);
      s += '<image href="' + href + '" xlink:href="' + href + '" x="' + (H.x - R) + '" y="' + (H.y - R) + '" width="' + (2 * R) + '" height="' + (2 * R) + '" preserveAspectRatio="xMidYMid slice" clip-path="url(#ahome)"/>'; }
    else s += '<circle cx="' + H.x + '" cy="' + H.y + '" r="' + R + '" fill="' + ac + '"/>';
    s += '<circle cx="' + H.x + '" cy="' + H.y + '" r="' + R + '" fill="none" stroke="' + ac + '" stroke-width="4.5"/>';
    s += '<text class="atlas-hlabel" x="' + H.x + '" y="' + (H.y + R + 33) + '" text-anchor="middle" fill="' + ac + '">' + esc(H.name) + '</text>';
    s += '</g>';
    s += '<g id="atlas-cnodes">';
    C.nodes.forEach(function (n, i) {
      // splay each label outward from home so the European cluster doesn't pile up
      var dx = n.x - H.x, dy = n.y - H.y, L = Math.sqrt(dx * dx + dy * dy) || 1;
      var lx = n.x + dx / L * 15, ly = n.y + dy / L * 15 + (dy >= 0 ? 16 : -8);
      var anchor = dx > 30 ? 'start' : (dx < -30 ? 'end' : 'middle');
      s += '<g class="atlas-cnode" data-i="' + i + '">';
      s += '<circle class="atlas-chalo" cx="' + n.x + '" cy="' + n.y + '" r="17" stroke="' + ac + '" fill="none"/>';
      s += '<circle class="atlas-cdot" cx="' + n.x + '" cy="' + n.y + '" r="9" fill="' + ac + '"/>';
      s += '<text class="atlas-clabel" x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1) + '" text-anchor="' + anchor + '">' + esc(clipText(n.name, 30)) + '</text>';
      s += '</g>';
    });
    s += '</g>';
    return s;
  }

  function computeBounds(C) {
    var X0 = 1e9, Y0 = 1e9, X1 = -1e9, Y1 = -1e9;
    if (C) {
      var pts = C.nodes.map(function (n) { return [n.x, n.y]; }); pts.push([C.home.x, C.home.y]);
      pts.forEach(function (q) { X0 = Math.min(X0, q[0]); Y0 = Math.min(Y0, q[1]); X1 = Math.max(X1, q[0]); Y1 = Math.max(Y1, q[1]); });
      var px = 120, py = 150;
      DATA._b = { x: X0 - px, y: Y0 - py, w: (X1 - X0) + 2 * px, h: (Y1 - Y0) + 2 * py };
    } else {
      (DATA.plates || []).forEach(function (pl) {
        X0 = Math.min(X0, pl.x); Y0 = Math.min(Y0, pl.y); X1 = Math.max(X1, pl.x + pl.w); Y1 = Math.max(Y1, pl.y + pl.h); });
      DATA.places.forEach(function (p) { bySlug[p.slug] = p;
        X0 = Math.min(X0, p.ax); Y0 = Math.min(Y0, p.ay); X1 = Math.max(X1, p.ax); Y1 = Math.max(Y1, p.ay); });
      DATA._b = { x: X0 - 40, y: Y0 - 40, w: (X1 - X0) + 80, h: (Y1 - Y0) + 80 };
    }
  }

  function wirePlaces(j) {
    [].forEach.call(world.querySelectorAll('.atlas-place'), function (el) {
      var p = bySlug[el.getAttribute('data-slug')];
      el.addEventListener('pointerenter', function () { if (!dragging) activate(p, el); });
      el.addEventListener('pointermove', function () { if (!dragging && active !== p) activate(p, el); });
      el.addEventListener('pointerleave', schedHide);
      el.addEventListener('click', function (e) { if (moved || e.target.closest('#atlas-card')) return; window.location.href = base + p.href; });
    });
    var byId = {};
    (j.plates || []).forEach(function (pl) { byId[pl.id] = pl; });
    [].forEach.call(world.querySelectorAll('.atlas-plate, .atlas-plname'), function (el) {
      var pl = byId[el.getAttribute('data-plate')];
      el.addEventListener('click', function () { if (moved || !pl) return; zoomToRect(pl.x - 30, pl.y - 30, pl.w + 60, pl.h + 60); });
    });
  }
  function wireConn(C) {
    [].forEach.call(world.querySelectorAll('.atlas-cnode'), function (el) {
      var i = +el.getAttribute('data-i'), n = C.nodes[i];
      el.addEventListener('pointerenter', function () { if (!dragging) showNode(C, i, el); });
      el.addEventListener('pointermove', function () { if (!dragging && active !== n) showNode(C, i, el); });
      el.addEventListener('pointerleave', function () { lit(i, false); schedHide(); });
      el.addEventListener('click', function (e) { if (moved) return; window.location.href = base + C.home.href; });
    });
    var home = world.querySelector('.atlas-home');
    if (home) {
      home.addEventListener('pointerenter', function () { if (!dragging) showHome(C, home); });
      home.addEventListener('pointermove', function () { if (!dragging && active !== C.home) showHome(C, home); });
      home.addEventListener('pointerleave', schedHide);
      home.addEventListener('click', function (e) { if (moved) return; window.location.href = base + C.home.href; });
    }
  }
  function lit(i, on) {
    var t = world.querySelector('.atlas-thread[data-i="' + i + '"]'); if (t) t.classList.toggle('lit', on);
    var nn = world.querySelector('.atlas-cnode[data-i="' + i + '"]'); if (nn) nn.classList.toggle('active', on);
  }

  function activate(p, el) {
    clearActive();
    active = p; p._el = el; el.classList.add('active');
    var opts = '<option value="" disabled selected>Jump to a chapter…</option>';
    (p.chapters || []).forEach(function (ch) { opts += '<option value="' + esc(base + ch.href) + '">' + esc(ch.t) + '</option>'; });
    card.innerHTML = '<a class="ac-title" href="' + esc(base + p.href) + '" style="color:' + esc(p.accent) + '">' + esc(p.title) + '</a>'
      + '<div class="ac-place">' + esc(p.label) + '</div>'
      + (p.chapters && p.chapters.length ? '<select class="ac-sel" aria-label="Jump to a chapter">' + opts + '</select>' : '');
    var sel = card.querySelector('.ac-sel');
    if (sel) sel.onchange = function () { if (this.value) window.location.href = this.value; };
    showCardAt(el);
  }
  function showNode(C, i, el) {
    clearActive(); var n = C.nodes[i]; active = n; lit(i, true);
    card.innerHTML = '<div class="ac-kind">' + esc(n.kind) + '</div>'
      + '<span class="ac-title" style="color:' + esc(C.home.accent) + '">' + esc(n.name) + '</span>'
      + '<div class="ac-place">' + esc(n.place) + '</div>'
      + (n.relation ? '<div class="ac-rel">' + esc(n.relation) + '</div>' : '');
    showCardAt(el);
  }
  function showHome(C, el) {
    clearActive(); var H = C.home; active = H;
    card.innerHTML = '<a class="ac-title" href="' + esc(base + H.href) + '" style="color:' + esc(H.accent) + '">' + esc(H.title) + '</a>'
      + '<div class="ac-place">' + esc(H.place) + '</div>'
      + (H.note ? '<div class="ac-rel">' + esc(H.note) + '</div>' : '')
      + '<div class="ac-cta" style="color:' + esc(H.accent) + '">Open corpus →</div>';
    showCardAt(el);
  }
  function showCardAt(el) {
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
  function clearActive() {
    if (!world) return;
    [].forEach.call(world.querySelectorAll('.atlas-place.active, .atlas-cnode.active'), function (e) { e.classList.remove('active'); });
    [].forEach.call(world.querySelectorAll('.atlas-thread.lit'), function (e) { e.classList.remove('lit'); });
  }
  function hideCard() { if (card) card.classList.remove('show'); clearActive(); active = null; }
})();
"""


# Floating light/dark toggle for pages that don't ship their own #theme-btn
# (library index, section fronts, forecast board, domain pages, wrapped…).
# The synchronous <head> boot script has already set data-theme before first
# paint — dark unless localStorage 'corpus-theme' says 'light' — so this only
# flips the attribute and persists the choice. Same affordance and placement
# as the edition pages' fixed theme button.
THEME_TOGGLE_CSS = """
#shell-theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer; }
#shell-theme-btn:hover { color: var(--accent); border-color: var(--text); }
"""

THEME_TOGGLE_JS = r"""
(function () {
  var toggle = function () {
    var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('corpus-theme', next); } catch (e) {}
  };
  var own = document.getElementById('theme-btn');
  if (own) {
    // page ships its own button; wire it only if its script didn't
    if (!own.onclick) own.onclick = toggle;
    return;
  }
  var b = document.createElement('button');
  b.id = 'shell-theme-btn'; b.title = 'Light / dark'; b.textContent = '◐ Theme';
  b.onclick = toggle;
  document.body.appendChild(b);
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
        f"<style>{THEME_TOGGLE_CSS}</style>"
        f"<script>{THEME_TOGGLE_JS}</script>"
        f"<style>{ATLAS_CSS}</style>"
        f"<script>{ATLAS_JS}</script>"
    )


LIBRARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{site_title}</title>
<meta name="description" content="{site_subtitle}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
{overture_head}
</head>
<body>
{overture}
{top_header}
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage">{brand_logo}<span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header>
  <div class="hero-text">
    <p class="kicker">A library of deep research</p>
    <h1>{site_title}</h1>
    <p class="tagline">{site_subtitle}</p>
    <p class="stats">{stats}</p>
    {hero_cta}
  </div>
  <div class="hero-art">{hero}</div>
</header>
{ticker}
{mirror}
{collections}
{quiz}
{bottom_scrolls}
<footer>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <p class="epigraph">“The medium is the message.” — Marshall McLuhan</p>
  <p class="colophon"><span class="mh-stamp">Machine generated</span></p>
  <p class="colophon marc">
  <span class="marc-l"><b>008</b> Generated by machine — Claude (Anthropic)</span>
  <span class="marc-l"><b>245</b> Machine Humanities : agentic scholarship</span>
  <span class="marc-l"><b>260</b> calvincollins.xyz : MMXXVI</span>
  <span class="marc-l"><b>500</b> Set in Charter &amp; Baskerville · no fonts were downloaded · reads anywhere, light or dark</span>
  </p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

# Theme is applied pre-paint by a synchronous <head> boot script in every
# template (dark by default; light only when localStorage 'corpus-theme' says
# so), so there is no init left to do here. Kept as an empty placeholder so
# the {theme_js} slots and concatenations keep working. Pages without their
# own #theme-btn get a floating toggle from shell_html().
LIBRARY_THEME_JS = ""

SCENE_LABELS = {
    "research": "A research-library table with open books, marked passages, and lamplight on the reading desk.",
    "collection": "A curated reading table with stacked chapters, notes, and a stitched path through the library.",
    "ghost": "A late-night editorial room with a lamp, marked pages, and a window beyond the desk.",
    "fingerprint": "A market-wire signal room with screens, broadcast arcs, and ticker bars.",
    "pamphlet": "A letterpress table with fresh pamphlet sheets, rollers, and a locked-up chase.",
    "briefing": "A briefing desk with screens, notes, and trade signals arranged for review.",
    "forecast": "A forecast desk with signal screens, probability sheets, and a small broadcast tower.",
    "map": "A map room with linked panes, table notes, and signal lines between ideas.",
    "quiz": "A study desk with question cards, marked pages, and a lit review lamp.",
    "wrapped": "A reading ledger table with stacked holdings, square marks, and a private year-end tally.",
}


SCENE_COVERS = {
    "research": ("american-pragmatism-research", "mcluhan-research", "carlyle-research", "jesus-research"),
    "collection": ("civil-war-religion-whitman-research", "dickinson-research", "emerson-research", "carlyle-research"),
    "ghost": ("carlyle-research", "dickinson-research", "mcluhan-research", "emerson-research"),
    "fingerprint": ("fox-roku-research", "agentic-advertising-protocols-research", "ctv-identity-signals-research", "india-advertising-research"),
    "pamphlet": ("carlyle-french-revolution-research", "carlyle-friedrich-research", "emerson-research", "dickinson-research"),
    "briefing": ("ctv-dsp-ssp-research", "containerized-bidding-research", "political-ctv-research", "walmart-vibe-research"),
    "forecast": ("walmart-vibe-research", "us-geopolitics-research", "fox-roku-research", "ctv-ssp-research"),
    "map": ("american-pragmatism-research", "jesus-research", "mcluhan-research", "india-advertising-research"),
    "quiz": ("piaget-research", "pareto-research", "jung-research", "deutsch-good-explanations-research"),
    "wrapped": ("carlyle-research", "american-pragmatism-research", "mcluhan-research", "jesus-research"),
}

SCENE_STAMPS = {
    "research": "FIELD NOTES",
    "collection": "CURATED PATH",
    "ghost": "NIGHT EDITION",
    "fingerprint": "MARKET WIRE",
    "pamphlet": "LETTERPRESS",
    "briefing": "BRIEFING ROOM",
    "forecast": "PROBABILITY DESK",
    "map": "IDEA ATLAS",
    "quiz": "QUESTION SET",
    "wrapped": "PRIVATE LEDGER",
}


_SCENE_COVER_SLUGS_CACHE = None
_RESEARCH_SCENES_CACHE = None


def _all_scene_cover_slugs():
    global _SCENE_COVER_SLUGS_CACHE
    if _SCENE_COVER_SLUGS_CACHE is None:
        if not COVERS_DIR.is_dir():
            _SCENE_COVER_SLUGS_CACHE = ()
        else:
            _SCENE_COVER_SLUGS_CACHE = tuple(dict.fromkeys(
                p.stem for p in sorted(COVERS_DIR.iterdir())
                if p.suffix.lower() in COVER_EXTS
            ))
    return _SCENE_COVER_SLUGS_CACHE


def _research_scenes():
    """Return one representative, content-specific scene for each corpus.

    Interior research scenes are stored as self-contained figure snippets with
    base64 images. The build publishes one representative image per corpus as a
    normal raster asset so scene plates can use the real research artwork
    without duplicating large data URIs into every page.
    """
    global _RESEARCH_SCENES_CACHE
    if _RESEARCH_SCENES_CACHE is not None:
        return _RESEARCH_SCENES_CACHE

    scenes = {}
    if FIGURES_DIR.is_dir():
        for folder in sorted(p for p in FIGURES_DIR.iterdir() if p.is_dir()):
            snippets = sorted(folder.glob("scene_*.html"))
            # Prefer a foundational/early scene over a future-scenario plate.
            snippets.sort(key=lambda p: ("future" in p.stem.lower(), p.name))
            for snippet in snippets:
                text = snippet.read_text(errors="ignore")
                img = re.search(
                    r'<img\b[^>]*\bsrc=["\']data:image/(jpeg|jpg|png|webp);base64,([^"\']+)["\'][^>]*>',
                    text,
                    re.IGNORECASE,
                )
                if not img:
                    continue
                tag = img.group(0)
                alt_match = re.search(r'\balt=["\']([^"\']*)["\']', tag, re.IGNORECASE)
                mime_ext = "jpg" if img.group(1).lower() in ("jpeg", "jpg") else img.group(1).lower()
                scenes[folder.name] = {
                    "bytes": base64.b64decode(img.group(2)),
                    "ext": mime_ext,
                    "alt": html.unescape(alt_match.group(1)) if alt_match else "",
                    "source": snippet.name,
                }
                break
    _RESEARCH_SCENES_CACHE = scenes
    return scenes


def publish_research_scenes(out):
    """Materialize representative interior scenes into the served build."""
    scene_dir = Path(out) / SCENE_ASSET_DIRNAME
    scene_dir.mkdir(parents=True, exist_ok=True)
    for slug, scene in _research_scenes().items():
        (scene_dir / f"{slug}.{scene['ext']}").write_bytes(scene["bytes"])


def _scene_subject_slug(kind, cover_slugs=None, seed=""):
    """Pick a content-specific corpus deterministically for a scene plate."""
    requested = [slug for slug in (cover_slugs or ()) if slug]
    if requested:
        return requested[0]

    available = list(_research_scenes())
    # Seeds commonly contain the corpus slug (reader cards, forecasts, bands).
    matches = [slug for slug in available if slug in (seed or "")]
    if matches:
        return max(matches, key=len)

    preferred = [slug for slug in SCENE_COVERS.get(kind, ()) if slug in available]
    pool = preferred or available
    if not pool:
        return None
    return min(pool, key=lambda slug: _scene_rank(f"{kind}:{seed}", slug))


def _scene_image(kind, root="", cover_slugs=None, seed=""):
    """Return (src, alt) for a real research scene, falling back to its cover."""
    slug = _scene_subject_slug(kind, cover_slugs=cover_slugs, seed=seed)
    if not slug:
        return None, ""

    scene = _research_scenes().get(slug)
    if scene:
        return (
            f"{root}{SCENE_ASSET_DIRNAME}/{slug}.{scene['ext']}",
            scene["alt"] or f"A narrative scene from the {humanize(slug)} research.",
        )

    cover = find_cover_image(slug)
    if cover:
        return f"{root}covers/{cover.name}", f"Cover scene for the {humanize(slug)} research."
    return None, ""


def _scene_rank(seed_key, slug):
    return hashlib.md5(f"{seed_key}:{slug}".encode("utf-8")).hexdigest()


def _scene_variant(kind, seed):
    if not seed:
        return 0
    return int(hashlib.md5(f"{kind}:{seed}".encode("utf-8")).hexdigest()[:2], 16) % 6


def _scene_cover_images(kind, root="", cover_slugs=None, seed=""):
    slugs = []
    for slug in (cover_slugs or ()):
        if slug and slug not in slugs:
            slugs.append(slug)

    pool = list(dict.fromkeys(SCENE_COVERS.get(kind, ()) + _all_scene_cover_slugs()))
    if seed:
        seed_key = f"{kind}:{seed}"
        pool.sort(key=lambda slug: _scene_rank(seed_key, slug))
    for slug in pool:
        if slug and slug not in slugs:
            slugs.append(slug)
        if len(slugs) >= 4:
            break

    imgs = []
    asset_root = root or ""
    for slug in slugs:
        img = find_cover_image(slug)
        if img is None:
            continue
        src = html.escape(f"{asset_root}covers/{img.name}", quote=True)
        imgs.append(
            f'<img class="sc-cover sc-c{len(imgs) + 1}" src="{src}" alt="" loading="lazy" decoding="async">'
        )
        if len(imgs) >= 4:
            break
    return "".join(imgs)


def scene_plate(kind, label=None, extra_class="", root="", cover_slugs=None, seed=""):
    """A full-bleed narrative scene drawn from the actual research artwork."""
    kind = kind if kind in SCENE_LABELS else "pamphlet"
    variant = _scene_variant(kind, seed)
    cls = f"scene-plate scene-{kind} scene-v{variant}" + (f" {extra_class}" if extra_class else "")
    src, scene_alt = _scene_image(kind, root=root, cover_slugs=cover_slugs, seed=seed)
    if not src:
        return ""
    alt = label or scene_alt or SCENE_LABELS[kind]
    return (
        f'<figure class="{html.escape(cls, quote=True)}">'
        f'<img class="sc-scene" src="{html.escape(src, quote=True)}" '
        f'alt="{html.escape(alt, quote=True)}" loading="lazy" decoding="async">'
        '</figure>'
    )


SCENE_PLATE_CSS = """
/* Scene plates: cover-wall/tabletop compositions that reuse the corpus covers
   and the archive-room language of the chapter scenes. */
.scene-plate { --sc-accent: var(--accent); --sc-ink: var(--text); --sc-paper: #f6f2e7;
  --sc-warm: #b9854b; --sc-blue: #274d5f; --sc-green: #566b40; --sc-red: #9a2c1a;
  --sc-shadow: rgba(31, 24, 17, .22); --sc-glass: rgba(255, 255, 255, .34);
  position: relative; display: block; width: 100%; aspect-ratio: 16 / 10; overflow: hidden;
  margin: 0; border: 1px solid color-mix(in srgb, var(--border) 72%, var(--text) 28%);
  border-radius: 3px; isolation: isolate; background:
    radial-gradient(circle at 22% 18%, color-mix(in srgb, var(--sc-accent) 32%, transparent), transparent 23%),
    radial-gradient(circle at 82% 26%, rgba(185, 133, 75, .22), transparent 25%),
    linear-gradient(135deg, color-mix(in srgb, var(--panel) 88%, #fff 12%), color-mix(in srgb, var(--bg) 78%, var(--sc-accent) 22%));
  box-shadow: 0 18px 44px color-mix(in srgb, var(--sc-shadow) 65%, transparent); }
[data-theme="dark"] .scene-plate { --sc-paper: #242018; --sc-shadow: rgba(0, 0, 0, .48);
  --sc-glass: rgba(255, 255, 255, .12); background:
    radial-gradient(circle at 22% 18%, color-mix(in srgb, var(--sc-accent) 36%, transparent), transparent 24%),
    radial-gradient(circle at 82% 26%, rgba(217, 128, 85, .18), transparent 25%),
    linear-gradient(135deg, color-mix(in srgb, var(--panel) 76%, #000 24%), color-mix(in srgb, var(--bg) 76%, var(--sc-accent) 24%)); }
.scene-plate::before { content: ""; position: absolute; inset: 0; z-index: 9; pointer-events: none; background:
  repeating-linear-gradient(0deg, rgba(80, 62, 36, .08) 0 1px, transparent 1px 5px),
  repeating-linear-gradient(90deg, rgba(80, 62, 36, .05) 0 1px, transparent 1px 7px),
  radial-gradient(circle at 50% 42%, transparent 0 54%, rgba(31, 24, 17, .18) 100%);
  mix-blend-mode: multiply; opacity: .72; }
[data-theme="dark"] .scene-plate::before { mix-blend-mode: screen; opacity: .2; }
.scene-plate::after { content: ""; position: absolute; inset: 10px; z-index: 8; pointer-events: none;
  border: 1px solid rgba(255,255,255,.28); box-shadow: inset 0 0 0 1px rgba(40, 31, 22, .18); }
.scene-plate > span, .scene-plate > img { position: absolute; display: block; }
.sc-wall { inset: 0; z-index: 0; background:
  linear-gradient(90deg, transparent 0 18%, rgba(255,255,255,.16) 18% 18.5%, transparent 18.5% 62%, rgba(255,255,255,.12) 62% 62.5%, transparent 62.5%),
  radial-gradient(circle at 48% 18%, color-mix(in srgb, var(--sc-accent) 25%, transparent), transparent 22%); }
.sc-window { right: 7%; top: 11%; width: 24%; height: 38%; z-index: 1; border: 1px solid color-mix(in srgb, var(--sc-ink) 36%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--sc-blue) 30%, transparent), transparent);
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.22); opacity: .68; }
.sc-window::before, .sc-window::after { content: ""; position: absolute; background: color-mix(in srgb, var(--sc-ink) 32%, transparent); }
.sc-window::before { left: 0; right: 0; top: 50%; height: 1px; }
.sc-window::after { top: 0; bottom: 0; left: 50%; width: 1px; }
.sc-cover { z-index: 3; object-fit: cover; border: 1px solid rgba(255,255,255,.48);
  box-shadow: 0 14px 28px var(--sc-shadow), 0 0 0 1px rgba(39, 30, 21, .2);
  filter: saturate(.92) contrast(1.06); background: var(--sc-paper); }
.sc-c1 { left: 9%; top: 12%; width: 28%; height: 58%; transform: rotate(-7deg); }
.sc-c2 { left: 29%; top: 8%; width: 24%; height: 52%; transform: rotate(4deg); }
.sc-c3 { right: 15%; top: 16%; width: 23%; height: 50%; transform: rotate(8deg); }
.sc-c4 { right: 33%; top: 22%; width: 19%; height: 42%; transform: rotate(-3deg); opacity: .9; }
.scene-v1 .sc-c1 { left: 12%; top: 8%; width: 25%; height: 55%; transform: rotate(-3deg); }
.scene-v1 .sc-c2 { left: 34%; top: 14%; width: 23%; height: 47%; transform: rotate(8deg); }
.scene-v1 .sc-c3 { right: 12%; top: 9%; width: 24%; height: 52%; transform: rotate(-6deg); }
.scene-v1 .sc-c4 { right: 33%; top: 29%; width: 18%; height: 38%; transform: rotate(3deg); }
.scene-v1 .sc-s1 { left: 16%; bottom: 19%; transform: rotate(-4deg); }
.scene-v1 .sc-s2 { right: 18%; bottom: 21%; transform: rotate(9deg); }
.scene-v1 .sc-lamp { left: 56%; }
.scene-v1 .sc-stamp { right: auto; left: 7%; transform: rotate(2deg); }
.scene-v2 .sc-c1 { left: 7%; top: 17%; width: 27%; height: 51%; transform: rotate(-10deg); }
.scene-v2 .sc-c2 { left: 25%; top: 7%; width: 26%; height: 55%; transform: rotate(2deg); }
.scene-v2 .sc-c3 { right: 18%; top: 11%; width: 22%; height: 49%; transform: rotate(11deg); }
.scene-v2 .sc-c4 { right: 38%; top: 24%; width: 18%; height: 40%; transform: rotate(-5deg); }
.scene-v2 .sc-wire1 { top: 24%; transform: rotate(2deg); }
.scene-v2 .sc-wire2 { top: 27%; transform: rotate(-18deg); }
.scene-v2 .sc-book { left: 27%; width: 37%; transform: rotate(2deg); }
.scene-v2 .sc-stamp { bottom: 11%; transform: rotate(-7deg); }
.scene-v3 .sc-c1 { left: 16%; top: 10%; width: 23%; height: 50%; transform: rotate(6deg); }
.scene-v3 .sc-c2 { left: 38%; top: 7%; width: 24%; height: 52%; transform: rotate(-6deg); }
.scene-v3 .sc-c3 { right: 8%; top: 18%; width: 24%; height: 50%; transform: rotate(4deg); }
.scene-v3 .sc-c4 { right: 29%; top: 18%; width: 18%; height: 39%; transform: rotate(9deg); }
.scene-v3 .sc-window { right: 11%; top: 8%; }
.scene-v3 .sc-s3 { left: 55%; bottom: 37%; transform: rotate(5deg); }
.scene-v3 .sc-lamp { left: 45%; }
.scene-v3 .sc-stamp { right: 8%; bottom: 13%; transform: rotate(3deg); }
.scene-v4 .sc-c1 { left: 10%; top: 7%; width: 29%; height: 57%; transform: rotate(-4deg); }
.scene-v4 .sc-c2 { left: 36%; top: 18%; width: 21%; height: 44%; transform: rotate(7deg); }
.scene-v4 .sc-c3 { right: 10%; top: 13%; width: 26%; height: 53%; transform: rotate(-9deg); }
.scene-v4 .sc-c4 { right: 36%; top: 9%; width: 17%; height: 37%; transform: rotate(2deg); }
.scene-v4 .sc-pin1 { left: 14%; top: 12%; }
.scene-v4 .sc-pin2 { right: 18%; top: 17%; }
.scene-v4 .sc-sheet { bottom: 16%; }
.scene-v4 .sc-stamp { right: 9%; bottom: 7%; transform: rotate(-1deg); }
.scene-v5 .sc-c1 { left: 6%; top: 11%; width: 24%; height: 52%; transform: rotate(-6deg); }
.scene-v5 .sc-c2 { left: 28%; top: 11%; width: 25%; height: 53%; transform: rotate(5deg); }
.scene-v5 .sc-c3 { right: 13%; top: 8%; width: 22%; height: 48%; transform: rotate(-2deg); }
.scene-v5 .sc-c4 { right: 31%; top: 28%; width: 20%; height: 43%; transform: rotate(8deg); }
.scene-v5 .sc-wire1 { left: 18%; width: 48%; transform: rotate(12deg); }
.scene-v5 .sc-wire2 { left: 37%; width: 40%; transform: rotate(-8deg); }
.scene-v5 .sc-lamp { left: 61%; height: 23%; }
.scene-v5 .sc-stamp { right: 6%; bottom: 10%; transform: rotate(-5deg); }
.sc-pin { width: 9px; height: 9px; border-radius: 50%; z-index: 5; background: var(--sc-accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--sc-accent) 20%, transparent), 0 2px 8px var(--sc-shadow); }
.sc-pin1 { left: 18%; top: 15%; } .sc-pin2 { right: 22%; top: 21%; }
.sc-wire { height: 1px; z-index: 2; transform-origin: 0 50%; background: color-mix(in srgb, var(--sc-accent) 72%, #fff 28%);
  box-shadow: 0 0 9px color-mix(in srgb, var(--sc-accent) 42%, transparent); }
.sc-wire1 { left: 22%; top: 19%; width: 42%; transform: rotate(8deg); }
.sc-wire2 { left: 43%; top: 18%; width: 35%; transform: rotate(-13deg); }
.sc-table { left: -5%; right: -5%; bottom: -1%; height: 34%; z-index: 4; background:
  linear-gradient(180deg, color-mix(in srgb, var(--sc-paper) 62%, #fff 38%), color-mix(in srgb, var(--sc-warm) 50%, #302114 50%));
  border-top: 1px solid color-mix(in srgb, var(--sc-ink) 24%, transparent);
  box-shadow: 0 -12px 30px color-mix(in srgb, var(--sc-shadow) 55%, transparent); transform: skewY(-2deg); }
.sc-book { left: 31%; bottom: 16%; width: 34%; height: 20%; z-index: 6; background: var(--sc-paper);
  border: 1px solid rgba(43, 34, 25, .28); border-radius: 1px 1px 12px 12px;
  box-shadow: 0 10px 18px var(--sc-shadow); transform: rotate(-1deg); }
.sc-book::before { content: ""; position: absolute; left: 49.5%; top: 0; bottom: 0; width: 1px; background: rgba(43, 34, 25, .34); }
.sc-book::after { content: ""; position: absolute; left: 8%; right: 8%; top: 28%; height: 1px; background: rgba(43, 34, 25, .32);
  box-shadow: 0 8px 0 rgba(43, 34, 25, .22), 0 16px 0 rgba(43, 34, 25, .18); }
.sc-sheet { z-index: 6; background: color-mix(in srgb, var(--sc-paper) 82%, #fff 18%);
  border: 1px solid rgba(43, 34, 25, .25); box-shadow: 0 8px 18px var(--sc-shadow); }
.sc-sheet::before { content: ""; position: absolute; left: 10%; right: 10%; top: 23%; height: 1px; background: rgba(43, 34, 25, .34);
  box-shadow: 0 8px 0 rgba(43, 34, 25, .18), 0 16px 0 rgba(43, 34, 25, .15), 0 24px 0 rgba(43, 34, 25, .12); }
.sc-s1 { left: 12%; bottom: 20%; width: 20%; height: 18%; transform: rotate(-8deg); }
.sc-s2 { right: 14%; bottom: 18%; width: 21%; height: 17%; transform: rotate(6deg); }
.sc-s3 { left: 60%; bottom: 34%; width: 16%; height: 13%; transform: rotate(-3deg); opacity: .92; }
.sc-lamp { left: 50%; bottom: 35%; width: 2px; height: 27%; z-index: 6; background: color-mix(in srgb, var(--sc-ink) 88%, #000 12%);
  box-shadow: 0 0 0 1px rgba(255,255,255,.12); }
.sc-lamp::before { content: ""; position: absolute; left: -19px; top: -10px; width: 40px; height: 20px;
  background: color-mix(in srgb, var(--sc-accent) 82%, #fff 18%); clip-path: polygon(20% 0, 80% 0, 100% 100%, 0 100%);
  box-shadow: 0 9px 28px color-mix(in srgb, var(--sc-accent) 52%, transparent); }
.sc-glow { left: 37%; bottom: 22%; width: 30%; height: 22%; z-index: 5; border-radius: 50%;
  background: radial-gradient(circle, color-mix(in srgb, var(--sc-accent) 42%, transparent), transparent 68%); opacity: .88; }
.sc-screen { z-index: 4; background: color-mix(in srgb, var(--sc-blue) 78%, #050505 22%);
  border: 1px solid color-mix(in srgb, var(--sc-blue) 48%, #fff 12%); box-shadow: 0 14px 28px var(--sc-shadow); }
.sc-screen::before { content: ""; position: absolute; left: 10%; right: 10%; top: 20%; height: 2px;
  background: color-mix(in srgb, var(--sc-accent) 82%, #fff 18%);
  box-shadow: 0 9px 0 rgba(255,255,255,.45), 0 18px 0 color-mix(in srgb, var(--sc-accent) 46%, transparent), 0 30px 0 rgba(255,255,255,.22); }
.sc-screen1 { left: 9%; top: 12%; width: 26%; height: 28%; transform: rotate(-2deg); }
.sc-screen2 { right: 8%; top: 14%; width: 25%; height: 27%; transform: rotate(3deg); }
.sc-ticket { z-index: 7; width: 18%; height: 17%; background: color-mix(in srgb, var(--sc-paper) 78%, #fff 22%);
  border: 1px solid rgba(43, 34, 25, .28); box-shadow: 0 9px 16px var(--sc-shadow); }
.sc-ticket::before { content: ""; position: absolute; left: 10%; right: 10%; top: 24%; height: 3px; background: var(--sc-accent);
  box-shadow: 0 11px 0 rgba(43, 34, 25, .28), 0 22px 0 rgba(43, 34, 25, .16); }
.sc-t1 { left: 15%; bottom: 18%; transform: rotate(-8deg); }
.sc-t2 { left: 42%; bottom: 18%; transform: rotate(3deg); }
.sc-t3 { right: 14%; bottom: 18%; transform: rotate(8deg); }
.sc-mapgrid { left: 12%; right: 10%; top: 12%; bottom: 22%; z-index: 2; border: 1px solid rgba(43, 34, 25, .22);
  background: linear-gradient(90deg, transparent 0 32%, rgba(43,34,25,.16) 32% 32.5%, transparent 32.5% 66%, rgba(43,34,25,.12) 66% 66.5%, transparent 66.5%),
  linear-gradient(0deg, transparent 0 45%, rgba(43,34,25,.12) 45% 45.5%, transparent 45.5%); opacity: .58; }
.sc-path { z-index: 5; width: 12px; height: 12px; border-radius: 50%; background: var(--sc-accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--sc-accent) 18%, transparent), 0 0 20px color-mix(in srgb, var(--sc-accent) 40%, transparent); }
.sc-path1 { left: 22%; top: 32%; } .sc-path2 { left: 49%; top: 24%; } .sc-path3 { right: 24%; top: 42%; }
.sc-roller { z-index: 7; height: 12%; border-radius: 999px; background: color-mix(in srgb, var(--sc-ink) 84%, #000 16%);
  box-shadow: 0 10px 18px var(--sc-shadow); }
.sc-r1 { left: 18%; right: 18%; bottom: 27%; transform: rotate(-2deg); }
.sc-r2 { left: 25%; right: 25%; bottom: 42%; background: var(--sc-accent); opacity: .82; transform: rotate(2deg); }
.sc-stamp { right: 7%; bottom: 8%; z-index: 10; padding: .34rem .5rem .3rem; font-family: var(--mono);
  font-size: clamp(.48rem, 1.15vw, .68rem); font-weight: 700; letter-spacing: .12em; color: var(--sc-accent);
  border: 1px solid currentColor; background: color-mix(in srgb, var(--sc-paper) 76%, transparent);
  transform: rotate(-3deg); box-shadow: 0 8px 14px var(--sc-shadow); white-space: nowrap; }
.scene-plate.band-scene { width: 164px; min-width: 164px; aspect-ratio: 4 / 3; }
.scene-plate.hero-scene { max-width: 420px; margin: 1.1rem 0 0; }
.scene-plate.section-scene { max-width: 760px; aspect-ratio: 21 / 9; margin: 1.05rem auto 1.25rem; }
.scene-plate.feature-scene { margin: 0 0 1rem; }
.scene-plate.article-scene, .scene-plate.edition-scene { max-width: 680px; margin: 0 auto 2rem; }
.scene-plate.card-scene { aspect-ratio: 16 / 9; margin: 0 0 .75rem; }
.scene-plate.reader-scene { aspect-ratio: 16 / 9; margin: 1rem 0 1.65rem; }
/* Section fronts share one banner ratio, so the numbers and the board sit near
   the fold instead of below a half-screen of scenery. */
.scene-plate.page-scene { max-width: 760px; aspect-ratio: 21 / 9; margin: 1.05rem auto 1.35rem; }
.scene-plate.detail-scene { max-width: 680px; margin: 1rem auto 1.2rem; }
.scene-plate .sc-scene { position: absolute; inset: 0; width: 100%; height: 100%;
  display: block; object-fit: cover; object-position: center; filter: saturate(.96) contrast(1.03); }
.scene-plate:has(.sc-scene)::before { z-index: 2; opacity: .22;
  background: linear-gradient(180deg, transparent 62%, rgba(20, 15, 10, .24)); }
.scene-plate:has(.sc-scene)::after { z-index: 3; }
[data-theme="dark"] .scene-plate .sc-scene { filter: saturate(.9) brightness(.82) contrast(1.08); }
[data-theme="dark"] .scene-plate:has(.sc-scene)::before { opacity: .3; mix-blend-mode: normal; }
.scene-ghost { --sc-accent: #9a2c1a; }
[data-theme="dark"] .scene-ghost { --sc-accent: #d98055; }
.scene-fingerprint, .scene-briefing { --sc-accent: #0d5b68; }
[data-theme="dark"] .scene-fingerprint, [data-theme="dark"] .scene-briefing { --sc-accent: #62aab8; }
.scene-pamphlet { --sc-accent: #8f2c18; }
.scene-forecast { --sc-accent: #b98512; }
[data-theme="dark"] .scene-forecast { --sc-accent: #f1c34e; }
.scene-map { --sc-accent: #5d6f36; }
.scene-quiz { --sc-accent: #69538f; }
.scene-wrapped { --sc-accent: #b36a2e; }
.scene-fingerprint .sc-lamp, .scene-fingerprint .sc-glow, .scene-fingerprint .sc-book, .scene-fingerprint .sc-roller,
.scene-fingerprint .sc-sheet, .scene-fingerprint .sc-window,
.scene-briefing .sc-lamp, .scene-briefing .sc-glow, .scene-briefing .sc-book, .scene-briefing .sc-window, .scene-briefing .sc-roller,
.scene-forecast .sc-lamp, .scene-forecast .sc-glow, .scene-forecast .sc-book, .scene-forecast .sc-window, .scene-forecast .sc-roller { display: none; }
.scene-fingerprint .sc-screen, .scene-briefing .sc-screen, .scene-forecast .sc-screen { display: block; }
.scene-fingerprint .sc-c1, .scene-briefing .sc-c1, .scene-forecast .sc-c1 { left: 35%; top: 10%; width: 22%; height: 48%; transform: rotate(-4deg); }
.scene-fingerprint .sc-c2, .scene-briefing .sc-c2, .scene-forecast .sc-c2 { left: 55%; top: 17%; width: 20%; height: 43%; transform: rotate(5deg); }
.scene-fingerprint .sc-c3, .scene-briefing .sc-c3, .scene-forecast .sc-c3 { display: none; }
.scene-fingerprint .sc-ticket, .scene-briefing .sc-ticket { height: 15%; bottom: 17%; }
.scene-pamphlet .sc-screen, .scene-pamphlet .sc-window, .scene-pamphlet .sc-lamp, .scene-pamphlet .sc-glow,
.scene-pamphlet .sc-book, .scene-pamphlet .sc-mapgrid, .scene-pamphlet .sc-path,
.scene-pamphlet .sc-wire, .scene-pamphlet .sc-pin { display: none; }
.scene-pamphlet .sc-c1 { left: 10%; top: 12%; width: 25%; height: 56%; }
.scene-pamphlet .sc-c2 { right: 11%; top: 14%; left: auto; width: 23%; height: 50%; }
.scene-pamphlet .sc-sheet { display: block; }
.scene-pamphlet .sc-s1 { width: 23%; height: 20%; left: 19%; bottom: 18%; }
.scene-pamphlet .sc-s2 { width: 23%; height: 20%; right: 19%; bottom: 18%; }
.scene-pamphlet .sc-s3 { left: 41%; bottom: 38%; width: 20%; height: 15%; }
.scene-research .sc-screen, .scene-research .sc-ticket, .scene-research .sc-roller, .scene-research .sc-mapgrid, .scene-research .sc-path,
.scene-collection .sc-screen, .scene-collection .sc-ticket, .scene-collection .sc-roller, .scene-collection .sc-mapgrid, .scene-collection .sc-path,
.scene-quiz .sc-screen, .scene-quiz .sc-roller, .scene-quiz .sc-mapgrid, .scene-quiz .sc-path,
.scene-wrapped .sc-screen, .scene-wrapped .sc-roller, .scene-wrapped .sc-mapgrid, .scene-wrapped .sc-path { display: none; }
.scene-map .sc-lamp, .scene-map .sc-glow, .scene-map .sc-book, .scene-map .sc-roller, .scene-map .sc-ticket,
.scene-map .sc-screen, .scene-map .sc-window { display: none; }
.scene-map .sc-mapgrid, .scene-map .sc-path { display: block; }
.scene-map .sc-c1 { left: 12%; top: 17%; width: 20%; height: 42%; transform: rotate(-4deg); }
.scene-map .sc-c2 { left: 41%; top: 10%; width: 18%; height: 38%; transform: rotate(3deg); }
.scene-map .sc-c3 { right: 13%; top: 24%; width: 19%; height: 40%; transform: rotate(7deg); }
.scene-map .sc-c4 { left: 29%; top: 39%; width: 17%; height: 34%; transform: rotate(-8deg); }
.scene-ghost .sc-screen, .scene-ghost .sc-roller, .scene-ghost .sc-ticket, .scene-ghost .sc-mapgrid, .scene-ghost .sc-path { display: none; }
.scene-ghost .sc-window { display: block; opacity: .78; }
.scene-ghost .sc-c1, .scene-ghost .sc-c2, .scene-ghost .sc-c3 { filter: grayscale(.12) sepia(.14) contrast(1.04); opacity: .94; }
.scene-quiz .sc-ticket { display: block; }
.scene-quiz .sc-t1::before, .scene-quiz .sc-t2::before, .scene-quiz .sc-t3::before { background: transparent;
  box-shadow: inset 0 0 0 2px var(--sc-accent), 0 13px 0 rgba(43, 34, 25, .2), 0 26px 0 rgba(43, 34, 25, .13); }
.scene-wrapped .sc-ticket { display: block; }
.scene-wrapped .sc-t1, .scene-wrapped .sc-t2, .scene-wrapped .sc-t3 { height: 22%; }
.scene-plate.band-scene .sc-stamp { display: none; }
.scene-plate.band-scene .sc-c4, .scene-plate.band-scene .sc-s3, .scene-plate.band-scene .sc-ticket,
.scene-plate.band-scene .sc-window, .scene-plate.band-scene .sc-mapgrid, .scene-plate.band-scene .sc-path { display: none; }
.scene-plate.band-scene .sc-c1 { width: 42%; height: 58%; left: 9%; top: 13%; }
.scene-plate.band-scene .sc-c2 { width: 36%; height: 50%; left: 45%; top: 18%; }
.scene-plate.band-scene .sc-c3 { width: 30%; height: 42%; right: 8%; top: 32%; }
.scene-plate.band-scene .sc-book { left: 18%; bottom: 16%; width: 48%; height: 19%; }
.scene-plate.band-scene .sc-sheet { width: 28%; height: 17%; bottom: 18%; }
.scene-plate.band-scene .sc-lamp { left: 62%; bottom: 32%; height: 24%; }
@media (max-width: 680px) {
  .scene-plate.band-scene { width: 100%; min-width: 0; max-width: 320px; }
  .scene-plate.hero-scene { max-width: 360px; }
  .scene-plate.section-scene { margin-top: .9rem; }
}
"""

# Live category filter + text search, scoped per `.lib-pane` — the home page
# carries two independent panes (Research Library, the mirrored desk) side by
# side, so each pane's search box and pills only ever touch its own grid.
# Category pills and the search box combine: a card shows only if it matches
# the active category AND the search term. Empty category sections hide their
# heading; a message shows if nothing matches.
LIBRARY_FILTER_JS = """
(function () {
  Array.prototype.slice.call(document.querySelectorAll('.lib-pane')).forEach(function (pane) {
  var search = pane.querySelector('.lib-search');
  var empty = pane.querySelector('.lib-empty');
  var pills = Array.prototype.slice.call(pane.querySelectorAll('.cat-pill'));
  var sections = Array.prototype.slice.call(pane.querySelectorAll('.cat-section'));
  var groups = Array.prototype.slice.call(pane.querySelectorAll('.domain-group'));
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
    // A domain super-heading hides when every category shelf under it is empty,
    // so filtering never leaves an orphaned domain title.
    groups.forEach(function (g) {
      var vis = 0;
      g.querySelectorAll('.cat-section').forEach(function (s) { if (!s.hidden) vis++; });
      g.hidden = vis === 0;
    });
    if (empty) empty.hidden = anyVisible;
    // The "For You" rows are a browse-what-to-read surface; hide them while a
    // category/search filter is active so they don't contradict the filtered grid.
    var fy = pane.querySelector('#foryou');
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
  });
})();
"""

# Today's Passage — a date-seeded pull-quote, preferring corpora the visitor
# hasn't opened. The home page mirrors two of these side by side — one drawn
# from the Research Library's passages, one from the detached desk's — each
# reading its own #passages-data-* / #daily-passage-* pair independently so
# the two picks are unrelated to one another. Pure client-side: date math +
# localStorage history, no fetch.
DAILY_PASSAGE_JS = r"""
(function () {
  var esc = function (s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  function render(el, dataEl, kicker) {
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
    var href = esc(p.slug) + '.html?q=' + encodeURIComponent(p.text.slice(0, 42)) + '#ch-' + p.chapter;
    el.innerHTML = '<a class="dp-quote" href="' + href + '">'
      + '<span class="dp-kicker">' + esc(kicker) + '</span>'
      + '<span class="dp-mark" aria-hidden="true">“</span>'
      + '<span class="dp-text">' + esc(p.text) + '</span>'
      + '<span class="dp-src">— ' + esc(p.title) + ' · ' + esc(p.chapterTitle) + '</span>'
      + '<span class="dp-cta">Read in context →</span>'
      + '<button class="dp-share" type="button" aria-label="Share this passage">Share ↗</button></a>';
    el.hidden = false;
    var sb = el.querySelector('.dp-share');
    if (sb) sb.onclick = function (ev) { ev.preventDefault(); ev.stopPropagation();
      if (!window.CorpusShare) return;  // the shell (which defines CorpusShare) loads after this script
      window.CorpusShare.open({ kicker: kicker, quote: p.text, source: p.title + ' · ' + p.chapterTitle,
        url: new URL(href, location.href).href, filename: 'passage' }); };
  }
  render(document.getElementById('daily-passage-lib'), document.getElementById('passages-data-lib'),
         'Today’s Passage · The Research');
  render(document.getElementById('daily-passage-adtech'), document.getElementById('passages-data-adtech'),
         'Today’s Passage · Ad Tech');
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
# assembled entirely from the existing brand (site title/subtitle + the section
# names, each a live link into its section) plus the trencadís mosaic. A tiny
# synchronous <head> script adds
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
.ov-tiles { display: flex; gap: 3px; justify-content: center; margin: 0 0 1.7rem; }
.ov-tiles span { width: 8px; height: 8px; border-radius: 0; transform: none; box-shadow: none; }
.ov-tiles span:nth-child(1) { background: var(--t1); }
.ov-tiles span:nth-child(2) { background: var(--t2); }
.ov-tiles span:nth-child(3) { background: var(--t3); }
.ov-tiles span:nth-child(4) { background: var(--t4); }
.ov-tiles span:nth-child(5) { background: var(--t5); }
.ov-brand { display: inline-block; padding: .3em .65em; border: 1.5px solid var(--accent);
  color: var(--accent); font-family: var(--mono); font-size: .68rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase; transform: rotate(-1.5deg);
  opacity: .92; border-radius: 1px; margin: 0 0 1rem; }
.ov-title { font-family: var(--display); font-weight: 600; font-size: clamp(2.4rem, 7vw, 4rem); line-height: 1.04;
  text-transform: uppercase; letter-spacing: .04em; border-top: 2px solid var(--text); border-bottom: 1px solid var(--text);
  padding: 1rem 0; margin: 0 0 .6rem; }
.ov-sub { font-family: var(--serif); font-style: italic; font-size: 1.25rem; color: var(--muted); margin: 0 0 1.8rem; }
.ov-sections { display: flex; flex-wrap: wrap; gap: .55rem 1.7rem; justify-content: center; font-family: var(--sans);
  font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--muted); margin: 0 0 2.2rem; }
.ov-sections a { position: relative; color: inherit; text-decoration: none;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-size: 0% 1.5px; background-repeat: no-repeat; background-position: 0 100%;
  transition: background-size .18s var(--ease), color .14s var(--ease); }
.ov-sections a:not(:last-child)::after { content: "·"; position: absolute; right: -1rem; color: var(--border); }
.ov-sections a:hover, .ov-sections a:focus-visible { background-size: 100% 1.5px; color: var(--accent); }
#ov-enter { background: var(--text); color: var(--bg); border: 1px solid var(--text); border-radius: 2px;
  font-family: var(--sans); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .1em;
  padding: .8rem 1.8rem; cursor: pointer; box-shadow: none;
  transition: background-color .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
#ov-enter:hover, #ov-enter:focus-visible { background: var(--accent); border-color: var(--accent);
  transform: translateY(-1px); box-shadow: var(--shadow-2); }
#ov-enter:active { transform: translateY(0); box-shadow: none; }
.ov-skip { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 1rem 0 0; }
html.show-overture .ov-inner > * { animation: ovRise .7s cubic-bezier(.2,.7,.3,1) both; }
html.show-overture .ov-tiles { animation-delay: .05s; }
html.show-overture .ov-brand { animation-delay: .14s; }
html.show-overture .ov-title { animation: ovTrack .6s var(--ease) both; animation-delay: .22s; }
html.show-overture .ov-sub { animation-delay: .34s; }
html.show-overture .ov-sections { animation-delay: .46s; }
html.show-overture #ov-enter { animation-delay: .58s; }
html.show-overture .ov-skip { animation-delay: .7s; }
@keyframes ovFade { from { opacity: 0; } to { opacity: 1; } }
@keyframes ovOut { from { opacity: 1; } to { opacity: 0; visibility: hidden; } }
@keyframes ovRise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; } }
@keyframes ovTrack { from { opacity: 0; letter-spacing: .3em; } to { opacity: 1; letter-spacing: .04em; } }
@media (prefers-reduced-motion: reduce) {
  html.show-overture #overture, html.show-overture .ov-inner > * { animation: none !important; }
  #ov-enter, #ov-enter:hover, #ov-enter:focus-visible, #ov-enter:active,
  .ov-sections a, .ov-sections a:hover, .ov-sections a:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
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
  // section names are live links: remember the overture was seen before leaving.
  // The Research stays on this page — dismiss in place and glide to the library.
  var links = Array.prototype.slice.call(ov.querySelectorAll('.ov-sections a'));
  links.forEach(function (a) {
    a.addEventListener('click', function (e) {
      try { localStorage.setItem('seen-overture', '1'); } catch (err) {}
      var href = a.getAttribute('href') || '';
      if (href.charAt(0) === '#') {
        e.preventDefault(); dismiss();
        var t = document.querySelector(href);
        var quick = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (t) setTimeout(function () { t.scrollIntoView({ behavior: quick ? 'auto' : 'smooth' }); }, quick ? 0 : 540);
      }
    });
  });
  document.addEventListener('keydown', function (e) {
    if (!document.getElementById('overture')) return;  // after dismissal this handler no-ops site-wide
    if (e.key === 'Enter' && links.indexOf(document.activeElement) !== -1) return;  // follow the focused section link
    if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); dismiss(); }
    else if (e.key === 'Tab') {                       // trap focus inside the dialog, cycling links → enter button
      var f = links.concat(enter ? [enter] : []);
      if (!f.length) return;
      e.preventDefault();
      var i = f.indexOf(document.activeElement);
      f[e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === f.length - 1 ? 0 : i + 1)].focus();
    }
  });
  if (enter) try { enter.focus(); } catch (e) {}  // move focus into the dialog on open
})();
"""

# "Test Yourself" — the self-quiz that lives on its own page (quiz.html), built
# live from three payloads inlined there: #quiz-data (per-corpus content facts —
# glossary terms, key dates, key people/organisations, each located to its
# chapter), plus #library-manifest (corpora, chapter titles, category — carried
# by the shared shell) and #passages-data (pull-quotes tagged with corpus slug +
# chapter). Every question has a ground-truth answer drawn from that data — no
# fetch, no ML, no authored answer key. Rounds lead with content questions (what
# the research SAYS: terms, dates, people) so the quiz exercises retention of
# what was read; the structural questions (chapter titles, shelving, passage
# placement) only backfill. Three challenge levels change the round length, the
# option count, which question directions qualify, and how confusable the
# distractors are. Readers arrive via quiz.html?on=<slug> from a corpus's
# sidebar. Best score per level+scope is kept in localStorage; results share via
# CorpusShare.
QUIZ_JS = r"""
(function () {
  function init() {
  // Deferred to DOMContentLoaded (below): the shell's #library-manifest is parsed
  // AFTER this bundle's <script>, so reading it inline would find nothing.
  var sec = document.getElementById('quiz');
  var mEl = document.getElementById('library-manifest');
  if (!sec || !mEl) return;
  var LIB, PAS = [];
  try { LIB = JSON.parse(mEl.textContent); } catch (e) { return; }
  var pEl = document.getElementById('passages-data');
  try { PAS = pEl ? (JSON.parse(pEl.textContent || '[]') || []) : []; } catch (e) { PAS = []; }
  var corpora = (LIB || []).filter(function (x) { return x.kind === 'corpus' && x.chapters && x.chapters.length; });
  // An optional #quiz-slugs payload scopes the whole quiz (dropdown, questions,
  // distractors) to one shelf — used by detached desk pages (e.g. Ad Tech) and
  // by the home page to keep desk material off the library quiz.
  var scEl = document.getElementById('quiz-slugs');
  if (scEl) { try { var SC = {}; (JSON.parse(scEl.textContent || '[]') || []).forEach(function (s) { SC[s] = 1; }); corpora = corpora.filter(function (c) { return SC[c.slug]; }); } catch (e) {} }
  if (corpora.length < 2) return;                       // need a field of distractors
  var bySlug = {}; corpora.forEach(function (c) { bySlug[c.slug] = c; });
  // Content facts (#quiz-data): {slug, terms:[{t,d,c}], dates:[{d,e,c,a?}], entities:[{n,r,c,a?}]}
  var QF = {}, qfEl = document.getElementById('quiz-data');
  try { (qfEl ? (JSON.parse(qfEl.textContent || '[]') || []) : []).forEach(function (f) { if (f && f.slug && bySlug[f.slug]) QF[f.slug] = f; }); } catch (e) {}
  var passBySlug = {}; PAS.forEach(function (p) { if (bySlug[p.slug]) (passBySlug[p.slug] = passBySlug[p.slug] || []).push(p); });
  // Chapter titles shared by >1 corpus are ambiguous as "which corpus owns it?"
  // questions — count them so the across-library builder can skip the generic ones.
  var chTitleCount = {};
  corpora.forEach(function (c) { c.chapters.forEach(function (ch) { if (ch) { var k = ch.toLowerCase(); chTitleCount[k] = (chTitleCount[k] || 0) + 1; } }); });

  // Challenge levels. n = questions per round, opts = choices per question,
  // rot = which question kinds rotate into the round (easy keeps to the
  // recognition directions and lets structural questions mix in; hard leads
  // with the recall directions and bans them), backfill = whether structural
  // 'meta' questions may top up a short round. Distractor selection also keys
  // off the level: easy picks candidates far from the answer, hard the nearest.
  var LEVELS = {
    easy:   { n: 5,  opts: 3, rot: ['k-who', 'k-when', 'k-def', 'meta'], backfill: true },
    medium: { n: 7,  opts: 4, rot: ['k-def', 'k-term', 'k-when', 'k-what', 'k-who', 'k-role'], backfill: true },
    hard:   { n: 10, opts: 5, rot: ['k-what', 'k-role', 'k-term', 'k-when', 'k-who', 'k-def'], backfill: false }
  };
  var level = 'medium';

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function shuffle(a) { a = a.slice(); for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t; } return a; }
  // Option set: the correct answer plus distinct distractors. An `ordered` pool
  // is consumed front-to-back (already ranked for the level); else random.
  function options(correct, pool, n, ordered) {
    var seen = {}; seen[String(correct).toLowerCase()] = 1; var ds = [];
    (ordered ? pool : shuffle(pool)).forEach(function (x) { var k = String(x).toLowerCase(); if (x && !seen[k]) { seen[k] = 1; ds.push(x); } });
    return shuffle([correct].concat(ds.slice(0, Math.max(1, (n || 4) - 1))));
  }
  // Distractor ranking. closeness(candidate) — higher = more confusable with
  // the answer. hard leads with the closest, easy with the farthest; medium
  // passes through unranked (options() shuffles). Ties stay shuffled.
  function ranked(cands, closeness) {
    if (level === 'medium') return { pool: cands, ord: false };
    var arr = shuffle(cands).map(function (c) { return { c: c, s: closeness(c) }; });
    arr.sort(function (p, q) { return q.s - p.s; });
    if (level === 'easy') arr.reverse();
    return { pool: arr.map(function (o) { return o.c; }), ord: true };
  }
  function yrOf(s) { var m = String(s).match(/\d{3,4}/); return m ? +m[0] : null; }
  function toks(s) { var o = {}; String(s).toLowerCase().replace(/[^a-z0-9\s'-]/g, ' ').split(/\s+/).forEach(function (w) { if (w.length >= 4) o[w] = 1; }); return o; }
  function overlap(a, b) { var n = 0, k; for (k in a) if (b[k]) n++; return n; }

  // --- question builders (each returns an array of {kind, fact?, prompt, passage?, answer, opts, src?}) ---
  // Content questions first-class: what the research says — its terms of art,
  // its dates, its people. `fact` keys stop both directions of one fact (e.g.
  // event→date and date→event) landing in the same round. `kind` groups feed
  // pickRound's rotation. Structural questions get kind 'meta' (backfill only).
  function buildFacts(slug, across) {
    var c = bySlug[slug], f = QF[slug]; if (!c || !f) return [];
    var K = (LEVELS[level] || LEVELS.medium).opts;
    var qs = [], pre = across ? 'In <em>' + esc(c.title) + '</em> — ' : '';
    var terms = f.terms || [], dates = f.dates || [], ents = f.entities || [];
    if (terms.length >= K) terms.forEach(function (t) {
      var fk = slug + '|t|' + t.t, s = { slug: slug, chapter: t.c || 0, q: t.t };
      var td = toks(t.d);
      var r = ranked(terms.filter(function (x) { return x !== t; }),
                     function (x) { return overlap(toks(x.d), td); });
      qs.push({ kind: 'k-def', fact: fk, prompt: pre + 'What does “' + esc(t.t) + '” mean?',
        answer: t.d, opts: options(t.d, r.pool.map(function (x) { return x.d; }), K, r.ord), src: s });
      qs.push({ kind: 'k-term', fact: fk, prompt: pre + 'Which term does this define?', passage: t.d,
        answer: t.t, opts: options(t.t, r.pool.map(function (x) { return x.t; }), K, r.ord), src: s });
    });
    if (dates.length >= K) dates.forEach(function (x) {
      var fk = slug + '|d|' + x.d + x.e, s = { slug: slug, chapter: x.c || 0, q: x.a };
      var y0 = yrOf(x.d);
      var r = ranked(dates.filter(function (y) { return y !== x; }),
                     function (y) { var yy = yrOf(y.d); return (y0 == null || yy == null) ? -1e9 : -Math.abs(yy - y0); });
      qs.push({ kind: 'k-when', fact: fk, prompt: pre + 'When did this happen?', passage: x.e,
        answer: x.d, opts: options(x.d, r.pool.map(function (y) { return y.d; }), K, r.ord), src: s });
      qs.push({ kind: 'k-what', fact: fk, prompt: pre + 'What happened ' + (/^\d{1,2} [A-Z]/.test(x.d) ? 'on ' : 'in ') + esc(x.d) + '?',
        answer: x.e, opts: options(x.e, r.pool.map(function (y) { return y.e; }), K, r.ord), src: s });
    });
    if (ents.length >= K) ents.forEach(function (x) {
      var fk = slug + '|e|' + x.n, s = { slug: slug, chapter: x.c || 0, q: x.a };
      var xr = toks(x.r);
      var r = ranked(ents.filter(function (y) { return y !== x; }),
                     function (y) { return overlap(toks(y.r), xr); });
      qs.push({ kind: 'k-who', fact: fk, prompt: pre + 'Who — or what — is described here?', passage: x.r,
        answer: x.n, opts: options(x.n, r.pool.map(function (y) { return y.n; }), K, r.ord), src: s });
      qs.push({ kind: 'k-role', fact: fk, prompt: pre + 'Which description fits <em>' + esc(x.n) + '</em>?',
        answer: x.r, opts: options(x.r, r.pool.map(function (y) { return y.r; }), K, r.ord), src: s });
    });
    return qs;
  }
  function buildAcross() {
    var qs = [], titles = corpora.map(function (c) { return c.title; });
    var K = (LEVELS[level] || LEVELS.medium).opts;
    corpora.forEach(function (c) { qs.push.apply(qs, buildFacts(c.slug, true)); });
    PAS.forEach(function (p) {
      if (!bySlug[p.slug] || !p.text || p.text.length < 40) return;
      qs.push({ kind: 'meta', fact: 'pas|' + p.text, prompt: 'Which corpus is this passage from?', passage: p.text,
        answer: bySlug[p.slug].title, opts: options(bySlug[p.slug].title, titles, K),
        src: { slug: p.slug, chapter: p.chapter, q: p.text } });
    });
    corpora.forEach(function (c) {
      c.chapters.forEach(function (ch, i) {
        if (!ch || ch.length < 5 || chTitleCount[ch.toLowerCase()] > 1) return;
        qs.push({ kind: 'meta', prompt: 'Which corpus has a chapter titled “' + esc(ch) + '”?',
          answer: c.title, opts: options(c.title, titles, K), src: { slug: c.slug, chapter: i } });
      });
    });
    var cats = {}; corpora.forEach(function (c) { if (c.category && c.category !== 'Other') cats[c.category] = 1; });
    var catList = Object.keys(cats);
    if (catList.length >= 3) corpora.forEach(function (c) {
      if (!c.category || c.category === 'Other') return;
      qs.push({ kind: 'meta', prompt: 'Under which subject is <em>' + esc(c.title) + '</em> shelved?',
        answer: c.category, opts: options(c.category, catList, K), src: { slug: c.slug, chapter: 0 } });
    });
    return qs;
  }
  function buildSingle(slug) {
    var c = bySlug[slug]; if (!c) return [];
    var qs = buildFacts(slug, false);
    var K = (LEVELS[level] || LEVELS.medium).opts;
    var mine = c.chapters.filter(function (ch) { return ch && ch.length >= 3; });
    var mineLower = {}; mine.forEach(function (ch) { mineLower[ch.toLowerCase()] = 1; });
    var others = []; corpora.forEach(function (o) { if (o.slug !== slug) o.chapters.forEach(function (ch) { if (ch && !mineLower[ch.toLowerCase()]) others.push(ch); }); });
    (passBySlug[slug] || []).forEach(function (p) {
      if (!p.chapterTitle || !p.text || p.text.length < 40 || mine.length < 2) return;
      qs.push({ kind: 'meta', fact: 'pas|' + p.text, prompt: 'Which chapter of <em>' + esc(c.title) + '</em> is this from?', passage: p.text,
        answer: p.chapterTitle, opts: options(p.chapterTitle, mine, K), src: { slug: slug, chapter: p.chapter, q: p.text } });
    });
    if (others.length >= 3) mine.forEach(function (ch, i) {
      qs.push({ kind: 'meta', prompt: 'Which of these is a real chapter in <em>' + esc(c.title) + '</em>?',
        answer: ch, opts: options(ch, others, K), src: { slug: slug, chapter: i } });
    });
    return qs;
  }
  // Assemble a round: dedup, then fill by rotating across the level's question
  // kinds so no two questions expose the same fact. Easy admits structural
  // 'meta' questions to its rotation; medium keeps them as backfill only; hard
  // excludes them entirely (a thin corpus just yields a shorter hard round).
  function pickRound(pool) {
    var L = LEVELS[level] || LEVELS.medium;
    var seen = {}, uniq = [];
    shuffle(pool).forEach(function (q) { var k = (q.passage || '') + '|' + q.answer + '|' + q.prompt; if (!seen[k]) { seen[k] = 1; uniq.push(q); } });
    var groups = {};
    uniq.forEach(function (q) { var g = q.kind || 'meta'; (groups[g] = groups[g] || []).push(q); });
    var rot = shuffle(L.rot.filter(function (g) { return (groups[g] || []).length; }));
    var picked = [], usedFact = {};
    function take(g) {
      while (g && g.length) {
        var q = g.pop();
        if (!q.fact || !usedFact[q.fact]) { if (q.fact) usedFact[q.fact] = 1; return q; }
      }
      return null;
    }
    var took = true;
    while (picked.length < L.n && took) {
      took = false;
      for (var i = 0; i < rot.length && picked.length < L.n; i++) {
        var q = take(groups[rot[i]]);
        if (q) { picked.push(q); took = true; }
      }
    }
    if (L.backfill) while (picked.length < L.n) {
      var mq = take(groups.meta);
      if (!mq) break;
      picked.push(mq);
    }
    return shuffle(picked);
  }

  // --- setup UI ---
  var scope = document.getElementById('quiz-scope');
  var optAll = document.createElement('option'); optAll.value = '__all__'; optAll.textContent = sec.getAttribute('data-all-label') || 'Across the whole library'; scope.appendChild(optAll);
  corpora.slice().sort(function (a, b) { return a.title.localeCompare(b.title); }).forEach(function (c) {
    var o = document.createElement('option'); o.value = c.slug; o.textContent = c.title; scope.appendChild(o);
  });
  sec.hidden = false;

  var setup = document.getElementById('quiz-setup'), stage = document.getElementById('quiz-stage'), bestEl = document.getElementById('quiz-best');
  var state = null;
  var NS = sec.getAttribute('data-ns') || '';   // separate best-score ledgers per desk
  var lvlBtns = Array.prototype.slice.call(document.querySelectorAll('.quiz-level'));
  function setLevel(lv) {
    if (!LEVELS[lv]) return;
    level = lv;
    lvlBtns.forEach(function (b) { b.classList.toggle('is-on', b.getAttribute('data-level') === lv); });
    showBest(scope.value);
  }
  lvlBtns.forEach(function (b) { b.addEventListener('click', function () { setLevel(b.getAttribute('data-level')); }); });
  function bestKey(s) { return 'quiz-best:' + NS + level + ':' + s; }
  function showBest(s) {
    var b = null; try { b = JSON.parse(localStorage.getItem(bestKey(s)) || 'null'); } catch (e) {}
    if (b && b.total) { bestEl.textContent = 'Your best here on ' + level + ': ' + b.score + ' / ' + b.total + '.'; bestEl.hidden = false; }
    else bestEl.hidden = true;
  }
  scope.addEventListener('change', function () { showBest(scope.value); });
  // Deep links: a corpus's sidebar arrives at quiz.html?on=<slug> (and an
  // optional &level=) with that corpus preselected, one click from a round.
  try {
    var usp = new URLSearchParams(location.search);
    if (usp.get('level')) setLevel(usp.get('level'));
    var on = usp.get('on');
    if (on && bySlug[on]) scope.value = on;
  } catch (e) {}
  showBest(scope.value);

  document.getElementById('quiz-start').addEventListener('click', function () {
    var sv = scope.value, pool = sv === '__all__' ? buildAcross() : buildSingle(sv);
    var qs = pickRound(pool);
    setup.hidden = true; stage.hidden = false;
    if (!qs.length) { stage.innerHTML = '<p class="quiz-empty">There isn’t enough material to build a test on this selection yet. Try “Across the whole library.”</p><p style="margin:1rem 0 0"><button class="quiz-btn quiz-again" type="button">← Back</button></p>'; wireAgain(); return; }
    state = { sv: sv, lv: level, qs: qs, i: 0, score: 0, label: sv === '__all__' ? (sec.getAttribute('data-all-name') || 'the whole library') : (bySlug[sv] ? bySlug[sv].title : '') };
    renderQ();
  });

  function renderQ() {
    var q = state.qs[state.i], h = '';
    h += '<div class="quiz-progress"><span>Question ' + (state.i + 1) + ' of ' + state.qs.length + '</span><span class="quiz-score">Score ' + state.score + '</span></div>';
    h += '<div class="quiz-bar"><span style="width:' + (state.i / state.qs.length * 100) + '%"></span></div>';
    h += '<p class="quiz-q">' + q.prompt + '</p>';
    if (q.passage) h += '<blockquote class="quiz-passage">' + esc(q.passage) + '</blockquote>';
    h += '<div class="quiz-opts">';
    q.opts.forEach(function (o) { h += '<button class="quiz-opt" type="button" data-val="' + esc(o) + '">' + esc(o) + '</button>'; });
    h += '</div><div class="quiz-feedback" id="quiz-feedback" hidden></div>';
    stage.innerHTML = h;
    Array.prototype.forEach.call(stage.querySelectorAll('.quiz-opt'), function (b) {
      b.addEventListener('click', function () { answer(b.getAttribute('data-val'), q); });
    });
  }

  function answer(chosen, q) {
    var btns = stage.querySelectorAll('.quiz-opt'), got = chosen === q.answer;
    if (got) state.score++;
    Array.prototype.forEach.call(btns, function (b) {
      b.disabled = true; var v = b.getAttribute('data-val');
      if (v === q.answer) b.classList.add('correct'); else if (v === chosen) b.classList.add('wrong');
    });
    var last = state.i >= state.qs.length - 1, fb = document.getElementById('quiz-feedback');
    var msg = got ? '<span class="quiz-ok">Correct.</span>'
                  : '<span class="quiz-no">Not quite — it’s <strong>' + esc(q.answer) + '</strong>.</span>';
    var link = '';
    if (q.src && q.src.slug) {
      var href = esc(q.src.slug) + '.html' + (q.src.q ? '?q=' + encodeURIComponent(String(q.src.q).slice(0, 42)) : '') + '#ch-' + (q.src.chapter || 0);
      link = '<a class="quiz-read" href="' + href + '">Read in context →</a>';
    }
    fb.innerHTML = '<p class="quiz-fb-msg">' + msg + ' ' + link + '</p>'
      + '<button class="quiz-btn quiz-next" type="button">' + (last ? 'See your score →' : 'Next question →') + '</button>';
    fb.hidden = false;
    var nx = fb.querySelector('.quiz-next');
    nx.addEventListener('click', function () { if (last) finish(); else { state.i++; renderQ(); } });
    try { nx.focus(); } catch (e) {}
  }

  function finish() {
    var n = state.qs.length, s = state.score, pct = Math.round(s / n * 100);
    try {
      var k = bestKey(state.sv), prev = JSON.parse(localStorage.getItem(k) || 'null');
      if (!prev || !prev.total || (s / n) > (prev.score / prev.total) || ((s / n) === (prev.score / prev.total) && n > prev.total))
        localStorage.setItem(k, JSON.stringify({ score: s, total: n }));
    } catch (e) {}
    var grade = pct >= 90 ? 'Scholar of the shelf.' : pct >= 70 ? 'Well read.' : pct >= 40 ? 'A page-turner in progress.' : 'Time for a re-read.';
    stage.innerHTML = '<div class="quiz-result"><p class="quiz-result-k">Your result</p>'
      + '<p class="quiz-score-big">' + s + '<span>/' + n + '</span></p>'
      + '<p class="quiz-grade">' + esc(grade) + '</p>'
      + '<p class="quiz-result-sub">on ' + esc(state.label) + ' · ' + esc(state.lv) + '</p>'
      + '<div class="quiz-result-actions"><button class="quiz-btn quiz-again" type="button">Play again</button>'
      + '<button class="quiz-btn quiz-share" type="button">Share result ↗</button></div></div>';
    showBest(state.sv);
    wireAgain();
    var sh = stage.querySelector('.quiz-share');
    if (sh) sh.addEventListener('click', function () {
      if (!window.CorpusShare) return;
      window.CorpusShare.open({ kicker: 'Test Yourself', quote: s + ' / ' + n + ' — ' + grade,
        source: 'The ' + state.lv + ' test on ' + state.label + ' · calvincollins.xyz', filename: 'quiz-result',
        shareText: 'I scored ' + s + '/' + n + ' on the ' + state.lv + ' test on ' + state.label + '.' });
    });
  }

  function wireAgain() {
    var a = stage.querySelector('.quiz-again');
    if (a) a.addEventListener('click', function () { stage.hidden = true; stage.innerHTML = ''; setup.hidden = false; showBest(scope.value); });
  }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
"""

# Static scaffold for the quiz page's card; QUIZ_JS populates the <select>, wires
# the level pills, and runs the round. Hidden until the script confirms there are
# ≥2 corpora to build distractors from.
QUIZ_CARD_HTML = (
    '<section class="quiz" id="quiz" hidden>'
    '<div class="quiz-card">'
    '<div class="quiz-setup" id="quiz-setup">'
    '<label class="quiz-field"><span class="quiz-label">Test me on</span>'
    '<select id="quiz-scope" class="quiz-select" aria-label="Choose what to be tested on"></select></label>'
    '<div class="quiz-field quiz-field-lv"><span class="quiz-label">Challenge</span>'
    '<div class="quiz-levels" role="group" aria-label="Choose a difficulty">'
    '<button type="button" class="quiz-level" data-level="easy">Easy</button>'
    '<button type="button" class="quiz-level is-on" data-level="medium">Medium</button>'
    '<button type="button" class="quiz-level" data-level="hard">Hard</button>'
    '</div></div>'
    '<button id="quiz-start" class="quiz-btn quiz-btn-go" type="button">Begin the test →</button>'
    '<p class="quiz-best" id="quiz-best" hidden></p>'
    '</div>'
    '<div class="quiz-stage" id="quiz-stage" hidden></div>'
    '</div></section>'
)


# A full embedded quiz section (heading + intro + the level-aware card) for
# pages that host the quiz inline — the detached desks. quiz.html itself uses
# the bare card (its page header replaces the h2/intro).
QUIZ_SECTION_HTML = (
    '<section class="quiz" id="quiz" hidden>'
    '<h2 class="quiz-h">Test Yourself</h2>'
    '<p class="quiz-intro">A short quiz on what this desk\'s research actually says — the dates, '
    'the people, the terms of art. Pick a corpus and a challenge level; every missed answer '
    'links back to the chapter that has it, and your best scores stay on this device.</p>'
    + QUIZ_CARD_HTML.replace('<section class="quiz" id="quiz" hidden>', '', 1)
)


def quiz_section_scoped(ns="", all_label="", all_name=""):
    """The quiz scaffold with scope attributes for a detached desk page: `ns`
    namespaces the localStorage best-score ledger; the labels rename the
    "whole library" option/result line. Pair with a #quiz-slugs payload."""
    attrs = "".join(
        f' data-{k}="{html.escape(v, quote=True)}"'
        for k, v in (("ns", ns), ("all-label", all_label), ("all-name", all_name)) if v
    )
    return QUIZ_SECTION_HTML.replace(
        '<section class="quiz" id="quiz" hidden>',
        f'<section class="quiz" id="quiz" hidden{attrs}>', 1)


# The index keeps only an invitation band; the quiz itself lives on quiz.html.
QUIZ_CTA_HTML = (
    '<section class="quiz quiz-cta">'
    '<h2 class="quiz-h">Test Yourself</h2>'
    '<p class="quiz-intro">A quiz on what the research actually says — the dates, the people, '
    'the terms of art. Three challenge levels, on any single corpus or the whole shelf at once; '
    'every missed answer links back to the chapter that has it.</p>'
    '<p class="quiz-cta-row"><a class="quiz-btn quiz-btn-go" href="quiz.html">Take the quiz →</a></p>'
    '</section>'
)

LIBRARY_CSS = """
:root {
  /* paper + ink */
  --bg: #fcfbf7;            /* warm paper white — the room */
  --panel: #f4f1e8;         /* mat board — sidebar, code, input wells, atlas landmass, figure plates ONLY */
  --text: #1e1b16;          /* letterpress ink */
  --muted: #6e6759;         /* pencil annotation */
  --accent: #9a2c1a;        /* collection-stamp red */
  --border: #d8d3c4;        /* the engraved hairline — load-bearing site-wide */
  --mark: #f4e7ad;          /* reading-slip highlight */
  --cover-bg: #f6f2e7;      /* plate paper */
  /* plate inks (the old trencadís slots): carmine, ochre, Prussian, verdigris, aubergine */
  --t1: #9a2c1a; --t2: #a3771c; --t3: #274d68; --t4: #4a6350; --t5: #64405a;
  /* elevation: flat by decree; only floating overlays get shadow-2/-3 */
  --shadow-1: 0 1px 0 rgba(30,27,22,.06);
  --shadow-2: 0 2px 10px rgba(30,27,22,.08);
  --shadow-3: 0 18px 44px rgba(30,27,22,.16);
  /* motion: one curve, no springs (token kept for consumers; bounce removed) */
  --ease: cubic-bezier(.25,.1,.25,1);
  --ease-spring: cubic-bezier(.25,.1,.25,1);
  /* focus: double ring — visible on any themed ground */
  --ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent);
  /* type */
  --display: Baskerville, 'Hoefler Text', 'Palatino Linotype', 'Book Antiqua', Cambria, Georgia, serif;
  --serif: Charter, 'Bitstream Charter', 'Iowan Old Style', Georgia, 'Times New Roman', serif;
  --sans: system-ui, -apple-system, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  --mono: ui-monospace, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
}
[data-theme="dark"] {
  /* the reading room after hours — lamplit ink on slate, flat, no glow */
  --bg: #181512;
  --panel: #211e19;
  --text: #eae6db;
  --muted: #9b9384;
  --accent: #d98055;        /* beside the #d98f5f the 30 dark theme blocks were tuned against */
  --border: #3a362f;
  --mark: #5a4a1d;
  --cover-bg: #232019;
  --t1: #d0704f; --t2: #cfa93e; --t3: #6e98b1; --t4: #8ba576; --t5: #a97b9c;
  --shadow-1: 0 1px 0 rgba(0,0,0,.35);
  --shadow-2: 0 2px 12px rgba(0,0,0,.45);
  --shadow-3: 0 18px 48px rgba(0,0,0,.60);
}
* { box-sizing: border-box; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--serif);
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; text-rendering: optimizeLegibility; }
.top-header-art { width: 100%; background: #0f0e0c; border-bottom: 1px solid var(--text); overflow: hidden; }
.top-header-img { width: 100%; height: clamp(140px, 18vw, 260px); object-fit: cover; object-position: center;
  display: block; filter: saturate(.9) contrast(1.02); }
.masthead { max-width: 1240px; margin: 0 auto; padding: .8rem 2rem; display: flex;
  justify-content: space-between; align-items: center; gap: 1.4rem; font-family: var(--sans); font-size: .66rem;
  color: var(--muted); text-transform: uppercase; letter-spacing: .1em; border-bottom: 1px solid var(--text); }
.mh-brand { display: inline-flex; align-items: center; gap: .55rem; line-height: 1.25; color: var(--muted);
  text-decoration: none; transition: color .2s ease; }
.mh-brand:hover, .mh-brand:focus-visible { color: var(--accent); }
.mh-brand:focus-visible { outline: 1px solid var(--accent); outline-offset: 4px; }
.mh-logo { width: 30px; height: 30px; border-radius: 50%; object-fit: cover; background: #0f0e0c;
  border: 1px solid var(--border); box-shadow: var(--shadow-1); flex: 0 0 auto;
  transition: transform .22s var(--ease), box-shadow .22s var(--ease); }
.mh-brand:hover .mh-logo, .mh-brand:focus-visible .mh-logo {
  transform: rotate(-7deg) scale(1.08); box-shadow: var(--shadow-2); }
/* Nav items break between links, never inside one — "AD TECH" and "THE GHOST OF
   TIMES" used to split across two lines and leave the row ragged. */
.mh-nav { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .45rem 1.15rem; }
.mh-brand { white-space: nowrap; }
.mh-nav a { color: var(--muted); text-decoration: none; padding-bottom: 2px; position: relative;
  white-space: nowrap; transition: color .2s ease; }
.mh-nav a::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 1.5px; background: var(--accent);
  transform: scaleX(0); transform-origin: right; transition: transform .18s var(--ease); }
.mh-nav a:hover { color: var(--accent); }
.mh-nav a:hover::after { transform: scaleX(1); transform-origin: left; }
.mh-nav a.active { color: var(--text); }
.mh-nav a.active::after { transform: scaleX(1); }
.section-title { max-width: 1120px; margin: 1.6rem auto .2rem; padding: 0 2rem; font-family: var(--display);
  font-weight: 600; font-size: 1.9rem; scroll-margin-top: 1rem; }
.section-title::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
/* Collections — the flagship: curated cross-corpus reading arcs, shown as posters. */
.collections { max-width: 1120px; margin: 1.9rem auto 0; padding: 0 2rem; }
.coll-h { max-width: 1120px; font-family: var(--display); font-weight: 600; font-size: 1.9rem; margin: 0; }
.coll-h::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.coll-intro { font-family: var(--sans); font-size: .9rem; color: var(--muted); margin: .7rem 0 1.2rem; max-width: 42rem; line-height: 1.5; }
.coll-shelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(264px, 1fr)); gap: 1.4rem; }
.coll-card { display: flex; flex-direction: column; background: var(--bg); border: 1px solid var(--border);
  border-radius: 0; overflow: hidden; text-decoration: none; color: var(--text); box-shadow: none;
  position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.coll-card:hover, .coll-card:focus-visible { border-color: var(--text); transform: translateY(-2px);
  box-shadow: var(--shadow-2); z-index: 1; }
.coll-poster { position: relative; background: var(--cover-bg); border-bottom: 1px solid var(--border); overflow: hidden; }
.coll-poster svg { width: 100%; height: 138px; display: block; }
.coll-poster-title { position: absolute; z-index: 1; left: 10px; right: auto; bottom: 10px; max-width: calc(100% - 20px);
  font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .12em; line-height: 1.4; color: var(--text);
  background: color-mix(in srgb, var(--bg) 90%, transparent);
  border: 1px solid var(--border); padding: .3rem .5rem; text-shadow: none; }
.coll-body { padding: 1rem 1.2rem 1.15rem; display: flex; flex-direction: column; flex: 1; }
.coll-note { font-family: var(--sans); font-size: .82rem; color: var(--muted); line-height: 1.5; margin: 0 0 .9rem; flex: 1;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.coll-meta { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums;
  color: var(--muted); margin: 0; display: flex; align-items: center; justify-content: space-between; gap: .6rem; }
/* Today's Passage — the daily pull-quote, mirrored in a pair atop the index
   (see .mirror-head / .dp-pane in HOME_MIRROR_CSS). */
.dp-pane[hidden] { display: none; }
.dp-quote { display: block; position: relative; overflow: hidden; text-decoration: none; color: var(--text);
  background: var(--bg); border: 1px solid var(--border); border-top: 3px solid var(--accent);
  border-radius: 0; padding: 1.4rem 1.6rem 1.3rem; box-shadow: none;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.dp-quote:hover, .dp-quote:focus-visible { border-color: var(--text); border-top-color: var(--accent);
  transform: translateY(-2px); box-shadow: var(--shadow-2); z-index: 1; }
.dp-kicker { display: block; font-family: var(--sans); font-size: .68rem; text-transform: uppercase;
  letter-spacing: .18em; color: var(--accent); margin: 0 0 .55rem; }
.dp-mark { position: absolute; top: .2rem; right: 1.2rem; font-family: var(--display); font-size: 5rem;
  line-height: 1; color: var(--accent); opacity: .15; }
.dp-text { display: block; font-family: var(--display); font-style: italic; font-size: clamp(1.2rem, 2.5vw, 1.6rem);
  line-height: 1.36; margin: 0 0 .8rem; max-width: 46rem; }
.dp-src { display: block; font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0 0 1rem; }
.dp-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.dp-quote:hover .dp-cta, .dp-quote:focus-visible .dp-cta { background: var(--text); border-color: var(--text);
  color: var(--bg); transform: translateX(3px); }
.dp-share { position: absolute; right: 1.5rem; bottom: 1.25rem; z-index: 2; font-family: var(--sans); font-size: .7rem;
  text-transform: uppercase; letter-spacing: .05em; color: var(--muted); background: var(--bg);
  border: 1px solid var(--border); border-radius: 2px; box-shadow: var(--shadow-2); padding: .35rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
.dp-share:hover, .dp-share:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }
/* "For You" — the living, personalized discovery zone (rows of cloned cards). */
#foryou { max-width: 1120px; margin: 1.4rem auto 0; padding: 0 2rem; }
#foryou[hidden] { display: none; }
.fy-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .15rem; }
.fy-note { font-family: var(--sans); font-size: .82rem; color: var(--muted); margin: .2rem 0 .4rem; max-width: 40rem; line-height: 1.5; }
.fy-group { margin: 0 0 1.1rem; }
.fy-h { font-family: var(--display); font-weight: 600; font-size: 1.12rem; margin: .9rem 0 .7rem; }
.fy-h em { font-style: italic; color: var(--accent); }
.fy-row { display: flex; gap: 1.1rem; overflow-x: auto; padding: .2rem .1rem 1rem; scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.fy-row::-webkit-scrollbar { height: 8px; }
.fy-row::-webkit-scrollbar-thumb { background: var(--border); border-radius: 0; }
.fy-row::-webkit-scrollbar-track { background: transparent; }
.fy-row .card { width: 264px; flex: 0 0 264px; scroll-snap-align: start; border: 1px solid var(--border); }
@media (max-width: 560px) { .fy-row .card { width: 78vw; flex-basis: 78vw; } #foryou { padding: 0 1.2rem; } }
/* The Ghost of Times announcement row — first entry in the ruled announcements column. */
.ghost-band { max-width: 1120px; margin: 0 auto; padding: 0 2rem; }
.ghost-band a { display: grid; grid-template-columns: auto minmax(118px, 142px) 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: var(--text); background: transparent; border: 0; border-top: 1px solid var(--border);
  border-radius: 0; box-shadow: none; padding: 1.3rem 0 1.4rem; position: relative; }
.ghost-band a:hover .gb-lead { color: var(--accent); }
.ghost-band .gb-flag { font-family: var(--display); font-weight: 600; font-size: 1.6rem; line-height: 1; color: var(--text);
  border-right: 1px solid var(--border); padding-right: 1.6rem; }
.ghost-band .gb-flag small { display: block; font-family: var(--serif); font-style: italic; font-size: .72rem;
  letter-spacing: normal; text-transform: none; color: var(--muted); margin-top: .5rem; }
.gb-flag .gb-nib { display: block; color: var(--accent); opacity: .8; margin-top: .3rem;
  transition: opacity .16s var(--ease); }
.ghost-band a:hover .gb-nib, .ghost-band a:focus-visible .gb-nib { opacity: 1; }
.gb-flag .gb-nib svg { width: 54px; height: 17px; }
.ghost-band .gb-mid .gb-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .35rem; }
.ghost-band .gb-mid .gb-lead { font-family: var(--display); font-weight: 600; font-size: 1.15rem; line-height: 1.28; margin: 0 0 .3rem; color: var(--text); }
.ghost-band .gb-mid .gb-sub { font-family: var(--sans); font-size: .82rem; line-height: 1.45; color: var(--muted); margin: 0; }
.ghost-band .gb-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.ghost-band a:hover .gb-cta, .ghost-band a:focus-visible .gb-cta { background: var(--text); border-color: var(--text);
  color: var(--bg); transform: translateX(3px); }
@media (max-width: 680px) {
  .ghost-band a { grid-template-columns: 1fr; gap: .9rem; }
  .ghost-band .gb-flag { border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 .9rem; }
  .ghost-band .scene-plate { max-width: 320px; }
  .masthead { flex-direction: column; align-items: flex-start; gap: .5rem; letter-spacing: .09em; }
  .mh-nav { flex-wrap: wrap; gap: .4rem 1.1rem; }
}
header { max-width: 1120px; margin: 0 auto; padding: 2.6rem 2rem 1rem; display: flex;
  align-items: center; gap: 2.5rem; }
.hero-text { flex: 1.1; }
.hero-art { flex: .55; min-width: 0; color: var(--text); display: flex; justify-content: center; }
.hero-art svg { width: 100%; height: auto; display: block; }
/* :not(.mascot-img) so the square-plate radius doesn't out-specify the mascot's
   circular crop below. */
.hero-art .hero-img:not(.mascot-img) { width: 100%; height: auto; display: block; border-radius: 10px; }
/* The mascot crop travels with the image, not with .hero-art — the section
   agents live inside .agent-chip on the desk fronts and were rendering as
   hard-edged cream squares on a dark page. */
.mascot-img { aspect-ratio: 1; object-fit: cover; border-radius: 50%;
  background: transparent; border: 0; box-shadow: var(--shadow-2); }
.hero-art .mascot-img { width: min(100%, 285px);
  animation: mhAgentFloat 6.5s ease-in-out infinite; transform-origin: 50% 58%; }
.hero-cta { display: flex; flex-wrap: wrap; gap: .7rem; margin: 1.5rem 0 0; }
.hero-cta-btn { font-family: var(--sans); font-size: .74rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; text-decoration: none; color: var(--text); border: 1px solid var(--border);
  padding: .78rem 1.25rem; white-space: nowrap;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.hero-cta-btn:hover, .hero-cta-btn:focus-visible { border-color: var(--text); transform: translateY(-2px); }
.hero-cta-btn.primary { background: var(--text); color: var(--bg); border-color: var(--text); }
.hero-cta-btn.primary:hover, .hero-cta-btn.primary:focus-visible {
  background: var(--accent); border-color: var(--accent); color: var(--bg); }
.agent-chip { display: inline-flex; flex-direction: column; align-items: center; gap: .7rem; }
.agent-chip .agent-portrait { width: min(100%, 285px); height: auto;
  animation: mhAgentFloat 7.5s ease-in-out infinite; transform-origin: 50% 58%; }
.agent-chip-label { font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; padding: .42rem .78rem; border: 1px solid currentColor; background: var(--bg); }
.agent-chip.agent-library { color: #9a2c1a; }
.agent-chip.agent-adtech { color: #0d5b68; }
[data-theme="dark"] .agent-chip.agent-library { color: #d98055; }
[data-theme="dark"] .agent-chip.agent-adtech { color: #62aab8; }
.agent-chip.compact { align-items: center; gap: .6rem; }
.agent-chip.compact .agent-portrait { width: 96px; box-shadow: var(--shadow-1); }
.agent-chip.compact .agent-chip-label { font-size: .58rem; letter-spacing: .12em; padding: .34rem .56rem; }
@keyframes mhAgentFloat {
  0%, 100% { transform: translateY(0) rotate(-1deg); filter: saturate(1.02) brightness(1); }
  45% { transform: translateY(-8px) rotate(1deg); filter: saturate(1.1) brightness(1.035); }
}
[data-theme="dark"] .hero-art .hero-core { fill: #d98055; }
[data-theme="dark"] .hero-art .hero-ground { fill: #eae6db; }
.kicker { font-family: var(--sans); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .18em; color: var(--accent); margin: 0 0 .6rem; }
.kicker::before { content: ""; display: inline-block; width: 26px; height: 8px; background: var(--accent);
  margin-right: .6rem; vertical-align: baseline; }
header h1 { font-family: var(--display); font-weight: 600; font-size: clamp(2.8rem, 7vw, 4.8rem);
  line-height: .96; margin: 0 0 .7rem; text-transform: uppercase; letter-spacing: .05em;
  border-top: 3px solid var(--text); border-bottom: 1px solid var(--text); padding: .9rem 0 .8rem; }
.tagline { color: var(--muted); margin: 0 0 1.1rem; font-family: var(--sans); font-size: .72rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: .14em; line-height: 1.5; max-width: none; }
.stats { font-family: var(--mono); font-size: .68rem; font-weight: 500; color: var(--muted); margin: 0;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; }
.library { max-width: 1120px; margin: 0 auto; padding: 0 0 3rem; }
/* Library toolbar — search box + category filter pills. */
.lib-toolbar { padding: 1.2rem 2rem .4rem; display: flex; flex-wrap: wrap; align-items: center;
  gap: .9rem 1.4rem; }
.lib-search { flex: 1 1 240px; min-width: 0; max-width: 360px; font-family: var(--sans); font-size: .95rem;
  color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 2px;
  padding: .62rem .95rem; -webkit-appearance: none; appearance: none; }
.lib-search:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.lib-search::placeholder { color: var(--muted); }
.cat-pills { display: flex; flex-wrap: wrap; gap: .5rem; }
.cat-pill { font-family: var(--sans); font-size: .7rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .1em; color: var(--muted);
  background: transparent; border: 1px solid var(--border); border-radius: 0; padding: .45rem .85rem;
  cursor: pointer; transition: color .15s var(--ease), border-color .15s var(--ease), background-color .15s var(--ease); }
.cat-pill:hover { color: var(--text); border-color: var(--text); background: var(--panel); }
.cat-pill.active { color: var(--bg); background: var(--text); border-color: var(--text); box-shadow: none; }
.cat-pill .cat-count { opacity: .65; font-size: .9em; }
.domain-group { padding-top: .4rem; }
.domain-group[hidden] { display: none; }
.domain-heading { max-width: 100%; margin: 2.2rem 0 .2rem; padding: 0 2rem .6rem;
  font-family: var(--sans); font-size: .95rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .22em; color: var(--text);
  display: flex; align-items: baseline; gap: .6rem; border-bottom: 2px solid var(--text); }
.domain-heading::before { content: ""; width: 10px; height: 10px; background: var(--accent); flex: none; align-self: center; }
.domain-heading .domain-count { font-family: var(--mono); font-size: .68rem; font-weight: 500; color: var(--muted);
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; }
.cat-section { padding-top: .6rem; }
.cat-section[hidden] { display: none; }
.cat-heading { max-width: 100%; margin: 1.2rem 0 0; padding: 0 2rem; font-family: var(--sans);
  font-size: .74rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em;
  color: var(--text); display: flex; align-items: baseline; gap: .5rem; }
.cat-heading .cat-count { font-family: var(--mono); font-size: .68rem; font-weight: 500; color: var(--muted);
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; }
.lib-empty { font-family: var(--sans); color: var(--muted); text-align: center; padding: 2.5rem 2rem; margin: 0; }
.grid { padding: 1.1rem 2rem 1.4rem; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1px; }
.card { display: flex; flex-direction: column; background: var(--bg);
  border: 0; outline: 1px solid var(--border); outline-offset: 0;
  border-radius: 0; box-shadow: none; overflow: hidden;
  text-decoration: none; color: var(--text); position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.card:hover, .card:focus-visible { outline-color: var(--text); transform: translateY(-2px);
  box-shadow: var(--shadow-2); z-index: 1; }
.card:hover h2, .card:focus-visible h2 { color: var(--accent); }
.cover { background: var(--cover-bg); border-bottom: 1px solid var(--border); overflow: hidden; }
.cover svg { width: 100%; height: 124px; display: block; }
/* Photo covers crop to the card band at any source size/aspect ratio — color at rest. */
.cover .cover-photo { width: 100%; height: 124px; object-fit: cover; object-position: center; display: block;
  filter: saturate(.85) contrast(1.02); transition: filter .18s var(--ease); }
.card:hover .cover-photo, .card:focus-visible .cover-photo { filter: saturate(1.08) contrast(1.04); }
.card-body { padding: 1.05rem 1.2rem 1.1rem; display: flex; flex-direction: column; flex: 1; }
.card h2 { font-family: var(--display); font-weight: 600; font-size: 1.3rem; line-height: 1.25; margin: 0 0 .4rem; }
.card .sub { color: var(--muted); font-size: .84rem; font-family: var(--sans);
  margin: 0 0 .9rem; line-height: 1.45; flex: 1;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card .meta { color: var(--muted); font-family: var(--mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; margin: 0; }
.card .meta::before { content: ""; display: inline-block; width: 8px; height: 8px;
  background: var(--accent); margin-right: .5rem; vertical-align: baseline; }
footer { max-width: 1120px; margin: 0 auto; padding: 1rem 2rem 3rem; border-top: 2px solid var(--text); }
.tiles { display: flex; gap: 3px; margin: 1.2rem 0 .9rem; }
.tiles span { width: 9px; height: 9px; border-radius: 0; transform: none; box-shadow: none; }
.tiles span:nth-child(1) { background: var(--t1); }
.tiles span:nth-child(2) { background: var(--t2); }
.tiles span:nth-child(3) { background: var(--t3); }
.tiles span:nth-child(4) { background: var(--t4); }
.epigraph { font-style: italic; color: var(--muted); margin: 0 0 .3rem; font-size: .92rem; }
.colophon { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0; }
.colophon .mh-stamp { display: inline-block; padding: .3em .65em; border: 1.5px solid var(--accent);
  color: var(--accent); font-family: var(--mono); font-size: .68rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase; transform: rotate(-1.5deg);
  opacity: .92; border-radius: 1px; margin: 0 0 .7rem; }
.colophon.marc { line-height: 1.9; }
.colophon.marc .marc-l { display: block; }
.colophon.marc .marc-l b { color: var(--accent); font-weight: 500; margin-right: .8rem; }
@media (max-width: 760px) {
  header { flex-direction: column-reverse; gap: 1.2rem; padding-top: 1.6rem; }
  .hero-art { width: 100%; }
  .top-header-img { height: 150px; }
  .hero-art .mascot-img { width: min(72vw, 270px); }
}
@media (prefers-reduced-motion: reduce) {
  /* every selector this constant transforms or draws, rest AND hover: */
  .card, .card:hover, .card:focus-visible,
  .coll-card, .coll-card:hover, .coll-card:focus-visible,
  .dp-quote, .dp-quote:hover, .dp-quote:focus-visible,
  .dp-cta, .dp-quote:hover .dp-cta, .dp-quote:focus-visible .dp-cta,
  .dp-share, .dp-share:hover, .dp-share:focus-visible,
  .ghost-band .gb-cta, .ghost-band a:hover .gb-cta, .ghost-band a:focus-visible .gb-cta,
  .gb-flag .gb-nib, .ghost-band a:hover .gb-nib, .ghost-band a:focus-visible .gb-nib,
  .mh-logo, .mh-brand:hover .mh-logo, .mh-brand:focus-visible .mh-logo,
  .cover .cover-photo, .card:hover .cover-photo, .card:focus-visible .cover-photo {
    transform: none !important;
    transition-duration: .01ms !important;
  }
  .hero-art .mascot-img, .agent-chip .agent-portrait { animation: none !important; }
  /* the nav underline keeps its scaleX states (transform none would show it at rest) — travel only is killed: */
  .mh-nav a::after, .mh-nav a:hover::after, .mh-nav a.active::after {
    transition-duration: .01ms !important;
  }
}
"""

# ---------------------------------------------------------------- "Test Yourself"
# The self-quiz's styles. The quiz lives on its own page (quiz.html, built by
# build_quiz_page) and is reached from the masthead, the ⌘K palette, an index
# invitation band, and each corpus reader's sidebar (quiz.html?on=<slug>). Built
# entirely from data the page carries — the inlined #quiz-data (per-corpus
# content facts: glossary terms, key dates, key people), #library-manifest
# (corpora + chapter titles + categories, via the shared shell) and
# #passages-data (pull-quotes tagged with corpus + chapter) — so every answer is
# ground-truth and nothing is fetched or generated. Two scopes (a single corpus
# or the whole shelf) × three challenge levels.
QUIZ_CSS = """
.quiz { max-width: 1120px; margin: 2.2rem auto 0; padding: 0 2rem; }
.quiz[hidden] { display: none; }
.quiz-h { font-family: var(--display); font-size: 1.9rem; margin: 0; }
.quiz-h::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.quiz-intro { font-family: var(--sans); font-size: .9rem; color: var(--muted); margin: .7rem 0 1.2rem; max-width: 44rem; line-height: 1.55; }
.quiz-card { background: transparent; border: 1px solid var(--border); border-radius: 0; padding: 1.6rem 1.8rem; box-shadow: none; }
.quiz-setup { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 1rem 1.4rem; }
.quiz-setup[hidden] { display: none; }  /* the flex rule would otherwise beat the UA [hidden] style */
.quiz-field { display: flex; flex-direction: column; gap: .4rem; flex: 1 1 260px; min-width: 0; }
.quiz-label { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); }
.quiz-select { font-family: var(--sans); font-size: .92rem; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 2px; padding: .6rem .8rem; -webkit-appearance: none; appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
  background-position: calc(100% - 18px) 1.15em, calc(100% - 13px) 1.15em; background-size: 5px 5px; background-repeat: no-repeat; padding-right: 2.2rem; }
.quiz-select:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.quiz-btn { font-family: var(--sans); font-size: .8rem; letter-spacing: .04em; cursor: pointer; border-radius: 2px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text); padding: .65rem 1.2rem;
  transition: border-color .15s var(--ease), color .15s var(--ease), background-color .15s var(--ease); }
.quiz-btn:hover { border-color: var(--accent); color: var(--accent); }
.quiz-btn-go { background: var(--text); color: var(--bg); border: 1px solid var(--text); border-radius: 2px;
  font-family: var(--sans); font-size: .78rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .1em; cursor: pointer;
  box-shadow: none; transition: background-color .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
.quiz-btn-go:hover, .quiz-btn-go:focus-visible { background: var(--accent); border-color: var(--accent); color: var(--bg);
  transform: translateY(-1px); box-shadow: var(--shadow-2); }
.quiz-btn-go:active { transform: translateY(0); box-shadow: none; }
.quiz-best { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0; flex-basis: 100%; }
.quiz-progress { display: flex; justify-content: space-between; align-items: baseline; font-family: var(--sans);
  font-size: .68rem; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); margin: 0 0 .5rem; }
.quiz-progress .quiz-score { color: var(--accent); }
.quiz-bar { height: 4px; background: var(--border); border-radius: 0; overflow: hidden; margin: 0 0 1.2rem; }
.quiz-bar span { display: block; height: 100%; background: var(--accent); transition: width .35s ease; }
.quiz-q { font-family: var(--display); font-size: 1.22rem; line-height: 1.32; margin: 0 0 .9rem; }
.quiz-q em { font-style: italic; color: var(--accent); }
.quiz-passage { font-family: var(--display); font-style: italic; font-size: 1.08rem; line-height: 1.45; color: var(--text);
  border-left: 3px solid var(--accent); margin: 0 0 1.1rem; padding: .2rem 0 .2rem 1.1rem; }
.quiz-opts { display: grid; gap: .6rem; }
.quiz-opt { text-align: left; font-family: var(--sans); font-size: .92rem; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 0; padding: .8rem 1rem .8rem 1.15rem; cursor: pointer;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-repeat: no-repeat; background-size: 3px 0%; background-position: 0 0;
  transition: background-size .18s var(--ease), background-color .15s var(--ease),
    border-color .15s var(--ease), color .15s var(--ease); }
.quiz-opt:hover:not(:disabled), .quiz-opt:focus-visible:not(:disabled) { border-color: var(--text); background-size: 3px 100%; }
.quiz-opt:disabled { cursor: default; }
.quiz-opt.correct { border-color: #3f8a52; background: rgba(63,138,82,.12); color: var(--text); }
.quiz-opt.wrong { border-color: var(--t1); background: color-mix(in srgb, var(--t1) 12%, transparent); color: var(--text); }
.quiz-opt.correct::after { content: " ✓"; color: #3f8a52; font-weight: 700; }
.quiz-opt.wrong::after { content: " ✕"; color: var(--t1); font-weight: 700; }
.quiz-feedback { margin: 1.1rem 0 0; }
.quiz-feedback[hidden] { display: none; }
.quiz-fb-msg { font-family: var(--sans); font-size: .88rem; line-height: 1.5; margin: 0 0 .9rem; }
.quiz-ok { color: #3f8a52; font-weight: 600; }
.quiz-no { color: var(--muted); }
.quiz-no strong { color: var(--text); }
.quiz-read { font-family: var(--sans); font-size: .8rem; color: var(--accent); text-decoration: none;
  padding-bottom: 2px; margin-left: .3rem;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-size: 0% 1.5px; background-repeat: no-repeat; background-position: 0 100%;
  transition: background-size .18s var(--ease), color .14s var(--ease); }
.quiz-read:hover, .quiz-read:focus-visible { background-size: 100% 1.5px; color: var(--accent); }
.quiz-next { background: var(--text); color: var(--bg); border: 1px solid var(--text); border-radius: 2px;
  font-family: var(--sans); font-size: .78rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .1em; cursor: pointer;
  box-shadow: none; transition: background-color .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
.quiz-next:hover, .quiz-next:focus-visible { background: var(--accent); border-color: var(--accent); color: var(--bg);
  transform: translateY(-1px); box-shadow: var(--shadow-2); }
.quiz-next:active { transform: translateY(0); box-shadow: none; }
.quiz-empty { font-family: var(--sans); color: var(--muted); margin: 0; }
.quiz-result { text-align: center; padding: .6rem 0; }
.quiz-result-k { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .5rem; }
.quiz-score-big { font-family: var(--display); font-weight: 600; font-size: clamp(3rem, 9vw, 5rem); line-height: 1; margin: 0; font-variant-numeric: oldstyle-nums; }
.quiz-score-big span { color: var(--muted); font-weight: 400; font-size: .42em; }
.quiz-grade { font-family: var(--display); font-style: italic; font-size: 1.25rem; margin: .5rem 0 .2rem; }
.quiz-result-sub { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0 0 1.4rem; }
.quiz-result-actions { display: flex; gap: .8rem; justify-content: center; flex-wrap: wrap; }
/* challenge-level pills (quiz page) */
.quiz-field-lv { flex: 0 0 auto; }
.quiz-levels { display: inline-flex; border: 1px solid var(--border); border-radius: 2px; overflow: hidden; background: var(--bg); }
.quiz-level { font-family: var(--sans); font-size: .8rem; letter-spacing: .03em; color: var(--muted);
  background: none; border: none; padding: .55rem .95rem; cursor: pointer; transition: color .15s ease, background .15s ease; }
.quiz-level + .quiz-level { border-left: 1px solid var(--border); }
.quiz-level:hover:not(.is-on) { color: var(--accent); }
.quiz-level.is-on { background: var(--accent); color: var(--bg); }
/* index invitation band (the quiz itself lives on quiz.html) */
a.quiz-btn { text-decoration: none; display: inline-block; }
.quiz-cta-row { margin: 0; }
@media (max-width: 560px) { .quiz { padding: 0 1.2rem; } .quiz-card { padding: 1.3rem 1.2rem; } }
@media (prefers-reduced-motion: reduce) {
  .quiz-opt, .quiz-opt:hover:not(:disabled), .quiz-opt:focus-visible:not(:disabled),
  .quiz-btn-go, .quiz-btn-go:hover, .quiz-btn-go:focus-visible, .quiz-btn-go:active,
  .quiz-next, .quiz-next:hover, .quiz-next:focus-visible, .quiz-next:active,
  .quiz-read, .quiz-read:hover, .quiz-read:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
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
            f'<span class="gb-nib" aria-hidden="true">{GHOST_NIB}</span>'
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
    scene_seed = f"ghost-band:{editions[0].get('date', '') if editions else 'empty'}"
    scene = scene_plate("ghost", extra_class="band-scene", seed=scene_seed)
    return (f'<div class="ghost-band"><a href="ghost.html">{flag}{scene}{mid}'
            f'<span class="gb-cta">{cta}</span></a></div>')


GHOST_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>The Ghost of Times — calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="ghost-plate">
  <p class="gp-kicker">A paper of writer-voiced op-eds</p>
  <h1 class="gp-name">The Ghost of Times</h1>
  <div class="gp-ornament" aria-hidden="true">{ornament}</div>
  <p class="gp-motto">“{motto}”</p>
  {scene}
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
  <p class="colophon"><a href="research.html">← Back to the Research Library</a></p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

# A fountain-pen nib, drawn as stroke-only line art (a sibling to the Fingerprint's
# broadcast ridges) — the nearest the Ghost's "writer-voiced op-eds" register has
# to a sigil, and the ornament that replaces the old raster masthead engraving.
GHOST_NIB = (
    '<svg viewBox="0 0 240 72" xmlns="http://www.w3.org/2000/svg" class="gp-nib-svg">'
    '<g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M 100 14 Q 120 7 140 14 Q 134 36 120 60 Q 106 36 100 14 Z"/>'
    '<circle cx="120" cy="26" r="4.2"/>'
    '<line x1="120" y1="30.2" x2="120" y2="55"/>'
    '</g></svg>'
)

GHOST_PAGE_CSS = """
/* The Ghost of Times — a newspaper section front, sharing the editions'
   warm-paper / serif / double-rule / stamp-red language. */
.ghost-plate { display: block; max-width: 820px; margin: 1.8rem auto 0; padding: 0 2rem; text-align: center; }
.gp-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .5rem; }
.gp-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.4rem, 6vw, 4rem);
  line-height: .98; letter-spacing: -.01em; margin: 0 0 .3rem; }
.gp-ornament { color: var(--accent); opacity: .85; margin: 0 0 .6rem; }
.gp-nib-svg { width: 180px; height: 54px; }
.gp-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.gp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--text); }
.gp-folio .gp-folio-c { color: var(--accent); font-weight: 600; }

/* featured (latest) edition — reads like a section lead */
.ged-feature { max-width: 720px; margin: 2.1rem auto 0; padding: 0 2rem; }
.ged-feature a { display: block; text-decoration: none; color: var(--text); }
.gedf-meta { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .7rem; }
.gedf-head { font-family: var(--display); font-weight: 600; font-size: clamp(1.9rem, 4.2vw, 2.7rem);
  line-height: 1.07; letter-spacing: -.01em; margin: 0 0 .7rem; transition: color .15s ease; }
.ged-feature a:hover .gedf-head { color: var(--accent); }
.gedf-dek { font-family: var(--serif); font-style: italic; font-size: 1.2rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 .9rem; }
.gedf-writers { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--accent); margin: 0 0 1.1rem; }
.gedf-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.ged-feature a:hover .gedf-cta, .ged-feature a:focus-visible .gedf-cta {
  background: var(--text); border-color: var(--text); color: var(--bg); transform: translateX(3px); }

/* back issues — newspaper archive rows */
.ged-issues { max-width: 720px; margin: 2.8rem auto 0; padding: 0 2rem 1rem; }
.ged-issues-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .3rem; }
.ged-row { display: grid; grid-template-columns: auto 1fr auto; gap: 1.2rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .95rem 0; border-bottom: 1px solid var(--border);
  position: relative; padding-right: 1.6rem; }
.ged-row::after { content: "→"; position: absolute; right: 0; top: 50%;
  margin-top: -.6em; font-family: var(--sans); font-size: .9rem; line-height: 1;
  color: var(--accent);
  opacity: 0; transform: translateX(-4px);
  transition: opacity .16s var(--ease), transform .16s var(--ease); }
.ged-row:hover::after, .ged-row:focus-visible::after { opacity: 1; transform: translateX(0); }
.ged-row-no { font-family: var(--display); font-size: 1.05rem; color: var(--accent); white-space: nowrap;
  font-variant-numeric: oldstyle-nums; }
.ged-row-body { min-width: 0; }
.ged-row-head { font-family: var(--display); font-size: 1.18rem; line-height: 1.18; display: block; transition: color .15s ease; }
.ged-row:hover .ged-row-head { color: var(--accent); }
.ged-row-meta { display: flex; align-items: baseline; gap: .55rem; margin-top: .25rem;
  font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); }
.ged-row-meta::after { content: ""; flex: 1; min-width: 1.5rem; height: .7em;
  background-image: radial-gradient(circle, var(--border) 1px, transparent 1.2px);
  background-size: 6px 2px; background-repeat: repeat-x; background-position: 0 60%; }
.ged-row:hover .ged-row-meta::after, .ged-row:focus-visible .ged-row-meta::after {
  background-image: radial-gradient(circle, var(--accent) 1px, transparent 1.2px); }
.ged-row-date { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }

.ged-empty { max-width: 720px; margin: 2.1rem auto 0; padding: 2rem; text-align: center;
  color: var(--muted); font-family: var(--sans); font-size: .9rem; border-top: 3px double var(--border); }

.ghost-foot { max-width: 720px; margin: 3rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.ghost-foot .epigraph { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .95rem; margin: 0 0 .6rem; }
.ghost-foot .colophon { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; margin: 0; }
.ghost-foot .colophon a { color: var(--accent); text-decoration: none; }
.ghost-foot .colophon a:hover { text-decoration: underline; }

@media (max-width: 560px) {
  .gp-folio { font-size: .58rem; letter-spacing: .08em; }
  .ged-row { grid-template-columns: auto 1fr; gap: .8rem; padding-right: 0; }
  .ged-row::after { display: none; }
  .ged-row-date { display: none; }
  .ged-row-meta::after { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .gedf-cta,
  .ged-feature a:hover .gedf-cta, .ged-feature a:focus-visible .gedf-cta,
  .ged-row::after, .ged-row:hover::after, .ged-row:focus-visible::after {
    transform: none !important;
    transition-duration: .01ms !important;
  }
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
            f'{scene_plate("ghost", extra_class="feature-scene", seed=ed.get("file") or ed.get("date", ""))}'
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
    page = GHOST_PAGE_TEMPLATE.format(
        css=LIBRARY_CSS + SCENE_PLATE_CSS + GHOST_PAGE_CSS,
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(active="ghost.html"),
        ornament=GHOST_NIB,
        motto=html.escape(ghost_cfg.get("motto", "")),
        scene=scene_plate("ghost", extra_class="section-scene", seed="ghost-front"),
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
.gh-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .45rem; }
.gh-name { display: inline-block; font-family: var(--display); font-weight: 600;
  font-size: clamp(2.1rem, 5.2vw, 3.1rem); line-height: .98; letter-spacing: -.02em;
  color: var(--text); text-decoration: none; margin: 0 0 .9rem; }
.gh-name:hover { color: var(--accent); }
.gh-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .5rem 0;
  font-family: var(--mono); font-size: .64rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; }
.gh-folio .gh-folio-c { color: var(--accent); font-weight: 700; }

/* contents — every story + author, clickable to its piece */
.gh-contents { display: block; margin: 0 0 2.8rem; }
.gh-contents-h { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .2em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .1rem; }
.gh-toc-item { display: grid; grid-template-columns: 3.2rem 1fr; gap: .8rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .7rem .2rem; border-bottom: 1px solid var(--border);
  position: relative; padding-right: 1.6rem; }
.gh-toc-item:hover { background: var(--panel); }
.gh-toc-item::after { content: "→"; position: absolute; right: 0; top: 50%;
  margin-top: -.6em; font-family: var(--sans); font-size: .9rem; line-height: 1;
  color: var(--accent);
  opacity: 0; transform: translateX(-4px);
  transition: opacity .16s var(--ease), transform .16s var(--ease); }
.gh-toc-item:hover::after, .gh-toc-item:focus-visible::after { opacity: 1; transform: translateX(0); }
.gh-toc-no { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--accent);
  white-space: nowrap; padding-top: .15rem; }
.gh-toc-item.is-lead .gh-toc-no { font-weight: 700; }
.gh-toc-head { font-family: var(--display); font-size: 1.12rem; line-height: 1.2; display: block;
  transition: color .15s var(--ease); }
.gh-toc-item:hover .gh-toc-head { color: var(--accent); }
.gh-toc-by { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted);
  display: block; margin-top: .25rem; }

/* the op-ed stack */
.gh-piece { margin: 0; scroll-margin-top: 1.2rem; }
.gh-head { font-family: var(--display); font-weight: 600; letter-spacing: -.01em;
  line-height: 1.1; margin: 0 0 .55rem; }
.gh-piece .gh-head { font-size: clamp(1.55rem, 3.2vw, 2rem); }
.gh-lead .gh-head { font-size: clamp(2rem, 4.6vw, 2.9rem); line-height: 1.06; }
.gh-lead .gh-head::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.gh-dek { font-family: var(--serif); font-style: italic; color: var(--muted);
  font-size: 1.16rem; line-height: 1.42; margin: 0 0 .9rem; }
.gh-lead .gh-dek { font-size: 1.28rem; }
.gh-byline { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--accent);
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  padding: .4rem 0; margin: 0 0 1.3rem; }
/* objective-voice abstract — neutral framing of what the piece is about, set apart from the body */
.gh-summary { margin: 0 0 1.5rem; padding: .85rem 1.1rem; background: transparent;
  border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 0; }
.gh-summary-label { display: block; font-family: var(--sans); font-size: .64rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .18em; color: var(--muted); margin: 0 0 .4rem; }
.gh-summary-text { font-family: var(--sans); font-size: .9rem; line-height: 1.55; color: var(--text); margin: 0; }
.gh-lead .gh-summary-text { font-size: .96rem; }
.gh-body { font-size: 1.05rem; line-height: 1.74; }
.gh-body p { margin: 0 0 1.15rem; }
.gh-body a { color: var(--accent); }
.gh-body em { font-style: italic; }
.gh-body blockquote { margin: 1.4rem 0; padding: .15rem 0 .15rem 1.2rem; border-left: 2px solid var(--accent);
  background: transparent; border-radius: 0; color: var(--muted); font-style: italic; }
/* drop cap opens each piece */
.gh-body > p:first-of-type::first-letter { font-family: var(--display); font-weight: 600;
  float: left; font-size: 3.1em; line-height: .72; padding: .06em .1em 0 0; color: var(--accent); }
.gh-sources { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; line-height: 1.6; color: var(--muted);
  margin: 1.2rem 0 0; }
.gh-sources a { color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; }
.gh-sources a:hover { border-bottom-color: var(--accent); }
.gh-sources .filed { text-transform: uppercase; letter-spacing: .08em; font-size: .7rem; }
.gh-sep { border: none; height: 1em; margin: 2.6rem 0; text-align: center; }
.gh-sep::before { content: "◆ ◆ ◆"; color: var(--accent); opacity: .4; font-size: .68rem; letter-spacing: 1em; padding-left: 1em; }

/* "The Facts" — the unfiltered wire ledger, as a ruled artifact */
.gh-facts { margin: 3rem auto 0; padding: 1.5rem 1.6rem 1.2rem; background: transparent;
  border: 1px solid var(--border); border-radius: 0; }
.gh-facts-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .2em;
  color: var(--accent); margin: 0 0 1.1rem; padding-bottom: .55rem; border-bottom: 2px solid var(--text); }
.gh-facts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.3rem 1.8rem; }
.gh-fact-head { font-family: var(--display); font-size: 1rem; line-height: 1.25; margin: 0 0 .25rem; }
.gh-fact-by { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0 0 .5rem; }
.gh-fact ul { margin: 0; padding-left: 1.05rem; }
.gh-fact li { font-family: var(--sans); font-size: .82rem; line-height: 1.5; color: var(--text); margin: 0 0 .35rem; }
.gh-fact li::marker { color: var(--accent); }

/* foot */
.gh-foot { max-width: 760px; margin: 2.6rem auto 0; padding: 1.5rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.gh-watch { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .98rem; margin: 0 0 .7rem; }
.gh-colophon { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0; }
.gh-colophon a { color: var(--accent); text-decoration: none; }
.gh-colophon a:hover { text-decoration: underline; }

/* floating theme toggle, same affordance as the corpus reader */
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#theme-btn:hover, #theme-btn:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }

@media (max-width: 620px) {
  .gh-edition { padding: 1.6rem 1.3rem 1rem; }
  .gh-folio { font-size: .58rem; letter-spacing: .08em; }
  .gh-toc-item { padding-right: 0; }
  .gh-toc-item::after { display: none; }
  .gh-facts-grid { grid-template-columns: 1fr; gap: 1.1rem; }
}

@media (prefers-reduced-motion: reduce) {
  .gh-toc-item::after, .gh-toc-item:hover::after, .gh-toc-item:focus-visible::after,
  #theme-btn, #theme-btn:hover, #theme-btn:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

GHOST_EDITION_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="../index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<main class="gh-edition">
  <header class="gh-nameplate">
    <p class="gh-kicker">A paper of writer-voiced op-eds</p>
    <a class="gh-name" href="../ghost.html">The Ghost of Times</a>
    <div class="gh-folio">
      <span>{folio_no}</span>
      <span class="gh-folio-c">{folio_date}</span>
      <span>{folio_writers}</span>
    </div>
  </header>
  {scene}
  <nav id="gh-contents"></nav>
  <div id="gh-pieces"></div>
  <section id="gh-facts"></section>
  <footer class="gh-foot">
    {watch}
    <p class="gh-colophon"><a href="../ghost.html">← All editions</a> &nbsp;·&nbsp; <a href="../research.html">The Research Library</a></p>
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
// theme toggle — the theme itself is applied pre-paint by the <head> boot script
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

const ed = JSON.parse(document.getElementById('ghost-edition-data').textContent);
var noMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
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
      if (t) { t.scrollIntoView(noMotion ? { block: 'start' } : { behavior: 'smooth', block: 'start' });
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
        nav=main_nav_html(prefix="../", active="ghost.html"),
        css=LIBRARY_CSS + SCENE_PLATE_CSS + GHOST_EDITION_CSS,
        folio_no=html.escape(folio_no),
        folio_date=html.escape(folio_date),
        folio_writers=html.escape(folio_writers),
        scene=scene_plate("ghost", extra_class="edition-scene", root="../", seed=f"ghost-edition:{date}"),
        watch=watch_html,
        data_json=json_for_html(data),
        marked_js=MARKED_JS,
        app_js=GHOST_EDITION_JS,
        shell=shell,
    )
    (out / "ghost" / f"{date}-ghost-of-times.html").write_text(page)
    return True


# ---------------------------------------------------------------- the pamphlets
# A fourth top-level section: "The Pamphlets" — standalone, single-voice essays
# (Carlyle, and other writers to come) each taking up one question of the present
# moment. Unlike the Ghost (a daily, many-writer paper keyed to the news) a
# pamphlet is one sustained essay in one voice. Each is authored as a small data
# file — docs/pamphlets/data/{slug}.json, deposited alongside a one-line entry in
# docs/pamphlets/manifest.json — and rendered NATIVELY here, so it inherits the
# site's design system (warm-paper tokens, Iowan/Georgia, terracotta, dark mode,
# the masthead nav) for free and stays a few KB. Bodies are markdown, parsed
# client-side by the same marked.js the corpus reader uses. No PDF.
#
# Design register: the corpus reader's reading column wearing a letterpress
# pamphlet's coat — a centred measure, a compact "The Pamphlets" nameplate with
# the folio double-rule that echoes the section front, the site's 4-colour
# gradient bar under the title, a drop-cap lead, an italic "in the voice of"
# byline, and a colophon naming the voice. Unmistakably the same site.

PAMPHLETS_BAND_CSS = """
/* The Pamphlets announcement row on the home page — one line of the ruled
   announcements column, signed by the double accent letterpress rule at left. */
.pamphlet-band { max-width: 1120px; margin: 0 auto; padding: 0 2rem; }
.pamphlet-band a { display: grid; grid-template-columns: auto minmax(118px, 142px) 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: var(--text); background: transparent;
  border: 0; border-top: 1px solid var(--border); border-left: 6px double var(--accent);
  border-radius: 0; box-shadow: none; padding: 1.3rem 0 1.4rem 1.1rem; position: relative; }
.pamphlet-band .pb-flag { font-family: var(--display); font-weight: 600; font-size: 1.6rem; line-height: 1.02;
  color: var(--text); border-right: 1px solid var(--border); padding-right: 1.6rem; }
.pamphlet-band .pb-flag small { display: block; font-family: var(--serif); font-style: italic; font-weight: 400;
  font-size: .72rem; letter-spacing: .01em; color: var(--muted); margin-top: .55rem; }
.pamphlet-band .pb-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .35rem; }
.pamphlet-band .pb-lead { font-family: var(--display); font-weight: 600; font-size: 1.2rem; line-height: 1.28;
  margin: 0 0 .3rem; transition: color .15s var(--ease); }
.pamphlet-band a:hover .pb-lead, .pamphlet-band a:focus-visible .pb-lead { color: var(--accent); }
.pamphlet-band .pb-sub { font-family: var(--serif); font-style: italic; font-size: .9rem; line-height: 1.45; color: var(--muted); margin: 0; }
.pamphlet-band .pb-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.pamphlet-band a:hover .pb-cta, .pamphlet-band a:focus-visible .pb-cta {
  background: var(--text); border-color: var(--text); color: var(--bg); transform: translateX(3px); }
@media (max-width: 680px) {
  .pamphlet-band a { grid-template-columns: 1fr; gap: .9rem; }
  .pamphlet-band .pb-flag { border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 .9rem; }
  .pamphlet-band .scene-plate { max-width: 320px; }
}
@media (prefers-reduced-motion: reduce) {
  .pamphlet-band .pb-cta,
  .pamphlet-band a:hover .pb-cta, .pamphlet-band a:focus-visible .pb-cta {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""


def _accent_css(accent, selector="body"):
    """A page-scoped --accent override: accent=(light, dark) hex pair, or None
    for no override. Used to keep a detached desk's whole page run (not just
    its home-page bands) in one signature color, e.g. the Ad Tech desk's blue."""
    if not accent:
        return ""
    light, dark = accent
    return (f'\n{selector} {{ --accent: {light}; }} '
            f'[data-theme="dark"] {selector} {{ --accent: {dark}; }}')


def read_pamphlets_manifest(out_dir, subdir="pamphlets"):
    """Load docs/<subdir>/manifest.json → list of pamphlet-shaped essays, newest
    first. Missing → []. Also serves desk essay racks (e.g. docs/briefings/)."""
    path = Path(out_dir) / subdir / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, treating as no pamphlets", file=sys.stderr)
        return []
    items = data.get("pamphlets", data) if isinstance(data, dict) else data
    items = [p for p in items if isinstance(p, dict) and p.get("slug")]
    items.sort(key=lambda p: (p.get("date", ""), p.get("slug", "")), reverse=True)
    return items


def _pamphlet_href(p, subdir="pamphlets"):
    return html.escape(p.get("file") or f"{subdir}/{p.get('slug','')}.html", quote=True)


def _pamphlet_voice(p):
    w = p.get("writer", "")
    return f"In the voice of {w}" if w else ""


def _pamphlet_meta(p, lead=False, noun="pamphlet"):
    """A '· '-joined dateline: [Latest ·] subject · long date."""
    bits = [f"Latest {noun}"] if lead else []
    if p.get("subject"):
        bits.append(p["subject"])
    if p.get("date"):
        bits.append(_long_date(p["date"]))
    return " · ".join(bits)


def pamphlets_band_html(items, cfg, page=None):
    """The Pamphlets feature band (home page), or — via `page` — a sibling essay
    rack's band, e.g. the Ad Tech desk's Briefings: {name, flag, href, cls}."""
    page = page or {}
    name = page.get("name", "The Pamphlets")
    href = page.get("href", "pamphlets.html")
    cls = f' {page["cls"]}' if page.get("cls") else ""
    scene_kind = "briefing" if "brf" in cls or "Briefing" in name else "pamphlet"
    motto = html.escape(cfg.get("motto", ""))
    blurb = html.escape(cfg.get("blurb", ""))
    flag_title = page.get("flag", "The<br>Pamphlets")
    flag = (f'<div class="pb-flag">{flag_title}<small>{motto}</small></div>')
    if items:
        latest = items[0]
        kicker = f"{name} · Latest"
        lead = html.escape(latest.get("title") or "Latest essay")
        sub_bits = [x for x in [latest.get("dek"), _pamphlet_voice(latest)] if x]
        sub = html.escape(" · ".join(sub_bits))
        cta = "Read the latest →"
    else:
        kicker = "A new section"
        lead = motto or name
        sub = blurb
        cta = "Coming soon →"
    mid = (f'<div class="pb-mid"><p class="pb-kicker">{html.escape(kicker)}</p>'
           f'<p class="pb-lead">{lead}</p><p class="pb-sub">{sub}</p></div>')
    latest_key = items[0].get("slug") if items else href
    scene = scene_plate(scene_kind, extra_class="band-scene", seed=f"{name}:{latest_key}")
    return (f'<div class="pamphlet-band{cls}"><a href="{html.escape(href, quote=True)}">{flag}{scene}{mid}'
            f'<span class="pb-cta">{cta}</span></a></div>')


PAMPHLETS_PAGE_CSS = """
/* The Pamphlets — a hand-set broadside: the uppercase lintel nameplate, the
   featured pamphlet as a framed sheet with the pressman's quoin, an arrowed
   archive ledger. Shares the paper / serif / rule / accent language. */
.pam-plate { display: block; max-width: 820px; margin: 1.6rem auto 0; padding: 0 2rem; text-align: center; }
.pam-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .22em; color: var(--accent); margin: 0 0 .5rem; }
.pam-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.8rem, 7vw, 4.8rem);
  line-height: .95; letter-spacing: .04em; text-transform: uppercase;
  border-top: 3px solid var(--text); border-bottom: 1px solid var(--text);
  padding: .9rem 0 .8rem; margin: 0 0 .7rem; }
.pam-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.pam-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--mono); font-size: .72rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--text); }
.pam-folio .pam-folio-c { color: var(--accent); font-weight: 700; }

/* featured (latest) pamphlet — the sheet itself, framed in the chase */
.pam-feature { max-width: 720px; margin: 2.4rem auto 0; padding: 0 2rem; }
.pam-feature a { display: block; text-decoration: none; color: var(--text);
  background: var(--bg); border: 1px solid var(--border); padding: 2rem 2.2rem 1.9rem;
  position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.pam-feature a:hover, .pam-feature a:focus-visible { border-color: var(--text);
  transform: translateY(-2px); box-shadow: var(--shadow-2); z-index: 1; }
/* the pressman's keystone — the carmine quoin locked into the chase's corner */
.pam-feature a::before { content: ""; position: absolute; top: -1px; left: -1px;
  width: 34px; height: 8px; background: var(--accent); }
.pamf-meta { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .7rem; }
.pamf-head { font-family: var(--display); font-weight: 600; font-size: clamp(2rem, 4.6vw, 3rem);
  line-height: 1.05; letter-spacing: -.01em; margin: 0 0 .7rem; transition: color .15s ease; }
.pamf-head::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.pam-feature a:hover .pamf-head { color: var(--accent); }
.pamf-dek { font-family: var(--serif); font-style: italic; font-size: 1.25rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 .9rem; }
.pamf-by { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--accent); margin: 0 0 1.1rem; }
.pamf-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.pam-feature a:hover .pamf-cta, .pam-feature a:focus-visible .pamf-cta {
  background: var(--text); border-color: var(--text); color: var(--bg); transform: translateX(3px); }

/* more pamphlets — archive rows */
.pam-issues { max-width: 720px; margin: 2.8rem auto 0; padding: 0 2rem 1rem; }
.pam-issues-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .3rem; }
.pam-row { display: grid; grid-template-columns: 1fr auto; gap: 1.2rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .95rem 0; border-bottom: 1px solid var(--border);
  position: relative; padding-right: 1.6rem; }
.pam-row::after { content: "→"; position: absolute; right: 0; top: 50%;
  margin-top: -.6em; font-family: var(--sans); font-size: .9rem; line-height: 1;
  color: var(--accent); opacity: 0; transform: translateX(-4px);
  transition: opacity .16s var(--ease), transform .16s var(--ease); }
.pam-row:hover::after, .pam-row:focus-visible::after { opacity: 1; transform: translateX(0); }
.pam-row-body { min-width: 0; }
.pam-row-head { font-family: var(--display); font-size: 1.22rem; line-height: 1.18; display: block; transition: color .15s ease; }
.pam-row:hover .pam-row-head { color: var(--accent); }
.pam-row-by { display: flex; align-items: baseline; gap: .55rem; margin-top: .25rem;
  font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); }
.pam-row-by::after { content: ""; flex: 1; min-width: 1.5rem; height: .7em;
  background-image: radial-gradient(circle, var(--border) 1px, transparent 1.2px);
  background-size: 6px 2px; background-repeat: repeat-x; background-position: 0 60%; }
.pam-row:hover .pam-row-by::after, .pam-row:focus-visible .pam-row-by::after {
  background-image: radial-gradient(circle, var(--accent) 1px, transparent 1.2px); }
.pam-row-date { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; }

.pam-empty { max-width: 720px; margin: 2.1rem auto 0; padding: 2rem; text-align: center;
  color: var(--muted); font-family: var(--sans); font-size: .9rem; border-top: 3px double var(--border); }

.pam-foot { max-width: 720px; margin: 3rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.pam-foot .epigraph { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .95rem; margin: 0 0 .6rem; }
.pam-foot .colophon { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; margin: 0; }
.pam-foot .colophon a { color: var(--accent); text-decoration: none; }
.pam-foot .colophon a:hover { text-decoration: underline; }

@media (max-width: 560px) {
  .pam-folio { font-size: .58rem; letter-spacing: .08em; }
  .pam-row-date { display: none; }
  .pam-row-by::after { display: none; }
  .pam-row { padding-right: 0; }
  .pam-row::after { display: none; }
  .pam-name { letter-spacing: .02em; }
}
@media (prefers-reduced-motion: reduce) {
  .pam-feature a, .pam-feature a:hover, .pam-feature a:focus-visible,
  .pamf-cta, .pam-feature a:hover .pamf-cta, .pam-feature a:focus-visible .pamf-cta,
  .pam-row::after, .pam-row:hover::after, .pam-row:focus-visible::after {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

PAMPHLETS_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{page_title} — calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}{accent_css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="pam-plate">
  <p class="pam-kicker">{kicker}</p>
  <h1 class="pam-name">{h1}</h1>
  <p class="pam-motto">“{motto}”</p>
  {scene}
  <div class="pam-folio">
    <span>Vol. 1</span>
    <span class="pam-folio-c">{stats}</span>
    <span>Published irregularly</span>
  </div>
</header>
<main class="pam-wrap">
{body}
</main>
<footer class="pam-foot">
  <p class="epigraph">{blurb}</p>
  <p class="colophon">{back}</p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""


def pamphlet_feature_html(p, subdir="pamphlets", noun="pamphlet"):
    """The latest pamphlet (or desk briefing), rendered like a section lead."""
    meta = _pamphlet_meta(p, lead=True, noun=noun)
    headline = html.escape(p.get("title") or f"Untitled {noun}")
    dek = html.escape(p.get("dek") or "")
    dek_html = f'<p class="pamf-dek">{dek}</p>' if dek else ""
    voice = html.escape(_pamphlet_voice(p))
    voice_html = f'<p class="pamf-by">{voice}</p>' if voice else ""
    scene_kind = "briefing" if noun == "briefing" or subdir != "pamphlets" else "pamphlet"
    return (f'<article class="pam-feature"><a href="{_pamphlet_href(p, subdir)}">'
            f'{scene_plate(scene_kind, extra_class="feature-scene", seed=_pamphlet_href(p, subdir))}'
            f'<p class="pamf-meta">{html.escape(meta)}</p>'
            f'<h2 class="pamf-head">{headline}</h2>{dek_html}{voice_html}'
            f'<span class="pamf-cta">Read the {html.escape(noun)} →</span></a></article>')


def pamphlet_row_html(p, subdir="pamphlets"):
    """An archive row for an older pamphlet (or desk briefing)."""
    headline = html.escape(p.get("title") or "Untitled pamphlet")
    voice = html.escape(_pamphlet_voice(p))
    voice_html = f'<span class="pam-row-by">{voice}</span>' if voice else ""
    when = html.escape(_long_date(p.get("date", "")) if p.get("date") else "")
    return (f'<a class="pam-row" href="{_pamphlet_href(p, subdir)}">'
            f'<span class="pam-row-body"><span class="pam-row-head">{headline}</span>{voice_html}</span>'
            f'<span class="pam-row-date">{when}</span></a>')


PAMPHLETS_NAV_DEFAULT = main_nav_html(active="pamphlets.html")


def build_pamphlets_page(out_dir, items, cfg, shell="", page=None):
    """Render an essay-rack section front — docs/pamphlets.html by default: a
    featured latest essay + the rest. A `page` override renders a desk's own
    rack (e.g. the Ad Tech desk's Briefings): {fname, title (h1), kicker, nav,
    back, empty, noun, subdir, accent (light, dark)}."""
    out = Path(out_dir)
    page = page or {}
    fname = page.get("fname", "pamphlets.html")
    h1 = page.get("title", "The Pamphlets")
    kicker = page.get("kicker", "Writer-voiced essays")
    nav = page.get("nav", PAMPHLETS_NAV_DEFAULT)
    back = page.get("back", '<a href="research.html">← Back to the Research Library</a>')
    noun = page.get("noun", "pamphlet")
    subdir = page.get("subdir", "pamphlets")
    accent = page.get("accent")
    accent_css = _accent_css(accent)
    scene_kind = "briefing" if noun == "briefing" or subdir != "pamphlets" else "pamphlet"
    if items:
        n = len(items)
        stats = f"{n} {noun}{'s' if n != 1 else ''}"
        body = pamphlet_feature_html(items[0], subdir=subdir, noun=noun)
        rest = items[1:]
        if rest:
            rows = "\n".join(pamphlet_row_html(p, subdir=subdir) for p in rest)
            body += f'<section class="pam-issues"><h2 class="pam-issues-h">More {noun}s</h2>{rows}</section>'
    else:
        body = '<p class="pam-empty">' + html.escape(page.get(
            "empty", "No pamphlets published yet. Drop an essay into "
                     "docs/pamphlets/data/ and list it in the manifest to see it here.")) + '</p>'
        stats = f"No {noun}s yet"
    og = (OG_META if fname == "pamphlets.html" else
          og_tags(h1, cfg.get("motto", ""), f"{SITE_URL}/{fname}", f"{SITE_URL}/{OG_IMAGE}"))
    page_html = PAMPHLETS_PAGE_TEMPLATE.format(
        page_title=html.escape(h1), h1=html.escape(h1), kicker=html.escape(kicker),
        nav=nav, back=back, accent_css=accent_css,
        css=LIBRARY_CSS + SCENE_PLATE_CSS + PAMPHLETS_PAGE_CSS,
        favicon=FAVICON, og_meta=og,
        motto=html.escape(cfg.get("motto", "")),
        scene=scene_plate(scene_kind, extra_class="section-scene", seed=f"{fname}:{scene_kind}"),
        blurb=html.escape(cfg.get("blurb", "")),
        stats=stats,
        body=body,
        theme_js=LIBRARY_THEME_JS,
        shell=shell,
    )
    (out / fname).write_text(_persona_public_copy(page_html))
    print(f"  ✓ {h1}  ({len(items)} {noun}{'s' if len(items) != 1 else ''}) → {fname}")


# ---------------------------------------------------------------- pamphlet pages
# Each pamphlet is rendered natively from docs/pamphlets/data/{slug}.json into
# docs/pamphlets/{slug}.html, inheriting the site's design system. Subdir pages,
# so every nav/footer href is ../-relative (the shell base is "../" too).

PAMPHLET_CSS = """
/* A single pamphlet — a hand-set broadside sheet in the catalogue register:
   press rule over the nameplate, set-type label, big keystone, small-caps
   opening line, four-line drop cap, fleuron close. Shares every token. */
.pm-essay { max-width: 720px; margin: 0 auto; padding: 2rem 2rem 1rem; }

/* nameplate — a compact echo of pamphlets.html's section plate. display:block
   overrides the generic header{display:flex} rule from LIBRARY_CSS. */
.pm-nameplate { display: block; text-align: center; max-width: 640px; margin: 0 auto 2.6rem;
  border-top: 3px solid var(--text); padding: 1rem 0 0; }
.pm-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .45rem; }
.pm-name { display: inline-block; font-family: var(--display); font-weight: 600;
  font-size: clamp(1.9rem, 4.6vw, 2.6rem); line-height: 1; letter-spacing: .04em; text-transform: uppercase;
  color: var(--text); text-decoration: none; margin: 0 0 .9rem; }
.pm-name:hover { color: var(--accent); }
.pm-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .5rem 0;
  font-family: var(--mono); font-size: .64rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; }
.pm-folio .pm-folio-c { color: var(--accent); font-weight: 600; }
.pm-folio span { flex: 1; }
.pm-folio span:first-child { text-align: left; }
.pm-folio span:last-child { text-align: right; }

/* the essay head — "A LATTER-DAY PAMPHLET" as a piece of set type */
.pm-tagline { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .16em; color: var(--accent);
  border: 1px solid var(--accent); padding: .3rem .7rem; margin: 0 0 .8rem; }
.pm-head { font-family: var(--display); font-weight: 600; letter-spacing: -.01em; line-height: 1.03;
  font-size: clamp(2.2rem, 5.2vw, 3.3rem); margin: 0 0 .7rem; }
.pm-head::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.pm-dek { font-family: var(--serif); font-style: italic; color: var(--muted);
  font-size: 1.32rem; line-height: 1.42; margin: 0 0 .9rem; }
.pm-byline { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--accent);
  border-top: 1px solid var(--border); border-bottom: 3px double var(--border);
  padding: .45rem 0 .5rem; margin: 0 0 1.7rem; }

/* the body */
.pm-body { font-size: 1.09rem; line-height: 1.78; }
.pm-body p { margin: 0 0 1.2rem; }
.pm-body a { color: var(--accent); }
.pm-body em { font-style: italic; }
.pm-body blockquote { margin: 1.6rem 0; padding: .15rem 0 .15rem 1.2rem; border-left: 2px solid var(--accent);
  background: transparent; border-radius: 0; color: var(--muted); font-style: italic; }
/* drop cap opens the essay at broadside scale */
.pm-body > p:first-of-type::first-letter { font-family: var(--display); font-weight: 600;
  float: left; font-size: 4.1em; line-height: .78; padding: .04em .12em 0 0; color: var(--accent); }
/* small-caps opening line */
.pm-body > p:first-of-type::first-line { font-variant-caps: small-caps; letter-spacing: .04em; }
/* the fleuron close */
.pm-body > p:last-of-type::after { content: " ❦"; color: var(--accent); }

/* foot — names the voice */
.pm-foot { max-width: 720px; margin: 2.8rem auto 0; padding: 1.5rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.pm-colophon { font-family: var(--serif); font-style: italic; color: var(--muted); font-size: .96rem; margin: 0 0 .8rem; }
.pm-nav { font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); margin: 0; }
.pm-nav a { color: var(--accent); text-decoration: none;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-size: 0% 1.5px; background-repeat: no-repeat; background-position: 0 100%;
  transition: background-size .18s var(--ease), color .14s var(--ease); }
.pm-nav a:hover, .pm-nav a:focus-visible { background-size: 100% 1.5px; color: var(--accent); }

/* floating theme toggle, same affordance as the corpus reader */
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#theme-btn:hover, #theme-btn:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }

@media (max-width: 620px) {
  .pm-essay { padding: 1.6rem 1.3rem 1rem; }
  .pm-folio { font-size: .58rem; letter-spacing: .04em; }
  .pm-body > p:first-of-type::first-letter { font-size: 3.3em; }
  .pm-tagline { letter-spacing: .12em; }
}
@media (prefers-reduced-motion: reduce) {
  #theme-btn, #theme-btn:hover, #theme-btn:focus-visible,
  .pm-nav a, .pm-nav a:hover, .pm-nav a:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

PAMPHLET_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}{accent_css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="../index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<main class="pm-essay">
  <header class="pm-nameplate">
    <p class="pm-kicker">{list_kicker}</p>
    <a class="pm-name" href="../{list_href}">{list_name}</a>
    <div class="pm-folio">
      <span>{folio_writer}</span>
      <span class="pm-folio-c">{folio_subject}</span>
      <span>{folio_date}</span>
    </div>
  </header>
  <article>
    {tagline}
    <h1 class="pm-head">{headline}</h1>
    {dek}
    {byline}
    {scene}
    <div id="pm-body" class="pm-body"></div>
  </article>
  <footer class="pm-foot">
    {colophon}
    <p class="pm-nav"><a href="../{list_href}">← All {list_noun}s</a> &nbsp;·&nbsp; <a href="../research.html">The Research Library</a></p>
  </footer>
</main>
<button id="theme-btn" title="Light / dark">◐ Theme</button>
<script id="pamphlet-data" type="application/json">{data_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

PAMPHLET_JS = r"""
// theme toggle — the theme itself is applied pre-paint by the <head> boot script
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

const pm = JSON.parse(document.getElementById('pamphlet-data').textContent);
const body = document.getElementById('pm-body');
body.innerHTML = marked.parse(pm.body || '');
body.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
"""


def read_pamphlet_data(out_dir, slug, subdir="pamphlets"):
    """Load docs/<subdir>/data/{slug}.json → dict, or None if absent/unreadable.
    subdir lets a desk essay rack (e.g. "briefings") share this loader."""
    path = Path(out_dir) / subdir / "data" / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, skipping that pamphlet", file=sys.stderr)
        return None


def build_pamphlet(out_dir, item, shell="", page=None):
    """Render one pamphlet page (docs/pamphlets/{slug}.html) from its data file.
    A `page` override renders a desk's own essay rack instead (e.g. the Ad Tech
    desk's Briefings): {subdir, list_href, list_name, nav_active, accent}.

    Returns True if rendered, False if the data file is absent (page left untouched)."""
    out = Path(out_dir)
    page = page or {}
    subdir = page.get("subdir", "pamphlets")
    list_href = page.get("list_href", "pamphlets.html")
    list_name = page.get("list_name", "The Pamphlets")
    accent_css = _accent_css(page.get("accent"))
    slug = item.get("slug", "")
    data = read_pamphlet_data(out, slug, subdir=subdir)
    if data is None:
        return False

    # Manifest entry is the source of truth for list metadata; the data file may
    # repeat or extend it. Prefer data-file values, fall back to the manifest.
    def pick(key, default=""):
        return data.get(key) or item.get(key) or default

    title = pick("title", "Untitled pamphlet")
    dek = pick("dek")
    writer = pick("writer")
    subject = pick("subject")
    date = pick("date")
    tagline = pick("kicker")  # e.g. "A Latter-Day Pamphlet"
    voice_note = pick("voice_note") or (f"Written in the voice of {writer}." if writer else "")
    scene_kind = "briefing" if subdir != "pamphlets" or page.get("noun") == "briefing" else "pamphlet"

    tagline_html = f'<p class="pm-tagline">{html.escape(tagline)}</p>' if tagline else ""
    dek_html = f'<p class="pm-dek">{html.escape(dek)}</p>' if dek else ""
    byline_html = f'<p class="pm-byline">In the voice of {html.escape(writer)}</p>' if writer else ""
    colophon_html = f'<p class="pm-colophon">{html.escape(voice_note)}</p>' if voice_note else ""

    nav_active = page.get("nav_active", list_href)
    page_html = PAMPHLET_TEMPLATE.format(
        title=html.escape(f"{title} — {list_name}"),
        description=html.escape(dek or subject or "A pamphlet"),
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(prefix="../", active=nav_active),
        css=LIBRARY_CSS + SCENE_PLATE_CSS + PAMPHLET_CSS,
        accent_css=accent_css,
        list_kicker=html.escape(page.get("kicker", "Writer-voiced essays")),
        list_href=html.escape(list_href, quote=True),
        list_name=html.escape(list_name),
        list_noun=html.escape(page.get("noun", "pamphlet")),
        folio_writer=html.escape(writer),
        folio_subject=html.escape(subject),
        folio_date=html.escape(_long_date(date) if date else ""),
        tagline=tagline_html,
        headline=html.escape(title),
        dek=dek_html,
        byline=byline_html,
        scene=scene_plate(scene_kind, extra_class="article-scene", root="../", seed=f"{subdir}:{slug}"),
        colophon=colophon_html,
        data_json=json_for_html(data),
        marked_js=MARKED_JS,
        app_js=PAMPHLET_JS,
        shell=shell,
    )
    (out / subdir).mkdir(parents=True, exist_ok=True)
    (out / subdir / f"{slug}.html").write_text(page_html)
    return True


# ---------------------------------------------------------------- the forecast desk
# A fifth top-level section: "The Forecast Desk" — every prediction the research
# makes, by category, worn like a prediction market's board. Two kinds of entry:
#   1. NATIVE forecasts (docs/forecast/manifest.json + docs/forecast/data/{slug}.json)
#      — standalone Predictive-Council runs (e.g. the World Cup pick) rendered as a
#      full "live market" page: consensus pick, the seven-profile predictor roster,
#      the field board, base rates, the market snapshot, flip triggers.
#   2. HARVESTED markets — each research corpus's manifest.json `scenarios` become
#      a multi-outcome market card (scenario = outcome, probability band = price),
#      deep-linked to that corpus's Future Trajectory chapter.
# Design register: a dark trading board set into the letterpress site — the same
# move as the Ghost's inky band. Board tokens are local (--fd*) so both site themes
# just work; numerals are mono; the leading outcome wears the up-tick green.

FORECAST_BAND_CSS = """
/* The Forecast Desk band on the home page — a ruled announcement row; market
   greens on paper (light #15803d / dark #4ade80). */
.fd-band { max-width: 1120px; margin: 0 auto; padding: 0 2rem; }
.fd-band a { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: var(--text); background: transparent; border: 0;
  border-top: 1px solid var(--border); border-radius: 0; box-shadow: none;
  padding: 1.3rem 0 1.4rem; position: relative; }
.fd-band a:hover .fdb-lead, .fd-band a:focus-visible .fdb-lead { color: #15803d; }
.fd-band .fdb-flag { font-family: var(--display); font-weight: 600; font-size: 1.6rem; line-height: 1.02;
  color: var(--text); border-right: 1px solid var(--border); padding-right: 1.6rem; }
.fd-band .fdb-flag small { display: block; font-family: var(--serif); font-style: italic; font-weight: 400;
  font-size: .72rem; color: var(--muted); margin-top: .55rem; }
.fd-band .fdb-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: #15803d; margin: 0 0 .35rem; }
.fd-band .fdb-kicker .fdb-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: #15803d; margin-right: .45rem; animation: fdb-pulse 1.8s ease infinite; }
@keyframes fdb-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .fd-band .fdb-kicker .fdb-dot { animation: none; } }
.fd-band .fdb-lead { font-family: var(--display); font-weight: 600; font-size: 1.2rem; line-height: 1.28;
  margin: 0 0 .3rem; color: var(--text); transition: color .15s var(--ease); }
.fd-band .fdb-lead .fdb-price { font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: .92rem; font-weight: 700; color: #fcfbf7; background: #15803d; border-radius: 2px;
  padding: .14rem .5rem; margin-left: .5rem; white-space: nowrap; }
.fd-band .fdb-sub { font-family: var(--serif); font-style: italic; font-size: .9rem; line-height: 1.45; color: var(--muted); margin: 0; }
.fd-band .fdb-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: #15803d; background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.fd-band a:hover .fdb-cta, .fd-band a:focus-visible .fdb-cta {
  background: #15803d; border-color: #15803d; color: #fcfbf7; transform: translateX(3px); }
[data-theme="dark"] .fd-band a:hover .fdb-lead, [data-theme="dark"] .fd-band a:focus-visible .fdb-lead { color: #4ade80; }
[data-theme="dark"] .fd-band .fdb-kicker { color: #4ade80; }
[data-theme="dark"] .fd-band .fdb-kicker .fdb-dot { background: #4ade80; }
[data-theme="dark"] .fd-band .fdb-lead .fdb-price { color: #0c1117; background: #4ade80; }
[data-theme="dark"] .fd-band .fdb-cta { color: #4ade80; }
[data-theme="dark"] .fd-band a:hover .fdb-cta, [data-theme="dark"] .fd-band a:focus-visible .fdb-cta {
  background: #4ade80; border-color: #4ade80; color: #0c1117; }
@media (max-width: 680px) {
  .fd-band a { grid-template-columns: 1fr; gap: .9rem; }
  .fd-band .fdb-flag { border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 .9rem; }
}
@media (prefers-reduced-motion: reduce) {
  .fd-band .fdb-cta,
  .fd-band a:hover .fdb-cta, .fd-band a:focus-visible .fdb-cta {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""


def read_forecast_manifest(out_dir):
    """Load docs/forecast/manifest.json → list of native forecasts, newest first. Missing → []."""
    path = Path(out_dir) / "forecast" / "manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, treating as no forecasts", file=sys.stderr)
        return []
    items = data.get("forecasts", data) if isinstance(data, dict) else data
    items = [f for f in items if isinstance(f, dict) and f.get("slug")]
    items.sort(key=lambda f: (f.get("logged", f.get("date", "")), f.get("slug", "")), reverse=True)
    return items


def read_forecast_data(out_dir, slug):
    """Load docs/forecast/data/{slug}.json → dict, or None if absent/unreadable."""
    path = Path(out_dir) / "forecast" / "data" / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, skipping that forecast", file=sys.stderr)
        return None


# ---- the grading loop -------------------------------------------------------
# docs/forecast/resolutions.json is the desk's grading ledger: one entry per
# resolved market, native or harvested. The build joins entries by slug and
# computes every hit/miss, record, Brier score, and calibration point from it —
# records are never hand-maintained. A native data file that carries its own
# `result` block (status graded/resolved) is honored as a second source.

# The seven standing desk personas (see the-forecaster skill). Profiles in native
# data files carry a stable `persona` id so a market-localized display name
# ("The Bracket Surgeon") still accrues to its standing persona's record.
FD_PERSONAS = [
    ("market-reader", "Mira Tape", "📈", "Follows the deepest, most liquid market prices."),
    ("quant", "Quinn Ratio", "🧮", "Trusts the simulation models over recency and sentiment."),
    ("historian", "Ada Ledger", "📜", "Counted reference-class base rates and venue history."),
    ("path-reader", "Rowan Wayfinder", "🔪", "Ignores reputation; reads the actual route, draw, and path."),
    ("talisman", "Stella North", "⭐", "Elite individual actors, motivation, proven leadership."),
    ("contrarian", "Nico Tilt", "🎭", "Favorites usually fail; hunts the underpriced live path."),
    ("prophet", "Elias Lantern", "🕊", "Divine providence — backs the fated, story-shaped ending, however long the odds."),
]
FD_PERSONA_DETAILS = {
    "market-reader": {"agent": "Mira Tape", "role": "Ticker-lens agent", "monogram": "MT", "mascot": "ticker-lens scout"},
    "quant": {"agent": "Quinn Ratio", "role": "Ratio-grid agent", "monogram": "QR", "mascot": "ratio-grid bot"},
    "historian": {"agent": "Ada Ledger", "role": "Archive-stack agent", "monogram": "AL", "mascot": "archive-stack keeper"},
    "path-reader": {"agent": "Rowan Wayfinder", "role": "Route-compass agent", "monogram": "RW", "mascot": "route-compass guide"},
    "talisman": {"agent": "Stella North", "role": "North-star agent", "monogram": "SN", "mascot": "north-star talisman"},
    "contrarian": {"agent": "Nico Tilt", "role": "Split-mask agent", "monogram": "NT", "mascot": "split-mask challenger"},
    "prophet": {"agent": "Elias Lantern", "role": "Signal-lantern agent", "monogram": "EL", "mascot": "signal-lantern seer"},
}
FD_PERSONA_ALIASES = {
    "the bracket surgeon": "path-reader",
    "the market reader": "market-reader",
    "the quant": "quant",
    "the historian": "historian",
    "the path reader": "path-reader",
    "the talisman": "talisman",
    "the contrarian": "contrarian",
    "the prophet": "prophet",
    **{v["agent"].casefold(): k for k, v in FD_PERSONA_DETAILS.items()},
}
FD_PERSONA_PUBLIC_REPLACEMENTS = {
    "The Market Reader": "Mira Tape",
    "The Quant": "Quinn Ratio",
    "The Historian": "Ada Ledger",
    "The Path Reader": "Rowan Wayfinder",
    "The Talisman": "Stella North",
    "The Contrarian": "Nico Tilt",
    "The Prophet": "Elias Lantern",
}


def _persona_public_copy(value):
    """Swap legacy archetype names in imported forecast copy for agent names."""
    if not isinstance(value, str):
        return value
    for old, new in FD_PERSONA_PUBLIC_REPLACEMENTS.items():
        value = value.replace(old, new)
        value = value.replace(old.replace("The ", "the "), new)
    return value


def _persona_agent_name(key, fallback=""):
    return FD_PERSONA_DETAILS.get(key, {}).get("agent") or fallback


def _persona_role(key, fallback=""):
    return FD_PERSONA_DETAILS.get(key, {}).get("role") or fallback


def _persona_monogram(key, fallback=""):
    return FD_PERSONA_DETAILS.get(key, {}).get("monogram") or fallback[:2].upper()


def _persona_mascot_name(key):
    return FD_PERSONA_DETAILS.get(key, {}).get("mascot", "forecast mark")


def _persona_mascot_html(key, size=""):
    """Reusable character mascot for one Forecast Desk agent."""
    meta = FD_PERSONA_DETAILS.get(key, {})
    label = f'{meta.get("agent", key)} mascot: {meta.get("mascot", "forecast mark")}'
    cls = f' fd-agent-{size}' if size else ""
    color = FDT_SERIES_COLORS.get(key, "#9aa1af") if "FDT_SERIES_COLORS" in globals() else "#9aa1af"
    return (f'<span class="fd-agent-mascot fdmas-{html.escape(key, quote=True)}{cls}" '
            f'style="--agent-color:{color}" aria-label="{html.escape(label, quote=True)}" '
            f'title="{html.escape(label, quote=True)}">'
            '<span class="fd-agent-glow"></span>'
            '<span class="fd-agent-prop"></span>'
            '<span class="fd-agent-prop2"></span>'
            '<span class="fd-agent-body"></span>'
            '<span class="fd-agent-arm left"></span><span class="fd-agent-arm right"></span>'
            '<span class="fd-agent-head"><span class="fd-agent-eye eye-l"></span>'
            '<span class="fd-agent-eye eye-r"></span><span class="fd-agent-mouth"></span></span>'
            f'<span class="fd-agent-mark">{html.escape(_persona_monogram(key))}</span></span>')

# ---- The spectrum: rigor → intuition → creative hypothesis -------------------
# The roster is not a committee of equals — it is a spectrum. On the left sit the
# calculators (decomposition, base rates, the tape); in the middle the readers of
# route and character; on the right the imaginers who back a story over a number.
# Each persona's forecasting MODEL and its play-money BETTING DOCTRINE are both
# drawn from where it sits, so the desk's fake-money P&L becomes the running
# answer to one question: over a season, does disciplined rigor or inspired
# long-shotting build the bigger roll? `spectrum` is 0.0 (pure rigor) → 1.0
# (pure creative hypothesis); `zone` is the band it falls in.
FD_PERSONA_PROFILES = {
    "quant": {
        "spectrum": 0.05, "zone": "Mathematical rigor",
        "model": "Builds the answer from parts. Decomposes a question into independent factors, "
                 "assigns each a number it can defend, and multiplies — trusting a structured model "
                 "over the mood of the tape or the last loud headline.",
        "doctrine": "Bets the mathematically optimal fraction, halved for safety, when its own number "
                    "says the market is wrong — and a token ante when the price already agrees. Often the "
                    "smallest bet on the board; never the reckless one.",
        "signature": "the cold decomposition",
    },
    "historian": {
        "spectrum": 0.20, "zone": "Mathematical rigor",
        "model": "Counts. Finds the reference class — the last N times something like this came up — "
                 "and reads the ledger of what usually happened. The base rate is the anchor, and the "
                 "story of the day rarely beats it.",
        "doctrine": "Stakes the same disciplined unit on its pick every time, win or lose. "
                    "The consistency is the edge.",
        "signature": "the counted precedent",
    },
    "market-reader": {
        "spectrum": 0.33, "zone": "Mathematical rigor",
        "model": "Reads the tape. The deepest, most liquid market price is the best estimate anyone "
                 "has; the job is to respect it and lean only when a real gap opens between the price "
                 "and the evidence.",
        "doctrine": "The smallest player at the table: a token stake riding the market's own price most "
                    "nights, and a real lean only when the tape visibly disagrees with itself.",
        "signature": "siding with the price",
    },
    "path-reader": {
        "spectrum": 0.50, "zone": "Intuition & reading",
        "model": "Ignores reputation and reads the actual route — the draw, the mechanism, the path "
                 "dependence. Who does the favorite actually have to get past, and does the road really "
                 "go through?",
        "doctrine": "Puts a fixed, unbothered stake on its read of the mechanism whatever the odds say. "
                    "It is buying the route, not the price.",
        "signature": "reading the draw",
    },
    "talisman": {
        "spectrum": 0.66, "zone": "Intuition & reading",
        "model": "Looks for the individual who bends the outcome — the proven leader, the elite actor, "
                 "the figure who has done it before. Systems matter, but at the hinge a person decides.",
        "doctrine": "Bets in proportion to its faith in that actor: sure of the hero, it loads up; "
                    "unsure, it barely shows.",
        "signature": "backing the man",
    },
    "contrarian": {
        "spectrum": 0.82, "zone": "Creative hypothesis",
        "model": "Starts from the premise that favorites are overrated and the crowd is a step behind. "
                 "Hunts the live path everyone has written off and asks what the board is mispricing.",
        "doctrine": "Bets bigger the longer the odds on the underdog it likes — half-size when the board "
                    "already agrees with it. Its worst nights are quiet, its best nights are loud.",
        "signature": "fading the chalk",
    },
    "prophet": {
        "spectrum": 0.96, "zone": "Creative hypothesis",
        "model": "Reads the story, not the tape. Asks which ending would make everything so far look "
                 "like foreshadowing, and backs the fated outcome however long the odds — the call "
                 "everyone swears, afterward, was obvious.",
        "doctrine": "Stakes heavy on the pick it believes is written, odds no object. It goes broke, "
                    "or it goes down in legend.",
        "signature": "backing the ending",
    },
}


def _persona_key(profile):
    """Stable ledger key for a roster profile: explicit `persona` id, a known
    alias, or the display name slugged (so unknown names still track)."""
    if profile.get("persona"):
        return str(profile["persona"]).strip().lower()
    name = (profile.get("name") or "").strip()
    alias = FD_PERSONA_ALIASES.get(name.casefold())
    if alias:
        return alias
    return re.sub(r"[^a-z0-9]+", "-", name.casefold().removeprefix("the ")).strip("-")


def read_forecast_resolutions(out_dir):
    """Load docs/forecast/resolutions.json → {slug: resolution dict}. Missing → {}."""
    path = Path(out_dir) / "forecast" / "resolutions.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ! could not read {path}, treating as ungraded", file=sys.stderr)
        return {}
    out = {}
    for r in data.get("resolutions", []):
        if isinstance(r, dict) and r.get("slug"):
            out[r["slug"]] = r
    return out


def _native_resolution(d, resolutions):
    """A native forecast's resolution: the central ledger wins; a `result` block
    in the data file itself (the skill's grading flow) is honored as fallback.
    Returns {winner, winner_flag, resolved, note, source_url, source_label} or None."""
    res = resolutions.get(d.get("slug", ""))
    if res and res.get("winner"):
        return res
    if d.get("status") in ("graded", "resolved") and isinstance(d.get("result"), dict):
        r = d["result"]
        if r.get("winner"):
            return {"slug": d.get("slug", ""), "winner": r["winner"],
                    "winner_flag": r.get("flag", r.get("winner_flag", "")),
                    "resolved": r.get("resolved", r.get("date", "")),
                    "note": r.get("note", ""), "source_url": r.get("source_url", ""),
                    "source_label": r.get("source_label", "")}
    return None


def _brier(p_pct, hit):
    """Binary Brier score for a stated probability (percent) against a 0/1 outcome.
    0 is clairvoyance, 0.25 is a coin flip, 1 is confident wrongness."""
    return (p_pct / 100.0 - (1 if hit else 0)) ** 2


def grade_native_forecast(d, res):
    """Grade one native forecast against its resolution: the council's consensus
    call plus every roster profile, each as a binary claim on its own pick.
    Returns {winner, winner_flag, resolved, note, source_url, source_label,
             consensus: {pick, prob, hit, brier}, profiles: [{persona, name,
             avatar, pick, flag, prob, hit, brier}]}."""
    winner = (res.get("winner") or "").strip()
    wkey = winner.casefold()
    c = d.get("consensus", {})
    lo, hi = c.get("band_low"), c.get("band_high")
    c_prob = ((lo + hi) / 2 if isinstance(lo, (int, float)) and isinstance(hi, (int, float))
              else None)
    if c_prob is None:
        band = _prob_band({"probability_range": c.get("band", "")})
        c_prob = (band[0] + band[1]) / 2 if band else 50.0
    c_hit = (c.get("pick", "").strip().casefold() == wkey)
    graded = {
        "winner": winner, "winner_flag": res.get("winner_flag", ""),
        "resolved": res.get("resolved", ""), "note": res.get("note", ""),
        "source_url": res.get("source_url", ""), "source_label": res.get("source_label", ""),
        "consensus": {"pick": c.get("pick", ""), "flag": c.get("flag", ""),
                      "prob": c_prob, "hit": c_hit, "brier": _brier(c_prob, c_hit)},
        "profiles": [],
    }
    for p in d.get("profiles", []):
        prob = p.get("prob_num")
        if not isinstance(prob, (int, float)):
            band = _prob_band({"probability_range": p.get("prob", "")})
            prob = (band[0] + band[1]) / 2 if band else 50.0
        hit = ((p.get("pick_scenario") or p.get("pick", "")).strip().casefold() == wkey)
        pkey = _persona_key(p)
        graded["profiles"].append({
            "persona": pkey, "name": _persona_agent_name(pkey, p.get("name", "")),
            "avatar": p.get("avatar", "🎯"), "pick": p.get("pick", ""),
            "flag": p.get("flag", ""), "prob": float(prob),
            "hit": hit, "brier": _brier(float(prob), hit),
        })
    return graded


def _match_market_outcome(m, res):
    """Index of the resolved outcome in a harvested market, matched
    case-insensitively on the scenario name. None when nothing matches."""
    want = (res.get("outcome") or "").strip().casefold()
    if not want:
        return None
    for i, o in enumerate(m["outcomes"]):
        if o["name"].strip().casefold() == want:
            return i
    return None


def attach_market_resolution(m, resolutions):
    """Join a harvested market with the grading ledger. On a match, sets
    m['resolution'] = {idx, name, resolved, note, source_url, source_label,
    lead_hit, brier} — graded on the desk's lead call (the top-priced outcome,
    at its band midpoint). Warns and leaves the market open on a name mismatch."""
    res = resolutions.get(m["slug"])
    if not res:
        return
    idx = _match_market_outcome(m, res)
    if idx is None:
        print(f"  ! resolution for {m['slug']}: outcome {res.get('outcome')!r} "
              f"matches no scenario — market left open", file=sys.stderr)
        return
    lead = m["outcomes"][0]
    lead_hit = (idx == 0)
    m["resolution"] = {
        "idx": idx, "name": m["outcomes"][idx]["name"],
        "resolved": res.get("resolved", ""), "note": res.get("note", ""),
        "source_url": res.get("source_url", ""), "source_label": res.get("source_label", ""),
        "lead_hit": lead_hit, "brier": _brier(lead["mid"], lead_hit),
    }


def build_forecast_ledger(native_items, markets):
    """Everything the record pages and board chips need, computed in one pass:
    - personas: {key: {name, avatar, criterion, graded, hits, briers[], calls[]}}
      for the seven standing personas (plus any guest), in roster order
    - calls: every graded call (council + personas + research lead calls)
    - desk: {graded, hits} — the desk's own record (consensus + lead calls)
    - pending: open positions, native first by grade date, then research by horizon
    native_items are manifest entries annotated with _graded (grade_native_forecast
    output) when resolved; native_data maps slug → full data dict."""
    personas = {k: {"key": k, "name": _persona_agent_name(k, n), "avatar": a, "criterion": c,
                    "graded": 0, "hits": 0, "briers": [], "calls": []}
                for k, n, a, c in FD_PERSONAS}
    calls, pending = [], []
    desk = {"graded": 0, "hits": 0, "briers": []}
    for f in native_items:
        g = f.get("_graded")
        if not g:
            pending.append({"kind": "native", "title": f.get("title", f.get("slug", "")),
                            "href": f.get("file") or f"forecast/{f.get('slug', '')}.html",
                            "call": f"{f.get('pick_flag', '')} {f.get('pick', '')} {f.get('band', '')}".strip(),
                            "due": f.get("grades", ""), "category": f.get("category", "")})
            continue
        c = g["consensus"]
        desk["graded"] += 1
        desk["hits"] += 1 if c["hit"] else 0
        desk["briers"].append(c["brier"])
        calls.append({"caller": "The Council", "avatar": "🏛", "kind": "council",
                      "market": f.get("title", f.get("slug", "")),
                      "href": f.get("file") or f"forecast/{f.get('slug', '')}.html",
                      "call": c["pick"], "flag": c.get("flag", ""), "prob": c["prob"],
                      "result": g["winner"], "result_flag": g.get("winner_flag", ""),
                      "hit": c["hit"], "brier": c["brier"], "date": g.get("resolved", "")})
        for p in g["profiles"]:
            pkey = p["persona"]
            entry = personas.setdefault(pkey, {
                "key": pkey, "name": _persona_agent_name(pkey, p["name"]), "avatar": p["avatar"],
                "criterion": "", "graded": 0, "hits": 0, "briers": [], "calls": []})
            entry["graded"] += 1
            entry["hits"] += 1 if p["hit"] else 0
            entry["briers"].append(p["brier"])
            entry["calls"].append({"market": f.get("title", f.get("slug", "")),
                                   "pick": p["pick"], "flag": p.get("flag", ""),
                                   "prob": p["prob"], "hit": p["hit"], "brier": p["brier"]})
            calls.append({"caller": entry["name"], "avatar": p["avatar"], "kind": "persona", "key": pkey,
                          "market": f.get("title", f.get("slug", "")),
                          "href": f.get("file") or f"forecast/{f.get('slug', '')}.html",
                          "call": p["pick"], "flag": p.get("flag", ""), "prob": p["prob"],
                          "result": g["winner"], "result_flag": g.get("winner_flag", ""),
                          "hit": p["hit"], "brier": p["brier"], "date": g.get("resolved", "")})
    for m in markets:
        r = m.get("resolution")
        lead = m["outcomes"][0]
        if not r:
            pending.append({"kind": "research", "title": m["title"], "href": m["href"],
                            "call": f"{lead['name']} {_fmt_band(lead['low'], lead['high'])}",
                            "due": (m.get("desk") or {}).get("grades", ""),
                            "horizon": m.get("horizon", ""),
                            "category": m["category"]})
            continue
        desk["graded"] += 1
        desk["hits"] += 1 if r["lead_hit"] else 0
        desk["briers"].append(r["brier"])
        calls.append({"caller": "The Research", "avatar": "📚", "kind": "research",
                      "market": m["title"], "href": m["href"],
                      "call": lead["name"], "flag": "", "prob": lead["mid"],
                      "result": r["name"], "result_flag": "",
                      "hit": r["lead_hit"], "brier": r["brier"], "date": r.get("resolved", "")})
        # A graded market whose corpus carries a forecast_desk dossier grades its
        # roster too — the standing personas' records accrue from research desks
        # exactly as they do from native forecasts.
        wkey = r["name"].strip().casefold()
        for p in (m.get("desk") or {}).get("profiles", []):
            prob = p.get("prob_num")
            if not isinstance(prob, (int, float)):
                band = _prob_band({"probability_range": p.get("prob", "")})
                prob = (band[0] + band[1]) / 2 if band else 50.0
            hit = ((p.get("pick_scenario") or p.get("pick", "")).strip().casefold() == wkey)
            brier = _brier(float(prob), hit)
            pkey = _persona_key(p)
            entry = personas.setdefault(pkey, {
                "key": pkey, "name": _persona_agent_name(pkey, p.get("name", "")),
                "avatar": p.get("avatar", "🎯"), "criterion": "",
                "graded": 0, "hits": 0, "briers": [], "calls": []})
            entry["graded"] += 1
            entry["hits"] += 1 if hit else 0
            entry["briers"].append(brier)
            entry["calls"].append({"market": m["title"], "pick": p.get("pick", ""),
                                   "flag": p.get("flag", ""), "prob": float(prob),
                                   "hit": hit, "brier": brier})
            calls.append({"caller": entry["name"], "avatar": entry["avatar"], "kind": "persona", "key": pkey,
                          "market": m["title"], "href": m["href"],
                          "call": p.get("pick", ""), "flag": p.get("flag", ""), "prob": float(prob),
                          "result": r["name"], "result_flag": "",
                          "hit": hit, "brier": brier, "date": r.get("resolved", "")})
    calls.sort(key=lambda c: c.get("date", ""), reverse=True)
    pending.sort(key=lambda p: (p["kind"] != "native", p.get("due", "") or "~", p.get("title", "")))
    return {"personas": personas, "calls": calls, "desk": desk, "pending": pending}


# ---- The Book: the roster bets fake money at the market's own odds ----------
# Every standing persona opens an account with FD_BANKROLL_START. On each market
# it stakes on its own pick at the market's implied odds — but each persona sizes
# that stake by its OWN doctrine (FD_DOCTRINES / _fd_persona_bet), drawn from where
# it sits on the rigor→creative spectrum: the Quant sizes half-Kelly on its edge,
# the Historian a flat unit, the Market Reader rides the tape for token money,
# the Contrarian and the Prophet swing for the fences on long odds. A win pays the
# book's decimal odds; a loss costs the stake; every persona antes on every market
# it has a pick on — the doctrines differ in size, never in absence.
# Bankrolls compound: settled bets apply in resolution-date order, so a hot streak
# stakes more real dollars. It is scored from the same graded ledger as the Brier
# records — never hand-maintained. This turns the desk's scoreboard from
# wins-and-losses into economic value, and into a running test of the spectrum
# itself: does disciplined rigor or inspired long-shotting build the bigger roll?
FD_BANKROLL_START = 1000.0   # every persona opens the book with $1,000
FD_KELLY_CAP = 0.25          # hard ceiling — no persona stakes more than a quarter of its roll
FD_CCY = "$"

# Each persona bets in its own style, drawn from its place on the spectrum. All
# bets pay at the market's own decimal odds (1/price) — personas differ only in
# how they SIZE the stake and when they PASS. `style` selects the sizing rule in
# _fd_persona_bet; `cap` is that persona's personal ceiling (≤ FD_KELLY_CAP).
FD_DOCTRINES = {
    "quant":         {"style": "half_kelly",  "cap": 0.18, "how": "Half-Kelly on its model edge; a token ante when the price already agrees"},
    "historian":     {"style": "flat_base",   "cap": 0.10, "how": "A flat reference-class unit, the same every time"},
    "market-reader": {"style": "tape_tail",   "cap": 0.06, "how": "Small tape-following stakes; leans only on a real gap"},
    "path-reader":   {"style": "conviction",  "cap": 0.14, "how": "A fixed unit on the mechanism, odds be damned"},
    "talisman":      {"style": "belief",      "cap": 0.20, "how": "Sized to its faith in the key actor"},
    "contrarian":    {"style": "longshot",    "cap": 0.22, "how": "Bigger the longer the odds on the underdog it fades to"},
    "prophet":       {"style": "all_in_fate", "cap": 0.25, "how": "A heavy fixed stake on the fated pick, odds no object"},
}
FD_CONF_MULT = {"high": 1.2, "medium": 1.0, "low": 0.7}


def _fd_money(x):
    """A grouped dollar string, no sign: 1234.5 → $1,234."""
    return f"{FD_CCY}{x:,.0f}"


def _fd_signed(x):
    """A signed dollar string with a proper minus glyph: -50 → −$50."""
    return f"+{FD_CCY}{x:,.0f}" if x >= 0 else f"−{FD_CCY}{abs(x):,.0f}"


def _fd_book_from_native(d):
    """Implied-price book for a native forecast: pick name (casefold) → price %.
    Team markets carry a `field` (team→price); scenario markets fall back to
    `outcomes` band midpoints. Empty when neither is present."""
    book = {}
    for o in d.get("field") or []:
        nm = (o.get("team") or o.get("name") or "").strip().casefold()
        if nm and isinstance(o.get("price"), (int, float)):
            book[nm] = float(o["price"])
    for o in d.get("outcomes") or []:
        nm = (o.get("name") or "").strip().casefold()
        lo, hi = o.get("low"), o.get("high")
        if nm and nm not in book and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            book[nm] = (float(lo) + float(hi)) / 2
    return book


def _fd_persona_bet(key, p_pct, q_pct, bankroll, confidence="medium"):
    """One persona's bet on its own pick, sized by its DOCTRINE (FD_DOCTRINES[key]).
    p_pct = the persona's stated probability; q_pct = the market's implied price
    for that pick, giving decimal odds 1/q at which every persona is paid. The
    doctrines differ only in how they size — every persona antes something on
    every market it has a pick on; nobody rides the rail:
      half_kelly  (Quant)         — ½·(p−q)/(1−q) on a positive edge; a token
                                    2.5% ante when the price already agrees
      tape_tail   (Market Reader) — ¼-Kelly past a 3-pt gap; otherwise a small
                                    1.5% tape-follow at the market's own price
      flat_base   (Historian)     — a flat confidence-scaled unit, every time
      conviction  (Path Reader)   — a fixed unit whatever the odds
      belief      (Talisman)      — scaled to its own stated conviction p
      longshot    (Contrarian)    — grows with the odds; half-size when the
                                    board already agrees with it
      all_in_fate (Prophet)       — a heavy flat stake, edge no object
    Returns {q, p, edge, dec_odds, frac, stake, to_win, style}.
    Every fraction is clamped to the persona's own cap (≤ FD_KELLY_CAP)."""
    if not (isinstance(p_pct, (int, float)) and isinstance(q_pct, (int, float))):
        return None
    q = min(max(q_pct / 100.0, 0.001), 0.999)
    p = min(max(p_pct / 100.0, 0.0), 1.0)
    dec = 1.0 / q
    edge = p - q
    doc = FD_DOCTRINES.get(key, {"style": "half_kelly", "cap": FD_KELLY_CAP})
    style, cap = doc["style"], doc["cap"]
    cm = FD_CONF_MULT.get((confidence or "medium").strip().lower(), 1.0)
    if style == "tape_tail":
        frac = 0.25 * edge / (1 - q) if edge > 0.03 and q < 1 else 0.015
    elif style == "flat_base":
        frac = 0.06 * cm
    elif style == "conviction":
        frac = 0.10 * cm
    elif style == "belief":
        frac = p * 0.22 * cm
    elif style == "longshot":
        frac = (0.035 if edge > 0 else 0.0175) * (dec - 1) * cm
    elif style == "all_in_fate":
        frac = 0.20 * cm
    else:  # half_kelly — the Quant and any unknown persona
        frac = (0.5 * edge / (1 - q)) * cm if edge > 0 and q < 1 else 0.025 * cm
    frac = max(0.0, min(frac, cap))
    stake = round(bankroll * frac, 2)
    return {"q": q_pct, "p": p_pct, "edge": p_pct - q_pct, "dec_odds": dec,
            "frac": frac, "stake": stake, "to_win": round(stake * (dec - 1), 2), "style": style}


def _fd_bet_rows(native_items, markets, native_data):
    """Every persona bet on the board — one row per (persona, market): {key,
    name, avatar, market, href, pick, flag, p, q, graded, hit, date, due}.
    Graded rows carry hit/date; open rows carry due. native_data maps slug → the
    full native data dict (for its book + roster probabilities)."""
    rows = []
    native_data = native_data or {}
    for f in native_items:
        d = native_data.get(f.get("slug", ""))
        if not d:
            continue
        book = _fd_book_from_native(d)
        g = f.get("_graded")
        wkey = (g["winner"].strip().casefold() if g else None)
        title = f.get("title", f.get("slug", ""))
        href = f.get("file") or f"forecast/{f.get('slug', '')}.html"
        for p in d.get("profiles", []):
            pick = (p.get("pick_scenario") or p.get("pick") or "").strip()
            q = book.get(pick.casefold())
            pp = p.get("prob_num")
            if q is None or not isinstance(pp, (int, float)):
                continue
            pkey = _persona_key(p)
            rows.append({"key": pkey, "name": _persona_agent_name(pkey, p.get("name", "")),
                         "avatar": p.get("avatar", "🎯"), "market": title, "href": href,
                         "pick": pick, "flag": p.get("flag", ""), "p": float(pp), "q": float(q),
                         "conf": p.get("confidence", "medium"),
                         "graded": bool(g), "hit": (pick.casefold() == wkey) if g else None,
                         "date": (g.get("resolved", "") if g else ""), "due": f.get("grades", "")})
    for m in markets:
        book = {o["name"].strip().casefold(): o["mid"] for o in m["outcomes"]}
        r = m.get("resolution")
        wkey = (r["name"].strip().casefold() if r else None)
        for p in (m.get("desk") or {}).get("profiles", []):
            pick = (p.get("pick_scenario") or p.get("pick") or "").strip()
            q = book.get(pick.casefold())
            pp = p.get("prob_num")
            if not isinstance(pp, (int, float)):
                band = _prob_band({"probability_range": p.get("prob", "")})
                pp = (band[0] + band[1]) / 2 if band else None
            if q is None or not isinstance(pp, (int, float)):
                continue
            pkey = _persona_key(p)
            rows.append({"key": pkey, "name": _persona_agent_name(pkey, p.get("name", "")),
                         "avatar": p.get("avatar", "🎯"), "market": m["title"], "href": m["href"],
                         "pick": pick, "flag": p.get("flag", ""), "p": float(pp), "q": float(q),
                         "conf": p.get("confidence", "medium"),
                         "graded": bool(r), "hit": (pick.casefold() == wkey) if r else None,
                         "date": (r.get("resolved", "") if r else ""),
                         "due": (m.get("desk") or {}).get("grades", "") or m.get("horizon", "")})
    return rows


def build_book(native_items, markets, native_data):
    """The Book: each standing persona's fake-money account, settled from the
    graded ledger and marked-to-market on open positions. Returns
    {personas: {key: account}, ranked: [account, …], start, settled, open_n,
     at_risk, leader}. Accounts seed in roster order so the leaderboard is
     complete even before a single bet settles. account = {key, name, avatar,
     color, bankroll, pnl, staked, settled, wins, passes, biggest, bets[],
     open[], at_risk}."""
    def _seed(key, name, avatar):
        return {"key": key, "name": name, "avatar": avatar,
                "color": FDT_SERIES_COLORS.get(key, "#9aa1af"),
                "bankroll": FD_BANKROLL_START, "pnl": 0.0, "staked": 0.0,
                "settled": 0, "wins": 0, "passes": 0, "biggest": None,
                "bets": [], "open": [], "at_risk": 0.0}
    accounts = {k: _seed(k, _persona_agent_name(k, n), a) for k, n, a, c in FD_PERSONAS}

    def acct(row):
        return accounts.setdefault(row["key"], _seed(row["key"], row["name"], row["avatar"]))

    rows = _fd_bet_rows(native_items, markets, native_data)
    settled = 0
    for r in sorted([r for r in rows if r["graded"]], key=lambda r: r.get("date") or "9999"):
        a = acct(r)
        bet = _fd_persona_bet(r["key"], r["p"], r["q"], a["bankroll"], r.get("conf"))
        if not bet or bet["stake"] <= 0:      # its doctrine sat this one out
            a["passes"] += 1
            a["bets"].append({**r, "stake": 0.0, "pnl": 0.0,
                              "dec_odds": (bet["dec_odds"] if bet else 0.0), "pass": True})
            continue
        pnl = bet["to_win"] if r["hit"] else -bet["stake"]
        a["bankroll"] += pnl
        a["pnl"] += pnl
        a["staked"] += bet["stake"]
        a["settled"] += 1
        a["wins"] += 1 if r["hit"] else 0
        settled += 1
        rec = {**r, "stake": bet["stake"], "dec_odds": bet["dec_odds"], "pnl": pnl, "pass": False}
        a["bets"].append(rec)
        if r["hit"] and (a["biggest"] is None or pnl > a["biggest"]["pnl"]):
            a["biggest"] = rec
    open_n, at_risk = 0, 0.0
    for r in [r for r in rows if not r["graded"]]:
        a = acct(r)
        bet = _fd_persona_bet(r["key"], r["p"], r["q"], a["bankroll"], r.get("conf"))
        if not bet:
            continue
        pos = {**r, "stake": bet["stake"], "dec_odds": bet["dec_odds"],
               "to_win": bet["to_win"], "edge": bet["edge"], "pass": bet["stake"] <= 0}
        a["open"].append(pos)
        if bet["stake"] > 0:
            a["at_risk"] += bet["stake"]
            at_risk += bet["stake"]
            open_n += 1
    ranked = sorted(accounts.values(), key=lambda a: (a["bankroll"], a["settled"]), reverse=True)
    leader = next((a for a in ranked if a["settled"]), None)
    return {"personas": accounts, "ranked": ranked, "start": FD_BANKROLL_START,
            "settled": settled, "open_n": open_n, "at_risk": at_risk, "leader": leader}


def _prob_band(scenario):
    """Normalize a scenario's probability to a (low, high) pair of percentages.
    Accepts {low,high} in 0–1 or 0–100, a bare float, or a prose range like
    '26–32%' / '~40%'. Returns None when nothing parseable is present."""
    p = scenario.get("probability")
    rng = scenario.get("probability_range")
    if isinstance(rng, str) and rng.strip():          # prose range wins when present
        nums = re.findall(r"\d+(?:\.\d+)?", rng)
        if nums:
            vals = [float(n) for n in nums[:2]]
            if len(vals) == 1:
                vals = vals * 2
            return (min(vals), max(vals))
    if isinstance(p, dict):
        lo, hi = p.get("low"), p.get("high")
        if lo is None and hi is None:
            return None
        lo = float(lo if lo is not None else hi)
        hi = float(hi if hi is not None else lo)
        if hi <= 1.001:
            lo, hi = lo * 100, hi * 100
        return (min(lo, hi), max(lo, hi))
    if isinstance(p, (int, float)):
        v = float(p)
        v = v * 100 if v <= 1.001 else v
        return (v, v)
    if isinstance(p, str):
        nums = re.findall(r"\d+(?:\.\d+)?", p)
        if nums:
            vals = [float(n) for n in nums[:2]]
            if len(vals) == 1:
                vals = vals * 2
            return (min(vals), max(vals))
    return None


def _fmt_band(lo, hi):
    def f(v):
        return str(int(round(v))) if abs(v - round(v)) < .05 else f"{v:.1f}"
    return f"{f(lo)}%" if abs(hi - lo) < .05 else f"{f(lo)}–{f(hi)}%"


def harvest_corpus_market(folder, corpus, category, cover=None, description=""):
    """Turn one research corpus's manifest `scenarios` into a Forecast Desk market
    (scenario = outcome, probability band = price). Returns None when the corpus
    carries no structured scenarios. Board cards open the market's own desk page
    (forecast/{slug}.html); the desk page carries the deep link into the corpus's
    Future Trajectory chapter (reader #ch-{i} anchors)."""
    try:
        manifest = json.loads((Path(folder) / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    scenarios = manifest.get("scenarios") or []
    outcomes = []
    for s in scenarios:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        band = _prob_band(s)
        if band is None:
            continue
        def _lst(key):
            v = s.get(key)
            return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
        outcomes.append({
            "name": s["name"],
            "low": band[0], "high": band[1],
            "mid": (band[0] + band[1]) / 2,
            "horizon": (s.get("time_horizon") or "").strip(),
            "description": (s.get("description") or "").strip(),
            "derivation": (s.get("derivation") or "").strip(),
            "drivers": _lst("drivers"),
            "signals": _lst("signals_to_watch"),
            "conditions": _lst("conditions"),
        })
    if not outcomes:
        return None
    outcomes.sort(key=lambda o: o["mid"], reverse=True)
    ft_i = next((i for i, d in enumerate(corpus["documents"])
                 if d.get("file") == "Future_Trajectory.md"
                 or "future trajectory" in d.get("title", "").lower()), None)
    research_href = f"{corpus['slug']}.html" + (f"#ch-{ft_i}" if ft_i is not None else "")
    horizon = (manifest.get("forecast_horizon") or outcomes[0]["horizon"] or "").strip()
    # The optional `forecast_desk` dossier (written by the-forecaster) upgrades this
    # market's desk page to the full live-market dress: predictor roster, consensus,
    # base rates, market snapshot, triggers, sources, grading date.
    desk = manifest.get("forecast_desk")
    return {
        "slug": corpus["slug"],
        "title": corpus["title"],
        "subtitle": corpus.get("subtitle", ""),
        "description": (description or "").strip(),  # the library card's blurb, shown on board cards too
        "category": category,
        "href": f"forecast/{corpus['slug']}.html",   # the market's own desk page
        "research_href": research_href,              # root-relative reader deep link
        "cover": cover,                              # root-relative covers/{name}, or None
        "horizon": horizon,
        "outcomes": outcomes,
        "desk": desk if isinstance(desk, dict) else {},
    }


def forecast_band_html(native_items, markets, cfg):
    """The Forecast Desk band for the home page. Leads with the latest live native
    market when one exists; otherwise counts the harvested board."""
    motto = html.escape(cfg.get("motto", ""))
    n_markets = len(native_items) + len(markets)
    n_outcomes = sum(len(m["outcomes"]) for m in markets) + len(native_items)
    n_graded = (sum(1 for f in native_items if f.get("_graded"))
                + sum(1 for m in markets if m.get("resolution")))
    hits = (sum(1 for f in native_items if f.get("_graded") and f["_graded"]["consensus"]["hit"])
            + sum(1 for m in markets if m.get("resolution") and m["resolution"]["lead_hit"]))
    rec_bit = f" · {n_graded} graded, record {hits}–{n_graded - hits}" if n_graded else ""
    flag = f'<div class="fdb-flag">The Forecast<br>Desk<small>{motto}</small></div>'
    live = next((f for f in native_items if f.get("status", "open") == "open"), None)
    if live:
        kicker = '<span class="fdb-dot"></span>Live market'
        q = html.escape(live.get("question") or live.get("title") or "Live forecast")
        price = ""
        if live.get("pick"):
            chip = html.escape(f"{live.get('pick_flag', '')} {live['pick']} {live.get('band', '')}".strip())
            price = f'<span class="fdb-price">{chip}</span>'
        lead = f"{q}{price}"
        sub = f"{n_markets} markets on the board · {n_outcomes} priced outcomes{rec_bit} · every one grounded in the research"
    else:
        kicker = "Predictions, by category"
        lead = html.escape(cfg.get("title", "The Forecast Desk"))
        sub = f"{n_markets} markets · {n_outcomes} priced outcomes across the research library{rec_bit}"
    mid = (f'<div class="fdb-mid"><p class="fdb-kicker">{kicker}</p>'
           f'<p class="fdb-lead">{lead}</p><p class="fdb-sub">{html.escape(sub) if live else sub}</p></div>')
    return (f'<div class="fd-band"><a href="forecast.html">{flag}{mid}'
            f'<span class="fdb-cta">To the board →</span></a></div>')


FORECAST_PAGE_CSS = """
/* The Forecast Desk — the site's one dark terminal, set into the paper site.
   Board tokens are local so light and dark site themes both read correctly.
   The paper plate signs itself in market green (light #15803d / dark #4ade80);
   accent has left this plate. */
.fd-plate { display: block; max-width: 900px; margin: 1.6rem auto 0; padding: 0 2rem; text-align: center; }
.fd-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: #15803d; margin: 0 0 .5rem; }
.fd-kicker::before { content: ""; display: inline-block; width: 8px; height: 8px; background: #15803d; margin-right: .5rem; }
[data-theme="dark"] .fd-kicker { color: #4ade80; }
[data-theme="dark"] .fd-kicker::before { background: #4ade80; }
.fd-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.4rem, 6vw, 4rem);
  line-height: .98; letter-spacing: 0; margin: 0 0 .55rem; }
.fd-name::after { content: ""; display: block; width: 148px; height: 8px; margin: .65rem auto 0; border-radius: 0;
  background:
    linear-gradient(#15803d, #15803d) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
[data-theme="dark"] .fd-name::after {
  background:
    linear-gradient(#4ade80, #4ade80) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.fd-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.fd-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 2px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--mono); font-size: .72rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--text); }
.fd-folio .fd-folio-c { color: #15803d; font-weight: 600; }
[data-theme="dark"] .fd-folio .fd-folio-c { color: #4ade80; }

/* the board — warm charcoal, not navy; the color lives in the outcomes */
.fd-board { --fdbg: #13151b; --fdcard: #1b1e26; --fdline: #2c303a; --fdtext: #edeff4;
  --fdmut: #9aa1af; --fdup: #22c55e; --fddn: #ef4444; --fdgold: #eab308; --fdblue: #60a5fa;
  --fdshadow: 0 6px 18px rgba(0,0,0,.45);
  --fdmono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  max-width: 1080px; margin: 2rem auto 0; padding: 1.6rem 1.6rem 2rem; border-radius: 2px;
  background: var(--fdbg); border: 1px solid var(--fdline); color: var(--fdtext);
  box-shadow: none; }

/* ticker tape */
.fd-tape { overflow: hidden; border: 1px solid var(--fdline); border-radius: 2px;
  background: #0a0f16; margin: 0 0 1.4rem; -webkit-mask-image: linear-gradient(90deg, transparent, #000 6% 94%, transparent);
  mask-image: linear-gradient(90deg, transparent, #000 6% 94%, transparent); }
.fd-tape-inner { display: inline-flex; gap: 2.2rem; padding: .55rem 0; white-space: nowrap;
  animation: fd-tape 240s linear infinite; will-change: transform; }
.fd-tape:hover .fd-tape-inner { animation-play-state: paused; }
@keyframes fd-tape { from { transform: translateX(0); } to { transform: translateX(-50%); } }
@media (prefers-reduced-motion: reduce) { .fd-tape-inner { animation: none; } }
.fd-tk { font-family: var(--fdmono); font-size: .78rem; color: var(--fdmut); font-variant-numeric: tabular-nums; }
.fd-tk b { color: var(--fdtext); font-weight: 600; }
.fd-tk .up { color: var(--fdup); font-weight: 700; }

/* live native market hero */
.fd-live { display: block; text-decoration: none; color: var(--fdtext); background: #10161f;
  border: 1px solid var(--fdline);
  border-radius: 2px; padding: 1.5rem 1.7rem; margin-bottom: 1.6rem;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease), border-color .15s var(--ease); }
.fd-live:hover { border-color: var(--fdup); transform: translateY(-2px); box-shadow: var(--fdshadow); }
.fd-live-top { display: flex; align-items: center; gap: .7rem; flex-wrap: wrap; margin-bottom: .7rem; }
.fd-chip-live { font-family: var(--sans); font-size: .64rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; color: #0c1117; background: var(--fdup); border-radius: 2px; padding: .22rem .6rem; }
.fd-chip-live .d { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #0c1117;
  margin-right: .4rem; animation: fdb-pulse 1.8s ease infinite; }
.fd-chip-cat { font-family: var(--sans); font-size: .64rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--fdblue); border: 1px solid var(--fdblue); border-radius: 2px; padding: .2rem .6rem; }
.fd-chip-date { font-family: var(--fdmono); font-size: .7rem; color: var(--fdmut); margin-left: auto; }
.fd-live-q { font-family: var(--display); font-weight: 600; font-size: clamp(1.5rem, 3.4vw, 2.1rem);
  line-height: 1.12; margin: 0 0 .9rem; }
.fd-live-grid { display: grid; grid-template-columns: auto 1fr; gap: 1.4rem; align-items: center; }
.fd-live-pick { text-align: center; background: #0c1117; border: 1px solid var(--fdline);
  border-radius: 2px; padding: .9rem 1.3rem; }
.fd-live-pick .f { font-size: 2rem; line-height: 1; }
.fd-live-pick .t { font-family: var(--sans); font-weight: 800; font-size: 1.05rem; margin-top: .25rem; }
.fd-live-pick .p { font-family: var(--fdmono); font-weight: 700; font-size: 1.05rem; color: var(--fdup); margin-top: .15rem;
  font-variant-numeric: tabular-nums; }
.fd-live-sub { font-family: var(--serif); font-style: italic; font-size: .95rem; color: var(--fdmut); line-height: 1.5; margin: 0; }
.fd-live-meta { display: flex; gap: 1.3rem; flex-wrap: wrap; margin-top: .8rem;
  font-family: var(--fdmono); font-size: .72rem; color: var(--fdmut); font-variant-numeric: tabular-nums; }
.fd-live-meta b { color: var(--fdtext); }
@media (max-width: 640px) { .fd-live-grid { grid-template-columns: 1fr; } }

/* category shelves + market cards */
.fd-cat-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: #c9cdd6; border-bottom: 1px solid var(--fdline); padding-bottom: .5rem; margin: 1.8rem 0 1rem; }
.fd-cat-h .tick { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: .55rem; vertical-align: -1px; }
.fd-cat-h .n { color: var(--fdup); font-family: var(--fdmono); }

/* super-section bands: the board's three wings — Sports & Politics (the
   standalone live calls), From the Research (corpus markets), Ad Tech & Media.
   Bigger and heavier than a category shelf head; each wears its own accent. */
.fd-sec { display: flex; align-items: baseline; gap: .7rem; flex-wrap: wrap;
  margin: 2.8rem 0 1.3rem; padding-bottom: .7rem; border-bottom: 2px solid var(--fdtext); }
.fd-sec:first-of-type { margin-top: 1.4rem; }
.fd-sec-bar { align-self: center; display: inline-block; width: 15px; height: 15px; border-radius: 3px; }
.fd-sec-t { font-family: var(--display); font-weight: 600; font-size: clamp(1.35rem, 3vw, 1.85rem);
  line-height: 1; margin: 0; color: var(--fdtext); }
.fd-sec-k { font-family: var(--serif); font-style: italic; font-size: .92rem; color: var(--fdmut); }
.fd-sec-n { margin-left: auto; align-self: center; font-family: var(--fdmono); font-size: .72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .1em; color: var(--fdmut); font-variant-numeric: tabular-nums; white-space: nowrap; }
@media (max-width: 640px) { .fd-sec-k { flex-basis: 100%; } .fd-sec-n { margin-left: 0; } }
.fd-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 1rem; }
.fd-card { display: flex; flex-direction: column; text-decoration: none; color: var(--fdtext);
  background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1.05rem 1.15rem;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease), border-color .15s var(--ease); }
.fd-card:hover { border-color: var(--fdup); transform: translateY(-2px); box-shadow: var(--fdshadow); }
.fd-card-top { display: flex; align-items: center; gap: .5rem; margin-bottom: .55rem; }
.fd-card-cover { width: 40px; height: 40px; object-fit: cover; border-radius: 2px;
  border: 1px solid var(--fdline); flex: none; }
.fd-chip-open { font-family: var(--sans); font-size: .58rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--fdup); border: 1px solid var(--fdup); border-radius: 2px; padding: .16rem .5rem; }
.fd-card-hz { font-family: var(--fdmono); font-size: .64rem; color: var(--fdmut); margin-left: auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }
.fd-card-q { font-family: var(--display); font-weight: 600; font-size: 1.06rem; line-height: 1.22; margin: 0 0 .75rem; }
.fd-card-sub { font-family: var(--serif); font-style: italic; font-size: .84rem; line-height: 1.5; color: var(--fdmut);
  margin: -.45rem 0 .8rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.fd-out { margin: 0 0 .55rem; }
.fd-out-l { display: flex; justify-content: space-between; gap: .8rem; align-items: baseline;
  font-family: var(--sans); font-size: .78rem; line-height: 1.3; margin-bottom: .28rem; }
.fd-out-l .nm { color: var(--fdmut); overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 1; -webkit-box-orient: vertical; }
.fd-out.lead .fd-out-l .nm { color: var(--fdtext); font-weight: 600; }
.fd-out-l .pc { font-family: var(--fdmono); font-size: .76rem; font-weight: 700; color: var(--fdmut); white-space: nowrap;
  font-variant-numeric: tabular-nums; }
.fd-out.lead .fd-out-l .pc { color: #0c1117; background: var(--fdup); border-radius: 2px; padding: .08rem .38rem; }
.fd-track { position: relative; height: 11px; border-radius: 0; background: #0a0f16; overflow: hidden; }
/* Filled is the DEFAULT state (no JS/animation dependency for correctness);
   the grow-in is a pure entrance flourish. */
.fd-fill { position: absolute; top: 0; bottom: 0; left: 0; border-radius: 0; background: var(--fdmut); opacity: .85;
  transform-origin: left; }
.fd-fill.rng { opacity: .3; }
.fd-out.lead .fd-fill { background: var(--fdup); opacity: 1; }
.fd-out.lead .fd-fill.rng { opacity: .35; }
@keyframes fd-grow { from { transform: scaleX(0); } }
@media (prefers-reduced-motion: no-preference) {
  .fd-fill { animation: fd-grow .8s cubic-bezier(.22,1,.36,1); }
}
.fd-card-foot { display: flex; justify-content: space-between; align-items: center; gap: .8rem;
  border-top: 1px solid var(--fdline); margin-top: auto; padding-top: .6rem;
  font-family: var(--sans); font-size: .68rem; color: var(--fdmut); }
.fd-card-foot .go { color: var(--fdup); text-transform: uppercase; letter-spacing: .08em; font-weight: 700;
  display: inline-block; transition: transform .16s var(--ease); }
.fd-card:hover .go { transform: translateX(3px); }

.fd-foot { max-width: 900px; margin: 2.4rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.fd-foot .epigraph { font-family: var(--serif); font-style: italic; color: var(--muted); margin: 0 0 .8rem; }
.fd-foot .colophon a { color: var(--accent); }
@media (max-width: 640px) { .fd-board { padding: 1rem 1rem 1.4rem; border-radius: 2px; margin-left: .6rem; margin-right: .6rem; } }
@media (prefers-reduced-motion: reduce) {
  .fd-live, .fd-live:hover,
  .fd-card, .fd-card:hover,
  .fd-card-foot .go, .fd-card:hover .go {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}

/* ---- the grading loop: verdict dress ---- */
.fd-chip-graded { font-family: var(--sans); font-size: .64rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: .14em; color: #0c1117; background: var(--fdup); border-radius: 2px; padding: .22rem .6rem; }
.fd-chip-graded.miss { background: var(--fddn); color: #fff; }
.fd-mk { font-family: var(--fdmono); font-weight: 700; font-size: .8rem; margin-right: .35rem; }
.fd-mk.won { color: var(--fdup); } .fd-mk.lost { color: var(--fddn); }
.fd-out.lost .nm { color: var(--fdmut); text-decoration: line-through; text-decoration-color: rgba(239,68,68,.5);
  text-decoration-thickness: 1px; }
.fd-out.lost .fd-track, .fd-out.lost .pc { opacity: .5; }
.fd-out.won .nm { color: var(--fdup); font-weight: 800; }
.fd-tk .dn { color: var(--fddn); font-weight: 700; }

/* mini predictor roster — pinned atop every board, links to full profiles */
.fdm-roster { margin: 0 0 1.4rem; }
.fdm-roster-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
  font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--fdmut); margin: 0 0 .5rem; }
.fdm-cmp { color: var(--fdblue); text-decoration: none; }
.fdm-cmp:hover { text-decoration: underline; }
.fdm-roster-row { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: .55rem; }
.fd-agent-mascot { --agent-color: #9aa1af; position: relative; display: inline-block; flex: none;
  width: 44px; height: 44px; color: var(--agent-color); background:
  radial-gradient(circle at 50% 28%, rgba(255,255,255,.08), transparent 34%), #0c1117;
  border: 1px solid var(--agent-color); border-radius: 7px; overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.04), 0 8px 18px rgba(0,0,0,.22); }
.fd-agent-mascot::before { content: ""; position: absolute; inset: 5px; border-radius: 6px;
  background: radial-gradient(circle at 50% 22%, currentColor, transparent 55%); opacity: .15; }
.fd-agent-mascot::after { content: ""; position: absolute; left: 14%; right: 14%; bottom: 8%;
  height: 12%; border-radius: 50%; background: currentColor; opacity: .16; }
.fd-agent-glow, .fd-agent-prop, .fd-agent-prop2, .fd-agent-body, .fd-agent-head,
.fd-agent-eye, .fd-agent-mouth, .fd-agent-arm, .fd-agent-mark { position: absolute; box-sizing: border-box; }
.fd-agent-glow { inset: 7%; border-radius: 50%; background: currentColor; opacity: .08; filter: blur(3px); }
.fd-agent-body { z-index: 2; left: 29%; bottom: 14%; width: 42%; height: 33%; border: 1.5px solid currentColor;
  border-radius: 45% 45% 12% 12%; background: linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,0)), currentColor;
  opacity: .46; }
.fd-agent-arm { z-index: 2; top: 58%; width: 20%; height: 2px; border-radius: 2px; background: currentColor; opacity: .78; }
.fd-agent-arm.left { left: 18%; transform: rotate(24deg); }
.fd-agent-arm.right { right: 18%; transform: rotate(-24deg); }
.fd-agent-head { z-index: 3; left: 31%; top: 24%; width: 38%; height: 36%; border: 1.5px solid currentColor;
  border-radius: 48% 48% 42% 42%; background: #dfd0bd; overflow: hidden; box-shadow: inset 0 -7px 0 rgba(12,17,23,.08); }
.fd-agent-eye { top: 42%; width: 9%; height: 9%; border-radius: 50%; background: #0c1117; }
.fd-agent-eye.eye-l { left: 29%; } .fd-agent-eye.eye-r { right: 29%; }
.fd-agent-mouth { left: 37%; bottom: 22%; width: 26%; height: 10%; border-bottom: 1.4px solid #0c1117; border-radius: 50%; }
.fd-agent-mark { right: 3px; bottom: 3px; z-index: 5; font-family: var(--fdmono);
  font-size: .52rem; font-weight: 800; line-height: 1; color: #0c1117; background: var(--agent-color);
  padding: 2px 3px; border-radius: 2px; letter-spacing: 0; }
.fd-agent-mini { width: 38px; height: 38px; }
.fd-agent-table { width: 34px; height: 34px; vertical-align: middle; margin-right: .5rem; }
.fd-agent-card { width: 46px; height: 46px; }
.fd-agent-title { width: 54px; height: 54px; vertical-align: middle; margin-right: .55rem; }
.fd-agent-title .fd-agent-mark { font-size: .62rem; }
.fd-profile-name { display: inline-flex; align-items: center; justify-content: center; gap: .2rem; flex-wrap: wrap; }
.fd-profile-name .txt { display: inline-flex; flex-direction: column; align-items: flex-start; line-height: 1; }
.fd-profile-name .role { font-family: var(--sans); font-size: .78rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: 0; color: var(--fdmut); margin-top: .3rem; }
.fdmas-market-reader .fd-agent-prop { z-index: 1; left: 9%; top: 25%; width: 31%; height: 21%;
  border-left: 1.7px solid currentColor; border-bottom: 1.7px solid currentColor;
  background: linear-gradient(135deg, transparent 52%, currentColor 53%, currentColor 60%, transparent 61%); opacity: .78; }
.fdmas-market-reader .fd-agent-prop2 { z-index: 4; right: 11%; top: 16%; width: 23%; height: 23%; border: 1.8px solid currentColor;
  border-radius: 50%; box-shadow: 5px 6px 0 -3px currentColor; transform: rotate(-12deg); }
.fdmas-quant .fd-agent-prop { z-index: 1; inset: 14%; border: 1px solid currentColor;
  background: radial-gradient(currentColor 1.2px, transparent 1.7px) 2px 2px / 8px 8px; opacity: .32; }
.fdmas-quant .fd-agent-prop2 { z-index: 4; left: 14%; bottom: 19%; width: 9%; height: 9%; border-radius: 50%;
  background: currentColor; box-shadow: 9px -8px 0 currentColor, 18px 1px 0 currentColor, 27px -9px 0 currentColor; opacity: .82; }
.fdmas-historian .fd-agent-prop { z-index: 4; left: 9%; bottom: 17%; width: 27%; height: 10%;
  border: 1.5px solid currentColor; background: #0c1117; box-shadow: 3px -5px 0 #0c1117, 4px -6px 0 currentColor, 7px -10px 0 #0c1117, 8px -11px 0 currentColor; }
.fdmas-historian .fd-agent-prop2 { z-index: 1; right: 11%; top: 17%; width: 21%; height: 30%;
  border: 1.5px solid currentColor; background: #0c1117; opacity: .7; }
.fdmas-historian .fd-agent-prop2::after { content: ""; position: absolute; left: 22%; right: 22%; top: 30%; height: 1.5px;
  background: currentColor; box-shadow: 0 6px 0 currentColor; }
.fdmas-path-reader .fd-agent-prop { z-index: 4; left: 11%; top: 62%; width: 58%; height: 2px; border-radius: 2px;
  background: currentColor; transform: rotate(-27deg); transform-origin: left center; }
.fdmas-path-reader .fd-agent-prop2 { z-index: 4; right: 12%; top: 16%; width: 22%; height: 22%;
  border: 1.7px solid currentColor; transform: rotate(45deg); }
.fdmas-path-reader .fd-agent-prop2::after { content: ""; position: absolute; left: 38%; top: -35%; width: 26%; height: 170%;
  background: currentColor; opacity: .75; }
.fdmas-talisman .fd-agent-prop { z-index: 4; left: 22%; top: 5%; width: 56%; height: 36%; background: currentColor;
  clip-path: polygon(50% 0, 61% 35%, 98% 35%, 68% 56%, 80% 96%, 50% 72%, 20% 96%, 32% 56%, 2% 35%, 39% 35%); }
.fdmas-talisman .fd-agent-prop2 { z-index: 1; left: 47%; top: 6%; width: 6%; height: 74%; background: currentColor; opacity: .14; }
.fdmas-talisman .fd-agent-head { top: 29%; }
.fdmas-contrarian .fd-agent-head::before { content: ""; position: absolute; inset: 0 50% 0 0; background: currentColor; opacity: .28; }
.fdmas-contrarian .fd-agent-mouth { transform: rotate(-10deg); }
.fdmas-contrarian .fd-agent-prop { z-index: 1; inset: 15%; border: 1.8px solid currentColor;
  background: linear-gradient(135deg, currentColor 0 45%, transparent 46%); opacity: .28; transform: rotate(-12deg); }
.fdmas-contrarian .fd-agent-prop2 { z-index: 4; right: 13%; top: 18%; width: 17%; height: 17%; border: 1.6px solid currentColor;
  background: #0c1117; transform: rotate(45deg); }
.fdmas-prophet .fd-agent-glow { opacity: .2; filter: blur(4px); }
.fdmas-prophet .fd-agent-prop { z-index: 4; right: 9%; bottom: 18%; width: 23%; height: 31%;
  border: 1.7px solid currentColor; border-radius: 48% 48% 10% 10%; background: rgba(12,17,23,.82); }
.fdmas-prophet .fd-agent-prop::before { content: ""; position: absolute; left: 30%; right: 30%; top: -22%; height: 24%;
  border: 1.4px solid currentColor; border-bottom: 0; border-radius: 50% 50% 0 0; }
.fdmas-prophet .fd-agent-prop2 { z-index: 5; right: 16%; bottom: 30%; width: 8%; height: 18%; border-radius: 50% 50% 45% 45%;
  background: currentColor; box-shadow: 0 0 10px currentColor; }
.fdm-card { display: flex; flex-direction: column; align-items: center; gap: .2rem; text-align: center;
  text-decoration: none; color: inherit; background: var(--fdcard); border: 1px solid var(--fdline);
  border-top: 3px solid var(--fdmut); border-radius: 2px; padding: .65rem .35rem .6rem;
  transition: transform .12s var(--ease), border-color .12s var(--ease); }
.fdm-card:hover { transform: translateY(-2px); border-color: var(--fdblue); }
.fdm-av { font-size: 1.3rem; line-height: 1; }
.fdm-nm { font-family: var(--sans); font-size: .66rem; font-weight: 800; line-height: 1.2; margin-top: .1rem; }
.fdm-role { font-family: var(--sans); font-size: .52rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0; color: var(--fdmut); line-height: 1.15; }
.fdm-rec { font-family: var(--fdmono); font-size: .68rem; color: var(--fdmut); font-variant-numeric: tabular-nums; }
.fdm-bank { font-family: var(--fdmono); font-size: .64rem; color: var(--fdgold); font-variant-numeric: tabular-nums; }
@media (max-width: 720px) { .fdm-roster-row { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 460px) { .fdm-roster-row { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.fd-live.graded:hover { border-color: var(--fdgold); }
.fd-live-grid.graded { grid-template-columns: auto auto 1fr; }
.fd-live-pick.won { border-color: var(--fdup); }
.fd-live-pick.won .p { color: var(--fdup); text-transform: uppercase; letter-spacing: .12em; font-size: .8rem; }
.fd-live-pick.called .p { font-size: .8rem; }
.fd-live-pick.called.missed { border-color: rgba(239,68,68,.5); }
.fd-live-pick.called.missed .p { color: var(--fddn); }
@media (max-width: 680px) { .fd-live-grid.graded { grid-template-columns: 1fr 1fr; } }
.fd-folio-g { color: var(--fdgold, #b8860b); font-weight: 700; }
.fd-rec-link { display: inline-block; margin-top: 1rem; font-family: var(--sans); font-size: .78rem;
  font-weight: 700; text-decoration: none; color: var(--accent); border: 1px solid var(--border);
  border-radius: 2px; padding: .5rem 1rem; transition: transform .14s var(--ease), box-shadow .14s var(--ease); }
.fd-rec-link:hover { transform: translateY(-1px); box-shadow: var(--shadow-2, 0 4px 12px rgba(0,0,0,.12)); }

/* ---- the track record page ---- */
.fdt-sum { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .9rem; }
.fdt-stat { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: .95rem 1.05rem; }
.fdt-stat .big { font-family: var(--fdmono); font-weight: 700; font-size: 1.55rem; color: var(--fdtext);
  font-variant-numeric: tabular-nums; }
.fdt-stat .big.up { color: var(--fdup); } .fdt-stat .big.gold { color: var(--fdgold); }
.fdt-stat .lbl { font-family: var(--sans); font-size: .64rem; text-transform: uppercase; letter-spacing: .12em;
  color: var(--fdmut); margin-top: .3rem; }
.fdt-note { font-family: var(--serif); font-style: italic; font-size: .9rem; line-height: 1.6; color: var(--fdmut); margin: 1rem 0 0; }
.fdt-roster { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
.fdt-p { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1rem 1.1rem; }
.fdt-p-top { display: flex; gap: .7rem; align-items: center; }
.fdt-p-av { font-size: 1.5rem; line-height: 1; background: #0c1117; border: 1px solid var(--fdline);
  border-radius: 2px; padding: .45rem .5rem; }
.fdt-p-nm { font-family: var(--sans); font-weight: 800; font-size: .98rem; }
.fdt-p-role { font-family: var(--sans); font-size: .58rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: 0; color: var(--fdmut); margin-top: .12rem; }
.fdt-p-crit { font-family: var(--serif); font-style: italic; font-size: .76rem; color: var(--fdmut); line-height: 1.4; margin-top: .1rem; }
.fdt-p-rec { margin-left: auto; text-align: right; white-space: nowrap; }
.fdt-p-rec .r { font-family: var(--fdmono); font-weight: 700; font-size: 1.15rem; color: var(--fdtext);
  font-variant-numeric: tabular-nums; }
.fdt-p-rec .r.up { color: var(--fdup); } .fdt-p-rec .r.dn { color: var(--fddn); }
.fdt-p-rec .b { font-family: var(--sans); font-size: .6rem; text-transform: uppercase; letter-spacing: .1em; color: var(--fdmut); }
.fdt-p-calls { border-top: 1px solid var(--fdline); margin-top: .7rem; padding-top: .6rem; }
.fdt-p-call { display: flex; justify-content: space-between; gap: .8rem; font-family: var(--sans); font-size: .74rem;
  color: var(--fdmut); margin: .3rem 0; }
.fdt-p-call .v { font-family: var(--fdmono); font-weight: 700; white-space: nowrap; }
.fdt-p-call .v.up { color: var(--fdup); } .fdt-p-call .v.dn { color: var(--fddn); }
.fdt-p-none { font-family: var(--serif); font-style: italic; font-size: .8rem; color: var(--fdmut);
  border-top: 1px solid var(--fdline); margin-top: .7rem; padding-top: .6rem; }
.fdt-p-cta { display: block; font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: var(--fdup); margin-top: .8rem; }
.fdt-cal { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1.1rem 1.25rem; }
.fdt-cal svg { display: block; width: 100%; height: auto; }
.fdt-cal-leg { display: flex; gap: 1.1rem; flex-wrap: wrap; font-family: var(--sans); font-size: .7rem;
  color: var(--fdmut); margin-top: .7rem; }
.fdt-cal-leg .sw { display: inline-block; width: .7em; height: .7em; border-radius: 50%; margin-right: .3em; }
.fdt-ledger { border: 1px solid var(--fdline); border-radius: 2px; overflow-x: auto; background: var(--fdcard); }
.fdt-ledger table { border-collapse: collapse; width: 100%; font-size: .78rem; }
.fdt-ledger th, .fdt-ledger td { text-align: left; padding: .6rem .8rem; border-bottom: 1px solid var(--fdline);
  vertical-align: top; font-family: var(--sans); color: var(--fdmut); white-space: nowrap; }
.fdt-ledger thead th { font-size: .58rem; text-transform: uppercase; letter-spacing: .1em; background: #0c1117; }
.fdt-ledger tbody tr:last-child td { border-bottom: none; }
.fdt-ledger td a { color: var(--fdtext); text-decoration: none; font-weight: 700; }
.fdt-ledger td a:hover { color: var(--fdblue); }
.fdt-ledger .num { font-family: var(--fdmono); font-variant-numeric: tabular-nums; }
.fdt-ledger .v-hit { color: var(--fdup); font-weight: 800; } .fdt-ledger .v-miss { color: var(--fddn); font-weight: 800; }
.fdt-empty { font-family: var(--serif); font-style: italic; font-size: .95rem; line-height: 1.6; color: var(--fdmut);
  border: 1px dashed var(--fdline); border-radius: 2px; padding: 1.1rem 1.25rem; }
.fdt-pend { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem 1.2rem; }
.fdt-pend a { display: flex; justify-content: space-between; gap: .8rem; align-items: baseline; text-decoration: none;
  border-bottom: 1px dotted var(--fdline); padding: .35rem 0; }
.fdt-pend .t { font-family: var(--sans); font-size: .8rem; font-weight: 600; color: var(--fdtext); }
.fdt-pend a:hover .t { color: var(--fdblue); }
.fdt-pend .w { font-family: var(--fdmono); font-size: .7rem; color: var(--fdmut); white-space: nowrap; }
@media (max-width: 640px) { .fdt-pend { grid-template-columns: 1fr; } }

/* ---- The Book: the roster's fake-money accounts ---- */
.fdt-lead { font-family: var(--serif); font-size: .98rem; line-height: 1.6; color: var(--fdtext); margin: .2rem 0 1rem; }
.fdt-lead .up { color: var(--fdup); font-weight: 700; } .fdt-lead .dn { color: var(--fddn); font-weight: 700; }
.fdt-bk-tbl table { font-variant-numeric: tabular-nums; }
.fdt-bk-tbl td, .fdt-bk-tbl th { white-space: nowrap; }
.fdt-bk-tbl .num.up { color: var(--fdup); font-weight: 700; } .fdt-bk-tbl .num.dn { color: var(--fddn); font-weight: 700; }
.fdt-bk-who { font-family: var(--sans); font-weight: 700; color: var(--fdtext) !important; }
.fdt-bk-who .av { font-size: 1rem; margin-right: .45rem; background: #0c1117; border: 1px solid var(--fdline);
  border-left-width: 3px; border-radius: 2px; padding: .12rem .32rem; }
.fdt-bk-worth { position: relative; min-width: 150px; }
.fdt-bk-worth .wbar { position: absolute; left: 0; top: 50%; transform: translateY(-50%); height: 60%;
  border-radius: 1px; opacity: .18; }
.fdt-bk-worth b { position: relative; color: var(--fdtext); }
.fdt-bk-sub { font-family: var(--sans); font-size: .72rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: .1em; color: var(--fdmut); margin: 1.4rem 0 .7rem; }
.fdt-bk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: .7rem; }
.fdt-bk-pos { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: .7rem .8rem; }
.fdt-bk-pos .who { display: flex; justify-content: space-between; align-items: baseline; gap: .5rem;
  font-family: var(--sans); font-weight: 700; font-size: .82rem; color: var(--fdtext); }
.fdt-bk-pos .who .odds { font-family: var(--fdmono); font-weight: 700; font-size: .92rem; color: var(--fdgold);
  font-variant-numeric: tabular-nums; }
.fdt-bk-pos .mk { font-family: var(--serif); font-style: italic; font-size: .74rem; color: var(--fdmut);
  margin: .25rem 0 .35rem; line-height: 1.3; }
.fdt-bk-pos .call { font-family: var(--sans); font-size: .82rem; font-weight: 600; color: var(--fdtext); }
.fdt-bk-pos .stake { display: flex; justify-content: space-between; gap: .5rem; margin-top: .4rem;
  padding-top: .4rem; border-top: 1px dotted var(--fdline); font-family: var(--fdmono); font-size: .72rem;
  color: var(--fdmut); font-variant-numeric: tabular-nums; }
.fdt-bk-pos .stake .w b { color: var(--fdup); } .fdt-bk-pos .stake b { color: var(--fdtext); }
.fdt-p-bank { display: flex; align-items: baseline; gap: .5rem; border-top: 1px solid var(--fdline);
  margin-top: .7rem; padding-top: .6rem; font-variant-numeric: tabular-nums; }
.fdt-p-bank .lbl { font-family: var(--sans); font-size: .58rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--fdmut); }
.fdt-p-bank .nw { font-family: var(--fdmono); font-weight: 700; font-size: 1rem; color: var(--fdtext); }
.fdt-p-bank .pl { font-family: var(--fdmono); font-size: .76rem; margin-left: auto; color: var(--fdmut); }
.fdt-p-bank .pl.up { color: var(--fdup); font-weight: 700; } .fdt-p-bank .pl.dn { color: var(--fddn); font-weight: 700; }

/* verdict banner on graded detail pages */
.fdd-verdict { border: 1px solid var(--fdup); background: rgba(34,197,94,.07); border-radius: 2px;
  padding: 1.1rem 1.3rem; }
.fdd-verdict.miss { border-color: var(--fddn); background: rgba(239,68,68,.07); }
.fdd-verdict .lbl { display: block; font-family: var(--sans); font-size: .62rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; color: var(--fdup); margin-bottom: .35rem; }
.fdd-verdict.miss .lbl { color: var(--fddn); }
.fdd-verdict p { font-family: var(--serif); font-size: .95rem; line-height: 1.62; color: #c6d0e0; margin: 0; }
.fdd-verdict .src { font-family: var(--sans); font-size: .72rem; margin-top: .5rem; }
.fdd-verdict .src a { color: var(--fdblue); }
.fdd-tag.hit { color: #0c1117; background: var(--fdup); }
.fdd-tag.missed { color: #fff; background: var(--fddn); }

/* The Forecasters — the spectrum hero + richer profile cards */
.fdt-fc-lead { font-family: var(--serif); font-size: 1rem; line-height: 1.68; color: #c6ccd6; margin: 0 0 1.2rem; }
.fdt-spectrum { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px;
  padding: 1.2rem 1.1rem .8rem; overflow-x: auto; }
.fdt-spectrum svg { display: block; width: 100%; height: auto; min-width: 560px; }
.fdt-spx-link { cursor: pointer; }
.fdt-spx-link circle:nth-of-type(2) { transition: r .12s var(--ease); }
.fdt-spx-link:hover circle:nth-of-type(2) { r: 15; }
.fdt-spx-link:hover text { text-decoration: underline; }
.fdt-p-zone { font-family: var(--sans); font-size: .56rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: .12em; padding: .16rem .5rem; border-radius: 999px; border: 1px solid currentColor;
  white-space: nowrap; }
.fdt-p-model { font-family: var(--serif); font-size: .82rem; line-height: 1.56; color: #b8bec9;
  border-top: 1px solid var(--fdline); margin-top: .7rem; padding-top: .6rem; }
.fdt-p-doc { display: flex; gap: .5rem; align-items: baseline; margin-top: .55rem; }
.fdt-p-doc .k { font-family: var(--sans); font-size: .55rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: .11em; color: var(--fdmut); flex: none; padding-top: .1rem; }
.fdt-p-doc .v { font-family: var(--sans); font-size: .8rem; font-weight: 600; color: var(--fdtext); line-height: 1.42; }
.fdt-p-spx { display: flex; align-items: center; gap: .5rem; margin-top: .6rem; }
.fdt-p-spx .track { position: relative; flex: 1; height: 4px; border-radius: 999px;
  background: linear-gradient(90deg, #6ea8ff33, #9aa1af33, #eab30833); }
.fdt-p-spx .pip { position: absolute; top: 50%; width: 10px; height: 10px; border-radius: 50%;
  transform: translate(-50%, -50%); border: 2px solid var(--fdbg); }
.fdt-p-spx .ends { font-family: var(--fdmono); font-size: .5rem; color: var(--fdmut); }
"""

FORECAST_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{page_title} — calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}{accent_css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="fd-plate">
  <p class="fd-kicker">{kicker}</p>
  <h1 class="fd-name">{h1}</h1>
  <p class="fd-motto">“{motto}”</p>
  {scene}
  <div class="fd-folio">
{folio}
  </div>
{plate_extra}
</header>
<main class="fd-board" id="fd-board">
{body}
</main>
<footer class="fd-foot">
  <p class="epigraph">{blurb}</p>
  <p class="colophon">{back}</p>
</footer>
<script>{theme_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

FORECAST_PAGE_JS = r"""
// The bars fill by default (see .fd-fill) — no arming required.
// live countdowns: <span data-grades="YYYY-MM-DD">
document.querySelectorAll('[data-grades]').forEach(el => {
  const d = new Date(el.dataset.grades + 'T23:59:59');
  const days = Math.max(0, Math.ceil((d - Date.now()) / 864e5));
  el.textContent = days === 0 ? 'grades today' : `grades in ${days}d`;
});
"""


# Outcome + category colorways — the Polymarket move: every outcome in a market
# wears its own hue, and every category shelf has a signature accent. Greens stay
# semantic (LIVE / OPEN / CTAs).
FD_OUT_COLORS = ["#22c55e", "#eab308", "#60a5fa", "#f4715c", "#c084fc"]
FD_CAT_COLORS = {
    "Sport & Games": "#22c55e",
    "Media & Advertising": "#2dd4bf",
    "The Modern World": "#f97316",
    "Mind & Philosophy": "#c084fc",
    "Faith & Religion": "#eab308",
    "Thomas Carlyle": "#9bb24f",
    "Heritage": "#f472b6",
}


def _fd_cat_color(cat):
    return FD_CAT_COLORS.get(cat) or FD_OUT_COLORS[sum(map(ord, cat or "")) % len(FD_OUT_COLORS)]


def _fd_outcome_rows(outcomes, cap=5, resolved_idx=None):
    """Outcome rows for a market card: name + a bar that FILLS from zero to the
    outcome's probability (Polymarket-style, 0–100 axis), the band's uncertainty
    shown as a lighter extension. Each outcome wears its own colorway. On a
    graded market (resolved_idx set) the winning outcome is stamped ✓ and the
    rest ✗-dimmed — the priced bars stay, as the record of what the desk said."""
    rows = []
    show = outcomes[:cap]
    if resolved_idx is not None and resolved_idx >= cap:
        show = outcomes[:cap - 1] + [outcomes[resolved_idx]]
    for i, o in enumerate(show):
        oi = outcomes.index(o)
        low = max(min(o["low"], 100.0), 0.0)
        high = max(min(o["high"], 100.0), low)
        rng = high - low
        col = FD_OUT_COLORS[oi % len(FD_OUT_COLORS)]
        lead = " lead" if oi == 0 else ""
        won = resolved_idx is not None and oi == resolved_idx
        lost = resolved_idx is not None and oi != resolved_idx
        bars = f'<span class="fd-fill" style="width:{max(low, 1.5):.1f}%;background:{col}"></span>'
        if rng >= .5:
            bars += f'<span class="fd-fill rng" style="left:{low:.1f}%;width:{rng:.1f}%;background:{col}"></span>'
        mark = '<span class="fd-mk won">✓</span>' if won else ('<span class="fd-mk lost">✗</span>' if lost else "")
        pc = (f'<span class="pc" style="background:{col};color:#0c1117">{_fmt_band(o["low"], o["high"])}</span>'
              if oi == 0 else
              f'<span class="pc" style="color:{col}">{_fmt_band(o["low"], o["high"])}</span>')
        rows.append(
            f'<div class="fd-out{lead}{" won" if won else ""}{" lost" if lost else ""}"><div class="fd-out-l">'
            f'{mark}<span class="nm">{html.escape(o["name"])}</span>{pc}</div>'
            f'<div class="fd-track">{bars}</div></div>'
        )
    extra = len(outcomes) - cap
    if extra > 0:
        rows.append(f'<div class="fd-out-l" style="margin-top:.1rem"><span class="nm">+{extra} more outcomes in the corpus</span></div>')
    return "".join(rows)


def _fd_market_card(m):
    hz = html.escape(m["horizon"]) if m["horizon"] else ""
    cc = _fd_cat_color(m["category"])
    cover = (f'<img class="fd-card-cover" src="{html.escape(m["cover"], quote=True)}" alt="" loading="lazy">'
             if m.get("cover") else "")
    dek = (f'<p class="fd-card-sub">{html.escape(m["description"])}</p>'
           if m.get("description") else "")
    r = m.get("resolution")
    if r:
        verdict = ("✓ the desk's lead call hit" if r["lead_hit"] else "✗ the desk's lead call missed")
        vcol = "var(--fdup)" if r["lead_hit"] else "var(--fddn)"
        chip = f'<span class="fd-chip-graded{"" if r["lead_hit"] else " miss"}">{"✓" if r["lead_hit"] else "✗"} Graded</span>'
        top_right = html.escape(_long_date(r["resolved"]) if r.get("resolved") else "resolved")
        foot = (f'<div class="fd-card-foot"><span style="color:{vcol}">{verdict} · Brier {r["brier"]:.3f}</span>'
                f'<span class="go" style="color:{cc}">The grade →</span></div>')
    else:
        chip = '<span class="fd-chip-open">Open</span>'
        top_right = hz
        foot = (f'<div class="fd-card-foot"><span>{len(m["outcomes"])} outcomes · from the research</span>'
                f'<span class="go" style="color:{cc}">Full forecast →</span></div>')
    return (
        f'<a class="fd-card{" graded" if r else ""}" href="{html.escape(m["href"], quote=True)}" style="border-top:3px solid {cc}">'
        f'<div class="fd-card-top">{cover}{chip}'
        f'<span class="fd-card-hz">{top_right}</span></div>'
        f'<h3 class="fd-card-q">{html.escape(m["title"])}</h3>{dek}'
        f'{_fd_outcome_rows(m["outcomes"], resolved_idx=r["idx"] if r else None)}'
        f'{foot}</a>'
    )


def _fd_live_hero(f):
    """The featured native market — a live board lead with the consensus pick.
    Once graded (f['_graded'] set by the resolutions ledger) the same hero turns
    into the verdict: winner named, the council's call stamped hit or miss."""
    q = html.escape(f.get("question") or f.get("title") or "Live forecast")
    cat = html.escape(f.get("category", ""))
    pick = html.escape(f.get("pick", ""))
    flagc = f.get("pick_flag", "")
    band = html.escape(f.get("band", ""))
    dek = html.escape(f.get("dek", ""))
    logged = html.escape(_long_date(f["logged"]) if f.get("logged") else "")
    grades = f.get("grades", "")
    profiles_n = f.get("profiles_n")
    prof_bit = f"<span>👥 <b>{profiles_n} predictor profiles</b></span>" if profiles_n else ""
    href = html.escape(f.get("file") or f"forecast/{f['slug']}.html", quote=True)
    g = f.get("_graded")
    if g:
        c = g["consensus"]
        hit = c["hit"]
        chip = (f'<span class="fd-chip-graded{"" if hit else " miss"}">{"✓" if hit else "✗"} Graded — '
                f'the desk {"called it" if hit else "missed"}</span>')
        graded_on = html.escape(_long_date(g["resolved"]) if g.get("resolved") else "")
        meta = (f'{prof_bit}<span>🏁 <b>graded {graded_on}</b></span>'
                f'<span>📉 <b>Brier {c["brier"]:.3f}</b></span>')
        winner_bit = (f'<div class="fd-live-pick won"><div class="f">{g.get("winner_flag", "") or "🏆"}</div>'
                      f'<div class="t">{html.escape(g["winner"])}</div><div class="p">won</div></div>')
        sub = html.escape(g.get("note", "")) or dek
        return (
            f'<a class="fd-live graded" href="{href}">'
            f'<div class="fd-live-top">{chip}'
            f'<span class="fd-chip-cat" style="color:{_fd_cat_color(f.get("category", ""))};border-color:{_fd_cat_color(f.get("category", ""))}">{cat}</span>'
            f'<span class="fd-chip-date">logged {logged}</span></div>'
            f'<h2 class="fd-live-q">{q}</h2>'
            f'<div class="fd-live-grid graded">{winner_bit}'
            f'<div class="fd-live-pick called{"" if hit else " missed"}">'
            f'<div class="f">{flagc}</div><div class="t">{pick}</div><div class="p">called {band}</div></div>'
            f'<div><p class="fd-live-sub">{sub}</p>'
            f'<div class="fd-live-meta">{meta}</div>'
            f'</div></div></a>'
        )
    grades_bit = (f'<span>⏳ <b data-grades="{html.escape(grades, quote=True)}">grades {html.escape(_long_date(grades))}</b></span>'
                  if grades else "")
    return (
        f'<a class="fd-live" href="{href}">'
        f'<div class="fd-live-top"><span class="fd-chip-live"><span class="d"></span>Live market</span>'
        f'<span class="fd-chip-cat" style="color:{_fd_cat_color(f.get("category", ""))};border-color:{_fd_cat_color(f.get("category", ""))}">{cat}</span>'
        f'<span class="fd-chip-date">logged {logged}</span></div>'
        f'<h2 class="fd-live-q">{q}</h2>'
        f'<div class="fd-live-grid"><div class="fd-live-pick">'
        f'<div class="f">{flagc}</div><div class="t">{pick}</div><div class="p">{band}</div></div>'
        f'<div><p class="fd-live-sub">{dek}</p>'
        f'<div class="fd-live-meta">{prof_bit}{grades_bit}<span>📋 <b>on the ledger</b></span></div>'
        f'</div></div></a>'
    )


def _fd_tape(native_items, markets):
    """The ticker tape: every market's leading outcome as one tick; graded
    markets tick their verdict instead."""
    ticks = []
    for f in native_items:
        g = f.get("_graded")
        if g:
            hit = g["consensus"]["hit"]
            cls = "up" if hit else "dn"
            ticks.append(f'<span class="fd-tk"><b>{html.escape(f.get("title", f["slug"]))}</b> · '
                         f'<span class="{cls}">{"✓" if hit else "✗"} {html.escape((g.get("winner_flag", "") + " " + g["winner"]).strip())} won'
                         f' — desk {"called it" if hit else "missed"}</span></span>')
        elif f.get("pick"):
            ticks.append(f'<span class="fd-tk"><b>{html.escape(f.get("title", f["slug"]))}</b> · '
                         f'<span class="up">{html.escape((f.get("pick_flag", "") + " " + f["pick"]).strip())} {html.escape(f.get("band", ""))}</span></span>')
    for m in markets:
        r = m.get("resolution")
        if r:
            cls = "up" if r["lead_hit"] else "dn"
            ticks.append(f'<span class="fd-tk"><b>{html.escape(m["title"])}</b> · '
                         f'<span class="{cls}">{"✓" if r["lead_hit"] else "✗"} {html.escape(r["name"][:46])}'
                         f' — {"called" if r["lead_hit"] else "missed"}</span></span>')
            continue
        o = m["outcomes"][0]
        cc = _fd_cat_color(m["category"])
        ticks.append(f'<span class="fd-tk"><b>{html.escape(m["title"])}</b> · '
                     f'{html.escape(o["name"][:46])} <span class="up" style="color:{cc}">{_fmt_band(o["low"], o["high"])}</span></span>')
    if not ticks:
        return ""
    row = "".join(ticks)
    # A calm crawl: ~8s per tick so a full loop of a 30-market board takes ~4min.
    dur = max(120, len(ticks) * 8)
    return (f'<div class="fd-tape" aria-hidden="true">'
            f'<div class="fd-tape-inner" style="animation-duration:{dur}s">{row}{row}</div></div>')


def _fd_mini_roster(led, book):
    """A compact predictor strip pinned atop every board (site-wide and each
    desk's own) — one small card per standing persona: avatar, name, cumulative
    record, current bankroll. Each card links straight to that predictor's full
    profile, and the strip's own header links to the side-by-side comparison
    page, so a visitor can size up or drill into any predictor before scrolling
    past a single market."""
    cards = []
    for key, name, avatar, criterion in FD_PERSONAS:
        p = led["personas"].get(key) or {"graded": 0, "hits": 0, "briers": []}
        acct = book["personas"].get(key)
        col = FDT_SERIES_COLORS.get(key, "#9aa1af")
        agent = _persona_agent_name(key, name)
        role = _persona_role(key, name)
        losses = p["graded"] - p["hits"]
        rec = f'{p["hits"]}–{losses}' if p["graded"] else "0–0"
        bank = f'<span class="fdm-bank">{_fd_money(acct["bankroll"])}</span>' if acct else ""
        cards.append(
            f'<a class="fdm-card" href="forecasters/{key}.html" style="border-top-color:{col}" '
            f'title="{html.escape(agent + " — " + criterion, quote=True)}">'
            f'{_persona_mascot_html(key, "mini")}'
            f'<span class="fdm-nm">{html.escape(agent)}</span>'
            f'<span class="fdm-role">{html.escape(role)}</span>'
            f'<span class="fdm-rec">{rec}</span>{bank}</a>')
    return (
        '<div class="fdm-roster">'
        '<div class="fdm-roster-head"><span>The Predictors</span>'
        '<a class="fdm-cmp" href="forecasters/compare.html">Compare all →</a></div>'
        f'<div class="fdm-roster-row">{"".join(cards)}</div>'
        '</div>'
    )


FORECAST_NAV_DEFAULT = main_nav_html(active="forecast.html")


def build_forecast_page(out_dir, native_items, markets, cfg, category_order=None, shell="", page=None, native_data=None, sections=None):
    """Render a Forecast board — docs/forecast.html by default: ticker, live
    native markets, then every harvested corpus market shelved by category
    (graded markets shelve last on their shelf, wearing their verdict).
    A `page` override scopes the board to a detached desk's own edition (e.g.
    the Ad Tech Board): {fname, title (h1), kicker, nav, back, record_fname}.
    `sections`, when given, splits the board into labeled super-section bands
    instead of one flat run of shelves — an ordered list of section dicts:
    {title, kicker, color, native:True} pins the standalone native heroes; a
    {title, kicker, color, categories:[...]} or {..., rest:True} band groups
    the category shelves it claims (rest = every category no other band took)."""
    out = Path(out_dir)
    page = page or {}
    fname = page.get("fname", "forecast.html")
    h1 = page.get("title", "The Forecast Desk")
    kicker = page.get("kicker", "Predictions, by category")
    nav = page.get("nav", FORECAST_NAV_DEFAULT)
    back = page.get("back", '<a href="research.html">← Back to the Research Library</a>')
    record_fname = page.get("record_fname", "forecast-record.html")
    accent_css = _accent_css(page.get("accent"))
    category_order = category_order or []
    led = build_forecast_ledger(native_items, markets)
    book = build_book(native_items, markets, native_data)
    parts = [_fd_tape(native_items, markets), _fd_mini_roster(led, book)]
    # Shelve harvested markets by category, config order first, then first-seen.
    cats = []
    for c in category_order:
        if any(m["category"] == c for m in markets) and c not in cats:
            cats.append(c)
    for m in markets:
        if m["category"] not in cats:
            cats.append(m["category"])

    def _shelf(c):
        group = sorted([m for m in markets if m["category"] == c],
                       key=lambda m: bool(m.get("resolution")))
        cards = "".join(_fd_market_card(m) for m in group)
        cc = _fd_cat_color(c)
        return (f'<h2 class="fd-cat-h"><span class="tick" style="background:{cc}"></span>'
                f'{html.escape(c)} <span class="n" style="color:{cc}">{len(group)}</span></h2>'
                f'<div class="fd-grid">{cards}</div>', len(group))

    if sections:
        claimed = {c for sec in sections for c in sec.get("categories", [])}
        for sec in sections:
            band, n = [], 0
            if sec.get("native"):
                band = [_fd_live_hero(f) for f in native_items]
                n = len(native_items)
            else:
                sec_cats = ([c for c in cats if c not in claimed] if sec.get("rest")
                            else [c for c in cats if c in sec.get("categories", [])])
                for c in sec_cats:
                    shtml, cnt = _shelf(c)
                    band.append(shtml)
                    n += cnt
            if not band:
                continue
            col = sec.get("color", "#9aa1af")
            parts.append(
                f'<div class="fd-sec"><span class="fd-sec-bar" style="background:{col}"></span>'
                f'<h2 class="fd-sec-t">{html.escape(sec["title"])}</h2>'
                f'<span class="fd-sec-k">{html.escape(sec.get("kicker", ""))}</span>'
                f'<span class="fd-sec-n">{n} {"market" if n == 1 else "markets"}</span></div>')
            parts.extend(band)
    else:
        for f in native_items:
            parts.append(_fd_live_hero(f))
        for c in cats:
            parts.append(_shelf(c)[0])
    n_outcomes = sum(len(m["outcomes"]) for m in markets) + len(native_items)
    n_markets = len(native_items) + len(markets)
    # The plate's graded line + the standing link to the track record.
    n_graded = (sum(1 for f in native_items if f.get("_graded"))
                + sum(1 for m in markets if m.get("resolution")))
    hits = (sum(1 for f in native_items if f.get("_graded") and f["_graded"]["consensus"]["hit"])
            + sum(1 for m in markets if m.get("resolution") and m["resolution"]["lead_hit"]))
    folio = (f'    <span>{n_markets} markets</span>\n'
             f'    <span class="fd-folio-c">{n_outcomes} priced outcomes</span>\n'
             f'    <span>{len(cats) + (1 if native_items else 0)} categories</span>')
    if n_graded:
        folio += (f'\n    <span class="fd-folio-g">{n_graded} graded · '
                  f'record {hits}–{n_graded - hits}</span>')
    if book["settled"] and book["leader"]:
        folio += (f'\n    <span class="fd-folio-g">book leader {html.escape(book["leader"]["name"])} '
                  f'{_fd_money(book["leader"]["bankroll"])}</span>')
    elif book["at_risk"]:
        folio += f'\n    <span>the book: {_fd_money(book["at_risk"])} at risk</span>'
    plate_extra = (f'  <a class="fd-rec-link" href="{html.escape(record_fname, quote=True)}">'
                   f'📒 The Track Record — the roster’s bankrolls, every graded call scored →</a>')
    og = og_tags(h1,
                 cfg.get("motto", "Every prediction the research makes, priced and graded."),
                 f"{SITE_URL}/{fname}", f"{SITE_URL}/{OG_IMAGE}")
    page_html = FORECAST_PAGE_TEMPLATE.format(
        page_title=html.escape(h1), h1=html.escape(h1), kicker=html.escape(kicker),
        nav=nav, back=back,
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_PAGE_CSS,
        accent_css=_accent_css(page.get("accent")),
        favicon=FAVICON, og_meta=og,
        motto=html.escape(cfg.get("motto", "")),
        blurb=html.escape(cfg.get("blurb", "")),
        folio=folio, plate_extra=plate_extra,
        scene=scene_plate("forecast", extra_class="page-scene", seed=f"forecast-front:{fname}"),
        body="\n".join(parts),
        theme_js=LIBRARY_THEME_JS,
        app_js=FORECAST_PAGE_JS,
        shell=shell,
    )
    (out / fname).write_text(_persona_public_copy(page_html))
    print(f"  ✓ {h1}  ({n_markets} markets, {n_outcomes} outcomes, {n_graded} graded) → {fname}")


# ------------------------------------------------------------- the track record
# The accountability page: every graded call scored, the standing personas'
# cumulative records, a calibration diagram, and the open positions still
# awaiting their grade. Site-wide (forecast-record.html) and one per detached
# desk that runs its own board ({slug}-record.html).

FDT_SERIES_COLORS = {
    "council": "#eab308", "research": "#60a5fa",
    "market-reader": "#22c55e", "quant": "#c084fc", "historian": "#f4715c",
    "path-reader": "#2dd4bf", "talisman": "#f472b6", "contrarian": "#9bb24f",
    "prophet": "#e2e8f0",
}


def _fdt_series_color(call):
    if call["kind"] == "persona":
        key = call.get("key") or FD_PERSONA_ALIASES.get(call.get("caller", "").casefold())
        key = key or re.sub(r"[^a-z0-9]+", "-", call["caller"].casefold().removeprefix("the ")).strip("-")
        return FDT_SERIES_COLORS.get(key, "#9aa1af")
    return FDT_SERIES_COLORS.get(call["kind"], "#9aa1af")


def _fdt_calibration_svg(calls):
    """A reliability diagram as inline SVG: every graded call is one dot at
    (stated probability, what happened), the dashed diagonal is perfect
    calibration, and once enough calls accumulate a binned frequency line
    (gold) shows where the desk actually sits. Sparse-safe: dots stack."""
    X0, X1, Y0, Y1 = 60, 620, 375, 25   # plot box; y inverted (0% at bottom)
    def px(p):
        return X0 + (X1 - X0) * p / 100.0
    def py(v):
        return Y0 + (Y1 - Y0) * v / 100.0
    grid = []
    for t in (0, 25, 50, 75, 100):
        grid.append(f'<line x1="{px(t):.0f}" y1="{Y0}" x2="{px(t):.0f}" y2="{Y1}" stroke="#2c303a" stroke-width="1"/>')
        grid.append(f'<line x1="{X0}" y1="{py(t):.0f}" x2="{X1}" y2="{py(t):.0f}" stroke="#2c303a" stroke-width="1"/>')
        grid.append(f'<text x="{px(t):.0f}" y="{Y0 + 18}" text-anchor="middle" fill="#9aa1af" font-size="11" font-family="ui-monospace,Menlo,monospace">{t}%</text>')
        grid.append(f'<text x="{X0 - 10}" y="{py(t) + 4:.0f}" text-anchor="end" fill="#9aa1af" font-size="11" font-family="ui-monospace,Menlo,monospace">{t}%</text>')
    diag = (f'<line x1="{px(0):.0f}" y1="{py(0):.0f}" x2="{px(100):.0f}" y2="{py(100):.0f}" '
            f'stroke="#9aa1af" stroke-width="1.2" stroke-dasharray="5 5" opacity=".7"/>')
    labels = (
        f'<text x="{(X0 + X1) / 2:.0f}" y="{Y0 + 36}" text-anchor="middle" fill="#9aa1af" font-size="11.5" '
        f'font-family="-apple-system,sans-serif">stated probability at log time →</text>'
        f'<text x="16" y="{(Y0 + Y1) / 2:.0f}" text-anchor="middle" fill="#9aa1af" font-size="11.5" '
        f'font-family="-apple-system,sans-serif" transform="rotate(-90 16 {(Y0 + Y1) / 2:.0f})">observed frequency →</text>'
        f'<text x="{px(72):.0f}" y="{py(78):.0f}" text-anchor="middle" fill="#9aa1af" font-size="10.5" '
        f'font-style="italic" font-family="Georgia,serif" transform="rotate(-29 {px(72):.0f} {py(78):.0f})">perfect calibration</text>'
    )
    dots, stacks = [], {}
    for c in calls:
        bucket = (round(c["prob"] / 4), c["hit"])          # stack near-identical dots
        k = stacks.get(bucket, 0)
        stacks[bucket] = k + 1
        y = py(97 - k * 4.5) if c["hit"] else py(3 + k * 4.5)
        col = _fdt_series_color(c)
        tip = f'{c["caller"]}: {c["call"]} @ {c["prob"]:g}% — {"hit" if c["hit"] else "miss"} ({c["market"]})'
        dots.append(f'<circle cx="{px(c["prob"]):.1f}" cy="{y:.1f}" r="5.5" fill="{col}" '
                    f'stroke="#0c1117" stroke-width="1.2" opacity=".92"><title>{html.escape(tip)}</title></circle>')
    curve = ""
    if len(calls) >= 8:
        pts = []
        for b0 in range(0, 100, 20):
            binned = [c for c in calls if b0 <= c["prob"] < b0 + 20 or (b0 == 80 and c["prob"] == 100)]
            if len(binned) >= 2:
                freq = sum(1 for c in binned if c["hit"]) / len(binned) * 100
                mid = sum(c["prob"] for c in binned) / len(binned)
                pts.append((px(mid), py(freq)))
        if len(pts) >= 2:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            curve = (f'<polyline points="{path}" fill="none" stroke="#eab308" stroke-width="2.2"/>'
                     + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#eab308"/>' for x, y in pts))
    empty = ("" if calls else
             f'<text x="{(X0 + X1) / 2:.0f}" y="{(Y0 + Y1) / 2 - 8:.0f}" text-anchor="middle" fill="#9aa1af" '
             f'font-size="13" font-style="italic" font-family="Georgia,serif">The diagram draws itself as calls grade.</text>'
             f'<text x="{(X0 + X1) / 2:.0f}" y="{(Y0 + Y1) / 2 + 14:.0f}" text-anchor="middle" fill="#9aa1af" '
             f'font-size="11" font-family="-apple-system,sans-serif">A well-calibrated desk lands its dots along the diagonal.</text>')
    return (f'<svg viewBox="0 0 660 420" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Calibration diagram: stated probability vs observed frequency">'
            f'{"".join(grid)}{diag}{labels}{curve}{"".join(dots)}{empty}</svg>')


def _fdt_fmt_brier(briers):
    return f"{sum(briers) / len(briers):.3f}" if briers else "—"


def _fdt_book_section(book, n):
    """The Book section for the track record: a fake-money standings table (net
    worth, P&L, ROI, record, stake at risk) over the roster, a plain-English note
    on the mechanic, and the live open positions sorted by potential payout —
    the longshots the roster is backing against the book, biggest score first."""
    ranked = book["ranked"]
    start = book["start"]
    maxworth = max((a["bankroll"] for a in ranked), default=start) or start
    trows = []
    for i, a in enumerate(ranked, 1):
        pnl, roi = a["pnl"], (a["pnl"] / a["staked"] * 100) if a["staked"] else 0.0
        pcls = "up" if pnl > 0.5 else ("dn" if pnl < -0.5 else "")
        wl = f'{a["wins"]}–{a["settled"] - a["wins"]}' if a["settled"] else "—"
        barw = max(4.0, a["bankroll"] / maxworth * 100)
        pnl_txt = _fd_signed(pnl) if a["settled"] else "—"
        roi_txt = f'{roi:+.0f}%' if a["staked"] else "—"
        trows.append(
            f'<tr><td class="num">{i}</td>'
            f'<td class="fdt-bk-who">{_persona_mascot_html(a["key"], "table")}'
            f'{html.escape(a["name"])}<br><span style="color:var(--fdmut);font-size:.62rem;text-transform:uppercase;letter-spacing:0">'
            f'{html.escape(_persona_role(a["key"], ""))}</span></td>'
            f'<td class="fdt-bk-worth"><span class="wbar" style="width:{barw:.0f}%;background:{a["color"]}"></span>'
            f'<b class="num">{_fd_money(a["bankroll"])}</b></td>'
            f'<td class="num {pcls}">{pnl_txt}</td>'
            f'<td class="num {pcls}">{roi_txt}</td>'
            f'<td class="num">{wl}</td>'
            f'<td class="num">{_fd_money(a["at_risk"]) if a["at_risk"] else "—"}</td></tr>')
    tbl = (f'<div class="fdt-ledger fdt-bk-tbl"><table><thead><tr><th>#</th><th>Predictor</th>'
           f'<th>Net worth</th><th>P&amp;L</th><th>ROI</th><th>Record</th><th>At risk</th>'
           f'</tr></thead><tbody>{"".join(trows)}</tbody></table></div>')
    positions = sorted([p for a in ranked for p in a["open"] if not p["pass"]],
                       key=lambda p: p["to_win"], reverse=True)
    pcards = []
    for p in positions[:12]:
        col = FDT_SERIES_COLORS.get(p["key"], "#9aa1af")
        call = html.escape((p.get("flag", "") + " " + p["pick"]).strip())
        pcards.append(
            f'<div class="fdt-bk-pos" style="border-left:3px solid {col}">'
            f'<div class="who"><span>{_persona_mascot_html(p["key"], "table")} {html.escape(p["name"])}</span>'
            f'<span class="odds">{p["dec_odds"]:.1f}×</span></div>'
            f'<div class="mk">{html.escape(p["market"])}</div>'
            f'<div class="call">{call}</div>'
            f'<div class="stake"><span class="s">stakes <b>{_fd_money(p["stake"])}</b></span>'
            f'<span class="w">to win <b>{_fd_money(p["to_win"])}</b></span></div></div>')
    more = len(positions) - len(pcards)
    pos_html = ((f'<h3 class="fdt-bk-sub">Live positions — the roster’s open bets, longest payout first</h3>'
                 f'<div class="fdt-bk-grid">{"".join(pcards)}</div>'
                 + (f'<p class="fdt-note">+{more} more open position(s) on the board.</p>' if more > 0 else ""))
                if pcards else "")
    if book["settled"]:
        lead = book["leader"]
        summ = (f'The book has settled {book["settled"]} bet(s). Out front: '
                f'{_persona_mascot_html(lead["key"], "table")} <b>{html.escape(lead["name"])}</b> at {_fd_money(lead["bankroll"])} '
                f'(<span class="{"up" if lead["pnl"] >= 0 else "dn"}">{_fd_signed(lead["pnl"])}</span>).')
    else:
        summ = (f'The book is open and even — every predictor holds {_fd_money(start)}. '
                f'{book["open_n"]} live bet(s), {_fd_money(book["at_risk"])} staked at risk; '
                f'the first settles when its market grades.')
    note = ('<p class="fdt-note">Each predictor opens with $1,000, plays every market it has a pick on, and '
            'every bet pays at the market’s own decimal odds — but each one <b>sizes in its own style, drawn '
            'from where it sits on the spectrum</b>. Quinn Ratio goes half-Kelly on an edge and antes small without '
            'one; Ada Ledger stakes the same flat unit every time; Mira Tape rides the tape for token '
            'money; Elias Lantern and Nico Tilt swing for the fences on long odds. A win pays the decimal odds, '
            'a loss costs the stake, and bankrolls compound — so the standings are the running verdict on rigor '
            'versus revelation.</p>')
    return (f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Book — the roster bets fake money at the market’s odds</h2>'
            f'<p class="fdt-lead">{summ}</p>{tbl}{note}{pos_html}')


def _fdt_spectrum_svg():
    """The roster laid out on one axis — mathematical rigor → intuition → creative
    hypothesis — each persona a marker at its `spectrum` position, colored to match
    its series everywhere else on the page. Labels stagger above/below to breathe."""
    W, H = 660, 208
    x0, x1, ax = 42, 618, 116
    width = x1 - x0
    def X(s):
        return x0 + max(0.0, min(1.0, s)) * width
    zb1, zb2 = 0.385, 0.72
    zones = [(0.0, zb1, "MATHEMATICAL RIGOR", "#6ea8ff"),
             (zb1, zb2, "INTUITION & READING", "#9aa1af"),
             (zb2, 1.0, "CREATIVE HYPOTHESIS", "#eab308")]
    parts = []
    for a, b, lbl, col in zones:
        parts.append(f'<rect x="{X(a):.0f}" y="30" width="{X(b) - X(a):.0f}" height="152" fill="{col}" opacity=".06"/>')
        parts.append(f'<text x="{(X(a) + X(b)) / 2:.0f}" y="22" text-anchor="middle" fill="{col}" opacity=".9" '
                     f'font-family="-apple-system,sans-serif" font-size="10.5" font-weight="700" letter-spacing="1.4">{lbl}</text>')
    for zb in (zb1, zb2):
        parts.append(f'<line x1="{X(zb):.0f}" y1="30" x2="{X(zb):.0f}" y2="182" stroke="#2c303a" stroke-width="1" stroke-dasharray="3 4"/>')
    parts.append(f'<line x1="{x0}" y1="{ax}" x2="{x1}" y2="{ax}" stroke="#3a3f4b" stroke-width="1.5"/>')
    parts.append(f'<text x="{x0}" y="{ax + 24}" fill="#9aa1af" font-family="ui-monospace,monospace" font-size="9">← trust the number</text>')
    parts.append(f'<text x="{x1}" y="{ax + 24}" text-anchor="end" fill="#9aa1af" font-family="ui-monospace,monospace" font-size="9">trust the story →</text>')
    # Stagger labels along SPECTRUM order (not roster order) so two personas that
    # sit close on the axis never share a row and collide.
    ordered = sorted(FD_PERSONAS, key=lambda t: FD_PERSONA_PROFILES.get(t[0], {}).get("spectrum", 0.5))
    for i, (key, name, av, _crit) in enumerate(ordered):
        s = FD_PERSONA_PROFILES.get(key, {}).get("spectrum", 0.5)
        col = FDT_SERIES_COLORS.get(key, "#9aa1af")
        agent = _persona_agent_name(key, name)
        role = _persona_role(key, name)
        mark = _persona_monogram(key, name)
        x = X(s)
        above = (i % 2 == 0)
        ny = ax - 32 if above else ax + 40
        y_conn = ny + (11 if above else -13)
        parts.append(f'<line x1="{x:.0f}" y1="{ax}" x2="{x:.0f}" y2="{y_conn:.0f}" stroke="{col}" stroke-width="1" opacity=".4"/>')
        parts.append(
            f'<a href="forecasters/{key}.html" class="fdt-spx-link">'
            f'<title>{html.escape(agent)} — {html.escape(role)} · {html.escape(_persona_mascot_name(key))}</title>'
            f'<rect x="{x - 16:.0f}" y="{ax - 16:.0f}" width="32" height="32" rx="2" fill="transparent"/>'
            f'<rect x="{x - 13:.0f}" y="{ax - 13:.0f}" width="26" height="26" rx="2" fill="#10161f" stroke="{col}" stroke-width="2"/>'
            f'<text x="{x:.0f}" y="{ax + 4:.0f}" text-anchor="middle" fill="{col}" '
            f'font-family="ui-monospace,monospace" font-size="9.5" font-weight="800">{html.escape(mark)}</text>'
            f'<text x="{x:.0f}" y="{ny:.0f}" text-anchor="middle" fill="{col}" '
            f'font-family="-apple-system,sans-serif" font-size="9.5" font-weight="700">{html.escape(agent)}</text>'
            f'</a>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="The forecaster roster arranged from mathematical rigor to creative hypothesis">'
            f'{"".join(parts)}</svg>')


def _fdt_forecasters_section(n):
    """The lead section of the Track Record: who the forecasters are, laid out as a
    spectrum from rigor to revelation. The per-persona detail (model, doctrine,
    live bankroll) rides on the roster cards further down."""
    intro = ("Seven forecasters work the desk, and they do not think alike. They sit on a single spectrum — "
             "from the calculators who build a number out of parts, through the readers of route and "
             "character, to the imaginers who back a story over a price. Each now bets play money in its own "
             "style, drawn from where it stands. The standings below are the spectrum arguing with itself.")
    return (f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Forecasters — one desk, across the spectrum</h2>'
            f'<p class="fdt-fc-lead">{intro}</p>'
            f'<div class="fdt-spectrum">{_fdt_spectrum_svg()}</div>')


def build_record_page(out_dir, native_items, markets, cfg, shell="", page=None, native_data=None):
    """Render a Track Record page from the graded ledger: desk summary, the
    standing predictor roster with cumulative records and Brier scores, the
    calibration diagram, every graded call as a ledger table, and the open
    positions still awaiting a grade. `page` scopes it to a detached desk:
    {fname, title, kicker, nav, back, board_href, board_title}."""
    out = Path(out_dir)
    page = page or {}
    fname = page.get("fname", "forecast-record.html")
    h1 = page.get("title", "The Track Record")
    kicker = page.get("kicker", "The desk, graded call by call")
    nav = page.get("nav", FORECAST_NAV_DEFAULT)
    board_href = page.get("board_href", "forecast.html")
    board_title = page.get("board_title", "The Forecast Desk")
    back = page.get("back", f'<a href="{board_href}">← Back to {board_title}</a>')
    led = build_forecast_ledger(native_items, markets)
    book = build_book(native_items, markets, native_data)
    bet_lookup = {(b["name"].casefold(), b["market"]): b
                  for a in book["personas"].values() for b in a["bets"]}
    desk, calls, pending = led["desk"], led["calls"], led["pending"]
    n_open = len(pending)
    parts = []
    # 01 — the forecasters, laid out on the spectrum.
    parts.append(_fdt_forecasters_section(1))
    # 02 — the desk's own record.
    rec_txt = f'{desk["hits"]}–{desk["graded"] - desk["hits"]}' if desk["graded"] else "0–0"
    hitrate = f'{desk["hits"] / desk["graded"] * 100:.0f}%' if desk["graded"] else "—"
    next_due = next((p for p in pending if p.get("due")), None)
    parts.append('<h2 class="fdd-h"><span class="n">02</span>The Desk Record — the house call on every graded market</h2>')
    parts.append(
        f'<div class="fdt-sum">'
        f'<div class="fdt-stat"><div class="big">{desk["graded"]}</div><div class="lbl">markets graded</div></div>'
        f'<div class="fdt-stat"><div class="big {"up" if desk["hits"] * 2 >= desk["graded"] and desk["graded"] else ""}">{rec_txt}</div><div class="lbl">record, hits–misses</div></div>'
        f'<div class="fdt-stat"><div class="big">{hitrate}</div><div class="lbl">hit rate</div></div>'
        f'<div class="fdt-stat"><div class="big gold">{_fdt_fmt_brier(desk["briers"])}</div><div class="lbl">mean Brier score</div></div>'
        f'<div class="fdt-stat"><div class="big">{n_open}</div><div class="lbl">open positions</div></div>'
        f'</div>')
    if not desk["graded"]:
        opens_line = (f' The first market <b data-grades="{html.escape(next_due["due"], quote=True)}">'
                      f'grades {html.escape(_long_date(next_due["due"]))}</b>.' if next_due and next_due.get("due") else "")
        parts.append(f'<p class="fdt-note">The ledger is open and nothing has been graded yet — every call below is '
                     f'frozen at log time and will be scored against the official result, no retro-edits.{opens_line}</p>')
    parts.append('<p class="fdt-note">Brier score = (stated probability − what happened)², averaged over calls. '
                 '0 is clairvoyance, 0.25 is a coin flip, 1 is confident wrongness. Lower is better.</p>')
    n = 3
    # 03 — The Book: the roster's fake-money accounts, bet at the market's odds.
    parts.append(_fdt_book_section(book, n))
    n += 1
    # 04 — the standing personas' cumulative records.
    personas = [p for p in led["personas"].values() if p["graded"] or p["criterion"]]
    if personas:
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Standing Roster — cumulative, market over market</h2>')
        cards = []
        for p in personas:
            col = FDT_SERIES_COLORS.get(p["key"], "#9aa1af")
            prof = FD_PERSONA_PROFILES.get(p["key"], {})
            model_html = f'<div class="fdt-p-model">{html.escape(prof["model"])}</div>' if prof.get("model") else ""
            doc_html = (f'<div class="fdt-p-doc"><span class="k">How it bets</span>'
                        f'<span class="v">{html.escape(prof["doctrine"])}</span></div>') if prof.get("doctrine") else ""
            if prof.get("spectrum") is not None:
                sx = max(0.0, min(1.0, prof["spectrum"])) * 100
                spx_html = (f'<div class="fdt-p-spx"><span class="ends">rigor</span>'
                            f'<span class="track"><span class="pip" style="left:{sx:.0f}%;background:{col}"></span></span>'
                            f'<span class="ends">story</span></div>')
            else:
                spx_html = ""
            bk = book["personas"].get(p["key"])
            if bk:
                bpnl = bk["pnl"]
                bcls = "up" if bpnl > 0.5 else ("dn" if bpnl < -0.5 else "")
                bpl = (_fd_signed(bpnl) if bk["settled"]
                       else (f'{_fd_money(bk["at_risk"])} at risk' if bk["at_risk"] else "no bet yet"))
                bankline = (f'<div class="fdt-p-bank"><span class="lbl">the book</span>'
                            f'<span class="nw">{_fd_money(bk["bankroll"])}</span>'
                            f'<span class="pl {bcls}">{bpl}</span></div>')
            else:
                bankline = ""
            if p["graded"]:
                losses = p["graded"] - p["hits"]
                cls = "up" if p["hits"] > losses else ("dn" if losses > p["hits"] else "")
                rec = (f'<div class="fdt-p-rec"><div class="r {cls}">{p["hits"]}–{losses}</div>'
                       f'<div class="b">Brier {_fdt_fmt_brier(p["briers"])}</div></div>')
                rows = "".join(
                    f'<div class="fdt-p-call"><span>{html.escape(c["market"])} · {html.escape((c.get("flag", "") + " " + c["pick"]).strip())} @ {c["prob"]:g}%</span>'
                    f'<span class="v {"up" if c["hit"] else "dn"}">{"✓ hit" if c["hit"] else "✗ miss"} · {c["brier"]:.3f}</span></div>'
                    for c in p["calls"][-6:])
                tail = f'<div class="fdt-p-calls">{rows}</div>'
            else:
                rec = '<div class="fdt-p-rec"><div class="r">0–0</div><div class="b">no grades yet</div></div>'
                tail = '<div class="fdt-p-none">First call on the ledger — awaiting its grade.</div>'
            cards.append(
                f'<a class="fdt-p fdt-p-link" href="forecasters/{p["key"]}.html" style="border-top:3px solid {col}">'
                f'<div class="fdt-p-top">{_persona_mascot_html(p["key"], "card")}'
                f'<div><div class="fdt-p-nm">{html.escape(p["name"])}</div>'
                f'<div class="fdt-p-role">{html.escape(_persona_role(p["key"], ""))}</div>'
                f'<div class="fdt-p-crit">{html.escape(p["criterion"])}</div></div>{rec}</div>'
                f'{model_html}{doc_html}{spx_html}{bankline}{tail}'
                f'<span class="fdt-p-cta">Full profile →</span></a>')
        parts.append(f'<div class="fdt-roster">{"".join(cards)}</div>')
        n += 1
    # 03 — calibration.
    parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Calibration — where the desk\'s confidence meets reality</h2>')
    leg_items = ['<span><span class="sw" style="background:#eab308"></span>The Council</span>',
                 '<span><span class="sw" style="background:#60a5fa"></span>The Research (lead scenarios)</span>']
    leg_items += [f'<span><span class="sw" style="background:{FDT_SERIES_COLORS.get(k, "#9aa1af")}"></span>{html.escape(_persona_agent_name(k, nm))}</span>'
                  for k, nm, _a, _c in FD_PERSONAS]
    parts.append(f'<div class="fdt-cal">{_fdt_calibration_svg(calls)}'
                 f'<div class="fdt-cal-leg">{"".join(leg_items)}</div></div>')
    n += 1
    # 04 — the graded ledger.
    def _pl_cell(c):
        b = bet_lookup.get((c["caller"].casefold(), c["market"]))
        if not b:
            return '<td class="num" style="color:var(--fdmut)">—</td>'
        if b.get("pass"):
            return '<td class="num" style="color:var(--fdmut)">pass</td>'
        color = "var(--fdup)" if b["pnl"] >= 0 else "var(--fddn)"
        return f'<td class="num" style="color:{color};font-weight:700">{_fd_signed(b["pnl"])}</td>'
    parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Ledger — every graded call, scored</h2>')
    if calls:
        rows = "".join(
            f'<tr><td class="num">{html.escape(c["date"] or "—")}</td>'
            f'<td><a href="{html.escape(c["href"], quote=True)}">{html.escape(c["market"])}</a></td>'
            f'<td>{(_persona_mascot_html(c["key"], "table") if c.get("kind") == "persona" and c.get("key") else c["avatar"])} {html.escape(c["caller"])}</td>'
            f'<td>{html.escape((c.get("flag", "") + " " + c["call"]).strip())}</td>'
            f'<td class="num">{c["prob"]:g}%</td>'
            f'<td>{html.escape((c.get("result_flag", "") + " " + c["result"]).strip())}</td>'
            f'<td class="{"v-hit" if c["hit"] else "v-miss"}">{"✓ hit" if c["hit"] else "✗ miss"}</td>'
            f'<td class="num">{c["brier"]:.3f}</td>{_pl_cell(c)}</tr>'
            for c in calls)
        parts.append(f'<div class="fdt-ledger"><table><thead><tr><th>Graded</th><th>Market</th><th>Caller</th>'
                     f'<th>The call</th><th>Price</th><th>Result</th><th>Verdict</th><th>Brier</th><th>Book P&amp;L</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
    else:
        first = (f' Next up: <a href="{html.escape(next_due["href"], quote=True)}" '
                 f'style="color:var(--fdblue)">{html.escape(next_due["title"])}</a>, which '
                 f'<b data-grades="{html.escape(next_due["due"], quote=True)}">'
                 f'grades {html.escape(_long_date(next_due["due"]))}</b>.'
                 if next_due and next_due.get("due") else "")
        parts.append(f'<div class="fdt-empty">No calls graded yet — the ledger below is all open positions.{first}</div>')
    n += 1
    # 05 — open positions.
    if pending:
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Open Positions — {len(pending)} markets awaiting their grade</h2>')
        rows = []
        for p in pending:
            when = (f'<span class="w" data-grades="{html.escape(p["due"], quote=True)}">grades {html.escape(_long_date(p["due"]))}</span>'
                    if p.get("due") else
                    f'<span class="w">{html.escape(p.get("horizon", "") or "horizon open")}</span>')
            rows.append(f'<a href="{html.escape(p["href"], quote=True)}"><span class="t">{html.escape(p["title"])}</span>{when}</a>')
        parts.append(f'<div class="fdt-pend">{"".join(rows)}</div>')
    if book["settled"] and book["leader"]:
        book_folio = (f'    <span class="fd-folio-g">book leader {html.escape(book["leader"]["name"])} '
                      f'{_fd_money(book["leader"]["bankroll"])}</span>')
    else:
        book_folio = f'    <span>book: {_fd_money(book["at_risk"])} at risk</span>'
    folio = (f'    <span>{desk["graded"]} graded</span>\n'
             f'    <span class="fd-folio-c">record {rec_txt}</span>\n'
             f'    <span>mean Brier {_fdt_fmt_brier(desk["briers"])}</span>\n'
             f'    <span>{n_open} open</span>\n'
             f'{book_folio}')
    plate_extra = (f'  <a class="fd-rec-link" href="{html.escape(board_href, quote=True)}">'
                   f'📊 {html.escape(board_title)} — the live board →</a>')
    og = og_tags(h1, "Every graded call, scored — records, Brier scores, and calibration.",
                 f"{SITE_URL}/{fname}", f"{SITE_URL}/{OG_IMAGE}")
    page_html = FORECAST_PAGE_TEMPLATE.format(
        page_title=html.escape(h1), h1=html.escape(h1), kicker=html.escape(kicker),
        nav=nav, back=back,
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_DETAIL_CSS,
        accent_css=_accent_css(page.get("accent")),
        favicon=FAVICON, og_meta=og,
        motto="A forecast you never grade is just a mood.",
        blurb="Every call is frozen when it is logged and scored against the official result when it resolves — "
              "hits and misses both stay on the ledger. That is the difference between a desk and a feed.",
        folio=folio, plate_extra=plate_extra,
        scene=scene_plate("forecast", extra_class="page-scene", seed=f"forecast-record:{fname}"),
        body="\n".join(parts),
        theme_js=LIBRARY_THEME_JS,
        app_js=FORECAST_PAGE_JS,
        shell=shell,
    )
    (out / fname).write_text(page_html)
    print(f"  ✓ {h1}  ({desk['graded']} graded, {n_open} open) → {fname}")


def _fdp_spectrum_bar(spectrum, color):
    """One persona's position on the rigor→story axis, standalone at profile-page
    scale (the roster card's version is a compressed pip; this is the full bar)."""
    pct = max(0.0, min(1.0, spectrum)) * 100
    return (f'<div class="fdp-spx"><span class="fdp-spx-end">trust the number</span>'
            f'<span class="fdp-spx-track"><span class="fdp-spx-pip" style="left:{pct:.1f}%;background:{color}"></span></span>'
            f'<span class="fdp-spx-end">trust the story</span></div>')


def build_persona_pages(out_dir, led, book, shell=""):
    """Render one full profile page per standing Forecast Desk persona
    (docs/forecasters/{key}.html) — its model, its betting doctrine, its position
    on the rigor→story spectrum, its cumulative record and Brier score, its play-
    money bankroll, and every call it has ever made (graded and still open). The
    roster cards on the Track Record page link here, so clicking a predictor
    always resolves to its complete record, not just the summary chip.
    `led` is build_forecast_ledger's output and `book` is build_book's — both
    computed once, site-wide, from every market (native + harvested + desk-scoped),
    so a persona's profile is never partial just because it also works a desk."""
    out = Path(out_dir) / "forecasters"
    out.mkdir(parents=True, exist_ok=True)
    n_rendered = 0
    for key, name, avatar, criterion in FD_PERSONAS:
        agent = _persona_agent_name(key, name)
        role = _persona_role(key, name)
        p = led["personas"].get(key) or {"key": key, "name": agent, "avatar": avatar,
                                          "criterion": criterion, "graded": 0, "hits": 0,
                                          "briers": [], "calls": []}
        prof = FD_PERSONA_PROFILES.get(key, {})
        acct = book["personas"].get(key)
        color = FDT_SERIES_COLORS.get(key, "#9aa1af")
        losses = p["graded"] - p["hits"]
        rec_txt = f'{p["hits"]}–{losses}' if p["graded"] else "0–0"
        hitrate = f'{p["hits"] / p["graded"] * 100:.0f}%' if p["graded"] else "—"
        brier = _fdt_fmt_brier(p["briers"]) if p["briers"] else "—"

        parts = [f'<p class="fdp-crit">{html.escape(criterion)}</p>']
        n = 1
        if prof.get("spectrum") is not None:
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Where It Sits on the Desk\'s Spectrum</h2>'
                         f'<p class="fdp-zone">{html.escape(prof.get("zone", ""))}</p>'
                         + _fdp_spectrum_bar(prof["spectrum"], color))
            n += 1
        if prof.get("model"):
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Model — how it forecasts</h2>'
                         f'<p class="fdp-prose">{html.escape(prof["model"])}</p>')
            n += 1
        if prof.get("doctrine"):
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Doctrine — how it bets</h2>'
                         f'<p class="fdp-prose">{html.escape(prof["doctrine"])}</p>')
            n += 1
        bank_html = ""
        if acct:
            bpnl = acct["pnl"]
            bcls = "up" if bpnl > 0.5 else ("dn" if bpnl < -0.5 else "")
            bank_html = (f'<div class="fdt-stat"><div class="big">{_fd_money(acct["bankroll"])}</div><div class="lbl">the book</div></div>'
                        f'<div class="fdt-stat"><div class="big {bcls}">{_fd_signed(acct["pnl"])}</div><div class="lbl">P&amp;L, play money</div></div>')
        parts.append(
            f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Record — mathematical rigor, scored</h2>'
            f'<div class="fdt-sum">'
            f'<div class="fdt-stat"><div class="big">{p["graded"]}</div><div class="lbl">markets graded</div></div>'
            f'<div class="fdt-stat"><div class="big {"up" if p["graded"] and p["hits"] * 2 >= p["graded"] else ""}">{rec_txt}</div><div class="lbl">record, hits–misses</div></div>'
            f'<div class="fdt-stat"><div class="big">{hitrate}</div><div class="lbl">hit rate</div></div>'
            f'<div class="fdt-stat"><div class="big gold">{brier}</div><div class="lbl">mean Brier score</div></div>'
            f'{bank_html}'
            f'</div>'
            f'<p class="fdt-note">Brier score = (stated probability − what happened)², averaged over every graded '
            f'call. 0 is clairvoyance, 0.25 is a coin flip, 1 is confident wrongness. Lower is better — it is the '
            f'one number that punishes both a wrong call and an overconfident right one.</p>')
        n += 1
        if p["calls"]:
            rows = "".join(
                f'<tr><td><a href="../{html.escape(c.get("href", "") or "forecast.html", quote=True)}">{html.escape(c["market"])}</a></td>'
                f'<td>{html.escape((c.get("flag", "") + " " + c["pick"]).strip())}</td>'
                f'<td class="num">{c["prob"]:g}%</td>'
                f'<td class="{"v-hit" if c["hit"] else "v-miss"}">{"✓ hit" if c["hit"] else "✗ miss"}</td>'
                f'<td class="num">{c["brier"]:.3f}</td></tr>'
                for c in sorted(p["calls"], key=lambda c: c.get("market", ""))
            )
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Every Graded Call ({p["graded"]})</h2>'
                        f'<div class="fdt-ledger"><table><thead><tr><th>Market</th><th>The call</th>'
                        f'<th>Price</th><th>Verdict</th><th>Brier</th></tr></thead>'
                        f'<tbody>{rows}</tbody></table></div>')
        else:
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Every Graded Call</h2>'
                        f'<p class="fdt-empty">No graded calls yet — this predictor\'s first market is still open.</p>')
        n += 1
        if acct and acct["open"]:
            rows = "".join(
                f'<a href="../{html.escape(o["href"], quote=True)}"><span class="t">{html.escape(o["market"])} '
                f'— {html.escape((o.get("flag", "") + " " + o["pick"]).strip())} @ {o["p"]:g}%</span>'
                f'<span class="w">{"grades " + html.escape(_long_date(o["due"])) if o.get("due") else "horizon open"}</span></a>'
                for o in acct["open"]
            )
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Open Positions ({len(acct["open"])})</h2>'
                        f'<div class="fdt-pend">{rows}</div>')

        title = f"{agent} — {role}"
        og = og_tags(title, criterion, f"{SITE_URL}/forecasters/{key}.html", f"{SITE_URL}/{OG_IMAGE}")
        folio = (f'    <span>{p["graded"]} graded</span>\n'
                f'    <span class="fd-folio-c">record {rec_txt}</span>\n'
                f'    <span>{html.escape(prof.get("zone", "The desk"))}</span>')
        headline = (f'<span class="fd-profile-name">{_persona_mascot_html(key, "title")}'
                    f'<span class="txt"><span>{html.escape(agent)}</span>'
                    f'<span class="role">{html.escape(role)}</span></span></span>')
        page = FORECAST_PAGE_TEMPLATE.format(
            page_title=html.escape(title), h1=headline,
            kicker="The Forecast Desk · Predictor profile",
            nav=main_nav_html(prefix="../", active="forecast.html"),
            back='<a href="../forecast-record.html">← The Track Record</a>',
            css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_DETAIL_CSS,
            accent_css="",
            favicon=FAVICON, og_meta=og,
            motto=html.escape(prof.get("signature", criterion)),
            folio=folio, plate_extra="",
            scene=scene_plate("forecast", extra_class="page-scene", root="../", seed=f"forecaster:{key}"),
            body="\n".join(parts),
            blurb="Every call this predictor has made, graded honestly — the desk keeps no predictor's record off the books.",
            theme_js=LIBRARY_THEME_JS, app_js="", shell=shell,
        )
        (out / f"{key}.html").write_text(_persona_public_copy(page))
        n_rendered += 1
    print(f"  ✓ Predictor profiles ({n_rendered}) → forecasters/")


def _fd_persona_legend_html():
    """A colored-dot key for the roster, shared across the comparison charts."""
    return '<div class="cmp-legend">' + "".join(
        f'<span>{_persona_mascot_html(key, "table")} {html.escape(_persona_agent_name(key, name))}</span>'
        for key, name, avatar, criterion in FD_PERSONAS
    ) + '</div>'


def _fdc_bankroll_race_svg(book):
    """The bankroll race: every persona's running bankroll traced across the
    shared sequence of settled bets, in settlement order — one line chart that
    turns seven different forecasting methods into a single number. Built from
    a shared event timeline (date, market) so every series shares one x-axis
    even though a persona can pass on a market its doctrine skips."""
    events, seen = [], set()
    for a in book["personas"].values():
        for b in a["bets"]:
            if not b.get("graded"):
                continue
            k = (b.get("date") or "", b["market"])
            if k not in seen:
                seen.add(k)
                events.append(k)
    events.sort()
    if len(events) < 2:
        return ""
    W, H = 660, 320
    X0, X1, Y0, Y1 = 58, 630, 268, 26
    n = len(events)
    def X(i):
        return X0 + (X1 - X0) * i / (n - 1)
    series = {}
    maxv = minv = FD_BANKROLL_START
    for key, name, avatar, criterion in FD_PERSONAS:
        acct = book["personas"].get(key)
        by_event = {}
        if acct:
            for b in acct["bets"]:
                if b.get("graded"):
                    by_event[(b.get("date") or "", b["market"])] = b
        vals, bal = [], FD_BANKROLL_START
        for ev in events:
            b = by_event.get(ev)
            if b is not None:
                bal += b.get("pnl", 0.0)
            vals.append(bal)
        maxv, minv = max(maxv, max(vals)), min(minv, min(vals))
        series[key] = vals
    pad = max((maxv - minv) * 0.1, 25)
    lo, hi = max(0.0, minv - pad), maxv + pad
    def Y(v):
        return Y0 - (Y0 - Y1) * (v - lo) / (hi - lo) if hi > lo else (Y0 + Y1) / 2
    grid = []
    for t in range(5):
        v = lo + (hi - lo) * t / 4
        y = Y(v)
        grid.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="#2c303a" stroke-width="1"/>')
        grid.append(f'<text x="{X0 - 10}" y="{y + 4:.1f}" text-anchor="end" fill="#9aa1af" font-size="10.5" '
                    f'font-family="ui-monospace,Menlo,monospace">${v:,.0f}</text>')
    base_y = Y(FD_BANKROLL_START)
    grid.append(f'<line x1="{X0}" y1="{base_y:.1f}" x2="{X1}" y2="{base_y:.1f}" stroke="#9aa1af" '
               f'stroke-width="1" stroke-dasharray="4 4" opacity=".55"/>')
    grid.append(f'<text x="{X1}" y="{base_y - 6:.1f}" text-anchor="end" fill="#9aa1af" font-size="9.5" '
               f'font-style="italic" font-family="Georgia,serif">starting bankroll</text>')
    lines = []
    for key, name, avatar, criterion in FD_PERSONAS:
        col = FDT_SERIES_COLORS.get(key, "#9aa1af")
        mark = _persona_monogram(key, name)
        vals = series[key]
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" opacity=".92"/>')
        lx, ly = X(n - 1), Y(vals[-1])
        lines.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="{col}" stroke="#0c1117" stroke-width="1.2"/>')
        lines.append(f'<rect x="{lx + 8:.1f}" y="{ly - 10:.1f}" width="18" height="18" rx="2" fill="#10161f" stroke="{col}" stroke-width="1.4"/>')
        lines.append(f'<text x="{lx + 17:.1f}" y="{ly + 2.8:.1f}" text-anchor="middle" fill="{col}" font-size="7.2" font-weight="800" '
                    f'font-family="ui-monospace,Menlo,monospace">{html.escape(mark)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Bankroll race across every graded call">{"".join(grid)}{"".join(lines)}</svg>')


def _fdc_record_bars_svg(led):
    """A ranked horizontal bar per predictor — hits shaded in its own color over
    a full-width track (graded markets), sorted best hit-rate first, Brier
    annotated at the end of the bar."""
    rows = []
    for key, name, avatar, criterion in FD_PERSONAS:
        p = led["personas"].get(key) or {"graded": 0, "hits": 0, "briers": []}
        graded, hits = p["graded"], p["hits"]
        hitrate = (hits / graded * 100) if graded else -1.0
        rows.append((key, _persona_agent_name(key, name), avatar, graded, hits, graded - hits, hitrate, _fdt_fmt_brier(p["briers"])))
    rows.sort(key=lambda r: r[6], reverse=True)
    if not any(r[3] for r in rows):
        return ""
    W = 660
    row_h = 42
    H = row_h * len(rows) + 16
    X0, X1 = 168, 470
    maxn = max((r[3] for r in rows), default=1) or 1
    bars = []
    for i, (key, name, avatar, graded, hits, losses, hitrate, brier) in enumerate(rows):
        y = 12 + i * row_h
        col = FDT_SERIES_COLORS.get(key, "#9aa1af")
        mark = _persona_monogram(key, name)
        w = (X1 - X0) * (graded / maxn) if maxn else 0
        hw = w * (hits / graded) if graded else 0
        bars.append(f'<rect x="{X0 - 162}" y="{y - 1}" width="24" height="24" rx="2" fill="#10161f" stroke="{col}" stroke-width="1.5"/>')
        bars.append(f'<text x="{X0 - 150}" y="{y + 15:.0f}" text-anchor="middle" fill="{col}" font-size="8" '
                    f'font-weight="800" font-family="ui-monospace,Menlo,monospace">{html.escape(mark)}</text>')
        bars.append(f'<text x="{X0 - 12}" y="{y + 15:.0f}" text-anchor="end" fill="#edeff4" font-size="11.5" '
                    f'font-weight="700" font-family="-apple-system,sans-serif">{html.escape(name)}</text>')
        bars.append(f'<rect x="{X0}" y="{y}" width="{w:.1f}" height="20" rx="2" fill="#2c303a"/>')
        if hw > 0:
            bars.append(f'<rect x="{X0}" y="{y}" width="{hw:.1f}" height="20" rx="2" fill="{col}"/>')
        rec_txt = f"{hits}–{losses} · {hitrate:.0f}% · Brier {brier}" if graded else "no grades yet"
        bars.append(f'<text x="{X0 + w + 10:.1f}" y="{y + 15:.0f}" fill="#9aa1af" font-size="10.5" '
                    f'font-family="ui-monospace,Menlo,monospace">{rec_txt}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Record by predictor, sorted by hit rate">{"".join(bars)}</svg>')


def _fdc_rigor_accuracy_svg(led):
    """A scatter of spectrum position (rigor → story) against mean Brier score,
    testing the roster's own premise: does more mathematical rigor actually
    predict a lower (better) Brier score, or does the spectrum just describe
    style, not accuracy?"""
    pts = []
    for key, name, avatar, criterion in FD_PERSONAS:
        p = led["personas"].get(key) or {}
        prof = FD_PERSONA_PROFILES.get(key, {})
        if not p.get("briers") or prof.get("spectrum") is None:
            continue
        pts.append((key, _persona_agent_name(key, name), avatar, prof["spectrum"], sum(p["briers"]) / len(p["briers"])))
    if len(pts) < 2:
        return ""
    W, H = 660, 300
    X0, X1, Y0, Y1 = 60, 620, 250, 34
    briers = [b for *_, b in pts]
    pad = max((max(briers) - min(briers)) * 0.2, 0.015)
    lo, hi = max(0.0, min(briers) - pad), max(briers) + pad
    def X(s):
        return X0 + (X1 - X0) * max(0.0, min(1.0, s))
    def Y(b):
        return Y1 + (Y0 - Y1) * (b - lo) / (hi - lo) if hi > lo else (Y0 + Y1) / 2
    grid = []
    for t in (0, .25, .5, .75, 1):
        grid.append(f'<line x1="{X(t):.1f}" y1="{Y0}" x2="{X(t):.1f}" y2="{Y1}" stroke="#2c303a" stroke-width="1"/>')
    for t in (0, .25, .5, .75, 1):
        v = lo + (hi - lo) * t
        grid.append(f'<line x1="{X0}" y1="{Y(v):.1f}" x2="{X1}" y2="{Y(v):.1f}" stroke="#2c303a" stroke-width="1"/>')
        grid.append(f'<text x="{X0 - 10}" y="{Y(v) + 4:.1f}" text-anchor="end" fill="#9aa1af" font-size="10" '
                    f'font-family="ui-monospace,Menlo,monospace">{v:.2f}</text>')
    labels = (
        f'<text x="{X0}" y="{Y0 + 24}" fill="#9aa1af" font-size="10.5" font-family="ui-monospace,Menlo,monospace">trust the number</text>'
        f'<text x="{X1}" y="{Y0 + 24}" text-anchor="end" fill="#9aa1af" font-size="10.5" font-family="ui-monospace,Menlo,monospace">trust the story</text>'
        f'<text x="18" y="{(Y0 + Y1) / 2:.0f}" text-anchor="middle" fill="#9aa1af" font-size="10.5" '
        f'font-family="-apple-system,sans-serif" transform="rotate(-90 18 {(Y0 + Y1) / 2:.0f})">mean Brier — lower is better ↑</text>'
    )
    dots = []
    for key, name, avatar, s, b in pts:
        col = FDT_SERIES_COLORS.get(key, "#9aa1af")
        mark = _persona_monogram(key, name)
        x, y = X(s), Y(b)
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{col}" stroke="#0c1117" stroke-width="1.5">'
                    f'<title>{html.escape(name)}: Brier {b:.3f} at spectrum {s:.2f}</title></circle>')
        dots.append(f'<text x="{x:.1f}" y="{y + 3.3:.1f}" text-anchor="middle" fill="#0c1117" font-size="7.5" '
                    f'font-weight="800" font-family="ui-monospace,Menlo,monospace">{html.escape(mark)}</text>')
        dots.append(f'<text x="{x:.1f}" y="{y - 15:.1f}" text-anchor="middle" fill="{col}" font-size="10.5" '
                    f'font-weight="700" font-family="-apple-system,sans-serif">{html.escape(name)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Rigor versus accuracy: spectrum position against mean Brier score">'
            f'{"".join(grid)}{labels}{"".join(dots)}</svg>')


def _fdc_head_to_head_html(led):
    """A market × predictor grid: every graded market as a row, every standing
    predictor's pick as a column, hit shaded green and miss shaded red — the
    fastest way to see which predictors were pulling in the same direction on
    the same call, and which weren't."""
    order, seen = [], set()
    for c in led["calls"]:
        if c["kind"] == "persona" and c["market"] not in seen:
            seen.add(c["market"])
            order.append((c["market"], c["href"], c["result"]))
    if not order:
        return ""
    head = "".join(
        f'<th class="h2h-persona" style="border-top-color:{FDT_SERIES_COLORS.get(key, "#9aa1af")}">'
        f'<a href="{key}.html" title="{html.escape(_persona_agent_name(key, name), quote=True)}">'
        f'{_persona_mascot_html(key, "table")}</a></th>'
        for key, name, avatar, criterion in FD_PERSONAS
    )
    rows = []
    for market, href, winner in order:
        cells = []
        for key, name, avatar, criterion in FD_PERSONAS:
            p = led["personas"].get(key) or {}
            call = next((c for c in p.get("calls", []) if c["market"] == market), None)
            if call is None:
                cells.append('<td class="h2h-none">—</td>')
                continue
            cls = "h2h-hit" if call["hit"] else "h2h-miss"
            cells.append(
                f'<td class="{cls}"><span class="h2h-pick">{call.get("flag", "")} {html.escape(call["pick"])}</span>'
                f'<span class="h2h-prob">{call["prob"]:g}%</span></td>')
        rows.append(
            f'<tr><th class="h2h-m"><a href="../{html.escape(href, quote=True)}">{html.escape(market)}</a>'
            f'<span class="h2h-w">won: {html.escape(winner)}</span></th>{"".join(cells)}</tr>')
    return (f'<div class="cmp-wrap"><table class="h2h-table"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def build_persona_compare_page(out_dir, led, book, shell=""):
    """Render docs/forecasters/compare.html — every standing predictor laid side
    by side in one table (criterion, spectrum position, model, doctrine, record,
    Brier, bankroll), so a visitor can read straight across instead of flipping
    between seven separate profile pages."""
    out = Path(out_dir) / "forecasters"
    out.mkdir(parents=True, exist_ok=True)
    cols = []
    for key, name, avatar, criterion in FD_PERSONAS:
        agent = _persona_agent_name(key, name)
        role = _persona_role(key, name)
        p = led["personas"].get(key) or {"graded": 0, "hits": 0, "briers": []}
        prof = FD_PERSONA_PROFILES.get(key, {})
        acct = book["personas"].get(key)
        losses = p["graded"] - p["hits"]
        cols.append({
            "key": key, "name": agent, "role": role, "avatar": avatar, "criterion": criterion,
            "col": FDT_SERIES_COLORS.get(key, "#9aa1af"),
            "zone": prof.get("zone", ""), "spectrum": prof.get("spectrum"),
            "model": prof.get("model", ""), "doctrine": prof.get("doctrine", ""),
            "graded": p["graded"],
            "rec": f'{p["hits"]}–{losses}' if p["graded"] else "0–0",
            "hitrate": f'{p["hits"] / p["graded"] * 100:.0f}%' if p["graded"] else "—",
            "brier": _fdt_fmt_brier(p["briers"]) if p["briers"] else "—",
            "bank": _fd_money(acct["bankroll"]) if acct else "—",
            "pnl": _fd_signed(acct["pnl"]) if acct else "—",
            "signature": prof.get("signature", ""),
        })

    def _row(label, render):
        return f'<tr><th>{html.escape(label)}</th>{"".join(f"<td>{render(c)}</td>" for c in cols)}</tr>'

    head_cells = "".join(
        f'<th class="cmp-persona" style="border-top-color:{c["col"]}">'
        f'<a href="{c["key"]}.html">{_persona_mascot_html(c["key"], "card")}'
        f'<span class="cmp-nm">{html.escape(c["name"])}</span>'
        f'<span class="cmp-role">{html.escape(c["role"])}</span></a></th>'
        for c in cols
    )
    rows = "".join([
        _row("Criterion", lambda c: f'<span class="cmp-crit">{html.escape(c["criterion"])}</span>'),
        _row("Where it sits", lambda c: (
            f'<span class="cmp-zone">{html.escape(c["zone"])}</span>'
            + (_fdp_spectrum_bar(c["spectrum"], c["col"]) if c["spectrum"] is not None else ""))),
        _row("The model", lambda c: f'<span class="cmp-prose">{html.escape(c["model"])}</span>'),
        _row("The doctrine", lambda c: f'<span class="cmp-prose">{html.escape(c["doctrine"])}</span>'),
        _row("Markets graded", lambda c: str(c["graded"])),
        _row("Record, hits–misses", lambda c: c["rec"]),
        _row("Hit rate", lambda c: c["hitrate"]),
        _row("Mean Brier score", lambda c: c["brier"]),
        _row("The book", lambda c: c["bank"]),
        _row("P&amp;L, play money", lambda c: c["pnl"]),
        _row("Signature", lambda c: f'<span class="cmp-sig">{html.escape(c["signature"])}</span>'),
    ])
    body = (
        '<p class="fdp-crit">Every standing predictor, side by side — the same rows the profile pages carry '
        'separately, read straight across so the differences in method (not just outcome) are visible at a glance.</p>'
        f'<div class="cmp-wrap"><table class="cmp-table"><thead><tr><th></th>{head_cells}</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )
    title = "Compare the Predictors — The Forecast Desk"
    og = og_tags(title, "Every standing predictor's model, doctrine, and record, compared side by side.",
                 f"{SITE_URL}/forecasters/compare.html", f"{SITE_URL}/{OG_IMAGE}")
    page = FORECAST_PAGE_TEMPLATE.format(
        page_title=html.escape(title), h1="Compare the Predictors",
        kicker="The Forecast Desk · All seven, side by side",
        nav=main_nav_html(prefix="../", active="forecast.html"),
        back='<a href="../forecast-record.html">← The Track Record</a>',
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_DETAIL_CSS,
        accent_css="",
        favicon=FAVICON, og_meta=og,
        motto="Seven models, one spectrum — no hiding behind a single number.",
        folio=f'    <span>{len(cols)} predictors</span>\n    <span class="fd-folio-c">every row comparable</span>',
        plate_extra="",
        scene=scene_plate("forecast", extra_class="page-scene", root="../", seed="forecaster:compare"),
        body=body,
        blurb="Every call this roster has made, graded honestly — the desk keeps no predictor's record off the books.",
        theme_js=LIBRARY_THEME_JS, app_js="", shell=shell,
    )
    (out / "compare.html").write_text(_persona_public_copy(page))
    print("  ✓ Compare the Predictors → forecasters/compare.html")


# ------------------------------------------------------------- native forecast pages
# Each native forecast renders from docs/forecast/data/{slug}.json into
# docs/forecast/{slug}.html — the full live-market treatment: consensus pick,
# predictor roster (trader cards), field board, base rates, markets, triggers.

FORECAST_DETAIL_CSS = FORECAST_PAGE_CSS + """
.fdd-wrap { max-width: 940px; margin: 0 auto; padding: 0 1.2rem; }
.fd-board.fdd { margin-top: 1.4rem; }
.fdd-res { font-family: var(--serif); font-style: italic; color: var(--fdmut); font-size: .9rem;
  line-height: 1.5; margin: .9rem 0 0; }
.fdd-h { font-family: var(--sans); font-size: .7rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--fdmut); border-bottom: 1px solid var(--fdline); padding-bottom: .5rem; margin: 2rem 0 1rem; }
.fdd-h .n { color: var(--fdup); font-family: var(--fdmono); margin-right: .5rem; }

/* consensus hero */
.fdd-hero { background: #10161f; border: 1px solid var(--fdline);
  border-radius: 2px; padding: 1.5rem 1.7rem; }
.fdd-pickrow { display: grid; grid-template-columns: auto 1fr; gap: 1.5rem; align-items: center; margin-top: 1rem; }
.fdd-pick { text-align: center; background: #0c1117; border: 1px solid var(--fdup); border-radius: 2px;
  padding: 1.1rem 1.7rem; }
.fdd-pick .f { font-size: 2.7rem; line-height: 1; }
.fdd-pick .t { font-family: var(--sans); font-weight: 800; font-size: 1.35rem; margin-top: .3rem; }
.fdd-pick .p { font-family: var(--fdmono); font-weight: 700; font-size: 1.25rem; color: var(--fdup); margin-top: .2rem;
  font-variant-numeric: tabular-nums; }
.fdd-pick .ru { font-family: var(--sans); font-size: .7rem; color: var(--fdmut); margin-top: .45rem; }
.fdd-case { font-family: var(--serif); font-size: .98rem; line-height: 1.62; color: #c6d0e0; margin: 0; }
@media (max-width: 680px) { .fdd-pickrow { grid-template-columns: 1fr; } }

/* tally leaderboard */
.fdd-tally { display: flex; height: 14px; border-radius: 0; overflow: hidden; margin: .9rem 0 .45rem;
  border: 1px solid var(--fdline); }
.fdd-tally span { display: block; }
.fdd-tally .t1 { background: var(--fdup); } .fdd-tally .t2 { background: var(--fdgold); } .fdd-tally .t3 { background: var(--fddn); }
.fdd-tally-l { display: flex; gap: 1.4rem; flex-wrap: wrap; font-family: var(--sans); font-size: .74rem; color: var(--fdmut); }
.fdd-tally-l b { color: var(--fdtext); }

/* trader cards */
.fdd-roster { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }
.fdd-trader { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1.05rem 1.15rem;
  display: flex; flex-direction: column; text-decoration: none; color: inherit;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease), border-color .15s var(--ease); }
.fdd-trader:hover { border-color: var(--fdblue); transform: translateY(-2px); box-shadow: var(--fdshadow); }
.fdd-tr-cta { display: block; font-family: var(--sans); font-size: .64rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: var(--fdblue); margin-top: .7rem; padding-top: .6rem; border-top: 1px solid var(--fdline); }
.fdd-tr-top { display: flex; gap: .8rem; align-items: flex-start; }
.fdd-tr-av { font-size: 1.6rem; line-height: 1; background: #0c1117; border: 1px solid var(--fdline);
  border-radius: 2px; padding: .5rem .55rem; }
.fdd-tr-nm { font-family: var(--sans); font-weight: 800; font-size: 1rem; }
.fdd-tr-role { font-family: var(--sans); font-size: .58rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: 0; color: var(--fdmut); margin-top: .12rem; }
.fdd-tr-crit { font-family: var(--serif); font-style: italic; font-size: .78rem; color: var(--fdmut); line-height: 1.4; margin-top: .15rem; }
.fdd-tr-call { margin-left: auto; text-align: center; max-width: 11rem; }
.fdd-tr-call .t { line-height: 1.25; }
.fdd-tr-call .f { font-size: 1.4rem; line-height: 1; }
.fdd-tr-call .t { font-family: var(--sans); font-size: .72rem; font-weight: 800; margin-top: .1rem; }
.fdd-tr-call .p { font-family: var(--fdmono); font-size: .82rem; font-weight: 700; color: var(--fdup);
  font-variant-numeric: tabular-nums; }
.fdd-tr-tags { display: flex; gap: .45rem; flex-wrap: wrap; margin: .7rem 0 .55rem; }
.fdd-tag { font-family: var(--sans); font-size: .58rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .1em; border-radius: 2px; padding: .18rem .55rem; }
.fdd-tag.reg { color: var(--fdgold); border: 1px solid var(--fdgold); }
.fdd-tag.conf-high { color: #0c1117; background: var(--fdup); }
.fdd-tag.conf-medium { color: var(--fdtext); border: 1px solid var(--fdmut); }
.fdd-tag.conf-low { color: var(--fdmut); border: 1px solid var(--fdline); }
.fdd-tag.rec { color: var(--fdblue); border: 1px solid var(--fdblue); }
.fdd-tr-blurb { font-family: var(--serif); font-size: .92rem; line-height: 1.6; color: #c6d0e0; margin: 0 0 .7rem; }
.fdd-tr-ev { border-top: 1px solid var(--fdline); margin-top: auto; padding-top: .6rem;
  font-family: var(--sans); font-size: .74rem; line-height: 1.45; color: var(--fdmut); }
.fdd-tr-ev b { color: var(--fdup); font-size: .6rem; text-transform: uppercase; letter-spacing: .1em; }

/* consensus + dissent */
.fdd-cons { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1.2rem 1.4rem; }
.fdd-cons p { font-family: var(--serif); font-size: .95rem; line-height: 1.62; color: #c6d0e0; margin: 0 0 1rem; }
.fdd-cons .lbl { display: block; font-family: var(--sans); font-size: .62rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; color: var(--fdup); margin-bottom: .3rem; }
.fdd-dissent { background: rgba(239,68,68,.07); border: 1px solid rgba(239,68,68,.35); border-radius: 2px;
  padding: .9rem 1.1rem; font-family: var(--serif); font-size: .9rem; line-height: 1.58; color: #d8c2c2; }
.fdd-dissent .lbl { color: var(--fddn); }

/* the field price board */
.fdd-field { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: 1.1rem 1.25rem; }
.fdd-frow { display: grid; grid-template-columns: 130px 1fr 58px; gap: .9rem; align-items: center; margin: .5rem 0; }
.fdd-frow .tm { font-family: var(--sans); font-size: .82rem; color: var(--fdmut); text-align: right; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.fdd-frow.lead .tm { color: var(--fdtext); font-weight: 700; }
.fdd-frow .pv { font-family: var(--fdmono); font-size: .82rem; font-weight: 700; color: var(--fdmut);
  font-variant-numeric: tabular-nums; }
.fdd-frow.lead .pv { color: var(--fdup); }
.fdd-frow .fd-track { height: 12px; }
.fdd-field-foot { font-family: var(--sans); font-size: .68rem; color: var(--fdmut); border-top: 1px solid var(--fdline);
  margin-top: .8rem; padding-top: .7rem; }
@media (max-width: 560px) { .fdd-frow { grid-template-columns: 92px 1fr 52px; } }

/* base-rate stat tiles */
.fdd-rates { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: .9rem; }
.fdd-rate { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px; padding: .95rem 1.05rem; }
.fdd-rate .big { font-family: var(--fdmono); font-weight: 700; font-size: 1.5rem; color: var(--fdup);
  font-variant-numeric: tabular-nums; }
.fdd-rate .txt { font-family: var(--sans); font-size: .78rem; line-height: 1.5; color: var(--fdmut); margin-top: .3rem; }

/* markets table */
.fdd-mkts { border: 1px solid var(--fdline); border-radius: 2px; overflow-x: auto; background: var(--fdcard); }
.fdd-mkts table { border-collapse: collapse; width: 100%; font-size: .8rem; }
.fdd-mkts th, .fdd-mkts td { text-align: left; padding: .65rem .85rem; border-bottom: 1px solid var(--fdline);
  vertical-align: top; font-family: var(--sans); color: var(--fdmut); }
.fdd-mkts thead th { font-size: .6rem; text-transform: uppercase; letter-spacing: .1em; color: var(--fdmut); background: #0c1117; }
.fdd-mkts td .plat { font-weight: 700; color: var(--fdtext); }
.fdd-mkts tbody tr:last-child td { border-bottom: none; }

/* triggers */
.fdd-trigs { counter-reset: t; margin: 0; padding: 0; list-style: none; }
.fdd-trigs li { counter-increment: t; position: relative; padding: .75rem 0 .75rem 2.6rem;
  border-bottom: 1px solid var(--fdline); font-family: var(--serif); font-size: .92rem; line-height: 1.55; color: #c6d0e0; }
.fdd-trigs li:last-child { border-bottom: none; }
.fdd-trigs li::before { content: counter(t); position: absolute; left: 0; top: .7rem; width: 1.7rem; height: 1.7rem;
  border-radius: 50%; background: var(--fdup); color: #0c1117; font-family: var(--fdmono); font-weight: 700;
  font-size: .8rem; display: flex; align-items: center; justify-content: center; }

.fdd-note { font-family: var(--serif); font-style: italic; font-size: .86rem; line-height: 1.6; color: var(--fdmut); margin: 1.2rem 0 0; }
.fdd-src { margin: .8rem 0 0; padding: 0; list-style: none; }
.fdd-src li { font-family: var(--sans); font-size: .76rem; margin: .35rem 0; }
.fdd-src a { color: var(--fdblue); text-decoration: none; }
.fdd-src a:hover { text-decoration: underline; }

/* research-market pages — the harvested corpora in the same live-market dress */
.fdd-pick.cvr { padding: 0; border-color: var(--fdline); overflow: hidden; }
.fdd-pick.cvr img { display: block; width: 132px; height: 132px; object-fit: cover; }
.fdd-pick.cvr .p { padding: .5rem .6rem .6rem; }
.fdr-leadlbl { font-family: var(--sans); font-size: .62rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--fdup); margin: 0 0 .3rem; }
.fdr-leadnm { font-family: var(--display); font-weight: 700; font-size: clamp(1.15rem, 2.6vw, 1.55rem);
  line-height: 1.18; margin: 0 0 .55rem; color: var(--fdtext); }
.fdr-cta { display: inline-block; font-family: var(--sans); font-size: .72rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .1em; color: #0c1117; background: var(--fdup);
  border-radius: 2px; padding: .55rem 1rem; text-decoration: none; margin-top: .8rem;
  transition: filter .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
.fdr-cta:hover { filter: brightness(1.1); transform: translateY(-1px); box-shadow: var(--fdshadow); }
.fdr-cta:active { transform: translateY(0); box-shadow: none; }
.fdr-out { background: var(--fdcard); border: 1px solid var(--fdline); border-radius: 2px;
  padding: 1.1rem 1.25rem; margin: 0 0 1rem; }
.fdr-out.lead { border-color: rgba(34,197,94,.55); }
.fdr-oh { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin-bottom: .55rem; }
.fdr-oh .nm { font-family: var(--sans); font-weight: 800; font-size: 1.02rem; line-height: 1.3; color: var(--fdtext); }
.fdr-oh .pc { font-family: var(--fdmono); font-size: .88rem; font-weight: 700; color: var(--fdmut);
  white-space: nowrap; font-variant-numeric: tabular-nums; }
.fdr-out.lead .fdr-oh .pc { color: #0c1117; background: var(--fdup); border-radius: 2px; padding: .1rem .45rem; }
.fdr-out .fd-track { height: 12px; margin-bottom: .8rem; }
.fdr-desc { font-family: var(--serif); font-size: .95rem; line-height: 1.62; color: #c6d0e0; margin: 0 0 .8rem; }
.fdr-deriv { background: #0c1117; border: 1px dashed var(--fdline); border-radius: 2px; padding: .7rem .85rem;
  font-family: var(--sans); font-size: .76rem; line-height: 1.5; color: var(--fdmut); margin: 0 0 .8rem; }
.fdr-deriv b { color: var(--fdup); font-size: .58rem; text-transform: uppercase; letter-spacing: .1em; }
.fdr-lists { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.fdr-l h4 { font-family: var(--sans); font-size: .6rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--fdmut); margin: 0 0 .4rem; }
.fdr-l ul { margin: 0; padding: 0 0 0 1rem; }
.fdr-l li { font-family: var(--sans); font-size: .78rem; line-height: 1.5; color: var(--fdmut); margin: .25rem 0; }
.fdr-hz { font-family: var(--fdmono); font-size: .7rem; color: var(--fdmut); border-top: 1px solid var(--fdline);
  margin-top: .85rem; padding-top: .65rem; }
@media (max-width: 620px) { .fdr-lists { grid-template-columns: 1fr; } .fdd-pick.cvr img { width: 100%; height: 110px; } }
@media (prefers-reduced-motion: reduce) {
  .fdd-trader, .fdd-trader:hover,
  .fdr-cta, .fdr-cta:hover, .fdr-cta:active {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}

/* predictor profile page */
.fdp-crit { font-family: var(--serif); font-style: italic; color: var(--fdmut); font-size: 1rem;
  line-height: 1.5; margin: 0 0 .5rem; }
.fdp-zone { font-family: var(--sans); font-size: .72rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .1em; color: var(--fdmut); margin: 0 0 .7rem; }
.fdp-prose { font-family: var(--serif); font-size: .98rem; line-height: 1.62; color: #c6d0e0; margin: 0; }
.fdp-spx { display: grid; grid-template-columns: auto 1fr auto; gap: .7rem; align-items: center; margin-top: .3rem; }
.fdp-spx-end { font-family: var(--fdmono); font-size: .68rem; color: var(--fdmut); white-space: nowrap; }
.fdp-spx-track { position: relative; height: 4px; background: var(--fdline); border-radius: 2px; }
.fdp-spx-pip { position: absolute; top: 50%; width: 14px; height: 14px; border-radius: 50%;
  transform: translate(-50%, -50%); border: 2px solid #10161f; }
.fdt-p-link { display: block; text-decoration: none; color: inherit; transition: transform .12s, border-color .12s; }
.fdt-p-link:hover { transform: translateY(-2px); border-color: var(--fdup) !important; }

/* predictor comparison table */
.cmp-wrap { overflow-x: auto; border: 1px solid var(--fdline); border-radius: 2px; background: var(--fdcard); }
.cmp-table { border-collapse: collapse; width: 100%; min-width: 960px; }
.cmp-table th, .cmp-table td { border-bottom: 1px solid var(--fdline); padding: .75rem .85rem; text-align: left;
  vertical-align: top; font-family: var(--sans); font-size: .78rem; color: var(--fdmut); }
.cmp-table tbody tr:last-child th, .cmp-table tbody tr:last-child td { border-bottom: none; }
.cmp-table thead th { background: #0c1117; }
.cmp-table tbody th { font-family: var(--sans); font-size: .64rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--fdtext); white-space: nowrap; position: sticky; left: 0; background: var(--fdcard); }
.cmp-persona { border-top: 3px solid var(--fdmut); min-width: 190px; }
.cmp-persona a { display: flex; flex-direction: column; align-items: center; gap: .3rem; text-decoration: none; color: inherit; }
.cmp-av { font-size: 1.5rem; line-height: 1; }
.cmp-nm { font-family: var(--sans); font-weight: 800; font-size: .82rem; color: var(--fdtext); }
.cmp-role { font-family: var(--sans); font-size: .56rem; font-weight: 800; text-transform: uppercase;
  letter-spacing: 0; color: var(--fdmut); }
.cmp-persona a:hover .cmp-nm { color: var(--fdblue); }
.cmp-crit, .cmp-prose, .cmp-sig { display: block; font-family: var(--serif); font-style: italic; font-size: .88rem;
  line-height: 1.55; color: #c6d0e0; }
.cmp-zone { display: block; font-family: var(--sans); font-size: .6rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: var(--fdmut); margin-bottom: .4rem; }
"""

FORECAST_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="../index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<div class="fdd-wrap">
<header class="fd-plate">
  <p class="fd-kicker">{kicker}</p>
  <h1 class="fd-name" style="font-size:clamp(1.9rem,4.6vw,3rem)">{headline}</h1>
  {scene}
  <div class="fd-folio">
{folio}
  </div>
</header>
<main class="fd-board fdd" id="fd-board">
{body}
</main>
<footer class="fd-foot">
  <p class="epigraph">{method_note}</p>
  <p class="colophon"><a href="../forecast.html">← The Forecast Desk</a> &nbsp;·&nbsp; <a href="../research.html">The Research Library</a></p>
</footer>
</div>
<script>{theme_js}</script>
<script>{app_js}</script>
{shell}
</body>
</html>
"""


def _fdd_hero(d, graded=None):
    c = d.get("consensus", {})
    tally = c.get("tally", [])
    total = sum(t.get("votes", 0) for t in tally) or 1
    seg = "".join(f'<span class="t{i+1}" style="width:{t["votes"]/total*100:.1f}%" title="{html.escape(t.get("team") or t.get("name", ""))}"></span>'
                  for i, t in enumerate(tally[:3]))
    leg = " ".join(f'<span>{t.get("flag", "")} <b>{html.escape(t.get("team") or t.get("name", ""))}</b> {t["votes"]}</span>' for t in tally)
    res = html.escape(d.get("resolution", ""))
    if graded:
        hit = graded["consensus"]["hit"]
        chip = f'<span class="fd-chip-graded{"" if hit else " miss"}">{"✓" if hit else "✗"} Graded</span>'
        date_bit = (f'<span class="fd-chip-date">graded '
                    f'{html.escape(_long_date(graded["resolved"]) if graded.get("resolved") else "")}</span>')
    else:
        chip = '<span class="fd-chip-live"><span class="d"></span>Live</span>'
        date_bit = f'<span class="fd-chip-date" data-grades="{html.escape(d.get("grades", ""), quote=True)}"></span>'
    return (
        f'<section class="fdd-hero">'
        f'<div class="fd-live-top">{chip}'
        f'<span class="fd-chip-cat">{html.escape(d.get("category", ""))}</span>'
        f'{date_bit}</div>'
        f'<div class="fdd-pickrow"><div class="fdd-pick">'
        f'<div class="f">{c.get("flag", "")}</div><div class="t">{html.escape(c.get("pick", ""))}</div>'
        f'<div class="p">{html.escape(c.get("band", ""))}</div>'
        + (f'<div class="ru">runner-up: {c.get("runner_up_flag", "")} {html.escape(c.get("runner_up", ""))} {html.escape(c.get("runner_up_band", ""))}</div>'
           if c.get("runner_up") else "") + '</div>'
        f'<div><div class="fdd-tally">{seg}</div><div class="fdd-tally-l">{leg}'
        f'<span style="margin-left:auto">council split · fused by grounding strength, not votes</span></div>'
        f'<p class="fdd-res">{res}</p></div></div></section>'
    )


def _fdd_roster(profiles, records=None, graded=None):
    """Trader cards for a roster. `records` (persona key → {graded, hits}) swaps
    the static record tag for the build-computed cumulative one; `graded`
    (grade_native_forecast output) stamps each card's call ✓ hit / ✗ miss."""
    verdicts = {p["persona"]: p for p in (graded or {}).get("profiles", [])}
    cards = []
    for p in profiles:
        key = _persona_key(p)
        agent = _persona_agent_name(key, p.get("name", ""))
        role = _persona_role(key, p.get("name", ""))
        rec = (records or {}).get(key) or p.get("record") or {}
        n_graded = rec.get("graded", 0)
        rec_tag = (f'<span class="fdd-tag rec">record {rec.get("hits", 0)}–{max(n_graded - rec.get("hits", 0), 0)}</span>'
                   if n_graded else '<span class="fdd-tag rec">first call on the ledger</span>')
        v = verdicts.get(key)
        v_tag = (f'<span class="fdd-tag {"hit" if v["hit"] else "missed"}">'
                 f'{"✓ hit" if v["hit"] else "✗ miss"} · Brier {v["brier"]:.3f}</span>' if v else "")
        conf = (p.get("confidence") or "medium").lower()
        cards.append(
            f'<a class="fdd-trader" href="../forecasters/{key}.html">'
            f'<div class="fdd-tr-top">{_persona_mascot_html(key, "card")}'
            f'<div><div class="fdd-tr-nm">{html.escape(agent)}</div>'
            f'<div class="fdd-tr-role">{html.escape(role)}</div>'
            f'<div class="fdd-tr-crit">{html.escape(p.get("criterion", ""))}</div></div>'
            f'<div class="fdd-tr-call"><div class="f">{p.get("flag", "")}</div>'
            f'<div class="t">{html.escape(p.get("pick", ""))}</div>'
            f'<div class="p">{html.escape(p.get("prob", ""))}</div></div></div>'
            f'<div class="fdd-tr-tags"><span class="fdd-tag reg">✍ {html.escape(p.get("register", ""))}</span>'
            f'<span class="fdd-tag conf-{conf}">{conf} conf.</span>{rec_tag}{v_tag}</div>'
            f'<p class="fdd-tr-blurb">{html.escape(p.get("blurb", ""))}</p>'
            f'<div class="fdd-tr-ev"><b>Key evidence</b> · {html.escape(p.get("evidence", ""))}</div>'
            f'<span class="fdd-tr-cta">Full profile →</span>'
            f'</a>'
        )
    return f'<div class="fdd-roster">{"".join(cards)}</div>'


def _fdd_field(field):
    if not field:
        return ""
    top = max(f.get("price", 0) for f in field) or 1
    rows = []
    for i, f in enumerate(field):
        w = f.get("price", 0) / top * 100
        col = FD_OUT_COLORS[i % len(FD_OUT_COLORS)]
        rows.append(
            f'<div class="fdd-frow{" lead" if i == 0 else ""}">'
            f'<span class="tm">{f.get("flag", "")} {html.escape(f.get("team", ""))}</span>'
            f'<div class="fd-track"><span class="fd-fill" style="left:0;width:{w:.1f}%;background:{col}"></span></div>'
            f'<span class="pv" style="color:{col}">{f.get("price", 0):g}%</span></div>'
        )
    return (f'<div class="fdd-field">{"".join(rows)}'
            f'<div class="fdd-field-foot">Blended exact-winner price across the liquid books · bars scaled to the leader</div></div>')


def build_forecast_item(out_dir, item, shell="", records=None):
    """Render one native forecast (docs/forecast/{slug}.html) from its data file.
    A graded item (item['_graded'] joined from the resolutions ledger) wears the
    verdict: graded hero, "what happened" banner, ✓/✗-stamped trader cards, and
    the build-computed cumulative `records` on the roster tags.
    Returns True if rendered, False if the data file is absent."""
    out = Path(out_dir)
    slug = item.get("slug", "")
    d = read_forecast_data(out, slug)
    if d is None:
        return False
    g = item.get("_graded")
    c = d.get("consensus", {})
    parts = [_fdd_hero(d, graded=g)]
    if g:
        gc = g["consensus"]
        note = html.escape(g.get("note", ""))
        src = (f'<div class="src">Result per <a href="{html.escape(g["source_url"], quote=True)}" target="_blank" '
               f'rel="noopener">{html.escape(g.get("source_label") or g["source_url"])}</a></div>'
               if g.get("source_url") else "")
        verdict_line = (f'{g.get("winner_flag", "")} <b>{html.escape(g["winner"])}</b> won. The council called '
                        f'{gc.get("flag", "")} <b>{html.escape(gc["pick"])}</b> at {gc["prob"]:g}% — '
                        f'{"the desk called it" if gc["hit"] else "the desk missed"} '
                        f'(Brier {gc["brier"]:.3f}).')
        parts.append(f'<section class="fdd-verdict{"" if gc["hit"] else " miss"}">'
                     f'<span class="lbl">{"✓" if gc["hit"] else "✗"} The Grade — what happened</span>'
                     f'<p>{verdict_line}{" " + note if note else ""}</p>{src}</section>')
    if d.get("profiles"):
        parts.append(f'<h2 class="fdd-h"><span class="n">01</span>The Predictor Roster — {len(d["profiles"])} standing profiles, tracked call by call</h2>')
        parts.append(_fdd_roster(d["profiles"], records=records, graded=g))
    n = 2
    if c.get("headline_case"):
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Consensus</h2>')
        cons = [f'<p><span class="lbl">The call</span>{html.escape(c["headline_case"])}</p>']
        if c.get("why_over_runner_up"):
            cons.append(f'<p><span class="lbl">Why over the runner-up</span>{html.escape(c["why_over_runner_up"])}</p>')
        if c.get("dissent"):
            cons.append(f'<div class="fdd-dissent"><span class="lbl">Strongest surviving dissent</span>{html.escape(c["dissent"])}</div>')
        parts.append(f'<div class="fdd-cons">{"".join(cons)}</div>')
        n += 1
    if d.get("field"):
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Field</h2>')
        parts.append(_fdd_field(d["field"]))
        n += 1
    if d.get("outcomes"):
        # Scenario-shaped native runs (no single named winner) deposit rich
        # `outcomes` instead of a `field` — same articles as a research market.
        outs = [x for x in (_norm_outcome(o) for o in d["outcomes"]) if x]
        if outs:
            parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Outcomes — {len(outs)} scenarios, priced</h2>')
            parts.append(_fdr_outcomes(outs))
            n += 1
    if d.get("base_rates"):
        tiles = "".join(f'<div class="fdd-rate"><div class="big">{html.escape(r.get("stat", ""))}</div>'
                        f'<div class="txt">{html.escape(r.get("text", ""))}</div></div>'
                        for r in d["base_rates"])
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Base Rates</h2><div class="fdd-rates">{tiles}</div>')
        n += 1
    if d.get("markets"):
        rows = "".join(
            f'<tr><td><span class="plat">{html.escape(m.get("platform", ""))}</span><br>{html.escape(m.get("market", ""))}</td>'
            f'<td>{html.escape(m.get("detail", ""))}</td><td>{html.escape(m.get("volume", ""))}</td></tr>'
            for m in d["markets"])
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Market Snapshot</h2>'
                     f'<div class="fdd-mkts"><table><thead><tr><th>Market</th><th>Prices</th><th>Volume</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
        n += 1
    if d.get("triggers"):
        lis = "".join(f'<li>{html.escape(t)}</li>' for t in d["triggers"])
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>What Would Flip the Pick</h2><ol class="fdd-trigs">{lis}</ol>')
        n += 1
    if d.get("sources"):
        lis = "".join(f'<li><a href="{html.escape(s.get("url", ""), quote=True)}" target="_blank" rel="noopener">'
                      f'{html.escape(s.get("label", s.get("url", "")))}</a></li>' for s in d["sources"])
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Sources</h2><ul class="fdd-src">{lis}</ul>')
    if d.get("honesty_note"):
        parts.append(f'<p class="fdd-note">{html.escape(d["honesty_note"])}</p>')
    title = d.get("title") or item.get("title") or slug
    grades = d.get("grades", item.get("grades", ""))
    og = og_tags(title, d.get("question", title), f"{SITE_URL}/forecast/{slug}.html", f"{SITE_URL}/{OG_IMAGE}")
    logged = html.escape(_long_date(d.get("logged", "")) if d.get("logged") else "—")
    if g:
        graded_span = (f'    <span class="fd-folio-c">Graded '
                       f'{html.escape(_long_date(g["resolved"]) if g.get("resolved") else "")} · '
                       f'{"✓ hit" if g["consensus"]["hit"] else "✗ miss"}</span>\n')
    else:
        graded_span = (f'    <span class="fd-folio-c" data-grades="{html.escape(grades, quote=True)}">'
                       f'Grades {html.escape(_long_date(grades) if grades else "—")}</span>\n')
    folio = (f'    <span>Logged {logged}</span>\n'
             f'{graded_span}'
             f'    <span>{html.escape(d.get("category", ""))}</span>')
    page = FORECAST_DETAIL_TEMPLATE.format(
        title=html.escape(f"{title} — The Forecast Desk"),
        description=html.escape(d.get("question", title)),
        favicon=FAVICON, og_meta=og,
        nav=main_nav_html(prefix="../", active="forecast.html"),
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_DETAIL_CSS,
        kicker=f'The Forecast Desk · {"Graded market" if g else "Live market"}',
        headline=html.escape(d.get("question", title)),
        scene=scene_plate("forecast", extra_class="detail-scene", root="../", seed=f"forecast-native:{slug}"),
        folio=folio,
        method_note=html.escape(d.get("method_note", "")),
        body="\n".join(parts),
        theme_js=LIBRARY_THEME_JS,
        app_js=FORECAST_PAGE_JS,
        shell=shell,
    )
    (out / "forecast").mkdir(parents=True, exist_ok=True)
    (out / "forecast" / f"{slug}.html").write_text(_persona_public_copy(page))
    return True


def _norm_outcome(o):
    """Normalize a deposited outcome (native data file) to the harvested-outcome
    shape _fdr_outcomes renders. Accepts numeric low/high, a band/probability
    string ('26–32%'), and the-forecaster's field names (signals_to_watch,
    time_horizon). Returns None when no probability is parseable."""
    if not isinstance(o, dict) or not o.get("name"):
        return None
    if isinstance(o.get("low"), (int, float)) and isinstance(o.get("high"), (int, float)):
        band = (float(o["low"]), float(o["high"]))
    else:
        band = _prob_band({"probability": o.get("band") or o.get("probability")})
    if band is None:
        return None
    def _lst(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    return {"name": o["name"], "low": band[0], "high": band[1],
            "description": (o.get("description") or "").strip(),
            "derivation": (o.get("derivation") or "").strip(),
            "drivers": _lst(o.get("drivers")),
            "signals": _lst(o.get("signals") or o.get("signals_to_watch")),
            "horizon": (o.get("horizon") or o.get("time_horizon") or "").strip()}


def _fdr_outcomes(outcomes, resolved_idx=None):
    """The priced-outcome articles: name + filled band bar, then the description,
    derivation, drivers / signals, and horizon. Shared by harvested research
    markets and scenario-shaped native forecasts. On a graded market
    (resolved_idx set) the outcome that happened is stamped ✓ and the rest ✗."""
    outs = []
    for i, o in enumerate(outcomes):
        low = max(min(o["low"], 100.0), 0.0)
        high = max(min(o["high"], 100.0), low)
        col = FD_OUT_COLORS[i % len(FD_OUT_COLORS)]
        bars = f'<span class="fd-fill" style="width:{max(low, 1.5):.1f}%;background:{col}"></span>'
        if high - low >= .5:
            bars += f'<span class="fd-fill rng" style="left:{low:.1f}%;width:{high - low:.1f}%;background:{col}"></span>'
        pc = (f'<span class="pc" style="background:{col};color:#0c1117">{_fmt_band(o["low"], o["high"])}</span>'
              if i == 0 else f'<span class="pc" style="color:{col}">{_fmt_band(o["low"], o["high"])}</span>')
        mark = ""
        if resolved_idx is not None:
            mark = ('<span class="fd-mk won">✓ happened</span>' if i == resolved_idx
                    else '<span class="fd-mk lost">✗</span>')
        bits = [f'<div class="fdr-oh"><span class="nm">{mark}{html.escape(o["name"])}</span>{pc}</div>'
                f'<div class="fd-track">{bars}</div>']
        if o.get("description"):
            bits.append(f'<p class="fdr-desc">{html.escape(o["description"])}</p>')
        if o.get("derivation"):
            bits.append(f'<div class="fdr-deriv"><b>How this number was derived</b><br>{html.escape(o["derivation"])}</div>')
        lists = []
        if o.get("drivers"):
            lis = "".join(f'<li>{html.escape(x)}</li>' for x in o["drivers"])
            lists.append(f'<div class="fdr-l"><h4>Drivers</h4><ul>{lis}</ul></div>')
        if o.get("signals"):
            lis = "".join(f'<li>{html.escape(x)}</li>' for x in o["signals"])
            lists.append(f'<div class="fdr-l"><h4>Signals to watch</h4><ul>{lis}</ul></div>')
        if lists:
            bits.append(f'<div class="fdr-lists">{"".join(lists)}</div>')
        if o.get("horizon"):
            bits.append(f'<div class="fdr-hz">Horizon · {html.escape(o["horizon"])}</div>')
        outs.append(f'<article class="fdr-out{" lead" if i == 0 else ""}" style="border-top:3px solid {col}">{"".join(bits)}</article>')
    return "".join(outs)


def build_corpus_market_page(out_dir, m, shell="", records=None):
    """Render a harvested research market's own desk page — forecast/{slug}.html —
    in the FULL live-market dress of a native forecast. Every outcome is priced
    with a filled bar plus its description / derivation / drivers / signals, and
    the hero deep-links into the corpus's Future Trajectory chapter. When the
    corpus manifest carries a `forecast_desk` dossier (written by the-forecaster),
    the page also gets the World-Cup-page anatomy: desk-split tally in the hero,
    the predictor roster, the consensus (call / why / dissent), base-rate tiles,
    the market snapshot, what-would-change-the-call triggers, sources, and the
    grading countdown chip. A graded market (m['resolution'] joined from the
    resolutions ledger) wears the verdict: graded chip, "what happened" banner,
    ✓/✗-stamped outcomes, and — with a dossier — a ✓/✗-stamped roster."""
    out = Path(out_dir)
    desk = m.get("desk") or {}
    cons = desk.get("consensus") or {}
    profiles = desk.get("profiles") or []
    r = m.get("resolution")
    # A graded dossier roster gets per-profile verdicts, same shape the native
    # page uses, so _fdd_roster can stamp the cards.
    rg = None
    if r and profiles:
        wkey = r["name"].strip().casefold()
        rg_profiles = []
        for p in profiles:
            prob = p.get("prob_num")
            if not isinstance(prob, (int, float)):
                band = _prob_band({"probability_range": p.get("prob", "")})
                prob = (band[0] + band[1]) / 2 if band else 50.0
            hit = ((p.get("pick_scenario") or p.get("pick", "")).strip().casefold() == wkey)
            rg_profiles.append({"persona": _persona_key(p), "hit": hit,
                                "brier": _brier(float(prob), hit)})
        rg = {"profiles": rg_profiles}
    lead = m["outcomes"][0]
    research = "../" + m["research_href"]
    ru_bit = (f'<div class="ru">runner-up: {html.escape(cons.get("runner_up", ""))} '
              f'{html.escape(cons.get("runner_up_band", ""))}</div>'
              if cons.get("runner_up") else "")
    cover_sq = (f'<div class="fdd-pick cvr"><img src="../{html.escape(m["cover"], quote=True)}" alt="">'
                f'<div class="p">{_fmt_band(lead["low"], lead["high"])}</div>{ru_bit}</div>'
                if m.get("cover") else
                f'<div class="fdd-pick"><div class="f">📚</div>'
                f'<div class="p">{_fmt_band(lead["low"], lead["high"])}</div>{ru_bit}</div>')
    status = (desk.get("status") or "open").strip().lower()
    grades = (desk.get("grades") or "").strip()
    if r:
        status_chip = (f'<span class="fd-chip-graded{"" if r["lead_hit"] else " miss"}">'
                       f'{"✓" if r["lead_hit"] else "✗"} Graded</span>')
        grades_chip = (f'<span class="fd-chip-date">graded '
                       f'{html.escape(_long_date(r["resolved"]) if r.get("resolved") else "")}</span>')
    else:
        status_chip = f'<span class="fd-chip-open">{html.escape(status.capitalize())}</span>'
        grades_chip = (f'<span class="fd-chip-date" data-grades="{html.escape(grades, quote=True)}"></span>'
                       if grades else "")
    # Desk-split tally across the roster's picks, worn like the native hero's.
    tally_bit = ""
    if profiles:
        counts = {}
        for p in profiles:
            k = (p.get("pick") or "").strip()
            if k:
                counts[k] = counts.get(k, 0) + 1
        tally = sorted(counts.items(), key=lambda kv: -kv[1])
        total = sum(counts.values()) or 1
        seg = "".join(f'<span class="t{i+1}" style="width:{v/total*100:.1f}%" title="{html.escape(k)}"></span>'
                      for i, (k, v) in enumerate(tally[:3]))
        leg = " ".join(f'<span><b>{html.escape(k)}</b> {v}</span>' for k, v in tally[:3])
        tally_bit = (f'<div class="fdd-tally">{seg}</div><div class="fdd-tally-l">{leg}'
                     f'<span style="margin-left:auto">desk split · consensus fused by grounding strength, not votes</span></div>')
    hero = (
        f'<section class="fdd-hero">'
        f'<div class="fd-live-top">{status_chip}'
        f'<span class="fd-chip-cat" style="color:{_fd_cat_color(m["category"])};border-color:{_fd_cat_color(m["category"])}">{html.escape(m["category"])}</span>'
        f'<span class="fd-chip-date">{html.escape(m["horizon"])}</span>{grades_chip}</div>'
        f'<div class="fdd-pickrow">{cover_sq}'
        f'<div><p class="fdr-leadlbl">{"The desk\'s lead call was" if r else "Most likely outcome"}</p>'
        f'<p class="fdr-leadnm">{html.escape(lead["name"])}</p>'
        f'{tally_bit}'
        f'<p class="fdd-res">{html.escape(desk.get("resolution") or m.get("subtitle", ""))}</p>'
        f'<a class="fdr-cta" href="{html.escape(research, quote=True)}">Read the full research →</a>'
        f'</div></div></section>'
    )
    parts = [hero]
    if r:
        note = html.escape(r.get("note", ""))
        src = (f'<div class="src">Result per <a href="{html.escape(r["source_url"], quote=True)}" target="_blank" '
               f'rel="noopener">{html.escape(r.get("source_label") or r["source_url"])}</a></div>'
               if r.get("source_url") else "")
        verdict_line = (f'<b>{html.escape(r["name"])}</b> is what happened. The research\'s lead call was '
                        f'<b>{html.escape(lead["name"])}</b> at {_fmt_band(lead["low"], lead["high"])} — '
                        f'{"the desk called it" if r["lead_hit"] else "the desk missed"} '
                        f'(Brier {r["brier"]:.3f} on the lead call).')
        parts.append(f'<section class="fdd-verdict{"" if r["lead_hit"] else " miss"}">'
                     f'<span class="lbl">{"✓" if r["lead_hit"] else "✗"} The Grade — what happened</span>'
                     f'<p>{verdict_line}{" " + note if note else ""}</p>{src}</section>')
    n = 1
    if profiles:
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Predictor Roster — {len(profiles)} standing profiles, tracked call by call</h2>')
        parts.append(_fdd_roster(profiles, records=records, graded=rg))
        n += 1
    if cons.get("headline_case"):
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Consensus</h2>')
        cbits = [f'<p><span class="lbl">The call</span>{html.escape(cons["headline_case"])}</p>']
        if cons.get("why_over_runner_up"):
            cbits.append(f'<p><span class="lbl">Why over the runner-up</span>{html.escape(cons["why_over_runner_up"])}</p>')
        if cons.get("dissent"):
            cbits.append(f'<div class="fdd-dissent"><span class="lbl">Strongest surviving dissent</span>{html.escape(cons["dissent"])}</div>')
        parts.append(f'<div class="fdd-cons">{"".join(cbits)}</div>')
        n += 1
    parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Outcomes — {len(m["outcomes"])} scenarios, priced by the research</h2>')
    parts.append(_fdr_outcomes(m["outcomes"], resolved_idx=r["idx"] if r else None))
    parts.append(f'<p class="fdd-note">Probabilities are the research\'s own scenario bands, priced as outcomes. '
                 f'The full argument — history, current state, drivers, and sources — lives in the corpus: '
                 f'<a href="{html.escape(research, quote=True)}" style="color:var(--fdblue)">read the Future Trajectory chapter</a>.</p>')
    n += 1
    if desk.get("base_rates"):
        tiles = "".join(f'<div class="fdd-rate"><div class="big">{html.escape(r.get("stat", ""))}</div>'
                        f'<div class="txt">{html.escape(r.get("text", ""))}</div></div>'
                        for r in desk["base_rates"] if isinstance(r, dict))
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Base Rates</h2><div class="fdd-rates">{tiles}</div>')
        n += 1
    if desk.get("markets"):
        rows = "".join(
            f'<tr><td><span class="plat">{html.escape(mk.get("platform", ""))}</span><br>{html.escape(mk.get("market", ""))}</td>'
            f'<td>{html.escape(mk.get("detail", ""))}</td><td>{html.escape(mk.get("volume", ""))}</td></tr>'
            for mk in desk["markets"] if isinstance(mk, dict))
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>The Market Snapshot</h2>'
                     f'<div class="fdd-mkts"><table><thead><tr><th>Market</th><th>Prices</th><th>Volume</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
        n += 1
    if desk.get("triggers"):
        lis = "".join(f'<li>{html.escape(str(t))}</li>' for t in desk["triggers"])
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>What Would Change the Call</h2><ol class="fdd-trigs">{lis}</ol>')
        n += 1
    if desk.get("sources"):
        lis = "".join(f'<li><a href="{html.escape(s.get("url", ""), quote=True)}" target="_blank" rel="noopener">'
                      f'{html.escape(s.get("label", s.get("url", "")))}</a></li>'
                      for s in desk["sources"] if isinstance(s, dict))
        parts.append(f'<h2 class="fdd-h"><span class="n">{n:02d}</span>Sources</h2><ul class="fdd-src">{lis}</ul>')
        n += 1
    if desk.get("honesty_note"):
        parts.append(f'<p class="fdd-note">{html.escape(desk["honesty_note"])}</p>')
    og_img = f"{SITE_URL}/{m['cover']}" if m.get("cover") else f"{SITE_URL}/{OG_IMAGE}"
    og = og_tags(m["title"], m.get("subtitle") or m["title"],
                 f"{SITE_URL}/forecast/{m['slug']}.html", og_img)
    if r:
        grades_folio = (f'    <span class="fd-folio-c">Graded '
                        f'{html.escape(_long_date(r["resolved"]) if r.get("resolved") else "")} · '
                        f'{"✓ hit" if r["lead_hit"] else "✗ miss"}</span>\n')
    else:
        grades_folio = (f'    <span class="fd-folio-c" data-grades="{html.escape(grades, quote=True)}">'
                        f'Grades {html.escape(_long_date(grades))}</span>\n' if grades else "")
    folio = (f'    <span>{html.escape(m["horizon"] or "Open")}</span>\n'
             f'    <span class="fd-folio-c">{len(m["outcomes"])} priced outcomes</span>\n'
             f'{grades_folio}'
             f'    <span>{html.escape(m["category"])}</span>')
    page = FORECAST_DETAIL_TEMPLATE.format(
        title=html.escape(f"{m['title']} — The Forecast Desk"),
        description=html.escape(m.get("subtitle") or m["title"]),
        favicon=FAVICON, og_meta=og,
        nav=main_nav_html(prefix="../", active="forecast.html"),
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FORECAST_DETAIL_CSS,
        kicker="The Forecast Desk · Research market",
        headline=html.escape(m["title"]),
        scene=scene_plate("forecast", extra_class="detail-scene", root="../", seed=f"forecast-corpus:{m['slug']}"),
        folio=folio,
        method_note=html.escape(desk.get("method_note", "")) or
                    "Priced from the corpus’s forecast scenarios — every band grounded in the research it links to.",
        body="\n".join(parts),
        theme_js=LIBRARY_THEME_JS,
        app_js=FORECAST_PAGE_JS,
        shell=shell,
    )
    (out / "forecast").mkdir(parents=True, exist_ok=True)
    (out / "forecast" / f"{m['slug']}.html").write_text(_persona_public_copy(page))
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
    "festival-desk":        ("#1c5fa8", "#6fa8e0"),
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
    "festival-desk":
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
        '<path d="M 9.5 20 Q 4.5 15 7 8"/><path d="M 14.5 20 Q 19.5 15 17 8"/>'
        '<polygon points="12,5 12.9,7.3 15.3,7.4 13.4,8.95 14.05,11.3 12,9.95 9.95,11.3 10.6,8.95 8.7,7.4 11.1,7.3" '
        'fill="currentColor" stroke="none"/></g></svg>',
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
    scene_seed = f"fingerprint-band:{editions[0].get('date', '') if editions else 'empty'}"
    scene = scene_plate("fingerprint", extra_class="band-scene", seed=scene_seed)
    return (f'<div class="fp-band"><a href="fingerprint.html">{flag}{scene}{mid}'
            f'<span class="fp-band-cta">{cta}</span></a></div>')


# Home-page band CSS (added to LIBRARY_CSS at build time).
FINGERPRINT_BAND_CSS = """
/* The Fingerprint announcement row — the market wire's line in the index's
   ruled announcements column, signed in petrol teal (house accent barred). */
.fp-band { max-width: 1120px; margin: 0 auto; padding: 0 2rem; }
.fp-band a { display: grid; grid-template-columns: auto minmax(118px, 142px) 1fr auto; align-items: center; gap: 1.6rem;
  text-decoration: none; color: var(--text); background: transparent;
  border: 0; border-top: 1px solid var(--border); border-left: 3px solid #0d5b68;
  border-radius: 0; box-shadow: none; padding: 1.3rem 0 1.4rem 1.1rem; position: relative;
  transition: border-color .15s var(--ease); }
.fp-band a:hover { border-left-color: #11808f; }
[data-theme="dark"] .fp-band a { border-left-color: #62aab8; }
.fp-band .fp-band-flag { font-family: var(--display); font-weight: 600; font-size: 1.85rem; line-height: 1;
  letter-spacing: -.01em; color: var(--text); border-right: 1px solid var(--border); padding-right: 1.5rem; }
.fp-band .fp-band-flag small { display: block; font-family: var(--sans); font-size: .55rem; font-weight: 400;
  letter-spacing: .2em; text-transform: uppercase; color: #0d5b68; margin-top: .5rem; }
[data-theme="dark"] .fp-band .fp-band-flag small { color: #62aab8; }
.fp-band .fp-band-mid { min-width: 0; }
.fp-band .fp-band-kicker { font-family: var(--sans); font-size: .66rem; text-transform: uppercase;
  letter-spacing: .16em; color: #0d5b68; margin: 0 0 .35rem; }
[data-theme="dark"] .fp-band .fp-band-kicker { color: #62aab8; }
.fp-band .fp-band-lead { font-family: var(--display); font-size: 1.15rem; line-height: 1.28; margin: 0 0 .4rem; color: var(--text);
  transition: color .15s var(--ease); }
.fp-band a:hover .fp-band-lead, .fp-band a:focus-visible .fp-band-lead { color: #0d5b68; }
[data-theme="dark"] .fp-band a:hover .fp-band-lead,
[data-theme="dark"] .fp-band a:focus-visible .fp-band-lead { color: #62aab8; }
.fp-band .fp-band-ticker { font-family: var(--mono); font-size: .68rem;
  letter-spacing: .04em; color: var(--muted); margin: 0; text-transform: uppercase;
  font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fp-band .fp-band-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: #0d5b68; background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
[data-theme="dark"] .fp-band .fp-band-cta { color: #62aab8; }
.fp-band a:hover .fp-band-cta, .fp-band a:focus-visible .fp-band-cta {
  background: #0d5b68; border-color: #0d5b68; color: #fcfbf7; transform: translateX(3px); }
[data-theme="dark"] .fp-band a:hover .fp-band-cta,
[data-theme="dark"] .fp-band a:focus-visible .fp-band-cta {
  background: #62aab8; border-color: #62aab8; color: #0c1117; }
@media (max-width: 680px) {
  .fp-band a { grid-template-columns: 1fr; gap: .8rem; }
  .fp-band .fp-band-flag { border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 .8rem; }
  .fp-band .scene-plate { max-width: 320px; }
}
@media (prefers-reduced-motion: reduce) {
  .fp-band .fp-band-cta,
  .fp-band a:hover .fp-band-cta, .fp-band a:focus-visible .fp-band-cta {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""


# ---- the section front: fingerprint.html ----

FINGERPRINT_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>The Fingerprint — calvincollins · xyz</title>
<meta name="description" content="{motto}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="fpp-plate">
  <p class="fpp-kicker">{kicker}</p>
  <h1 class="fpp-name">The Fingerprint</h1>
  <div class="fpp-ridges" aria-hidden="true">{ridges}</div>
  <p class="fpp-motto">“{motto}”</p>
  {scene}
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
  <p class="colophon"><a href="adtech.html">← Back to the Ad Tech desk</a> · <a href="research.html">The Research Library</a></p>
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
.fpp-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.4rem, 6vw, 4rem);
  line-height: .98; letter-spacing: -.01em; margin: 0 0 .3rem; }
.fpp-ridges { color: #0d5b68; opacity: .8; margin: 0 0 .6rem; }
[data-theme="dark"] .fpp-ridges { color: #62aab8; }
.fpp-ridge-svg { width: 200px; height: 50px; }
.fpp-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0 0 1.3rem; }
.fpp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--mono); font-size: .72rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--text); }
.fpp-folio .fpp-folio-c { color: #0d5b68; font-weight: 700; }
[data-theme="dark"] .fpp-folio .fpp-folio-c { color: #62aab8; }

/* featured (latest) edition — a section lead */
.fped-feature { max-width: 720px; margin: 2.1rem auto 0; padding: 0 2rem; }
.fped-feature a { display: block; text-decoration: none; color: var(--text); }
.fpedf-meta { font-family: var(--mono); font-size: .72rem; letter-spacing: .04em;
  text-transform: uppercase; font-variant-numeric: tabular-nums; color: #0d5b68; margin: 0 0 .7rem; }
[data-theme="dark"] .fpedf-meta { color: #62aab8; }
.fpedf-head { font-family: var(--display); font-weight: 600; font-size: clamp(1.9rem, 4.2vw, 2.7rem);
  line-height: 1.07; letter-spacing: -.01em; margin: 0 0 .7rem; transition: color .15s var(--ease); }
.fped-feature a:hover .fpedf-head { color: #0d5b68; }
[data-theme="dark"] .fped-feature a:hover .fpedf-head { color: #62aab8; }
.fpedf-dek { font-family: var(--serif); font-style: italic; font-size: 1.2rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 .9rem; }
.fpedf-beats { font-family: var(--mono); font-size: .7rem; letter-spacing: .02em;
  text-transform: uppercase; font-variant-numeric: tabular-nums; color: var(--muted); margin: 0 0 1.1rem; }
.fpedf-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: #0d5b68; background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
[data-theme="dark"] .fpedf-cta { color: #62aab8; }
.fped-feature a:hover .fpedf-cta, .fped-feature a:focus-visible .fpedf-cta {
  background: #0d5b68; border-color: #0d5b68; color: #fcfbf7; transform: translateX(3px); }
[data-theme="dark"] .fped-feature a:hover .fpedf-cta,
[data-theme="dark"] .fped-feature a:focus-visible .fpedf-cta {
  background: #62aab8; border-color: #62aab8; color: #0c1117; }

/* back issues */
.fped-issues { max-width: 720px; margin: 2.8rem auto 0; padding: 0 2rem 1rem; }
.fped-issues-h { font-family: var(--sans); font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  color: var(--muted); border-bottom: 2px solid var(--text); padding-bottom: .5rem; margin: 0 0 .3rem; }
.fped-row { display: grid; grid-template-columns: auto 1fr auto; gap: 1.2rem; align-items: baseline;
  text-decoration: none; color: var(--text); padding: .95rem 1.6rem .95rem 0; border-bottom: 1px solid var(--border);
  position: relative; }
.fped-row::after { content: "→"; position: absolute; right: 0; top: 50%;
  margin-top: -.6em; font-family: var(--sans); font-size: .9rem; line-height: 1;
  color: #0d5b68;
  opacity: 0; transform: translateX(-4px);
  transition: opacity .16s var(--ease), transform .16s var(--ease); }
[data-theme="dark"] .fped-row::after { color: #62aab8; }
.fped-row:hover::after, .fped-row:focus-visible::after { opacity: 1; transform: translateX(0); }
.fped-row-no { font-family: var(--mono); font-size: .9rem; font-variant-numeric: tabular-nums;
  color: #0d5b68; white-space: nowrap; }
[data-theme="dark"] .fped-row-no { color: #62aab8; }
.fped-row-body { min-width: 0; }
.fped-row-head { font-family: var(--display); font-size: 1.18rem; line-height: 1.18; display: block; transition: color .15s var(--ease); }
.fped-row:hover .fped-row-head { color: #0d5b68; }
[data-theme="dark"] .fped-row:hover .fped-row-head { color: #62aab8; }
.fped-row-meta { font-family: var(--mono); font-size: .66rem; letter-spacing: .02em;
  text-transform: uppercase; color: var(--muted); margin-top: .25rem;
  display: flex; align-items: baseline; gap: .55rem; }
.fped-row-meta::after { content: ""; flex: 1; min-width: 1.5rem; height: .7em;
  background-image: radial-gradient(circle, var(--border) 1px, transparent 1.2px);
  background-size: 6px 2px; background-repeat: repeat-x; background-position: 0 60%; }
.fped-row:hover .fped-row-meta::after, .fped-row:focus-visible .fped-row-meta::after {
  background-image: radial-gradient(circle, #0d5b68 1px, transparent 1.2px); }
[data-theme="dark"] .fped-row:hover .fped-row-meta::after,
[data-theme="dark"] .fped-row:focus-visible .fped-row-meta::after {
  background-image: radial-gradient(circle, #62aab8 1px, transparent 1.2px); }
.fped-row-date { font-family: var(--mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums;
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
  .fped-row { grid-template-columns: auto 1fr; gap: .8rem; padding-right: 0; }
  .fped-row::after { display: none; }
  .fped-row-date { display: none; }
  .fped-row-meta::after { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .fpedf-cta,
  .fped-feature a:hover .fpedf-cta, .fped-feature a:focus-visible .fpedf-cta,
  .fped-row::after,
  .fped-row:hover::after, .fped-row:focus-visible::after {
    transform: none !important;
    transition-duration: .01ms !important;
  }
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
            f'{scene_plate("fingerprint", extra_class="feature-scene", seed=_fp_ed_href(ed))}'
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
        css=LIBRARY_CSS + SCENE_PLATE_CSS + FINGERPRINT_BAND_CSS + FINGERPRINT_PAGE_CSS,
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(active="adtech.html"),
        kicker=html.escape(cfg.get("kicker", "The global programmatic & Advanced TV market paper")),
        ridges=FP_RIDGES,
        motto=html.escape(cfg.get("motto", "All the news that's fit to Fingerprint")),
        scene=scene_plate("fingerprint", extra_class="section-scene", seed="fingerprint-front"),
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
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div id="fp-progress" aria-hidden="true"></div>
<div class="masthead">
  <a class="mh-brand" href="../index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
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
  {scene}
  <nav class="fp-jump" id="fp-jump" aria-label="Sections"></nav>
  <section id="fp-frontpage" class="fp-frontpage" aria-label="In this edition"></section>
  <section id="fp-lead" class="fp-lead-wrap"></section>
  <div id="fp-body"></div>
  <footer class="fp-foot">
    {watch}
    <p class="fp-colophon"><a href="../fingerprint.html">← All editions</a> &nbsp;·&nbsp; <a href="../research.html">The Research Library</a></p>
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
  background: #0d5b68; z-index: 50; transition: width .1s linear; }
[data-theme="dark"] #fp-progress { background: #62aab8; }
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#theme-btn:hover, #theme-btn:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }

/* nameplate */
.fp-nameplate { display: block; text-align: center; margin: .6rem 0 1.4rem; }
.fp-np-kicker { font-family: var(--sans); font-size: .66rem; text-transform: uppercase;
  letter-spacing: .2em; color: #0d5b68; margin: 0 0 .4rem; }
[data-theme="dark"] .fp-np-kicker { color: #62aab8; }
.fp-np-name { display: inline-block; font-family: var(--display); font-weight: 600;
  font-size: clamp(2.1rem, 5.4vw, 3.2rem); line-height: .98; letter-spacing: -.02em;
  color: var(--text); text-decoration: none; margin: 0 0 .9rem; }
.fp-np-name:hover { color: #0d5b68; }
[data-theme="dark"] .fp-np-name:hover { color: #62aab8; }
/* Special-edition stamp — only shown when an edition carries a non-default subtitle. */
.fp-np-special { display: inline-block; font-family: var(--sans); font-size: .66rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .18em; color: #fff; background: #1c5fa8;
  padding: .28rem .72rem; border-radius: 2px; margin: 0 0 1.1rem; }
[data-theme="dark"] .fp-np-special { background: #2f6aae; color: #f3ede0; }
.fp-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 1px solid var(--text); border-bottom: 1px solid var(--text); padding: .5rem 0;
  font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .64rem; text-transform: uppercase; letter-spacing: .1em;
  font-variant-numeric: tabular-nums; }
.fp-folio .fp-folio-c { color: #0d5b68; font-weight: 700; }
[data-theme="dark"] .fp-folio .fp-folio-c { color: #62aab8; }

/* sticky beat-navigator — internet-native wayfinding */
.fp-jump { position: sticky; top: 0; z-index: 15; display: flex; gap: .3rem; overflow-x: auto;
  margin: 0 -2rem 2rem; padding: .55rem 2rem; background: color-mix(in srgb, var(--bg) 86%, transparent);
  backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); scrollbar-width: none; }
.fp-jump::-webkit-scrollbar { display: none; }
.fp-jump a { display: inline-flex; align-items: center; gap: .42rem; white-space: nowrap; text-decoration: none;
  font-family: var(--sans); font-size: .7rem; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); padding: .32rem .6rem; border-radius: 0; transition: color .15s, background .15s; }
.fp-jump a::before { content: ""; width: 8px; height: 8px; border-radius: 0; background: var(--sec, var(--muted)); flex: none; }
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
.fp-lead-head { font-family: var(--display); font-weight: 600; letter-spacing: -.015em; line-height: 1.05;
  font-size: clamp(2rem, 5vw, 3rem); margin: 0 0 .6rem; }
.fp-lead-head::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(#0d5b68, #0d5b68) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
[data-theme="dark"] .fp-lead-head::after {
  background:
    linear-gradient(#62aab8, #62aab8) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.fp-lead-dek { font-family: var(--serif); font-style: italic; font-size: 1.3rem; line-height: 1.4;
  color: var(--muted); margin: 0 0 1rem; }
.fp-lead-body { font-size: 1.08rem; line-height: 1.75; }
.fp-lead-body > p:first-of-type::first-letter { font-family: var(--display); font-weight: 600;
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
.fp-story-head { font-family: var(--display); font-weight: 600; font-size: 1.3rem; line-height: 1.22;
  letter-spacing: -.01em; margin: 0 0 .35rem; }
.fp-story-dek { font-family: var(--serif); font-style: italic; font-size: 1.02rem; line-height: 1.42;
  color: var(--muted); margin: 0 0 .55rem; }
.fp-story-body { font-size: 1.0rem; line-height: 1.68; margin: 0 0 .5rem; }
.fp-story-body p { margin: 0 0 .7rem; }
.fp-src { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .68rem; letter-spacing: .02em;
  color: var(--muted); margin: .2rem 0 0; font-variant-numeric: tabular-nums; }
.fp-src a { color: var(--sec); text-decoration: none; border-bottom: 1px solid transparent; }
.fp-src a:hover { border-bottom-color: var(--sec); }
.fp-src .fp-filed { text-transform: uppercase; }

/* inline newspaper-style chart */
.fp-chart { margin: .8rem 0 .9rem; padding: .9rem 1rem; background: var(--panel); border: 1px solid var(--border); border-radius: 2px; }
.fp-chart-title { font-family: var(--sans); font-size: .76rem; font-weight: 700; color: var(--text); margin: 0 0 .5rem; }
.fp-chart svg { width: 100%; height: auto; display: block; }
.fp-chart-src { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: .62rem; color: var(--muted); margin: .4rem 0 0; }
.fp-chart text { font-family: var(--sans); fill: var(--muted); }
.fp-chart .fp-stat-num { font-family: var(--display); font-weight: 600; fill: var(--sec); }

/* op-ed panel */
.fp-oped { display: grid; grid-template-columns: 132px 1fr; gap: 1.4rem; align-items: start;
  background: transparent; border: 1px solid var(--border); border-left: 4px solid var(--sec);
  border-radius: 0; padding: 1.5rem 1.6rem; margin: 2.4rem 0 0; scroll-margin-top: 4rem; }
.fp-oped-portrait { text-align: center; }
.fp-oped-portrait img { width: 112px; height: 112px; object-fit: cover; border-radius: 0;
  border: 2px solid var(--sec); display: block; margin: 0 auto .5rem; filter: grayscale(.15); }
.fp-oped-cap { font-family: var(--sans); font-size: .68rem; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.fp-oped-kicker { font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .14em; color: var(--sec); margin: 0 0 .4rem; }
.fp-oped-head { font-family: var(--display); font-weight: 600; font-size: 1.5rem; line-height: 1.15; margin: 0 0 .45rem; }
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
.fp-colophon { font-family: var(--mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums;
  color: var(--muted); margin: 0; }
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
@media (prefers-reduced-motion: reduce) {
  #theme-btn,
  #theme-btn:hover, #theme-btn:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
__FP_BEAT_CSS__
"""

FINGERPRINT_EDITION_JS = r"""
// theme toggle — the theme itself is applied pre-paint by the <head> boot script
document.getElementById('theme-btn').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('corpus-theme', next);
};

const ed = JSON.parse(document.getElementById('fp-edition-data').textContent);
var noMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const MARKS = JSON.parse(document.getElementById('fp-marks').textContent);

// Special-edition stamp: surface a non-default subtitle in the nameplate (regular
// editions carry the default motto and are untouched).
(function(){
  const sub = (ed.subtitle || '').trim();
  if (sub && sub !== "All the news that's fit to Fingerprint") {
    const folio = document.querySelector('.fp-nameplate .fp-folio');
    if (folio) {
      const badge = document.createElement('span');
      badge.className = 'fp-np-special';
      badge.textContent = sub;
      folio.parentNode.insertBefore(badge, folio);
    }
  }
})();
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls;
  if (html != null) n.innerHTML = html; return n; };
const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
const openExternal = (root) => root.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });

// ---- source / dateline line (multi-source) ----
// Canonical field is `sources` (a list of {publication, url}); the legacy single
// source_publication/source_url pair is honoured as a fallback. Mirrors the
// renderer's _build_byline_html so the web edition cites every outlet too.
function srcLine(s, beat) {
  // esc() (textContent->innerHTML) does NOT escape double-quotes, so a URL must
  // be quote-escaped before it goes into an href="" attribute (mirrors the
  // Python renderer's html.escape(url, quote=True)).
  const escAttr = (v) => esc(v).replace(/"/g, '&quot;');
  let srcs = [];
  // Only take the canonical branch when it holds real {publication,url} objects;
  // otherwise fall back to the legacy pair (mirrors Python _normalize_sources).
  if (Array.isArray(s.sources) && s.sources.some(x => x && typeof x === 'object')) {
    srcs = s.sources;
  } else if (s.source_publication || s.source_url) {
    srcs = [{publication: s.source_publication, url: s.source_url}];
  }
  const seen = new Set(), links = [];
  for (const x of srcs) {
    const url = ((x && (x.url || x.source_url)) || '').trim();
    const pub = ((x && (x.publication || x.source_publication)) || '').trim();
    if (!url && !pub) continue;
    const key = (url || pub).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const label = esc(pub || url);
    links.push(url ? `<a href="${escAttr(url)}">${label}</a>` : label);
  }
  if (!links.length) return '';
  const verb = links.length > 1 ? 'On reporting in ' : '';
  const filed = s.filed_date ? ` · <span class="fp-filed">Filed ${esc(s.filed_date)}</span>` : '';
  return `<p class="fp-src">${verb}${links.join(' · ')}${filed}</p>`;
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
    txt.insertAdjacentHTML('beforeend', srcLine(s, beat));  // trend sources, as the PDF shows
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
        const t = document.querySelector(it.href); if (t) t.scrollIntoView(noMotion ? { block: 'start' } : { behavior: 'smooth', block: 'start' }); };
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
    a.onclick = (e) => { e.preventDefault(); document.getElementById(it.id).scrollIntoView(noMotion ? { block: 'start' } : { behavior: 'smooth', block: 'start' }); };
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
    css = (LIBRARY_CSS + SCENE_PLATE_CSS + FINGERPRINT_EDITION_CSS).replace("__FP_BEAT_CSS__", _fp_beat_css())
    page = FINGERPRINT_EDITION_TEMPLATE.format(
        title=html.escape(f"{lead_head} — The Fingerprint"),
        description=html.escape(data.get("lead", {}).get("dek", "") or "The Fingerprint"),
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(prefix="../", active="adtech.html"),
        css=css,
        folio_no=html.escape(folio_no),
        folio_date=html.escape(folio_date),
        folio_meta=html.escape(folio_meta),
        scene=scene_plate("fingerprint", extra_class="edition-scene", root="../", seed=f"fingerprint-edition:{date}"),
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
.cx-head .kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .5rem; }
.cx-head h1 { font-family: var(--display); font-weight: 600; font-size: clamp(2rem, 5vw, 3rem); line-height: 1.05; margin: 0; }
.cx-head p { font-family: var(--sans); color: var(--muted); font-size: .9rem; line-height: 1.5; margin: .55rem auto 0; max-width: 38rem; }
.cx-legend { display: flex; flex-wrap: wrap; gap: .5rem 1.1rem; justify-content: center; margin: 1.1rem 0 1rem;
  font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
.cx-leg { display: inline-flex; align-items: center; gap: .4rem; }
.cx-leg i { width: 10px; height: 10px; border-radius: 0; display: inline-block; }
.cx-stage { position: relative; background: var(--bg); border: 1px solid var(--border); border-radius: 0; overflow: hidden; }
#cx-svg { width: 100%; height: auto; display: block; }
.cx-edge { stroke: var(--border); stroke-width: 1.2; opacity: .55; transition: opacity .15s ease, stroke .15s ease; }
.cx-edge.lit { stroke: var(--accent); opacity: .95; stroke-width: 1.9; }
#cx-svg.dimmed .cx-edge:not(.lit) { opacity: .1; }
.cx-node { cursor: pointer; }
.cx-node circle { stroke: var(--bg); stroke-width: 2.5; transition: opacity .15s ease; }
.cx-node:focus { outline: none; }
.cx-node:focus circle, .cx-node.hot circle { stroke: var(--accent); stroke-width: 3; }
.cx-label { font-family: var(--sans); font-size: 10.5px; fill: var(--muted); pointer-events: none; transition: opacity .15s ease, fill .15s ease;
  paint-order: stroke; stroke: var(--bg); stroke-width: 3.4px; stroke-linejoin: round; }
.cx-node.hot .cx-label, .cx-node.adj .cx-label { fill: var(--text); }
/* lifted/lit labels carry above their neighbours so an active thread reads cleanly */
.cx-node.hot .cx-label, .cx-node.adj .cx-label { font-weight: 600; }
#cx-svg.dimmed .cx-node:not(.hot):not(.adj) { opacity: .26; }
#cx-svg.dimmed .cx-node:not(.hot):not(.adj) .cx-label { opacity: 0; }
#cx-info { position: absolute; left: 1rem; bottom: 1rem; max-width: min(360px, 72%); background: var(--bg);
  border: 1px solid var(--border); border-radius: 0; padding: .8rem 1rem; box-shadow: var(--shadow-2);
  opacity: 1; transition: opacity .15s ease; pointer-events: none; }
#cx-info.show { opacity: 1; }
#cx-info .cx-i-t { display: block; font-family: var(--display); font-size: 1.12rem; line-height: 1.2; }
#cx-info .cx-i-n { display: block; font-family: var(--sans); font-size: .76rem; color: var(--muted); margin: .35rem 0 .55rem; line-height: 1.45; }
#cx-info .cx-i-list { margin: 0; padding: 0; list-style: none; display: grid; gap: .45rem; }
#cx-info .cx-i-item { display: grid; gap: .1rem; padding-top: .45rem; border-top: 1px solid var(--border); }
#cx-info .cx-i-item:first-child { padding-top: 0; border-top: 0; }
#cx-info .cx-i-link { font-family: var(--sans); font-size: .74rem; font-weight: 700; color: var(--text); }
#cx-info .cx-i-why { font-family: var(--sans); font-size: .72rem; color: var(--muted); line-height: 1.4; }
#cx-info .cx-i-cta { display: block; margin-top: .65rem; font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); }
#cx-info .cx-i-help { display: block; font-family: var(--sans); font-size: .72rem; color: var(--muted); line-height: 1.45; }
.cx-hint { text-align: center; font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); margin: .9rem 0 0; }
.cx-foot { max-width: 1120px; margin: 2rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.cx-foot .colophon { font-family: var(--mono); font-size: .68rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 0; }
.cx-foot a { color: var(--accent); text-decoration: none; }
@media (max-width: 600px) { .cx-label { font-size: 9px; } #cx-info { left: .6rem; bottom: .6rem; } }
"""

CONNECTIONS_JS = r"""
(function () {
  var svg = document.getElementById('cx-svg'); if (!svg) return;
  var base = window.SHELL_BASE || '';
  var dEl = document.getElementById('cx-data');
  var data = (dEl && JSON.parse(dEl.textContent || '{}')) || {};
  var titles = data.titles || {};
  var reasons = data.reasons || {};
  var info = document.getElementById('cx-info');
  var nodes = [].slice.call(svg.querySelectorAll('.cx-node'));
  var edges = [].slice.call(svg.querySelectorAll('.cx-edge'));
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function renderEmpty() {
    if (!info) return;
    info.innerHTML = '<span class="cx-i-t">Why this connection exists</span>'
      + '<span class="cx-i-n">Pick a work on the map.</span>'
      + '<span class="cx-i-help">This panel will explain each linked work in simple terms.</span>';
    info.classList.add('show');
  }
  function clear() { svg.classList.remove('dimmed'); nodes.forEach(function (n) { n.classList.remove('hot', 'adj'); });
    edges.forEach(function (e) { e.classList.remove('lit'); }); renderEmpty(); }
  function focusNode(n) {
    var slug = n.getAttribute('data-slug'), nbr = (n.getAttribute('data-nbr') || '').split(' ').filter(Boolean);
    svg.classList.add('dimmed');
    nodes.forEach(function (m) { m.classList.remove('hot', 'adj'); });
    n.classList.add('hot');
    nodes.forEach(function (m) { if (nbr.indexOf(m.getAttribute('data-slug')) >= 0) m.classList.add('adj'); });
    edges.forEach(function (e) { var a = e.getAttribute('data-a'), b = e.getAttribute('data-b'); e.classList.toggle('lit', a === slug || b === slug); });
    if (info) {
      var links = nbr.map(function (s) {
        return '<li class="cx-i-item"><span class="cx-i-link">' + esc(titles[s] || s) + '</span>'
          + '<span class="cx-i-why">' + esc((reasons[slug] && reasons[slug][s]) || 'They share overlapping themes.') + '</span></li>';
      }).join('');
      info.innerHTML = '<span class="cx-i-t">' + esc(titles[slug] || slug) + '</span>'
        + '<span class="cx-i-n">' + (nbr.length ? 'Why these connections show up:' : 'No strong links yet.') + '</span>'
        + (nbr.length ? '<ul class="cx-i-list">' + links + '</ul>' : '')
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
  renderEmpty();
})();
"""

CONNECTIONS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>Connections — calvincollins · xyz</title>
<meta name="description" content="A map of ideas: how the research corpora relate by theme.">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<main class="cx-wrap">
  <header class="cx-head">
    <p class="kicker">A map of ideas</p>
    <h1>Connections</h1>
    <p>How the corpora speak to one another — clustered by subject, linked where they share the most ground. Hover a work to light its threads; open it with a click.</p>
    {scene}
  </header>
  <div class="cx-legend">{legend}</div>
  <div class="cx-stage">{svg}<div id="cx-info"></div></div>
  <p class="cx-hint">Lines connect works that share the most thematic vocabulary. Tab through the map by keyboard, or open any work to read it.</p>
</main>
<footer class="cx-foot">
  <p class="colophon"><a href="research.html">← Back to the Research Library</a></p>
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
    by_slug = {n["slug"]: n for n in nodes}
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
                mx, my = ax + spread * math.cos(a2) * 0.66, ay + spread * math.sin(a2) * 0.66
            # label side: nodes above their cluster centre caption upward, those
            # below caption downward — so neighbouring labels fan apart, not stack.
            side = -1 if (k > 1 and my < ay - 2) else 1
            pos[m["slug"]] = (mx, my, side)

    edges = set()
    for n in nodes:
        for r in n.get("related", []):
            if r.get("slug") in by_slug:
                edges.add(tuple(sorted((n["slug"], r["slug"]))))
    nbr = {n["slug"]: set() for n in nodes}
    for a, b in edges:
        nbr[a].add(b); nbr[b].add(a)

    parts = [f'<svg id="cx-svg" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="A graph of how the research corpora relate by theme"><g id="cx-edges">']
    for a, b in sorted(edges):
        ax, ay, _ = pos[a]; bx, by, _ = pos[b]
        parts.append(f'<line class="cx-edge" data-a="{html.escape(a, quote=True)}" data-b="{html.escape(b, quote=True)}" '
                     f'x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/>')
    parts.append('</g><g id="cx-nodes">')
    for n in nodes:
        x, y, side = pos[n["slug"]]
        rad = 9 + min(15, len(n.get("chapters", [])) * 0.7)
        ly = (y - rad - 8) if side < 0 else (y + rad + 13)
        parts.append(
            f'<g class="cx-node" data-slug="{html.escape(n["slug"], quote=True)}" '
            f'data-nbr="{html.escape(" ".join(sorted(nbr[n["slug"]])), quote=True)}" '
            f'tabindex="0" role="link" aria-label="{html.escape(n["title"])}">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{cat_color[n["category"]]}"/>'
            f'<text class="cx-label" x="{x:.1f}" y="{ly:.1f}" text-anchor="middle">{html.escape(n["title"])}</text>'
            f'</g>'
        )
    parts.append('</g></svg>')
    legend = "".join(f'<span class="cx-leg"><i style="background:{cat_color[c]}"></i>{html.escape(c)}</span>' for c in seen)
    titles = {n["slug"]: n["title"] for n in nodes}
    reasons = {
        n["slug"]: {r["slug"]: r.get("reason", "") for r in n.get("related", []) if r.get("slug") in by_slug}
        for n in nodes
    }
    page = CONNECTIONS_TEMPLATE.format(
        css=LIBRARY_CSS + SCENE_PLATE_CSS + CONNECTIONS_CSS, favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(active="connections.html"),
        svg="".join(parts), legend=legend,
        scene=scene_plate("map", extra_class="page-scene", seed="connections-map"),
        data_json=json_for_html({"titles": titles, "reasons": reasons}),
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


def _connection_reason(a, b, limit=3):
    """Short plain-language explanation for why two corpora connect."""
    shared = sorted(a["keywords"] & b["keywords"], key=lambda w: (-len(w), w))
    picks = []
    for word in shared:
        if word in picks:
            continue
        picks.append(word.replace("-", " "))
        if len(picks) == limit:
            break
    if picks:
        if len(picks) == 1:
            return f"Both spend time on {picks[0]}."
        if len(picks) == 2:
            return f"Both spend time on {picks[0]} and {picks[1]}."
        return f"Both spend time on {', '.join(picks[:-1])}, and {picks[-1]}."
    if a["category"] == b["category"]:
        return f"Both sit in {a['category']}."
    return "They share overlapping themes."


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
            # apparatus blockquotes are not passages: anything opening with a
            # "Label:" prefix (Sourcing honesty:, Stakes:, Method note:, Key
            # claim 4.1 —, Tier key: …) or dense with = signs is machinery.
            if (re.match(r"^[\w'’\"() .&/-]{2,28}:\s", raw)
                    or re.match(r"^[A-Za-z ]{3,20}\d+(\.\d+)?\s*[—–-]", raw)
                    or re.search(r"\btier\s+\d\s*=", raw, re.I)
                    or raw.count("=") >= 3):
                continue
            q = _clean_passage(raw, 260)
            if len(q) >= 45:
                quotes.append({"chapter": di, "chapterTitle": d["title"], "text": q, "kind": "epigraph"})
        prose = strip_md(re.sub(r"(?m)^#.*$", "", body), cap=600)
        ms = re.search(r"(?:^|(?<=[\s\"“‘(]))([A-Z][^.!?]{45,180}[.!?])", prose)
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


_MONTHS = ("January February March April May June July August September October "
           "November December").split()


def _pretty_date(s):
    """Manifest dates arrive in mixed shapes; ISO ones read badly in a quiz
    prompt ('What happened in 2015-07-13?'). Render ISO as prose, pass the
    rest ('c. 1175', '1919–1926') through untouched."""
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", s)
    if not m:
        return s
    y, mo, day = m.group(1), int(m.group(2)), m.group(3)
    if not 1 <= mo <= 12:
        return s
    return (f"{int(day)} " if day else "") + f"{_MONTHS[mo - 1]} {y}"


def quiz_facts_for(corpus, folder):
    """Content facts for the index's "Test Yourself" quiz — glossary terms, key
    dates, and key people/organisations from the research manifest — so the quiz
    can test what a corpus says, not just what its chapters are called.

    Each fact is located to the first chapter that mentions it (`c` = chapter
    index, `a` = anchor text) so a missed answer can deep-link into the prose via
    the reader's ?q= scroll-and-flash. Facts whose wording gives the answer away
    (an event that states its own year, a description that names its subject) are
    dropped. Lists are capped: this payload is inlined on the index page.
    """
    texts = [strip_md(d["body"], cap=120000).lower() for d in corpus["documents"]]

    def locate(needle):
        n = (needle or "").strip().lower()
        if len(n) < 4:
            return None
        for i, t in enumerate(texts):
            if n in t:
                return i
        return None

    facts = {"terms": [], "dates": [], "entities": []}
    for g in (corpus.get("glossary") or [])[:24]:
        facts["terms"].append({"t": g["term"], "d": g["def"], "c": locate(g["term"]) or 0})
    try:
        m = json.loads((Path(folder) / "manifest.json").read_text())
    except Exception:
        m = {}
    for kd in m.get("key_dates") or []:
        if not isinstance(kd, dict):  # some corpora carry these as plain strings
            continue
        if len(facts["dates"]) >= 18:
            break
        date = str(kd.get("date") or "").strip()
        event = _clean_passage(str(kd.get("event") or "").strip(), 170)
        if not date or not event:
            continue
        if any(y in event for y in re.findall(r"\d{3,4}", date)):
            continue  # the event wording contains its own date
        entry = {"d": _pretty_date(date), "e": event, "c": 0}
        # Anchor on the event's most distinctive located word (fewest chapters
        # mention it), so "Read in context" flashes near the fact rather than
        # the first occurrence of some common word like "Christianity".
        best = None
        for w in set(re.findall(r"[A-Za-z][A-Za-z'\-]{5,}", event)):
            hits = [i for i, t in enumerate(texts) if w.lower() in t]
            if hits and (best is None or (len(hits), -len(w)) < (len(best[1]), -len(best[0]))):
                best = (w, hits)
        if best:
            entry["c"], entry["a"] = best[1][0], best[0]
        facts["dates"].append(entry)
    for ke in m.get("key_entities") or []:
        if not isinstance(ke, dict):  # some corpora carry these as plain strings
            continue
        if len(facts["entities"]) >= 18:
            break
        name = str(ke.get("name") or "").strip()
        role = _clean_passage(str(ke.get("role") or "").split(";")[0].strip(), 170)
        if not name or not role:
            continue
        if any(w.lower() in role.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", name)):
            continue  # the description names its subject
        entry = {"n": name, "r": role, "c": 0}
        for cand in (name, name.split()[-1]):
            ch = locate(cand)
            if ch is not None:
                entry["c"], entry["a"] = ch, cand
                break
        facts["entities"].append(entry)
    return facts


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
            {"slug": b["slug"], "title": b["title"], "category": b["category"], "reason": _connection_reason(a, b)}
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
            "palette": col.get("palette"), "slugs": list(dict.fromkeys(used))}
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


# ---- detached domain fronts: a config domain with a "page" key becomes its
# own top-level section of the site (e.g. Ad Tech — docs/adtech.html). Its
# category shelves move off the home page onto the section front, and any bands
# named in page.include (currently just "fingerprint") move with them, keeping
# the trade desk separate from the liberal-arts library. ----

DOMAIN_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{title} — calvincollins · xyz</title>
<meta name="description" content="{subtitle}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}{accent_css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="dk-plate">
  <div class="dk-hero">
    <div class="dk-copy">
      <p class="dk-kicker">{kicker}</p>
      <h1 class="dk-name">{title}</h1>
      <p class="dk-motto">{subtitle}</p>
      <div class="dk-folio">
        <span>{folio_left}</span>
        <span class="dk-folio-c">{stats}</span>
        <span>{folio_right}</span>
      </div>
    </div>
    <div class="dk-agent">{hero}</div>
    <div class="dk-scene">{scene}</div>
  </div>
</header>
{bands}
{tools}
<h2 class="section-title" id="library">The Research</h2>
<main class="library">
{cards}
</main>
{quiz}
{bottom_bands}
<footer>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <p class="epigraph">{epigraph}</p>
  <p class="colophon"><a href="research.html">← Back to the Research Library</a></p>
</footer>
<script>{theme_js}</script>
{shell}
</body>
</html>
"""

DOMAIN_PAGE_CSS = """
/* A detached domain front (the Ad Tech desk) — a working-desk nameplate signed
   in the Fingerprint's petrol teal, over the shared library card grid. */
/* 1120px to match every other section — the plate used to be 1080 and left a
   20px stagger against the bands below it. */
.dk-plate { display: block; max-width: 1120px; margin: 1.9rem auto 0; padding: 0 2rem; }
/* Nameplate and numbers first, decoration second — the fold should tell you
   what the desk holds, not just that it has a mascot. */
.dk-hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(160px, 220px);
  align-items: center; gap: 2.4rem; }
.dk-copy { text-align: center; min-width: 0; }
.dk-agent { display: flex; justify-content: center; }
.dk-agent .agent-chip { align-items: center; }
.dk-agent .agent-portrait { width: min(100%, 200px); }
/* Sits in the copy column so it lines up under the nameplate rather than
   centring itself across the mascot too. */
.dk-scene { grid-column: 1; margin: 1.5rem 0 0; min-width: 0; }
.dk-scene .scene-plate { aspect-ratio: 21 / 9; max-width: none; margin: 0; }
.dk-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .5rem; }
.dk-kicker::before { content: ""; display: inline-block; width: 8px; height: 8px; background: var(--accent); margin-right: .5rem; }
.dk-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.6rem, 6.5vw, 4.4rem);
  line-height: .98; letter-spacing: -.01em; margin: 0 0 .55rem; }
.dk-name::after { content: ""; display: block; width: 148px; height: 8px; margin: .65rem auto 0; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.dk-motto { font-family: var(--serif); font-style: italic; font-size: 1.08rem; line-height: 1.5;
  color: var(--muted); max-width: 640px; margin: 0 auto 1.4rem; }
.dk-folio { display: flex; justify-content: space-between; align-items: center; gap: 1rem;
  border-top: 2px solid var(--text); border-bottom: 1px solid var(--text); padding: .55rem 0;
  font-family: var(--mono); font-size: .72rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--text); }
.dk-folio .dk-folio-c { color: var(--accent); font-weight: 600; }
footer .colophon a { color: var(--accent); text-decoration: none; }
footer .colophon a:hover { text-decoration: underline; }
@media (max-width: 560px) {
  .dk-hero { grid-template-columns: 1fr; }
  .dk-agent { order: -1; }
  .dk-folio { font-size: .58rem; letter-spacing: .04em; }
  .dk-tools { padding: 0 1.2rem; }
  .dk-tool { padding: 1.05rem 1.1rem; }
}
/* the desk's own apparatus — its forecaster + dictionary, as teal-ruled tool plates */
.dk-tools { max-width: 1080px; margin: 1.4rem auto 0; padding: 0 2rem; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.1rem; }
.dk-tool { display: flex; flex-direction: column; text-decoration: none; color: var(--text);
  background: var(--bg); border: 1px solid var(--border); border-left: 3px solid var(--accent);
  border-radius: 0; padding: 1.2rem 1.4rem; position: relative;
  transition: transform .16s var(--ease), box-shadow .16s var(--ease),
              border-color .16s var(--ease), outline-color .16s var(--ease); }
.dk-tool:hover, .dk-tool:focus-visible { border-color: var(--text); border-left-color: var(--accent);
  transform: translateY(-2px); box-shadow: var(--shadow-2); z-index: 1; }
.dk-tool-k { font-family: var(--sans); font-size: .64rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .14em; color: var(--accent); margin: 0 0 .35rem; }
.dk-tool-t { font-family: var(--display); font-weight: 600; font-size: 1.35rem; line-height: 1.15; margin: 0 0 .3rem; }
.dk-tool-m { font-family: var(--sans); font-size: .78rem; color: var(--muted); margin: 0 0 .75rem; line-height: 1.45; }
.dk-tool-cta { display: inline-block; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; white-space: nowrap;
  color: var(--accent); background: transparent;
  border: 1px solid currentColor; border-radius: 0; padding: .45rem .85rem;
  margin-top: auto; align-self: flex-start;
  transition: background-color .16s var(--ease), color .16s var(--ease),
              border-color .16s var(--ease), transform .16s var(--ease); }
.dk-tool:hover .dk-tool-cta, .dk-tool:focus-visible .dk-tool-cta {
  background: var(--accent); border-color: var(--accent); color: #fcfbf7; transform: translateX(3px); }
[data-theme="dark"] .dk-tool:hover .dk-tool-cta, [data-theme="dark"] .dk-tool:focus-visible .dk-tool-cta {
  color: #0c1117; }

/* the desk's essay rack band (Briefings) — the pamphlet band in desk teal */
.pamphlet-band.brf { --accent: #0d5b68; }
[data-theme="dark"] .pamphlet-band.brf { --accent: #62aab8; }
@media (prefers-reduced-motion: reduce) {
  .dk-tool, .dk-tool:hover, .dk-tool:focus-visible,
  .dk-tool-cta, .dk-tool:hover .dk-tool-cta, .dk-tool:focus-visible .dk-tool-cta {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""


def _desk_tools_html(tools):
    """The desk's apparatus row (its own forecaster / dictionary), rendered as
    tool cards between the bands and the card grid. Empty list → empty string."""
    if not tools:
        return ""
    cards = "".join(
        f'<a class="dk-tool" href="{html.escape(t["href"], quote=True)}">'
        f'<p class="dk-tool-k">{html.escape(t["kicker"])}</p>'
        f'<p class="dk-tool-t">{html.escape(t["title"])}</p>'
        f'<p class="dk-tool-m">{html.escape(t["meta"])}</p>'
        f'<span class="dk-tool-cta">{html.escape(t["cta"])}</span></a>'
        for t in tools
    )
    return f'<div class="dk-tools">{cards}</div>'


def domain_band_html(page_cfg, dom, n_corpora, fp_editions=None):
    """Home-page band pointing at a detached domain front. Wears the market-wire
    .fp-band dress so the Ad Tech desk keeps the Fingerprint's teal signature;
    when the desk took the Fingerprint with it, the band tickers the latest wire."""
    title = page_cfg.get("title") or dom.get("title", "The Desk")
    slug = page_cfg.get("slug", "adtech")
    words = title.split()
    flag_title = ("<br>".join(html.escape(w) for w in words[:2])
                  if len(words) > 1 else html.escape(title))
    flag = (f'<div class="fp-band-flag">{flag_title}'
            f'<small>{html.escape(page_cfg.get("kicker", "a separate desk"))}</small></div>')
    if fp_editions:
        latest = fp_editions[0]
        no = latest.get("edition_number")
        wire = "The Fingerprint" + (f" Nº {no:02d}" if isinstance(no, int) else "")
        kicker = f"{title} · {n_corpora} research corpora + the daily wire"
        lead = html.escape(latest.get("lead_headline") or "The desk is open")
        beats = latest.get("beats") or []
        ticker = html.escape(f"On the wire · {wire}")
        if beats:
            ticker += " — " + " · ".join(html.escape(b) for b in beats[:4])
    else:
        kicker = f"{title} · {n_corpora} research corpora"
        lead = html.escape(page_cfg.get("subtitle") or dom.get("title", ""))
        ticker = html.escape(page_cfg.get("kicker", ""))
    mid = (f'<div class="fp-band-mid"><p class="fp-band-kicker">{html.escape(kicker)}</p>'
           f'<p class="fp-band-lead">{lead}</p>'
           f'<p class="fp-band-ticker">{ticker}</p></div>')
    scene = scene_plate("briefing", extra_class="band-scene", seed=f"domain-band:{slug}:{lead}")
    return (f'<div class="fp-band"><a href="{slug}.html">{flag}{scene}{mid}'
            f'<span class="fp-band-cta">Enter the desk →</span></a></div>')


HOME_MIRROR_CSS = """
/* The home ticker — the Forecast Desk's own ticker tape, borrowed onto the
   front page. Needs the board's dark-terminal custom properties, which
   .fd-tape itself only inherits from .fd-board on the Forecast page, so this
   slim wrapper carries the same values. */
.home-ticker { --fdline: #2c303a; --fdmut: #9aa1af; --fdup: #22c55e; --fdtext: #edeff4;
  --fdmono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  display: flex; align-items: center; gap: 1rem; max-width: 1120px; margin: .7rem auto 0; padding: 0 2rem; }
.home-ticker-label { flex: 0 0 auto; font-family: var(--sans); font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; color: #15803d; text-decoration: none; white-space: nowrap; }
[data-theme="dark"] .home-ticker-label { color: #4ade80; }
.home-ticker-label:hover { text-decoration: underline; }
.home-ticker-board { flex: 1 1 auto; min-width: 0; }
.home-ticker .fd-tape { flex: 1 1 auto; min-width: 0; margin: 0; }
@media (max-width: 680px) { .home-ticker { flex-wrap: wrap; } }
/* .fd-tape itself is Forecast-page CSS, not otherwise loaded on the index —
   duplicated here (not shared) so the ticker works without pulling in the
   whole dark-terminal board stylesheet. */
.fd-tape { overflow: hidden; border: 1px solid var(--fdline); border-radius: 2px;
  background: #0a0f16; margin: 0; -webkit-mask-image: linear-gradient(90deg, transparent, #000 6% 94%, transparent);
  mask-image: linear-gradient(90deg, transparent, #000 6% 94%, transparent); }
.fd-tape-inner { display: inline-flex; gap: 2.2rem; padding: .55rem 0; white-space: nowrap;
  animation: fd-tape 240s linear infinite; will-change: transform; }
.fd-tape:hover .fd-tape-inner { animation-play-state: paused; }
@keyframes fd-tape { from { transform: translateX(0); } to { transform: translateX(-50%); } }
@media (prefers-reduced-motion: reduce) { .fd-tape-inner { animation: none; } }
.fd-tk { font-family: var(--fdmono); font-size: .78rem; color: var(--fdmut); font-variant-numeric: tabular-nums; }
.fd-tk b { color: var(--fdtext); font-weight: 600; }
.fd-tk .up { color: var(--fdup); font-weight: 700; }
.fd-tk .dn { color: #ef4444; font-weight: 700; }

/* The mirror spread — the front page's two co-equal panes (the Research
   Library and the detached desk, e.g. Ad Tech), starting with a mirrored
   Today's Passage and continuing as two parallel scrollable shelves. Reads
   as one deliberate choice, not a wall of nav, and the whole thing scrolls
   with the page — nothing here is independently scrollable. */
.mirror-spread { max-width: 1120px; margin: 1.6rem auto 0; padding: 0 2rem; }
.mirror-head { display: grid; grid-template-columns: 1fr 1fr; gap: 1.2rem; margin-bottom: 1.6rem; }
.mc-lib { --mc-accent: #9a2c1a; }
[data-theme="dark"] .mc-lib { --mc-accent: #d98055; }
.mc-adtech { --mc-accent: #0d5b68; }
[data-theme="dark"] .mc-adtech { --mc-accent: #62aab8; }
.dp-pane .dp-quote { border-top-color: var(--mc-accent, var(--accent)); }
.dp-pane .dp-quote:hover, .dp-pane .dp-quote:focus-visible { border-top-color: var(--mc-accent, var(--accent)); }
.dp-pane .dp-kicker, .dp-pane .dp-mark, .dp-pane .dp-cta { color: var(--mc-accent, var(--accent)); }
.dp-pane .dp-quote:hover .dp-cta, .dp-pane .dp-quote:focus-visible .dp-cta { color: var(--bg); }
/* Three shared rows — masthead, passage, shelf — so both panes stay level. */
.mirror-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2.2rem;
  grid-template-rows: auto auto 1fr; align-items: start; }
.mirror-col { display: grid; grid-row: 1 / -1; grid-template-rows: subgrid; row-gap: 0;
  border-top: 3px solid var(--mc-accent, var(--accent)); padding-top: 1rem; min-width: 0; }
.mirror-col #resume:empty { display: none; }
.mirror-col #foryou { max-width: none; margin: .3rem 0 1rem; padding: 0; }
/* Mascot beside the title rather than stacked above it: the two agents have
   different natural heights and used to shove each column's title out of line. */
.mirror-mast { display: flex; align-items: center; gap: 1rem; padding-bottom: 1.1rem; min-width: 0; }
.mirror-id { min-width: 0; }
.mirror-h { font-family: var(--display); font-weight: 600; font-size: 1.55rem; margin: 0;
  line-height: 1.12; text-wrap: balance; }
.mirror-h a { color: inherit; text-decoration: none; }
.mirror-h a:hover { color: var(--mc-accent, var(--accent)); }
.mirror-sub { font-family: var(--mono); font-size: .68rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: .06em; font-variant-numeric: tabular-nums; margin: .4rem 0 0; line-height: 1.5; }
.mirror-agent { flex: 0 0 auto; }
.mirror-agent .agent-chip.compact { gap: 0; }
/* In the spread the heading right next to the mascot already names the section,
   so the boxed caption is just noise — keep it for the desk fronts, where the
   agent stands on its own. */
.mirror-agent .agent-chip-label { display: none; }
.mirror-passage { min-width: 0; padding-bottom: 1.1rem; }
.mirror-body { min-width: 0; display: flex; flex-direction: column; }
.mirror-body .mirror-more { align-self: flex-start; margin-top: auto; }
.mirror-col .lib-toolbar { padding: 0 0 .7rem; }
.mirror-col .grid { padding: .2rem 0 1rem; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
.mirror-col .cat-heading, .mirror-col .domain-heading { padding: 0; }
.mirror-col .lib-empty { padding: 1.6rem 0; }
.mirror-more { display: inline-block; margin-top: 1rem; font-family: var(--sans); font-size: .7rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .1em; color: var(--mc-accent, var(--accent)); text-decoration: none;
  border: 1px solid currentColor; padding: .5rem 1rem;
  transition: background-color .16s var(--ease), color .16s var(--ease), transform .16s var(--ease); }
.mirror-more:hover, .mirror-more:focus-visible { background: var(--mc-accent, var(--accent)); color: var(--bg);
  transform: translateX(3px); }
@media (max-width: 900px) {
  .mirror-head, .mirror-grid { grid-template-columns: 1fr; }
}

/* Bottom scrolls — Ghost of Times / Pamphlets / Briefings as horizontal
   carousel rows at the foot of the page: light, native-reading hints rather
   than another full shelf. */
.scroll-row { max-width: 1120px; margin: 0 auto; padding: 1.7rem 2rem 0; }
.sr-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem 1.6rem;
  flex-wrap: wrap; margin-bottom: .8rem; border-bottom: 1px solid var(--border); padding-bottom: .6rem; }
.sr-h { font-family: var(--display); font-weight: 600; font-size: 1.3rem; margin: 0; }
.sr-h a { color: inherit; text-decoration: none; }
.sr-h a:hover { color: var(--sr-accent, var(--accent)); }
.sr-sub { font-family: var(--sans); font-size: .8rem; color: var(--muted); margin: 0; flex: 1 1 260px; }
.sr-more { font-family: var(--sans); font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .1em; color: var(--sr-accent, var(--accent)); text-decoration: none; white-space: nowrap; }
.sr-more:hover { text-decoration: underline; }
/* The track fades at both ends so a half-visible card reads as "more this way"
   rather than as a card someone forgot to finish. */
.sr-track-wrap { position: relative; }
.sr-track-wrap::after { content: ""; position: absolute; top: 0; right: 0; bottom: 1.3rem; width: 3.2rem;
  pointer-events: none; background: linear-gradient(90deg, transparent, var(--bg) 82%); }
.sr-track { display: flex; gap: 1rem; overflow-x: auto; padding: .2rem .1rem 1.3rem; scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch; scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  align-items: stretch; }
.sr-track::-webkit-scrollbar { height: 8px; }
.sr-track::-webkit-scrollbar-thumb { background: var(--border); border-radius: 0; }
.sr-track::-webkit-scrollbar-track { background: transparent; }
/* Cards in a row are one height and their meta lines share a baseline, however
   many lines the headline runs to. */
.sr-card { flex: 0 0 240px; scroll-snap-align: start; display: flex; flex-direction: column;
  text-decoration: none; color: var(--text);
  background: var(--bg); border: 1px solid var(--border); border-top: 3px solid var(--sr-accent, var(--accent));
  border-radius: 0; padding: 1rem 1.1rem; transition: transform .16s var(--ease), box-shadow .16s var(--ease); }
.sr-card:hover, .sr-card:focus-visible { transform: translateY(-2px); box-shadow: var(--shadow-2); }
.sr-card-kicker { font-family: var(--sans); font-size: .62rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .12em; color: var(--sr-accent, var(--accent)); margin: 0 0 .4rem; }
.sr-card-title { font-family: var(--display); font-weight: 600; font-size: 1.02rem; line-height: 1.25; margin: 0 0 .4rem;
  text-wrap: pretty; }
.sr-card-meta { font-family: var(--sans); font-size: .74rem; color: var(--muted); margin: .55rem 0 0; line-height: 1.4;
  padding-top: .55rem; border-top: 1px solid var(--border); }
.sr-card .sr-card-meta { margin-top: auto; }
@media (max-width: 560px) { .sr-card { flex-basis: 74vw; } .scroll-row { padding-left: 1.2rem; padding-right: 1.2rem; } }
"""


def _pane_grid_html(dom_cards, cat_order, placeholder="Search…"):
    """A `.lib-pane` grid: a search box (+ category pills once there's more
    than one category) over `dom_cards`, grouped by category. Same markup and
    classes the site-wide library pane uses, so LIBRARY_FILTER_JS wires it up
    automatically — used for both a detached desk's own page and its mirrored
    pane on the home page."""
    cats = [c for c in (cat_order or []) if any(cc["category"] == c for cc in dom_cards)]
    for cc in dom_cards:
        if cc["category"] not in cats:
            cats.append(cc["category"])
    multi = len(cats) > 1
    pills = ['<button class="cat-pill active" data-cat="all">All</button>'] if multi else []
    sections = []
    for cat in cats:
        cat_cards = [c["html"] for c in dom_cards if c["category"] == cat]
        if multi:
            pills.append(f'<button class="cat-pill" data-cat="{html.escape(cat, quote=True)}">'
                         f'{html.escape(cat)} <span class="cat-count">{len(cat_cards)}</span></button>')
        head = (f'<h3 class="cat-heading">{html.escape(cat)} '
                f'<span class="cat-count">{len(cat_cards)}</span></h3>') if multi else ""
        sections.append(
            f'<section class="cat-section" data-cat="{html.escape(cat, quote=True)}">{head}'
            f'<div class="grid">{"".join(cat_cards)}</div></section>')
    pills_html = f'<div class="cat-pills">{"".join(pills)}</div>' if multi else ""
    return (
        '<div class="lib-pane">'
        '<div class="lib-toolbar">'
        f'<input class="lib-search" type="search" placeholder="{html.escape(placeholder, quote=True)}" '
        f'aria-label="{html.escape(placeholder, quote=True)}" autocomplete="off">'
        f'{pills_html}</div>'
        + "\n".join(sections)
        + '<p class="lib-empty" hidden>No corpora match your search.</p>'
        + '</div>'
    )


def mirror_spread_html(library_pane, library_meta, lib_passages, adtech_passages, hub_desk):
    """The home page's mirrored spread: Today's Passage side by side (one pull
    from the Research Library, one from the detached desk), then the two
    panes continuing as parallel scrollable shelves underneath. Falls back to
    a single Research pane when no desk is configured."""
    # Each column is three fixed bands — masthead, passage, shelf — laid onto the
    # parent's rows with subgrid so the two sides stay level with each other no
    # matter how long a title runs or how tall a mascot is.
    body = (
        '<div class="mirror-grid">'
        '<div class="mirror-col mc-lib">'
        '<div class="mirror-mast">'
        f'<div class="mirror-agent">{section_agent_art("library", compact=True)}</div>'
        '<div class="mirror-id">'
        '<h2 class="mirror-h" id="library"><a href="research.html">The Research Library</a></h2>'
        f'<p class="mirror-sub">{html.escape(library_meta)}</p>'
        '</div></div>'
        '<div class="mirror-passage">'
        '<div class="dp-pane mc-lib" id="daily-passage-lib" hidden></div>'
        f'<script id="passages-data-lib" type="application/json">{json_for_html(lib_passages)}</script>'
        '</div>'
        '<div class="mirror-body">'
        f'{library_pane}'
        # Personalised blocks sit under the shelf, so both columns open on their
        # search toolbar and the spread still reads as a mirror.
        '<div id="resume"></div>'
        '<section id="foryou" hidden></section>'
        '<a class="mirror-more" href="research.html">Enter the full library →</a>'
        '</div></div>'
    )
    if hub_desk:
        desk_pane = _pane_grid_html(hub_desk["cards"], hub_desk.get("cats") or [],
                                    placeholder=f'Search {hub_desk["title"]}…')
        href = html.escape(hub_desk["href"], quote=True)
        body += (
            '<div class="mirror-col mc-adtech">'
            '<div class="mirror-mast">'
            f'<div class="mirror-agent">{section_agent_art("adtech", compact=True)}</div>'
            '<div class="mirror-id">'
            f'<h2 class="mirror-h"><a href="{href}">{html.escape(hub_desk["title"])}</a></h2>'
            f'<p class="mirror-sub">{html.escape(hub_desk["meta"])}</p>'
            '</div></div>'
            '<div class="mirror-passage">'
            '<div class="dp-pane mc-adtech" id="daily-passage-adtech" hidden></div>'
            f'<script id="passages-data-adtech" type="application/json">{json_for_html(adtech_passages)}</script>'
            '</div>'
            '<div class="mirror-body">'
            f'{desk_pane}'
            f'<a class="mirror-more" href="{href}">Enter the full desk →</a>'
            '</div></div>'
        )
    body += '</div>'
    return f'<section class="mirror-spread">{body}</section>'


def _scroll_card_html(kicker, title, meta, href, scene_kind="pamphlet", seed=None):
    return (f'<a class="sr-card" href="{html.escape(href, quote=True)}">'
            f'{scene_plate(scene_kind, extra_class="card-scene", seed=seed or href or title)}'
            f'<p class="sr-card-kicker">{html.escape(kicker)}</p>'
            f'<h3 class="sr-card-title">{html.escape(title)}</h3>'
            f'<p class="sr-card-meta">{html.escape(meta)}</p></a>')


def bottom_scroll_row_html(title, sub, href, cards_html, accent=None):
    """A bottom-of-page horizontal carousel row (Ghost of Times / Pamphlets /
    Briefings) — a scrollable hint at further reading, not another full
    shelf. Empty string (no row at all) when there's nothing to show."""
    if not cards_html:
        return ""
    style = f' style="--sr-accent:{html.escape(accent, quote=True)}"' if accent else ""
    return (
        f'<section class="scroll-row"{style}>'
        f'<div class="sr-head"><h2 class="sr-h"><a href="{html.escape(href, quote=True)}">{html.escape(title)}</a></h2>'
        f'<p class="sr-sub">{html.escape(sub)}</p>'
        f'<a class="sr-more" href="{html.escape(href, quote=True)}">See all →</a></div>'
        f'<div class="sr-track-wrap"><div class="sr-track">{cards_html}</div></div>'
        '</section>'
    )


def build_domain_page(out_dir, page_cfg, dom, dom_cards, cat_order, stats, bands="", tools="", quiz="",
                      bottom_bands="", shell=""):
    """Render a detached domain front (docs/<slug>.html): nameplate, any bands it
    pulled off the home page (the wire, its Briefings rack), its own apparatus row
    (tools — the desk's forecaster and dictionary), its category shelves as the
    standard card grid (with the live search box; category headings only when it
    shelves >1 category), and — when granted — its own Test Yourself quiz.
    page_cfg's optional "accent": (light, dark) hex pair keeps the whole desk's
    page run (not just its home-page bands) in one signature color."""
    out = Path(out_dir)
    accent_css = _accent_css(page_cfg.get("accent"))
    slug = page_cfg.get("slug", "adtech")
    title = page_cfg.get("title") or dom.get("title", "The Desk")
    kicker = page_cfg.get("kicker", "A separate desk")
    subtitle = page_cfg.get("subtitle", dom.get("title", ""))
    scene_kind = "research" if slug == "research" else "briefing"
    search_placeholder = "Search the library..." if slug == "research" else "Search the desk..."
    body = _pane_grid_html(dom_cards, cat_order, placeholder=search_placeholder)
    og = og_tags(title, subtitle or title, f"{SITE_URL}/{slug}.html", f"{SITE_URL}/{OG_IMAGE}")
    (out / f"{slug}.html").write_text(DOMAIN_PAGE_TEMPLATE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        kicker=html.escape(kicker),
        slug=slug,
        favicon=FAVICON,
        og_meta=og,
        nav=main_nav_html(active=f"{slug}.html"),
        css=(LIBRARY_CSS + SCENE_PLATE_CSS + FINGERPRINT_BAND_CSS + PAMPHLETS_BAND_CSS + DOMAIN_PAGE_CSS
             + (QUIZ_CSS if quiz else "")),
        accent_css=accent_css,
        folio_left=html.escape(page_cfg.get("folio_left", "The desk")),
        stats=html.escape(stats),
        folio_right=html.escape(page_cfg.get("folio_right", "Research + the daily wire")),
        scene=scene_plate(scene_kind, extra_class="section-scene", seed=f"domain:{slug}"),
        hero=desk_hero_art(slug),
        bands=bands,
        tools=tools,
        quiz=quiz,
        bottom_bands=bottom_bands,
        cards=body,
        epigraph=html.escape(page_cfg.get("epigraph", "“We sell — or else.” — David Ogilvy")),
        theme_js=LIBRARY_THEME_JS + LIBRARY_FILTER_JS + (QUIZ_JS if quiz else ""),
        shell=shell,
    ))


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
.wr-kicker { font-family: var(--sans); font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); margin: 0 0 .5rem; }
.wr-name { font-family: var(--display); font-weight: 600; font-size: clamp(2.4rem, 6vw, 4rem); line-height: .98; letter-spacing: -.01em; margin: 0 0 .55rem; }
.wr-motto { font-family: var(--serif); font-style: italic; font-size: 1.05rem; color: var(--muted); margin: 0; }
.wr-holdings { max-width: 560px; margin: 1.6rem auto 0; padding: 0 2rem; text-align: center; }
.wr-holdings svg { width: 100%; max-width: 460px; height: auto; display: block; margin: 0 auto; }
.wr-holdings .hc-1 { fill: var(--t1); } .wr-holdings .hc-2 { fill: var(--t2); }
.wr-holdings .hc-3 { fill: var(--t3); } .wr-holdings .hc-4 { fill: var(--t4); }
.wr-holdings .hc-5 { fill: var(--t5); }
.wr-holdings .hc-cell { stroke: var(--border); stroke-width: 1; }
.wr-holdings figcaption { font-family: var(--mono); font-size: .66rem; letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); margin-top: .6rem; }
#wrapped-body { max-width: 820px; margin: 1.8rem auto 0; padding: 0 2rem 2rem; }
.wr-identity { font-family: var(--display); font-size: 1.35rem; text-align: center; margin: 0 0 1.6rem; }
.wr-identity strong { color: var(--accent); }
.wr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1rem; }
.wr-card { background: transparent; border: 1px solid var(--border); border-radius: 0; padding: 1.3rem 1.4rem; display: flex; flex-direction: column; box-shadow: none;
  transition: border-color .16s var(--ease); }
.wr-card:hover { border-color: var(--text); }
.wr-val { font-family: var(--display); font-weight: 600; font-variant-numeric: oldstyle-nums; font-size: 2.4rem; color: var(--accent); line-height: 1; }
.wr-label { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-top: .55rem; }
.wr-sub { font-family: var(--sans); font-size: .78rem; color: var(--text); margin-top: .2rem; }
.wr-share { display: block; margin: 1.9rem auto 0; padding: .7rem 1.4rem; background: var(--text); color: var(--bg);
  border: 1px solid var(--text); border-radius: 2px; font-family: var(--sans); font-size: .78rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .1em; cursor: pointer; box-shadow: none;
  transition: background-color .15s var(--ease), transform .14s var(--ease), box-shadow .14s var(--ease); }
.wr-share:hover, .wr-share:focus-visible { background: var(--accent); border-color: var(--accent);
  transform: translateY(-1px); box-shadow: var(--shadow-2); }
.wr-share:active { transform: translateY(0); box-shadow: none; }
.wr-empty { max-width: 540px; margin: 2.5rem auto; text-align: center; font-family: var(--sans); color: var(--muted); line-height: 1.65; }
.wr-foot { max-width: 820px; margin: 2.5rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.wr-foot a { color: var(--accent); text-decoration: none; font-family: var(--mono); font-size: .68rem; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; }
@media (prefers-reduced-motion: reduce) {
  .wr-share, .wr-share:hover, .wr-share:focus-visible, .wr-share:active {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
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
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>Research Wrapped — calvincollins · xyz</title>
<meta name="description" content="Your private year in reading.">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<header class="wr-plate">
  <p class="wr-kicker">A private year in reading</p>
  <h1 class="wr-name">Research Wrapped</h1>
  <p class="wr-motto">Yours alone — computed on this device, stored nowhere else.</p>
  {scene}
</header>
<figure class="wr-holdings" aria-label="Every corpus in the library, one square each">{holdings}<figcaption>The holdings — one square per corpus, inked by subject</figcaption></figure>
<main id="wrapped-body"></main>
<footer class="wr-foot"><a href="research.html">← Back to the Research Library</a></footer>
<script id="wrapped-stats" type="application/json">{stats_json}</script>
<script>{theme_js}</script>
<script>{wrapped_js}</script>
{shell}
</body>
</html>
"""


# ---------------------------------------------------------------- the Glossary page
# A single site-wide reference: every term of art across the whole library, merged
# and de-duplicated, defined in plain language, with links back to the corpora that
# use each one. Fed by each corpus's `glossary` (see load_glossary / build.py loop).
GLOSSARY_EXTRA_CSS = """
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border);
  border-radius: 2px; box-shadow: var(--shadow-2); padding: .4rem .7rem; cursor: pointer;
  transition: color .15s var(--ease), border-color .15s var(--ease), transform .16s var(--ease); }
#theme-btn:hover, #theme-btn:focus-visible { color: var(--accent); border-color: var(--text); transform: translateY(-2px); }
.gl-wrap { max-width: 880px; margin: 0 auto; padding: 2rem 2rem 5rem; }
.gl-head { display: block; text-align: left; margin: .6rem 0 .4rem; }
.gl-head .kicker { margin: 0 0 .5rem; }
.gl-head h1 { font-size: clamp(2.4rem, 5vw, 3.2rem); margin: 0; }
.gl-head h1::after { content: ""; display: block; width: 148px; height: 8px; margin-top: .65rem; border-radius: 0;
  background:
    linear-gradient(var(--accent), var(--accent)) 0 0 / 28px 8px no-repeat,
    linear-gradient(180deg, var(--text) 0 3px, transparent 3px 5px, var(--text) 5px 6px, transparent 6px) 36px 0 / 112px 8px no-repeat; }
.gl-head .tagline { max-width: 56ch; margin: 1rem 0 0; }
/* sticky toolbar — filter + A-Z jump, frosted so the prose reads under it */
.gl-tools { position: sticky; top: 0; z-index: 5; margin: 1.4rem 0 .4rem; padding: .8rem 0 .7rem;
  background: color-mix(in srgb, var(--bg) 90%, transparent); -webkit-backdrop-filter: blur(9px); backdrop-filter: blur(9px);
  border-bottom: 1px solid var(--border); }
.gl-filters { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem .8rem; }
#gl-q { flex: 1 1 240px; min-width: 0; font-family: var(--serif); font-size: 1.05rem; color: var(--text);
  background: var(--panel); border: 1px solid var(--border); border-radius: 2px; padding: .75rem 1rem;
  outline: none; -webkit-appearance: none; appearance: none; box-shadow: none; }
#gl-q:focus { border-color: var(--accent); box-shadow: var(--ring); }
#gl-q::placeholder { color: var(--muted); }
#gl-article { flex: 0 1 220px; min-width: 0; font-family: var(--sans); font-size: .82rem; font-weight: 600; color: var(--text);
  background: var(--panel); border: 1px solid var(--border); border-radius: 2px; padding: .6rem .8rem;
  outline: none; -webkit-appearance: none; appearance: none; cursor: pointer; box-shadow: none; }
#gl-article:focus { border-color: var(--accent); box-shadow: var(--ring); }
#gl-cats { margin-top: .65rem; }
#gl-cats .cat-pill { font-size: .68rem; padding: .28rem .6rem; }
/* the letter rail — a mini plate lattice, the site's index move at reference scale */
#gl-az { display: flex; flex-wrap: wrap; gap: 1px; margin-top: .7rem; }
#gl-az a { font-family: var(--sans); font-size: .66rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0; color: var(--muted); text-decoration: none;
  min-width: 1.7rem; text-align: center; padding: .3rem 0; border-radius: 0;
  outline: 1px solid var(--border); outline-offset: 0; position: relative;
  transition: color .15s var(--ease), background-color .15s var(--ease), outline-color .15s var(--ease); }
#gl-az a:hover, #gl-az a:focus-visible { color: var(--bg); background: var(--text); outline-color: var(--text); z-index: 1; }
.gl-group { margin-top: 2.4rem; scroll-margin-top: 6rem; }
.gl-group > h2 { font-family: var(--display); font-size: 2.3rem; font-weight: 600; color: var(--accent); margin: 0;
  display: flex; align-items: center; gap: .8rem; }
.gl-group > h2::after { content: ""; flex: 1; height: 2px; background: var(--text); }
.gl-group > dl { margin: .3rem 0 0; }
.gl-item { display: grid; grid-template-columns: minmax(128px, 208px) 1fr; gap: .25rem 1.9rem;
  padding: 1.05rem 0 1.05rem .9rem; border-bottom: 1px solid var(--border); align-items: baseline;
  background-image: linear-gradient(var(--accent), var(--accent));
  background-repeat: no-repeat; background-size: 3px 0%; background-position: 0 0;
  transition: background-size .18s var(--ease), background-color .15s var(--ease),
    border-color .15s var(--ease), color .15s var(--ease); }
.gl-item:hover { background-size: 3px 100%; }
.gl-item:hover dt { color: var(--accent); }
.gl-item dt { font-family: var(--display); font-weight: 600; font-size: 1.12rem; line-height: 1.28; }
.gl-item .gl-aka { display: block; font-family: var(--mono); font-weight: 500; font-size: .68rem;
  letter-spacing: .06em; text-transform: none; font-variant-numeric: tabular-nums; color: var(--muted);
  margin-top: .25rem; line-height: 1.45; }
.gl-item dd { margin: 0; font-family: var(--serif); font-size: 1.02rem; line-height: 1.62; color: var(--text); }
.gl-src { display: block; margin-top: .5rem; font-family: var(--mono); font-weight: 500; font-size: .68rem;
  text-transform: uppercase; letter-spacing: .06em; font-variant-numeric: tabular-nums; color: var(--muted); }
.gl-src a { display: inline-block; font-family: var(--mono); font-size: .72rem; letter-spacing: .02em;
  text-transform: none; color: var(--accent); text-decoration: none;
  border: 1px solid var(--border); border-radius: 0; padding: .14rem .5rem; margin: .15rem .35rem .15rem 0;
  transition: background-color .15s var(--ease), color .15s var(--ease), border-color .15s var(--ease); }
.gl-src a:hover, .gl-src a:focus-visible { background: var(--accent); border-color: var(--accent); color: var(--bg); }
#gl-none { color: var(--muted); font-family: var(--sans); padding: 2.5rem 0; text-align: center; }
.cx-foot { max-width: 1120px; margin: 2rem auto 0; padding: 1.4rem 2rem 3rem; border-top: 1px solid var(--border); text-align: center; }
.cx-foot .colophon { font-family: var(--mono); font-size: .68rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 0; }
.cx-foot a { color: var(--accent); text-decoration: none; }
@media (max-width: 640px) {
  .gl-wrap { padding: 1.4rem 1.3rem 4rem; }
  .gl-item { grid-template-columns: 1fr; gap: .35rem; padding: .95rem 0; }
  .gl-item dd { font-size: .98rem; }
  #gl-az a { min-width: 1.55rem; }
  .gl-item { padding-left: .7rem; }
}
@media (prefers-reduced-motion: reduce) {
  .gl-item, .gl-item:hover,
  #theme-btn, #theme-btn:hover, #theme-btn:focus-visible {
    transform: none !important;
    transition-duration: .01ms !important;
  }
}
"""

GLOSSARY_JS = r"""
(function () {
  // theme toggle — the theme itself is applied pre-paint by the <head> boot script
  document.getElementById('theme-btn').onclick = function () {
    var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('corpus-theme', next);
  };
})();
(function () {
  var noMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var q = document.getElementById('gl-q'), none = document.getElementById('gl-none');
  var article = document.getElementById('gl-article');
  var pills = [].slice.call(document.querySelectorAll('#gl-cats .cat-pill'));
  var items = [].slice.call(document.querySelectorAll('.gl-item'));
  var groups = [].slice.call(document.querySelectorAll('.gl-group'));
  var activeCat = 'all';

  function apply() {
    var s = q.value.trim().toLowerCase();
    var art = article.value;
    var shown = 0;
    items.forEach(function (it) {
      var textOk = !s || it.getAttribute('data-term').indexOf(s) >= 0;
      var catOk = activeCat === 'all' || it.getAttribute('data-cats').indexOf('|' + activeCat + '|') >= 0;
      var artOk = art === 'all' || it.getAttribute('data-slugs').indexOf('|' + art + '|') >= 0;
      var hit = textOk && catOk && artOk;
      it.style.display = hit ? '' : 'none'; if (hit) shown++;
    });
    groups.forEach(function (g) {
      var vis = g.querySelector('.gl-item:not([style*="none"])');
      g.style.display = vis ? '' : 'none';
    });
    none.hidden = shown > 0;
  }

  q.addEventListener('input', apply);
  article.addEventListener('change', apply);
  pills.forEach(function (pill) {
    pill.addEventListener('click', function () {
      activeCat = pill.dataset.cat;
      pills.forEach(function (p) { p.classList.toggle('active', p === pill); });
      apply();
    });
  });

  document.querySelectorAll('#gl-az a').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault(); var t = document.getElementById(a.getAttribute('href').slice(1));
      if (t) t.scrollIntoView(noMotion ? { block: 'start' } : { behavior: 'smooth', block: 'start' });
    });
  });
})();
"""

GLOSSARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>{page_title} — calvincollins · xyz</title>
<meta name="description" content="{descr}">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}{accent_css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<main class="gl-wrap">
  <header class="gl-head">
    <p class="kicker">{kicker}</p>
    <h1>{h1}</h1>
    <p class="tagline">{intro}</p>
    {scene}
  </header>
  <div class="gl-tools">
    <div class="gl-filters">
      <input id="gl-q" type="search" placeholder="Filter {n} terms…" autocomplete="off" spellcheck="false" aria-label="Filter terms">
      <select id="gl-article" aria-label="Filter by article">{article_options}</select>
    </div>
    <div class="cat-pills" id="gl-cats">{cat_pills}</div>
    <nav id="gl-az" aria-label="Jump to a letter">{az}</nav>
  </div>
  <div id="gl-list">{entries}</div>
  <p id="gl-none" hidden>No terms match your filter.</p>
</main>
<footer class="cx-foot">
  <p class="colophon">{back}</p>
</footer>
<button id="theme-btn" title="Light / dark">◐ Theme</button>
<script>{app_js}</script>
{shell}
</body>
</html>
"""

GLOSSARY_NAV_DEFAULT = main_nav_html(active="glossary.html")


def build_glossary_page(out_dir, glossary_index, category_order=None, shell="", page=None):
    """Render a Glossary — docs/glossary.html by default: one merged, de-duplicated,
    searchable reference of every term across the given corpora, each linking back
    to the corpora that use it. Filterable by category (any category a source corpus
    belongs to) and by article (a specific corpus) in addition to the free-text
    search. A `page` override scopes it to a detached desk's own dictionary (e.g.
    the Ad Tech Glossary): {fname, title (h1), kicker, scope, nav, back, accent}."""
    out = Path(out_dir)
    page = page or {}
    fname = page.get("fname", "glossary.html")
    h1 = page.get("title", "Glossary")
    kicker = page.get("kicker", "Reference")
    scope = page.get("scope", "across the research library")
    nav = page.get("nav", GLOSSARY_NAV_DEFAULT)
    back = page.get("back", '<a href="research.html">← Back to the Research Library</a>')
    accent_css = _accent_css(page.get("accent"))
    merged = {}
    for g in glossary_index:
        for t in g["terms"]:
            term = (t.get("term") or "").strip()
            definition = (t.get("def") or "").strip()
            if not term or not definition:
                continue
            key = term.lower()
            e = merged.setdefault(key, {"term": term, "def": definition,
                                        "aliases": set(), "sources": {}})
            # keep the fullest definition; prefer a canonical casing that isn't all-lower
            if len(definition) > len(e["def"]):
                e["def"] = definition
            if term != term.lower() and e["term"] == e["term"].lower():
                e["term"] = term
            for a in (t.get("aliases") or []):
                if a and a.strip().lower() != key:
                    e["aliases"].add(a.strip())
            e["sources"][g["slug"]] = (g["title"], g["href"], g.get("category") or "")
    if not merged:
        return False
    entries = sorted(merged.values(), key=lambda e: e["term"].lower())

    def letter_of(term):
        c = term[:1].upper()
        return c if c.isalpha() else "#"

    from collections import OrderedDict
    groups = OrderedDict()
    for e in entries:
        groups.setdefault(letter_of(e["term"]), []).append(e)

    # Category order: configured order first, then any leftover categories in
    # first-seen order — same idiom used for the library index and Connections.
    seen_cats = []
    for g in glossary_index:
        c = g.get("category")
        if c and c not in seen_cats:
            seen_cats.append(c)
    gloss_cat_order = [c for c in (category_order or []) if c in seen_cats]
    gloss_cat_order += [c for c in seen_cats if c not in gloss_cat_order]
    cat_counts = {c: 0 for c in gloss_cat_order}

    az = "".join(f'<a href="#gl-{L}">{L}</a>' for L in groups)
    blocks = []
    for L, es in groups.items():
        rows = []
        for e in es:
            aka = ""
            if e["aliases"]:
                aka = ' <span class="gl-aka">' + html.escape(", ".join(sorted(e["aliases"]))) + "</span>"
            src_vals = sorted(e["sources"].values())  # (title, href, category)
            srcs = ", ".join(f'<a href="{href}">{html.escape(title)}</a>' for title, href, _ in src_vals)
            e_cats = {cat for _, _, cat in src_vals if cat}
            e_slugs = set(e["sources"].keys())
            for c in e_cats:
                if c in cat_counts:
                    cat_counts[c] += 1
            # a lowercase haystack for the client text filter (term + aliases)
            hay = html.escape((e["term"] + " " + " ".join(e["aliases"])).lower(), quote=True)
            # pipe-delimited membership lists for the category / article filters
            cats_attr = html.escape("|" + "|".join(sorted(e_cats)) + "|", quote=True)
            slugs_attr = html.escape("|" + "|".join(sorted(e_slugs)) + "|", quote=True)
            rows.append(
                f'<div class="gl-item" data-term="{hay}" data-cats="{cats_attr}" data-slugs="{slugs_attr}">'
                f'<dt>{html.escape(e["term"])}{aka}</dt>'
                f'<dd>{html.escape(e["def"])}'
                f'<span class="gl-src">Appears in {srcs}</span></dd></div>'
            )
        blocks.append(f'<section class="gl-group" id="gl-{L}"><h2>{L}</h2><dl>' + "".join(rows) + "</dl></section>")

    cat_pills = ['<button class="cat-pill active" data-cat="all">All categories</button>']
    for c in gloss_cat_order:
        cat_pills.append(
            f'<button class="cat-pill" data-cat="{html.escape(c, quote=True)}">'
            f'{html.escape(c)} <span class="cat-count">{cat_counts[c]}</span></button>'
        )
    article_opts = ['<option value="all">All articles</option>']
    for g in sorted(glossary_index, key=lambda g: g["title"].lower()):
        article_opts.append(
            f'<option value="{html.escape(g["slug"], quote=True)}">{html.escape(g["title"])}</option>'
        )

    n = len(entries)
    intro = (f"Every acronym, term of art, and named system {scope} — "
             f"{n} in all, defined in plain language, each linked to the corpora that use it. "
             f"Filter by category, article, or search below, or jump by letter.")
    descr = f"Every term of art {scope}, defined in plain language."
    og = og_tags(h1, descr, f"{SITE_URL}/{fname}", f"{SITE_URL}/{OG_IMAGE}")
    page_html = GLOSSARY_TEMPLATE.format(
        page_title=html.escape(h1), h1=html.escape(h1), kicker=html.escape(kicker),
        descr=html.escape(descr), nav=nav, back=back,
        favicon=FAVICON, og_meta=og, css=LIBRARY_CSS + SCENE_PLATE_CSS + GLOSSARY_EXTRA_CSS, accent_css=accent_css,
        intro=intro, n=n, az=az, entries="".join(blocks), app_js=GLOSSARY_JS, shell=shell,
        scene=scene_plate("briefing" if page.get("accent") else "research",
                          extra_class="page-scene", seed=f"glossary:{fname}"),
        cat_pills="".join(cat_pills), article_options="".join(article_opts),
    )
    (out / fname).write_text(page_html)
    print(f"  ✓ {h1}  ({n} terms across {len(glossary_index)} corpora, {len(gloss_cat_order)} categories) → {fname}")
    return True


QUIZ_PAGE_CSS = """
#theme-btn { position: fixed; bottom: 1.1rem; right: 1.1rem; z-index: 20; font-family: var(--sans);
  font-size: .8rem; color: var(--muted); background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: .4rem .7rem; cursor: pointer; }
#theme-btn:hover { color: var(--accent); border-color: var(--accent); }
.qz-wrap { max-width: 1080px; margin: 0 auto; padding: 2rem 2rem 5rem; }
.qz-head { display: block; text-align: left; margin: .6rem 0 .4rem; }
.qz-head .kicker { margin: 0 0 .5rem; }
.qz-head h1 { font-size: clamp(2.4rem, 5vw, 3.2rem); margin: 0; }
.qz-head h1::after { content: ""; display: block; height: 4px; width: 96px; margin-top: .7rem; border-radius: 2px;
  background: linear-gradient(90deg, var(--t1) 0 25%, var(--t2) 0 50%, var(--t3) 0 75%, var(--t4) 0); }
.qz-head .tagline { max-width: 56ch; margin: 1rem 0 0; }
.qz-wrap .quiz { padding: 0; margin-top: 1.8rem; }
@media (max-width: 640px) { .qz-wrap { padding: 1.4rem 1.3rem 4rem; } }
"""

QUIZ_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){{var t=null;try{{t=localStorage.getItem('corpus-theme')}}catch(e){{}}document.documentElement.dataset.theme=t==='light'?'light':'dark';}})();</script>
<title>Test Yourself — calvincollins · xyz</title>
<meta name="description" content="A quiz on what the research library actually says — three challenge levels, any corpus or the whole shelf.">
<link rel="icon" href="{favicon}">
{og_meta}
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <a class="mh-brand" href="index.html" aria-label="Go to the calvincollins.xyz homepage"><span>calvincollins · xyz</span></a>
  <nav class="mh-nav">
{nav}
  </nav>
</div>
<main class="qz-wrap">
  <header class="qz-head">
    <p class="kicker">Retention</p>
    <h1>Test Yourself</h1>
    <p class="tagline">{intro}</p>
    {scene}
  </header>
  {card}
</main>
<footer class="cx-foot" style="max-width:860px;margin:2rem auto 0;padding:1.4rem 2rem 3rem;border-top:1px solid var(--border);text-align:center">
  <p class="colophon" style="font-family:var(--sans);font-size:.74rem;color:var(--muted);margin:0"><a href="index.html" style="color:var(--accent);text-decoration:none">← Back to the Research Library</a></p>
</footer>
<button id="theme-btn" title="Light / dark">◐ Theme</button>
{payloads}
<script>{app_js}</script>
{shell}
</body>
</html>
"""


def build_quiz_page(out_dir, quiz_facts, all_passages, shell=""):
    """Render docs/quiz.html — the Test Yourself section: a self-quiz on what the
    research says (terms, dates, people, passages) at three challenge levels,
    scoped to one corpus or the whole shelf. Fact payloads are inlined here;
    questions build client-side, and readers deep-link in via ?on=<slug>."""
    out = Path(out_dir)
    intro = ("A short quiz on what the research actually says — the dates, the people, the "
             "terms of art, the passages. Pick a corpus or take on the whole shelf, choose a "
             "challenge level, and every missed answer links back to the chapter that has it. "
             "Your best scores stay on this device; nothing is sent anywhere.")
    og = og_tags("Test Yourself",
                 "A quiz on what the research library actually says — three challenge levels, any corpus or the whole shelf.",
                 f"{SITE_URL}/quiz.html", f"{SITE_URL}/{OG_IMAGE}")
    payloads = (f'<script id="quiz-data" type="application/json">{json_for_html(quiz_facts)}</script>'
                f'<script id="passages-data" type="application/json">{json_for_html(all_passages)}</script>')
    page = QUIZ_PAGE_TEMPLATE.format(
        favicon=FAVICON, og_meta=og, css=LIBRARY_CSS + SCENE_PLATE_CSS + QUIZ_CSS + QUIZ_PAGE_CSS,
        nav=main_nav_html(active="quiz.html"),
        intro=intro, card=QUIZ_CARD_HTML, payloads=payloads,
        scene=scene_plate("quiz", extra_class="page-scene", seed="quiz-page"),
        app_js=LIBRARY_THEME_JS + QUIZ_JS, shell=shell,
    )
    (out / "quiz.html").write_text(page)
    print(f"  ✓ Test Yourself  ({len(quiz_facts)} corpora with facts) → quiz.html")


def holdings_chart_svg(stats, category_order=None):
    """One 14px square per corpus, 12 per row, 4px gap, inked by category (t1..t5
    cycle, category_order first, then first-seen). Classes hc-1..hc-5 + hc-cell;
    WRAPPED_CSS colors them."""
    cats = list(category_order or [])
    for w in stats:
        if w["category"] not in cats:
            cats.append(w["category"])
    ink = {c: (i % 5) + 1 for i, c in enumerate(cats)}
    n = len(stats)
    rows = max(1, math.ceil(n / 12))
    cells = []
    for i, w in enumerate(stats):
        x, y = (i % 12) * 18, (i // 12) * 18
        cells.append(f"<rect class='hc-cell hc-{ink[w['category']]}' x='{x}' y='{y}' width='14' height='14'/>")
    return (f"<svg viewBox='0 0 {12 * 18 - 4} {rows * 18 - 4}' xmlns='http://www.w3.org/2000/svg' "
            f"role='img' aria-label='{n} corpora'>" + "".join(cells) + "</svg>")


def build_wrapped_page(out_dir, wrapped_stats, shell="", category_order=None):
    """Render docs/wrapped.html — a client-side 'year in reading' from localStorage."""
    out = Path(out_dir)
    page = WRAPPED_TEMPLATE.format(
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(active="wrapped.html"),
        css=LIBRARY_CSS + SCENE_PLATE_CSS + WRAPPED_CSS,
        stats_json=json_for_html(wrapped_stats),
        holdings=holdings_chart_svg(wrapped_stats, category_order),
        scene=scene_plate("wrapped", extra_class="page-scene", seed="wrapped-page"),
        theme_js=LIBRARY_THEME_JS,
        wrapped_js=WRAPPED_JS,
        shell=shell,
    )
    (out / "wrapped.html").write_text(page)
    print("  ✓ Research Wrapped → wrapped.html")


def build(folders, out_dir, site_title, site_subtitle, ghost_cfg=None, descriptions=None,
          fingerprint_cfg=None, pamphlets_cfg=None, forecast_cfg=None, titles=None, category_order=None, domains=None, collections=None, atlas_cfg=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    publish_research_scenes(out)
    # Copy top-level image assets into the served output so absolute URLs resolve on the live site.
    for image_asset in SITE_IMAGE_ASSETS:
        src = HERE / image_asset
        if src.exists():
            shutil.copy(src, out / image_asset)
    ghost_cfg = ghost_cfg or {}
    fingerprint_cfg = fingerprint_cfg or {}
    pamphlets_cfg = pamphlets_cfg or {}
    forecast_cfg = forecast_cfg or {}
    descriptions = descriptions or {}
    titles = titles or {}
    category_order = category_order or []
    domains = domains or []
    # Corpus category -> the detached domain page it belongs to (e.g. "Media &
    # Advertising" -> adtech.html), so a corpus reader's "Library" link goes
    # back to the desk it lives on rather than always the site-wide index.
    domain_page_by_category = {}
    for _d in domains:
        _pg = _d.get("page")
        if not _pg:
            continue
        for _c in _d.get("categories", []):
            domain_page_by_category[_c] = (f"{_pg['slug']}.html", _pg.get("title", _d["title"]))
    cards = []
    manifest = []          # cross-page command-palette index (corpora + sections + editions)
    pages = []             # reader renders deferred until the manifest below is complete
    search_entries = []    # trimmed chapter text for the palette's "in the text" search
    corpus_meta = []       # {slug,title,category,keywords} for the similarity graph
    all_passages = []      # pull-quotes across every corpus, for Today's Passage
    quiz_facts = []        # {slug, terms, dates, entities} per corpus, for Test Yourself
    glossary_index = []    # {slug,title,href,terms[]} per corpus, for the site-wide Glossary page
    corpora_by_slug = {}   # full corpus objects, retained for assembling collections
    atlas_corpora = {}     # {slug: {title,href,accent,img,chapters}} for the Atlas map
    wrapped_stats = []     # per-corpus {slug,title,category,chapters,words} for Research Wrapped
    fd_markets = []        # harvested Forecast Desk markets (one per corpus with scenarios)
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
        corpus["glossary"] = load_glossary(folder)
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
            "category": override.get("category") or "Other",
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
            f'<div class="cover">{card_cover(corpus["slug"], corpus["title"], theme_cover_palette(theme), cat=category)}</div>'
            f'<div class="card-body"><h2>{html.escape(corpus["title"])}</h2>'
            f'<p class="sub">{html.escape(card_sub)}</p>'
            f'<p class="meta">{" · ".join(meta_bits)}</p></div></a>'
        )
        cards.append({"category": category, "html": card_html, "slug": corpus["slug"]})
        manifest.append({
            "slug": corpus["slug"],
            "title": corpus["title"],
            "category": category,
            "kind": "corpus",
            "href": f"{corpus['slug']}.html",
            "chapters": [d["title"] for d in corpus["documents"]],
        })
        if corpus.get("glossary"):
            glossary_index.append({
                "slug": corpus["slug"], "title": corpus["title"], "category": category,
                "href": f"{corpus['slug']}.html", "terms": corpus["glossary"],
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
        _qf = quiz_facts_for(corpus, folder)
        if any(_qf.values()):
            quiz_facts.append(dict(_qf, slug=corpus["slug"]))
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
        fd_m = harvest_corpus_market(folder, corpus, category,
                                     cover=f"covers/{_atlas_img.name}" if _atlas_img else None,
                                     description=card_sub)
        if fd_m:
            fd_markets.append(fd_m)
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
    pamphlet_items = read_pamphlets_manifest(out) if pamphlets_cfg.get("enabled", True) else []
    forecast_items = read_forecast_manifest(out) if forecast_cfg.get("enabled", True) else []

    # The grading loop: join docs/forecast/resolutions.json against every market.
    # Graded native items carry `_graded` (the full scored verdict); graded
    # harvested markets carry `resolution`. Cumulative per-persona records are
    # computed here — never hand-maintained — and worn by every roster.
    fd_resolutions = read_forecast_resolutions(out) if forecast_cfg.get("enabled", True) else {}
    fd_native_data = {}
    for f in forecast_items:
        d = read_forecast_data(out, f.get("slug", ""))
        if not d:
            continue
        fd_native_data[f.get("slug", "")] = d   # slug → full data, for The Book's odds
        res = _native_resolution(d, fd_resolutions)
        if res:
            f["_graded"] = grade_native_forecast(d, res)
            f["status"] = "graded"
    for m in fd_markets:
        attach_market_resolution(m, fd_resolutions)
    persona_records = {k: {"graded": v["graded"], "hits": v["hits"]}
                       for k, v in build_forecast_ledger(forecast_items, fd_markets)["personas"].items()}

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
    if pamphlets_cfg.get("enabled", True):
        manifest.append({"title": "The Pamphlets", "kind": "section",
                         "category": "Essays", "href": "pamphlets.html",
                         "meta": "writer-voiced essays"})
        for p in pamphlet_items:
            voice = f" · {p['writer']}" if p.get("writer") else ""
            manifest.append({
                "title": p.get("title") or f"Pamphlet — {p.get('slug', '')}",
                "kind": "pamphlet", "category": "The Pamphlets",
                "href": p.get("file") or f"pamphlets/{p.get('slug', '')}.html",
                "meta": (p.get("dek", "") + voice).strip(" ·"),
            })

    if forecast_cfg.get("enabled", True):
        manifest.append({"title": "The Forecast Desk", "kind": "section",
                         "category": "Predictions", "href": "forecast.html",
                         "meta": "every prediction, priced and graded"})
        manifest.append({"title": "The Track Record", "kind": "section",
                         "category": "Predictions", "href": "forecast-record.html",
                         "meta": "graded calls, Brier scores, calibration"})
        for f in forecast_items:
            _gm = f.get("_graded")
            _meta = (f'graded — {("✓ " if _gm["consensus"]["hit"] else "✗ ") + _gm["winner"]} won' if _gm
                     else " · ".join(x for x in [f.get("pick", ""), f.get("band", "")] if x))
            manifest.append({
                "title": f.get("question") or f.get("title") or f"Forecast — {f.get('slug', '')}",
                "kind": "forecast", "category": "The Forecast Desk",
                "href": f.get("file") or f"forecast/{f.get('slug', '')}.html",
                "meta": _meta,
            })

    # Detached domain fronts (config domains carrying a "page" key) — e.g. the
    # Ad Tech desk — join the palette as sections so ⌘K can jump to them, along
    # with any apparatus of their own (page.include: "forecast" / "glossary").
    for d in domains:
        p = d.get("page")
        if not p:
            continue
        _slug = p.get("slug", "adtech")
        _title = p.get("title") or d.get("title", "")
        _cats = set(d.get("categories", []))
        _inc = p.get("include") or []
        manifest.append({"title": _title, "kind": "section",
                         "category": "The desk", "href": f"{_slug}.html",
                         "meta": p.get("kicker", "a separate desk")})
        if "forecast" in _inc and any(m["category"] in _cats for m in fd_markets):
            manifest.append({"title": p.get("forecast_title", f"The {_title} Board"),
                             "kind": "section", "category": "The desk",
                             "href": f"{_slug}-forecast.html",
                             "meta": "the desk's predictions, priced"})
            manifest.append({"title": p.get("record_title", f"The {_title} Track Record"),
                             "kind": "section", "category": "The desk",
                             "href": f"{_slug}-record.html",
                             "meta": "the desk's graded calls, scored"})
        if "glossary" in _inc and any(g.get("category") in _cats for g in glossary_index):
            manifest.append({"title": p.get("glossary_title", f"The {_title} Glossary"),
                             "kind": "section", "category": "The desk",
                             "href": f"{_slug}-glossary.html",
                             "meta": "the trade's terms, defined"})
        if "briefings" in _inc:
            _bslug = p.get("briefings_slug", "briefings")
            _btitle = p.get("briefings_title", "The Briefings")
            manifest.append({"title": _btitle, "kind": "section", "category": "The desk",
                             "href": f"{_bslug}.html",
                             "meta": p.get("briefings_kicker", "essays from the desk")})
            for b in read_pamphlets_manifest(out, subdir=_bslug):
                _voice = f" · {b['writer']}" if b.get("writer") else ""
                manifest.append({
                    "title": b.get("title") or f"Briefing — {b.get('slug', '')}",
                    "kind": "pamphlet", "category": _btitle,
                    "href": b.get("file") or f"{_bslug}/{b.get('slug', '')}.html",
                    "meta": (b.get("dek", "") + _voice).strip(" ·"),
                })

    manifest.append({"title": "Research Wrapped", "kind": "section", "category": "You",
                     "href": "wrapped.html", "meta": "your year in reading"})
    manifest.append({"title": "Connections", "kind": "section",
                     "category": "The map of ideas", "href": "connections.html",
                     "meta": "how the corpora relate"})
    if glossary_index:
        _gterms = sum(len({t["term"].lower() for t in g["terms"]}) for g in glossary_index)
        manifest.append({"title": "Glossary", "kind": "section", "category": "Reference",
                         "href": "glossary.html",
                         "meta": "every term across the research, defined"})
    if quiz_facts:
        manifest.append({"title": "Test Yourself", "kind": "section", "category": "Reference",
                         "href": "quiz.html",
                         "meta": "a quiz on the research — three levels"})

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
            groups=atlas_cfg.get("groups") or ATLAS_GROUPS,
        )
        if atlas_data:
            conn = compute_atlas_connections(atlas_geo, atlas_corpora, load_atlas_connections())
            if conn:
                atlas_data["connections"] = conn
            (out / "atlas.json").write_text(json.dumps(atlas_data, separators=(",", ":"), ensure_ascii=False))
            n_nodes = sum(len(v["nodes"]) for v in conn.values()) if conn else 0
            cmsg = f", {len(conn)} intellectual-world maps ({n_nodes} nodes)" if conn else ""
            print(f"  ✓ Atlas: {len(atlas_data['places'])} stamps across {len(atlas_data['plates'])} plates{cmsg} → atlas.json")
        else:
            print("  ! Atlas: no corpora placed (check the 'atlas' placements)", file=sys.stderr)
    else:
        print("  ! Atlas: atlas/geo.json not found — run scripts/build_atlas_geo.py", file=sys.stderr)

    # Now write the reader pages, each carrying the shared shell.
    for p in pages:
        _back_href, _back_label = domain_page_by_category.get(p["category"], ("index.html", "Library"))
        (out / f"{p['slug']}.html").write_text(READER_TEMPLATE.format(
            title=html.escape(p["title"]),
            subtitle=html.escape(p["subtitle"]),
            css=CSS + SCENE_PLATE_CSS,
            theme_style=p["theme_style"],
            favicon=FAVICON, og_meta=p["reader_og"],
            data_json=shrink_data_uris(p["data_json"]),
            marked_js=MARKED_JS,
            app_js=APP_JS,
            shell=shell_root,
            back_href=_back_href,
            back_label=_back_label,
            scene=scene_plate("briefing" if p["category"] == "Media & Advertising" else "research",
                              extra_class="reader-scene", cover_slugs=(p["slug"],), seed=p["slug"]),
        ))

    # Collection pages — merged cross-corpus readers, rendered through the same
    # reader template so they inherit the TOC, search, pager, keyboard nav + shell.
    for col_corpus, meta in resolved_collections:
        col_og = og_tags(meta["title"], (meta["essay"][:200] or meta["title"]),
                         f"{SITE_URL}/{meta['slug']}.html", f"{SITE_URL}/{OG_IMAGE}")
        (out / f"{meta['slug']}.html").write_text(READER_TEMPLATE.format(
            title=html.escape(meta["title"]), subtitle=html.escape(col_corpus["subtitle"]),
            css=CSS + SCENE_PLATE_CSS, theme_style="", favicon=FAVICON, og_meta=col_og,
            data_json=shrink_data_uris(json_for_html(col_corpus)), marked_js=MARKED_JS, app_js=APP_JS, shell=shell_root,
            back_href="research.html", back_label="Library",
            scene=scene_plate("collection", extra_class="reader-scene",
                              cover_slugs=meta.get("slugs"), seed=meta["slug"])))
    if resolved_collections:
        print(f"  ✓ Rendered {len(resolved_collections)} collection(s)")

    # The Connections page — interactive theme-graph of the corpora.
    build_connections_page(out, [e for e in manifest if e.get("kind") == "corpus"], category_order, shell=shell_root)

    # Detached domain fronts (config domains carrying a "page" object, e.g. the
    # Ad Tech desk) can claim site apparatus via page.include: the categories of
    # a desk that lists "glossary" / "forecast" come OFF the site-wide pages and
    # get desk-scoped editions instead (built alongside the desk, further down).
    detached = [d for d in domains if d.get("page")]
    detached_cats = {c for d in detached for c in d.get("categories", [])}
    gl_desk_cats = {c for d in detached if "glossary" in (d["page"].get("include") or [])
                    for c in d.get("categories", [])}
    fc_desk_cats = {c for d in detached if "forecast" in (d["page"].get("include") or [])
                    for c in d.get("categories", [])}
    qz_desk_cats = {c for d in detached if "quiz" in (d["page"].get("include") or [])
                    for c in d.get("categories", [])}

    build_glossary_page(out, [g for g in glossary_index if g.get("category") not in gl_desk_cats],
                        category_order=category_order, shell=shell_root)
    # Test Yourself quizzes the WHOLE library — desk corpora included; a quiz on
    # what you read spans every shelf, unlike the desk-scoped reference pages.
    build_quiz_page(out, quiz_facts, all_passages, shell=shell_root)

    # The Ghost of Times section (second top-level section of the site). Its
    # old single-line home-page teaser is gone — the front page now carries a
    # scrollable Ghost of Times row at the bottom instead (see bottom_scrolls).
    build_ghost_page(out, editions, ghost_cfg, shell=shell_root)
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

    # The Pamphlets section (fourth top-level section — standalone writer
    # essays). Home-page presence is now the bottom scroll row, not a band.
    build_pamphlets_page(out, pamphlet_items, pamphlets_cfg, shell=shell_root)
    pam_rendered = sum(build_pamphlet(out, p, shell=shell_sub) for p in pamphlet_items)
    if pamphlet_items:
        print(f"  ✓ Rendered {pam_rendered}/{len(pamphlet_items)} pamphlet page(s) from data")

    # The Forecast Desk (fifth top-level section). The site-wide board is split
    # into three labeled wings: the standalone live calls (sports & politics),
    # the research corpora, and the Ad Tech markets. Ad Tech shows on BOTH the
    # main board (this wing) and its own detached desk board (built below), so
    # unlike glossary/quiz the forecast markets are NOT held back here.
    adtech_title = next((d.get("title", "Ad Tech & Media") for d in detached
                         if "forecast" in (d["page"].get("include") or [])), "Ad Tech & Media")
    fd_sections = [
        {"title": "Sports & Politics", "color": "#f59e0b", "native": True,
         "kicker": "Standalone live calls — bare-topic questions, not drawn from a corpus"},
        {"title": "From the Research", "color": "#60a5fa", "rest": True,
         "kicker": "Every research corpus's forecast, priced as a market"},
        {"title": adtech_title, "color": _fd_cat_color("Media & Advertising"),
         "categories": sorted(fc_desk_cats),
         "kicker": "The trade desk's predictions — also on the Ad Tech board"},
    ]
    build_forecast_page(out, forecast_items, fd_markets, forecast_cfg,
                        category_order=category_order, shell=shell_root,
                        native_data=fd_native_data, sections=fd_sections)
    # Home-page presence is now the top ticker (see ticker_html), not a band.
    fd_rendered = sum(build_forecast_item(out, f, shell=shell_sub, records=persona_records)
                      for f in forecast_items)
    fdm_rendered = sum(build_corpus_market_page(out, m, shell=shell_sub, records=persona_records)
                       for m in fd_markets)
    if fd_markets:
        print(f"  ✓ Rendered {fdm_rendered} research market page(s) → forecast/")
    if forecast_items:
        print(f"  ✓ Rendered {fd_rendered}/{len(forecast_items)} live forecast page(s) from data")
    # The Track Record — the site-wide accountability page covers EVERY market,
    # desk-scoped ones included; each detached desk also gets its own scoped copy.
    build_record_page(out, forecast_items, fd_markets, forecast_cfg, shell=shell_root,
                       native_data=fd_native_data)
    # One full profile page per standing predictor (docs/forecasters/{key}.html),
    # plus one page comparing all seven side by side — both computed from every
    # market site-wide so a desk-scoped record still rolls up into the same
    # complete history. The roster cards on both Track Record pages (site-wide
    # and each desk's own), the mini roster atop every board, and every
    # per-market trader card all link into these.
    _fd_site_led = build_forecast_ledger(forecast_items, fd_markets)
    _fd_site_book = build_book(forecast_items, fd_markets, fd_native_data)
    build_persona_pages(out, _fd_site_led, _fd_site_book, shell=shell_root)
    build_persona_compare_page(out, _fd_site_led, _fd_site_book, shell=shell_root)

    # ---- Detached domain fronts: a config domain carrying a "page" object is
    # lifted off the home page onto its own top-level section of the site (e.g.
    # Ad Tech — docs/adtech.html), keeping the trade desk separate from the
    # liberal-arts library. Its category shelves render there instead of the
    # index; page.include moves the Fingerprint band with it and grants the desk
    # its own apparatus — a scoped Forecast board and Glossary — rendered as a
    # tools row. The home page gets a desk band pointing at the section instead.
    index_cards = [c for c in cards if c["category"] not in detached_cats]
    home_bands = []
    hub_desk = None      # first detached domain (e.g. Ad Tech) — the home page's mirrored twin pane
    hub_briefings = None  # that desk's Briefings rack, if it has one — feeds a bottom scroll row
    for d in detached:
        pcfg = d["page"]
        slug = pcfg.get("slug", "adtech")
        title = pcfg.get("title") or d.get("title", "The Desk")
        dcats = d.get("categories", [])
        dcards = [c for c in cards if c["category"] in dcats]
        dws = [w for w in wrapped_stats if w["category"] in dcats]
        dstats = (f"{len(dcards)} corpora · {sum(w['chapters'] for w in dws)} chapters · "
                  f"{round(sum(w['words'] for w in dws) / 1000)}k words")
        if hub_desk is None:
            hub_desk = {
                "href": f"{slug}.html",
                "kicker": pcfg.get("kicker", "A separate desk"),
                "title": title,
                "meta": f"{dstats} — plus the desk's own wire, board, and glossary",
                "cta": "Enter the desk →",
                "accent": pcfg.get("accent", ("#0d5b68", "#62aab8")),
                "cards": dcards,
                "slugs": [c["slug"] for c in dcards],
                "cats": dcats,
            }
        inc = pcfg.get("include") or []
        takes_fp = "fingerprint" in inc
        bands = fingerprint_band if takes_fp else ""
        if takes_fp:
            fingerprint_band = ""   # the wire band now lives on the desk, not the index
        # The desk's own apparatus, scoped to just its categories — same
        # canonical nav as every other page, underlining this desk's own link.
        desk_nav = main_nav_html(active=f"{slug}.html")
        desk_back = f'<a href="{slug}.html">← Back to the {html.escape(title)} desk</a>'
        tools = []
        if "forecast" in inc:
            dmarkets = [m for m in fd_markets if m["category"] in dcats]
            dnative = [f for f in forecast_items if f.get("category") in dcats]
            if dmarkets or dnative:
                fc_title = pcfg.get("forecast_title", f"The {title} Board")
                build_forecast_page(out, dnative, dmarkets, forecast_cfg, category_order=dcats,
                                    shell=shell_root, native_data=fd_native_data,
                                    page={"fname": f"{slug}-forecast.html", "title": fc_title,
                                          "kicker": "The desk's predictions, by category",
                                          "nav": desk_nav, "back": desk_back,
                                          "record_fname": f"{slug}-record.html",
                                          "accent": pcfg.get("accent")})
                n_out = sum(len(m["outcomes"]) for m in dmarkets)
                tools.append({"href": f"{slug}-forecast.html", "kicker": "The desk's forecaster",
                              "title": fc_title,
                              "meta": f"{len(dmarkets) + len(dnative)} markets · {n_out + len(dnative)} priced outcomes — "
                                      f"every one from the research",
                              "cta": "To the board →"})
                # The desk's own track record, scoped to its markets.
                rec_title = pcfg.get("record_title", f"The {title} Track Record")
                build_record_page(out, dnative, dmarkets, forecast_cfg, shell=shell_root,
                                  native_data=fd_native_data,
                                  page={"fname": f"{slug}-record.html", "title": rec_title,
                                        "kicker": "The desk, graded call by call",
                                        "nav": desk_nav, "back": desk_back,
                                        "board_href": f"{slug}-forecast.html",
                                        "board_title": fc_title,
                                        "accent": pcfg.get("accent")})
                dgraded = (sum(1 for f in dnative if f.get("_graded"))
                           + sum(1 for m in dmarkets if m.get("resolution")))
                dopen = len(dmarkets) + len(dnative) - dgraded
                tools.append({"href": f"{slug}-record.html", "kicker": "The desk's ledger",
                              "title": rec_title,
                              "meta": (f"{dgraded} graded · {dopen} open positions — "
                                       f"every call scored when it resolves"),
                              "cta": "See the record →"})
        if "glossary" in inc:
            dgloss = [g for g in glossary_index if g.get("category") in dcats]
            if dgloss:
                gl_title = pcfg.get("glossary_title", f"The {title} Glossary")
                gl_terms = len({(t.get("term") or "").strip().lower()
                                for g in dgloss for t in g["terms"]} - {""})
                build_glossary_page(out, dgloss, category_order=dcats, shell=shell_root,
                                    page={"fname": f"{slug}-glossary.html", "title": gl_title,
                                          "kicker": f"Reference · {title}",
                                          "scope": f"across the {title} desk",
                                          "nav": desk_nav, "back": desk_back,
                                          "accent": pcfg.get("accent")})
                tools.append({"href": f"{slug}-glossary.html", "kicker": "The desk's dictionary",
                              "title": gl_title,
                              "meta": f"{gl_terms} terms across {len(dgloss)} corpora, "
                                      f"defined in plain language",
                              "cta": "Look it up →"})
        if "briefings" in inc:
            # The desk's essay rack — its Pamphlets sibling. Renders (and bands)
            # even when empty, so the section exists before the first essay files.
            b_slug = pcfg.get("briefings_slug", "briefings")
            b_title = pcfg.get("briefings_title", "The Briefings")
            b_cfg = {
                "motto": pcfg.get("briefings_motto",
                                  "The desk's essays — filed one argument at a time."),
                "blurb": pcfg.get("briefings_blurb",
                                  f"Standalone essays on the programmatic and Connected-TV trade — "
                                  f"{b_title} is the {title} desk's own rack of arguments, "
                                  f"kept apart from the library's Pamphlets."),
            }
            b_items = read_pamphlets_manifest(out, subdir=b_slug)
            hub_briefings = {"items": b_items, "slug": b_slug, "title": b_title}
            build_pamphlets_page(out, b_items, b_cfg, shell=shell_root,
                                 page={"fname": f"{b_slug}.html", "title": b_title,
                                       "kicker": pcfg.get("briefings_kicker", f"Essays from the {title} desk"),
                                       "nav": desk_nav, "back": desk_back, "noun": "briefing", "subdir": b_slug,
                                       "empty": f"No briefings filed yet. Drop an essay into "
                                                f"docs/{b_slug}/data/ and list it in the manifest "
                                                f"to see it here.",
                                       "accent": pcfg.get("accent")})
            bands += pamphlets_band_html(b_items, b_cfg,
                                         page={"name": b_title, "flag": "The<br>Briefings",
                                               "href": f"{b_slug}.html", "cls": "brf"})
            b_rendered = sum(build_pamphlet(out, p, shell=shell_sub,
                                            page={"subdir": b_slug, "list_href": f"{b_slug}.html",
                                                  "list_name": b_title, "nav_active": f"{slug}.html",
                                                  "kicker": pcfg.get("briefings_kicker",
                                                                     f"Essays from the {title} desk"),
                                                  "noun": "briefing", "accent": pcfg.get("accent")})
                             for p in b_items)
            if b_items:
                print(f"  ✓ Rendered {b_rendered}/{len(b_items)} {b_title} essay page(s) from data")
        quiz_html = ""
        if "quiz" in inc:
            # The desk's own Test Yourself — the quiz engine scoped (via
            # #quiz-slugs) to just this desk's corpora, facts, and passages.
            dslugs = [m["slug"] for m in corpus_meta if m["category"] in dcats]
            dset = set(dslugs)
            dfacts = [f for f in quiz_facts if f["slug"] in dset]
            dpass = [p for p in all_passages if p["slug"] in dset]
            if dfacts:
                quiz_html = (
                    quiz_section_scoped(ns=f"{slug}:", all_label="Across the whole desk",
                                        all_name=f"the {title} desk")
                    + f'<script id="quiz-slugs" type="application/json">{json_for_html(dslugs)}</script>'
                    + f'<script id="quiz-data" type="application/json">{json_for_html(dfacts)}</script>'
                    + f'<script id="passages-data" type="application/json">{json_for_html(dpass)}</script>'
                )
        build_domain_page(out, pcfg, d, dcards, dcats, dstats, bands=bands,
                          tools=_desk_tools_html(tools), quiz=quiz_html, shell=shell_root)
        home_bands.append(domain_band_html(pcfg, d, len(dcards), fp_editions if takes_fp else None))
        print(f"  ✓ Detached desk: {title} — {dstats}"
              f"{' + board' if any(t['href'].endswith('-forecast.html') for t in tools) else ''}"
              f"{' + glossary' if any(t['href'].endswith('-glossary.html') for t in tools) else ''}"
              f" → {slug}.html")
    fingerprint_band += "".join(home_bands)

    index_ws = [w for w in wrapped_stats if w["category"] not in detached_cats]
    stats = (f"{len(index_cards)} corpora · {sum(w['chapters'] for w in index_ws)} chapters · "
             f"{round(sum(w['words'] for w in index_ws) / 1000)}k words")

    # The index carries only a quiz invitation band — the full quiz lives on
    # quiz.html and spans every shelf, desks included; the desks additionally
    # run their own scoped editions inline (quiz_section_scoped + #quiz-slugs).

    # Group the cards into category sections. Categories appear in the configured
    # category_order; any category not listed (and the "Other" catch-all) follows
    # in first-seen order so a corpus is never silently dropped from the library.
    # Detached domains' cards shelve on their own section front, not here.
    seen_order = []
    for card in index_cards:
        if card["category"] not in seen_order:
            seen_order.append(card["category"])

    # Top-level domains (config "domains") group the fine categories into the
    # three standing shelves — Politics & Economics, Ad Tech & Media, Philosophy/
    # Theology/Psychology. A category claimed by a domain renders under that
    # domain's super-heading; categories no domain claims (e.g. Heritage) render
    # ungrouped after the domains so a corpus is never silently dropped.
    cat_to_domain = {}
    for d in domains:
        if d.get("page"):
            continue  # detached domains front their own page, not an index shelf
        for c in d.get("categories", []):
            cat_to_domain.setdefault(c, d.get("title", ""))

    # Order categories domain-by-domain (config order), each domain's categories
    # in category_order, then any leftover categories in first-seen order. With no
    # domains configured this collapses to the prior category_order behavior.
    ordered_cats = []
    for d in domains:
        if d.get("page"):
            continue
        for c in d.get("categories", []):
            if c in seen_order and c not in ordered_cats:
                ordered_cats.append(c)
    for c in category_order:
        if c in seen_order and c not in ordered_cats:
            ordered_cats.append(c)
    for c in seen_order:
        if c not in ordered_cats:
            ordered_cats.append(c)

    pills = ['<button class="cat-pill active" data-cat="all">All</button>']
    sections = []
    cur_domain = "\x00"   # sentinel distinct from None (the "no domain" group)
    group_open = False
    for cat in ordered_cats:
        cat_cards = [c["html"] for c in index_cards if c["category"] == cat]
        dom = cat_to_domain.get(cat)
        if dom != cur_domain:
            if group_open:
                sections.append('</div>')
                group_open = False
            cur_domain = dom
            if dom:
                dom_count = sum(1 for c in index_cards if cat_to_domain.get(c["category"]) == dom)
                sections.append(
                    f'<div class="domain-group" data-domain="{html.escape(dom, quote=True)}">'
                    f'<h2 class="domain-heading">{html.escape(dom)}'
                    f'<span class="domain-count">{dom_count}</span></h2>'
                )
                group_open = True
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
    if group_open:
        sections.append('</div>')
    library_body = (
        '<div class="lib-pane">'
        '<div class="lib-toolbar">'
        '<input class="lib-search" type="search" '
        'placeholder="Search the library…" aria-label="Search the research library" autocomplete="off">'
        f'<div class="cat-pills">{"".join(pills)}</div>'
        '</div>'
        + "\n".join(sections)
        + '<p class="lib-empty" hidden>No corpora match your search.</p>'
        + '</div>'
    )

    # The Fingerprint + Pamphlets bands share the library CSS, so fold them in once.
    library_css = LIBRARY_CSS + SCENE_PLATE_CSS + HOME_MIRROR_CSS + FINGERPRINT_BAND_CSS + PAMPHLETS_BAND_CSS + FORECAST_BAND_CSS + OVERTURE_CSS + QUIZ_CSS
    # First-visit overture markup — built from the existing brand only (no new copy).
    # The Fingerprint bills under its desk's name once a detached domain takes it.
    # Every section name is a live link: The Research stays on this page (#library);
    # the rest jump straight to their section fronts.
    fp_moved = any("fingerprint" in ((d.get("page") or {}).get("include") or []) for d in domains)
    ov_links = ([("The Research", "research.html"), ("The Ghost of Times", "ghost.html")]
                + ([] if fp_moved else [("The Fingerprint", "fingerprint.html")])
                + [("The Pamphlets", "pamphlets.html"), ("The Forecast Desk", "forecast.html")]
                + [((d["page"].get("title") or d.get("title", "")),
                    f"{d['page'].get('slug', 'adtech')}.html") for d in detached])
    ov_sections = "".join(
        '<a href="%s">%s</a>' % (html.escape(href, quote=True), html.escape(name))
        for name, href in ov_links
    )
    overture_html = (
        '<div id="overture" role="dialog" aria-modal="true" aria-label="Welcome to the library"><div class="ov-inner">'
        '<div class="ov-tiles" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div>'
        '<p class="ov-brand">calvincollins · xyz</p>'
        f'<h1 class="ov-title">{html.escape(site_title)}</h1>'
        f'<p class="ov-sub">{html.escape(site_subtitle)}</p>'
        f'<div class="ov-sections">{ov_sections}</div>'
        '<button id="ov-enter" type="button">Enter the library →</button>'
        '<p class="ov-skip">Press Esc to skip</p>'
        '</div></div>'
    )
    collections_html = collections_section_html([m for _cc, m in resolved_collections])

    # The Forecast Desk ticker — the board's own ticker tape, surfaced at the
    # very top of the front page (replaces the old single-line teaser band).
    _ticker_tape = _fd_tape(forecast_items, fd_markets)
    ticker_html = (
        '<div class="home-ticker">'
        '<a class="home-ticker-label" href="forecast.html">The Forecast Desk →</a>'
        f'<div class="home-ticker-board">{_ticker_tape}</div></div>'
    ) if _ticker_tape else ""

    # The mirrored spread — Today's Passage side by side, then the Research
    # Library and the detached desk (Ad Tech) continuing as two parallel
    # scrollable shelves. A corpus belongs to exactly one side, so the
    # passages split is a clean complement.
    _adtech_slugs = set(hub_desk["slugs"]) if hub_desk else set()
    lib_passages = [p for p in all_passages if p["slug"] not in _adtech_slugs]
    adtech_passages = [p for p in all_passages if p["slug"] in _adtech_slugs]
    mirror_html = mirror_spread_html(library_body, stats, lib_passages, adtech_passages, hub_desk)
    # Bottom scrolls — Ghost of Times, the Fingerprint, Pamphlets, and (if the
    # desk has filed any) its Briefings rack, as light horizontal carousels at
    # the foot of the page rather than full shelves.
    ghost_cards = "".join(
        _scroll_card_html(_no_label(e), e.get("lead_headline") or f"Edition of {e.get('date', '')}",
                          (f"{_weekday(e.get('date', ''))} · {e.get('date', '')}"
                           if _weekday(e.get("date", "")) else e.get("date", "")),
                          _ed_href(e), scene_kind="ghost")
        for e in editions[:10]
    )
    ghost_row = bottom_scroll_row_html("The Ghost of Times", ghost_cfg.get("motto", ""), "ghost.html", ghost_cards)
    fp_cards = "".join(
        _scroll_card_html(_fp_no_label(e), e.get("lead_headline") or f"Edition of {e.get('date', '')}",
                          (f"{e.get('dispatches')} dispatches"
                           if isinstance(e.get("dispatches"), int)
                           else " · ".join(e.get("beats") or [])[:90]),
                          _fp_ed_href(e), scene_kind="fingerprint")
        for e in fp_editions[:10]
    )
    fp_row = bottom_scroll_row_html("The Fingerprint", fingerprint_cfg.get("motto", ""),
                                    "fingerprint.html", fp_cards, accent="#0d5b68")
    pam_cards = "".join(
        _scroll_card_html(p.get("subject") or _pamphlet_voice(p) or "Pamphlet",
                          p.get("title") or "Untitled pamphlet",
                          _pamphlet_meta(p) or _long_date(p.get("date", "")),
                          _pamphlet_href(p, "pamphlets"), scene_kind="pamphlet")
        for p in pamphlet_items[:10]
    )
    pam_row = bottom_scroll_row_html("The Pamphlets", pamphlets_cfg.get("motto", ""), "pamphlets.html", pam_cards)
    brief_row = ""
    if hub_briefings and hub_briefings["items"]:
        brief_cards = "".join(
            _scroll_card_html(p.get("subject") or _pamphlet_voice(p) or "Briefing",
                              p.get("title") or "Untitled briefing",
                              _pamphlet_meta(p) or _long_date(p.get("date", "")),
                              _pamphlet_href(p, hub_briefings["slug"]), scene_kind="briefing")
            for p in hub_briefings["items"][:10]
        )
        brief_row = bottom_scroll_row_html(hub_briefings["title"], "The Ad Tech desk's own essay rack",
                                           f'{hub_briefings["slug"]}.html', brief_cards, accent="#0d5b68")
    bottom_scrolls_html = ghost_row + fp_row + pam_row + brief_row

    (out / "index.html").write_text(LIBRARY_TEMPLATE.format(
        site_title=html.escape(site_title),
        site_subtitle=html.escape(site_subtitle),
        css=library_css,
        favicon=FAVICON, og_meta=OG_META,
        nav=main_nav_html(active="index.html"),
        top_header=top_header_art(),
        brand_logo=brand_logo_art(),
        stats=stats,
        hero=hero_art(),
        hero_cta=hero_cta_html(hub_desk),
        ticker=ticker_html,
        mirror=mirror_html,
        overture_head=OVERTURE_HEAD,
        overture=overture_html,
        collections=collections_html,
        quiz=QUIZ_CTA_HTML,
        bottom_scrolls=bottom_scrolls_html,
        theme_js=LIBRARY_THEME_JS + LIBRARY_FILTER_JS + DAILY_PASSAGE_JS + HOME_JS + OVERTURE_JS,
        shell=shell_root,
    ))
    research_quiz = (
        quiz_section_scoped(ns="research:", all_label="Across the whole library", all_name="the research library")
        + f'<script id="quiz-slugs" type="application/json">{json_for_html([m["slug"] for m in corpus_meta if m["category"] not in detached_cats])}</script>'
        + f'<script id="quiz-data" type="application/json">{json_for_html(quiz_facts)}</script>'
        + f'<script id="passages-data" type="application/json">{json_for_html(lib_passages)}</script>'
    )
    research_page_cfg = {
        "accent": ("#9a2c1a", "#d98055"),
        "epigraph": "“The medium is the message.” — Marshall McLuhan",
        "folio_left": "The library",
        "folio_right": "Corpora, collections, and reference",
        "kicker": "A library of deep research",
        "subtitle": site_subtitle,
        "title": site_title,
    }
    research_tools = [
        {"href": "connections.html", "kicker": "The map of ideas", "title": "Connections",
         "meta": "How the corpora relate across themes, writers, markets, and arguments",
         "cta": "Open the map ->"},
        {"href": "glossary.html", "kicker": "The reference shelf", "title": "Glossary",
         "meta": f"{sum(len({t['term'].lower() for t in g['terms']}) for g in glossary_index)} terms across the library, defined in plain language",
         "cta": "Look it up ->"},
        {"href": "quiz.html", "kicker": "The examination room", "title": "Test Yourself",
         "meta": "A whole-library quiz built from the research facts and passages",
         "cta": "Start the quiz ->"},
        {"href": "wrapped.html", "kicker": "Your reading record", "title": "Research Wrapped",
         "meta": "Your year in reading, computed privately from this browser",
         "cta": "See wrapped ->"},
    ]
    build_domain_page(out, {"slug": "research", **research_page_cfg}, {"title": site_title}, index_cards,
                      [c for c in category_order if c not in detached_cats], stats,
                      tools=_desk_tools_html(research_tools), quiz=research_quiz,
                      bottom_bands=collections_html, shell=shell_root)
    build_wrapped_page(out, wrapped_stats, shell=shell_root, category_order=category_order)
    print(f"\nBuilt {len(cards)} corpora + {len(editions)} ghost + {len(fp_editions)} fingerprint editions ({stats}) → {out}/index.html, {out}/research.html")


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
        "pamphlets": cfg.get("pamphlets", {}),
        "forecast": cfg.get("forecast", {}),
        "descriptions": cfg.get("descriptions", {}),
        "titles": cfg.get("titles", {}),
        "category_order": cfg.get("category_order", []),
        "domains": cfg.get("domains", []),
        "collections": cfg.get("collections", []),
        "atlas": cfg.get("atlas", {}),
    }


def deploy_to_site(out_dir, message=None):
    """Commit the built output and push it to the GitHub Pages branch so the live
    site (calvincollins.xyz) updates. Pages serves main:/docs and rebuilds
    on push, so a successful push IS the deploy. Safe no-op when nothing changed;
    never raises — reports and returns False on any failure so the build still 'succeeded'."""
    import datetime
    out_path = Path(out_dir).resolve()
    try:
        repo = subprocess.run(["git", "-C", str(out_path), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"  ! deploy skipped: {out_path} is not inside a git repository", file=sys.stderr)
        return False

    def git(*a, check=True):
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=check)

    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("  ✓ deploy: nothing changed since last publish — site already up to date")
        return True
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    msg = message or f"Rebuild research library — {datetime.datetime.now():%Y-%m-%d %H:%M}"
    try:
        git("commit", "-m", msg)
        print(f"  ✓ committed: {msg}")
    except subprocess.CalledProcessError as e:
        print(f"  ! deploy failed at commit:\n    {(e.stderr or e.stdout or '').strip()}", file=sys.stderr)
        return False
    # This repo has an external auto-committer / parallel sessions that move origin/main,
    # so rebase onto any remote advance before pushing rather than failing non-fast-forward.
    pull = git("pull", "--rebase", "origin", branch, check=False)
    if pull.returncode != 0:
        git("rebase", "--abort", check=False)
        print(f"  ! deploy: origin/{branch} diverged and auto-rebase hit a conflict — push skipped.",
              file=sys.stderr)
        print(f"    {(pull.stderr or '').strip()}", file=sys.stderr)
        print("    Your commit is saved locally. Resolve with `git pull --rebase`, then re-run --deploy.",
              file=sys.stderr)
        return False
    push = git("push", "origin", branch, check=False)
    if push.returncode != 0:
        print(f"  ! push failed (commit saved locally):\n    {(push.stderr or push.stdout or '').strip()}",
              file=sys.stderr)
        return False
    print(f"  ✓ pushed to origin/{branch} → GitHub Pages is rebuilding calvincollins.xyz (~1 min)")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="*", help="research corpus folders (optional if --config is given)")
    ap.add_argument("-c", "--config", help="path to build.config.json (title/subtitle/out/corpora/ghost)")
    ap.add_argument("-o", "--out", default=None, help="output directory (default: dist, or config's out)")
    ap.add_argument("--title", default=None, help="library page title")
    ap.add_argument("--subtitle", default=None, help="library page subtitle")
    ap.add_argument("--deploy", action="store_true",
                    help="after building, commit the output and push to the GitHub Pages "
                         "branch — publishes live to calvincollins.xyz")
    ap.add_argument("-m", "--message", default=None,
                    help="commit message for --deploy (default: a timestamped message)")
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
        pamphlets_cfg = cfg["pamphlets"]
        forecast_cfg = cfg["forecast"]
        descriptions = cfg["descriptions"]
        titles = cfg["titles"]
        category_order = cfg["category_order"]
        domains = cfg["domains"]
        collections = cfg["collections"]
        atlas_cfg = cfg["atlas"]
    elif args.folders:
        folders = args.folders
        out = args.out or "dist"
        title = args.title or "Research Library"
        subtitle = args.subtitle or "Deep-research corpora, readable and searchable."
        ghost_cfg = {}
        fingerprint_cfg = {}
        pamphlets_cfg = {}
        forecast_cfg = {}
        descriptions = {}
        titles = {}
        category_order = []
        domains = []
        collections = []
        atlas_cfg = {}
    else:
        ap.error("no corpus folders and no --config / build.config.json found")

    build(folders, out, title, subtitle, ghost_cfg=ghost_cfg, descriptions=descriptions,
          fingerprint_cfg=fingerprint_cfg, pamphlets_cfg=pamphlets_cfg, forecast_cfg=forecast_cfg,
          titles=titles, category_order=category_order,
          domains=domains, collections=collections, atlas_cfg=atlas_cfg)

    if args.deploy:
        deploy_to_site(out, message=args.message)
