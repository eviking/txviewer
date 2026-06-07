---
name: feedback_insights_section
description: Every response must end with an Insights section — written as a sr engineer explaining a gotcha to a jr engineer
metadata:
  node_type: memory
  type: feedback
  originSessionId: 7d900a05-eb79-47d4-9058-5d2064f1a5e7
---

Every response must end with an **Insights:** section.

**Why:** Captures non-obvious things learned this turn in a way that's useful to a human reading it — not just a note to self, but a clear explanation a junior engineer could act on without needing to dig further.

**How to apply:** Write each insight the way a senior engineer would explain a gotcha to a junior engineer on the team — as if saying "hey, before you touch that, you should know...". Every insight must include three things:

1. **Scope label** — `[system]`, `[module]`, `[class]`, or `[method]` at the start, followed by the FQN or file path it applies to. E.g. `[method] services/api/main.py · store_insights`
2. **The fact** — state plainly what is true, what happens, or what breaks
3. **A code snippet** — a short inline snippet (≤8 lines) that shows the exact line(s) where the behaviour lives. If no single snippet captures it, reference the file + line number instead.
4. **So what** — one sentence on what a future engineer should do differently because of this

Bad (no scope, no snippet, no consequence):
> The KG writer silently discards nodes when a field is None.

Good:
> **[method] `services/ingestion/kg/writer.py` · `KGWriter.write_node`** — silently drops any node where a required field is `None` with no log or exception:
> ```python
> if not node.get("fqn"):
>     return   # silent discard
> ```
> If you add a new parser, validate all required fields before calling the writer or you'll get graph gaps with no error signal.

**Categories of insights worth capturing:**
- A hidden coupling ("changing X also silently changes Y because...")
- A silent failure mode ("if you do X without Y first, it fails without telling you")
- A constraint that isn't in the code ("this must always be called before Z")
- A design decision and its consequence ("it's built this way because of X, which means you can't do Y")
- Something that was tried and failed ("approach X doesn't work because...")

**Format rules:**
- Generate exactly **5 insights** per turn (or as many as exist if fewer than 5 things were learned — never pad with obvious facts to reach 5).
- Each insight includes an **importance score (1–100)** on the same line as the scope label, e.g. `[method · importance: 82]`.
- Never describe what *you did* this turn — only what you *learned* about the system.
- If the insight is durable (survives beyond this session), also call `capture_insight` with the same importance score and include the snippet in the insight text. Only insights with importance > 75 are published to the agent index and shown to future agents.
- If nothing non-obvious was learned: **Insights:** None this turn.

**Importance scoring guide:**
- 90–100: Will almost certainly prevent a serious bug or wasted day if missed
- 76–89: Meaningfully changes how you'd approach work in this area
- 51–75: Good to know but not critical — captured but not published to agents
- 1–50: Minor or obvious after reading the code — don't capture these at all

---

After the Insights section, always append a **Technical Debt:** subsection.

**What to include:** any shortcuts, workarounds, missing error handling, hardcoded values, or structural weaknesses that were *noticed* this turn — whether introduced now or pre-existing. Only include items actually observed in the code touched this turn, never speculate.

**Format:** One bullet per item. Each bullet must have:
- The file + approximate line or function
- What the debt is (one sentence)
- The risk if left unaddressed (one sentence)

**Example:**
> **Technical Debt:**
> - `services/console/routes/system_health.py · _parse_pipeline_progress` — done-fragment detection uses `str.index()` which throws `ValueError` if the fragment appears before the start fragment in an out-of-order log; no guard exists. Risk: a malformed or interleaved log line could 500 the scan-progress endpoint.
> - `services/console/scan_launcher.py · launch_scan` — container name is derived from `repo_id.lower().replace(' ', '-')` with no length cap or character sanitisation beyond that. Risk: a repo_id with special characters (slashes, dots) will cause the Docker API to reject the container name silently.

If no debt was observed this turn: **Technical Debt:** None observed.
