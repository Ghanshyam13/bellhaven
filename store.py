from __future__ import annotations
"""SQLite persistence for proposals and reviewer decisions.

This table is what makes re-runs safe: a proposal's `fingerprint` (what would change) is unique, and
once a row is rejected / applied / failed the pipeline never turns it back into a pending item.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from config import DB_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint   TEXT UNIQUE NOT NULL,
  kind          TEXT NOT NULL,
  group_key     TEXT NOT NULL,
  group_title   TEXT NOT NULL,
  title         TEXT NOT NULL,
  summary       TEXT NOT NULL,
  confidence    TEXT NOT NULL,
  payload       TEXT NOT NULL,              -- full proposal JSON: actions, diff, evidence, notes, links
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending | rejected | applied | failed | stale
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  decided_at    TEXT,
  decided_by    TEXT,
  decision_note TEXT DEFAULT '',
  progress      TEXT NOT NULL DEFAULT '{}', -- executor state so a half-finished apply can resume
  apply_log     TEXT NOT NULL DEFAULT '[]',
  error         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, stats TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


@contextmanager
def connect(path=None):
    con = sqlite3.connect(str(path or DB_FILE))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["progress"] = json.loads(d["progress"])
    d["apply_log"] = json.loads(d["apply_log"])
    return d


def upsert_proposals(con, proposals: list[dict]) -> dict:
    """Merge a fresh matcher run into the table. Returns counters for the run summary.

    - new fingerprint                       -> inserted as pending
    - existing pending / stale              -> refreshed (evidence may have changed), pending again
    - existing rejected / applied / failed  -> left alone (decision already made); only last_seen moves
    - pending/failed row not produced now   -> stale (the CRM or website changed, it no longer applies)
    """
    t = now()
    stats = {"new": 0, "refreshed": 0, "already_decided": 0, "went_stale": 0, "redetected_applied": 0}
    seen = set()
    for p in proposals:
        fp = p["fingerprint"]
        seen.add(fp)
        cur = con.execute("SELECT id, status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
        payload = json.dumps(p)
        if cur is None:
            con.execute(
                "INSERT INTO proposals(fingerprint,kind,group_key,group_title,title,summary,confidence,payload,"
                "status,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                (fp, p["kind"], p["group_key"], p["group_title"], p["title"], p["summary"], p["confidence"],
                 payload, t, t))
            stats["new"] += 1
        elif cur["status"] in ("pending", "stale"):
            con.execute("UPDATE proposals SET payload=?, title=?, summary=?, confidence=?, group_title=?, "
                        "status='pending', last_seen=? WHERE id=?",
                        (payload, p["title"], p["summary"], p["confidence"], p["group_title"], t, cur["id"]))
            stats["refreshed"] += 1
        else:
            con.execute("UPDATE proposals SET last_seen=? WHERE id=?", (t, cur["id"]))
            stats["already_decided"] += 1
            if cur["status"] == "applied":
                stats["redetected_applied"] += 1
    for r in con.execute("SELECT id, fingerprint FROM proposals WHERE status IN ('pending','failed')").fetchall():
        if r["fingerprint"] not in seen:
            con.execute("UPDATE proposals SET status='stale', error='No longer applies: CRM/website changed since it was proposed' "
                        "WHERE id=?", (r["id"],))
            stats["went_stale"] += 1
    return stats


def list_proposals(con, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM proposals"
    args: tuple = ()
    if status and status != "all":
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY group_title COLLATE NOCASE, id"
    return [_row(r) for r in con.execute(q, args).fetchall()]


def get(con, pid: int) -> dict | None:
    r = con.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _row(r) if r else None


def counts(con) -> dict:
    out = {"pending": 0, "applied": 0, "rejected": 0, "failed": 0, "stale": 0}
    for r in con.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


def update(con, pid: int, **cols) -> None:
    for k, v in list(cols.items()):
        if isinstance(v, (dict, list)):
            cols[k] = json.dumps(v)
    sets = ", ".join(f"{k}=?" for k in cols)
    con.execute(f"UPDATE proposals SET {sets} WHERE id=?", (*cols.values(), pid))


def record_run(con, started: str, stats: dict) -> None:
    con.execute("INSERT INTO runs(started_at, finished_at, stats) VALUES(?,?,?)", (started, now(), json.dumps(stats)))


def last_run(con) -> dict | None:
    r = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = dict(r)
    d["stats"] = json.loads(d["stats"])
    return d


def claim(con, pid: int) -> bool:
    """Atomically move pending/failed -> applying so a double click cannot apply twice."""
    cur = con.execute("UPDATE proposals SET status='applying', error='' WHERE id=? AND status IN ('pending','failed')", (pid,))
    return cur.rowcount == 1


def recover_interrupted(con) -> int:
    """If the app died mid-apply, make the row retryable (executor steps are idempotent / resumable)."""
    cur = con.execute("UPDATE proposals SET status='failed', error='Interrupted while applying - safe to retry' "
                      "WHERE status='applying'")
    return cur.rowcount
