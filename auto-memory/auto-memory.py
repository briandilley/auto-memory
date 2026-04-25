#!/usr/bin/env python3
"""auto-memory: read-only recall over Claude Code session transcripts.

Indexes ~/.claude/projects/*/*.jsonl into a SQLite cache (FTS5) and exposes
list / files / search / show / health subcommands. Stdlib only. Read-only
with respect to the source jsonl files.

Inspired by https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
CACHE_DIR = Path(os.path.expanduser("~/.claude/auto-memory"))
DB_PATH = CACHE_DIR / "index.db"

SCHEMA_VERSION = 1
FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}
FILE_PATH_KEYS = ("file_path", "notebook_path", "path")


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  project_dir TEXT,
  cwd TEXT,
  git_branch TEXT,
  started_at TEXT,
  ended_at TEXT,
  message_count INTEGER,
  first_prompt TEXT,
  jsonl_path TEXT,
  jsonl_mtime REAL,
  jsonl_size INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);

CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT,
  idx INTEGER,
  ts TEXT,
  role TEXT,
  text TEXT,
  PRIMARY KEY (session_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  session_id UNINDEXED,
  idx UNINDEXED,
  ts UNINDEXED,
  role UNINDEXED,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS file_accesses (
  session_id TEXT,
  ts TEXT,
  tool TEXT,
  path TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_path ON file_accesses(path);
CREATE INDEX IF NOT EXISTS idx_files_ts ON file_accesses(ts DESC);
CREATE INDEX IF NOT EXISTS idx_files_session ON file_accesses(session_id);
"""


def open_db() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for stmt in SCHEMA.strip().split(";\n"):
        s = stmt.strip()
        if s:
            cur.execute(s)
    cur.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) != SCHEMA_VERSION:
        # Schema mismatch: nuke and rebuild.
        cur.executescript(
            "DROP TABLE IF EXISTS sessions;"
            "DROP TABLE IF EXISTS messages;"
            "DROP TABLE IF EXISTS messages_fts;"
            "DROP TABLE IF EXISTS file_accesses;"
            "DROP TABLE IF EXISTS meta;"
        )
        for stmt in SCHEMA.strip().split(";\n"):
            s = stmt.strip()
            if s:
                cur.execute(s)
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def with_retry(fn, *args, **kwargs):
    """Retry on SQLite OperationalError (locked DB) with exponential backoff."""
    delay = 0.05
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2


# ---------------------------------------------------------------------------
# JSONL parsing & indexing
# ---------------------------------------------------------------------------


def _flatten_text(content: Any) -> str:
    """Extract searchable text from a message.content field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(str(block.get("text", "")))
            elif t == "thinking":
                # thinking blocks are useful for search but mark them
                parts.append(str(block.get("thinking", "")))
            elif t == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {}) or {}
                # include common high-signal fields
                hints = []
                for k in ("command", "description", "pattern", "query", "prompt", "file_path", "path"):
                    v = inp.get(k)
                    if v:
                        hints.append(f"{k}={v}")
                parts.append(f"[tool:{name} {' '.join(hints)}]")
            elif t == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(str(sub.get("text", "")))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content)


def _extract_file_paths(content: Any) -> Iterator[tuple[str, str]]:
    """Yield (tool_name, path) from tool_use blocks."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        if name not in FILE_TOOLS:
            continue
        inp = block.get("input", {}) or {}
        for key in FILE_PATH_KEYS:
            v = inp.get(key)
            if isinstance(v, str) and v:
                yield name, v
                break


