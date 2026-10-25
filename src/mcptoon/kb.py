# Copyright 2025-2026 activeing123
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""mcptoon kb — search *inside* documents, not just which document to open.

Why this exists, and how it differs from ``mcptoon docs``
---------------------------------------------------------
``docs`` answers "which FILE holds the answer" — it ranks whole files by their
headings and returns paths. That is the right tool when you have nineteen
chapters and want to open one. It is the wrong tool when you have one 95 KB
README and need the four lines about reciprocal rank fusion: ``docs`` hands you
the file, and you still read 95 KB to find them.

``kb`` answers "which PASSAGE". It chunks the text, stores the chunks in SQLite
FTS5, and returns the matching windows. The document never enters the context
whole; only the passages that matched do. Measured on this project's own
context-mode README: 94,980 bytes on disk → the 3 relevant snippets, ~900 bytes.

Why two indexes instead of one
------------------------------
Neither tokenizer is enough alone (both failures reproduced here):

* **porter** stems words, so "caching" finds "cached"/"caches". But it cannot
  match a *fragment*: "useEff" does not find "useEffect".
* **trigram** matches substrings, so "useEff" finds "useEffect". But it has no
  notion of word boundaries, so it is noisy on prose.

Running both and merging with **Reciprocal Rank Fusion** gives a document that
ranks well in *either* strategy a place near the top, instead of a cascade where
the second index only runs when the first returns nothing. RRF is rank-based, so
the two indexes never have to agree on a score scale.

Everything here is standard library: ``sqlite3`` (FTS5 ships with CPython on
Windows/macOS/Linux), ``urllib`` for fetching, ``html.parser`` for HTML. Zero
dependencies, same as the rest of the package.

Honest limits, stated rather than hidden
----------------------------------------
* **FTS5 must be present.** It is in CPython's bundled SQLite on every platform
  this project targets, but ``kb`` checks and says so plainly if it is not,
  instead of dying with an ``OperationalError``.
* **HTML → text is a flattening, not a conversion.** No dependency means no real
  HTML-to-Markdown; tags become whitespace and entities are unescaped. Tables and
  nested lists lose their structure. For a docs page that is fine; for a table
  you actually need, fetch the page and read it.
* **Trigram needs SQLite >= 3.34.** Below that the substring half is skipped and
  search degrades to porter-only, which the result carries as a note.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from collections.abc import Iterable
from typing import Any

# ─── Defaults ───

#: A fetched page is reused for this long before re-fetching. Long enough that a
#: session re-reading the same spec does not hit the network twice; short enough
#: that a changelog does not go stale for a week.
DEFAULT_TTL = 24 * 3600

#: Rows older than this are dropped on write. A content index is a cache, not an
#: archive — keeping it bounded is what stops it growing into a second repo.
RETENTION_DAYS = 14

#: Chunks bigger than this are split on paragraph boundaries. A 40 KB single
#: heading with no subheadings is common in generated docs and would otherwise
#: be one unusable chunk.
MAX_CHUNK_CHARS = 4000

#: Chars of context returned around a hit.
SNIPPET_BEFORE = 120
SNIPPET_AFTER = 260

#: RRF's rank constant. 60 is the value from the original paper (Cormack et al.,
#: 2009) and what every RRF implementation in the wild uses; changing it changes
#: how sharply top ranks dominate.
RRF_K = 60


class KBError(Exception):
    """A problem the caller can fix (no FTS5, missing index, bad URL)."""


# ─── Storage ───

