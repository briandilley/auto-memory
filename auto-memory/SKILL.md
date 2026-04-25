---
name: auto-memory
description: Use at the start of any conversation, and whenever the user references prior work, prior conversations, files they've worked on before, or context that should already be known - retrieves relevant prior session context from Claude Code's transcript history via the `auto-memory` CLI so you don't have to ask the user to re-explain.
---

# auto-memory

A read-only recall layer over Claude Code's session transcripts. The `auto-memory` CLI indexes every `~/.claude/projects/*/*.jsonl` into a SQLite cache (FTS5) and exposes commands to list past sessions, surface recently-touched files, full-text search across history, and dump a specific session.

Inspired by [the Copilot CLI auto-memory pattern](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/), adapted to Claude Code.

**Read-only.** Never writes to the source jsonl files; the SQLite index lives in `~/.claude/auto-memory/`.

## Invocation

The CLI ships next to this SKILL.md. Invoke it via:

```bash
python3 ~/.claude/skills/auto-memory/auto_memory.py <subcommand> [...]
```

If the user has also symlinked it onto PATH (see install), the bare `auto-memory` command works too. The examples below use the bare form for readability — substitute the full `python3 ...` form if PATH isn't set up.

## When to Use

- **Start of any conversation** in a project that has prior history — get the lay of the land before responding.
- User references prior work: *"like we did before"*, *"the auth refactor"*, *"that script I wrote last week"*.
- User mentions a file you haven't seen yet — check whether it was touched in a previous session.
- User asks *"what was I working on?"* / *"where did we leave off?"*.
- Debugging a regression and you want to see when a file was last edited and in what context.

## When NOT to Use

- The user asked you to ignore prior context for this session.
- The current task is fully self-contained (one-off question, no project context needed).
- You already have the relevant context loaded in this conversation.

## Tiered Query Pattern

Use the cheapest tier that answers the question. Add `--cwd "$PWD"` to scope to the current project.

### Tier 1: Orientation (~50–150 tokens)

At conversation start in a project with history, run both:

```bash
auto-memory list  --limit 5  --cwd "$PWD"
auto-memory files --limit 10 --cwd "$PWD"
```

Tells you: recent sessions in this project (with first-prompt previews) and recently-touched files. Often enough.

### Tier 2: Focused Recall (~200–400 tokens)

When the user references something specific:

```bash
auto-memory search "auth migration" --days 30 --cwd "$PWD" --limit 5
```

Returns ranked snippets with session id + message index. Plain text — no need to know FTS5 syntax (the CLI sanitizes by default; pass `--raw` for advanced FTS queries).

### Tier 3: Deep Context (~500+ tokens)

To pull a full session's summary (file accesses + first messages):

```bash
auto-memory show 9a8500e4 --limit-messages 20
```

8-character session-id prefix is enough.

## Quick Reference

| Goal | Command |
|------|---------|
| Recent sessions in this project | `auto-memory list --cwd "$PWD"` |
| Recently-touched files | `auto-memory files --cwd "$PWD"` |
| Find sessions by keyword | `auto-memory search "term"` |
| Restrict by recency | add `--days 7` |
| Restrict to user prompts | `search ... --role user` |
| Inspect one session | `auto-memory show <id-prefix>` |
| Index health / freshness | `auto-memory health` |
| Force a full reindex | `auto-memory reindex` |
| Machine-readable output | global `--json` flag |

All commands run an incremental reindex first (cheap — only touches changed files). Use the global `--no-reindex` flag if you've just indexed.

## How It Works

1. Walks `~/.claude/projects/*/*.jsonl`. Each file = one Claude Code session.
2. Skips files whose mtime/size haven't changed since the last index.
3. For each new/changed file: parses records, extracts user/assistant messages, tool-use blocks (Read/Edit/Write/etc. → file_accesses), `cwd`, `gitBranch`, timestamps.
4. Stores in SQLite at `~/.claude/auto-memory/index.db` with WAL mode and FTS5.
5. Subcommands query the cache.

## Common Mistakes

- **Forgetting `--cwd "$PWD"`** — without it you'll get hits from unrelated projects and waste tokens.
- **Treating recall as authoritative** — sessions are snapshots in time. A path or function named in a memory may have been renamed or deleted. Verify with `Read`/`Grep` before recommending action based on it.
- **Over-fetching** — going straight to `show` when `list` would have answered the question. Start at Tier 1.
- **Passing FTS5 syntax without `--raw`** — the default sanitizer strips operators. Use `--raw` only when you need them.
- **Searching across all projects when you only care about this one** — always pass `--cwd` for project-scoped questions.

## What's NOT Indexed

- `tool_result` outputs are kept (so you can search what tools returned), but be aware the index can grow large in long sessions.
- Sidechain (subagent) sessions are stored in the same jsonl files and are indexed alongside main sessions.
- The CLI never writes to the source jsonl. The index is rebuildable from scratch via `auto-memory reindex`.
