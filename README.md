# Corpus App

Turns deep-research corpus folders into an interactive, shareable static website:
a library home page plus a reader per corpus with chapter navigation, full-text
search, dark mode, and saved reading progress.

## Build

```bash
python3 corpus-app/build.py jung-research uap-research ... -o corpus-app/docs \
  --title "Calvin's Research Library"
```

Each input folder can be:
- a corpus with a `manifest.json` — both the narrative-corpus schema
  (`title`/`documents`) and the deep-research skill schema
  (`topic`/`documents_index`) are supported, or
- a plain folder of numbered chapters (`00_*.md`, `01_*.md`, …) — titles and
  summaries are inferred from frontmatter and headings.

Output is plain HTML in `docs/`. Every reader page is fully self-contained
(markdown renderer embedded) — no server, build tools, or internet required
to read.

## Preview locally

```bash
python3 -m http.server 8642 --directory corpus-app/docs
# open http://localhost:8642
```

(Or just double-click `docs/index.html` — everything works from file:// too.)

## Share it

- **Netlify Drop** (easiest): drag the `docs/` folder onto https://app.netlify.com/drop — instant URL.
- **GitHub Pages**: push `docs/` to a repo, enable Pages in settings.
- **Direct**: zip `docs/` and send it, or send a single corpus `.html` file — each one stands alone.

## Reader features

- Chapter sidebar with active-chapter highlight, summaries on hover
- Full-text search across the whole corpus with snippet previews; clicking a
  result jumps to the chapter and scrolls to the match
- Prev/next buttons and ←/→ keyboard navigation
- Cross-chapter `.md` links rewritten to in-app navigation
- Dark/light theme (follows system, manual toggle persisted)
- Reading progress remembered per corpus (localStorage)
- Mobile-friendly (collapsible sidebar)
