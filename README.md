# auto-memory for Claude Code

A read-only recall layer over Claude Code's session transcripts, packaged as a Claude Code Skill.

Inspired by [I wasted 68 minutes a day re-explaining my code, then I built auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/) — same idea, ported from GitHub Copilot CLI to Claude Code.

## What it does

Indexes every `~/.claude/projects/*/*.jsonl` (Claude Code's per-session transcript files) into a SQLite cache with FTS5 full-text search, then exposes a `auto-memory` CLI to query it. The bundled SKILL teaches Claude Code to invoke the CLI automatically when it needs prior context — so you stop re-explaining what you were working on.

**Read-only.** Never writes to the source jsonl files. The index lives in `~/.claude/auto-memory/index.db`.

**Stdlib only.** Single Python file, no dependencies.

## Tiered queries

Following the original article's design, queries are tiered by token cost:

| Tier | Command | Purpose | ~Tokens |
|------|---------|---------|---------|
| 1 | `auto-memory list --cwd "$PWD"` | Recent sessions in this project | ~50 |
| 1 | `auto-memory files --cwd "$PWD"` | Recently-touched files | ~100 |
| 2 | `auto-memory search "term" --cwd "$PWD"` | Full-text recall | ~200 |
| 3 | `auto-memory show <id-prefix>` | Full session detail | ~500+ |

The skill instructs Claude Code to start at Tier 1 and escalate only when needed.

## Install

```bash
git clone https://github.com/briandilley/auto-memory.git ~/auto-memory

# Make the CLI available on your PATH
ln -s ~/auto-memory/auto_memory.py ~/.local/bin/auto-memory

# Install the skill so Claude Code can discover it
ln -s ~/auto-memory/auto-memory ~/.claude/skills/auto-memory
```

Verify:

```bash
auto-memory health
```

You should see session/message/file-access counts climb after Claude Code writes new sessions.

## CLI reference

```bash
auto-memory list    [--limit N] [--days N] [--cwd PATH]
auto-memory files   [--limit N] [--days N] [--cwd PATH] [--contains STR]
auto-memory search  QUERY [--limit N] [--days N] [--cwd PATH] [--role user|assistant] [--raw]
auto-memory show    SESSION_ID [--limit-messages N] [--limit-files N]
auto-memory health
auto-memory reindex [--verbose]
```

Global flags:
- `--json` — machine-readable output
- `--no-reindex` — skip the incremental index pass on this invocation

`SESSION_ID` accepts an 8-character prefix.

By default, plain text in `search` is sanitized to a safe FTS5 query. Pass `--raw` if you want to use FTS5 operators directly (`AND`, `OR`, `NEAR`, column filters, etc.).

## How it differs from the article's tool

- **Source data:** Claude Code stores per-session jsonl files (one file per session) instead of Copilot CLI's single SQLite `session-store.db`. This tool indexes those jsonls into its own SQLite cache.
- **Same surface:** `list` / `files` / `search` / `show` / `health` mirror the original tiered design.
- **Same constraints:** stdlib only, read-only with respect to source data, WAL-safe for concurrent Claude Code processes.

## What's stored

| Kind | Captured |
|------|----------|
| Sessions | id, project dir, cwd, git branch, started/ended timestamps, message count, first prompt |
| Messages | session id, index, timestamp, role, flattened text (FTS-indexed) |
| File accesses | session id, timestamp, tool name (Read/Edit/Write/MultiEdit/NotebookEdit), file path |

Not stored: vector embeddings, cross-machine sync, the user's environment/secrets.

## License

MIT — see [LICENSE](LICENSE).
