#!/usr/bin/env python3
"""
CodeKG Sidecar — real-time Claude Code session viewer.

Watches the active transcript file and renders each turn's steps,
token costs, and tool calls as they stream in.

Usage:
    python3 txviewer.py                  # auto-detect latest session
    python3 txviewer.py <session.jsonl>  # watch specific file

Keys:
    Tab        switch focus between left (turns) and right (steps) pane
    ↑ / ↓      navigate turns (left focus) or step through steps (right focus)
    j / k      scroll detail pane down / up
    Enter      pin/unpin selected turn detail
    l          toggle live mode (auto-follow latest)
    s          session summary (token spend by activity)
    h          help
    q / Ctrl-C quit
"""
from __future__ import annotations

import curses
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Step:
    index: int
    tool_name: str
    tool_use_id: str
    input_summary: str          # meaningful target (file, question, command)
    result_preview: str
    step_tokens: int            # Δ(cache_read + cache_creation) + input + output
    is_ops: bool = False        # docker/kubectl/etc
    is_codekg: bool = False

@dataclass
class Turn:
    index: int
    prompt: str
    steps: list[Step] = field(default_factory=list)
    response_text: str = ""
    input_context: int = 0      # cache_read + input + cache_creation
    output_tokens: int = 0
    cache_read_tokens: int = 0
    is_complete: bool = False   # has final assistant text with no more tools

    @property
    def cache_hit_pct(self) -> float:
        return (self.cache_read_tokens / self.input_context * 100) if self.input_context else 0.0

    @property
    def ops_tokens(self) -> int:
        return sum(s.step_tokens for s in self.steps if s.is_ops)

# ── Transcript parser ─────────────────────────────────────────────────────────

_OPS_PREFIXES = ("docker ", "kubectl ", "systemctl ", "service ", "helm ",
                 "terraform ", "ansible ", "aws ", "gcloud ", "az ")

def _is_ops(cmd: str) -> bool:
    return any(cmd.lstrip().startswith(p) for p in _OPS_PREFIXES)

def _input_summary(tool_name: str, inp: dict) -> str:
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        # Inline python3 -c — anywhere in the command (direct or inside docker exec)
        m = _re.search(r'python3?\s+-c\s+["\'](.+)', cmd, _re.DOTALL)
        if m:
            # Use full code — no truncation — for accurate analysis
            code = m.group(1).rstrip("'\"").strip()
            summary = _summarise_inline_python(code)
            # Prefix with container name if this is docker exec
            dm = _re.match(r'docker\s+exec\s+(\S+)', cmd)
            prefix = f"docker exec {dm.group(1)}  " if dm else ""
            return f"{prefix}python3 -c  [{summary}]"
        return cmd[:120]
    fp = inp.get("file_path") or inp.get("path") or ""
    if fp:
        # strip repo root
        for marker in ("/codeKG/", "/codekg/"):
            if marker in fp:
                fp = fp.split(marker)[-1]
                break
        return fp
    return (inp.get("question") or inp.get("query") or inp.get("fqn") or
            inp.get("description") or "")[:120]

