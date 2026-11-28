"""SQLite chat history for deepcode. Standard library only.

One file, one database: chats, their messages, and a per-chat event log live in
<session-dir>/history.db. deepcode.py imports HistoryStore from here; nothing in
this module depends on deepcode.py, so it can be tested or reused on its own.
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
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


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTH_FULL = (r"\b(?:in |during |of |last |this )?(january|february|march|april|may|june|july|"
               r"august|september|october|november|december)\b")


def _resolve(day_start: datetime, month: int, day: int, year: int | None, today: datetime):
    """A calendar date; with no year, the most recent past occurrence."""
    try:
        if year is not None:
            return datetime(year if year > 99 else 2000 + year, month, day)
        found = datetime(today.year, month, day)
        return found if found <= today else datetime(today.year - 1, month, day)
    except ValueError:
        return None


def parse_when(text, today: datetime | None = None):
    """Find a date in free text. Returns (start, end, label) with end exclusive, or None."""
    t = str(text or "").lower()
    today = today or datetime.now()
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    one = timedelta(days=1)

    def day_range(day: datetime):
        return day, day + one, day.strftime("%B %d, %Y").replace(" 0", " ")

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        day = _resolve(midnight, int(m[2]), int(m[3]), int(m[1]), today)
        return day_range(day) if day else None
    m = re.search(_MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", t)
    if m:
        day = _resolve(midnight, _MONTHS[m[1][:3]], int(m[2]), int(m[3]) if m[3] else None, today)
        if day:
            return day_range(day)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r"\b(?:,?\s+(\d{4}))?", t)
    if m:
        day = _resolve(midnight, _MONTHS[m[2][:3]], int(m[1]), int(m[3]) if m[3] else None, today)
        if day:
            return day_range(day)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)      # day/month[/year]
    if m:
        day = _resolve(midnight, int(m[2]), int(m[1]), int(m[3]) if m[3] else None, today)
        if day:
            return day_range(day)
    if "yesterday" in t:
        return day_range(midnight - one)
    m = re.search(r"\b(\d{1,2}) days? ago\b", t)
    if m:
        return day_range(midnight - one * int(m[1]))
    if re.search(r"\b(last|past) week\b", t):
        return midnight - one * 7, midnight + one, "the last 7 days"
    if re.search(r"\btoday\b", t):
        return day_range(midnight)
    m = re.search(_MONTH_FULL, t)
    if m:
        month = _MONTHS[m[1][:3]]
        year = today.year if month <= today.month else today.year - 1
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), month % 12 + 1, 1)
        return start, end, start.strftime("%B %Y")
    return None


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

    def find_prompts(self, start: datetime | None = None, end: datetime | None = None,
                     keyword: str = "", limit: int = 15, exclude: int | None = None) -> list[dict]:
        """User prompts across all saved chats, oldest first, optionally by date range / keyword."""
        sql = ("SELECT m.ts, m.payload, m.session_id, s.title FROM messages m "
               "JOIN sessions s ON s.id = m.session_id WHERE m.role = 'user'")
        params: list = []
        if start is not None:
            sql += " AND m.ts >= ?"
            params.append(start.isoformat(timespec="seconds"))
        if end is not None:
            sql += " AND m.ts < ?"
            params.append(end.isoformat(timespec="seconds"))
        sql += " ORDER BY m.ts DESC, m.id DESC LIMIT ?"
        params.append(int(limit) * (8 if keyword else 3) + 5)
        needle = keyword.lower().strip()
        found: list[dict] = []
        for row in self._query(sql, params):
            if exclude is not None and row["session_id"] == exclude:
                continue
            try:
                content = str(json.loads(row["payload"]).get("content") or "")
            except (ValueError, AttributeError):
                continue
            if needle and needle not in content.lower():
                continue
            found.append({"ts": row["ts"], "title": row["title"], "content": content})
            if len(found) >= limit:
                break
        found.reverse()
        return found

    def days_with_chats(self, limit: int = 12) -> list[str]:
        rows = self._query("SELECT substr(ts, 1, 10) AS d FROM messages WHERE role = 'user' "
                           "GROUP BY d ORDER BY d DESC LIMIT ?", (int(limit),))
        return [row["d"] for row in rows]

    def history_report(self, text: str = "", keyword: str = "", exclude: int | None = None,
                       max_chars: int = 2000) -> str:
        """Plain-text lookup of earlier prompts: by the date found in `text`, else the latest."""
        when = parse_when(text)
        start, end, label = when if when else (None, None, "the most recent chats")
        rows = self.find_prompts(start, end, keyword, exclude=exclude)
        what = label + (f" matching {keyword!r}" if keyword else "")
        if not rows:
            days = ", ".join(self.days_with_chats())
            return (f"No saved prompts found for {what}. "
                    + (f"Days that do have saved chats: {days}." if days else "No chats are saved yet."))
        lines = [f"Saved prompts from {what} (oldest first):"]
        for row in rows:
            lines.append(f"- [{row['ts'].replace('T', ' ')[:16]}] (chat: {_clip(row['title'], 40)}) "
                         f"asked: {_clip(' '.join(row['content'].split()), 240)}")
        out = "\n".join(lines)
        return out if len(out) <= max_chars else out[:max_chars - 1].rstrip() + "…"

    def recall_text(self, max_chars: int = 1800, exclude: int | None = None,
                    max_chats: int = 8) -> str:
        """Digest of the user's earlier chats (newest first) for the system prompt.

        Lets a brand-new chat still answer "what did I ask before?".
        """
        def flat(text, width: int) -> str:
            return _clip(" ".join(str(text or "").split()), width)

        blocks: list[str] = []
        used = 0
        newest = True
        for row in self.list_sessions(max_chats + 1):
            if row["id"] == exclude:
                continue
            _, messages = self.load_messages(row["id"])
            turns: list[list[str]] = []
            for message in messages:
                role = message.get("role")
                if role == "user":
                    turns.append([flat(message.get("content"), 200), ""])
                elif (role == "assistant" and turns and not message.get("tool_calls")
                      and message.get("content")):
                    turns[-1][1] = flat(message.get("content"), 160)
            if not turns:
                continue
            lines = [f"Chat \"{flat(row['title'], 50)}\" ({rel_time(row['updated_at'])}):"]
            for ask, reply in turns[-4:]:
                lines.append(f"  - asked: {ask}" + (f" | you replied: {reply}" if reply else ""))
            if newest:
                lines.insert(0, f"The user's most recent earlier prompt was: {turns[-1][0]}")
                newest = False
            block = "\n".join(lines)
            if blocks and used + len(block) > max_chars:
                break
            blocks.append(_clip(block, max_chars))
            used += len(block) + 1
        if not blocks:
            return ""
        return ("MEMORY OF EARLIER CHATS: you DO have access to the user's previous chats "
                "with you, listed below (newest first). When asked what they asked or said "
                "before, answer from this list; never claim you cannot recall earlier prompts.\n"
                + "\n".join(blocks))

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