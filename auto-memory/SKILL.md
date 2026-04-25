---
name: auto-memory
description: Use when starting any conversation or whenever the user references prior work, prior conversations, their role/preferences, project context, or external systems - manages a persistent file-based memory system so future sessions retain user profile, feedback rules, project context, and external references without re-explanation.
---

# auto-memory

A persistent, file-based memory system for Claude Code. Stores what the user has taught you across sessions so future conversations start with context — who the user is, how they want to collaborate, what to avoid, and the background behind the work.

Inspired by [the auto-memory pattern for Copilot CLI](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/), adapted as a Claude Code skill.

## Storage Location

Memory lives in: `~/.claude/projects/<project-slug>/memory/`

The directory is created on first write. Don't check for existence — just write to it.

Each memory is a markdown file with frontmatter. An index file `MEMORY.md` lists every memory as a one-line link. `MEMORY.md` is loaded into context every session; individual memory files are loaded on demand.

## Memory Types

There are exactly four types. Pick the one that fits; do not invent new types.

### user
**What:** Role, goals, responsibilities, expertise level, domain knowledge.
**When to save:** Any time you learn a stable fact about who the user is.
**How to use:** Tailor explanations and recommendations to their background. A senior Go engineer asking about React needs different framing than a beginner.
**Don't save:** Negative judgments. Ephemeral mood. Things that aren't useful for future work.

Examples:
- "User is a data scientist currently focused on observability/logging"
- "10 years of Go, new to React — frame frontend explanations via backend analogues"

### feedback
**What:** Rules the user has given about how to work — both corrections AND validated approaches.
**When to save:** Any correction ("don't do X", "stop doing Y") AND any quiet confirmation ("yes that was right", accepting an unusual choice without pushback). Save successes too — corrections-only memory makes you over-cautious and you'll drift away from approaches the user has already validated.
**How to use:** Apply the rule so the user never has to repeat themselves.
**Body structure:** Lead with the rule, then `**Why:**` (the reason — often a past incident) and `**How to apply:**` (when this kicks in). Knowing *why* lets you judge edge cases instead of blindly following.

Examples:
- "Integration tests must hit a real database, not mocks. **Why:** prior incident where mock/prod divergence masked a broken migration. **How to apply:** any test touching DB code in this repo."
- "User wants terse responses with no trailing summaries. **Why:** they read the diff. **How to apply:** skip end-of-turn recaps unless explicitly asked."

### project
**What:** Ongoing work, goals, initiatives, bugs, incidents that aren't visible from code or git history.
**When to save:** When you learn who's doing what, why, or by when. Always convert relative dates to absolute (`"Thursday"` → `"2026-03-05"`) so the memory stays interpretable later.
**How to use:** Inform suggestions with the broader context behind the request.
**Body structure:** Fact/decision, then `**Why:**` (motivation, constraint, deadline) and `**How to apply:**` (how this should shape suggestions). Project memories decay fast — the why helps you judge if it's still load-bearing.

Examples:
- "Merge freeze begins 2026-03-05 for mobile release. **Why:** mobile team cutting a release branch. **How to apply:** flag any non-critical PR scheduled after that date."
- "Auth middleware rewrite is compliance-driven, not tech-debt cleanup. **Why:** legal flagged session token storage. **How to apply:** scope decisions favor compliance over ergonomics."

### reference
**What:** Pointers to where information lives in external systems.
**When to save:** When you learn an external resource exists and what it's for.
**How to use:** When the user references that system or topic, you know where to look.

Examples:
- "Pipeline bugs tracked in Linear project INGEST"
- "grafana.internal/d/api-latency is the oncall latency dashboard — check when editing request-path code"

## What NOT to Save

These are NOT memories, even if the user asks you to save them:

- Code patterns, conventions, architecture, file paths, project structure → derivable from current code
- Git history, recent changes, who-changed-what → `git log` / `git blame` are authoritative
- Debugging solutions or fix recipes → the fix is in the code; commit message has context
- Anything already in CLAUDE.md
- In-progress task state, current conversation context → use TodoWrite or plans, not memory

If the user asks you to save a PR list, activity summary, or similar derivable data: ask what was *surprising* or *non-obvious* about it — that's the part worth keeping.

## How to Save

Two-step process:

**Step 1** — write the memory to its own file in the memory directory. Filename is descriptive (e.g., `user_role.md`, `feedback_testing.md`, `project_auth_rewrite.md`). Use this frontmatter:

```markdown
---
name: {{memory name}}
description: {{one-line description — used in future sessions to decide relevance, be specific}}
type: {{user|feedback|project|reference}}
---

{{content — for feedback/project, structure as: rule/fact, then **Why:** and **How to apply:**}}
```

**Step 2** — add a one-line pointer to `MEMORY.md`:

```markdown
- [Title](filename.md) — one-line hook
```

`MEMORY.md` is the index — never put memory content in it directly. Keep it under ~150 lines (lines after 200 are truncated). It has no frontmatter.

Organize semantically by topic, not chronologically. Update or remove memories that turn out to be wrong. Don't write duplicates — check existing memories first, update in place.

## When to Read Memories

- When memories seem relevant to the current request
- When the user references prior conversations or prior work
- **MUST read** when the user explicitly asks you to check, recall, or remember
- **MUST NOT apply** when the user says to ignore memory for this conversation

## Verify Before Recommending

Memory is a snapshot in time. A memory that names a function, file, flag, or path is a claim that it existed *when written* — it may have been renamed, removed, or never merged.

Before acting on a memory:
- Path named? Check the file exists.
- Function or flag named? Grep for it.
- About to recommend an action based on it? Verify first.

If a memory conflicts with current observed state: trust what you observe now, then update or delete the stale memory. Don't act on stale memory.

For "what's recent / what's current" questions, prefer `git log` or reading code over recalling a snapshot.

## Memory vs. Other Persistence

Memory is for *future sessions*. Other mechanisms are for *current session*:

- **Plan** → alignment on approach for a non-trivial implementation in this session
- **TodoWrite** → tracking discrete steps and progress in this session
- **Memory** → things future-you needs to know about the user, project, or external systems

If you're about to save something that's only useful inside this conversation, use a plan or todos instead.

## Quick Reference

| Situation | Action |
|-----------|--------|
| User shares role, expertise, or preference | Save `user` memory |
| User corrects your approach | Save `feedback` memory with **Why:** + **How to apply:** |
| User validates a non-obvious choice you made | Save `feedback` memory (don't only save corrections) |
| User explains motivation, deadline, ownership | Save `project` memory with absolute dates |
| User mentions external system (Linear, Grafana, Slack channel) | Save `reference` memory |
| User asks you to save derivable info (file paths, git log) | Decline — ask what was surprising about it |
| User says "remember X" | Save immediately as best-fit type |
| User says "forget X" | Find and remove the entry from file + index |
| Current memory contradicts what you see now | Trust observation, update/delete the memory |

## Common Mistakes

- **Saving conversation transients as memory** — task state, today's progress, what we just discussed. Use TodoWrite/plans instead.
- **Writing memory content into MEMORY.md** — `MEMORY.md` is an index of one-line links only.
- **Saving corrections without rationale** — without **Why:**, you can't judge edge cases later.
- **Saving only corrections, never confirmations** — you'll drift away from approaches the user already validated.
- **Relative dates** — "Thursday" is meaningless in three weeks. Always absolute.
- **Stale memory drift** — recommending a function from memory without grepping to confirm it still exists.
- **Duplicate memories** — always search existing files for the topic before creating a new file.
- **Negative judgments about the user** — memory is for being more helpful, not labeling.
