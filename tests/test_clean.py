"""Markdown output, on fixed HTML — no network."""

from app.scrape.clean import normalise_for_match, page_title, tidy, to_markdown

ARTICLE = """
<html><head>
  <title>Introduction - The WebAssembly Component Model</title>
</head><body>
  <nav><a href="/x">Keyboard shortcuts</a></nav>
  <article>
    <h1>Component Model</h1>
    <p>The component model is an architecture for building interoperable
       WebAssembly libraries. See the <a href="https://example.test/spec">specification</a>
       for details, which covers the full surface in depth and at length.</p>
    <p>This documentation is aimed at users of the component model: developers
       of libraries and applications who want to ship portable code.</p>
    <table><tr><th>Lang</th><th>Status</th></tr><tr><td>Rust</td><td>Ready</td></tr></table>
  </article>
  <footer>Copyright</footer>
</body></html>
"""


class TestTidy:
    def test_collapses_runs_of_blank_lines(self):
        assert tidy("a\n\n\n\n\nb") == "a\n\nb\n"

    def test_strips_trailing_whitespace_per_line(self):
        assert tidy("a   \nb\t\n") == "a\nb\n"

    def test_strips_the_pad_before_a_table_pipe(self):
        # Extraction emits "| cell | " with a stray space before the closing pipe.
        assert tidy("| a | b | \n") == "| a | b |\n"

    def test_always_ends_with_exactly_one_newline(self):
        for raw in ("x", "x\n", "x\n\n\n"):
            assert tidy(raw) == "x\n"


class TestToMarkdown:
    def setup_method(self):
        self.md = to_markdown(ARTICLE, "https://example.test/")

    def test_keeps_links(self):
        # A scrape endpoint that strips every link throws away half of what a page
        # says: "see the specification" is useless without the href.
        assert "https://example.test/spec" in self.md

    def test_links_can_be_turned_off(self):
        assert "https://example.test/spec" not in to_markdown(
            ARTICLE, "https://example.test/", include_links=False
        )

    def test_keeps_tables(self):
        assert "Rust" in self.md

    def test_output_is_tidy(self):
        assert "\n\n\n" not in self.md
        assert not any(line != line.rstrip() for line in self.md.splitlines())
        assert self.md.endswith("\n")

    def test_does_not_prepend_a_title_heading(self):
        # The title is metadata and travels in its own field. Injecting it as a
        # heading duplicated the page's real h1 and, when the heuristic misfired,
        # put a nav label at the top of the document.
        assert not self.md.lstrip().startswith("# Introduction")

    def test_falls_back_rather_than_returning_nothing(self):
        # A shell trafilatura cannot parse must still yield its text.
        shell = "<html><body><div>" + "some standalone text " * 30 + "</div></body></html>"
        assert "standalone text" in to_markdown(shell)


class TestPageTitle:
    def test_reads_the_document_title(self):
        assert page_title(ARTICLE) == "Introduction - The WebAssembly Component Model"

    def test_og_title_wins(self):
        html = ARTICLE.replace(
            "<title>", '<meta property="og:title" content="Shared Title"><title>'
        )
        assert page_title(html) == "Shared Title"

    def test_none_when_the_page_has_no_title(self):
        assert page_title("<html><body><p>hi</p></body></html>") is None

    def test_does_not_pick_up_nav_text(self):
        # trafilatura's metadata heuristic returned "Keyboard shortcuts" for a real
        # docs site whose <title> said something else entirely.
        assert page_title(ARTICLE) != "Keyboard shortcuts"


class TestNormaliseForMatch:
    def test_collapses_case_and_whitespace(self):
        assert normalise_for_match("  Hello   WORLD \n") == "hello world"

    def test_unifies_comma_decimals(self):
        assert normalise_for_match("3,5") == "3.5"
