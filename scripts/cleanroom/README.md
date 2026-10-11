# Clean room — test mcptoon end to end without touching this machine

mcptoon reads and writes your agent config files. That is exactly the surface you
do not want a test to get wrong, and exactly the surface a unit test with fake
paths cannot check. The clean room closes that gap: it stands up a throwaway
`$HOME` / `$APPDATA` plus every `MCPTOON_*` path, seeds all twelve agent hosts
there, and drives the **real** `mcptoon` binary through the whole lifecycle —
while hashing this machine's real configs before and after to prove none moved.

## Run it

```powershell
# Tier 1 -- on this machine (Windows paths, the ones you actually use)
python scripts/cleanroom/run_cleanroom.py

# Keep the throwaway root around to poke at afterwards
python scripts/cleanroom/run_cleanroom.py --root .scratch/cleanroom

# Tier 2 -- the same assertions on Linux, in a container (other half of the path logic)
docker build -f scripts/cleanroom/Dockerfile -t mcptoon-cleanroom .
docker run --rm mcptoon-cleanroom
```

Exit code `0` = every check passed and this machine did not change. `1` = at
least one check failed; the report names which.

## What it checks

| # | Check | What it proves | What it does **not** prove |
|---|-------|----------------|----------------------------|
| 1 | seed 12 agent hosts | the seeder builds every host mcptoon knows about | — |
| 2 | `skills index` | the catalog index is written | — |
| 3 | `skills list` | the catalog is readable back | — |
| 4 | `skills resolve` | the right skill is found for a task | ranking quality (3 skills is too few to measure it) |
| 5 | `skills sync` | the catalog lands in every view root | a **real** host's view (the views are empty dirs here) |
| 6 | `skills doctor` | doctor exits 0 on a healthy room | doctor's *diagnosis* (the real machine's 413 skills are the live test) |
| 7 | `sync --self` → 6 JSON hosts | the gateway is written into every MCP host | a GUI program reading it (no GUI runs here) |
| 8 | `sync --self` → 7 pointer files | the pointer block is written into every CLI host | a CLI agent loading it (no agent runs here) |
| 9 | `status` | the gateway is reported present | — |
| 10 | `off` → 6 global + 1 project | the gateway is removed from every host, both legs | — |
| 11 | own servers identical | **reversibility**: every host's own servers survive `off` entry-for-entry | — |
| 12 | pointer blocks gone | the pointer text is fully stripped | — |
| 13 | no empty-file residue | a file created only for the pointer is deleted, not left 0 bytes | — |
| 14 | `off` idempotent | a second `off` changes nothing | — |
| 15 | `manifest` | a real stdio server answers `tools/list` through the proxy | a real *remote* server (the one here is local, 40 lines) |
| 16 | `call` | a real tool call round-trips through the proxy | auth, network, or a slow server |
| 17 | real machine untouched | sha256 of 11 sensitive real files is unchanged | files not on the watched list |

★ Row 17 is the safety claim. It is a **comparison of real bytes on disk**, not a
promise — if any watched file changes, the run fails and names it.

## The three files

- **`seed_agents.py`** — writes every host's stub config under a clean root. The
  paths come from mcptoon itself (`sync._paths_for`), not a second copy of the
  rules, so the stubs land where the product looks. Returns `{host: {shape, paths}}`.
- **`local_mcp_server.py`** — a real, tiny stdio MCP server (stdlib only) so
  `manifest` / `call` have something to talk to. Self-contained on purpose: the
  test suite's `tests/echo_server.py` is excluded from the Docker build context.
- **`run_cleanroom.py`** — seeds a room, runs the checks, prints the report, and
  proves the real machine is untouched. `--root` keeps the room for inspection.
- **`Dockerfile`** — Tier 2. Same suite, Linux path logic.

## Honest boundaries

The clean room is a **file-level** proof. It proves mcptoon reads and writes the
right config files, reversibly, on both platforms, without side effects. It does
**not** prove:

- that a real GUI program (Claude Desktop, Cursor, ...) consumes the config it
  reads — no GUI runs in the clean room;
- that a real remote MCP server behaves like the local stub;
- anything about a machine whose paths differ from this one (a host installed
  somewhere unexpected). Cross-machine differences are out of scope.

For those, the live check is the real machine: `mcptoon skills doctor` and a real
`mcptoon off` / `sync` on this box, which is what stage 0 does.

## A defect this already found

`seed_agents.py` first ran from the repo and wrote `.cursor/mcp.json` **into the
repo** — because Cursor's project-level path is `Path.cwd() / ".cursor/mcp.json"`.
The seeder now `chdir`s to a neutral directory inside the clean room. The same
class of bug (a `path=` argument that a helper ignored, writing the real file) was
what the `pointer=` override fixed in `sync.py` this same session.
