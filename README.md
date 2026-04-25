# auto-memory

A persistent, file-based memory system for Claude Code, packaged as a Skill.

Inspired by [I wasted 68 minutes a day re-explaining my code, then I built auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/) — the same idea, adapted for Claude Code's Skill system instead of a SQLite query layer.

## What it does

Teaches Claude Code to maintain a persistent memory across sessions so future conversations start with context — who you are, how you want to collaborate, what to avoid, and the background behind the work — instead of re-explaining everything every time.

Memories are organized into four explicit types:

- **user** — your role, expertise, preferences
- **feedback** — corrections AND validated approaches (with *why* + *how to apply*)
- **project** — ongoing work, deadlines, motivations not visible from code
- **reference** — pointers to external systems (Linear, Grafana, dashboards)

## Storage

Memories live in `~/.claude/projects/<project-slug>/memory/` as individual markdown files with frontmatter. An index file `MEMORY.md` lists every memory as a one-line link and is loaded into context every session; individual memory files are loaded on demand.

## Install

Clone this repo, then symlink (or copy) the `auto-memory/` directory into your Claude Code skills folder:

```bash
git clone https://github.com/briandilley/auto-memory.git
ln -s "$(pwd)/auto-memory/auto-memory" ~/.claude/skills/auto-memory
```

Claude Code auto-discovers skills in `~/.claude/skills/`. The skill's description triggers it automatically when relevant — when you share preferences, correct an approach, or reference prior work.

## Why a skill instead of a system prompt section?

- **Portable** — clone once, share across machines and teams
- **Versionable** — track changes in git, propose improvements via PR
- **Composable** — combine with other skills without bloating the system prompt
- **Discoverable** — Claude loads the description, then reads the body only when relevant

## What's NOT stored

By design, the skill refuses to save things that should be derived from current state:

- Code patterns, conventions, file paths, project structure → read the code
- Git history, recent changes → `git log`
- Debug fixes → the fix is in the code
- Ephemeral task state → use TodoWrite or plans, not memory

If you ask it to save derivable data, it will ask what was *surprising or non-obvious* about it — that's the part worth keeping.

## License

MIT — see [LICENSE](LICENSE).
