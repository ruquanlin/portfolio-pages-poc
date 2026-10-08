#!/usr/bin/env python3
"""Check local HTML and CSS references resolve under a Pages project subpath."""

from __future__ import annotations

import argparse
import html.parser
import posixpath
import re
import sys
import urllib.parse
from pathlib import Path


class AssetParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str]] = []
        self.has_base = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "base" and values.get("href"):
            self.has_base = True
        for attr in ("src", "href", "poster"):
            value = values.get(attr)
            if value:
                self.references.append((value, f"<{tag} {attr}>") )


CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*(['\"])(.*?)\1\s*\)?", re.IGNORECASE)
EXTERNAL_SCHEMES = ("http:", "https:", "data:", "mailto:", "tel:", "javascript:", "blob:")


def is_external(reference: str) -> bool:
    value = reference.strip()
    lowered = value.lower()
    return (
        not value
        or value.startswith("#")
        or value.startswith("//")
        or lowered.startswith(EXTERNAL_SCHEMES)
    )


def resolve_reference(page_root: Path, source_file: Path, reference: str) -> Path | None:
    value = urllib.parse.unquote(reference.strip())
    value = value.split("#", 1)[0].split("?", 1)[0]
    if is_external(value):
        return None
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"root-absolute or backslash resource is not project-path safe: {reference!r}")
    source_rel = source_file.relative_to(page_root).as_posix()
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), value))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"resource escapes the work directory: {reference!r}")
    target = page_root.joinpath(*Path(resolved).parts)
    if not target.is_file():
        raise ValueError(f"local resource not found: {reference!r} from {source_rel!r}")
    return target


def check_tree(root: Path) -> int:
    if not root.is_dir():
        raise ValueError(f"work directory does not exist: {root}")
    html_files = sorted(root.rglob("*.html"))
    if not html_files or not (root / "index.html").is_file():
        raise ValueError("work must contain a root index.html")
    checked = 0
    for html_file in html_files:
        parser = AssetParser()
        parser.feed(html_file.read_text(encoding="utf-8"))
        parser.close()
        if parser.has_base:
            raise ValueError(f"<base href> is unsupported in project Pages paths: {html_file}")
        for reference, context in parser.references:
            if is_external(reference):
                continue
            resolve_reference(root, html_file, reference)
            checked += 1

    for css_file in sorted(root.rglob("*.css")):
        css = css_file.read_text(encoding="utf-8")
        references = [match.group(2) for match in CSS_URL.finditer(css)]
        references.extend(match.group(2) for match in CSS_IMPORT.finditer(css))
        for reference in references:
            if is_external(reference):
                continue
            resolve_reference(root, css_file, reference)
            checked += 1
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_root", type=Path)
    args = parser.parse_args()
    try:
        checked = check_tree(args.work_root)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Relative asset validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Relative asset paths OK ({checked} local HTML/CSS reference(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
