---
name: session-viz
description: Turn a Claude Code session transcript into a single-file HTML report of what changed, why, and what was learned — per-symbol was/now/example, timeline with lessons, progress toward the goal — and optionally publish it to a private URL. Use when the user says "visualize session", "session report", "what did we do this session", "/session-viz", or asks for a shareable write-up of a coding session's changes.
---

# session-viz

Pipeline: `extract.py` (deterministic facts) → **you** write `narrative.json` (judgment only) → `build.py` (validates, renders) → optional `publish.sh`.

`SV=~/.claude/skills/session-viz` below (a symlink to the source repo). Run everything from the repo the session worked in.

## 1. Resolve the session and output dir

Arguments the user may give: `--session <path|latest>` (default `latest` = newest transcript for this cwd), `--since <sha>`.

```bash
SID=$(basename "$(python3 $SV/scripts/extract.py --session latest --print-path)" .jsonl)   # or the given path
OUT=${SESSION_VIZ_OUT:-$HOME/.cache/session-viz}/$SID
```

If the transcript was `/compact`-ed (facts will contain a `compacted_without_since` discrepancy), ask the user for the commit the session started from and re-run with `--since <sha>`.

## 2. Extract

```bash
python3 $SV/scripts/extract.py --session <path|latest> [--since <sha>] -o $OUT/facts.json
```

Read `$OUT/facts.json`. Key parts: `session` (goal_candidate, user_messages), `files[path].symbols[]` (SymbolChange: qualname, status, aspects, before_src/after_src), `files[path].data_changes|csv|sections` for non-code, `events[]` (uuid, ts, kind, summary, exit_code, output_tail), `tasks[]`, `discrepancies[]`.

Test runs: `exit_code` is often 0 even for failures because of `| tail` pipes — use `tests_failed` and `output_tail`. Files excluded from analysis (gitignored, outside the repo) are listed in `session.ignored_files` / `session.outside_repo_files`; don't describe them.

facts.json can be hundreds of KB; read it in slices, not whole (`jq` may not be installed):

```bash
python3 -c "import json;f=json.load(open('$OUT/facts.json'));print(json.dumps({p:{'status':v.get('status'),'changed':[s['qualname']+':'+s['status'] for s in v.get('symbols',[]) if s['status']!='unchanged']} for p,v in f['files'].items()},indent=1))"
python3 -c "import json;f=json.load(open('$OUT/facts.json'));[print(e['uuid'],e['kind'],e.get('tests_failed',''),e['summary']) for e in f['events']]"
```
Then pull individual symbols' `before_src`/`after_src` only for those you describe.

## 3. Write `$OUT/narrative.json`

Schema: `$SV/schema/narrative.schema.json`. Shape:

```jsonc
{
  "goal": {"statement": "...", "completion_indicator": "what observable thing means done", "source": "user_msg#<uuid>"},
  "progress": {"percent": 80, "basis": "4/5 tasks completed; publish step pending", "done": ["..."], "remaining": ["..."]},
  "files": {
    "<path exactly as in facts.files>": {
      "summary": "one or two sentences on the file's role in this session",
      "symbols": {
        "<qualname exactly as in facts>": {
          "was": "≤2 sentences, \"\" if added",
          "now": "≤2 sentences, \"\" if removed",
          "example": {"before": "parse('1,2') → ['1', '2']", "after": "parse('1,2') → [1, 2]"},
          "category": "accuracy | speed | refactoring",
          "why": "reason, citing evidence (ev:<event uuid>)"
        }
      },
      "data_changes": [{"what": "...", "why": "... (ev:<uuid>)", "category": "..."}]
    }
  },
  "lessons": [{"ts": "<event ts, copied exactly>", "event_ref": "<event uuid>", "learned": "pytest failed on empty input → added guard in parse()"}]
}
```

Rules (build.py enforces the starred ones):
- ★ Only paths in `facts.files` and only symbols whose status ≠ `unchanged`. Use `symbols` only where `symbol_support` is true. Everything else (facts `data_changes`, `csv`, `sections` for markdown/text, plain diffs) is described under narrative `data_changes`.
- ★ Every `why` contains at least one `(ev:<uuid>)` citing a real `facts.events` uuid — the user message that asked for it, the failing test that prompted it, the edit itself. Never invent a reason the events don't support; if the only evidence is the edit, cite the edit and say what it does. A file with no events of its own (a `git_only` discrepancy) cites the user message or command most plausibly responsible, and says it was changed outside the tools.
- ★ `example.before`/`after` look like runnable calls with results: `f(x) → y` (`->` and `=>` also accepted). `before` is `""` only for added symbols. For refactors, before and after results are equal — that's the point. For classes, show construction + a method call; for constants, `NAME → value`.
- ★ `goal.source` is `user_msg#<uuid>` of a user_msg event (usually `session.goal_candidate`); restate the goal crisply, don't paste it.
- ★ `lessons[].ts` equals the referenced event's `ts`. Lessons come from `error`/`test_run` events and user corrections: what went wrong → what changed because of it. 0 lessons is fine if nothing went wrong.
- One `category` per symbol: `accuracy` = behaviour/correctness changed (incl. new features, and config that changes behaviour), `speed` = performance, `refactoring` = same behaviour, different structure (incl. docs-only and test-only changes).
- `progress.percent`: prefer task completion (`tasks` done/total) when tasks exist; if `tasks` is empty, judge against the completion indicator and say so in `basis`.
- Scope: every changed file gets a `summary`. Per-symbol entries go to public / top-level symbols of the files that carry the session's main changes; describing a class covers its methods unless a method changed for its own reason. Tests, fixtures and small helpers may be summary-only. `<module>` may be skipped. build.py warns only about gaps in files where you described some symbols.

## 4. Validate and build

```bash
python3 $SV/scripts/build.py $OUT/facts.json $OUT/narrative.json --check
```

On errors: each line names the JSON path, what's wrong, and valid alternatives. Fix `narrative.json` and re-run until it passes. Do not edit facts.json to make errors go away. Then:

```bash
python3 $SV/scripts/build.py $OUT/facts.json $OUT/narrative.json -o $OUT/dist/index.html
```

Tell the user the path; it opens directly from disk (`file://`), no network needed.

## 5. Publish (ask first)

Ask whether to publish. Default target is Cloudflare Workers static assets behind Cloudflare Access (private, email-OTP login); each session gets a preview URL `https://<session-id>-session-viz.<sub>.workers.dev`:

```bash
$SV/scripts/publish.sh $OUT/dist $SID
```

It fails loudly if wrangler is missing or not logged in; relay the fix it prints. One-time setup is in `$SV/README.md` § Hosting. Never publish to GitHub Pages — those URLs are public even from a private repo.

Alternatives:
- In Cowork / claude.ai with an Artifact tool: publish `$OUT/dist/index.html` as an artifact (private claude.ai URL until shared).
- No URL needed: hand over `$OUT/dist/index.html`.
