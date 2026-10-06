# Decisions

Started 2026-10-06. Append-only; every entry dated. Deviations from PLAN.md are marked **[deviation]**.

## 2026-10-06 — Task tracking lives in chat + this log
**[deviation]** PLAN asks for a task list; no task-tracking tool (TaskCreate/TodoWrite) is available in the build session, so progress is tracked in the conversation and section completions are noted here.

## 2026-10-06 — Target Python 3.10, not 3.11+
**[deviation]** Host has Python 3.10.12. Code avoids 3.11-only features (`tomllib`, `ExceptionGroup`, `Self`). TOML diff uses `tomllib` if importable, else `tomli`, else falls back to line diff.

## 2026-10-06 — Develop in ~/session-viz, symlink into ~/.claude/skills
PLAN §6 places the skill at `~/.claude/skills/session-viz/`. The repo is the source of truth; the skill dir is a symlink to the repo so edits and tests stay in one place.

## 2026-10-06 — Verification against the working tree when HEAD is absent
**[deviation]** §7.1 checks reconstructed `after` against `git show HEAD:<path>`. This repo has no commits yet and committing is the user's call, so `extract.py --verify` compares against the working-tree file. For clean files that's identical to HEAD; for dirty files it's the correct final state. Same intent: "does replay reproduce the real final file".

## 2026-10-06 — Extra seed source + resync from `originalFile`
**[deviation]** Write/Edit `toolUseResult` records include `originalFile`, the exact on-disk content immediately before the mutation. Seed order is now git → `originalFile` of first mutation → first full `Read` → new. During replay, if a mutation's `originalFile` differs from the running copy, replay resyncs to it and logs `seed_mismatch` (first mutation: uncommitted pre-session changes) or `external_modification` (Bash `sed -i`, user edits). This makes replay robust to out-of-band edits instead of failing on them.

## 2026-10-06 — Event citation key is the tool_result record uuid
One assistant record can carry several `tool_use` blocks; each `tool_result` lives in its own user record, so its uuid is unique per tool call. User-message events use the prompt record's uuid.

## 2026-10-06 — Python symbol model extensions
`symbols()` also emits module-level simple assignments (kind `variable`) and a `<module>` pseudo-symbol (imports, `if __name__` block, other top-level code with defs/variables masked) so import and constant changes are visible. "body" comparison is textual over the symbol's own lines with nested defs replaced by placeholders: a method edit marks only the method modified, not its class; comment-only edits count as body changes.

## 2026-10-06 — Scope of `facts.files`
Files outside the repo root (scratchpad, `~/.claude/...`) and gitignored files are excluded from `files` and listed in `session.outside_repo_files` / `session.ignored_files`. Full file before/after text is not stored: Python files carry per-symbol `before_src`/`after_src`, everything else carries a unified diff (capped at 2000 lines) plus the structured data diff. Keeps facts.json small enough to inline.

## 2026-10-06 — `--since` takes precedence; start_sha fallbacks
**[deviation]** PLAN lists `--since` last. An explicit flag should win over heuristics, so order is `--since` → transcript `git rev-parse HEAD` → parent of first commit authored at/after session start → HEAD if it predates the session → none (empty tree).

## 2026-10-06 — TodoWrite counted as tasks
TodoWrite snapshots (older Claude Code) are folded into `facts.tasks` alongside TaskCreate/TaskUpdate so progress works on both.

## 2026-10-06 — Bash exit codes
Bash results carry no exit-code field. `exit_code` is 0 when `is_error` is false, else parsed from "Exit code N" in the result text, else 1. Because `pytest … | tail` exits 0 even on failure, `test_run` events also carry `tests_failed`, parsed from the runner's summary line. `pytest --version` is not a test run.

## 2026-10-06 — Citation syntax: `(ev:<uuid>)` inside text
PLAN says every `why` "must cite an event uuid" but gives `why` as a plain string. Citations are inline `(ev:<uuid>)` tokens: the schema requires at least one (regex) and build.py checks each uuid exists. The template renders them as links into the timeline.

## 2026-10-06 — Empty narrative is a mode, not a schema exception
`{}` or a missing narrative file renders a facts-only page (§7.3 "graceful"). Any non-empty narrative must fully pass the schema. Partial narratives are still rejected.

## 2026-10-06 — Built-in validator when jsonschema is absent
**[deviation]** PLAN says "jsonschema optional; warn if absent". Warning alone would let invalid narratives through, which breaks the "builder refuses on schema failure" rule. build.py instead falls back to a ~40-line validator for the schema subset we use, and prints a NOTE. A test asserts it reports the same error paths as jsonschema.

## 2026-10-06 — Referential checks beyond the schema
build.py also rejects: unknown files/symbols (with did-you-mean), describing `unchanged` symbols, non-empty `was` on added or `now` on removed symbols, examples without `→`/`->`/`=>`, symbols on files without symbol support, unknown citation uuids, `goal.source` that isn't a user_msg, and lesson `ts` ≠ event ts. Gaps produce warnings only, and only for files where some symbols were described (summary-only files are a deliberate choice).

## 2026-10-06 — Inline JSON escaping
Inline JSON escapes every `<` as `<` (valid JSON, neutralises `</script>` and `<!--`). The first version escaped `<!--` as `<\!--`, an invalid JSON escape, so the narrative silently failed to parse. Found by the §7.3 screenshot; a test now round-trips the embedded JSON.

## 2026-10-06 — Default output dir outside the target repo
SKILL.md writes to `${SESSION_VIZ_OUT:-~/.cache/session-viz}/<session-id>/` so running the skill never dirties the repo being visualized. Within this repo, dev outputs go to `out/` (gitignored).

