"""One-off: dump what Google actually served, so the extractor can be fixed against
reality rather than against an assumption about its markup."""

from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

from app.search.browser import chrome_context
from app.search.identity import IdentityPool
from app.settings import Settings

OUT = Path("/tmp/serp_debug")

PROBES = {
    "a[href^=http]": "a[href^='http']",
    "a:has(h3)": "a:has(h3)",
    "h3": "h3",
    "div#search": "div#search",
    "div#rso": "div#rso",
    "div[data-hveid]": "div[data-hveid]",
    "form#captcha-form": "form#captcha-form",
    "div#recaptcha": "div#recaptcha",
    "textarea[name=q]": "textarea[name='q']",
}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = Settings(identities_file=Path("config/identities.txt"))
    pool = IdentityPool.from_file(settings)
    ident = await pool.acquire()

    query = "amul masti dahi nutrition per 100g"
    url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"q": query, "num": "15", "hl": "en", "gl": "in"}
    )

    async with chrome_context(ident, settings) as ctx:
        page = await ctx.new_page()
        await page.goto(url, referer="https://www.google.com/", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        print("final url :", page.url)
        print("title     :", await page.title())
        print()

        for label, sel in PROBES.items():
            try:
                n = await page.locator(sel).count()
            except Exception as exc:
                n = f"ERR {exc}"
            print(f"{label:22} {n}")

        print("\n--- first 5 anchors owning an h3 ---")
        sample = await page.evaluate("""
        () => [...document.querySelectorAll('a')]
          .filter(a => a.querySelector('h3'))
          .slice(0, 5)
          .map(a => ({href: a.href, cls: a.className, h3: a.querySelector('h3').innerText}));
        """)
        for row in sample:
            print(row)

        print("\n--- first 5 h3 with their anchor ancestry ---")
        h3s = await page.evaluate("""
        () => [...document.querySelectorAll('h3')].slice(0, 5).map(h => {
          const a = h.closest('a');
          const near = a ? a.href : (h.parentElement?.querySelector('a')?.href ?? null);
          return {text: h.innerText.slice(0,60), inAnchor: !!a, href: near};
        });
        """)
        for row in h3s:
            print(row)

        html = await page.content()
        (OUT / "serp.html").write_text(html)
        await page.screenshot(path=str(OUT / "serp.png"), full_page=False)
        print(f"\nsaved {OUT/'serp.html'} ({len(html)} bytes) and serp.png")
        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
