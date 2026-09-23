#!/usr/bin/env python3
"""Keep data/publications.json in sync with Google Scholar.

What it does, in order:
  1. Reads the publication list from the Google Scholar profile. If Scholar
     blocks the request (it often does from cloud servers), it falls back to
     OpenAlex, looked up by ORCID.
  2. Matches each item to an existing entry (by Scholar id, arXiv/bioRxiv id,
     DOI or title). Unmatched items become new entries, with full metadata
     from arXiv, bioRxiv, Crossref and OpenAlex.
  3. Checks every preprint for a published version (a DOI announced on arXiv,
     bioRxiv's "published" field, or a Crossref title search). When one is
     found, the entry switches to the journal version and keeps a link to the
     preprint. Preprints that are not yet published get their title and
     author list refreshed from the latest version.
  4. Saves a first thumbnail for new arXiv papers (the first figure of the
     arXiv HTML version) under assets/images/publications/.
  5. Writes the file sorted by date and prints a Markdown report, which the
     update-publications workflow uses as the pull request description.

Hand-curated fields (research_topics, image, pdf_path, keywords) are never
overwritten. Items listed under "ignore" in scripts/publications_config.json
are skipped. Only the Python standard library is used.

  python3 scripts/update_publications.py              # update the data file
  python3 scripts/update_publications.py --dry-run    # report only
  python3 scripts/update_publications.py --source openalex
"""
from __future__ import annotations

import argparse
import difflib
import functools
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "publications.json"
IMAGE_DIR = ROOT / "assets" / "images" / "publications"
CONFIG_FILE = Path(__file__).resolve().parent / "publications_config.json"

BOT_UA = "h-rajpal.github.io publications updater (+https://github.com/h-rajpal/h-rajpal.github.io)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

PREPRINT_SERVERS = (
    "arxiv", "biorxiv", "medrxiv", "psyarxiv", "osf", "ssrn",
    "research square", "preprints.org", "chemrxiv", "repec",
)
TITLE_MATCH = 0.9  # difflib ratio between normalised titles
STOPWORDS = {
    "a", "an", "the", "on", "of", "in", "for", "and", "to", "as", "by", "with",
    "from", "towards", "toward", "is", "are", "how", "what", "who", "why", "when",
    "beyond", "into", "via", "do", "does",
}
# Key order for new entries (existing entries keep their order).
FIELD_ORDER = [
    "id", "title", "authors", "abstract", "date", "year", "type", "journal_name",
    "journal_url", "doi", "volume", "issue", "pages", "pdf_path", "preprint_url",
    "research_topics", "scholar_ids",
]

ARXIV_RE = re.compile(r"(?:arxiv[:.]\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})", re.I)
BIORXIV_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2}\.\d{6})")
BIORXIV_HINT = re.compile(r"biorxiv|medrxiv|10\.1101/|10\.64898/", re.I)


class Blocked(Exception):
    """A publication-list source refused the request or returned no data."""


