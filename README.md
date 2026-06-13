# Corpus App

Turns deep-research corpus folders into an interactive, shareable static website:
a library home page plus a reader per corpus with chapter navigation, full-text
search, dark mode, and saved reading progress.

The site has two sections, both linked from the home page:

- **The Research Library** — the grid of corpus readers (`docs/index.html`).
- **The Ghost of Times** — a daily paper of writer-voiced op-eds, with its own
  index at `docs/ghost.html`. Editions are authored by the `ghost_of_times`
  skill and published with its `publish_to_site.py` (see below).

## Build

The simplest build reads `build.config.json` (title, subtitle, output dir, the
corpus list, and Ghost section settings) — no arguments needed:

```bash
python3 corpus-app/build.py            # uses corpus-app/build.config.json
```

You can still pass folders explicitly (this overrides the config's corpus list):

```bash
python3 corpus-app/build.py jung-research uap-research ... -o corpus-app/docs \
  --title "Research Library"
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

## The Ghost of Times section

`build.py` also generates `docs/ghost.html` from `docs/ghost/manifest.json`. Each
edition is a self-contained HTML file in `docs/ghost/`; the manifest carries the
date, edition number, lead headline/dek/writer, and writer roster. The home page
shows a feature band for the most recent edition.

You don't edit the manifest by hand — the `ghost_of_times` skill publishes an
edition (and rebuilds the site) with:

```bash
python3 ghost_of_times/scripts/publish_to_site.py --date YYYY-MM-DD          # local
python3 ghost_of_times/scripts/publish_to_site.py --date YYYY-MM-DD --push   # deploy
```

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
