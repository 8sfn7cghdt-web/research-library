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
import hashlib
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKED_JS = (HERE / "vendor" / "marked.min.js").read_text()

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect x='2' y='2' width='28' height='28' rx='7' fill='%23b3502f'/>"
           "<rect x='34' y='2' width='28' height='28' rx='7' fill='%23c9a227'/>"
           "<rect x='2' y='34' width='28' height='28' rx='7' fill='%232e5266'/>"
           "<rect x='34' y='34' width='28' height='28' rx='7' fill='%236b7d4f'/></svg>")

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


def cover_svg(slug):
    seed = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16)
    r = _rng(seed)
    pal = COVER_PALETTES[(seed >> 3) % 4]
    variant = seed % 4
    parts = [_cover_rose, _cover_arcade, _cover_shards, _cover_hills][variant](r, pal)
    return ("<svg viewBox='0 0 320 140' preserveAspectRatio='xMidYMid slice' "
            "xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>" + "".join(parts) + "</svg>")


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


# ---------------------------------------------------------------- templates

READER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="{favicon}">
<style>{css}</style>
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
#content hr::before { content: "◆ ◆ ◆"; color: var(--accent); opacity: .45; font-size: .7rem; letter-spacing: 1.1em;
  padding-left: 1.1em; }
figure.corpus-fig { margin: 2.2rem 0; padding: 1.1rem 1.1rem .9rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: 16px; }
figure.corpus-fig svg { width: 100%; height: auto; display: block; }
figure.corpus-fig figcaption { font-family: var(--sans); font-size: .78rem; color: var(--muted);
  text-align: center; margin-top: .7rem; line-height: 1.45; }
figure.corpus-fig figcaption strong { color: var(--accent); }
.corpus-fig svg text { font-family: var(--sans); fill: var(--text); }
.corpus-fig svg .t-muted { fill: var(--muted); }
.corpus-fig svg .t-acc { fill: var(--accent); }
.corpus-fig svg .t-serif { font-family: var(--display); }
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
<style>{css}</style>
</head>
<body>
<div class="masthead">
  <span>research · calvincollins · xyz</span>
  <span>est. may 2026</span>
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
<main class="grid">
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
  justify-content: space-between; font-family: var(--sans); font-size: .68rem; color: var(--muted);
  text-transform: uppercase; letter-spacing: .14em; border-bottom: 1px solid var(--border); }
header { max-width: 1080px; margin: 0 auto; padding: 2.6rem 2rem 1rem; display: flex;
  align-items: center; gap: 2.5rem; }
.hero-text { flex: 1.1; }
.hero-art { flex: 1; min-width: 0; }
.hero-art svg { width: 100%; height: auto; display: block; }
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
.grid { max-width: 1080px; margin: 0 auto; padding: 1.8rem 2rem 3rem;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.4rem; }
.card { display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden; text-decoration: none; color: var(--text);
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.card:hover { transform: translateY(-3px); box-shadow: 0 10px 30px rgba(0,0,0,.1); border-color: var(--accent); }
.cover { background: var(--cover-bg); border-bottom: 1px solid var(--border); }
.cover svg { width: 100%; height: 124px; display: block; }
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


# ---------------------------------------------------------------- build

def json_for_html(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(folders, out_dir, site_title, site_subtitle):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    total_chapters = 0
    total_words = 0

    for n_corpus, folder in enumerate(folders, 1):
        corpus = load_corpus(folder)
        if not corpus["documents"]:
            print(f"  ! {folder}: no chapters found, skipped", file=sys.stderr)
            continue
        figs = inject_figures(corpus, folder)
        page = READER_TEMPLATE.format(
            title=html.escape(corpus["title"]),
            subtitle=html.escape(corpus["subtitle"]),
            css=CSS,
            favicon=FAVICON,
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
        cards.append(
            f'<a class="card" href="{corpus["slug"]}.html">'
            f'<div class="cover">{cover_svg(corpus["slug"])}</div>'
            f'<div class="card-body"><h2>{html.escape(corpus["title"])}</h2>'
            f'<p class="sub">{html.escape(corpus["subtitle"] or "")}</p>'
            f'<p class="meta">{" · ".join(meta_bits)}</p></div></a>'
        )
        fig_note = f", {figs} figures" if figs else ""
        print(f"  ✓ {corpus['title']}  ({n} chapters{fig_note})")

    stats = f"{len(cards)} corpora · {total_chapters} chapters · {round(total_words / 1000)}k words"
    (out / "index.html").write_text(LIBRARY_TEMPLATE.format(
        site_title=html.escape(site_title),
        site_subtitle=html.escape(site_subtitle),
        css=LIBRARY_CSS,
        favicon=FAVICON,
        stats=stats,
        hero=hero_svg(),
        cards="\n".join(cards),
        theme_js=LIBRARY_THEME_JS,
    ))
    print(f"\nBuilt {len(cards)} corpora ({stats}) → {out}/index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="+", help="research corpus folders")
    ap.add_argument("-o", "--out", default="dist", help="output directory (default: dist)")
    ap.add_argument("--title", default="Research Library", help="library page title")
    ap.add_argument("--subtitle", default="Deep-research corpora, readable and searchable.",
                    help="library page subtitle")
    args = ap.parse_args()
    build(args.folders, args.out, args.title, args.subtitle)
