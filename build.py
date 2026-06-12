#!/usr/bin/env python3
"""Build an interactive, shareable website from deep-research corpus folders.

Usage:
    python3 build.py <research-folder> [<research-folder> ...] [-o dist]

Each folder is either:
  - a corpus with a manifest.json (title/subtitle/documents), or
  - a folder of numbered markdown chapters (00_*.md, 01_*.md, ...) — metadata
    is inferred from frontmatter / first headings.

Output: a static site in dist/ — index.html (library) plus one self-contained
HTML reader per corpus. No server or internet needed to read it; share the
folder, or deploy it to GitHub Pages / Netlify for a URL.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKED_JS = (HERE / "vendor" / "marked.min.js").read_text()


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


# ---------------------------------------------------------------- templates

READER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<button id="menu-btn" title="Chapters">☰</button>
<aside id="sidebar">
  <a class="back" href="index.html">← Library</a>
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
  --serif: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
  --sans: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
}
[data-theme="dark"] {
  --bg: #161513; --panel: #1f1d1a; --text: #e8e4db; --muted: #968f82;
  --accent: #d98f5f; --border: #35322c; --mark: #5c4a1e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--serif); }
#sidebar {
  position: fixed; top: 0; left: 0; bottom: 0; width: 320px; overflow-y: auto;
  background: var(--panel); border-right: 1px solid var(--border); padding: 1.2rem 1.2rem 2rem;
}
#sidebar h1 { font-size: 1.15rem; line-height: 1.3; margin: .8rem 0 .2rem; }
#sidebar .subtitle { font-size: .8rem; color: var(--muted); margin: 0 0 1rem; font-family: var(--sans); }
.back { font-family: var(--sans); font-size: .78rem; color: var(--muted); text-decoration: none; }
.back:hover { color: var(--accent); }
#search { width: 100%; padding: .45rem .6rem; font-size: .85rem; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--text); font-family: var(--sans); }
#toc { margin-top: .8rem; }
#toc a { display: block; padding: .45rem .55rem; margin: .1rem 0; border-radius: 6px;
  color: var(--text); text-decoration: none; font-family: var(--sans); font-size: .84rem; line-height: 1.35; }
#toc a .num { color: var(--muted); font-size: .72rem; margin-right: .4rem; }
#toc a:hover { background: var(--bg); }
#toc a.active { background: var(--bg); color: var(--accent); font-weight: 600; }
#search-results { font-family: var(--sans); font-size: .8rem; }
#search-results .hit { padding: .5rem .55rem; border-bottom: 1px solid var(--border); cursor: pointer; border-radius: 6px; }
#search-results .hit:hover { background: var(--bg); }
#search-results .hit b { color: var(--accent); display: block; margin-bottom: .15rem; }
#search-results mark { background: var(--mark); color: inherit; border-radius: 2px; }
#search-results .none { color: var(--muted); padding: .5rem .55rem; }
#theme-btn { margin-top: 1.2rem; font-family: var(--sans); font-size: .78rem; color: var(--muted);
  background: none; border: 1px solid var(--border); border-radius: 6px; padding: .35rem .7rem; cursor: pointer; }
#main { margin-left: 320px; display: flex; flex-direction: column; min-height: 100vh; }
#content { max-width: 720px; width: 100%; margin: 0 auto; padding: 3rem 2rem 2rem;
  font-size: 1.04rem; line-height: 1.72; flex: 1; }
#content h1 { font-size: 1.9rem; line-height: 1.25; margin-top: 0; }
#content h2 { font-size: 1.35rem; margin-top: 2.2rem; }
#content h3 { font-size: 1.1rem; }
#content a { color: var(--accent); }
#content blockquote { margin: 1.2rem 0; padding: .2rem 1.2rem; border-left: 3px solid var(--accent);
  color: var(--muted); font-style: italic; }
#content code { background: var(--panel); padding: .1em .35em; border-radius: 4px; font-size: .88em; }
#content pre { background: var(--panel); padding: 1rem; border-radius: 8px; overflow-x: auto; }
#content pre code { background: none; padding: 0; }
#content table { border-collapse: collapse; font-family: var(--sans); font-size: .85rem; width: 100%; margin: 1.2rem 0; }
#content th, #content td { border: 1px solid var(--border); padding: .45rem .6rem; text-align: left; vertical-align: top; }
#content th { background: var(--panel); }
#content img { max-width: 100%; }
#content hr { border: none; border-top: 1px solid var(--border); margin: 2rem 0; }
#pager { max-width: 720px; width: 100%; margin: 0 auto; padding: 1rem 2rem 3rem;
  display: flex; align-items: center; justify-content: space-between; font-family: var(--sans); }
#pager button { font-size: 1rem; padding: .4rem 1rem; border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); color: var(--text); cursor: pointer; }
#pager button:disabled { opacity: .3; cursor: default; }
#pager-label { font-size: .78rem; color: var(--muted); }
#menu-btn { display: none; position: fixed; top: .7rem; left: .7rem; z-index: 20; font-size: 1.1rem;
  background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 8px;
  padding: .3rem .6rem; cursor: pointer; }
@media (max-width: 860px) {
  #sidebar { transform: translateX(-100%); transition: transform .2s; z-index: 10; width: 300px; }
  body.menu-open #sidebar { transform: none; box-shadow: 0 0 40px rgba(0,0,0,.3); }
  #menu-btn { display: block; }
  #main { margin-left: 0; }
  #content { padding-top: 3.6rem; }
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
<style>{css}</style>
</head>
<body>
<header>
  <h1>{site_title}</h1>
  <p>{site_subtitle}</p>
</header>
<main class="grid">
{cards}
</main>
<script>
const pref = localStorage.getItem('corpus-theme');
if (pref) document.documentElement.dataset.theme = pref;
else if (matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.dataset.theme = 'dark';
</script>
</body>
</html>
"""