class Report:
    def __init__(self) -> None:
        self.source = ""
        self.new: list[tuple[dict, bool]] = []  # (entry, topics_guessed)
        self.thumbnails: dict[str, str] = {}  # entry id -> saved image path
        self.upgraded: list[tuple[dict, str]] = []  # (entry, previous venue)
        self.refreshed: list[tuple[dict, list[str]]] = []
        self.attention: list[str] = []
        self.skipped: list[str] = []
        self.bookkeeping = False  # file changed, but nothing a visitor would notice

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)
        self.attention.append(message)

    @property
    def changed(self) -> bool:
        return bool(self.new or self.upgraded or self.refreshed)

    def markdown(self) -> str:
        out = ["## Publications update", "", f"Source: {self.source}", ""]
        if self.new:
            out += [f"### New publications ({len(self.new)})", ""]
            for entry, guessed in self.new:
                out.append(f"**{entry['title']}**  ")
                out.append(f"{entry.get('journal_name', '?')}, {entry.get('year', '?')} · {entry.get('journal_url', '')}")
                if entry.get("research_topics"):
                    note = " (guessed from the title and abstract, please check)" if guessed else ""
                    out.append(f"- Topic: {', '.join(entry['research_topics'])}{note}")
                else:
                    out.append("- Topic: none guessed. Set `research_topics` in `data/publications.json`")
                if entry["id"] in self.thumbnails:
                    out.append(f"- Thumbnail: saved the first figure of the arXiv HTML version as "
                               f"`{self.thumbnails[entry['id']]}`. Replace it if another figure works better")
                else:
                    out.append(f"- Thumbnail: add `assets/images/publications/{entry['id']}.png` (any image format)")
                if not entry.get("pdf_path"):
                    out.append(f"- PDF (optional): add `content/publications/pdfs/{entry['id']}.pdf`")
                out.append("")
        if self.upgraded:
            out += [f"### Preprints now published ({len(self.upgraded)})", ""]
            for entry, before in self.upgraded:
                out.append(f"- **{entry['title']}**: {before} → *{entry['journal_name']}* ({entry['year']}), {entry['journal_url']}")
            out.append("")
        if self.refreshed:
            out += [f"### Preprint details updated ({len(self.refreshed)})", ""]
            for entry, fields in self.refreshed:
                out.append(f"- **{entry['title']}**: {' and '.join(fields)} updated from the latest preprint version")
            out.append("")
        if self.attention:
            out += [f"### Needs a manual look ({len(self.attention)})", ""]
            out += [f"- {line}" for line in self.attention]
            out.append("")
        if self.skipped:
            out += [
                f"<details><summary>Skipped {len(self.skipped)} item(s) on the ignore list</summary>",
                "",
                *[f"- {line}" for line in self.skipped],
                "",
                "</details>",
                "",
            ]
        if self.bookkeeping:
            out += ["Only small housekeeping changes: Google Scholar record ids, or author names spelled "
                    "according to `author_aliases`.", ""]
        elif not (self.changed or self.attention):
            out += ["No changes: the website already lists everything on the profile.", ""]
        out += [
            "---",
            "To review: edit `data/publications.json` on this branch (set `research_topics`, fix anything that "
            "looks off), add thumbnails, then merge to publish. To stop an item from coming back, add its title "
            "to `ignore` in `scripts/publications_config.json`.",
        ]
        return "\n".join(out) + "\n"


# ------------------------------------------------------------------ HTTP

def fetch_bytes(url: str, params: dict | None = None, browser: bool = False, tries: int = 3) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA if browser else BOT_UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch(url: str, params: dict | None = None, browser: bool = False, tries: int = 3) -> str:
    return fetch_bytes(url, params, browser, tries).decode("utf-8", errors="replace")


def fetch_json(url: str, params: dict | None = None) -> dict:
    return json.loads(fetch(url, params))


# ------------------------------------------------------------------ text

