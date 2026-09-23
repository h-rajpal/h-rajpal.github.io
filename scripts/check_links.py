#!/usr/bin/env python3
"""Fail if the built site links to pages, images or PDFs that do not exist.

Only links inside the site are checked (external sites are too flaky to gate a
deploy on). Run after building:

  hugo --baseURL https://h-rajpal.github.io/
  python3 scripts/check_links.py public --base-url https://h-rajpal.github.io/
"""
from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "whatsapp:")


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if not value:
                continue
            if name in ("href", "src"):
                self.links.append(value)
            elif name == "srcset":
                self.links += [part.split()[0] for part in value.split(",") if part.strip()]


def target_exists(root: Path, url_path: str) -> bool:
    path = root / unquote(url_path).lstrip("/")
    return (
        path.is_file()
        or (path / "index.html").is_file()
        or path.with_suffix(".html").is_file()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("public_dir", type=Path)
    parser.add_argument("--base-url", default="/", help="the baseURL the site was built with")
    args = parser.parse_args()

    root = args.public_dir.resolve()
    base = urlsplit(args.base_url)
    broken: dict[str, set[str]] = {}
    pages = sorted(root.rglob("*.html"))
    for page in pages:
        collector = LinkCollector()
        collector.feed(page.read_text(errors="replace"))
        page_url = "/" + page.relative_to(root).as_posix()
        for link in collector.links:
            if link.startswith("#") or link.lower().startswith(SKIP_SCHEMES):
                continue
            url = urlsplit(urljoin(page_url, link))
            if url.scheme or url.netloc:
                if url.netloc != base.netloc:
                    continue  # external link
            path = url.path
            if base.path.rstrip("/") and path.startswith(base.path):
                path = "/" + path[len(base.path):]
            if not target_exists(root, path):
                broken.setdefault(path, set()).add(page_url)

    for path, sources in sorted(broken.items()):
        print(f"broken: {path}  (linked from {', '.join(sorted(sources)[:3])}{' ...' if len(sources) > 3 else ''})")
    print(f"Checked {len(pages)} pages: {len(broken)} broken internal link(s).")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
