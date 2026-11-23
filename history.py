"""SQLite chat history for deepcode. Standard library only.

One file, one database: chats, their messages, and a per-chat event log live in
<session-dir>/history.db. deepcode.py imports HistoryStore from here; nothing in
this module depends on deepcode.py, so it can be tested or reused on its own.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_NAME = "history.db"          # SQLite chat history, inside --session-dir
TITLE_MAX = 70                  # chat title length in the history list


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_title(text) -> str:
    """Chat title: the first non-empty line of the first request."""
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return _clip(line, TITLE_MAX)
    return "Untitled chat"


def rel_time(iso) -> str:
    try:
        then = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    now = datetime.now()
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = (now.date() - then.date()).days
    if days <= 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return then.strftime("%b %d" if then.year == now.year else "%Y-%m-%d")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    model       TEXT,
    workspace   TEXT,
    source_file TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    role        TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
"""


class HistoryStore:
    """Chats, their messages, and a per-chat event log, in one SQLite file.

    A chat row is only created when its first message is saved, so opening the
    app and quitting leaves nothing behind. Write failures never raise; the
    first one is kept in `error` so the caller can warn once.
    """

    def __init__(self, directory: Path | None):
        self.directory = directory
        self.path: Path | None = None
        self.error: str | None = None
        self._lock = threading.RLock()
        if directory is None:
            target = ":memory:"
        else:
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / DB_NAME
            target = str(self.path)
        self.db = sqlite3.connect(target, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()
        if directory is not None:
            self.import_legacy()

    # -- plumbing ----------------------------------------------------------
    def _write(self, *statements) -> bool:
        with self._lock:
            try:
                with self.db:  # one transaction: commit, or roll back on error
                    for sql, params in statements:
                        self.db.execute(sql, params)
                return True
            except sqlite3.Error as exc:
                self.error = self.error or str(exc)
                return False

    def _query(self, sql: str, params=()) -> list:
        with self._lock:
            try:
                return self.db.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                self.error = self.error or str(exc)
                return []

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    # -- writing -----------------------------------------------------------
    def create_session(self, model: str, workspace: str) -> int | None:
        ts = now_iso()
        with self._lock:
            try:
                with self.db:
                    cur = self.db.execute(
                        "INSERT INTO sessions(title, created_at, updated_at, model, workspace) "
                        "VALUES('', ?, ?, ?, ?)", (ts, ts, model, workspace))
                return cur.lastrowid
            except sqlite3.Error as exc:
                self.error = self.error or str(exc)
                return None

    def add_message(self, sid: int, message: dict) -> None:
        ts = now_iso()
        payload = json.dumps(message, ensure_ascii=False, default=str)
        statements = [
            ("INSERT INTO messages(session_id, ts, role, payload) VALUES(?, ?, ?, ?)",
             (sid, ts, str(message.get("role") or ""), payload)),
            ("UPDATE sessions SET updated_at = ? WHERE id = ?", (ts, sid)),
        ]
        if message.get("role") == "user":
            statements.append(("UPDATE sessions SET title = ? WHERE id = ? AND title = ''",
                               (make_title(message.get("content")), sid)))
        self._write(*statements)

    def add_event(self, sid: int, kind: str, data: dict) -> None:
        self._write(("INSERT INTO events(session_id, ts, kind, data) VALUES(?, ?, ?, ?)",
                     (sid, now_iso(), kind, json.dumps(data, ensure_ascii=False, default=str))))

    def rename_session(self, sid: int, title: str) -> None:
        self._write(("UPDATE sessions SET title = ? WHERE id = ?", (title, sid)))

    def delete_session(self, sid: int) -> None:
        self._write(("DELETE FROM sessions WHERE id = ?", (sid,)))

    def delete_messages(self, ids: list[int]) -> None:
        if ids:
            self._write(*[("DELETE FROM messages WHERE id = ?", (i,)) for i in ids])

    # -- reading -----------------------------------------------------------
    def list_sessions(self, limit: int = 15) -> list[dict]:
        rows = self._query(
            "SELECT s.id, s.title, s.created_at, s.updated_at, s.model, s.workspace, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.role = 'user') AS turns "
            "FROM sessions s WHERE EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id) "
            "ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (int(limit),))
        return [dict(row) for row in rows]

    def get_session(self, sid: int) -> dict | None:
        rows = self._query("SELECT id, title, created_at, updated_at, model, workspace "
                           "FROM sessions WHERE id = ?", (sid,))
        return dict(rows[0]) if rows else None

    def load_messages(self, sid: int) -> tuple[list[int], list[dict]]:
        ids: list[int] = []
        messages: list[dict] = []
        for row in self._query("SELECT id, payload FROM messages WHERE session_id = ? ORDER BY id", (sid,)):
            try:
                message = json.loads(row["payload"])
            except ValueError:
                continue
            if isinstance(message, dict):
                ids.append(row["id"])
                messages.append(message)
        return ids, messages

    # -- legacy JSONL import -----------------------------------------------
    def import_legacy(self) -> int:
        """Import sessions/*.jsonl logs from before the SQLite store. Files are left in place."""
        if self.directory is None:
            return 0
        seen = {row["source_file"] for row in
                self._query("SELECT source_file FROM sessions WHERE source_file IS NOT NULL")}
        imported = 0
        for path in sorted(self.directory.glob("*.jsonl")):
            if path.name not in seen and self.import_file(path) is not None:
                imported += 1
        return imported

    def import_file(self, path: Path) -> int | None:
        """Import one JSONL log as a chat. Returns its chat id, or None if it has no messages."""
        existing = self._query("SELECT id FROM sessions WHERE source_file = ?", (path.name,))
        if existing:
            return existing[0]["id"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        start: dict = {}
        messages: list[tuple[str, dict]] = []
        events: list[tuple[str, str, dict]] = []
        first_ts = ""
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            ts = str(record.get("ts") or now_iso())
            first_ts = first_ts or ts
            kind = record.get("kind")
            if kind == "message" and isinstance(record.get("message"), dict):
                messages.append((ts, record["message"]))
            elif kind:
                events.append((ts, str(kind), {k: v for k, v in record.items() if k not in ("ts", "kind")}))
                if kind == "start" and not start:
                    start = record
        if not messages:
            return None
        first_user = next((m.get("content") for _, m in messages if m.get("role") == "user"), "")
        with self._lock:
            try:
                with self.db:
                    cur = self.db.execute(
                        "INSERT INTO sessions(title, created_at, updated_at, model, workspace, source_file) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (make_title(first_user), first_ts or messages[0][0], messages[-1][0],
                         start.get("model"), start.get("workspace"), path.name))
                    sid = cur.lastrowid
                    for ts, message in messages:
                        self.db.execute(
                            "INSERT INTO messages(session_id, ts, role, payload) VALUES(?, ?, ?, ?)",
                            (sid, ts, str(message.get("role") or ""),
                             json.dumps(message, ensure_ascii=False, default=str)))
                    for ts, kind, data in events:
                        self.db.execute(
                            "INSERT INTO events(session_id, ts, kind, data) VALUES(?, ?, ?, ?)",
                            (sid, ts, kind, json.dumps(data, ensure_ascii=False, default=str)))
                return sid
            except sqlite3.Error as exc:
                self.error = self.error or str(exc)
                return None