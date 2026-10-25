# Real-world benchmark: 7 public MCP servers, measured on a real machine

The headline number in the README (99.2% on 255 tools) comes from a **synthetic
sample** — a fixed catalog assembled to be representative. That is a fair way to
compare formats, but a skeptical reader is right to ask: *does it hold on servers
that actually exist?*

This page answers that with a snapshot measured on a real machine against **real,
publicly available MCP servers**, not a synthetic catalog.

## The measurement

Source: `mcptoon bench` reading mcptoon's own schema cache, after `mcptoon manifest`
had fetched the schemas of the servers configured on that machine. mcptoon's own
bundled test servers (`demo`, `echo`) are **excluded** — only third-party servers
count here.

| Server | Tools |
|---|---:|
| `rea` (rea-agents) | 170 |
| `filesystem` (@modelcontextprotocol/server-filesystem) | 14 |
| `git` (@modelcontextprotocol/server-git) | 12 |
| `memory` (@modelcontextprotocol/server-memory) | 9 |
| `time` (@modelcontextprotocol/server-time) | 2 |
| `fetch` (mcp-server-fetch) | 1 |
| `sequential-thinking` (@modelcontextprotocol/server-sequential-thinking) | 1 |
| **Total** | **209** |

| What the agent loads | Tokens (cl100k_base) | vs native |
|---|---:|---:|
| Native: every full JSON schema | 640,606 | — |
| **`mcptoon manifest` (name index)** | **905** | **−99.86%** |
| `mcptoon manifest --slim` (name + params) | 3,035 | −99.53% |

So on 209 tools from seven real servers, the name index is **905 tokens** where the
full schemas are **640,606** — the same ~99.9% saving the synthetic sample shows,
measured on servers that exist in the wild. The absolute total is dominated by `rea`
(170 tools); the percentage is not sensitive to that because both the numerator and
the denominator scale with the same catalog.

## Reproduce it on your own machine

```bash
pip install mcptoon
mcptoon manifest        # fetch + cache the schemas of your configured servers
mcptoon bench           # prints the same table for YOUR catalog
```

`mcptoon bench` measures **your** machine — your servers, your tool count — not this
snapshot. If you have not configured any servers yet, `mcptoon add demo --stdio python
-m mcptoon demo-server` gives you an 11-tool server to measure against.

## Calibers (what these numbers do and do not mean)

- **Token counts** are real `tiktoken` `cl100k_base` counts of the exact strings the
  agent would receive — not an estimate.
- **"Native"** is the sum over every tool of its full JSON (name + description +
  input schema + annotations), the same caliber `mcptoon status` reports.
- **"Name index"** is the compact `manifest` view the agent actually loads each turn;
  full schemas stay on disk and are fetched on demand with `mcptoon inspect`.
- These figures measure **tool discovery cost**, not the cost of a tool's *output*.
  Result compression is a separate, also-lossless mechanism.
- The snapshot is one machine on one date; your absolute numbers will differ. The
  *ratio* is what generalizes, and it is what the table is for.