def _kb_dir() -> Path:
    override = os.environ.get("MCPTOON_KB_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("MCPTOON_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".mcptoon"
    return base / "kb"


def _db_path() -> Path:
    return _kb_dir() / "content.sqlite"


def _have_fts5(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        con.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _have_trigram(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE temp.__tri_probe USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE temp.__tri_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    if not _have_fts5(con):
        con.close()
        raise KBError(
            "this Python's SQLite has no FTS5, so `mcptoon kb` cannot index. "
            f"sqlite {sqlite3.sqlite_version}. Use `mcptoon docs` (whole-file "
            "routing, no FTS5 needed) or a Python built against SQLite with FTS5."
        )
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            doc_id     INTEGER PRIMARY KEY,
            source     TEXT NOT NULL,
            title      TEXT NOT NULL DEFAULT '',
            ord        INTEGER NOT NULL DEFAULT 0,
            body       TEXT NOT NULL,
            fetched_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
        """
    )
    # FTS5 external-content tables mirror `chunks`. External content keeps one
    # copy of the text on disk instead of three (chunks + 2 FTS tables).
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_porter USING fts5("
        "title, body, content='chunks', content_rowid='doc_id', tokenize='porter')"
    )
    if _have_trigram(con):
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5("
            "title, body, content='chunks', content_rowid='doc_id', tokenize='trigram')"
        )
    con.commit()
    return con


def _rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute("INSERT INTO chunks_porter(chunks_porter) VALUES('rebuild')")
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_tri'"
    ).fetchone()
    if row:
        con.execute("INSERT INTO chunks_tri(chunks_tri) VALUES('rebuild')")
    con.commit()


# ─── Chunking ───

def _split_long(text: str, limit: int) -> list[str]:
    """Split an over-long chunk on paragraph boundaries, then hard-wrap."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        if size + len(para) + 2 > limit and buf:
            out.append("\n\n".join(buf))
            buf, size = [], 0
        if len(para) > limit:
            # A single paragraph longer than the limit: hard-wrap it.
            if buf:
                out.append("\n\n".join(buf))
                buf, size = [], 0
            for i in range(0, len(para), limit):
                out.append(para[i:i + limit])
            continue
        buf.append(para)
        size += len(para) + 2
    if buf:
        out.append("\n\n".join(buf))
    return out


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Split markdown into ``(title, body)`` chunks at headings, code fences intact.

    Fences matter: a ``# comment`` inside a ``` block is not a heading, and
    splitting on it would slice a code sample in half. The fence flag is the
    whole reason this is a loop and not a regex split.
    """
    lines = text.splitlines()
    chunks: list[tuple[str, str]] = []
    title = ""
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if not buf:
            return
        body = "\n".join(buf).strip("\n")
        if not body.strip():
            return
        for piece in _split_long(body, max_chars):
            chunks.append((title, piece))

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and re.match(r"^#{1,6}\s", line):
            flush()
            buf = []
            title = line.lstrip("#").strip()
            buf.append(line)
            continue
        buf.append(line)
    flush()
    return chunks


def chunk_text(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Plain-text chunking: fixed windows on paragraph boundaries, no titles."""
    out: list[tuple[str, str]] = []
    for piece in _split_long(text, max_chars):
        out.append(("", piece))
    return out


# ─── HTML → text (flattening; see the module docstring's limits) ───

class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BLOCK = {
        "p", "div", "section", "article", "header", "footer", "main", "li",
        "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
        "table", "ul", "ol", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = html.unescape(raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        # A malformed page should still yield whatever was parsed before the
        # break — a partial index beats a hard failure on a real-world page.
        pass
    return p.text()


# ─── Indexing ───

@dataclass
class IndexReport:
    source: str
    chunks: int
    bytes: int
    skipped: bool = False
    reason: str | None = None
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "source": self.source, "chunks": self.chunks,
            "bytes": self.bytes, "skipped": self.skipped,
        }
        if self.reason:
            d["reason"] = self.reason
        if self.notes:
            d["notes"] = self.notes
        return d


def _sweep(con: sqlite3.Connection) -> int:
    cutoff = time.time() - RETENTION_DAYS * 86400
    cur = con.execute("DELETE FROM chunks WHERE fetched_at < ?", (cutoff,))
    removed = cur.rowcount or 0
    if removed:
        con.commit()
        _rebuild_fts(con)
    return removed


def index_text(source: str, text: str, *, title: str = "",
               markdown: bool = True) -> IndexReport:
    """Replace every chunk for ``source`` with the chunks of ``text``."""
    con = _connect()
    try:
        chunks = chunk_markdown(text) if markdown else chunk_text(text)
        if not chunks:
            return IndexReport(source, 0, len(text.encode("utf-8")),
                               skipped=True, reason="empty document")
        con.execute("DELETE FROM chunks WHERE source = ?", (source,))
        now = time.time()
        rows = []
        for i, (t, body) in enumerate(chunks):
            rows.append((source, t or title, i, body, now))
        con.executemany(
            "INSERT INTO chunks(source, title, ord, body, fetched_at) "
            "VALUES(?,?,?,?,?)", rows,
        )
        con.commit()
        _rebuild_fts(con)
        removed = _sweep(con)
        notes = []
        if removed:
            notes.append(f"dropped {removed} chunk(s) older than {RETENTION_DAYS} days")
        return IndexReport(source, len(chunks), len(text.encode("utf-8")), notes=notes)
    finally:
        con.close()


def index_file(path: str, *, source: str | None = None) -> IndexReport:
    p = Path(path).expanduser()
    if not p.is_file():
        raise KBError(f"file not found: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    is_md = p.suffix.lower() in (".md", ".markdown", ".mdx")
    return index_text(source or str(p), text, markdown=is_md)


# ─── Fetch + TTL cache ───

def _cache_key(source: str | None, url: str) -> str:
    # Two URLs sharing a user label must not collide; a label-less URL is its
    # own key. Same composition as context-mode's fetch cache, for the same
    # reason: indexing dedups by source, so the source must be unique per URL.
    return url if source is None else f"{source}::{url}"


def _source_ts(con: sqlite3.Connection, source: str) -> float | None:
    row = con.execute(
        "SELECT MAX(fetched_at) FROM chunks WHERE source = ?", (source,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _http_get(url: str, timeout: int = 30) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "mcptoon-kb/1.0 (+https://github.com/activeing123/mcptoon)",
        "Accept": "text/html,text/plain,text/markdown;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="replace"), ctype


def fetch_and_index(url: str, *, source: str | None = None,
                    ttl: int = DEFAULT_TTL, force: bool = False) -> IndexReport:
    """Fetch ``url``, convert to text, index it. Reuses the index within ``ttl``.

    A cache hit does no network work at all — the point of the cache is that the
    second read of a spec costs neither a fetch nor the page's bytes in context.
    """
    key = _cache_key(source, url)
    con = _connect()
    try:
        if not force and ttl > 0:
            ts = _source_ts(con, key)
            if ts is not None and (time.time() - ts) < ttl:
                n = con.execute(
                    "SELECT COUNT(*) FROM chunks WHERE source = ?", (key,)
                ).fetchone()[0]
                age = int(time.time() - ts)
                return IndexReport(key, n, 0, skipped=True,
                                   reason=f"cache hit ({age}s old, ttl {ttl}s)")
    finally:
        con.close()

    try:
        body, ctype = _http_get(url)
    except urllib.error.HTTPError as exc:
        raise KBError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise KBError(f"network error fetching {url}: {exc.reason}") from exc
    except Exception as exc:  # timeout, ssl, ...
        raise KBError(f"could not fetch {url}: {exc}") from exc

    is_html = "html" in ctype.lower() or body.lstrip()[:15].lower().startswith("<!doctype")
    text = html_to_text(body) if is_html else body
    return index_text(key, text, markdown=not is_html)


# ─── Search ───

def _levenshtein(a: str, b: str, cap: int = 2) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _vocabulary(con: sqlite3.Connection, limit: int = 4000) -> set[str]:
    """Distinct words in the index, for typo correction.

    Bounded: a huge index would make every query O(vocabulary). 4,000 words
    covers the real cases (a typo in a word that appears in the corpus) without
    scanning a 200k-word index on every keystroke.
    """
    words: set[str] = set()
    for (body,) in con.execute("SELECT body FROM chunks LIMIT 2000"):
        for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", body.lower()):
            words.add(w)
            if len(words) >= limit:
                return words
    return words


def _correct_terms(terms: Iterable[str], vocab: set[str]) -> tuple[list[str], list[str]]:
    fixed: list[str] = []
    corrections: list[str] = []
    for t in terms:
        low = t.lower()
        if low in vocab or len(low) < 4:
            fixed.append(t)
            continue
        best, best_d = None, 3
        for w in vocab:
            if abs(len(w) - len(low)) > 2:
                continue
            d = _levenshtein(low, w)
            if d < best_d:
                best, best_d = w, d
                if d == 1:
                    break
        if best and best_d <= 2:
            fixed.append(best)
            corrections.append(f"{t}→{best}")
        else:
            fixed.append(t)
    return fixed, corrections


def _fts_query(terms: list[str]) -> str:
    """Build a safe FTS5 MATCH expression: quoted terms, OR-ed.

    Quoting is not cosmetic: a raw user query containing ``-`` or ``:`` is
    FTS5 syntax (column filter, NOT operator) and would either error or silently
    change meaning. Every term becomes a quoted string; prefix matching is added
    on the last term so "kubernet" finds "kubernetes" while typing.
    """
    clean = []
    for t in terms:
        t = t.replace('"', "")
        if t.strip():
            clean.append(f'"{t}"')
    if not clean:
        return ""
    # Last term gets a prefix star so a partial word still matches.
    clean[-1] = clean[-1] + "*"
    return " OR ".join(clean)


def _snippet(body: str, terms: list[str]) -> str:
    """A window around the first term hit — not a head truncation.

    Truncating the head of a long chunk is how you return the table of contents
    instead of the answer. Finding where the query actually appears and taking a
    window around it is the difference between a snippet and a teaser.
    """
    low = body.lower()
    pos = -1
    for t in terms:
        p = low.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return body[:SNIPPET_BEFORE + SNIPPET_AFTER].strip()
    start = max(0, pos - SNIPPET_BEFORE)
    end = min(len(body), pos + SNIPPET_AFTER)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return (prefix + body[start:end].strip() + suffix)


@dataclass
class Hit:
    source: str
    title: str
    snippet: str
    score: float
    ord: int

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "title": self.title,
                "snippet": self.snippet, "score": round(self.score, 5),
                "ord": self.ord}


@dataclass
class SearchReport:
    query: str
    hits: list[Hit]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query,
                "hits": [h.as_dict() for h in self.hits],
                "notes": self.notes}


def search(query: str, *, k: int = 5, source: str | None = None) -> SearchReport:
    """Ranked passages for ``query`` via porter + trigram, fused with RRF."""
    terms = [t for t in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", query) if t]
    if not terms:
        raise KBError("query has no searchable terms")
    if k <= 0:
        raise KBError("k must be positive")

    con = _connect()
    notes: list[str] = []
    try:
        if not con.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
            raise KBError("the knowledge base is empty. Index something first: "
                          "mcptoon kb index <path>  |  mcptoon kb fetch <url>")

        vocab = _vocabulary(con)
        terms, corrections = _correct_terms(terms, vocab)
        if corrections:
            notes.append("corrected: " + ", ".join(corrections))

        expr = _fts_query(terms)
        if not expr:
            raise KBError("query has no searchable terms")

        src_clause = ""
        params: list[Any] = [expr]
        if source:
            src_clause = " AND c.source LIKE ?"
            params.append(f"%{source}%")

        porter_rows = con.execute(
            "SELECT c.doc_id FROM chunks_porter f JOIN chunks c ON c.doc_id = f.rowid "
            "WHERE chunks_porter MATCH ?" + src_clause +
            " ORDER BY bm25(chunks_porter, 5.0, 1.0) LIMIT 50",
            params,
        ).fetchall()

        tri_rows: list[tuple] = []
        has_tri = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_tri'"
        ).fetchone()
        if has_tri:
            tri_rows = con.execute(
                "SELECT c.doc_id FROM chunks_tri f JOIN chunks c ON c.doc_id = f.rowid "
                "WHERE chunks_tri MATCH ?" + src_clause +
                " ORDER BY bm25(chunks_tri, 5.0, 1.0) LIMIT 50",
                params,
            ).fetchall()
        else:
            notes.append("trigram index unavailable (SQLite < 3.34): substring "
                         "matching skipped, porter only")

        # Reciprocal Rank Fusion: score = Σ 1/(K + rank). Rank-based, so the two
        # indexes never need a shared score scale.
        fused: dict[int, float] = {}
        for rows in (porter_rows, tri_rows):
            for rank, (doc_id,) in enumerate(rows):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        # Proximity rerank: a passage where the query terms sit close together is
        # a better answer than one where they are pages apart.
        for doc_id in list(fused):
            body = con.execute(
                "SELECT body FROM chunks WHERE doc_id = ?", (doc_id,)
            ).fetchone()[0]
            low = body.lower()
            positions = [low.find(t.lower()) for t in terms]
            if all(p >= 0 for p in positions):
                span = max(positions) - min(positions)
                fused[doc_id] += 1.0 / (RRF_K + 1 + span / 200.0)

        top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        hits: list[Hit] = []
        for doc_id, score in top:
            row = con.execute(
                "SELECT source, title, body, ord FROM chunks WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if not row:
                continue
            src, title, body, ordn = row
            hits.append(Hit(src, title, _snippet(body, terms), score, ordn))
        return SearchReport(query, hits, notes)
    finally:
        con.close()


# ─── Housekeeping ───

def list_sources() -> list[dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT source, COUNT(*) AS n, SUM(LENGTH(body)) AS chars, "
            "MAX(fetched_at) AS ts FROM chunks GROUP BY source ORDER BY source"
        ).fetchall()
        return [{"source": s, "chunks": n, "chars": chars or 0,
                 "age_seconds": int(time.time() - ts)} for s, n, chars, ts in rows]
    finally:
        con.close()


def stats() -> dict[str, Any]:
    con = _connect()
    try:
        n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_sources = con.execute(
            "SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        chars = con.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0) FROM chunks").fetchone()[0]
        db_bytes = _db_path().stat().st_size if _db_path().exists() else 0
        has_tri = bool(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_tri'"
        ).fetchone())
        return {"chunks": n_chunks, "sources": n_sources, "chars": chars,
                "db_bytes": db_bytes, "db": str(_db_path()),
                "trigram": has_tri, "retention_days": RETENTION_DAYS}
    finally:
        con.close()


def purge() -> int:
    con = _connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        con.execute("DELETE FROM chunks")
        con.commit()
        _rebuild_fts(con)
        return n
    finally:
        con.close()


# ─── CLI ───

_KB_USAGE = """mcptoon kb — search inside documents, not just which file to open

Usage:
    mcptoon kb index <path> [<path> ...]   Index markdown/text files
    mcptoon kb fetch <url> [--source L]    Fetch a URL, index it (24h TTL cache)
    mcptoon kb search "<query>"            Ranked passages (porter + trigram, RRF)
    mcptoon kb list                        Indexed sources, with size and age
    mcptoon kb stats                       Chunk count, DB size, trigram support
    mcptoon kb purge                       Delete everything (--yes to confirm)

Options:
    --source <label>   Name an indexed source; two URLs sharing a label are distinct
    --ttl <seconds>    Cache lifetime for `fetch` (default 86400; 0 = always fetch)
    --force            Re-fetch even inside the TTL window
    --k <n>            Results for `search` (default 5)
    --source <label>   Also: restrict `search` to one source (substring match)
    --json             Machine-readable output

`kb` differs from `docs`: `docs` returns the FILE that answers a question, `kb`
returns the PASSAGES. Use `docs` to pick one chapter out of nineteen; use `kb`
to pull four lines out of one large document without reading the rest.
"""


def _flag(rest: list[str], name: str, default: str = "") -> str:
    for i, a in enumerate(rest):
        if a == name and i + 1 < len(rest):
            return rest[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _positional(rest: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for i, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a in ("--source", "--ttl", "--k"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: ``mcptoon kb <action> [...]``."""
    action = rest[0] if rest else ""
    args = rest[1:]

    if action in ("", "help", "-h", "--help"):
        print(_KB_USAGE)
        return

    try:
        if action == "index":
            paths = _positional(args)
            if not paths:
                print("mcptoon kb: index needs at least one path", file=sys.stderr)
                sys.exit(1)
            reports = [index_file(p) for p in paths]
            if fmt == "json":
                print(json.dumps([r.as_dict() for r in reports],
                                 ensure_ascii=False, indent=1))
                return
            total = sum(r.chunks for r in reports)
            for r in reports:
                print(f"  {r.chunks:>5} chunks  {r.bytes:>9} B  {r.source}")
            print(f"indexed {total} chunk(s) from {len(reports)} source(s)")
            return

        if action == "fetch":
            urls = _positional(args)
            if not urls:
                print("mcptoon kb: fetch needs a URL", file=sys.stderr)
                sys.exit(1)
            source = _flag(args, "--source") or None
            ttl_raw = _flag(args, "--ttl", "")
            ttl = int(ttl_raw) if ttl_raw.isdigit() else DEFAULT_TTL
            force = "--force" in args
            reports = [fetch_and_index(u, source=source, ttl=ttl, force=force)
                       for u in urls]
            if fmt == "json":
                print(json.dumps([r.as_dict() for r in reports],
                                 ensure_ascii=False, indent=1))
                return
            for r in reports:
                if r.skipped:
                    print(f"  cached ({r.chunks} chunks)  {r.source}  — {r.reason}")
                else:
                    print(f"  {r.chunks:>5} chunks  {r.bytes:>9} B  {r.source}")
            return

        if action in ("search", "find", "q"):
            query = _flag(args, "--query", "") or " ".join(_positional(args))
            if not query:
                print("mcptoon kb: search needs a query", file=sys.stderr)
                sys.exit(1)
            k_raw = _flag(args, "--k", "")
            k = int(k_raw) if k_raw.isdigit() else 5
            source = _flag(args, "--source") or None
            report = search(query, k=k, source=source)
            if fmt == "json":
                print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
                return
            if not report.hits:
                print(f"no passage matches {query!r}")
            for i, h in enumerate(report.hits, 1):
                head = f"{i}. {h.source}"
                if h.title:
                    head += f"  ·  {h.title}"
                print(head)
                for line in h.snippet.splitlines():
                    print(f"   {line}")
                print()
            for note in report.notes:
                print(f"note: {note}", file=sys.stderr)
            return

        if action == "list":
            rows = list_sources()
            if fmt == "json":
                print(json.dumps(rows, ensure_ascii=False, indent=1))
                return
            if not rows:
                print("nothing indexed yet. mcptoon kb index <path>")
                return
            for r in rows:
                print(f"  {r['chunks']:>5} chunks  {r['chars']:>9} chars  "
                      f"{r['age_seconds']:>7}s old  {r['source']}")
            return

        if action == "stats":
            s = stats()
            if fmt == "json":
                print(json.dumps(s, ensure_ascii=False, indent=1))
                return
            print(f"sources:  {s['sources']}")
            print(f"chunks:   {s['chunks']}")
            print(f"chars:    {s['chars']:,}")
            print(f"db:       {s['db']}  ({s['db_bytes']:,} bytes)")
            print(f"trigram:  {'yes' if s['trigram'] else 'no (porter only)'}")
            print(f"retention:{s['retention_days']} days")
            return

        if action in ("purge", "clear", "reset"):
            if "--yes" not in args and "-y" not in args:
                s = stats()
                print(f"would delete {s['chunks']} chunk(s) from {s['sources']} "
                      f"source(s). Re-run with --yes to confirm.")
                return
            n = purge()
            print(f"purged {n} chunk(s)")
            return

    except KBError as exc:
        print(f"mcptoon kb: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"mcptoon kb: unknown action {action!r}", file=sys.stderr)
    print(_KB_USAGE, file=sys.stderr)
    sys.exit(1)
