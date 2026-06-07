#!/usr/bin/env python3
"""
insightviewer — live **Insight:** tracker for Claude Code sessions.

Watches a transcript file and collects every **Insight:** block from assistant
responses. In LIVE mode insights appear as they arrive. In PAUSE mode they are
sorted by: program → module → class → method → time.

Usage:
    python3 insightviewer.py                  # auto-detect latest session
    python3 insightviewer.py <session.jsonl>  # watch specific file
    python3 insightviewer.py --list           # pick a session

Keys:
    l          toggle live / pause mode
    ↑ / ↓      scroll insight list
    j / k      scroll detail pane (selected insight full text)
    Tab        switch focus between list and detail pane
    Enter      expand / collapse selected insight
    s          cycle sort key (in pause mode)
    h          help
    q / Esc    quit
"""
from __future__ import annotations

import curses
import json
import os
import re
import sys
import time
import unicodedata as _unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Insight:
    turn_index: int        # which turn it came from (0-based)
    turn_prompt: str       # first line of the prompt that produced it
    text: str              # full insight text (stripped)
    timestamp: float       # st_mtime of the file when this turn was parsed
    # derived sort keys (extracted from backtick refs)
    ref_file: str = ""     # e.g. "services/ingestion/kg/writer.py"
    ref_module: str = ""   # e.g. "writer"
    ref_class: str = ""    # e.g. "KGWriter"
    ref_method: str = ""   # e.g. "write_node"

_NONE_PATTERNS = re.compile(
    r"^(none|no new|nothing|n/a|session started|no insight)",
    re.IGNORECASE,
)

def _is_empty(text: str) -> bool:
    return bool(_NONE_PATTERNS.match(text.strip()))

_BACKTICK_REF = re.compile(r"`([^`]+)`")

_FILE_EXTS = {".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".cpp",
              ".c", ".h", ".html", ".css", ".json", ".yaml", ".yml", ".md"}

def _looks_like_file(r: str) -> bool:
    """True if r is a plausible source file path (not a URL or CSS selector)."""
    if r.startswith(("http", "/.", ".", "#")):
        return False
    # must contain a known extension
    suffix = "." + r.rsplit(".", 1)[-1] if "." in r else ""
    if suffix not in _FILE_EXTS:
        return False
    # reject paths that look like URL routes or CSS (contain spaces, {, /)
    if " " in r or "{" in r:
        return False
    return True

def _extract_refs(text: str) -> tuple[str, str, str, str]:
    """Heuristically extract (file, module, class, method) from backtick refs."""
    refs = _BACKTICK_REF.findall(text)
    ref_file = ref_module = ref_class = ref_method = ""

    for r in refs:
        # real source file path
        if _looks_like_file(r) and not ref_file:
            ref_file = r
            stem = r.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if not ref_module:
                ref_module = stem
        # dotted module path like services.ingestion.kg.writer or llm_audit.aggregate_stats
        elif re.match(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$", r):
            parts = r.split(".")
            if not ref_module:
                ref_module = parts[-2] if len(parts) >= 2 else parts[0]
            if not ref_method and len(parts) >= 2:
                ref_method = parts[-1]
        # CamelCase → likely a class (exclude Python builtins like None, True, False)
        elif (re.match(r"^[A-Z][a-zA-Z0-9]{2,}$", r)
              and r not in ("None", "True", "False")
              and not ref_class):
            ref_class = r
        # snake_case with underscore → likely a method/function
        elif re.match(r"^[a-z_][a-z0-9_]+$", r) and "_" in r and not ref_method:
            ref_method = r

    return ref_file, ref_module, ref_class, ref_method

# ── Transcript parser ─────────────────────────────────────────────────────────

_INSIGHT_RE = re.compile(
    r"\*\*Insights?:\*\*\s*(.+?)(?=\n\*\*[A-Z]|\n---|\Z)",
    re.DOTALL | re.IGNORECASE,
)

def parse_insights(lines: list[str], file_mtime: float) -> list[Insight]:
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    insights: list[Insight] = []
    turn_index = -1
    turn_prompt = ""

    for e in entries:
        etype = e.get("type")

        if etype == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, str):
                turn_index += 1
                turn_prompt = content.strip().splitlines()[0][:120]
            elif isinstance(content, list):
                has_text = any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in content
                )
                if has_text:
                    turn_index += 1
                    texts = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    turn_prompt = texts.strip().splitlines()[0][:120]

        elif etype == "assistant" and turn_index >= 0:
            msg = e.get("message", {})
            for b in msg.get("content", []) or []:
                if not (isinstance(b, dict) and b.get("type") == "text"):
                    continue
                text = b.get("text", "")
                for m in _INSIGHT_RE.finditer(text):
                    raw = m.group(1).strip()
                    if not raw or _is_empty(raw):
                        continue
                    rf, rm, rc, rmeth = _extract_refs(raw)
                    insights.append(Insight(
                        turn_index=turn_index,
                        turn_prompt=turn_prompt,
                        text=raw,
                        timestamp=file_mtime,
                        ref_file=rf,
                        ref_module=rm,
                        ref_class=rc,
                        ref_method=rmeth,
                    ))

    return insights