def _iter_records(jsonl_path: Path) -> Iterator[dict]:
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def index_session_file(conn: sqlite3.Connection, jsonl_path: Path) -> bool:
    """Index a single session jsonl file. Returns True if indexed/reindexed."""
    try:
        st = jsonl_path.stat()
    except FileNotFoundError:
        return False

    cur = conn.cursor()
    session_id = jsonl_path.stem
    project_dir = jsonl_path.parent.name

    cur.execute(
        "SELECT jsonl_mtime, jsonl_size FROM sessions WHERE session_id=?",
        (session_id,),
    )
    row = cur.fetchone()
    if row and row["jsonl_mtime"] == st.st_mtime and row["jsonl_size"] == st.st_size:
        return False  # already up-to-date

    cwd: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    first_prompt: str | None = None
    msg_count = 0
    messages_to_insert: list[tuple[str, int, str, str, str]] = []
    files_to_insert: list[tuple[str, str, str, str]] = []

    idx = 0
    for rec in _iter_records(jsonl_path):
        rtype = rec.get("type")
        ts = rec.get("timestamp") or ""
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts
        if cwd is None and rec.get("cwd"):
            cwd = rec["cwd"]
        if git_branch is None and rec.get("gitBranch"):
            git_branch = rec["gitBranch"]

        if rtype in ("user", "assistant"):
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            text = _flatten_text(content)
            if rtype == "user" and first_prompt is None:
                # skip tool_result-only user turns
                stripped = text.strip()
                if stripped and not stripped.startswith("[tool:"):
                    first_prompt = stripped[:500]
            if text.strip():
                messages_to_insert.append((session_id, idx, ts, rtype, text))
                idx += 1
                msg_count += 1
            if rtype == "assistant":
                for tool_name, path in _extract_file_paths(content):
                    files_to_insert.append((session_id, ts, tool_name, path))

        elif rtype == "queue-operation" and rec.get("operation") == "enqueue":
            # Catch initial prompts that may not show as user messages yet
            if first_prompt is None:
                content = rec.get("content")
                if isinstance(content, str) and content.strip():
                    first_prompt = content.strip()[:500]

    def _do_index() -> None:
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            cur.execute(
                "DELETE FROM messages_fts WHERE session_id=?", (session_id,)
            )
            cur.execute(
                "DELETE FROM file_accesses WHERE session_id=?", (session_id,)
            )
            cur.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

            cur.execute(
                """INSERT INTO sessions(
                    session_id, project_dir, cwd, git_branch,
                    started_at, ended_at, message_count, first_prompt,
                    jsonl_path, jsonl_mtime, jsonl_size
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, project_dir, cwd, git_branch,
                    started_at, ended_at, msg_count, first_prompt,
                    str(jsonl_path), st.st_mtime, st.st_size,
                ),
            )
            if messages_to_insert:
                cur.executemany(
                    "INSERT INTO messages(session_id, idx, ts, role, text) VALUES (?,?,?,?,?)",
                    messages_to_insert,
                )
                # FTS column order: text, session_id, idx, ts, role.
                # messages_to_insert is (session_id, idx, ts, role, text) — reorder.
                fts_rows = [
                    (text, sid, idx, ts, role)
                    for (sid, idx, ts, role, text) in messages_to_insert
                ]
                cur.executemany(
                    "INSERT INTO messages_fts(text, session_id, idx, ts, role) VALUES (?,?,?,?,?)",
                    fts_rows,
                )
            if files_to_insert:
                cur.executemany(
                    "INSERT INTO file_accesses(session_id, ts, tool, path) VALUES (?,?,?,?)",
                    files_to_insert,
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    with_retry(_do_index)
    return True


def reindex_all(conn: sqlite3.Connection, verbose: bool = False) -> dict:
    """Walk the projects dir; index any new/changed session files."""
    indexed = 0
    skipped = 0
    if not PROJECTS_DIR.exists():
        return {"indexed": 0, "skipped": 0, "total": 0}
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if index_session_file(conn, jsonl):
                indexed += 1
                if verbose:
                    print(f"indexed {jsonl.name}", file=sys.stderr)
            else:
                skipped += 1
        except Exception as e:
            if verbose:
                print(f"FAILED {jsonl}: {e}", file=sys.stderr)
    return {"indexed": indexed, "skipped": skipped, "total": indexed + skipped}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cutoff_iso(days: int | None) -> str | None:
    if days is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat().replace("+00:00", "Z")


def _fmt_when(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:19]
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _emit(rows: list[dict], json_out: bool, formatter) -> None:
    if json_out:
        print(json.dumps(rows, indent=2, default=str))
    else:
        formatter(rows)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args, conn: sqlite3.Connection) -> int:
    where = ["1=1"]
    params: list[Any] = []
    cutoff = _cutoff_iso(args.days)
    if cutoff:
        where.append("started_at >= ?")
        params.append(cutoff)
    if args.cwd:
        where.append("cwd = ?")
        params.append(os.path.abspath(args.cwd))
    sql = f"""
        SELECT session_id, cwd, git_branch, started_at, ended_at,
               message_count, first_prompt
        FROM sessions
        WHERE {' AND '.join(where)}
        ORDER BY started_at DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    def fmt(rs: list[dict]) -> None:
        if not rs:
            print("(no sessions)")
            return
        for r in rs:
            print(
                f"{_fmt_when(r['started_at'])}  "
                f"{r['session_id'][:8]}  "
                f"msgs={r['message_count'] or 0:>3}  "
                f"{r['cwd'] or '?'}"
            )
            if r["first_prompt"]:
                print(f"    > {_truncate(r['first_prompt'], 100)}")

    _emit(rows, args.json, fmt)
    return 0


def cmd_files(args, conn: sqlite3.Connection) -> int:
    where = ["1=1"]
    params: list[Any] = []
    cutoff = _cutoff_iso(args.days)
    if cutoff:
        where.append("fa.ts >= ?")
        params.append(cutoff)
    if args.cwd:
        where.append("s.cwd = ?")
        params.append(os.path.abspath(args.cwd))
    if args.contains:
        where.append("fa.path LIKE ?")
        params.append(f"%{args.contains}%")
    sql = f"""
        SELECT fa.path, MAX(fa.ts) AS last_ts, COUNT(*) AS hits,
               COUNT(DISTINCT fa.session_id) AS sessions
        FROM file_accesses fa
        JOIN sessions s ON s.session_id = fa.session_id
        WHERE {' AND '.join(where)}
        GROUP BY fa.path
        ORDER BY last_ts DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    def fmt(rs: list[dict]) -> None:
        if not rs:
            print("(no file accesses)")
            return
        for r in rs:
            print(
                f"{_fmt_when(r['last_ts'])}  "
                f"hits={r['hits']:>3}  "
                f"sessions={r['sessions']:>2}  "
                f"{r['path']}"
            )

    _emit(rows, args.json, fmt)
    return 0


def _sanitize_fts_query(q: str) -> str:
    """Convert plain text to a safe FTS5 query.

    Tokenizes on whitespace, drops FTS5-special chars from each token, and
    quotes each surviving token. Multiple tokens are AND'd implicitly.
    Preserves the user's intent (find sessions containing these words) without
    requiring them to know FTS5 syntax.
    """
    out: list[str] = []
    for raw in q.split():
        # strip chars that have special meaning to FTS5
        clean = "".join(c for c in raw if c.isalnum() or c == "_")
        if clean:
            out.append(f'"{clean}"')
    return " ".join(out) if out else '""'


def cmd_search(args, conn: sqlite3.Connection) -> int:
    query = args.query if args.raw else _sanitize_fts_query(args.query)
    where = ["messages_fts MATCH ?"]
    params: list[Any] = [query]
    cutoff = _cutoff_iso(args.days)
    if cutoff:
        where.append("messages_fts.ts >= ?")
        params.append(cutoff)
    if args.cwd:
        where.append("s.cwd = ?")
        params.append(os.path.abspath(args.cwd))
    if args.role:
        where.append("messages_fts.role = ?")
        params.append(args.role)
    sql = f"""
        SELECT s.session_id, s.cwd, s.started_at,
               messages_fts.ts AS msg_ts,
               messages_fts.role AS role,
               messages_fts.idx AS idx,
               snippet(messages_fts, 0, '«', '»', '…', 12) AS snippet,
               bm25(messages_fts) AS rank
        FROM messages_fts
        JOIN sessions s ON s.session_id = messages_fts.session_id
        WHERE {' AND '.join(where)}
        ORDER BY rank
        LIMIT ?
    """
    params.append(args.limit)
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as e:
        # FTS5 syntax errors when query has special chars
        print(f"search error: {e}", file=sys.stderr)
        return 2

    def fmt(rs: list[dict]) -> None:
        if not rs:
            print("(no matches)")
            return
        for r in rs:
            print(
                f"{_fmt_when(r['msg_ts'])}  "
                f"{r['session_id'][:8]}#{r['idx']}  "
                f"[{r['role']}]  "
                f"{r['cwd'] or '?'}"
            )
            print(f"    {_truncate(r['snippet'], 200)}")

    _emit(rows, args.json, fmt)
    return 0


def cmd_show(args, conn: sqlite3.Connection) -> int:
    # Resolve prefix -> full id
    cur = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 2",
        (args.session_id + "%",),
    )
    matches = [r["session_id"] for r in cur.fetchall()]
    if not matches:
        print(f"no session matches '{args.session_id}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous prefix '{args.session_id}': {matches}", file=sys.stderr)
        return 1
    sid = matches[0]

    sess = conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    msgs = conn.execute(
        "SELECT idx, ts, role, text FROM messages WHERE session_id=? ORDER BY idx ASC LIMIT ?",
        (sid, args.limit_messages),
    ).fetchall()
    files = conn.execute(
        """SELECT path, COUNT(*) AS hits, MAX(ts) AS last_ts
           FROM file_accesses WHERE session_id=?
           GROUP BY path ORDER BY last_ts DESC LIMIT ?""",
        (sid, args.limit_files),
    ).fetchall()

    if args.json:
        print(json.dumps({
            "session": dict(sess) if sess else None,
            "messages": [dict(m) for m in msgs],
            "files": [dict(f) for f in files],
        }, indent=2, default=str))
        return 0

    print(f"session   {sid}")
    print(f"started   {_fmt_when(sess['started_at'])}")
    print(f"ended     {_fmt_when(sess['ended_at'])}")
    print(f"cwd       {sess['cwd']}")
    print(f"branch    {sess['git_branch']}")
    print(f"messages  {sess['message_count']}")
    if sess["first_prompt"]:
        print(f"first     {_truncate(sess['first_prompt'], 200)}")
    print()
    print("files:")
    if not files:
        print("  (none)")
    for f in files:
        print(f"  {_fmt_when(f['last_ts'])}  hits={f['hits']:>2}  {f['path']}")
    print()
    print(f"messages (first {len(msgs)}):")
    for m in msgs:
        print(f"  #{m['idx']:>3} {_fmt_when(m['ts'])} [{m['role']}]")
        print(f"      {_truncate(m['text'], 240)}")
    return 0


def cmd_health(args, conn: sqlite3.Connection) -> int:
    t0 = time.time()
    counts = {
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "file_accesses": conn.execute(
            "SELECT COUNT(*) FROM file_accesses"
        ).fetchone()[0],
    }
    latest = conn.execute(
        "SELECT MAX(ended_at) AS m FROM sessions"
    ).fetchone()["m"]
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    schema_v = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    # Test FTS query latency
    try:
        conn.execute("SELECT 1 FROM messages_fts WHERE messages_fts MATCH 'test' LIMIT 1").fetchall()
        fts_ok = True
    except sqlite3.OperationalError:
        fts_ok = False
    latency_ms = round((time.time() - t0) * 1000, 1)

    info = {
        "db_path": str(DB_PATH),
        "db_size_bytes": db_size,
        "schema_version": schema_v["value"] if schema_v else None,
        "sessions": counts["sessions"],
        "messages": counts["messages"],
        "file_accesses": counts["file_accesses"],
        "latest_session_ts": latest,
        "fts_ok": fts_ok,
        "query_latency_ms": latency_ms,
        "projects_dir_exists": PROJECTS_DIR.exists(),
    }

    if args.json:
        print(json.dumps(info, indent=2))
        return 0

    for k, v in info.items():
        print(f"{k:<22} {v}")
    return 0


def cmd_reindex(args, conn: sqlite3.Connection) -> int:
    stats = reindex_all(conn, verbose=args.verbose)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"indexed={stats['indexed']} skipped={stats['skipped']} total={stats['total']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="auto-memory",
        description="Read-only recall over Claude Code session transcripts.",
    )
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument(
        "--no-reindex",
        action="store_true",
        help="skip incremental reindex on this invocation",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list recent sessions")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--days", type=int, default=None, help="only last N days")
    sp.add_argument("--cwd", type=str, default=None, help="filter by working dir")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("files", help="list recently accessed files")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("--cwd", type=str, default=None)
    sp.add_argument("--contains", type=str, default=None, help="path substring")
    sp.set_defaults(func=cmd_files)

    sp = sub.add_parser("search", help="full-text search across messages")
    sp.add_argument("query", help="FTS5 query (e.g. \"auth migration\")")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("--cwd", type=str, default=None)
    sp.add_argument(
        "--role",
        choices=["user", "assistant"],
        default=None,
        help="restrict to messages from one role",
    )
    sp.add_argument(
        "--raw",
        action="store_true",
        help="pass query straight to FTS5 (default sanitizes plain text)",
    )
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("show", help="show one session in detail")
    sp.add_argument("session_id", help="session id (8-char prefix is fine)")
    sp.add_argument("--limit-messages", type=int, default=20)
    sp.add_argument("--limit-files", type=int, default=20)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("health", help="show index/db health")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("reindex", help="force a reindex pass")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_reindex)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = open_db()
    try:
        if not args.no_reindex and args.cmd != "reindex":
            with_retry(reindex_all, conn, verbose=False)
        return args.func(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
