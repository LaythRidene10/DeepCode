# deepcode

A small autonomous coding agent for Windows. Python standard library only.
It runs a tool loop against a local Ollama model by default, and can point at
any OpenAI-compatible API (DeepSeek, OpenRouter, LM Studio).

No web UI, no TUI, no pip dependencies.

## Requirements

- Python 3.10+
- Ollama running at `http://127.0.0.1:11434`
- A tool-capable model, default `qwen3.5:4b`

```powershell
ollama pull qwen3.5:4b
```

## Run

```powershell
cd C:\path\to\project
python C:\Users\zarzi\OneDrive\Bureau\opencode\Projects\deepcode\deepcode.py

# or pick a workspace explicitly
python deepcode.py --workspace C:\path\to\project
```

Type a request at `YOU ›`. `/new` starts a fresh chat, `/history` lists recent
chats, `/resume` reopens one, `/exit` quits, Ctrl+C aborts the current
generation.

## Example

```text
YOU › Fix buggy.py so test_buggy.py passes. Run the test to verify.

  STEP 01   4.0s · 14 tok · 16.2/s
  LIST     .
  │ ...buggy.py  (32 bytes)
  │ ...test_buggy.py  (101 bytes)

  STEP 04   12s · 144 tok · 12.3/s
  SAY
  │ add() subtracts instead of adding.
  EDIT     ...\buggy.py
  │ -     return a - b
  │ +     return a + b

  STEP 06   4.0s · 48 tok · 14.4/s
  SHELL    python ...\test_buggy.py
  │ exit 0 · 1 line
  │ PASS

  ✓ DONE    36s · 7 steps · 385 tokens · 13.6 tok/s
    The test passes.
```

## Tools

| Tool           | What it does                                                |
| -------------- | ----------------------------------------------------------- |
| `shell`        | PowerShell 5.1, full disk, optional `workdir` and `timeout` |
| `read_file`    | line-numbered reads with `offset`/`limit`                   |
| `write_file`   | create or overwrite, parents created                        |
| `edit_file`    | exact replacement, optional `replace_all`, shown as a diff  |
| `list_dir`     | directory listing, optional recursive                       |
| `search_files` | case-sensitive regex content search with glob filter        |
| `update_todo`  | checklist for multi-step work                               |

A reply with no tool calls ends the turn. There is no `finish()` requirement.

`AGENTS.md` in the workspace is loaded into the system prompt automatically.
The real Desktop path is resolved through the registry, so "put it on my
desktop" works even when OneDrive has redirected known folders.

## Performance profile

Defaults are tuned for a laptop with limited VRAM:

| Flag            | Default | Why                                          |
| --------------- | ------- | -------------------------------------------- |
| `--threads`     | 8       | leaves CPU headroom; biggest heat lever      |
| `--ctx`         | 4096    | smaller KV cache and prompt                  |
| `--max-predict` | 1024    | caps runaway generations                     |
| `--max-minutes` | 20      | hard wall-clock stop per request; 0 disables |
| `--max-steps`   | 60      | bounds total model turns                     |

The system prompt is static and the conversation is append-only, so Ollama can
reuse its prompt cache. History is trimmed before each turn from `--ctx` and
`--max-predict`, leaving room for the answer; raise `--ctx` to keep more
history. Tool output is capped at 6,000 characters (3,072 at the default 4k
context).

`--doctor` measures the real model once and prints load time, generation speed,
and the GPU/CPU split:

```text
BACKEND    ollama
MODEL      qwen3.5:4b
LOAD       0.01s
GEN        17.0 tok/s (16 tokens)
SETTINGS   ctx 4096 · 8 threads · max predict 1024
MEMORY     3.7 GB · GPU 50% · CPU 50%
! WARN     CPU fallback is active; expect heat.
```

On a 4 GB GPU, a 3.7 GB model is split across CPU and GPU. No harness setting
changes that. If it still runs hot, lower `--threads 6` (or 4), use a smaller
model such as `qwen3.5:2b`, or use a remote API.

## Remote API

The OpenAI-compatible path is selected automatically when `--api` ends in `/v1`:

```powershell
python deepcode.py `
  --api https://api.deepseek.com/v1 `
  --model deepseek-chat `
  --key $env:DEEPSEEK_API_KEY
```

