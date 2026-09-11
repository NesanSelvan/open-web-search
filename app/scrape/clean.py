"""HTML -> clean markdown.

trafilatura does the extraction; this module decides what "clean" means and fixes
what it leaves behind.

Three choices worth knowing about:

* **Links are kept.** A scrape endpoint that strips every link throws away half of
  what a page says — "see the spec" is useless without the href, and anything
  crawling onward needs them.
* **The title is metadata, not content.** It is returned as its own field rather
  than injected as a heading. trafilatura's metadata heuristic is unreliable — on
  a real docs site it returned "Keyboard shortcuts" while `<title>` said
  "Introduction - The WebAssembly Component Model" — so `page_title` reads the
  document's own tags instead, and the markdown is left as the page wrote it.
* **Whitespace is normalised.** Extraction preserves the source's own layout, so
  a table cell written across indented HTML comes out as
  `Vada Pav \t\t\t\t\t\t per 1 piece - Calories: 304kcal`. Those runs are noise to a
  reader and pure cost to a model. Leading indentation is kept, because in
  markdown it means list nesting and code blocks; runs *inside* a line collapse.
"""

from __future__ import annotations

import contextlib
import re

import trafilatura
import trafilatura.meta

_FALLBACK_STRIP = ("script", "style", "noscript", "svg", "nav", "footer")

# Collapse 3+ newlines to one paragraph break; strip trailing spaces per line.
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.M)
# Markdown tables come out with a stray space before each row's closing pipe.
_TABLE_PAD_RE = re.compile(r" +\|$", re.M)
# A run of whitespace INSIDE a line is the source HTML's indentation leaking
# through. Leading indentation is handled separately because markdown uses it.
_INNER_WS_RE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_LEADING_WS_RE = re.compile(r"^([ \t]*)(.*)$")
# More than this much leading whitespace is not list nesting, it is source layout.
_MAX_INDENT = 8


def tidy(markdown: str) -> str:
    """Normalise the whitespace extraction leaves behind.

    Runs of tabs inside a line are the biggest offender: a single table row can
    arrive carrying hundreds of them, which reads as garbage and costs tokens for
    nothing.
    """
    lines = []
    for raw in markdown.splitlines():
        indent, rest = _LEADING_WS_RE.match(raw).groups()
        if len(indent) > _MAX_INDENT:
            indent = indent[:_MAX_INDENT]
        rest = _INNER_WS_RE.sub(" ", rest)
        lines.append((indent + rest).rstrip())

    text = "\n".join(lines)
    text = _TRAILING_WS_RE.sub("", text)
    text = _TABLE_PAD_RE.sub(" |", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip() + "\n"


def to_markdown(
    html: str,
    url: str = "",
    *,
    include_links: bool = True,
    include_images: bool = False,
    favor_recall: bool = False,
) -> str:
    """Extract a page's main content as markdown.

    `favor_recall` trades precision for coverage. OFF by default because on a
    well-formed page it drags in navigation and boilerplate — and the retry below
    already covers the case where strict extraction finds nothing.
    """
    text = _extract(html, url, include_links, include_images, favor_recall)

    # Strict extraction can come back empty on an unusual layout. Retry once with
    # recall favoured before falling back to a crude strip: a thin page beats none.
    if not text and not favor_recall:
        text = _extract(html, url, include_links, include_images, True)

    if not text:
        text = _crude_strip(html)

    return tidy(text)


def _extract(
    html: str, url: str, include_links: bool, include_images: bool, favor_recall: bool
) -> str | None:
    # trafilatura memoises per document and does NOT key on the options, so a call
    # with different settings can hand back the previous call's result — observed
    # directly: the same HTML returned 421 chars with its table, then 390 without,
    # purely because an earlier call had asked for no links.
    with contextlib.suppress(Exception):
        trafilatura.meta.reset_caches()

    return trafilatura.extract(
        html,
        url=url or None,
        output_format="markdown",
        include_tables=True,        # tables often carry the actual content
        include_links=include_links,
        include_images=include_images,
        include_comments=False,
        include_formatting=True,    # headings, bold, lists
        favor_recall=favor_recall,
        deduplicate=True,           # repeated blocks are boilerplate
    )


def page_title(html: str) -> str | None:
    """The page's own title, from its own tags.

    Deliberately NOT trafilatura's metadata heuristic: on a real documentation site
    that returned "Keyboard shortcuts" — a nav label — while the document's
    `<title>` said "Introduction - The WebAssembly Component Model". og:title is
    what a site chooses to show when shared, so it wins; `<title>` is the fallback.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for sel, attr in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ):
        node = tree.css_first(sel)
        if node and (val := (node.attributes.get(attr) or "").strip()):
            return val
    node = tree.css_first("title")
    if node and (val := node.text(strip=True)):
        return val
    return None


def _crude_strip(html: str) -> str:
    """Last resort for pages trafilatura cannot parse at all."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for tag in _FALLBACK_STRIP:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


def normalise_for_match(text: str) -> str:
    """Lowercased, comma-decimals unified, whitespace collapsed.

    For asking "does this string actually appear on the page?" without tripping
    over formatting.
    """
    return " ".join(text.replace(",", ".").lower().split())
