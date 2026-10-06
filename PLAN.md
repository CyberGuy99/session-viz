# session-viz — implementation plan

Owner: Rushil (rushilcd@umd.edu). Target: a Claude Code skill that turns a session transcript into a static website.
Hosting requirement: **private** URL, no server to run. GitHub plan: Pro.

## 0. Before writing code

Create in the repo root, per house convention:
- `architecture.md` — system overview, what talks to what, the 2–3 key data flows, prose.
- `glossary.md` — internal names (facts.json, narrative.json, SymbolChange, event, lesson, discrepancy…).
- `decisions.md` — header + today's date; append every non-obvious decision as it is made.
- `CLAUDE.md` — stack, conventions, commands, architecture notes, Do-NOT list (template at bottom of this file).

## 1. Architecture (deterministic → LLM → deterministic)

```
transcript.jsonl + git repo
        │
   [1] extract.py  ──►  facts.json        pure code, no LLM, reproducible
        │
   [2] Claude (SKILL.md) ──►  narrative.json   descriptions / why / goal / lessons, schema-validated
        │
   [3] build.py  ──►  dist/index.html     single self-contained file, JSON inlined
        │
   [4] publish.sh ──►  private URL        Cloudflare Pages + Access (see §5)
```

Everything computable (files, symbols, before/after source, timestamps, errors) is computed. Claude writes only judgment fields, against a JSON schema; the builder refuses to run on schema failure.

## 2. `scripts/extract.py` → `facts.json`

Input: path to `.jsonl` (default: newest session under `~/.claude/projects/<encoded-cwd>/`). Stdlib only (`ast`, `json`, `difflib`, `subprocess`).

Transcript parsing
- Each line is a message object. Walk `message.content[]`; collect `tool_use` blocks with `name ∈ {Write, Edit, MultiEdit, NotebookEdit, Bash, TaskCreate, TaskUpdate}` and match `tool_result` by `tool_use_id`; keep `timestamp`, `uuid`.
- Per file, replay edits in order to reconstruct `before`/`after`. `Write` → full content; `Edit` → apply `old_string→new_string` to the running copy. Seed `before` from `git show <start_sha>:<path>`, else first `Read` tool_result of that file, else new file.
- `start_sha`: first `git rev-parse HEAD` seen in a Bash tool_result, else first commit with author date ≥ session start, else `--since <sha>` flag (required after `/compact`).
- Subagent transcripts are sibling `.jsonl` files; include any whose `parentUuid` chain links to the main session.
- Git is authoritative for *which* files changed (`git diff --name-status <start_sha>`); transcript gives *sequence*. Files in one source but not the other → `facts.discrepancies[]`.

Symbol diff (`.py`)
```python
def symbols(src) -> dict[str, Symbol]:
    # qualname -> {kind, lineno, end_lineno, source, signature, docstring}; nested → "Class.method"
def diff_symbols(before, after) -> list[SymbolChange]:
    # added | removed | modified(signature|body|docstring) | renamed | unchanged
    # rename = removed+added with SequenceMatcher body ratio > 0.8
    # each change carries before_src, after_src, unified_diff
```

Non-Python
- `.json/.yaml/.toml` → key-path diff (added/removed/changed leaves).
- `.csv` → row/col count delta, header changes, first 3 changed rows.
- `.md/.txt` → heading-section diff + unified line diff.
- `.js/.ts/.go/.rs` → symbol diff if `tree-sitter` + grammar importable, else line diff with `symbol_support: false`. Optional dependency; never block on it.

Also emitted
- `facts.session`: first user message (goal candidate), all user messages + timestamps, duration, turn count.
- `facts.tasks[]`: TaskCreate/TaskUpdate history (id, subject, status transitions) → progress.
- `facts.events[]`: ordered `{ts, turn, kind: edit|bash|error|test_run|user_msg, file?, summary, exit_code?}`. `test_run` = Bash matching `pytest|npm test|cargo test|go test`; `error` = `is_error` or non-zero exit.

## 3. Narrative (Claude, inside the skill) → `narrative.json`

SKILL.md tells Claude to read `facts.json` and emit this, validated by `schema/narrative.schema.json`:

```jsonc
{
  "goal": {"statement": str, "completion_indicator": str, "source": "user_msg#<uuid>"},
  "progress": {"percent": int, "basis": str, "done": [str], "remaining": [str]},
  "files": {
    "<path>": {
      "summary": str,
      "symbols": {
        "<qualname>": {
          "was": str, "now": str,                       // ≤2 sentences; "" if added/removed
          "example": {"before": str, "after": str},     // concrete call → result, e.g. "parse('1,2') → ['1','2']" / "→ [1, 2]"
          "category": "accuracy" | "speed" | "refactoring",
          "why": str                                    // must cite an event uuid or user_msg uuid
        }
      },
      "data_changes": [{"what": str, "why": str, "category": "..."}]   // non-code files
    }
  },
  "lessons": [{"ts": str, "event_ref": str, "learned": str}]   // "pytest failed on empty input → added guard in parse()"
}
```
Rules: every `why` cites evidence; `example` looks runnable, not prose; one `category` per symbol; no symbols that aren't in `facts.json`. Validation failure → Claude fixes and re-runs.

