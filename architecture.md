# Architecture

session-viz is a Claude Code skill that turns one Claude Code session transcript into a single, self-contained HTML report explaining what changed in the codebase, why, and what was learned along the way.

## Shape: deterministic → LLM → deterministic

The pipeline is deliberately sandwiched. Everything that *can* be computed is computed by plain Python with no model involvement; Claude is only asked for judgment (descriptions, rationale, lessons), and its output is schema-validated before anything renders it.

```
transcript.jsonl (+ subagent .jsonl) + git repo
        │
   scripts/extract.py  ──►  facts.json        pure code, reproducible, byte-identical on rerun
        │
   Claude via SKILL.md ──►  narrative.json    judgment fields only, validated by schema/narrative.schema.json
        │
   scripts/build.py    ──►  dist/index.html   one file, JSON inlined, no external requests
        │
   scripts/publish.sh  ──►  private URL       Cloudflare Workers static assets behind Cloudflare Access
```

## Components

**extract.py** reads the session JSONL that Claude Code writes under `~/.claude/projects/<encoded-cwd>/`. It pairs every `tool_use` block with its `tool_result` by `tool_use_id`, then does three things: replays file mutations (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`) per file to reconstruct the before/after source; runs a symbol-level diff on that pair (AST for Python, key-paths for structured data, sections for prose, line diff otherwise); and builds an ordered event log (edits, bash runs, test runs, errors, user messages) plus the task history. It cross-checks the set of touched files against `git` and records disagreements as discrepancies instead of guessing.

**Claude (SKILL.md)** reads `facts.json` and writes `narrative.json`: the session goal, progress, a was/now/example/why/category entry per changed symbol, and lessons tied to specific events. It may only reference symbols and event IDs that exist in `facts.json`; `build.py` enforces both the JSON schema and these referential rules.

**build.py** validates the narrative, inlines both JSON documents into `templates/index.html`, and writes `dist/index.html`. The template is vanilla JS that renders header/progress, a timeline rail, a per-file symbol accordion with inline diffs, and a discrepancies panel. It tolerates an empty narrative (renders facts only).

**publish.sh** uploads `dist/` as static assets to a Cloudflare Worker whose hostnames (`session-viz.<sub>.workers.dev` and the preview wildcard `*-session-viz.<sub>.workers.dev`) are behind a Cloudflare Access email-OTP policy. Each session is a Worker version with its own preview alias, so it gets its own URL; `--production` promotes one to the main URL. It verifies Access by HTTP before uploading and again after.

**project.py** (project mode) runs extract.py + build.py over every main transcript in `~/.claude/projects/<encoded-cwd>/` and writes a static site: `dist/index.html` (session list from `templates/project.html`) plus `dist/s/<session-id>/index.html` per session, each with a back-link. Facts, summaries and a cache key live in the per-session dir the skill already uses (`~/.cache/session-viz/<session-id>/`), so a narrative the skill writes there shows up on the next run. Claude is not invoked; sessions without a valid narrative render facts-only and are listed with the command that generates one.

## Key data flows

1. **Edit replay.** For each file touched in the transcript: seed `before` from `git show <start_sha>:<path>` → else from the first `Read` tool_result of that file → else empty (new file). Apply each mutation in transcript order to a running copy. The final copy is `after`. If an `Edit`'s `old_string` is not found in the running copy, the replay for that file is marked broken, and the working-tree content is used for `after` with a discrepancy logged.

2. **Source of truth reconciliation.** Git decides *which* files changed (`git diff --name-status <start_sha> [<end_ref>]`, plus untracked files when the end state is the working tree; `end_ref` is the last commit authored before the session ended if later commits exist, so older sessions aren't diffed against later work); the transcript decides *the order and the reasons*. A file changed in git but never touched by a tool (e.g. created through Bash) and a file touched by a tool but unchanged in git both become `facts.discrepancies[]` entries.

3. **Narrative grounding.** `narrative.json` keys mirror `facts.json` keys (`files.<path>.symbols.<qualname>`), and every `why`/`event_ref` must cite an event `uuid` present in `facts.events`. The builder rejects narratives that cite non-existent symbols or events, so the rendered page cannot claim something the transcript does not contain.
