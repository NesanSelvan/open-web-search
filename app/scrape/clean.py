"""HTML -> clean markdown, tables preserved.

trafilatura scores F1 0.859 at ~44ms/page — this is exactly the value a 1-credit
Firecrawl scrape was buying, and it is free.
"""

from __future__ import annotations

import trafilatura

_FALLBACK_STRIP = ("script", "style", "noscript", "svg")


def to_markdown(html: str, url: str = "") -> str:
    text = trafilatura.extract(
        html,
        url=url or None,
        output_format="markdown",
        include_tables=True,      # tables often carry the actual content — never drop them
        include_links=False,
        include_comments=False,
        favor_recall=True,        # a stray nav line costs nothing; lost content costs everything
    )
    if text:
        return text

    # trafilatura gives up on some SPA shells. Fall back to a crude strip rather
    # than returning nothing — the verification gate downstream stays honest either way.
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for tag in _FALLBACK_STRIP:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


def normalise_for_match(text: str) -> str:
    """Lowercased, comma-decimals unified, whitespace collapsed.

    Used by the anti-fabrication gate to ask "does this number actually appear on
    the page?" without tripping over formatting.
    """
    return " ".join(text.replace(",", ".").lower().split())