def parse_transcript(lines: list[str]) -> list[Turn]:
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    # Build tool_result lookup
    tool_results: dict[str, str] = {}
    for e in entries:
        if e.get("type") == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id", "")
                        rc = b.get("content", "")
                        if isinstance(rc, list):
                            rc = " ".join(c.get("text", "") for c in rc if isinstance(c, dict))
                        tool_results[tid] = str(rc)[:300]

    turns: list[Turn] = []
    current_turn: Optional[Turn] = None
    prev_cache_read = 0
    prev_cache_creation = 0
    step_counter = 0

    for e in entries:
        etype = e.get("type")

        if etype == "user":
            content = e.get("message", {}).get("content", [])
            # Real user message (not just tool results)
            if isinstance(content, str):
                prompt = content[:200]
                current_turn = Turn(index=len(turns), prompt=prompt)
                turns.append(current_turn)
                step_counter = 0
            elif isinstance(content, list):
                has_text = any(b.get("type") == "text" for b in content if isinstance(b, dict))
                if has_text:
                    text = " ".join(b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("type") == "text")
                    current_turn = Turn(index=len(turns), prompt=text[:200])
                    turns.append(current_turn)
                    step_counter = 0

        elif etype == "assistant" and current_turn is not None:
            msg = e.get("message", {})
            u = msg.get("usage", {}) or {}

            cur_cr  = u.get("cache_read_input_tokens", 0) or 0
            cur_cc  = u.get("cache_creation_input_tokens", 0) or 0
            cur_inp = u.get("input_tokens", 0) or 0
            cur_out = u.get("output_tokens", 0) or 0

            step_delta = (cur_cr - prev_cache_read) + (cur_cc - prev_cache_creation) + cur_inp + cur_out
            prev_cache_read = max(prev_cache_read, cur_cr)
            prev_cache_creation = max(prev_cache_creation, cur_cc)

            # Update turn-level token totals
            current_turn.cache_read_tokens = cur_cr
            current_turn.input_context = cur_cr + cur_cc + cur_inp
            current_turn.output_tokens += cur_out

            content = msg.get("content", [])
            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            text_parts = [b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text"]

            for tu in tool_uses:
                name = tu.get("name", "")
                tid  = tu.get("id", "")
                inp  = tu.get("input") or {}
                summary = _input_summary(name, inp)
                result  = tool_results.get(tid, "")
                step_counter += 1
                step = Step(
                    index=step_counter,
                    tool_name=name,
                    tool_use_id=tid,
                    input_summary=summary,
                    result_preview=result[:200],
                    step_tokens=max(0, step_delta),
                    is_ops=(name == "Bash" and _is_ops(inp.get("command", ""))),
                    is_codekg="codekg" in name.lower(),
                )
                current_turn.steps.append(step)

            if text_parts and not tool_uses:
                current_turn.response_text = " ".join(text_parts)[:400]
                current_turn.is_complete = True

    return turns

# ── Auto-detect latest transcript ────────────────────────────────────────────

def find_latest_transcript() -> Optional[Path]:
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None

# ── Colours ───────────────────────────────────────────────────────────────────

C_NORMAL   = 0
C_HEADER   = 1   # bold white on blue
C_SELECTED = 2   # black on cyan
C_DIM      = 3   # grey
C_GREEN    = 4
C_YELLOW   = 5
C_RED      = 6
C_CYAN     = 7
C_MAGENTA  = 8
C_BOLD     = 9

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER,   curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(C_DIM,      curses.COLOR_WHITE,  -1)   # white — readable on any monitor
    curses.init_pair(C_GREEN,    curses.COLOR_GREEN,  -1)
    curses.init_pair(C_YELLOW,   curses.COLOR_YELLOW, -1)
    curses.init_pair(C_RED,      curses.COLOR_RED,    -1)
    curses.init_pair(C_CYAN,     curses.COLOR_CYAN,   -1)
    curses.init_pair(C_MAGENTA,  curses.COLOR_MAGENTA,-1)
    curses.init_pair(C_BOLD,     curses.COLOR_WHITE,  -1)

def cp(n: int) -> int:
    return curses.color_pair(n)

# ── Rendering helpers ─────────────────────────────────────────────────────────

def _tool_color(step: Step) -> int:
    if step.is_codekg: return cp(C_GREEN) | curses.A_BOLD
    if step.is_ops:    return cp(C_YELLOW)
    name = step.tool_name
    if name in ("Edit", "Write", "str_replace_based_edit_tool"): return cp(C_YELLOW)
    if name == "Read":  return cp(C_CYAN)
    if name == "Bash":  return cp(C_MAGENTA)
    return cp(C_DIM)

def _tool_short(name: str) -> str:
    name = name.replace("mcp__codekg__", "kg:")
    name = name.replace("str_replace_based_edit_tool", "Edit")
    return name[:18]

def _fmt_tok(n: int) -> str:
    if n >= 1000: return f"{n/1000:.1f}k"
    return str(n)

import unicodedata as _unicodedata

def _char_width(c: str) -> int:
    eaw = _unicodedata.east_asian_width(c)
    return 2 if eaw in ('W', 'F') else 1

def _clip_to_width(text: str, max_w: int) -> str:
    """Clip text to at most max_w terminal columns, respecting double-width chars."""
    cols = 0
    for i, c in enumerate(text):
        cw = _char_width(c)
        if cols + cw > max_w:
            return text[:i]
        cols += cw
    return text

def _str_width(text: str) -> int:
    """Return the terminal column width of text."""
    return sum(_char_width(c) for c in text)

def _sanitize(text: str) -> str:
    """Replace control characters (newlines, tabs, ANSI escapes, etc.) with safe equivalents."""
    import re as _re2
    # strip ANSI escape sequences
    text = _re2.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    # collapse all whitespace variants to a single space
    return text.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')

def _addstr_clipped(win, y: int, x: int, text: str, attr: int, max_w: int):
    """Write text clipped to max_w terminal columns, never raising on overflow."""
    h, w = win.getmaxyx()
    if y >= h - 1 or x >= w: return
    avail = min(max_w, w - x - 1)
    if avail <= 0: return
    try:
        win.addstr(y, x, _clip_to_width(text, avail), attr)
    except curses.error:
        pass

# ── Session summary builder ───────────────────────────────────────────────────

import re as _re
import collections as _collections
import ast as _ast

def _summarise_inline_python(code: str) -> str:
    """
    Derive a one-line summary of an inline python3 -c '...' program
    using static analysis only — no LLM needed.
    """
    code = code.strip()
    if not code:
        return ""

    # ── First meaningful comment wins — these are often the best description ───
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            comment = stripped.lstrip('#').strip()
            # Skip trivial comments: drop total, 0-indexed, etc.
            if len(comment) > 10 and not _re.match(r'^[\d\s\-\(\)]+$', comment):
                # Capitalise and strip trailing punctuation
                comment = comment[0].upper() + comment[1:]
                comment = comment.rstrip('.').rstrip(',')
                return comment[:80]
        elif stripped and not stripped.startswith('import') and not stripped.startswith('from'):
            # Hit real code before any comment — stop looking
            break

    # ── Fast-path regex checks before AST (handles truncated/broken code) ──────
    if _re.search(r'py_compile\.compile', code):
        return "syntax check"
    if _re.search(r'from scripts\.sidecar import|from sidecar import', code):
        if 'build_session_summary' in code:    return "test session summary"
        if '_summarise_inline_python' in code: return "test inline python summariser"
        if '_step_bucket' in code:             return "test step bucketing"
        if '_input_summary' in code:           return "analyse transcript"
        if 'parse_transcript' in code:         return "analyse transcript"
        return "test sidecar"
    if _re.search(r'from agent_index', code):
        if '_detect_datastores' in code:       return "test datastore detection"
        if '_con\b' in code or '_con()' in code or "execute(" in code:
            if _re.search(r'SELECT|INSERT|UPDATE|DELETE', code, _re.I):
                verb = _re.search(r'\b(SELECT|INSERT|UPDATE|DELETE)\b', code, _re.I)
                return f"query agent index DB: {verb.group(1).lower()}" if verb else "query agent index DB"
            return "query agent index DB"
        if 'list_files' in code:               return "list agent index files"
        if 'toggle_hidden' in code:            return "toggle agent index visibility"
        if 'mark_published' in code:           return "mark index files published"
        if 'generate_' in code:               return "test index generator"
        return "agent index operation"
    if _re.search(r'import sys.*sys\.path\.insert', code, _re.DOTALL):
        # Strip the sys.path boilerplate and re-analyse what follows
        rest = _re.sub(r'^[^\n]*sys\.path[^\n]*\n', '', code, flags=_re.MULTILINE).strip()
        if rest:
            return _summarise_inline_python(rest)
    if _re.search(r'\.jsonl|transcript|sessions_dir|parse_transcript', code):
        if 'json.loads' in code or 'splitlines' in code:
            return "parse transcript for analysis"
        if 'rglob' in code or 'glob' in code or 'sorted' in code:
            return "find + read transcript"
        return "read transcript"
    if 'repos.json' in code or 'registry.json' in code:
        return "look up repo registry"
    if 'readlines' in code and ('writelines' in code or ".write(" in code):
        return "edit file in place"
    if _re.match(r'import \w+.*print.*ok', code, _re.DOTALL) and len(code) < 100:
        return "check module available"
    # sys.path manipulation prefix — strip it and re-analyse the real code
    if code.startswith("import sys; sys.path.insert") or code.startswith("import sys\nsys.path"):
        rest = _re.sub(r'^import sys[^\n]*\n', '', code).strip()
        if rest:
            return _summarise_inline_python(rest)

    # Parse the AST for richer analysis
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        # Fall back to regex heuristics on unparseable code
        return _summarise_inline_python_regex(code)

    # Collect: imports, top-level calls, assignments, print args
    imports:   list[str] = []
    calls:     list[str] = []
    prints:    list[str] = []
    db_ops:    list[str] = []   # sqlite/neo4j operations

    for node in _ast.walk(tree):
        # Imports
        if isinstance(node, _ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])

        # Function calls
        elif isinstance(node, _ast.Call):
            func = node.func
            fname = ""
            if isinstance(func, _ast.Attribute):
                fname = func.attr
            elif isinstance(func, _ast.Name):
                fname = func.id
            if fname:
                calls.append(fname.lower())

            # Capture print arguments as plain text
            if fname == "print":
                for arg in node.args:
                    if isinstance(arg, _ast.Constant) and isinstance(arg.value, str):
                        prints.append(arg.value[:40])
                    elif isinstance(arg, _ast.JoinedStr):  # f-string
                        # collect the literal parts
                        lit = "".join(
                            v.value for v in arg.values
                            if isinstance(v, _ast.Constant) and isinstance(v.value, str)
                        )
                        if lit:
                            prints.append(lit[:40])

        # SQL strings — detect execute("SELECT/INSERT/UPDATE/DELETE ...")
        elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
            upper = node.value.strip().upper()
            for kw in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
                if upper.startswith(kw):
                    db_ops.append(kw.lower())
                    break

    # Match patterns → summary
    imp_set = set(imports)

    # Database introspection
    if "sqlite3" in imp_set and "pragma" in " ".join(calls + db_ops).lower():
        return "inspect SQLite schema"
    if "sqlite3" in imp_set and db_ops:
        return f"SQLite: {db_ops[0]} query"
    if "sqlite3" in imp_set and "execute" in calls:
        return "SQLite query"
    if "sqlite3" in imp_set:
        return "SQLite inspection"

    # Neo4j
    if "neo4j" in imp_set and ("run" in calls or "session" in calls):
        # Look for Cypher verb in string constants
        cypher_verbs = [v for v in db_ops if v in ("match", "return", "merge", "create")]
        if cypher_verbs:
            return f"Neo4j: {cypher_verbs[0].upper()} query"
        return "Neo4j query"

    # Sidecar / transcript parsing (import as 'scripts.sidecar' or direct)
    if "sidecar" in imp_set or "scripts" in imp_set or "parse_transcript" in calls or \
       any("sidecar" in i for i in imports):
        if "build_session_summary" in calls:    return "test session summary"
        if "_summarise_inline_python" in calls or "_summarise" in " ".join(calls):
            return "test inline python summariser"
        if "_step_bucket" in calls:             return "test step bucketing"
        if "compile" in calls:                  return "syntax check"
        return "test sidecar parser"

    # JSON
    if "json" in imp_set and "loads" in calls and "urllib" in imp_set:
        return "HTTP request → parse JSON"
    if "json" in imp_set and "loads" in calls:
        return "parse JSON data"

    # HTTP / API calls
    if "urllib" in imp_set and "urlopen" in calls:
        # Look for URL hint
        url_nodes = [
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str)
            and ("http://" in n.value or "https://" in n.value)
        ]
        if url_nodes:
            url = url_nodes[0].value
            path = url.split("//", 1)[-1].split("/", 1)[-1] if "/" in url else url
            return f"call API: /{path[:30]}"
        return "HTTP request"

    # File operations — go deeper based on what files and what's done with them
    uses_path = "pathlib" in imp_set or "Path" in {
        n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)
    }
    if uses_path or "open" in calls:
        # Extract ALL string literals as hints (paths + comments + names)
        all_strings = " ".join(
            n.value for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str)
        ).lower()
        # Also use variable names and attribute names as hints
        var_names = " ".join(
            n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)
        ).lower()
        hints = all_strings + " " + var_names

        # In-place file surgery (readlines + writelines / direct line manipulation)
        if "readlines" in calls and ("writelines" in calls or "write" in calls):
            return "edit file in place"
        if "readlines" in calls:
            return "read file lines"

        # Transcript / session analysis
        if any(x in hints for x in (".jsonl", "jsonl", "sessions_dir", "session_id",
                                     "parse_transcript", "transcript")):
            if any(x in hints for x in ("parse_transcript", "sidecar", "build_session")):
                if "build_session_summary" in hints: return "test session summary"
                if "docker" in hints:                return "analyse docker cmds in transcript"
                if "_summarise" in hints:            return "test inline python summariser"
                return "analyse transcript"
            if "json" in imp_set and "loads" in calls:
                return "parse transcript for analysis"
            if "rglob" in calls or "glob" in calls or "sorted" in calls:
                return "find + read transcript"
            return "read transcript file"

        # Registry / config lookup
        if any(x in hints for x in ("repos.json", "registry.json", "registry")):
            if "isdir" in calls or "exists" in calls:
                return "look up repo path + verify"
            return "look up repo registry"

        # Agent index files
        if ".codekg" in hints or "agent_index" in imp_set or \
           any(x in hints for x in ("list_files", "agent_index_files", "store")):
            if "list_files" in calls:      return "list agent index files"
            if "write_text" in calls:      return "write agent index file"
            if "read_text" in calls:       return "read agent index file"
            if "toggle_hidden" in calls:   return "toggle agent index visibility"
            return "agent index operation"

        # DB / config files
        if ".db" in hints and "sqlite" not in imp_set:
            return "open database file"
        if any(x in hints for x in ("repos.json", ".json")) and "loads" in calls:
            return "read + parse JSON config"
        if ".json" in hints:
            return "read JSON file"

        # Filesystem discovery
        if "rglob" in calls or "glob" in calls:
            if "jsonl" in hints:            return "find transcript files"
            if ".py" in hints:              return "find Python source files"
            if "stat" in calls or "mtime" in hints: return "find newest file"
            return "search directory tree"

        # Writing vs reading
        if "write_text" in calls:           return "write file(s)"
        if "unlink" in calls:               return "delete file(s)"
        if "read_text" in calls and "json" in imp_set and "loads" in calls:
            return "read + parse file"
        if "read_text" in calls:            return "read file(s)"
        if "stat" in calls or "getsize" in calls:
            return "inspect file metadata"
        if "isdir" in calls or "exists" in calls:
            return "check path exists"

    # OS / filesystem
    if "os" in imp_set:
        if "listdir" in calls:  return "list directory"
        if "walk" in calls:     return "walk directory tree"
        if "getsize" in calls:  return "check file sizes"
        if "isdir" in calls or "path.join" in " ".join(calls):
            return "check filesystem paths"

    # Subprocess
    if "subprocess" in imp_set:
        return "run subprocess"

    # Stdin processing (piped input)
    if "stdin" in {n.attr for n in _ast.walk(tree) if isinstance(n, _ast.Attribute)}:
        if "int" in calls or "float" in calls:
            return "process piped numeric data"
        return "process piped input"

    # Regex / text analysis
    if "re" in imp_set and "findall" in calls or "search" in calls:
        all_strings_raw = [
            n.value for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str)
        ]
        # Look for target of analysis
        if any("question" in s.lower() or "prompt" in s.lower() for s in all_strings_raw):
            return "test keyword extraction"
        return "regex analysis"

    # Agent index / store operations (inside container, no pathlib)
    if any(x in imp_set for x in ("store", "agent_index")) or \
       any("agent_index" in i for i in imports):
        if "list_files" in calls:           return "list agent index files"
        if "toggle_hidden" in calls:        return "toggle agent index visibility"
        if "_con" in calls or "execute" in calls:
            # Check what SQL is being run
            if db_ops:                      return f"query agent index DB: {db_ops[0]}"
            return "query agent index DB"
        if "upsert_file" in calls:          return "write to agent index store"
        if "mark_published" in calls:       return "mark index files published"
        return "agent index operation"

    if "generator" in imp_set or any("generator" in i for i in imports):
        if "detect_datastores" in calls:    return "test datastore detection"
        if any("generate_" in c for c in calls): return "test index generator"
        return "test agent index generator"

    # Print-only scripts — describe by what they print
    if prints:
        first = prints[0].strip().rstrip(":").lower()
        if first and len(first) > 2:
            return f"print: {first[:40]}"

    # Generic fallback: describe by primary import
    meaningful = [i for i in imports if i not in ("sys", "os", "re", "json",
                                                    "__future__", "typing", "collections")]
    if meaningful:
        top = meaningful[0]
        # Make the import name human-readable
        top_clean = top.replace("_", " ")
        return f"use {top_clean}"

    return "inline python"


