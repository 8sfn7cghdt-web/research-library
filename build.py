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
<button id="menu-btn" title="Chapters">☰</button>
<aside id="sidebar">
  <a class="back" href="index.html">← Library</a>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <h1>{title}</h1>
  <p class="subtitle">{subtitle}</p>
  <input id="search" type="search" placeholder="Search the corpus…" autocomplete="off">
  <div id="search-results"></div>
  <nav id="toc"></nav>
  <button id="theme-btn">◐ Theme</button>
</aside>
<main id="main">
  <article id="content"></article>
  <div id="pager">
    <button id="prev">←</button>
    <span id="pager-label"></span>
    <button id="next">→</button>
  </div>
</main>
<script id="corpus-data" type="application/json">{data_json}</script>
<script>{marked_js}</script>
<script>{app_js}</script>
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
#theme-btn { margin-top: 1.2rem; font-family: var(--sans); font-size: .78rem; color: var(--muted);
  background: none; border: 1px solid var(--border); border-radius: 10px; padding: .35rem .7rem; cursor: pointer; }
#theme-btn:hover { color: var(--accent); border-color: var(--accent); }
#main { margin-left: 320px; display: flex; flex-direction: column; min-height: 100vh; }
#content { max-width: 740px; width: 100%; margin: 0 auto; padding: 3rem 2rem 2rem;
  font-size: 1.04rem; line-height: 1.72; flex: 1; }
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

function show(i, anchorText) {
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
  document.getElementById('pager-label').textContent = (current + 1) + ' / ' + docs.length + ' · ' + docs[current].title;
  history.replaceState(null, '', '#ch-' + current);
  localStorage.setItem(key, current);
  if (anchorText) {
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    let node; while ((node = walker.nextNode())) {
      const idx = node.textContent.toLowerCase().indexOf(anchorText.toLowerCase());
      if (idx >= 0) { node.parentElement.scrollIntoView({ block: 'center' }); return; }
    }
  }
  document.getElementById('main').scrollIntoView();
  window.scrollTo(0, 0);
}

document.getElementById('prev').onclick = () => show(current - 1);
document.getElementById('next').onclick = () => show(current + 1);
document.addEventListener('keydown', (e) => {
  if (e.target === searchBox) return;
  if (e.key === 'ArrowLeft') show(current - 1);
  if (e.key === 'ArrowRight') show(current + 1);
});

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

// initial chapter: hash > saved progress > 0
const hash = location.hash.match(/^#ch-(\d+)$/);
show(hash ? +hash[1] : +(localStorage.getItem(key) || 0));
"""

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
</head>
<body>
<div class="masthead">
  <span class="mh-brand">research · calvincollins · xyz</span>
  <nav class="mh-nav">
    <a href="ghost.html">The Ghost of Times</a>
    <a href="fingerprint.html">The Fingerprint</a>
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
{ghost_band}
{fingerprint_band}
<h2 class="section-title" id="library">The Research Library</h2>
<main class="library">
{cards}
</main>
<footer>
  <div class="tiles" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  <p class="epigraph">“The medium is the message.” — Marshall McLuhan</p>
  <p class="colophon">Every corpus reads anywhere — no server, no tracking, light or dark.</p>
</footer>
<script>{theme_js}</script>
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
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.card:hover { transform: translateY(-3px); box-shadow: 0 10px 30px rgba(0,0,0,.1); border-color: var(--accent); }
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


def build_ghost_page(out_dir, editions, ghost_cfg):
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


def build_ghost_edition(out_dir, ed):
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


def build_fingerprint_page(out_dir, editions, cfg):
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


def build_fingerprint_edition(out_dir, ed):
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
    )
    (out / "fingerprint" / f"{date}-fingerprint.html").write_text(page)
    return True


# ---------------------------------------------------------------- build

def json_for_html(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(folders, out_dir, site_title, site_subtitle, ghost_cfg=None, descriptions=None,
          fingerprint_cfg=None, titles=None, category_order=None):
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
        page = READER_TEMPLATE.format(
            title=html.escape(corpus["title"]),
            subtitle=html.escape(corpus["subtitle"]),
            css=CSS,
            theme_style=render_theme_style(theme),
            favicon=FAVICON, og_meta=reader_og,
            data_json=json_for_html(corpus),
            marked_js=MARKED_JS,
            app_js=APP_JS,
        )
        (out / f"{corpus['slug']}.html").write_text(page)
        n = len(corpus["documents"])
        words = sum(len(d["body"].split()) for d in corpus["documents"])
        total_chapters += n
        total_words += words
        meta_bits = [f"Nº {n_corpus:02d}", f"{n} chapters"]
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
        card_html = (
            f'<a class="card" href="{corpus["slug"]}.html" data-cat="{html.escape(category, quote=True)}" '
            f'data-search="{search_text}">'
            f'<div class="cover">{card_cover(corpus["slug"], corpus["title"], theme_cover_palette(theme))}</div>'
            f'<div class="card-body"><h2>{html.escape(corpus["title"])}</h2>'
            f'<p class="sub">{html.escape(card_sub)}</p>'
            f'<p class="meta">{" · ".join(meta_bits)}</p></div></a>'
        )
        cards.append({"category": category, "html": card_html})
        fig_note = f", {figs} figures" if figs else ""
        print(f"  ✓ {corpus['title']}  ({n} chapters{fig_note})")

    # The Ghost of Times section (second top-level section of the site).
    editions = read_ghost_manifest(out) if ghost_cfg.get("enabled", True) else []
    build_ghost_page(out, editions, ghost_cfg)
    ghost_band = ghost_band_html(editions, ghost_cfg)
    # Render each edition page natively from its deposited data (skips any that
    # predate the data-driven renderer and so have no docs/ghost/data/*.json).
    rendered = sum(build_ghost_edition(out, ed) for ed in editions)
    if editions:
        print(f"  ✓ Rendered {rendered}/{len(editions)} edition page(s) from data")

    # The Fingerprint section (third top-level section of the site).
    fp_editions = read_fingerprint_manifest(out) if fingerprint_cfg.get("enabled", True) else []
    build_fingerprint_page(out, fp_editions, fingerprint_cfg)
    fingerprint_band = fingerprint_band_html(fp_editions, fingerprint_cfg)
    fp_rendered = sum(build_fingerprint_edition(out, ed) for ed in fp_editions)
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
    library_css = LIBRARY_CSS + FINGERPRINT_BAND_CSS
    (out / "index.html").write_text(LIBRARY_TEMPLATE.format(
        site_title=html.escape(site_title),
        site_subtitle=html.escape(site_subtitle),
        css=library_css,
        favicon=FAVICON, og_meta=OG_META,
        stats=stats,
        hero=hero_art(),
        ghost_band=ghost_band,
        fingerprint_band=fingerprint_band,
        cards=library_body,
        theme_js=LIBRARY_THEME_JS + LIBRARY_FILTER_JS,
    ))
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
    else:
        ap.error("no corpus folders and no --config / build.config.json found")

    build(folders, out, title, subtitle, ghost_cfg=ghost_cfg, descriptions=descriptions,
          fingerprint_cfg=fingerprint_cfg, titles=titles, category_order=category_order)