# ── Sort helpers ───────────────────────────────────────────────────────────────

SORT_KEYS = ["time", "program", "module", "class", "method"]

def _sort_key(ins: Insight, by: str) -> tuple:
    if by == "time":
        return (ins.turn_index,)
    if by == "program":
        return (ins.ref_file.lower() or "\xff", ins.ref_module.lower() or "\xff",
                ins.ref_class.lower() or "\xff", ins.ref_method.lower() or "\xff",
                ins.turn_index)
    if by == "module":
        return (ins.ref_module.lower() or "\xff", ins.ref_class.lower() or "\xff",
                ins.ref_method.lower() or "\xff", ins.turn_index)
    if by == "class":
        return (ins.ref_class.lower() or "\xff", ins.ref_module.lower() or "\xff",
                ins.ref_method.lower() or "\xff", ins.turn_index)
    if by == "method":
        return (ins.ref_method.lower() or "\xff", ins.ref_class.lower() or "\xff",
                ins.ref_module.lower() or "\xff", ins.turn_index)
    return (ins.turn_index,)

# ── Auto-detect transcript ────────────────────────────────────────────────────

def find_latest_transcript() -> Optional[Path]:
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None

def list_sessions() -> list[Path]:
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

def resolve_transcript(arg: str) -> Optional[Path]:
    p = Path(arg)
    if p.exists() and p.suffix == ".jsonl":
        return p
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

def _slug_to_human_path(slug: str) -> str:
    """Reconstruct a human-readable path from a Claude Code project slug."""
    HIDDEN = "\x00"
    normalized = slug.replace("--", HIDDEN).lstrip("-").replace(HIDDEN, "/.")
    segs = normalized.split("/")
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
    print(f"{'#':>3}  {'Session ID':36}  {'Project':32}  {'Modified':16}  {'Insights':>8}  {'Size':>6}")
    print("─" * 110)
    for i, s in enumerate(sessions[:30], 1):
        project = _slug_to_human_path(s.parent.name)
        if len(project) > 32:
            project = "…" + project[-31:]
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.stat().st_mtime))
        size_kb = s.stat().st_size // 1024
        try:
            lines = s.read_text(errors="replace").splitlines()
            n_insights = len(parse_insights(lines, s.stat().st_mtime))
        except Exception:
            n_insights = 0
        insight_col = f"{n_insights:>8}" if n_insights else "        "
        print(f"{i:>3}  {s.stem[:36]:36}  {project:32}  {mtime}  {insight_col}  {size_kb:>5}k")
    print(f"\n{min(30, len(sessions))} of {len(sessions)} sessions.")
    print("Run:  python3 insightviewer.py <session-id>  to open one.")

# ── Colours ───────────────────────────────────────────────────────────────────

C_NORMAL   = 0
C_HEADER   = 1
C_SELECTED = 2
C_DIM      = 3
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
    curses.init_pair(C_DIM,      curses.COLOR_WHITE,  -1)
    curses.init_pair(C_GREEN,    curses.COLOR_GREEN,  -1)
    curses.init_pair(C_YELLOW,   curses.COLOR_YELLOW, -1)
    curses.init_pair(C_RED,      curses.COLOR_RED,    -1)
    curses.init_pair(C_CYAN,     curses.COLOR_CYAN,   -1)
    curses.init_pair(C_MAGENTA,  curses.COLOR_MAGENTA,-1)
    curses.init_pair(C_BOLD,     curses.COLOR_WHITE,  -1)