def _summarise_inline_python_regex(code: str) -> str:
    """Regex fallback for code that won't parse."""
    if _re.search(r'sqlite3', code):
        if _re.search(r'SELECT|INSERT|UPDATE|DELETE', code, _re.I):
            return "SQLite query"
        return "SQLite inspection"
    if _re.search(r'neo4j', code):
        return "Neo4j query"
    if _re.search(r'urllib|requests\.get', code):
        return "HTTP request"
    if _re.search(r'open\(|read_text|write_text', code):
        return "file operation"
    return "inline python"


def _step_bucket(step: Step) -> str:
    """
    Derive a human-readable activity bucket for a step entirely from its data.
    No hardcoded bucket names — the bucket is derived from the tool and input.
    """
    name = step.tool_name
    summary = step.input_summary.strip()

    # CodeKG MCP tools — group by the specific tool suffix
    if "codekg" in name.lower():
        suffix = name.replace("mcp__codekg__", "").replace("_", " ")
        return f"kg: {suffix}"

    # Bash — group by verb, with extra depth for docker
    if name == "Bash":
        cmd = summary.lstrip()
        # Strip env-var prefixes like FOO=bar cmd ...
        cmd = _re.sub(r'^([A-Z_]+=\S+\s+)+', '', cmd)
        parts = cmd.split()
        verb = parts[0] if parts else "bash"
        verb = verb.split("/")[-1]          # strip path prefix

        # Docker — one more level of detail
        if verb == "docker":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "exec":
                return "docker exec  (inspect container)"
            elif sub in ("compose",):
                # further: up/down/build = infra, logs = monitoring
                action = parts[2] if len(parts) > 2 else ""
                if action in ("up", "down", "restart", "build", "start", "stop"):
                    return f"docker compose {action}  (infra)"
                return "docker compose  (infra)"
            elif sub in ("ps", "stats"):
                return f"docker {sub}  (monitoring)"
            elif sub in ("logs",):
                return "docker logs  (monitoring)"
            elif sub in ("build",):
                return "docker build  (infra)"
            elif sub in ("pull", "push", "tag"):
                return f"docker {sub}  (registry)"
            else:
                return f"docker {sub}" if sub else "docker"

        verb = verb.rstrip("3456789")       # python3 → python
        if verb in ("python", "py"):        verb = "python"
        if verb in ("node", "nodejs"):      verb = "node"
        return f"bash: {verb}"

    # Read/Write/Edit — group by file type or notable path segment
    if name in ("Read", "Write", "Edit", "str_replace_based_edit_tool"):
        op = "read" if name == "Read" else "write/edit"
        if not summary:
            return op
        # Notable path patterns take priority
        for patterns, label in [
            ((".codekg/",),            "codekg index file"),
            (("templates/",),          "template"),
            (("/tests/", "test_"),     "test file"),
            ((".md",),                 "markdown"),
            ((".json",),               "json"),
            ((".yml", ".yaml"),        "yaml/config"),
        ]:
            if any(pat in summary for pat in patterns):
                return f"{op}: {label}"
        # Fall back to file extension
        ext = summary.rsplit(".", 1)[-1].lower() if "." in summary else ""
        if ext in ("py", "js", "ts", "go", "java", "cpp", "c", "rs"):
            return f"{op}: .{ext} source"
        if ext:
            return f"{op}: .{ext}"
        return op

    # Everything else — just use the tool name
    clean = name.replace("mcp__", "").replace("__", ": ").replace("_", " ")
    return clean


