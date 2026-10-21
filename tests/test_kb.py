"""`mcptoon kb` — search inside documents, not just which file to open.

The behaviour that matters, and the failure each test prevents:

* **A passage, not the file.** Searching a 95 KB document must return the few
  matching lines, not the document. If a hit's snippet were as big as the source,
  `kb` would be a slower `cat`.
* **Substring and typo both reach the answer.** porter alone misses "useEff";
  trigram alone is noisy. RRF over both is the whole point of having two indexes,
  so each strategy gets a test that fails if its index is silently dropped.
* **Cache within TTL, refetch after.** A second `fetch` inside the window must do
  no network work; that is the only reason a fetch cache exists.
* **Code fences are not headings.** A `# comment` inside a ``` block must not
  split a chunk, or every code sample gets sliced in half.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from mcptoon import kb


DOC = """# Alpha

Alpha intro line about kubernetes deployments.

## Beta

Beta covers caching. The cached value is reused when caching is enabled.

## Gamma

Gamma has a code sample:

```python
# this is a comment, not a heading
def useEffect():
    pass
```

Gamma closing line.
"""


class KBBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(os.environ, {"MCPTOON_KB_DIR": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)


class ChunkingTests(unittest.TestCase):
    def test_headings_split_chunks(self):
        chunks = kb.chunk_markdown(DOC)
        titles = [t for t, _ in chunks]
        self.assertIn("Alpha", titles)
        self.assertIn("Beta", titles)
        self.assertIn("Gamma", titles)

    def test_code_fence_hash_is_not_a_heading(self):
        chunks = kb.chunk_markdown(DOC)
        # The "# this is a comment" line must live inside Gamma, not start a chunk.
        gamma = [body for title, body in chunks if title == "Gamma"]
        self.assertTrue(gamma)
        self.assertIn("this is a comment, not a heading", "\n".join(gamma))
        self.assertNotIn("this is a comment", [t for t, _ in chunks])

    def test_long_chunk_is_split(self):
        text = "## Big\n\n" + ("word " * 2000)
        chunks = kb.chunk_markdown(text, max_chars=500)
        self.assertGreater(len(chunks), 1)
        for _, body in chunks:
            self.assertLessEqual(len(body), 700)  # allow the fence/para slack

    def test_empty_document_yields_nothing(self):
        self.assertEqual(kb.chunk_markdown("   \n\n  "), [])


class IndexAndSearchTests(KBBase):
    def _index_doc(self):
        return kb.index_text("doc://test", DOC, markdown=True)

    def test_index_reports_chunks(self):
        r = self._index_doc()
        self.assertGreater(r.chunks, 0)
        self.assertFalse(r.skipped)

    def test_search_returns_passages_not_the_whole_doc(self):
        self._index_doc()
        rep = kb.search("caching enabled", k=3)
        self.assertTrue(rep.hits)
        # The winning snippet is a window, far smaller than the document.
        self.assertLess(len(rep.hits[0].snippet), len(DOC))

    def test_stemming_finds_inflections(self):
        self._index_doc()
        # "caching" in the query, "cached"/"caching" in the doc — porter bridges.
        rep = kb.search("cached", k=3)
        self.assertTrue(rep.hits)

    def test_substring_finds_partial_word(self):
        self._index_doc()
        # "useEff" is a prefix of "useEffect"; only trigram can match it.
        rep = kb.search("useEff", k=3)
        self.assertTrue(rep.hits, "trigram half must catch a partial word")

    def test_typo_is_corrected(self):
        self._index_doc()
        rep = kb.search("kubernetse", k=3)  # transposed letters
        self.assertTrue(rep.hits)
        self.assertTrue(any("corrected" in n for n in rep.notes))

    def test_source_filter_restricts_results(self):
        kb.index_text("one", "# A\n\nneedle here", markdown=True)
        kb.index_text("two", "# B\n\nneedle there", markdown=True)
        rep = kb.search("needle", k=5, source="one")
        self.assertTrue(all("one" in h.source for h in rep.hits))

    def test_empty_index_is_a_clear_error(self):
        with self.assertRaises(kb.KBError) as ctx:
            kb.search("anything")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_query_with_fts_syntax_chars_does_not_crash(self):
        self._index_doc()
        # A raw "-" or ":" is FTS5 syntax; it must be quoted, not passed through.
        rep = kb.search("caching: enabled -cached", k=3)
        self.assertIsInstance(rep.hits, list)

    def test_stats_and_list(self):
        self._index_doc()
        s = kb.stats()
        self.assertGreater(s["chunks"], 0)
        self.assertEqual(s["sources"], 1)
        rows = kb.list_sources()
        self.assertEqual(rows[0]["source"], "doc://test")

    def test_purge_empties_the_base(self):
        self._index_doc()
        n = kb.purge()
        self.assertGreater(n, 0)
        self.assertEqual(kb.stats()["chunks"], 0)


class FetchCacheTests(KBBase):
    def test_cache_hit_skips_the_network(self):
        calls = {"n": 0}

        def fake_get(url, timeout=30):
            calls["n"] += 1
            return "<html><body><h1>Title</h1><p>body text about widgets</p></body></html>", "text/html"

        with mock.patch.object(kb, "_http_get", fake_get):
            first = kb.fetch_and_index("https://example.com/x", ttl=3600)
            self.assertFalse(first.skipped)
            self.assertEqual(calls["n"], 1)

            second = kb.fetch_and_index("https://example.com/x", ttl=3600)
            self.assertTrue(second.skipped)      # served from the index
            self.assertEqual(calls["n"], 1)      # no second network call

    def test_force_bypasses_the_cache(self):
        calls = {"n": 0}

        def fake_get(url, timeout=30):
            calls["n"] += 1
            return "<html><body><p>content</p></body></html>", "text/html"

        with mock.patch.object(kb, "_http_get", fake_get):
            kb.fetch_and_index("https://example.com/y", ttl=3600)
            kb.fetch_and_index("https://example.com/y", ttl=3600, force=True)
            self.assertEqual(calls["n"], 2)

    def test_source_label_separates_same_url(self):
        def fake_get(url, timeout=30):
            return "<html><body><p>text</p></body></html>", "text/html"

        with mock.patch.object(kb, "_http_get", fake_get):
            a = kb.fetch_and_index("https://example.com/z", source="A")
            b = kb.fetch_and_index("https://example.com/z", source="B")
            self.assertNotEqual(a.source, b.source)


class HtmlTests(unittest.TestCase):
    def test_script_and_style_are_dropped(self):
        text = kb.html_to_text(
            "<html><head><style>.x{}</style></head>"
            "<body><script>alert(1)</script><h1>Hi</h1><p>Body</p></body></html>"
        )
        self.assertIn("Hi", text)
        self.assertIn("Body", text)
        self.assertNotIn("alert", text)
        self.assertNotIn(".x{}", text)

    def test_headings_become_markdown(self):
        text = kb.html_to_text("<h2>Section</h2><p>text</p>")
        self.assertIn("## Section", text)


if __name__ == "__main__":
    unittest.main()
