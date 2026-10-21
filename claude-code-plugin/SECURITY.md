# Security Policy — mcptoon Claude Code plugin

This directory is the **Claude Code plugin shell** for
[mcptoon](https://github.com/activeing123/mcptoon). The CLI itself is zero-dependency
Python (standard library only); the plugin only wires it into Claude Code.

## Supported Versions

The plugin tracks the CLI version it ships with. Only the version matching the
latest `mcptoon` release is supported.

| Version | Supported |
|---------|-----------|
| 0.8.x   | ✅ Active support |
| < 0.8   | ❌ Not supported |

## Reporting a Vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting:
go to the repository's **Security** tab → **Report a vulnerability**, or use
<https://github.com/activeing123/mcptoon/security/advisories/new>.

Include a description, reproduction steps, potential impact, and a suggested fix
if you have one. You will get a response within 48 hours; a confirmed
vulnerability is fixed within 7 days.

## What this plugin can reach

- It runs the `mcptoon` CLI, which talks **only** to MCP servers *you* configure.
- It stores no credentials; secrets are read from environment variables or the
  user-managed `~/.mcptoon/config.json` and passed straight through, never logged.
- The only files it writes live under `~/.mcptoon/` and `~/.cache/mcptoon/`.

The full threat model and local data-file table live in the repository's root
[`SECURITY.md`](https://github.com/activeing123/mcptoon/blob/main/SECURITY.md).