def build_session_summary(turns: list[Turn]) -> dict:
    """
    Aggregate token spend and activity across all turns.
    Returns a dict ready for rendering — all buckets derived dynamically.
    """
    if not turns:
        return {}

    total_input   = max(t.input_context for t in turns)   # cumulative high-water
    total_output  = sum(t.output_tokens for t in turns)
    total_cache   = max(t.cache_read_tokens for t in turns)
    total_steps   = sum(len(t.steps) for t in turns)

    # Token spend by activity bucket
    bucket_tokens: dict[str, int]      = _collections.defaultdict(int)
    bucket_steps:  dict[str, int]      = _collections.defaultdict(int)
    # Token spend per turn (for sparkline / most expensive)
    turn_tokens:   list[tuple[int, str]] = []

    for turn in turns:
        turn_tok = 0
        for step in turn.steps:
            tok = step.step_tokens or 0
            bucket = _step_bucket(step)
            bucket_tokens[bucket] += tok
            bucket_steps[bucket]  += 1
            turn_tok += tok
        turn_tokens.append((turn_tok, turn.prompt))

    # Sort buckets by token spend descending
    sorted_buckets = sorted(bucket_tokens.items(), key=lambda x: -x[1])

    # Top files read/edited
    file_counts: dict[str, int] = _collections.defaultdict(int)
    for turn in turns:
        for step in turn.steps:
            if step.tool_name in ("Read", "Edit", "Write", "str_replace_based_edit_tool"):
                key = step.input_summary.strip()
                if key:
                    file_counts[key] += 1
    top_files = sorted(file_counts.items(), key=lambda x: -x[1])[:10]

    # Most expensive turns
    top_turns = sorted(turn_tokens, key=lambda x: -x[0])[:5]

    # Steps with no token data (step_tokens == 0) — count them
    unmetered = sum(
        1 for t in turns for s in t.steps if not s.step_tokens
    )

    return {
        "total_input":    total_input,
        "total_output":   total_output,
        "total_cache":    total_cache,
        "cache_pct":      (total_cache / total_input * 100) if total_input else 0,
        "total_turns":    len(turns),
        "total_steps":    total_steps,
        "unmetered_steps": unmetered,
        "buckets":        sorted_buckets,
        "bucket_steps":   bucket_steps,
        "top_files":      top_files,
        "top_turns":      top_turns,
    }