def cp(n: int) -> int:
    return curses.color_pair(n)

# ── Rendering helpers ─────────────────────────────────────────────────────────

def _char_width(c: str) -> int:
    eaw = _unicodedata.east_asian_width(c)
    return 2 if eaw in ('W', 'F') else 1

def _clip_to_width(text: str, max_w: int) -> str:
    cols = 0
    for i, c in enumerate(text):
        cw = _char_width(c)
        if cols + cw > max_w:
            return text[:i]
        cols += cw
    return text

def _addstr_clipped(win, y: int, x: int, text: str, attr: int, max_w: int):
    h, w = win.getmaxyx()
    if y >= h - 1 or x >= w:
        return
    avail = min(max_w, w - x - 1)
    if avail <= 0:
        return
    try:
        win.addstr(y, x, _clip_to_width(text, avail), attr)
    except curses.error:
        pass

def _wrap(text: str, width: int) -> list[str]:
    """Word-wrap text to width, preserving explicit newlines."""
    out = []
    for para in text.splitlines():
        para = para.strip()
        if not para:
            out.append("")
            continue
        words = para.split()
        line = ""
        for word in words:
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        if line:
            out.append(line)
    return out or [""]

# ── Main TUI ──────────────────────────────────────────────────────────────────

def run_ui(stdscr, transcript_path: Path):
    curses.curs_set(0)
    stdscr.nodelay(True)
    init_colors()

    insights: list[Insight] = []
    displayed: list[Insight] = []   # sorted/ordered view

    selected = 0       # index into displayed
    list_scroll = 0
    detail_scroll = 0
    live_mode = True
    focus = "left"     # "left" = list, "right" = detail
    sort_by = "time"   # current sort key (pause mode only)
    show_help = False

    file_lines: list[str] = []
    last_mtime = 0.0
    last_size  = 0

    def reload() -> bool:
        nonlocal file_lines, last_mtime, last_size, insights, displayed
        try:
            st = transcript_path.stat()
            if st.st_mtime == last_mtime and st.st_size == last_size:
                return False
            last_mtime = st.st_mtime
            last_size  = st.st_size
            file_lines = transcript_path.read_text(errors="replace").splitlines()
            insights = parse_insights(file_lines, st.st_mtime)
            displayed = _build_display(insights, live_mode, sort_by)
            return True
        except Exception:
            return False

    def _build_display(ins: list[Insight], live: bool, sort: str) -> list[Insight]:
        if live:
            return list(ins)
        return sorted(ins, key=lambda i: _sort_key(i, sort))

    reload()

    while True:
        reload()

        if live_mode:
            displayed = _build_display(insights, True, sort_by)
            if displayed:
                selected = len(displayed) - 1

        h, w = stdscr.getmaxyx()
        left_w  = max(30, w // 3)
        right_w = w - left_w - 1

        stdscr.erase()

        # ── Header ────────────────────────────────────────────────────────────
        mode_tag = "LIVE" if live_mode else f"PAUSED  sort:{sort_by}"
        header = (f" insightviewer  |  {transcript_path.name[:35]}  |"
                  f"  {len(displayed)} insights  |  {mode_tag}  |  {time.strftime('%H:%M:%S')} ")
        _addstr_clipped(stdscr, 0, 0, header.ljust(w), cp(C_HEADER) | curses.A_BOLD, w)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = " Tab focus  Up/Dn navigate  j/k scroll  l live  s sort  h help  q quit "
        _addstr_clipped(stdscr, h - 1, 0, footer.ljust(w), cp(C_HEADER), w)

        list_h = h - 2

        # ── Left pane: insight list ───────────────────────────────────────────
        # Clear left pane
        for r in range(list_h):
            row = r + 1
            if row >= h - 1:
                break
            _addstr_clipped(stdscr, row, 0, " " * left_w, C_NORMAL, left_w)

        # Each insight occupies 3 content rows + 1 blank separator = 4 rows total
        ROWS_PER = 4
        list_focused = (focus == "left")

        if displayed:
            selected = max(0, min(selected, len(displayed) - 1))

        # Scroll to keep selected visible
        if displayed:
            top_row = selected * ROWS_PER
            bot_row = top_row + ROWS_PER - 2  # last content row (exclude blank separator)
            if top_row < list_scroll:
                list_scroll = top_row
            if bot_row >= list_scroll + list_h:
                list_scroll = bot_row - list_h + 1
            list_scroll = max(0, list_scroll)

        slot_w = left_w - 5   # text width after " N. " label

        abs_row = 0
        for idx, ins in enumerate(displayed):
            is_sel = (idx == selected)
            if is_sel and list_focused:
                bg = cp(C_SELECTED)
            elif is_sel:
                bg = cp(C_DIM) | curses.A_REVERSE
            else:
                bg = C_NORMAL

            # Three rows per insight
            rows = [
                # row 0: number + first ~slot_w chars of insight
                (f" {idx+1:2d}. " + ins.text.replace("\n", " ")[:slot_w],
                 bg if is_sel else (cp(C_BOLD) | curses.A_BOLD)),
                # row 1: ref tags
                ("     "
                 + " ".join(filter(None, [
                     f"[{ins.ref_file.rsplit('/',1)[-1]}]" if ins.ref_file else "",
                     f"[{ins.ref_class}]" if ins.ref_class else "",
                     f"[{ins.ref_method}()]" if ins.ref_method else "",
                 ]))[:slot_w],
                 bg if is_sel else cp(C_CYAN)),
                # row 2: turn reference
                (f"     T{ins.turn_index+1}  {ins.turn_prompt[:slot_w - 8]}",
                 bg if is_sel else cp(C_DIM)),
            ]

            for ri, (text, attr) in enumerate(rows):
                screen_row = abs_row - list_scroll + 1
                if 1 <= screen_row <= h - 2:
                    if is_sel:
                        _addstr_clipped(stdscr, screen_row, 0, " " * left_w, bg, left_w)
                    _addstr_clipped(stdscr, screen_row, 0,
                                    _clip_to_width(text, left_w), attr, left_w)
                abs_row += 1

            # blank separator row between insights
            screen_row = abs_row - list_scroll + 1
            if 1 <= screen_row <= h - 2:
                _addstr_clipped(stdscr, screen_row, 0, " " * left_w, C_NORMAL, left_w)
            abs_row += 1

        # ── Divider ───────────────────────────────────────────────────────────
        for row in range(1, h - 1):
            _addstr_clipped(stdscr, row, left_w, "│", cp(C_DIM), 1)
        mid = (h - 2) // 2
        arrow = "▶" if focus == "right" else "◀"
        _addstr_clipped(stdscr, mid, left_w, arrow, cp(C_CYAN) | curses.A_BOLD, 1)

        # ── Right pane: full insight detail ───────────────────────────────────
        rx = left_w + 1
        detail_lines: list[tuple[str, int]] = []

        if displayed and 0 <= selected < len(displayed):
            ins = displayed[selected]

            # Header
            detail_lines.append((f" Insight {selected+1} of {len(displayed)}", cp(C_BOLD) | curses.A_BOLD))
            detail_lines.append(("", C_NORMAL))

            # Turn context
            detail_lines.append((f" Turn {ins.turn_index+1}  {ins.turn_prompt}", cp(C_DIM)))
            detail_lines.append((" " + "─" * (right_w - 3), cp(C_DIM)))

            # Code references
            refs = list(filter(None, [
                f"file: {ins.ref_file}"     if ins.ref_file   else "",
                f"module: {ins.ref_module}" if ins.ref_module else "",
                f"class: {ins.ref_class}"   if ins.ref_class  else "",
                f"method: {ins.ref_method}" if ins.ref_method else "",
            ]))
            if refs:
                for ref in refs:
                    detail_lines.append((f" {ref}", cp(C_CYAN)))
                detail_lines.append((" " + "─" * (right_w - 3), cp(C_DIM)))

            detail_lines.append(("", C_NORMAL))

            # Full insight text, word-wrapped
            for wline in _wrap(ins.text, right_w - 4):
                detail_lines.append((f"  {wline}", C_NORMAL))

            detail_lines.append(("", C_NORMAL))

        elif not displayed:
            detail_lines.append((" No insights yet.", cp(C_DIM)))
            detail_lines.append(("", C_NORMAL))
            detail_lines.append((" Insights appear when Claude writes:", cp(C_DIM)))
            detail_lines.append(("   **Insight:** <text>", cp(C_YELLOW)))
            detail_lines.append((" at the end of a response.", cp(C_DIM)))

        # Render right pane
        max_scroll = max(0, len(detail_lines) - (h - 3))
        detail_scroll = min(detail_scroll, max_scroll)
        for i, (text, attr) in enumerate(detail_lines[detail_scroll:]):
            row = i + 1
            if row >= h - 1:
                break
            _addstr_clipped(stdscr, row, rx, text, attr, right_w - 1)

        # ── Help popup ────────────────────────────────────────────────────────
        if show_help:
            _draw_help(stdscr, h, w)

        stdscr.refresh()

        # ── Input ─────────────────────────────────────────────────────────────
        try:
            key = stdscr.getch()
        except Exception:
            key = -1

        if show_help:
            if key != -1:
                show_help = False
            time.sleep(0.2)
            continue

        if key in (ord('q'), ord('Q'), 27):
            break
        elif key in (ord('h'), ord('H')):
            show_help = True
        elif key == ord('\t'):
            focus = "right" if focus == "left" else "left"
            detail_scroll = 0
        elif key == curses.KEY_UP:
            if focus == "left":
                selected = max(0, selected - 1)
                detail_scroll = 0
            else:
                detail_scroll = max(0, detail_scroll - 1)
        elif key == curses.KEY_DOWN:
            if focus == "left":
                selected = min(len(displayed) - 1, selected + 1) if displayed else 0
                detail_scroll = 0
            else:
                detail_scroll = min(max_scroll, detail_scroll + 1)
        elif key in (curses.KEY_PPAGE, ord('k')):
            detail_scroll = max(0, detail_scroll - 10)
        elif key in (curses.KEY_NPAGE, ord('j')):
            detail_scroll += 10
        elif key in (ord('l'), ord('L')):
            live_mode = not live_mode
            if not live_mode:
                displayed = _build_display(insights, False, sort_by)
            else:
                displayed = _build_display(insights, True, sort_by)
                selected = max(0, len(displayed) - 1)
            detail_scroll = 0
        elif key in (ord('s'), ord('S')):
            if not live_mode:
                idx = SORT_KEYS.index(sort_by)
                sort_by = SORT_KEYS[(idx + 1) % len(SORT_KEYS)]
                displayed = _build_display(insights, False, sort_by)
                selected = min(selected, max(0, len(displayed) - 1))

        time.sleep(0.2)


def _draw_help(stdscr, h: int, w: int):
    lines = [
        "  insightviewer — Help  ",
        "",
        "  Navigation",
        "  Tab        Switch focus: list <-> detail",
        "  Up / Down  Navigate insights (list) or scroll (detail)",
        "  j / PgDn   Scroll detail down",
        "  k / PgUp   Scroll detail up",
        "",
        "  Modes",
        "  l          Toggle LIVE / PAUSE mode",
        "  s          Cycle sort key (pause mode only)",
        "             time > program > module > class > method",
        "",
        "  Other",
        "  h          This help",
        "  q / Esc    Quit",
        "",
        "  Press any key to close",
    ]
    box_w = max(len(l) for l in lines) + 4
    box_h = len(lines) + 2
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    try:
        for row in range(box_h):
            _addstr_clipped(stdscr, y0 + row, x0, " " * box_w, cp(C_HEADER), box_w)
        for row, text in enumerate(lines):
            _addstr_clipped(stdscr, y0 + 1 + row, x0 + 2,
                            text.ljust(box_w - 4),
                            cp(C_HEADER) | (curses.A_BOLD if row == 0 else 0),
                            box_w - 4)
    except curses.error:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args and args[0] in ("--help", "-h", "help"):
        print(__doc__)
        sys.exit(0)

    if args and args[0] in ("--list", "-l", "list"):
        print_session_list()
        sys.exit(0)

    if args:
        path = resolve_transcript(args[0])
        if path is None:
            print(f"No transcript found matching '{args[0]}'.", file=sys.stderr)
            print("Run  python3 insightviewer.py --list  to see available sessions.",
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
