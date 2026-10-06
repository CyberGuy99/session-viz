# Glossary

**session** — One Claude Code conversation, stored as `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. `encoded-cwd` is the absolute cwd with `/` (and `.`) replaced by `-`.

**subagent transcript** — A sibling `.jsonl` written by an Agent/Task subagent. Included when its `parentUuid` chain (or `sessionId`) links to the main session.

**turn** — One human prompt and everything the assistant did in response. Numbered from 1 by counting human-origin user messages.

**facts.json** — Deterministic output of `extract.py`. Contains `session`, `files`, `tasks`, `events`, `discrepancies`. Same input → byte-identical output.

**narrative.json** — Claude's judgment layer: `goal`, `progress`, per-file/per-symbol explanations, `lessons`. Validated against `schema/narrative.schema.json` plus referential checks in `build.py`.

**start_sha** — Commit treated as the "before" state for the session. Resolved from the first `git rev-parse HEAD` output in the transcript, else the first commit at/after session start, else `--since`. `null` when the repo had no commits.

**mutation** — A tool call that changes a file: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`.

**replay** — Applying a file's mutations in order to a seeded `before` copy to reconstruct `after`.

**Symbol** — A named code unit found by the symbol parser: `{kind, lineno, end_lineno, source, signature, docstring}`, keyed by qualname (`Class.method`, `outer.inner`).

**SymbolChange** — One entry from `diff_symbols`: `{qualname, status, aspects, before_src, after_src, unified_diff, renamed_from?}`. `status ∈ added|removed|modified|renamed|unchanged`; `aspects ⊆ {signature, body, docstring}` for modified.

**data change** — The non-code equivalent of a SymbolChange: key-path leaf changes for JSON/YAML/TOML, row/header deltas for CSV, heading-section changes for Markdown/text.

**event** — One entry in `facts.events`: `{uuid, ts, turn, kind, file?, summary, exit_code?}` with `kind ∈ edit|bash|error|test_run|user_msg`. The `uuid` is the transcript message uuid and is the citation key for narrative evidence.

**lesson** — A narrative entry linking an event (typically `error` or `test_run`) to what was changed as a result.

**discrepancy** — A disagreement between the transcript and git (or a replay that could not be applied cleanly). Shown in the report rather than silently resolved.

**category** — Exactly one of `accuracy` (behavior/correctness change), `speed` (performance), `refactoring` (no behavior change) per symbol or data change.