def render_summary(detail_lines: list, summary: dict, right_w: int):
    """Append session summary lines to detail_lines."""
    W = right_w - 2

    def line(text="", attr=C_NORMAL):
        detail_lines.append((" " + text, attr))

    def ruler():
        detail_lines.append((" " + "─" * W, cp(C_DIM)))

    line("SESSION SUMMARY", cp(C_BOLD) | curses.A_BOLD)
    line()

    # Totals
    tin  = _fmt_tok(summary["total_input"])
    tout = _fmt_tok(summary["total_output"])
    hit  = f"{summary['cache_pct']:.0f}%"
    line(f"Turns: {summary['total_turns']}   Steps: {summary['total_steps']}   "
         f"Input ctx: {tin}   Output: {tout}   Cache hit: {hit}", cp(C_DIM))
    if summary["unmetered_steps"]:
        line(f"Note: {summary['unmetered_steps']} steps have no per-step token data "
             f"(old transcript format)", cp(C_DIM))
    line()
    ruler()

    # Activity breakdown — bar chart
    # Use token data only when the majority of steps actually have token measurements.
    # If most steps are unmetered (old transcript format), step count is more honest.
    metered = summary["total_steps"] - summary["unmetered_steps"]
    has_tok_data = metered > (summary["total_steps"] * 0.5) if summary["total_steps"] else False
    # Sort by steps when no token data; buckets already sorted by tokens when available
    if has_tok_data:
        sorted_display = summary["buckets"]
        metric_label   = "Token spend by activity"
        max_val        = sorted_display[0][1] if sorted_display else 1
        def row_val(bucket, tok): return tok
        def val_str(bucket, tok, steps):
            return f"{_fmt_tok(tok):>7}  {steps:>3} steps"
    else:
        step_items     = sorted(summary["bucket_steps"].items(), key=lambda x: -x[1])
        sorted_display = [(b, 0) for b, _ in step_items]
        metric_label   = "Activity breakdown by step count  (no per-step token data yet)"
        max_val        = step_items[0][1] if step_items else 1
        def row_val(bucket, tok): return summary["bucket_steps"].get(bucket, 0)
        def val_str(bucket, tok, steps):
            return f"{steps:>4} steps"

    line(metric_label, cp(C_CYAN) | curses.A_BOLD)
    line()
    bar_w = min(30, W - 44)
    for bucket, tok in sorted_display:
        steps = summary["bucket_steps"].get(bucket, 0)
        val   = row_val(bucket, tok)
        bar_n = max(1, int(val / max_val * bar_w)) if max_val else 1
        bar   = "█" * bar_n
        label = f"{bucket:<30}"[:30]
        right = val_str(bucket, tok, steps)
        bl    = bucket.lower()
        if "codekg" in bl:                attr = cp(C_GREEN)
        elif "infra" in bl:               attr = cp(C_RED)
        elif "inspect container" in bl:   attr = cp(C_CYAN)
        elif "monitoring" in bl:          attr = cp(C_MAGENTA)
        elif "docker" in bl:              attr = cp(C_YELLOW)
        elif bl.startswith("read"):       attr = cp(C_CYAN)
        elif "write" in bl or "edit" in bl: attr = cp(C_YELLOW)
        elif bl.startswith("bash:"):      attr = cp(C_MAGENTA)
        else:                             attr = cp(C_DIM)
        line(f"{label} {bar:<{bar_w}} {right}", attr)

    line()
    ruler()

    # Top files touched
    if summary["top_files"]:
        line("Most-touched files", cp(C_CYAN) | curses.A_BOLD)
        line()
        for path, count in summary["top_files"]:
            short = path[-W+6:] if len(path) > W - 6 else path
            line(f"  {count:>3}×  {short}", cp(C_DIM))
        line()
        ruler()

    # Most expensive turns
    if any(tok for tok, _ in summary["top_turns"]):
        line("Most token-intensive turns", cp(C_CYAN) | curses.A_BOLD)
        line()
        for tok, prompt in summary["top_turns"]:
            if not tok:
                continue
            short = prompt.replace("\n", " ")[:W - 12]
            line(f"  {_fmt_tok(tok):>6}  {short}", cp(C_DIM))
        line()