`--key` can also come from `DEEPCODE_API_KEY`, `DEEPSEEK_API_KEY`, or
`OPENAI_API_KEY`. Remote calls are currently non-streaming; local Ollama calls
stream so Ctrl+C stops generation immediately.

## Chat history

Every chat is saved to a SQLite database, `sessions/history.db` (see
`--session-dir`). All of the SQLite code lives in one file, `history.py`, which
must stay next to `deepcode.py`. A chat is created when you send its first request, so opening
the app and quitting leaves nothing behind. The title is the first request.

| Command             | What it does                                                             |
| ------------------- | ------------------------------------------------------------------------ |
| `/history [N\|all]` | list recent chats (default 15), newest first, current one marked `●`     |
| `/resume [N]`       | reopen chat number N from the list; without N it shows the list and asks |
| `/rename TITLE`     | rename the current chat                                                  |
| `/delete N`         | delete chat N after a y/N confirmation                                   |
| `/new`              | start a fresh chat; the previous one stays saved                         |

```text
YOU › /history

  RECENT CHATS
  ● 1  Fix buggy.py so test_buggy.py passes                  2 turns · 5m ago
      C:\Users\you\project
    2  Can you give me the path of FIFA23.exe                4 turns · 2d ago
      C:\Users\you\Downloads\PC-manager-main

YOU › /resume 2

  ↺ RESUMED Can you give me the path of FIFA23.exe          4 turns · 2d ago

YOU › Can you give me the path of FIFA23.exe
    · 1 tool call
    AGENT
    │ The path of FIFA23.exe is: C:\\Users\\you\\Desktop\\FIFA23.exe

YOU › I want you to open microsoft edge
    · 1 tool call
    AGENT
    │ Microsoft Edge has been opened.
```

Resuming restores the full conversation and replays it on screen: every prompt
and every agent reply, in order, with a one-line count of tool calls per turn
(tool output itself is not reprinted). The current launch's workspace and model are
used; if the chat was started in a different workspace you get a warning.

From the command line:

```powershell
python deepcode.py --history           # list chats and exit
python deepcode.py --resume            # latest chat
python deepcode.py --resume 3          # chat #3 from --history
```

An interrupted tool-call tail is dropped when a chat is loaded. Tables:
`sessions`, `messages` (one JSON message per row), and `events` (timing, tool
calls, errors). Old `sessions/*.jsonl` logs are imported into the database
automatically the first time it is opened; the files are left in place.
`--resume path\to\old.jsonl` still works.

## Flags

| Flag            | Default                  | Meaning                                                                |
| --------------- | ------------------------ | ---------------------------------------------------------------------- |
| `--model`       | `qwen3.5:4b`             | Ollama model or API model                                              |
| `--api`         | `http://127.0.0.1:11434` | Native Ollama URL, or OpenAI base ending in `/v1`                      |
| `--key`         | env                      | API key for remote backends                                            |
| `--backend`     | `auto`                   | `auto`, `ollama`, `openai`                                             |
| `--workspace`   | current dir              | Base for relative paths                                                |
| `--max-steps`   | 60                       | Model turns per request                                                |
| `--max-minutes` | 20                       | Wall-clock cap per request                                             |
| `--threads`     | 8                        | Ollama CPU threads, 0 = auto                                           |
| `--ctx`         | 4096                     | Context window in tokens                                               |
| `--max-predict` | 1024                     | Max generated tokens per call                                          |
| `--temperature` | 0.2                      | Sampling temperature                                                   |
| `--color`       | `auto`                   | `auto`, `always`, `never`                                              |
| `--session-dir` | `./sessions`             | Folder holding `history.db`                                            |
| `--resume`      | off                      | Latest chat, a chat number from `--history`, or a legacy `.jsonl` path |
| `--history`     | off                      | List recent chats and exit                                             |
| `--doctor`      | off                      | Measure model speed and GPU split                                      |
| `--self-check`  | off                      | Local sanity checks, no model                                          |

## Safety

- **No path clamp.** File tools reach the whole disk. Shell runs as you.
- **No confirmations.** The model executes what it emits.
- **No undo.** Overwrites and deletes are permanent. Use git for checkpoints.
- **Chat history is not a backup.** It records what happened; it cannot restore files.
- Standard library only; no network calls except the configured model API.

## Self-check

```powershell
python deepcode.py --self-check
```

Checks path resolution, history trimming, tool schemas, OpenAI/Ollama message
conversion, streaming chunk merging, todo validation, colors, and the spinner.
