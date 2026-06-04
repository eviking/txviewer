# txviewer — transcript viewer for Claude Code

A real-time terminal viewer for [Claude Code](https://claude.ai/code) sessions.
Watch every step Claude takes as it happens — tool calls, token costs, file edits,
bash commands — all in a clean split-pane TUI.

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  txviewer  │  f53d98e2.jsonl  │  41 turns  │  LIVE  │  14:23:01            ║
╠════════════════╦═══════════════════════════════════════════════════════════════╣
║ T01 add a feat ║ Turn 3  refactor the telemetry hook                         ║
║ T02 fix the bu ║ In(cached): 348k  In(new): 12k  Out: 8k  Cache: 97%       ║
║ T03 refactor t ║ ─────────────────────────────────────────────────────────  ║
║ the telemetry  ║  1. Read    .claude/hooks/require_telemetry.py             ║
║ hook           ║  2. Edit    .claude/hooks/require_telemetry.py             ║
║                ║  3. Bash    docker compose up --build api                  ║
║                ║  4. kg:     answer_question  [telemetry hook classes]      ║
╚════════════════╩═══════════════════════════════════════════════════════════════╝
```

## Features

- **Live updates** — watches the transcript file and refreshes as Claude works
- **Step-by-step detail** — every tool call with its target, result preview, and token cost
- **Smart Bash summaries** — `python3 -c` scripts are summarised from their comments
  and code: `[Why are there no cross-module dependencies?]`, `[SQLite: select query]`
- **Docker depth** — distinguishes infra (`docker compose up`) from discovery
  (`docker exec ... python3 -c`) from monitoring (`docker logs`)
- **Token breakdown** — cached vs uncached input, output, cache hit %, ops overhead
- **Session summary** — press `s` for a bar chart of activity across the whole session,
  most-touched files, and most expensive turns. Buckets are derived from what actually
  happened — no hardcoded categories
- **Browse old sessions** — `--list` shows all sessions with dates and sizes;
  open any by ID prefix
- **Zero dependencies** — pure Python stdlib, works anywhere Python 3.8+ is installed

## Install

```bash
# No install needed — just run it
python3 txviewer.py

# Or make it executable
chmod +x txviewer.py
./txviewer.py
```

## Usage

```bash
python3 txviewer.py                  # attach to the latest active session
python3 txviewer.py --list           # browse all sessions
python3 txviewer.py f53d98e2         # open by session ID prefix
python3 txviewer.py path/to/session.jsonl  # open a specific file
python3 txviewer.py --help           # full documentation
```

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate turns |
| `j` / `k` | Scroll detail pane |
| `l` | Toggle LIVE mode |
| `Enter` | Pin / unpin selected turn |
| `s` | Session summary |
| `h` | Help overlay |
| `q` / `Esc` | Quit |

## How it works

Claude Code writes every turn to a `.jsonl` file in `~/.claude/projects/`.
txviewer polls that file for changes, parses the JSON transcript entries, and
renders them in a curses TUI. No network connection, no API keys, no config.

The transcript format is the same one the Claude Code Stop hook reads — if you
use the hook you'll recognise the data model immediately.

## Token stats explained

| Field | Meaning |
|-------|---------|
| `In(cached)` | Tokens served from Anthropic's prompt cache — cheapest |
| `In(new)` | Uncached tokens — new context added this turn |
| `Out` | Output tokens generated |
| `Cache %` | Fraction of input served from cache |
| `Ops` | Tokens on infra steps (docker, kubectl, etc.) |

## Session summary buckets

The `s` view derives activity buckets on the fly from what actually happened.
A long session might produce buckets like:

```
write/edit: .py source    ████████████████████████████  118 steps
docker exec (inspect)     ███████████████████████        94 steps
read: .py source          ████████████████████           79 steps
bash: grep                ████████████                   48 steps
kg: capture insight       ████                           16 steps
```

When per-step token data is available (sessions from recent Claude Code versions)
the bars show token spend instead of step count.

## Compatibility

- Python 3.8+
- macOS and Linux (uses `curses`)
- Claude Code any version — reads the standard transcript format

## Licence

MIT — see [LICENSE](LICENSE)