LIBRARY_CSS = """
:root {
  --bg: #faf8f4; --panel: #f1ede5; --text: #1f1d1a; --muted: #6e6a62;
  --accent: #8c4a2f; --border: #ddd6c9;
}
[data-theme="dark"] {
  --bg: #161513; --panel: #1f1d1a; --text: #e8e4db; --muted: #968f82;
  --accent: #d98f5f; --border: #35322c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: Georgia, 'Iowan Old Style', serif; }
header { max-width: 1080px; margin: 0 auto; padding: 3.5rem 2rem 1rem; }
header h1 { font-size: 2.2rem; margin: 0 0 .3rem; }
header p { color: var(--muted); margin: 0; font-family: -apple-system, 'Segoe UI', sans-serif; font-size: .9rem; }
.grid { max-width: 1080px; margin: 0 auto; padding: 1.5rem 2rem 4rem;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.2rem; }
.card { display: block; background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.3rem 1.4rem; text-decoration: none; color: var(--text); transition: transform .12s, box-shadow .12s; }
.card:hover { transform: translateY(-2px); box-shadow: 0 6px 24px rgba(0,0,0,.08); }
.card h2 { font-size: 1.12rem; line-height: 1.3; margin: 0 0 .35rem; }
.card .sub { color: var(--muted); font-size: .8rem; font-family: -apple-system, 'Segoe UI', sans-serif;
  margin: 0 0 .8rem; line-height: 1.4; }
.card .meta { color: var(--accent); font-size: .74rem; font-family: -apple-system, 'Segoe UI', sans-serif;
  text-transform: uppercase; letter-spacing: .06em; }
"""


# ---------------------------------------------------------------- build

def json_for_html(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(folders, out_dir, site_title, site_subtitle):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cards = []

    for folder in folders:
        corpus = load_corpus(folder)
        if not corpus["documents"]:
            print(f"  ! {folder}: no chapters found, skipped", file=sys.stderr)
            continue
        page = READER_TEMPLATE.format(
            title=html.escape(corpus["title"]),
            subtitle=html.escape(corpus["subtitle"]),
            css=CSS,
            data_json=json_for_html(corpus),
            marked_js=MARKED_JS,
            app_js=APP_JS,
        )
        (out / f"{corpus['slug']}.html").write_text(page)
        n = len(corpus["documents"])
        meta_bits = [f"{n} chapters"]
        if corpus["generated"]:
            meta_bits.append(corpus["generated"])
        cards.append(
            f'<a class="card" href="{corpus["slug"]}.html">'
            f'<h2>{html.escape(corpus["title"])}</h2>'
            f'<p class="sub">{html.escape(corpus["subtitle"] or "")}</p>'
            f'<p class="meta">{" · ".join(meta_bits)}</p></a>'
        )
        print(f"  ✓ {corpus['title']}  ({n} chapters)")

    (out / "index.html").write_text(LIBRARY_TEMPLATE.format(
        site_title=html.escape(site_title),
        site_subtitle=html.escape(site_subtitle),
        css=LIBRARY_CSS,
        cards="\n".join(cards),
    ))
    print(f"\nBuilt {len(cards)} corpora → {out}/index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="+", help="research corpus folders")
    ap.add_argument("-o", "--out", default="dist", help="output directory (default: dist)")
    ap.add_argument("--title", default="Research Library", help="library page title")
    ap.add_argument("--subtitle", default="Deep-research corpora, readable and searchable.",
                    help="library page subtitle")
    args = ap.parse_args()
    build(args.folders, args.out, args.title, args.subtitle)
