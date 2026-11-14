# Third-party notices

Folio 译坊 bundles the following third-party components for offline, local-first use.

## KaTeX

- Version: 0.16.11
- License: MIT
- Files: `app/static/vendor/katex/`
- Upstream: https://github.com/KaTeX/KaTeX
- License text: `app/static/vendor/katex/LICENSE`

## Fandol fonts

- Files: `app/static/fonts/FandolSong-Regular.otf`, `FandolHei-Regular.otf`
- License: GPL with font embedding exception
- Upstream: https://www.ctan.org/pkg/fandol
- Used for consistent Chinese typography in the web UI and PDF export.

## DjVuLibre

- Version: 3.5.29 (Ubuntu package build)
- License: GPL-2.0-or-later
- Files: `vendor/djvulibre/` (`ddjvu`, `djvutxt`, shared library)
- Upstream: https://djvu.sourceforge.net/
- Copyright and packaging details: `vendor/djvulibre/usr/share/doc/*/copyright`

## Python packages

FastAPI, Uvicorn, httpx, PyMuPDF, python-docx, ebooklib, BeautifulSoup, lxml,
latex2mathml, mathml2omml and their transitive dependencies retain their own
licenses. See the package metadata in your environment.

## Demo content

Screenshots and examples contain short excerpts from the user's own reference
books. They are included only to demonstrate the UI and are not relicensed by
this repository.