def strip_tags(text: str | None) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def norm(text: str | None) -> str:
    ascii_text = unicodedata.normalize("NFKD", strip_tags(text)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def same_title(a: str | None, b: str | None) -> bool:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= TITLE_MATCH


def surname(name: str) -> str:
    words = norm(name).split()
    return words[-1] if words else ""


def surnames(authors: list[str] | None) -> list[str]:
    return [surname(a) for a in authors or []]


def tidy_name(given: str, family: str = "") -> str:
    """'Pedro A. M.' + 'Mediano' -> 'Pedro AM Mediano', the style used on the site."""
    if not family:
        parts = given.split()
        given, family = " ".join(parts[:-1]), (parts[-1] if parts else "")
    out: list[str] = []
    for token in given.replace(".", ". ").split():
        token = token.rstrip(".")
        if len(token) == 1 and token.isalpha() and out and re.fullmatch(r"[A-Z]+", out[-1]):
            out[-1] += token.upper()
        else:
            out.append(token)
    return " ".join(out + [family]).strip()


def clean_abstract(text: str | None) -> str:
    text = strip_tags(text)
    return re.sub(r"^(abstract|summary)[\s.:]+", "", text, flags=re.I)


def is_preprint_venue(venue: str | None) -> bool:
    venue = (venue or "").lower()
    return any(server in venue for server in PREPRINT_SERVERS)


def arxiv_id(*texts: str | None) -> str | None:
    for text in texts:
        match = ARXIV_RE.search(text or "")
        if match:
            return match.group(1)
    return None


def biorxiv_id(*texts: str | None) -> str | None:
    for text in texts:
        if text and BIORXIV_HINT.search(text):
            match = BIORXIV_RE.search(re.sub(r"\s+", "", text))
            if match:
                return match.group(1)
    return None


def entry_keys(entry: dict) -> set[str]:
    links = [entry.get("doi"), entry.get("journal_url"), entry.get("preprint_url")]
    keys = {f"scholar:{sid}" for sid in entry.get("scholar_ids", [])}
    if entry.get("doi"):
        keys.add(f"doi:{entry['doi'].lower()}")
    if arxiv_id(*links):
        keys.add(f"arxiv:{arxiv_id(*links)}")
    if biorxiv_id(*links):
        keys.add(f"biorxiv:{biorxiv_id(*links)}")
    return keys


def row_keys(row: dict) -> set[str]:
    keys = set()
    if row.get("scholar_id"):
        keys.add(f"scholar:{row['scholar_id']}")
    if row.get("doi"):
        keys.add(f"doi:{row['doi'].lower()}")
    if row.get("arxiv"):
        keys.add(f"arxiv:{row['arxiv']}")
    if row.get("biorxiv"):
        keys.add(f"biorxiv:{row['biorxiv']}")
    return keys


def find_entry(pubs: list[dict], keys: set[str], title: str) -> dict | None:
    for entry in pubs:
        if keys & entry_keys(entry):
            return entry
    for entry in pubs:
        if same_title(title, entry.get("title")):
            return entry
    return None


# ------------------------------------------------------------------ publication lists

def scholar_rows(user: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        params = {"user": user, "hl": "en", "cstart": start, "pagesize": 100, "sortby": "pubdate"}
        try:
            page = fetch("https://scholar.google.com/citations", params, browser=True, tries=1)
        except (urllib.error.URLError, TimeoutError) as err:
            raise Blocked(f"Google Scholar request failed ({err})") from err
        found = re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page, re.S)
        if not found and not rows:
            raise Blocked("Google Scholar returned a page without publications (probably a CAPTCHA)")
        for raw in found:
            link = re.search(r'citation_for_view=([\w-]+:[\w-]+)"\s+class="gsc_a_at">(.*?)</a>', raw, re.S)
            if not link:
                continue
            grays = [strip_tags(g) for g in re.findall(r'<div class="gs_gray">(.*?)</div>', raw, re.S)]
            authors = grays[0] if grays else ""
            venue = re.sub(r",\s*\d{4}$", "", grays[1]) if len(grays) > 1 else ""
            year = re.search(r'gsc_a_h gsc_a_hc gs_ibl">(\d{4})', raw)
            rows.append({
                "source": "Google Scholar",
                "scholar_id": link.group(1),
                "title": strip_tags(link.group(2)),
                "authors": [a.strip() for a in authors.split(",") if a.strip() and a.strip() != "..."],
                "venue": venue,
                "year": year.group(1) if year else "",
                "arxiv": arxiv_id(venue),
                "biorxiv": biorxiv_id(venue),
                "url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user="
                       f"{user}&citation_for_view={link.group(1)}",
            })
        if len(found) < 100:
            return rows
        start += 100
        time.sleep(3)


def openalex_rows(orcid: str) -> list[dict]:
    try:
        data = fetch_json("https://api.openalex.org/works", {
            "filter": f"author.orcid:{orcid}",
            "per-page": 200,
            "sort": "publication_date:desc",
            "select": "doi,title,publication_date,primary_location,locations,authorships",
        })
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise Blocked(f"OpenAlex request failed ({err})") from err
    rows = []
    for work in data.get("results", []):
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        links = [doi] + [loc.get("landing_page_url") or "" for loc in work.get("locations") or []]
        venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        rows.append({
            "source": "OpenAlex",
            "title": strip_tags(work.get("title")),
            "authors": [a["author"]["display_name"] for a in work.get("authorships", [])],
            "venue": venue,
            "year": (work.get("publication_date") or "")[:4],
            "doi": doi or None,
            "arxiv": arxiv_id(*links),
            "biorxiv": biorxiv_id(doi),
            "url": f"https://doi.org/{doi}" if doi else "",
        })
    if not rows:
        raise Blocked("OpenAlex returned no works for this ORCID")
    return rows


def publication_rows(config: dict, source: str, report: Report) -> list[dict]:
    if source in ("auto", "scholar"):
        try:
            rows = scholar_rows(config["scholar_user"])
            report.source = "Google Scholar"
            return rows
        except Blocked as err:
            if source == "scholar":
                raise
            print(f"warning: {err}; falling back to OpenAlex", file=sys.stderr)
            report.source = f"OpenAlex, because Google Scholar was unavailable ({err})"
    else:
        report.source = "OpenAlex"
    return openalex_rows(config["orcid"])


# ------------------------------------------------------------------ metadata sources

class Arxiv:
    """arXiv API client that caches entries by id."""

    NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    def __init__(self) -> None:
        self.cache: dict[str, dict | None] = {}

    def prefetch(self, ids: list[str]) -> None:
        wanted = sorted({i for i in ids if i and i not in self.cache})
        if not wanted:
            return
        feed = fetch("https://export.arxiv.org/api/query", {"id_list": ",".join(wanted), "max_results": len(wanted)})
        for entry in ET.fromstring(feed).findall("a:entry", self.NS):
            meta = self._parse(entry)
            if meta:
                self.cache[meta["arxiv"]] = meta
        for i in wanted:
            self.cache.setdefault(i, None)
        time.sleep(3)  # arXiv asks for a pause between API calls

    def get(self, ident: str) -> dict | None:
        self.prefetch([ident])
        return self.cache.get(ident)

    def _parse(self, entry: ET.Element) -> dict | None:
        text = lambda path: (entry.findtext(path, default="", namespaces=self.NS) or "").strip()
        ident = arxiv_id(text("a:id"))
        if not ident or not text("a:title"):
            return None
        return {
            "arxiv": ident,
            "title": " ".join(text("a:title").split()),
            "authors": [tidy_name(a.findtext("a:name", "", self.NS)) for a in entry.findall("a:author", self.NS)],
            "abstract": " ".join(text("a:summary").split()),
            "date": text("a:published")[:10],
            "type": "preprint",
            "journal_name": "arXiv",
            "journal_url": f"https://arxiv.org/abs/{ident}",
            "doi": f"10.48550/arXiv.{ident}",
            "pdf_path": f"https://arxiv.org/pdf/{ident}",
            "published_doi": text("arxiv:doi") or None,
        }


def crossref_date(item: dict) -> str:
    for key in ("published", "published-online", "published-print", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            year, month, day = (list(parts) + [1, 1])[:3]
            return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def crossref_meta(item: dict) -> dict:
    kind = item.get("type")
    venue = strip_tags((item.get("container-title") or [""])[0])
    if kind == "posted-content" and not venue:
        venue = ((item.get("institution") or [{}])[0]).get("name") or "Preprint"
    meta = {
        "title": strip_tags((item.get("title") or [""])[0]),
        "authors": [
            tidy_name(a.get("given", ""), a["family"]) if a.get("family") else a.get("name", "")
            for a in item.get("author", [])
        ],
        "abstract": clean_abstract(item.get("abstract")),
        "date": crossref_date(item),
        "type": {"posted-content": "preprint", "proceedings-article": "conference"}.get(kind, "journal"),
        "journal_name": venue,
        "journal_url": f"https://doi.org/{item['DOI']}",
        "doi": item["DOI"],
    }
    for key in ("volume", "issue"):
        if item.get(key):
            meta[key] = item[key]
    if item.get("page") or item.get("article-number"):
        meta["pages"] = item.get("page") or item.get("article-number")
    return meta


def crossref_by_doi(doi: str) -> dict:
    return crossref_meta(fetch_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")["message"])


def crossref_search(title: str, author: str, types: tuple[str, ...]) -> dict | None:
    items = fetch_json("https://api.crossref.org/works", {
        "query.bibliographic": title,
        "query.author": author,
        "rows": 5,
    })["message"]["items"]
    for item in items:
        families = [norm(a.get("family")) for a in item.get("author", [])]
        if (
            item.get("type") in types
            and same_title(title, (item.get("title") or [""])[0])
            and any(norm(author) in family.split() for family in families)
        ):
            return crossref_meta(item)
    return None


@functools.lru_cache(maxsize=None)
def biorxiv_info(ident: str) -> dict | None:
    """Latest version of a bioRxiv/medRxiv preprint (DOI prefix changed in 2025, so try both)."""
    for prefix in ("10.64898", "10.1101"):
        for server in ("biorxiv", "medrxiv"):
            data = fetch_json(f"https://api.biorxiv.org/details/{server}/{prefix}/{ident}")
            versions = data.get("collection") or []
            if versions:
                latest = versions[-1]
                published = latest.get("published")
                return {
                    "doi": latest["doi"],
                    "server": server,
                    "version": latest.get("version", "1"),
                    "abstract": " ".join((latest.get("abstract") or "").split()),
                    "published_doi": published if published and published != "NA" else None,
                }
    return None


def biorxiv_meta(ident: str) -> dict | None:
    info = biorxiv_info(ident)
    if not info:
        return None
    meta = crossref_by_doi(info["doi"])
    base = f"https://www.{info['server']}.org/content/{info['doi']}"
    meta.update({
        "type": "preprint",
        "journal_name": "bioRxiv" if info["server"] == "biorxiv" else "medRxiv",
        "journal_url": base,
        "pdf_path": f"{base}v{info['version']}.full.pdf",
        "abstract": meta.get("abstract") or info["abstract"],
        "published_doi": info["published_doi"],
    })
    return meta


def openalex_abstract(doi: str) -> str:
    work = fetch_json(f"https://api.openalex.org/works/https://doi.org/{doi}", {"select": "abstract_inverted_index"})
    index = work.get("abstract_inverted_index") or {}
    words = sorted((position, word) for word, positions in index.items() for position in positions)
    return " ".join(word for _, word in words)


def with_abstract(meta: dict | None) -> dict | None:
    if meta and not meta.get("abstract") and meta.get("doi"):
        try:
            meta["abstract"] = openalex_abstract(meta["doi"])
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
    return meta


def describe(row: dict, config: dict, arxiv: Arxiv) -> dict | None:
    """Full metadata for a new item, from the most authoritative source available."""
    if row.get("arxiv") and arxiv.get(row["arxiv"]):
        return dict(arxiv.get(row["arxiv"]))
    if row.get("biorxiv"):
        meta = biorxiv_meta(row["biorxiv"])
        if meta:
            return meta
    if row.get("doi"):
        return with_abstract(crossref_by_doi(row["doi"]))
    types = ("journal-article", "proceedings-article", "posted-content", "book-chapter")
    return with_abstract(crossref_search(row["title"], config["author_surname"], types))


def published_version(entry: dict, config: dict, arxiv: Arxiv) -> dict | None:
    """The journal/conference version of a preprint entry, if one exists."""
    links = [entry.get("doi"), entry.get("journal_url")]
    doi = None
    if arxiv_id(*links):
        doi = (arxiv.get(arxiv_id(*links)) or {}).get("published_doi")
    elif biorxiv_id(*links):
        doi = (biorxiv_info(biorxiv_id(*links)) or {}).get("published_doi")
    if doi:
        meta = crossref_by_doi(doi)
    else:
        meta = crossref_search(entry["title"], config["author_surname"], ("journal-article", "proceedings-article"))
    if meta and meta["type"] != "preprint":
        return with_abstract(meta)
    return None


def current_preprint(entry: dict, arxiv: Arxiv) -> dict | None:
    links = [entry.get("doi"), entry.get("journal_url")]
    if arxiv_id(*links):
        return arxiv.get(arxiv_id(*links))
    if biorxiv_id(*links):
        info = biorxiv_info(biorxiv_id(*links))
        return crossref_by_doi(info["doi"]) if info else None
    return None


def save_arxiv_figure(ident: str, pub_id: str) -> Path | None:
    """Save the first sizeable figure of the arXiv HTML version as the thumbnail."""
    if any(IMAGE_DIR.glob(f"{pub_id}.*")):
        return None
    page_url = f"https://arxiv.org/html/{ident}"
    try:
        page = fetch(page_url, tries=1)
    except (urllib.error.URLError, TimeoutError):
        return None  # not every paper has an HTML version
    candidates = []
    for tag in re.findall(r"<img\b[^>]*>", page):
        src = re.search(r'src="([^"]+)"', tag)
        if "ltx_graphics" not in tag or not src or src.group(1).lower().endswith(".svg"):
            continue
        size = [int(v) for v in re.findall(r'(?:width|height)="(\d+)"', tag)[:2]]
        candidates.append((size[0] * size[1] if len(size) == 2 else 0, src.group(1)))
    # Prefer the first figure that is big enough to read; otherwise the biggest one.
    choice = next((src for area, src in candidates if area >= 300 * 200), None)
    if choice is None and candidates:
        choice = max(candidates)[1]
    if choice is None:
        return None
    url = urllib.parse.urljoin(page_url, choice)  # src looks like "2609.07624v1/fig_1.png"
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower() or ".png"
    try:
        image = fetch_bytes(url, tries=1)
    except (urllib.error.URLError, TimeoutError):
        return None
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_DIR / f"{pub_id}{suffix}"
    target.write_bytes(image)
    return target


def canonical_authors(meta: dict | None, config: dict) -> dict | None:
    """Apply "author_aliases" from the config, so names are spelled the same everywhere."""
    if meta and meta.get("authors"):
        aliases = config.get("author_aliases", {})
        meta["authors"] = [aliases.get(name, name) for name in meta["authors"]]
    return meta


# ------------------------------------------------------------------ entries

def merge_title_authors(entry: dict, meta: dict) -> list[str]:
    """Take title/authors from meta only when they really differ (not just in formatting)."""
    changed = []
    if meta.get("title") and norm(meta["title"]) != norm(entry.get("title")):
        entry["title"] = meta["title"]
        changed.append("title")
    if meta.get("authors") and surnames(meta["authors"]) != surnames(entry.get("authors")):
        entry["authors"] = meta["authors"]
        changed.append("authors")
    return changed


def apply_published(entry: dict, meta: dict) -> None:
    if entry.get("journal_url") and not entry.get("preprint_url"):
        entry["preprint_url"] = entry["journal_url"]
    merge_title_authors(entry, meta)
    entry["date"] = meta["date"]
    entry["year"] = int(meta["date"][:4])
    for key in ("type", "journal_name", "journal_url", "doi"):
        entry[key] = meta[key]
    for key in ("volume", "issue", "pages"):
        if meta.get(key):
            entry[key] = meta[key]
        else:
            entry.pop(key, None)
    if not entry.get("abstract") and meta.get("abstract"):
        entry["abstract"] = meta["abstract"]


def make_id(meta: dict, taken: set[str]) -> str:
    # First author's family name (e.g. "Ah-Weng" -> "ahweng"), year, first significant title word.
    first = re.sub(r"[^a-z]", "", norm(meta["authors"][0].split()[-1])) if meta.get("authors") else "anon"
    word = next((w for w in norm(meta["title"]).split() if w not in STOPWORDS and not w.isdigit()), "paper")
    base = f"{first}{meta['date'][:4]}{word}"
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}{n}", n + 1
    return ident


def guess_topics(entry: dict, config: dict) -> list[str]:
    title, abstract = norm(entry.get("title")), norm(entry.get("abstract"))
    scores = {}
    for topic, words in config.get("topic_keywords", {}).items():
        score = sum(2 * title.count(norm(w)) + abstract.count(norm(w)) for w in words)
        if score:
            scores[topic] = score
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked and ranked[0][1] >= 3 and (len(ranked) == 1 or ranked[0][1] >= 1.5 * ranked[1][1]):
        return [ranked[0][0]]
    return []


def fallback_meta(row: dict) -> dict:
    return {
        "title": row["title"],
        "authors": [tidy_name(a) for a in row.get("authors", [])],
        "date": f"{row.get('year') or time.strftime('%Y')}-01-01",
        "type": "preprint" if is_preprint_venue(row.get("venue")) else "journal",
        "journal_name": row.get("venue") or "",
        "journal_url": row.get("url") or "",
    }


def new_entry(meta: dict, row: dict, pubs: list[dict], config: dict) -> tuple[dict, bool]:
    meta = {**meta, "date": meta.get("date") or fallback_meta(row)["date"]}
    entry = {key: meta[key] for key in FIELD_ORDER if meta.get(key)}
    entry["id"] = make_id(meta, {p["id"] for p in pubs})
    entry["year"] = int(entry["date"][:4])
    topics = guess_topics(entry, config)
    entry["research_topics"] = topics
    if row.get("scholar_id"):
        entry["scholar_ids"] = [row["scholar_id"]]
    return {key: entry[key] for key in FIELD_ORDER if key in entry}, bool(topics)


def ignored(row: dict, config: dict) -> str | None:
    for item in config.get("ignore", []):
        if same_title(row["title"], item["title"]):
            return item.get("reason", "on the ignore list")
    for pattern in config.get("ignore_venue_patterns", []):
        if re.search(pattern, row.get("venue") or "", re.I):
            return f"venue matches '{pattern}'"
    return None


# ------------------------------------------------------------------ output

def dump(value, level: int = 0) -> str:
    """JSON with lists of plain values kept on one line, matching the hand-written file."""
    pad = "  " * level
    if isinstance(value, dict) and value:
        items = [f"{pad}  {json.dumps(k)}: {dump(v, level + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
        return "[\n" + ",\n".join(f"{pad}  {dump(v, level + 1)}" for v in value) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


# ------------------------------------------------------------------ main

def run(args: argparse.Namespace) -> Report:
    config = json.loads(CONFIG_FILE.read_text())
    data = json.loads(DATA_FILE.read_text())
    pubs: list[dict] = data["publications"]
    report = Report()
    arxiv = Arxiv()
    network_errors = (urllib.error.URLError, TimeoutError, ValueError, KeyError, ET.ParseError)

    rows = publication_rows(config, args.source, report)
    try:
        arxiv.prefetch([r.get("arxiv") for r in rows] + [arxiv_id(p.get("doi"), p.get("journal_url")) for p in pubs])
    except network_errors as err:
        report.warn(f"The arXiv API was unavailable ({err}); arXiv details may be missing.")

    # 1. Match the profile against the site; collect what is new.
    published_hints: dict[str, str] = {}
    for row in rows:
        entry = find_entry(pubs, row_keys(row), row["title"])
        reason = None if entry else ignored(row, config)
        if reason:
            report.skipped.append(f"{row['title']} ({reason})")
            continue
        if entry is None:
            try:
                meta = canonical_authors(describe(row, config, arxiv), config)
            except network_errors as err:
                report.warn(f"Could not fetch details for '{row['title']}' ({err}); used the basic listing instead.")
                meta = None
            if meta is None:
                meta = fallback_meta(row)
                report.attention.append(
                    f"Only basic details were available for **{row['title']}**: check its authors, date and link."
                )
            # The full metadata may reveal an id that the listing did not show.
            entry = find_entry(pubs, entry_keys(meta), meta["title"])
            if entry is None:
                entry, guessed = new_entry(meta, row, pubs, config)
                pubs.append(entry)
                report.new.append((entry, guessed))
                if row.get("arxiv") and not args.dry_run:
                    saved = save_arxiv_figure(row["arxiv"], entry["id"])
                    if saved:
                        report.thumbnails[entry["id"]] = saved.relative_to(ROOT).as_posix()
                for other in pubs:
                    overlap = set(surnames(other.get("authors"))) & set(surnames(entry.get("authors")))
                    if (
                        other is not entry
                        and len(overlap) >= 0.6 * max(1, len(entry.get("authors", [])))
                        and difflib.SequenceMatcher(None, norm(other["title"]), norm(entry["title"])).ratio() >= 0.5
                    ):
                        report.attention.append(
                            f"**{entry['title']}** may be another version of **{other['title']}**. "
                            "If so, keep one entry and delete the other."
                        )
        if row.get("scholar_id") and row["scholar_id"] not in entry.get("scholar_ids", []):
            entry["scholar_ids"] = entry.get("scholar_ids", []) + [row["scholar_id"]]
        if not is_preprint_venue(row.get("venue")) and row.get("venue"):
            published_hints[entry["id"]] = row["venue"]

    # 2. Upgrade preprints that have been published; refresh the rest.
    known_dois = {p["doi"].lower() for p in pubs if p.get("doi")}
    for entry in [p for p in pubs if p.get("type") == "preprint"]:
        try:
            meta = canonical_authors(published_version(entry, config, arxiv), config)
            if meta and meta["doi"].lower() in known_dois:
                report.attention.append(
                    f"**{entry['title']}** looks published as {meta['journal_url']}, which is already listed "
                    "as a separate entry. Keep one of the two."
                )
            elif meta:
                before = entry.get("journal_name", "preprint")
                apply_published(entry, meta)
                report.upgraded.append((entry, before))
            else:
                if entry["id"] in published_hints:
                    report.attention.append(
                        f"Google Scholar lists **{entry['title']}** in *{published_hints[entry['id']]}*, but no "
                        "matching published version was found. Update the entry by hand if it has been published."
                    )
                latest = canonical_authors(current_preprint(entry, arxiv), config)
                changed = merge_title_authors(entry, latest) if latest else []
                if changed and not any(e is entry for e, _ in report.new):
                    report.refreshed.append((entry, changed))
        except network_errors as err:
            report.warn(f"Could not check **{entry['title']}** for a published version ({err}).")

    for entry in pubs:
        canonical_authors(entry, config)
    pubs.sort(key=lambda p: p.get("date", ""), reverse=True)
    text = dump(data) + "\n"
    report.bookkeeping = text != DATA_FILE.read_text() and not report.changed
    if not args.dry_run and text != DATA_FILE.read_text():
        DATA_FILE.write_text(text)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", choices=("auto", "scholar", "openalex"), default="auto",
                        help="where to read the publication list (default: Scholar, falling back to OpenAlex)")
    parser.add_argument("--dry-run", action="store_true", help="print the report without changing the data file")
    parser.add_argument("--report", type=Path, help="also write the Markdown report to this file")
    args = parser.parse_args()
    try:
        report = run(args)
    except Blocked as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    text = report.markdown()
    print(text)
    if args.report:
        args.report.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