## 4. `scripts/build.py` + `templates/index.html` → `dist/index.html`

Single file, zero external requests (works from `file://`). Vanilla JS, ~400 lines.

1. Header: goal, completion indicator, progress bar (`tasks done/total`, fallback `progress.percent`).
2. Timeline rail: `facts.events` ⨝ `narrative.lessons` by timestamp; lessons highlighted; click edit-event → scroll to file.
3. Files accordion. Per symbol: status badge, category chip (accuracy=green, speed=blue, refactoring=grey), was/now, example before→after, collapsible side-by-side diff (minimal inline diff renderer). Non-code: data-change table.
4. Discrepancies panel if non-empty.

Data embedded as `<script id="facts" type="application/json">` + same for narrative. Base64 only for files > 200 KB.

## 5. Hosting — private, no server

**Do not use GitHub Pages for this.** Pages built from a private repo on GitHub Pro are still served at a public URL (only obscurity protects it); access-controlled Pages require GitHub Enterprise Cloud.

Default: **Cloudflare Pages + Cloudflare Access** (free; Access free tier covers up to 50 users).
- One-time: `npm i -g wrangler && wrangler login && wrangler pages project create session-viz`; in Zero Trust dashboard add an Access application for `session-viz.pages.dev` with an email-OTP policy allowing `rushilcd@umd.edu` (or `@umd.edu`). Also enable Access for the `*.session-viz.pages.dev` preview wildcard.
- Each run: `scripts/publish.sh` → `wrangler pages deploy dist --project-name session-viz --branch <session-id>` → prints URL. Each session gets its own preview URL; `main` branch is "latest".
- Repo stays private on GitHub; Pages is deployed from `dist/`, not from the repo, so nothing in the repo is exposed.

Alternatives (document in SKILL.md, don't implement first):
- Running inside Cowork: publish `dist/index.html` with the Artifact tool (private claude.ai URL until shared).
- No URL needed: `dist/index.html` opens directly from disk.

## 6. Skill layout

```
~/.claude/skills/session-viz/
  SKILL.md                     # triggers: "visualize session", "session report", "/session-viz"
  scripts/extract.py
  scripts/build.py             # jsonschema optional; warn if absent
  scripts/publish.sh           # wrangler; fails loudly if not logged in
  schema/narrative.schema.json
  templates/index.html
  tests/test_symbols.py        # fixtures: add/remove/modify/rename/nested-class
```
SKILL.md flow: resolve session (`--session <path|latest>`, `--since <sha>`) → `extract.py` → Claude writes `narrative.json` → `build.py` → ask to publish → `publish.sh`.

## 7. Build order + verification

1. Context docs (§0). Then `extract.py` on a real session JSONL; assert reconstructed `after` == `git show HEAD:<path>` for ≥95% of files, rest logged as discrepancies. `tests/test_symbols.py` green.
2. Schema + SKILL.md; run Claude on step-1 `facts.json`; confirm validation errors are actionable.
3. `build.py` + template; headless Chromium screenshot with empty `narrative.json` (graceful) and full one.
4. `publish.sh` to Cloudflare; confirm URL demands login and renders after.
5. Acceptance: run the skill on the session that built the skill. The visualizer visualizing itself is done-done.

## CLAUDE.md template

```
# Project: session-viz

## Stack
- Python 3.11+ (stdlib; optional: jsonschema, tree-sitter), vanilla JS/HTML
- No package manager required; wrangler (npm) for deploy only
- Deploy target: Cloudflare Pages behind Cloudflare Access

## Conventions
- scripts/ are CLIs with argparse; no side effects outside --out paths
- facts.json is deterministic: same input → byte-identical output (sort keys)
- Tests: pytest, fixtures under tests/fixtures/; every symbol-diff case gets a fixture

## Commands
- Test: `pytest -q`
- Extract: `python scripts/extract.py --session latest -o facts.json`
- Build: `python scripts/build.py facts.json narrative.json -o dist/index.html`
- Publish: `scripts/publish.sh dist <session-id>`

## Architecture notes
- extract.py owns transcript parsing + symbol diffs; build.py owns rendering; Claude owns narrative.json only
- Edit replay must seed from git before falling back to transcript Reads — see decisions.md
- After /compact, early edits exist only in git; require --since

## Do NOT
- Deploy to GitHub Pages (public URL)
- Let build.py accept narrative.json that fails schema
- Fetch anything external from index.html
- Append decisions.md entries without a date
```
