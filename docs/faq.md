# Troubleshooting & FAQ

Short answers to the errors people actually hit. Each entry is the message you
see, why it happens, and the one command that fixes it. If your problem is not
here, `mcptoon doctor` self-diagnoses config and connectivity, and
`mcptoon status` shows what is configured and what it saves.

---

## `Server 'X' not configured`

You called a server mcptoon does not know about. The most common case is the
tutorial example:

```
$ mcptoon call everything echo '{"message":"hi"}'
Error: Server 'everything' not configured
```

`everything` (the reference MCP server) is only present if **you** added it —
`mcptoon quickstart` never installs it. Either add it (needs Node):

```bash
mcptoon add everything --stdio npx -y @modelcontextprotocol/server-everything
```

…or use the built-in demo server, which ships inside mcptoon, needs no Node and
no network, and always lists its 11 tools:

```bash
mcptoon add demo --stdio python -m mcptoon demo-server
mcptoon call demo echo_message '{"message":"hi"}'
```

Check what you do have with `mcptoon list`.

---

## `mcptoon skills sync` stops with "skill(s) are in git HEAD but missing"

```
🛑 mis-delete gate: 3 skill(s) are in git HEAD but missing from <source> — undecided removal
   restore them (git checkout -- <path>), or, if the removal is real, delete from
   the source on purpose (`mcptoon skills remove <name>`) and re-run; pass
   --allow-tracked-deletes to proceed anyway
```

This is a safety gate, not an error. A skill that is still committed in git but
gone from your working tree usually means an accidental deletion, so `sync`
refuses to propagate it to every agent folder. Pick one:

- **It was a mistake** — restore it: `git checkout -- <path>`, then re-run.
- **The removal is intentional** — retire it properly:
  `mcptoon skills remove <name>`, then re-run.
- **You know what you are doing** — pass the flag to override the gate:
  `mcptoon skills sync --allow-tracked-deletes`.

Preview first with `mcptoon skills sync --dry`; a dry run never writes and never
exits non-zero for this gate.

---

## `mcptoon <typo>` says "unknown command" and exits 1

```
$ mcptoon notacommand
mcptoon: unknown command 'notacommand'
Run 'mcptoon help' for the command list, or 'mcptoon --help'.
$ echo $?
1
```

This is correct behaviour: an unknown command is a failure, so it goes to stderr
and exits non-zero — a bad step in a script or `&&` chain stops instead of
looking green. Run `mcptoon help` to see the real command list. (Before
2026-10-17 this printed to stdout and exited 0, which hid the mistake.)

---

## `unknown option '--xyz'` and exit 2

```
$ mcptoon manifest --jsn
mcptoon: unknown option '--jsn' - see 'mcptoon --help' for the flags this version accepts
$ echo $?
2
```

mcptoon refuses an option it does not implement rather than silently ignoring it,
so a typo like `--jsn` (for `--json`) cannot quietly produce default output. Fix
the spelling. If you are piping a *newer* flag through an *older* CLI and only
want a warning, set `MCPTOON_STRICT_FLAGS=0` to downgrade it.

---

## The footer / `status` shows "省 99%" (or a number lower than I expected)

Savings percentages are **floored**, never rounded up. 640,834 → 2,499 tokens is
99.61% saved, shown as **99%**, not a rounded "100%". A "100%" would claim the
payload is free, which is never true. The tools line, `status`, `welcome`,
`report` and `bench` all use this one rule, so they agree with each other. The
skills line keeps two decimals (99.99%) for the same reason.

To see the exact pair behind any figure, run `mcptoon report` or
`mcptoon footer-facts --json`.

---

## `status` says 464 skills but the footer says 469

`status` reads the built skills **index** (fast, no filesystem walk); the footer
and `bench` re-measure the **live** catalog. When the index is behind, `status`
now says so:

```
  Skills in catalog  : 464 (recursive, deduped)  [skills index]
                       (live catalog has 469 — index may be stale, run `mcptoon skills index`)
```

Run `mcptoon skills index` to rebuild it. The counts then agree.

---

## `mcptoon select "<task>"` finds nothing

`select` always exits 0. If no tool matches — common when the query is in one
language and the tool descriptions are in another — it prints the tips instead of
crashing:

```
No tools found matching '<task>'.
  ...
  Try: mcptoon install --search '<task>'
```

Broaden the query, or install a server that covers the task with
`mcptoon install --search "<query>"`.

---

## Still stuck

1. `mcptoon doctor` — self-diagnose config and connectivity.
2. `mcptoon status` — what is configured and what it saves.
3. `mcptoon --version` — confirm which version you are running.
4. File an issue with the exact command and output:
   <https://github.com/activeing123/mcptoon/issues>
