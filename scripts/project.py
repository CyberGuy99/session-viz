#!/usr/bin/env python3
"""Build a static site for a whole Claude Code project: a session index plus one session-viz report each.

    project.py [--project <transcript dir | repo path>] [-o OUT] [--cache DIR] [--force] [--only SID ...]

Every main transcript in ~/.claude/projects/<encoded-cwd>/ is extracted (extract.py) and rendered
(build.py) into OUT/dist/s/<session-id>/index.html; OUT/dist/index.html lists them. Per-session
facts and narratives live in CACHE/<session-id>/, the same directory the session-viz skill writes,
so a narrative written there is picked up on the next run. Unchanged sessions are not re-extracted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "project.html"
CODE = [ROOT / "scripts" / "extract.py", ROOT / "scripts" / "build.py", ROOT / "templates" / "index.html"]
KEY_VERSION = 1


# ───────────────────────── discovery ─────────────────────────

def project_dir(arg: str | None) -> Path:
    """A transcript dir as given, or the ~/.claude/projects dir for a repo path."""
    p = Path(arg or os.getcwd()).expanduser().resolve()
    if any(p.glob("*.jsonl")):
        return p
    return Path.home() / ".claude" / "projects" / extract.encode_cwd(str(p))


def main_transcripts(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.jsonl") if not p.name.startswith("agent-"))


def _stat(p: Path) -> list:
    st = p.stat()
    return [str(p), st.st_size, st.st_mtime_ns]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def worktree_hash(repo: str) -> str:
    """Changes on top of HEAD: tracked diff + untracked files' stats."""
    diff = subprocess.run(["git", "-C", repo, "diff", "HEAD", "--binary"], capture_output=True).stdout
    untracked = (extract.git(repo, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    stats = [_stat(Path(repo) / u) for u in untracked if (Path(repo) / u).is_file()]
    return _sha(diff + json.dumps(stats).encode())


def cache_key(main: Path, cwd: str | None, narrative: Path, prev: dict) -> dict:
    """Everything a session's facts + page depend on. The worktree only matters while the
    session's end state is the worktree (no later commits)."""
    side = sorted(main.parent.glob("agent-*.jsonl")) + sorted((main.parent / main.stem).rglob("*.jsonl"))
    repo = (extract.git(cwd, "rev-parse", "--show-toplevel") or "").strip() if cwd and os.path.isdir(cwd) else ""
    key = {
        "v": KEY_VERSION,
        "transcripts": [_stat(p) for p in [main, *side]],
        "code": [_sha(p.read_bytes()) for p in CODE],
        "narrative": _stat(narrative) if narrative.exists() else None,
        "head": (extract.git(repo, "rev-parse", "HEAD") or "").strip() if repo else None,
    }
    if repo and prev.get("end_ref") == "worktree":
        key["worktree"] = worktree_hash(repo)
    return key


# ───────────────────────── per session ─────────────────────────

def narrative_state(path: Path, facts: dict) -> tuple[dict, str, list[str]]:
    """(narrative to render, ok|stale|missing, first errors)."""
    if not path.exists():
        return {}, "missing", []
    try:
        narr = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return {}, "stale", [f"invalid JSON: {e}"]
    if not narr:
        return {}, "missing", []
    errs, _ = build.validate(narr, facts)
    return (narr, "ok", []) if not errs else ({}, "stale", errs[:3])


def transcript_extras(records: list[dict]) -> dict:
    """Fields only the raw transcript has: Claude Code's own title and cost."""
    title = cost = None
    for r in records:
        if r.get("type") == "ai-title" and r.get("aiTitle"):
            title = r["aiTitle"]
        elif r.get("type") == "cost-state" and r.get("totalCostUSD") is not None:
            cost = round(float(r["totalCostUSD"]), 4)
    return {"ai_title": title, "cost_usd": cost}


def summarize(main: Path, records: list[dict], facts: dict, narr: dict, state: str, errors: list[str],
              href: str) -> dict:
    s, events = facts["session"], facts["events"]
    changed = [f for f in facts["files"].values() if f.get("status") != "unchanged"]
    tests = [e for e in events if e["kind"] == "test_run"]
    tasks = facts["tasks"]
    extras = transcript_extras(records)
    goal = (s.get("goal_candidate") or {}).get("text", "")
    title = extras["ai_title"] or (narr.get("goal") or {}).get("statement") or extract.first_line(goal, 120)
    regen = [f"/session-viz --session {main}"]
    if any(d["kind"] == "compacted_without_since" for d in facts["discrepancies"]):
        regen[0] += " --since <commit the session started from>"
    regen.append(f"python3 {ROOT / 'scripts' / 'project.py'} --project {main.parent}")
    return {
        "id": main.stem, "href": href, "transcript": str(main), "title": title,
        "first_prompt": extract.first_line(goal, 300),
        "start": s["start"], "end": s["end"], "duration_s": s["duration_s"], "turns": s["turns"],
        "branch": s.get("git_branch"), "cwd": s.get("cwd"), "compacted": s["compacted"],
        "end_ref": s.get("end_ref"),
        "files_changed": len(changed),
        "added_lines": sum(f.get("added_lines", 0) for f in changed),
        "removed_lines": sum(f.get("removed_lines", 0) for f in changed),
        "edits": sum(1 for e in events if e["kind"] == "edit"),
        "test_runs": len(tests), "tests_failed": sum(1 for e in tests if e.get("tests_failed")),
        "errors": sum(1 for e in events if e["kind"] == "error"),
        "tasks_total": len(tasks), "tasks_done": sum(1 for t in tasks if t["status"] == "completed"),
        "discrepancies": len(facts["discrepancies"]),
        "subagents": len(s.get("subagent_transcripts", [])),
        "progress": (narr.get("progress") or {}).get("percent") if state == "ok" else None,
        "cost_usd": extras["cost_usd"],
        "narrative": state, "narrative_errors": errors, "regen": regen if state != "ok" else [],
    }


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def process(main: Path, cache: Path, dist: Path, force: bool) -> tuple[dict | None, str]:
    """(summary or None if the transcript has no human prompt, cached|built|skipped|failed)."""
    sdir = cache / main.stem
    key_file, sum_file, facts_file = sdir / "build.key.json", sdir / "summary.json", sdir / "facts.json"
    page = dist / "s" / main.stem / "index.html"
    prev = json.loads(key_file.read_text()) if key_file.exists() else {}
    prev_sum = json.loads(sum_file.read_text()) if sum_file.exists() else None
    records = None
    if prev_sum and prev_sum.get("cwd"):
        cwd = prev_sum["cwd"]
    else:
        records = extract.load_jsonl(main)
        cwd = next((r["cwd"] for r in records if r.get("cwd")), None)
    key = cache_key(main, cwd, sdir / "narrative.json", prev)
    if not force and prev.get("key") == key and prev_sum is not None and (prev_sum == {} or page.exists()):
        return prev_sum or None, "cached"

    records = records if records is not None else extract.load_jsonl(main)
    if not any(extract.is_human_prompt(r) and r.get("timestamp") for r in records):
        summary, status = {}, "skipped"
    else:
        facts, _ = extract.extract(main, None, None)
        facts_file.parent.mkdir(parents=True, exist_ok=True)
        facts_file.write_text(json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        narr, state, errors = narrative_state(sdir / "narrative.json", facts)
        write_if_changed(page, build.render(facts, narr, "../../index.html"))
        summary = summarize(main, records, facts, narr, state, errors, f"s/{main.stem}/index.html")
        status = "built"
    sdir.mkdir(parents=True, exist_ok=True)
    sum_file.write_text(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    end_ref = (summary or {}).get("end_ref")
    if end_ref == "worktree" and "worktree" not in key and cwd:  # first build: key it on the worktree now
        key = cache_key(main, cwd, sdir / "narrative.json", {"end_ref": "worktree"})
    key_file.write_text(json.dumps({"key": key, "end_ref": end_ref}, indent=2, sort_keys=True) + "\n")
    return summary or None, status


# ───────────────────────── index ─────────────────────────

def render_index(proj: Path, sessions: list[dict]) -> str:
    sessions = sorted(sessions, key=lambda s: (s["start"] or "", s["id"]), reverse=True)
    cwd = next((s["cwd"] for s in sessions if s.get("cwd")), None)
    data = {"project": {"dir": str(proj), "cwd": cwd, "name": Path(cwd).name if cwd else proj.name},
            "sessions": sessions}
    tpl = TEMPLATE.read_text(encoding="utf-8")
    title = f"{data['project']['name']}: Claude Code sessions".replace("&", "&amp;").replace("<", "&lt;")
    return tpl.replace("<!--DATA-->", build.embed("project", data)).replace("<!--TITLE-->", title)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="transcript dir (~/.claude/projects/<encoded>) or repo path (default: cwd)")
    ap.add_argument("--cache", help="per-session facts/narratives (default: $SESSION_VIZ_OUT or ~/.cache/session-viz)")
    ap.add_argument("-o", "--out", help="site output dir; the site is OUT/dist (default: CACHE/project-<encoded>)")
    ap.add_argument("--force", action="store_true", help="re-extract every session, ignoring the cache")
    ap.add_argument("--only", nargs="+", metavar="SID", help="limit the site to these session ids (prefixes ok)")
    a = ap.parse_args(argv)

    proj = project_dir(a.project)
    mains = main_transcripts(proj) if proj.is_dir() else []
    if a.only:
        mains = [m for m in mains if any(m.stem.startswith(o) for o in a.only)]
    if not mains:
        print(f"no session transcripts under {proj}", file=sys.stderr)
        return 1
    cache = Path(a.cache or os.environ.get("SESSION_VIZ_OUT") or Path.home() / ".cache" / "session-viz").expanduser()
    out = Path(a.out).expanduser() if a.out else cache / f"project-{proj.name.lstrip('-')}"
    dist = out / "dist"

    sessions, counts = [], {}
    for m in mains:
        try:
            s, status = process(m, cache, dist, a.force)
        except Exception as e:  # one bad transcript must not sink the whole site
            print(f"FAILED {m.stem}: {type(e).__name__}: {e}", file=sys.stderr)
            s, status = None, "failed"
        counts[status] = counts.get(status, 0) + 1
        if s:
            sessions.append(s)

    keep = {s["id"] for s in sessions}
    for d in sorted((dist / "s").glob("*")) if (dist / "s").is_dir() else []:
        if d.is_dir() and d.name not in keep:  # transcript deleted, or excluded by --only
            shutil.rmtree(d)
    write_if_changed(dist / "index.html", render_index(proj, sessions))

    print(", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + f" → {dist / 'index.html'}", file=sys.stderr)
    todo = [s for s in sorted(sessions, key=lambda s: s["start"] or "") if s["narrative"] != "ok"]
    if todo:
        print(f"\n{len(todo)} session(s) without a valid narrative (pages are facts-only):", file=sys.stderr)
        for s in todo:
            print(f"  {s['id'][:8]}  {s['narrative']:<7}  {s['title'][:70]}", file=sys.stderr)
            for err in s["narrative_errors"]:
                print(f"      {err}", file=sys.stderr)
            print(f"      in Claude Code: {s['regen'][0]}", file=sys.stderr)
        print(f"Then rebuild (only changed sessions are re-extracted):\n  {todo[0]['regen'][1]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
