#!/usr/bin/env python3
"""Render the README demo screenshot: one real /search call, framed like a terminal.

    set -a && . ./.env && set +a            # exports WS_API_KEY
    ./venv/bin/python scripts/render_demo.py            # hits localhost:8080, writes docs/demo.png
    ./venv/bin/python scripts/render_demo.py resp.json  # render a saved response instead

The picture is drawn from the real JSON the service returned: the only edit is that
the long `markdown` string is cut after a few lines so the image stays readable.
The API key is shown as `$WS_API_KEY`, never its value.
"""

from __future__ import annotations

import html
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.png"

QUERY = {
    "query": "Skyr Yogurt nutrition per 100g",
    "limit": 1,
    "scrape": ["markdown"],
    "scrape_deadline_ms": 8000,
}
MARKDOWN_PREVIEW_CHARS = 330

COMMAND_LINES = [
    ("curl", " -sS -X POST localhost:8080/search \\"),
    ("", '-H "X-API-Key: $WS_API_KEY" \\'),
    ("", "-H 'Content-Type: application/json' \\"),
    ("", "-d '" + json.dumps(QUERY) + "' | jq ."),
]


def fetch() -> dict:
    key = os.environ.get("WS_API_KEY")
    if not key:
        sys.exit("WS_API_KEY is not set (set -a && . ./.env && set +a)")
    req = urllib.request.Request(
        "http://localhost:8080/search",
        data=json.dumps(QUERY).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


# ---------------------------------------------------------------- jq-style HTML

def esc(s: str) -> str:
    return html.escape(s, quote=False)


def span(cls: str, text: str) -> str:
    return f'<span class="{cls}">{esc(text)}</span>'


class Lines:
    """Terminal output as (indent, html) rows; wrapped rows hang at their indent."""

    def __init__(self) -> None:
        self.rows: list[list] = []
        self.pending = ""  # appended to the current row once it is complete (after any comma)

    def new(self, indent: int) -> None:
        if self.rows and self.pending:
            self.rows[-1][1] += self.pending
        self.pending = ""
        self.rows.append([indent, ""])

    def add(self, fragment: str) -> None:
        self.rows[-1][1] += fragment

    def html(self) -> str:
        return "".join(f'<span class="ln" style="--i:{i}">{h}</span>' for i, h in self.rows)


def render_value(v, indent: int, b: Lines, key_path: tuple = ()) -> None:
    if isinstance(v, dict):
        if not v:
            b.add(span("p", "{}"))
            return
        b.add(span("p", "{"))
        items = list(v.items())
        for i, (k, val) in enumerate(items):
            b.new(indent + 1)
            b.add(span("k", json.dumps(k)) + span("p", ": "))
            render_value(val, indent + 1, b, key_path + (k,))
            if i < len(items) - 1:
                b.add(span("p", ","))
        b.new(indent)
        b.add(span("p", "}"))
    elif isinstance(v, list):
        if not v:
            b.add(span("p", "[]"))
            return
        b.add(span("p", "["))
        for i, val in enumerate(v):
            b.new(indent + 1)
            render_value(val, indent + 1, b, key_path + (i,))
            if i < len(v) - 1:
                b.add(span("p", ","))
        b.new(indent)
        b.add(span("p", "]"))
    elif isinstance(v, str):
        if key_path and key_path[-1] == "markdown" and len(v) > MARKDOWN_PREVIEW_CHARS:
            shown = json.dumps(v[:MARKDOWN_PREVIEW_CHARS])[:-1] + "…\""
            kb = len(v.encode()) / 1024
            b.add(span("s", shown))
            b.pending = span("dim", f"   ⋯ {kb:.1f} kB, trimmed for the picture")
        else:
            b.add(span("s", json.dumps(v)))
    elif v is None:
        b.add(span("null", "null"))
    elif isinstance(v, bool):
        b.add(span("b", "true" if v else "false"))
    else:
        b.add(span("n", json.dumps(v)))


def build_html(resp: dict) -> str:
    b = Lines()
    for i, (head, rest) in enumerate(COMMAND_LINES):
        b.new(0 if i == 0 else 2)
        if i == 0:
            b.add('<span class="prompt">❯</span> ')
        line = (span("cmd", head) if head else "") + esc(rest)
        line = line.replace("\\", '<span class="dim">\\</span>')
        line = line.replace("$WS_API_KEY", '<span class="var">$WS_API_KEY</span>')
        b.add(line)
    b.new(0)
    render_value(resp, 0, b)
    b.new(0)
    b.add('<span class="prompt">❯</span> <span class="cursor"></span>')
    body = b.html()

    return f"""<!doctype html>
<meta charset="utf-8">
<style>
  :root {{
    --bg: #0b0e14; --bar: #131820; --edge: rgba(255,255,255,.08);
    --fg: #e6edf3; --dim: #6e7681; --key: #79c0ff; --str: #7ee787;
    --num: #e6edf3; --null: #8b949e; --accent: #3fb950;
  }}
  html, body {{ margin: 0; background: transparent; }}
  #shot {{ display: inline-block; padding: 36px; }}
  .win {{
    width: 880px; border-radius: 12px; overflow: hidden; background: var(--bg);
    box-shadow: 0 0 0 1px var(--edge), 0 24px 60px -12px rgba(0,0,0,.55), 0 8px 20px -8px rgba(0,0,0,.4);
    font: 13px/1.62 "SF Mono", Menlo, ui-monospace, monospace; color: var(--fg);
  }}
  .bar {{ height: 40px; background: var(--bar); border-bottom: 1px solid var(--edge);
          display: flex; align-items: center; padding: 0 16px; position: relative; }}
  .dots {{ display: flex; gap: 8px; }}
  .dots i {{ width: 12px; height: 12px; border-radius: 50%; display: block; }}
  .dots i:nth-child(1) {{ background: #ff5f57; }}
  .dots i:nth-child(2) {{ background: #febc2e; }}
  .dots i:nth-child(3) {{ background: #28c840; }}
  .title {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
            color: var(--dim); font: 12.5px -apple-system, "SF Pro Text", system-ui, sans-serif; letter-spacing: .01em; }}
  .body {{ padding: 18px 24px 20px; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .ln {{ display: block; padding-left: calc(var(--i, 0) * 2ch); min-height: 1.62em; }}
  .prompt {{ color: var(--accent); font-weight: 600; }}
  .cmd {{ color: var(--fg); font-weight: 600; }}
  .var {{ color: var(--key); }}
  .k {{ color: var(--key); font-weight: 600; }}
  .s {{ color: var(--str); }}
  .n {{ color: var(--num); }}
  .b {{ color: var(--fg); }}
  .null {{ color: var(--null); }}
  .p {{ color: var(--fg); font-weight: 600; }}
  .dim {{ color: var(--dim); }}
  .cursor {{ display: inline-block; width: 8px; height: 15px; background: var(--fg); opacity: .85; vertical-align: -2px; }}
</style>
<div id="shot"><div class="win">
  <div class="bar"><div class="dots"><i></i><i></i><i></i></div><div class="title">open-web-search — zsh</div></div>
  <div class="body">{body}</div>
</div></div>
"""


def main() -> None:
    resp = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else fetch()
    page_html = build_html(resp)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(device_scale_factor=2, viewport={"width": 1000, "height": 900})
        page.set_content(page_html)
        page.wait_for_timeout(150)
        page.locator("#shot").screenshot(path=str(OUT), omit_background=True)
        browser.close()
    print(f"wrote {OUT.relative_to(ROOT)}  total_ms={resp.get('timing_ms', {}).get('total_ms')}")


if __name__ == "__main__":
    main()
