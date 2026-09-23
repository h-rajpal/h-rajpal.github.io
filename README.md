# Personal Academic Website

Built upon a retro-looking [Hugo](https://gohugo.io/) theme inspired by
[gruvbox](https://github.com/schnerring/hugo-theme-gruvbox), developed with lots of love by [Michael Schnerring](https://github.com/schnerring).

## Publications

All publications live in [`data/publications.json`](data/publications.json). The
Publications page, the home page carousel, the research topic pages and the CV
are all generated from it, newest first.

**Automatic updates.** Every Monday the
[Update publications](.github/workflows/update-publications.yml) workflow checks
Google Scholar (falling back to OpenAlex if Scholar blocks it) and opens a pull
request with any new papers, plus preprints that have since been published. To
publish: set `research_topics`, check the thumbnail, and merge. It can also be
run by hand from the Actions tab, or locally:

```sh
python3 scripts/update_publications.py --dry-run   # see what would change
python3 scripts/update_publications.py             # update the data file
```

Items that should never be listed (abstracts, theses, ...) go under `ignore` in
[`scripts/publications_config.json`](scripts/publications_config.json).

**Images and PDFs** are found by the publication's `id`:

- thumbnail: `assets/images/publications/<id>.png` (any image format)
- PDF: `content/publications/pdfs/<id>.pdf`, or set `pdf_path` to a URL

Images must be stored in the repository. The build fails on image links to
other websites, because publisher links expire.

Research topic images go in `assets/images/research/<page-name>.png`, or set
`image:` in the page's front matter to a path under `assets/images/`.

## Analytics

Visits are counted with [Umami Cloud](https://cloud.umami.is) (no cookies). The
script address and website id are under `[params.umami]` in
`config/_default/config.toml`, and only production builds include the script,
so local previews with `hugo server` are not counted.

## Checks

Every build runs [`scripts/check_links.py`](scripts/check_links.py), which fails
if any page links to a page, image or PDF on the site that does not exist.
