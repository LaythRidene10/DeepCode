#!/usr/bin/env python3
"""deepcode over Telegram: a long-poll bot that runs requests on this PC.

Standard library only. Keep this file next to deepcode.py and history.py.
The bot talks outbound to api.telegram.org, so no port forwarding is needed.
Only ids in --allow can use it.
"""

import argparse
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import deepcode as dc
from history import DB_NAME

MAX_TEXT = 4000                 # Telegram caps at 4096; leave room for a marker
POLL_TIMEOUT = 25               # seconds the server may hold getUpdates open


# --------------------------------------------------------------------------
# Small pure helpers (covered by --self-check)
# --------------------------------------------------------------------------
def parse_allow(text: str) -> set[int]:
    """'1, 2 x 3' -> {1, 2, 3}. Anything that is not a number is ignored."""
    allowed: set[int] = set()
    for part in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            allowed.add(int(part))
    return allowed


def split_message(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line breaks near `limit`; join(chunks) is the original text."""
    text = str(text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            room = limit - len(current) - (1 if current else 0)
            if room <= 0:
                chunks.append(current)
                current = ""
                continue
            current = f"{current}\n{line[:room]}" if current else line[:room]
            line = line[room:]
            chunks.append(current)
            current = ""
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def parse_command(text: str) -> tuple[str, str]:
    """'/history@my_bot all' -> ('/history', 'all'); plain text -> ('', text)."""
    head, _, rest = str(text or "").strip().partition(" ")
    if head.startswith("/"):
        return head.split("@")[0].lower(), rest.strip()
    return "", str(text or "").strip()


class OffsetState:
    """The next Telegram update_id, so a restart never replays a request."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> int | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("offset") if isinstance(data, dict) else None
        return value if isinstance(value, int) else None

    def save(self, value: int) -> None:
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps({"offset": value}), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass


class CaptureUI(dc.UI):
    """A UI that prints nothing, but remembers the answer and the failure."""

    def __init__(self):
        self.buffer = io.StringIO()
        super().__init__(color="never", stream=self.buffer)
        self.answer = ""
        self.failed = ""
        self.footer = ""

    def done(self, summary, seconds, steps, tokens, rate=0.0, load=0.0, gap=True):
        self.answer = str(summary or "")
        parts = [f"{steps} step{'s' if steps != 1 else ''}", dc.fmt_time(seconds)]
        if tokens:
            parts.append(f"{tokens:,} tok")
        self.footer = " · ".join(parts)
        super().done(summary, seconds, steps, tokens, rate, load, gap)

    def error(self, message):
        self.failed = str(message)
        super().error(message)

    def stop(self, message):
        self.failed = self.failed or str(message)
        super().stop(message)

    def text(self) -> str:
        return self.buffer.getvalue().strip()


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
HELP = """deepcode on Telegram

Send any text to run it as a coding request.
/history [N|all]  recent chats
/resume N         reopen chat N
/new              start a fresh chat
/rename TITLE     rename the current chat
/delete N         delete chat N (then confirm)
/status           model, workspace, chat
/help             this list

Runs on your PC against your files. One request at a time."""


class Bot:
    def __init__(self, cfg, token: str, allow: set[int]):
        self.cfg = cfg
        self.token = token
        self.allow = allow
        self.state = OffsetState(Path(cfg.session_dir) / "telegram.json")
        self.console = dc.UI(cfg.color)
        self.harness = dc.Harness(cfg, self.console)
        self.pending_delete: dict[int, int] = {}

    # -- transport ---------------------------------------------------------
    def call(self, method: str, payload: dict | None = None, timeout: float = 40):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
            except ValueError:
                body = {}
            retry = (body.get("parameters") or {}).get("retry_after")
            if exc.code == 429 and retry:
                time.sleep(min(30, int(retry)))
            raise RuntimeError(body.get("description") or f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"network: {getattr(exc, 'reason', exc)}")
        if not body.get("ok"):
            raise RuntimeError(body.get("description") or "telegram rejected the request")
        return body.get("result")

    def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        for index, chunk in enumerate(split_message(text)):
            payload = {"chat_id": chat_id, "text": chunk,
                       "disable_web_page_preview": True}
            if reply_to and index == 0:
                payload["reply_to_message_id"] = reply_to
            self.call("sendMessage", payload)

    def typing(self, chat_id: int) -> "Typing":
        return Typing(self, chat_id)

    # -- one turn ----------------------------------------------------------
    def with_capture(self, action) -> tuple[str, CaptureUI]:
        ui = CaptureUI()
        self.harness.ui = ui
        try:
            action()
        finally:
            self.harness.ui = self.console
        return ui.text(), ui

    def run_task(self, chat_id: int, text: str) -> None:
        with self.typing(chat_id):
            _, ui = self.with_capture(lambda: self.harness.run_turn(text))
        if ui.answer:
            reply = ui.answer
            if ui.footer:
                reply += f"\n\n· {ui.footer}"
        elif ui.failed:
            reply = f"✗ {ui.failed}"
        else:
            tail = ui.text()[-1500:].strip()
            reply = f"No answer.\n{tail}" if tail else "No answer from the model."
        self.send(chat_id, reply)

    # -- commands ----------------------------------------------------------
    def command(self, chat_id: int, cmd: str, arg: str) -> bool:
        if cmd in {"/start", "/help"}:
            self.send(chat_id, HELP)
            return True
        if cmd == "/new":
            text, _ = self.with_capture(self.harness.new_session)
            self.send(chat_id, text or "new chat started")
            return True
        if cmd in {"/history", "/chats"}:
            text, _ = self.with_capture(lambda: self.harness.cmd_history(arg))
            self.send(chat_id, text or "No saved chats yet.")
            return True
        if cmd == "/resume":
            number = arg.lstrip("#").strip()
            if not number.isdigit():
                self.send(chat_id, "Usage: /resume N — N is the number from /history.")
                return True
            text, _ = self.with_capture(lambda: self.harness.cmd_resume(number))
            self.send(chat_id, text or "That chat has no usable messages.")
            return True
        if cmd == "/rename":
            text, _ = self.with_capture(lambda: self.harness.cmd_rename(arg))
            self.send(chat_id, text or "Renamed.")
            return True
        if cmd == "/delete":
            self.delete(chat_id, arg)
            return True
        if cmd == "/status":
            self.send(chat_id, self.status())
            return True
        return False

    def delete(self, chat_id: int, arg: str) -> None:
        number = arg.split()[0] if arg.split() else ""
        confirmed = len(arg.split()) > 1 and arg.split()[1].lower() == "confirm"
        sid = self.pending_delete.get(chat_id)
        if confirmed and sid is not None:
            self.harness.store.delete_session(sid)
            self.pending_delete.pop(chat_id, None)
            if sid == self.harness.session_id:
                self.harness.new_session()
            self.send(chat_id, "Deleted.")
            return
        if not number.lstrip("#").isdigit():
            self.send(chat_id, "Usage: /delete N — N is a number from /history.")
            return
        chosen = self.harness._pick(number)
        if chosen is None:
            self.send(chat_id, "No chat by that number; run /history.")
            return
        info = self.harness.store.get_session(chosen) or {}
        title = dc.ellipsize(info.get("title") or "Untitled chat", 40)
        self.pending_delete[chat_id] = chosen
        self.send(chat_id, f'Delete "{title}"?\nSend /delete {number} confirm to remove it.')

    def status(self) -> str:
        cfg = self.cfg
        threads = f"{cfg.threads} threads" if cfg.threads else "auto threads"
        lines = [
            f"MODEL      {cfg.model}",
            f"BACKEND    {cfg.backend} · ctx {cfg.ctx} · {threads}",
            f"WORKSPACE  {cfg.workspace}",
            f"LIMITS     {cfg.max_steps} steps · {cfg.max_minutes} min",
        ]
        sid = self.harness.session_id
        if sid is None:
            lines.append("CHAT       not started (first request creates it)")
        else:
            info = self.harness.store.get_session(sid) or {}
            lines.append(f"CHAT       #{sid} · {info.get('title') or 'Untitled chat'}")
        lines.append(f"DATABASE   {self.harness.store.path or 'in memory'}")
        return "\n".join(lines)

    # -- updates -----------------------------------------------------------
    def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not chat_id or sender.get("is_bot"):
            return
        if sender.get("id") not in self.allow:
            who = sender.get("username") or sender.get("first_name") or "?"
            print(f"  ignored id={sender.get('id')} @{who} chat={chat_id}", flush=True)
            self.send(chat_id, f"Not authorized. Your id is {sender.get('id')}; "
                               "add it to DEEPCODE_TG_ALLOW on the PC.")
            return
        text = message.get("text")
        if not text:
            self.send(chat_id, "Text only for now: send a request or /help.")
            return
        command, arg = parse_command(text)
        print(f"  <- {command or 'task'}: {one_line(text)[:80]}", flush=True)
        if command:
            if not self.command(chat_id, command, arg):
                self.send(chat_id, f"Unknown command {command}.\n\n{HELP}")
            return
        try:
            self.send(chat_id, "Working on it…")
            self.run_task(chat_id, text)
        except Exception as exc:                # the poll loop must survive
            self.send(chat_id, f"✗ {type(exc).__name__}: {exc}")

    def bootstrap_offset(self) -> int | None:
        """First start: step past anything queued before now, so a bot that was
        off for a day does not execute yesterday's requests."""
        offset = self.state.load()
        if offset is not None:
            return offset
        stale = self.call("getUpdates", {
            "offset": -1, "timeout": 0, "allowed_updates": ["message"]}) or []
        if not stale:
            return None
        offset = int(stale[-1].get("update_id") or 0) + 1
        self.state.save(offset)
        print("  skipped updates queued before this start", flush=True)
        return offset

    def poll_forever(self) -> None:
        me = self.call("getMe")
        print(f"  bot        @{me.get('username')}")
        print(f"  allowed    {sorted(self.allow)}")
        print(f"  workspace  {self.cfg.workspace}")
        print(f"  database   {self.harness.store.path or 'in memory'}")
        print("  listening (Ctrl+C stops)\n", flush=True)
        offset = self.bootstrap_offset()
        while True:
            try:
                updates = self.call("getUpdates", {
                    "offset": offset, "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message"]}, timeout=POLL_TIMEOUT + 15) or []
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"! {exc}", file=sys.stderr, flush=True)
                time.sleep(3)
                continue
            for update in updates:
                # Save first: a crash must not re-run a command already sent.
                offset = int(update.get("update_id") or 0) + 1
                self.state.save(offset)
                try:
                    self.handle(update)
                except Exception as exc:        # one bad update must not stop the bot
                    print(f"! {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


class Typing:
    """Keep the 'typing…' indicator alive while a long turn runs."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.bot.call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})
                except Exception:
                    return
                self.stop_event.wait(4.0)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()


def one_line(text: str) -> str:
    return " ".join(str(text or "").split())


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def whoami(token: str, seconds: int = 90) -> int:
    """Print the id of the first person who messages the bot."""
    print("Send any message to your bot from your phone…", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            request = urllib.request.Request(
                url, data=b'{"timeout": 20, "allowed_updates": ["message"]}',
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=35) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            print(f"! {exc}", file=sys.stderr)
            time.sleep(3)
            continue
        for update in body.get("result") or []:
            message = update.get("message") or {}
            sender = message.get("from") or {}
            if not sender.get("id"):
                continue
            name = sender.get("username") or sender.get("first_name") or ""
            print(f"\n  chat_id={sender['id']}  user={sender['id']}  @{name}")
            print(f"  setx DEEPCODE_TG_ALLOW \"{sender['id']}\"\n")
            return 0
    print("Nothing arrived. Check the token and send the bot a message.")
    return 1


def bot_self_check() -> None:
    assert parse_allow(" 1, 2 ;3 abc 4x ") == {1, 2, 3}
    assert parse_allow("") == set()
    assert parse_command("/history all") == ("/history", "all")
    assert parse_command("/resume@my_bot 2") == ("/resume", "2")
    assert parse_command("fix buggy.py") == ("", "fix buggy.py")
    assert split_message("") == []
    assert split_message("hi") == ["hi"]
    long = "\n".join(f"line {i}" for i in range(900))
    chunks = split_message(long, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks), max(map(len, chunks))
    assert "\n".join(chunks) == long
    wide = "x" * 2500
    chunks = split_message(wide, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks) and "".join(chunks) == wide
    ui = CaptureUI()
    ui.done("answer", 12.0, 3, 385)
    assert ui.answer == "answer" and "3 steps" in ui.footer and "12s" in ui.footer
    assert "\x1b[" not in ui.text()
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        state = OffsetState(Path(temp) / "telegram.json")
        assert state.load() is None
        state.save(42)
        assert state.load() == 42
    print("bot self-check OK")


def build_cfg(args) -> argparse.Namespace:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace is not a directory: {workspace}")
    backend = args.backend
    if backend == "auto":
        backend = "openai" if args.api.rstrip("/").endswith("/v1") else "ollama"
    key = (args.key or os.environ.get("DEEPCODE_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    return argparse.Namespace(
        model=args.model, api=args.api, key=key, backend=backend,
        workspace=workspace, max_steps=args.max_steps, max_minutes=args.max_minutes,
        threads=args.threads, ctx=args.ctx, max_predict=args.max_predict,
        temperature=args.temperature, session_dir=args.session_dir,
        resume=args.resume, history=False, doctor=False, self_check=False,
        color="never",
    )


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run deepcode from a Telegram chat.")
    parser.add_argument("--token", default=os.environ.get("DEEPCODE_TG_TOKEN"),
                        help="Bot token from @BotFather (or DEEPCODE_TG_TOKEN).")
    parser.add_argument("--allow", default=os.environ.get("DEEPCODE_TG_ALLOW"),
                        help="Comma-separated Telegram user ids allowed to use the bot.")
    parser.add_argument("--whoami", action="store_true",
                        help="Print the id of the next person who messages the bot, then exit.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--model", default=os.environ.get("DEEPCODE_MODEL", dc.DEFAULT_MODEL))
    parser.add_argument("--api", default=os.environ.get("DEEPCODE_API", dc.DEFAULT_API))
    parser.add_argument("--key", default=None)
    parser.add_argument("--backend", choices=("auto", "ollama", "openai"), default="auto")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--max-steps", type=int, default=dc.MAX_STEPS)
    parser.add_argument("--max-minutes", type=int, default=dc.MAX_MINUTES)
    parser.add_argument("--threads", type=int, default=dc.DEFAULT_THREADS)
    parser.add_argument("--ctx", type=int, default=dc.DEFAULT_CTX)
    parser.add_argument("--max-predict", type=int, default=dc.DEFAULT_MAX_PREDICT)
    parser.add_argument("--temperature", type=float, default=dc.DEFAULT_TEMPERATURE)
    parser.add_argument("--session-dir",
                        default=str(Path(__file__).resolve().parent / "sessions"))
    parser.add_argument("--resume", nargs="?", const="latest", default=None)
    args = parser.parse_args(argv)

    if args.self_check:
        bot_self_check()
        return 0
    if not args.token:
        print("missing bot token: set DEEPCODE_TG_TOKEN or pass --token", file=sys.stderr)
        return 2
    if args.whoami:
        return whoami(args.token)
    allow = parse_allow(args.allow)
    if not allow:
        print("refusing to start without an allowlist: set DEEPCODE_TG_ALLOW "
              "to your numeric Telegram id (use --whoami to find it)", file=sys.stderr)
        return 2

    cfg = build_cfg(args)
    bot = Bot(cfg, args.token, allow)
    print("\n  deepcode · telegram")
    try:
        bot.poll_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        bot.harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