def draw_help_popup(stdscr, h: int, w: int):
    """Draw a centred help overlay."""
    lines = [
        "  CodeKG Sidecar — Help  ",
        "",
        "  Navigation",
        "  Tab          Switch focus left ◀ / right ▶ panel",
        "  ↑ / ↓       Navigate turns (left) or scroll (right)",
        "  j / PgDn    Scroll detail down",
        "  k / PgUp    Scroll detail up",
        "",
        "  Modes",
        "  l            Toggle LIVE mode (auto-follow)",
        "  Enter        Pin / unpin current turn",
        "  s            Toggle session summary",
        "",
        "  Other",
        "  h            This help",
        "  q / Esc      Quit",
        "",
        "  Press any key to close",
    ]
    box_w = max(len(l) for l in lines) + 4
    box_h = len(lines) + 2
    y0    = max(1, (h - box_h) // 2)
    x0    = max(1, (w - box_w) // 2)

    # Shadow
    for row in range(box_h):
        _addstr_clipped(stdscr, y0 + row, x0, " " * box_w, cp(C_HEADER), box_w)

    # Border
    try:
        stdscr.attron(cp(C_HEADER))
        stdscr.border() if box_w >= w else None
        for row, text in enumerate(lines):
            _addstr_clipped(stdscr, y0 + 1 + row, x0 + 2, text.ljust(box_w - 4),
                            cp(C_HEADER) | (curses.A_BOLD if row == 0 else 0), box_w - 4)
        stdscr.attroff(cp(C_HEADER))
    except curses.error:
        pass


# ── Main TUI ──────────────────────────────────────────────────────────────────

def run_ui(stdscr, transcript_path: Path):
    curses.curs_set(0)
    stdscr.nodelay(True)
    init_colors()

    turns: list[Turn] = []
    selected = 0       # turn index selected in left pane
    list_scroll = 0    # row offset for left pane scrolling
    detail_scroll = 0  # scroll offset in right pane
    live_mode = True   # auto-follow latest turn
    pinned = False     # user manually selected a turn
    show_summary = False
    show_help = False
    focus = "left"     # which pane owns ↑/↓: "left" or "right"
    selected_step = 0  # step index highlighted in right pane (when focus=="right" and paused)

    file_lines: list[str] = []
    last_mtime = 0.0
    last_size  = 0

    def reload():
        nonlocal file_lines, last_mtime, last_size, turns
        try:
            st = transcript_path.stat()
            if st.st_mtime == last_mtime and st.st_size == last_size:
                return False
            last_mtime = st.st_mtime
            last_size  = st.st_size
            file_lines = transcript_path.read_text(errors="replace").splitlines()
            turns = parse_transcript(file_lines)
            return True
        except Exception:
            return False

    reload()

    while True:
        changed = reload()

        if turns and live_mode and not pinned:
            selected = len(turns) - 1
            detail_scroll = 999999  # will be clamped to max_scroll after detail_lines is built

        h, w = stdscr.getmaxyx()
        left_w  = max(30, w // 3)
        right_w = w - left_w - 1

        stdscr.erase()

        # ── Header bar ────────────────────────────────────────────────────────
        mode_tag = "SUMMARY" if show_summary else ("LIVE" if live_mode else "PAUSED")
        header = f" CodeKG Sidecar  │  {transcript_path.name[:40]}  │  {len(turns)} turns  │  {mode_tag}  │  {time.strftime('%H:%M:%S')} "
        _addstr_clipped(stdscr, 0, 0, header.ljust(w), cp(C_HEADER) | curses.A_BOLD, w)

        # ── Footer bar ────────────────────────────────────────────────────────
        footer = " Tab focus  ↑↓ navigate/scroll  j/k scroll  Enter pin  l live  s summary  h help  q quit "
        _addstr_clipped(stdscr, h-1, 0, footer.ljust(w), cp(C_HEADER), w)

        # ── Left pane: turn list (multi-line prompts) ─────────────────────────
        list_h = h - 2   # usable rows between header and footer
        text_w = left_w - 6  # chars available after the " Tnn " label

        def _wrap_prompt(prompt: str) -> list[str]:
            """Wrap prompt into lines of text_w, preserving newlines."""
            lines = []
            for raw in prompt.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                while len(raw) > text_w:
                    lines.append(raw[:text_w])
                    raw = raw[text_w:]
                lines.append(raw)
            return lines or [""]

        # Build full line list so we can scroll by row
        # Each entry: (turn_index, line_text, is_first_line)
        all_list_rows: list[tuple[int, str, bool]] = []
        for i, turn in enumerate(turns):
            wrapped = _wrap_prompt(turn.prompt)
            for li, line in enumerate(wrapped):
                all_list_rows.append((i, line, li == 0))

        # Ensure selected turn is visible: find its first row
        if all_list_rows:
            sel_first_row = next(
                (r for r, (ti, _, first) in enumerate(all_list_rows) if ti == selected and first),
                0
            )
            sel_last_row = max(
                (r for r, (ti, _, _) in enumerate(all_list_rows) if ti == selected),
                default=sel_first_row
            )
            # Scroll so selected turn is visible
            if sel_first_row < list_scroll:
                list_scroll = sel_first_row
            if sel_last_row >= list_scroll + list_h:
                list_scroll = sel_last_row - list_h + 1
            list_scroll = max(0, list_scroll)

        # Clear all left-pane rows first so no stale content bleeds through
        for r in range(list_h):
            row = r + 1
            if row >= h - 1:
                break
            _addstr_clipped(stdscr, row, 0, " " * left_w, C_NORMAL, left_w)

        for r, (ti, line_text, is_first) in enumerate(all_list_rows[list_scroll:]):
            row = r + 1
            if row >= h - 1:
                break
            is_sel = (ti == selected)
            left_focused = (focus == "left")
            if is_sel and left_focused:
                attr_base = cp(C_SELECTED)
            elif is_sel:
                attr_base = cp(C_DIM) | curses.A_REVERSE
            else:
                attr_base = C_NORMAL

            # Paint the full row background for selected turns
            if is_sel:
                _addstr_clipped(stdscr, row, 0, " " * left_w, attr_base, left_w)

            label = f" T{ti+1:02d} " if is_first else "      "
            if is_first:
                if is_sel and left_focused:
                    label_attr = cp(C_SELECTED)
                elif is_sel:
                    label_attr = cp(C_DIM) | curses.A_REVERSE
                else:
                    label_attr = cp(C_GREEN) | curses.A_BOLD
            else:
                label_attr = attr_base

            # label occupies cols 0-4 (5 chars), text starts at col 5
            _addstr_clipped(stdscr, row, 0, label, label_attr, 5)
            _addstr_clipped(stdscr, row, 5, line_text[:text_w].ljust(text_w), attr_base, text_w)

        # ── Divider — cyan ▶ on left edge when right pane focused, ◀ on right edge when left focused ──
        for row in range(1, h - 1):
            _addstr_clipped(stdscr, row, left_w, "│", cp(C_DIM), 1)
        # Draw a cyan focus arrow at mid-height on the divider
        mid = (h - 2) // 2
        if focus == "right":
            _addstr_clipped(stdscr, mid, left_w, "▶", cp(C_CYAN) | curses.A_BOLD, 1)
        else:
            _addstr_clipped(stdscr, mid, left_w, "◀", cp(C_CYAN) | curses.A_BOLD, 1)

        # ── Right pane: summary or step detail ────────────────────────────────
        rx = left_w + 1
        detail_lines: list[tuple[str, int]] = []

        if show_summary:
            summary = build_session_summary(turns)
            render_summary(detail_lines, summary, right_w)

        elif turns and 0 <= selected < len(turns):
            turn = turns[selected]

            step_navigating = (focus == "right" and not live_mode and not show_summary)

            # Clamp selected_step to valid range
            if turn.steps:
                selected_step = max(0, min(selected_step, len(turn.steps) - 1))
            else:
                selected_step = 0

            # Turn header
            prompt_display = _sanitize(turn.prompt)
            detail_lines.append((f" Turn {turn.index+1}  {prompt_display}", cp(C_BOLD) | curses.A_BOLD))
            detail_lines.append(("", C_NORMAL))

            # Token stats row — show cached and uncached input separately
            cached   = _fmt_tok(turn.cache_read_tokens)
            uncached = _fmt_tok(turn.input_context - turn.cache_read_tokens)
            out      = _fmt_tok(turn.output_tokens)
            hit      = f"{turn.cache_hit_pct:.0f}%"
            stats = (f" In(cached): {cached}  In(new): {uncached}"
                     f"  Out: {out}  Cache: {hit}  Steps: {len(turn.steps)}")
            if turn.ops_tokens:
                stats += f"  Ops: {_fmt_tok(turn.ops_tokens)}"
            detail_lines.append((stats, cp(C_GREEN)))
            detail_lines.append((" " + "─" * (right_w - 3), cp(C_DIM)))

            # Steps — track which detail_lines row each step starts on for auto-scroll
            step_first_line: list[int] = []  # detail_lines index of each step's first line

            for si, step in enumerate(turn.steps):
                is_sel_step = step_navigating and (si == selected_step)
                tool_disp  = _tool_short(step.tool_name)
                tok_disp   = f"+{_fmt_tok(step.step_tokens)}" if step.step_tokens else ""
                badges     = ""
                if step.is_ops:    badges += " [ops]"
                if step.is_codekg: badges += " [kg]"

                step_first_line.append(len(detail_lines))

                if is_sel_step:
                    line_attr = cp(C_SELECTED) | curses.A_BOLD
                    sub_attr  = cp(C_SELECTED)
                else:
                    line_attr = _tool_color(step)
                    sub_attr  = cp(C_DIM)

                # Step number + tool name + token
                line1 = f" {step.index:2d}. {tool_disp:<20}{badges:<8}{tok_disp:>6} "
                detail_lines.append((line1, line_attr))

                # Input summary — full text (wrap long lines) when selected, one line otherwise
                if step.input_summary:
                    raw = _sanitize(step.input_summary)
                    wrap_w = right_w - 10
                    if is_sel_step:
                        first = True
                        while raw:
                            prefix = "     ↳ " if first else "       "
                            clipped = _clip_to_width(raw, wrap_w)
                            detail_lines.append((f"{prefix}{clipped}", sub_attr))
                            raw = raw[len(clipped):]
                            first = False
                    else:
                        detail_lines.append((f"     ↳ {_clip_to_width(raw, wrap_w)}", sub_attr))

                # Result preview — full text when selected, first line otherwise
                if step.result_preview:
                    raw = _sanitize(step.result_preview)
                    wrap_w = right_w - 10
                    if is_sel_step:
                        first = True
                        while raw:
                            prefix = "     → " if first else "       "
                            clipped = _clip_to_width(raw, wrap_w)
                            detail_lines.append((f"{prefix}{clipped}", sub_attr))
                            raw = raw[len(clipped):]
                            first = False
                    else:
                        detail_lines.append((f"     → {_clip_to_width(raw, wrap_w)}", sub_attr))

                detail_lines.append(("", C_NORMAL))

            # Auto-scroll so selected step is visible in right pane
            if step_navigating and turn.steps:
                step_row = step_first_line[selected_step]
                visible_h = h - 3
                if step_row < detail_scroll:
                    detail_scroll = step_row
                elif step_row >= detail_scroll + visible_h:
                    detail_scroll = step_row - visible_h + 1

            # Final response text
            if turn.response_text:
                detail_lines.append((" " + "─" * (right_w - 3), cp(C_DIM)))
                detail_lines.append((" Response:", cp(C_CYAN) | curses.A_BOLD))
                wrap_w = right_w - 3
                words = _sanitize(turn.response_text).split()
                line_buf = " "
                for word in words:
                    candidate = line_buf + (" " if line_buf.strip() else "") + word
                    if _str_width(candidate) > wrap_w:
                        detail_lines.append((line_buf, C_NORMAL))
                        line_buf = "  " + word
                    else:
                        line_buf = candidate
                if line_buf.strip():
                    detail_lines.append((line_buf, C_NORMAL))
            elif not turn.is_complete and turn.steps:
                detail_lines.append(("", C_NORMAL))
                detail_lines.append((" ⟳ running…", cp(C_YELLOW)))

        # Render detail_lines into right pane (shared by both modes)
        max_scroll = max(0, len(detail_lines) - (h - 3))
        detail_scroll = min(detail_scroll, max_scroll)
        for i, (text, attr) in enumerate(detail_lines[detail_scroll:]):
            row = i + 1
            if row >= h - 1:
                break
            _addstr_clipped(stdscr, row, rx, text, attr, right_w - 1)

        # Help popup (drawn on top of everything)
        if show_help:
            draw_help_popup(stdscr, h, w)

        stdscr.refresh()

        # ── Input handling ─────────────────────────────────────────────────────
        try:
            key = stdscr.getch()
        except Exception:
            key = -1

        if show_help:
            if key != -1:
                show_help = False
            time.sleep(0.2)
            continue

        if key in (ord('q'), ord('Q')):
            break
        elif key == 27:  # Esc — close summary/help or quit
            if show_summary: show_summary = False
            elif show_help:  show_help = False
            else:            break
        elif key in (ord('h'), ord('H')):
            show_help = True
        elif key in (ord('s'), ord('S')):
            show_summary = not show_summary
            detail_scroll = 0
        elif key == ord('\t'):  # Tab — switch focus between panels
            focus = "right" if focus == "left" else "left"
            selected_step = 0
            if focus == "right" and live_mode:
                # Pause so arrow keys navigate steps immediately
                live_mode = False
                pinned = True
        elif key == curses.KEY_UP:
            if focus == "left":
                selected = max(0, selected - 1)
                pinned = True
                live_mode = False
                detail_scroll = 0
                selected_step = 0
            elif not live_mode and not show_summary:
                selected_step = max(0, selected_step - 1)
            else:
                detail_scroll = max(0, detail_scroll - 1)
        elif key == curses.KEY_DOWN:
            if focus == "left":
                selected = min(len(turns) - 1, selected + 1) if turns else 0
                pinned = True
                live_mode = False
                detail_scroll = 0
                selected_step = 0
            elif not live_mode and not show_summary:
                cur_turn = turns[selected] if turns and 0 <= selected < len(turns) else None
                max_step = len(cur_turn.steps) - 1 if cur_turn else 0
                selected_step = min(max_step, selected_step + 1)
            else:
                detail_scroll += 1
        elif key in (curses.KEY_PPAGE, ord('k')):
            detail_scroll = max(0, detail_scroll - 10)
        elif key in (curses.KEY_NPAGE, ord('j')):
            detail_scroll += 10
        elif key in (10, 13, curses.KEY_ENTER):
            pinned = not pinned
            if not pinned:
                live_mode = True
                detail_scroll = 999999  # clamped to max_scroll after render
        elif key in (ord('l'), ord('L')):
            live_mode = not live_mode
            if live_mode:
                pinned = False
                detail_scroll = 999999  # clamped to max_scroll after render

        time.sleep(0.2)


_HELP = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                        CodeKG Sidecar — User Guide                         ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT IT DOES
  Sidecar watches a Claude Code transcript file in real time and gives you a
  live, structured view of everything Claude is doing — step by step, turn by
  turn. It reads the same JSON lines that Claude Code writes to disk, so there
  is no network connection and nothing to configure.

  Open it in a second terminal window while Claude Code is running. It updates
  automatically as new turns stream in.

USAGE
  python3 txviewer.py                   # auto-detect the latest session
  python3 txviewer.py --list            # list recent sessions to pick one
  python3 txviewer.py <session.jsonl>   # watch a specific file
  python3 txviewer.py <session-id>      # match by session ID prefix

  Transcripts live in:
    ~/.claude/projects/<project-slug>/<session-id>.jsonl

LAYOUT
  ┌────────────────┬─────────────────────────────────────────────────────────┐
  │  Turn list     │  Step detail / Session summary                          │
  │                │                                                         │
  │  T01 prompt…   │  Turn N  <full prompt>                                  │
  │  T02 prompt…   │  In(cached): 348k  In(new): 12k  Out: 8k  Cache: 97%  │
  │► T03 prompt…   │                                                         │
  │                │   1. Read    services/api/main.py                       │
  │                │   2. Edit    services/api/main.py                       │
  │                │   3. Bash    docker compose up --build api              │
  │                │   4. kg:     answer_question  [...]                     │
  └────────────────┴─────────────────────────────────────────────────────────┘

LEFT PANE — Turn list
  Each turn is the full prompt you typed, word-wrapped across as many lines as
  needed. The highlighted turn is the one shown in the right pane.

RIGHT PANE — Step detail
  Shows every tool call Claude made for the selected turn:
  • Tool name (Read / Edit / Bash / kg: answer_question / etc.)
  • Target — the file path, bash command, or question asked
  • For  python3 -c  commands: a one-line summary derived from the code itself
    e.g.  python3 -c  [SQLite: select query]
          python3 -c  [Why are there no cross-module dependencies?]  ← comment
  • Token delta for that step (when available)
  • [ops] badge on infra commands  (docker compose up/down/build)
  • [kg]  badge on CodeKG MCP calls

TOKEN STATS (green line under each turn)
  In(cached)   Tokens read from Anthropic's prompt cache — cheapest category
  In(new)      Uncached input tokens — new context added this turn
  Out          Output tokens generated
  Cache %      Fraction of input served from cache
  Ops          Tokens spent on ops/infra steps (docker, kubectl, etc.)

SESSION SUMMARY  (press s)
  A bar chart of every activity bucket across the whole session, ranked by
  step count (or by token spend when per-step data is available).

  Buckets are derived on the fly from what actually happened — no hardcoded
  list. Docker is split into:
    docker exec  (inspect container)  — discovery / debug inside a container
    docker compose <action>  (infra)  — starting, stopping, building services
    docker logs/ps  (monitoring)      — observing running state

  Below the chart: most-touched files and most token-intensive turns.

LIVE vs PAUSED
  LIVE    The right pane scrolls to the latest step as it arrives.
  PAUSED  You navigated away; sidecar still watches but holds your position.
          Tab into the right pane to step through individual tool calls.

PANEL FOCUS
  The divider between the two panes shows a cyan arrow indicating which panel
  is active:  ◀  means the left (turn list) is focused,  ▶  means the right
  (step detail) is focused.

  Press Tab to switch focus. Arrow keys act on whichever panel is focused:
    Left focused   ↑ / ↓  navigate between turns
    Right focused  ↑ / ↓  move the cyan cursor between steps in the turn.
                           The selected step expands its full input and result.
                           Tabbing to the right pane auto-pauses live mode.

KEYBOARD SHORTCUTS
  Tab          Switch focus between left and right pane
  ↑ / ↓        Navigate turns (left focus) or step through steps (right focus)
  j / PgDn     Scroll detail pane down
  k / PgUp     Scroll detail pane up
  l            Toggle LIVE mode
  Enter        Pin / unpin selected turn  (unpin returns to LIVE)
  s            Toggle session summary
  h            In-app help overlay
  q / Esc      Quit

TIPS
  • Run sidecar before you start a Claude Code session so it picks up turn 1.
  • Press Tab then ↑/↓ to inspect any step in full — input and result expand.
  • Press s after a long session to see where the time actually went.
  • The "In(new)" number tells you how much new context was added each turn —
    a high number means Claude read a lot of new files or tool results.
  • Steps with  [kg]  are CodeKG MCP calls — these replace greps and file reads.
    The session summary shows how many steps those saved.
"""


def list_sessions() -> list[Path]:
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_transcript(arg: str) -> Optional[Path]:
    """
    Accept:
      - a full or relative path to a .jsonl file
      - a session ID or prefix (matched against filenames across all projects)
    """
    # Exact path
    p = Path(arg)
    if p.exists() and p.suffix == ".jsonl":
        return p

    # Session ID / prefix match — search ~/.claude/projects recursively
    sessions = list_sessions()
    matches = [s for s in sessions if s.stem.startswith(arg) or arg in s.stem]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous session ID '{arg}' — {len(matches)} matches:", file=sys.stderr)
        for m in matches[:8]:
            print(f"  {m.stem[:36]}  {m.parent.name}", file=sys.stderr)
        sys.exit(1)

    return None


def slug_to_human_path(slug: str) -> str:
    """Reconstruct a human-readable path from a Claude Code project slug.

    Claude Code slugifies project paths by replacing '/' with '-' and '.' with '-',
    so '--' in the slug marks a hidden directory (path-sep + dot).  We probe the
    filesystem to disambiguate hyphens-as-separator from hyphens-in-name.
    """
    HIDDEN = "\x00"
    # '--' encodes '/<hidden-dir>' (the dot is also replaced with '-')
    normalized = slug.replace("--", HIDDEN).lstrip("-").replace(HIDDEN, "/.")
    segs = normalized.split("/")

    # Flatten into parts with MUST-split markers between known '/' boundaries
    MUST = object()
    flat: list = []
    for i, seg in enumerate(segs):
        if i > 0:
            flat.append(MUST)
        flat.extend(seg.split("-"))

    best: list[str] = [""]

    def solve(idx: int, current: Path) -> None:
        if idx == len(flat):
            p = str(current)
            if len(p) > len(best[0]):
                best[0] = p
            return
        if flat[idx] is MUST:
            solve(idx + 1, current)
            return
        seg = flat[idx]
        j = idx
        while True:
            candidate = current / seg
            if candidate.exists():
                solve(j + 1, candidate)
            j += 1
            if j >= len(flat) or flat[j] is MUST:
                break
            seg = seg + "-" + flat[j]

    solve(0, Path("/"))

    result = best[0] if best[0] else "/" + normalized.replace("-", "/")
    home = str(Path.home())
    if result.startswith(home):
        result = "~" + result[len(home):]
    return result


def print_session_list():
    sessions = list_sessions()
    if not sessions:
        print("No transcripts found in ~/.claude/projects/")
        return

    print(f"{'#':>3}  {'Session ID':36}  {'Project':30}  {'Modified':16}  {'Size':>6}")
    print("─" * 100)
    for i, s in enumerate(sessions[:30], 1):
        project = slug_to_human_path(s.parent.name)
        if len(project) > 30:
            project = "…" + project[-29:]
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.stat().st_mtime))
        size_kb = s.stat().st_size // 1024
        print(f"{i:>3}  {s.stem[:36]:36}  {project:30}  {mtime}  {size_kb:>5}k")

    print()
    print(f"Showing {min(30, len(sessions))} of {len(sessions)} sessions.")
    print("Run:  python3 txviewer.py <session-id>  to open one.")


def main():
    args = sys.argv[1:]

    if args and args[0] in ("--help", "-h", "help"):
        print(_HELP)
        sys.exit(0)

    if args and args[0] in ("--list", "-l", "list"):
        print_session_list()
        sys.exit(0)

    if args:
        path = resolve_transcript(args[0])
        if path is None:
            print(f"No transcript found matching '{args[0]}'.", file=sys.stderr)
            print("Run  python3 txviewer.py --list  to see available sessions.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Watching: {path}", flush=True)
        time.sleep(0.3)
    else:
        path = find_latest_transcript()
        if not path:
            print("No Claude Code transcript found. Start a Claude Code session first.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Watching: {path}", flush=True)
        time.sleep(0.3)

    try:
        curses.wrapper(run_ui, path)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