## 2026-10-06 — publish.sh refuses unless Access is verified
Additions beyond PLAN §5: (1) refuse to deploy if `index.html` references external URLs; (2) before deploying, `curl` the project's pages.dev host and refuse unless it 30x-redirects to `*.cloudflareaccess.com`; (3) after deploying, check the deployment URL and the branch alias the same way, exit 3 with instructions if either is public. The host comes from `wrangler pages project list`, because pages.dev names are global and a taken name gets a suffix. `SESSION_VIZ_SKIP_ACCESS_CHECK=1` bypasses both checks (for tests only). Branch = session id lowercased, sanitised, ≤28 chars.

## 2026-10-06 — §7.2 verification caveat
A fresh subagent following only SKILL.md wrote a narrative that passed on the first `--check`. That understates the difficulty: facts.json for this session contains build.py's source, so the validator's rules were visible to it. Error actionability is covered separately by a deliberately broken narrative (`out/bad_narrative.json`) and `tests/test_build.py::test_referential_errors_are_actionable`. The subagent's SKILL.md feedback (missing jq, sections→data_changes mapping, docs category, scope for large files, piped exit codes) was folded into SKILL.md and the warning logic.

## 2026-10-06 — One sed edit to publish.sh
Convention says project files are edited with Write/Edit. One `sed -i` on `scripts/publish.sh` slipped through (replacing `$PROJECT.pages.dev` with `$HOST`). Left as is. With no later tool edit to that file, replay can't resync from `originalFile`, so the acceptance run should report a `replay_mismatch` discrepancy for it. That is the honest outcome, and it exercises the discrepancy path on real data. Confirmed in §7.5: fidelity 24/25 = 96%, the one miss being this file.

## 2026-10-06 — Grouped citations `(ev:a, ev:b)`
While writing the §7.5 narrative I naturally wrote `(ev:a, ev:b)`, and the validator rejected it with "must cite evidence" even though citations were present. Schema, build.py and template now accept grouped citations, and each uuid is still checked individually. Found only by dogfooding.

## 2026-10-06 — §7 status at end of build session
§7.1 ✔ (26→27 tests, 96% fidelity) · §7.2 ✔ (with caveat above) · §7.3 ✔ (empty + full + mobile screenshots) · §7.4 ✘ not run: needs the user's Cloudflare login, Pages project, and Access application, which are account/infra actions · §7.5 ✔ locally (`~/.cache/session-viz/633112c0-…/dist/index.html`), unpublished.

## 2026-10-06 — Hosting moves from Cloudflare Pages to Workers static assets
**[deviation]** PLAN §5 targets Cloudflare Pages. wrangler 4.148 delegates `pages project create` to "Cloudflare Pages, now part of Cloudflare Workers"; the old product is reachable only via `--force`. The user chose Workers (option 1 of 2) over relying on `--force`. Mapping: Pages branch preview → Worker version `--preview-alias <session-id>` (`<alias>-session-viz.<sub>.workers.dev`); production branch → `wrangler deploy`. Access protects `session-viz.<sub>.workers.dev` and `*-session-viz.<sub>.workers.dev`. The workers.dev subdomain is read from `SESSION_VIZ_SUBDOMAIN` or `~/.config/session-viz/config`, because wrangler has no command to print it and the URL must be known *before* upload for the Access pre-check. Deploys run from a throwaway temp dir with a generated `wrangler.jsonc`, so nothing lands in the repo.

## 2026-10-06 — Never let wrangler pick the workers.dev subdomain
With no workers.dev subdomain registered, `wrangler deploy` tries to auto-register one named after the current directory. A placeholder deploy run from `scratchpad/placeholder` attempted to register `placeholder` as the account-wide subdomain, and failed only because the name was taken. The subdomain is the user's choice. publish.sh can't trigger this: it refuses unless a subdomain is configured *and* the host already redirects to Access, which can't be true for an unregistered subdomain.

## 2026-10-06 — publish.sh finds nvm-installed wrangler
wrangler lives under `~/.nvm/versions/node/*/bin`, which isn't on PATH in non-interactive shells (the skill's). publish.sh now looks there before falling back to `npx`, and prepends that bin dir so wrangler's `#!/usr/bin/env node` resolves to nvm's node, not the old system node. Before this fix, that mismatch surfaced as a misleading "not logged in".

## 2026-10-06 — §7.4 published; login is Cloudflare-account, not email PIN
The user set up Access with the one-click workers.dev / Preview URLs toggles and allowed both of their emails. Verified by HTTP: `session-viz.rushilcd.workers.dev`, a random `probe-…` preview host, and the session alias all 302 to `lingering-hill-8eb3.cloudflareaccess.com`. The login page offers only "Sign in with: Cloudflare". That deviates from PLAN's email-OTP: access requires a Cloudflare account under an allowed email. Accepted for now. Switch to a manual self-hosted application with One-time PIN if non-Cloudflare viewers are ever needed. publish.sh uploaded version `6bb53afa` with alias `633112c0-…-session-viz.rushilcd.workers.dev`, and its post-upload check confirmed both URLs redirect to Access. The main URL still serves the placeholder; `--production` would promote a report there. Remaining human check: log in and see the report render.

## 2026-10-06 — §7.4 confirmed end to end
The first login to the report URL failed with "That account does not have access". The workers.dev application's policy was correct (the placeholder loaded after login), but the separate **Preview URLs** application had its own policy that didn't include the user's email. After that policy was fixed, the user logged in and saw the report. Lesson: the two toggles create two Access applications with independent policies, and per-session reports live on the preview one. README § Hosting step 6 now says so. PLAN §7 is complete.
