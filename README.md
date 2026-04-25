# auto-memory for Claude Code

A read-only recall layer over Claude Code's session transcripts, packaged as a Claude Code Skill.

Inspired by [I wasted 68 minutes a day re-explaining my code, then I built auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/) — same idea, ported from GitHub Copilot CLI to Claude Code.

## What it does

Indexes every `~/.claude/projects/*/*.jsonl` (Claude Code's per-session transcript files) into a SQLite cache with FTS5 full-text search, then exposes an `auto-memory.py` CLI to query it.

The bundled `SKILL.md` is wired so Claude Code invokes the CLI at the start of every conversation (and again whenever you reference prior work) — so you stop re-explaining what you were working on.

- **Read-only.** Never writes to the source jsonl files. The index lives in `~/.claude/auto-memory/index.db`.
- **Stdlib only.** Single Python file, no dependencies.
- **WAL-safe.** Designed to run concurrently with live Claude Code processes.

## How it gets invoked

The skill's `description` field uses the strongest session-start triggering language Claude Code accepts ("Use when starting ANY conversation... BEFORE composing your first response"). In practice this means Claude reads the skill at the top of every conversation in a project that has prior history and runs the Tier 1 commands before replying.

This is reliable but not deterministic — Claude could still skip it on a prompt that looks fully self-contained. If you want a hard guarantee, add a `SessionStart` hook in `~/.claude/settings.json` that runs:

```bash
python3 ~/.claude/skills/auto-memory/auto-memory.py list  --limit 5  --cwd "$PWD" --no-reindex
python3 ~/.claude/skills/auto-memory/auto-memory.py files --limit 10 --cwd "$PWD" --no-reindex
```

…and emits the output as additional context. The skill alone is enough for most workflows.

## Tiered queries

Following the original article's design, queries are tiered by token cost. The skill instructs Claude to start at Tier 1 and escalate only when needed.

| Tier | Subcommand | Purpose | ~Tokens |
|------|------------|---------|---------|
| 1 | `list --cwd "$PWD"` | Recent sessions in this project | ~50 |
| 1 | `files --cwd "$PWD"` | Recently-touched files | ~100 |
| 2 | `search "term" --cwd "$PWD"` | Full-text recall | ~200 |
| 3 | `show <id-prefix>` | Full session detail | ~500+ |

All commands above are subcommands of `python3 ~/.claude/skills/auto-memory/auto-memory.py` (or `auto-memory` if you've added the optional PATH symlink — see install).

## Layout

The repo bundles the CLI inside the skill directory so the skill is self-contained:

```
auto-memory/                 (repo root)
├── README.md
├── LICENSE
└── auto-memory/             (the Claude Code skill)
    ├── SKILL.md
    └── auto-memory.py       (CLI — runs from anywhere)
```

## Install

```bash
git clone https://github.com/briandilley/auto-memory.git ~/auto-memory-repo

# Required: install the skill so Claude Code can discover it.
# The CLI script lives inside this directory — one symlink, both pieces installed.
ln -s ~/auto-memory-repo/auto-memory ~/.claude/skills/auto-memory

# Optional: put `auto-memory` on PATH for shell use. The SKILL itself does
# not depend on this — it always invokes the script via its full path.
mkdir -p ~/.local/bin
ln -s ~/.claude/skills/auto-memory/auto-memory.py ~/.local/bin/auto-memory
```

Verify:

```bash
python3 ~/.claude/skills/auto-memory/auto-memory.py health
# or, if the optional PATH symlink is in place:
auto-memory health
```

You should see session/message/file-access counts climb after Claude Code writes new sessions.

## CLI reference

Examples below show the bare `auto-memory` form for readability; the SKILL itself always uses the full `python3 ~/.claude/skills/auto-memory/auto-memory.py` path.

```bash
auto-memory list    [--limit N] [--days N] [--cwd PATH]
auto-memory files   [--limit N] [--days N] [--cwd PATH] [--contains STR]
auto-memory search  QUERY [--limit N] [--days N] [--cwd PATH] [--role user|assistant] [--raw]
auto-memory show    SESSION_ID [--limit-messages N] [--limit-files N]
auto-memory health
auto-memory reindex [--verbose]
```

Global flags (placed BEFORE the subcommand):
- `--json` — machine-readable output
- `--no-reindex` — skip the incremental index pass on this invocation

`SESSION_ID` accepts an 8-character prefix. If the prefix is ambiguous, the CLI prints all matches and exits non-zero.

By default, plain text in `search` is sanitized to a safe FTS5 query (special chars stripped, tokens AND'd as quoted phrases). Pass `--raw` if you want to use FTS5 operators directly (`AND`, `OR`, `NEAR`, column filters, etc.).

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
