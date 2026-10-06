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
import blocks  # noqa: E402
import build  # noqa: E402
import extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "project.html"
CODE = [ROOT / "scripts" / "extract.py", ROOT / "scripts" / "build.py", ROOT / "scripts" / "blocks.py",
        ROOT / "scripts" / "project.py",
        ROOT / "templates" / "index.html", ROOT / "templates" / "blocks.js", ROOT / "templates" / "blocks.css"]
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


def cache_key(main: Path, cwd: str | None, narrative: Path, prev: dict, site_session: dict | None = None) -> dict:
    """Everything a session's facts + page depend on. The worktree only matters while the
    session's end state is the worktree (no later commits)."""
    side = sorted(main.parent.glob("agent-*.jsonl")) + sorted((main.parent / main.stem).rglob("*.jsonl"))
    repo = (extract.git(cwd, "rev-parse", "--show-toplevel") or "").strip() if cwd and os.path.isdir(cwd) else ""
    key = {
        "v": KEY_VERSION,
        "site": _sha(json.dumps(site_session or {}, sort_keys=True).encode()),
        "transcripts": [_stat(p) for p in [main, *side]],
        "code": [_sha(p.read_bytes()) for p in CODE],
        "narrative": _stat(narrative) if narrative.exists() else None,
        "narrative_meta": _stat(build.meta_path(narrative)) if build.meta_path(narrative).exists() else None,
        "head": (extract.git(repo, "rev-parse", "HEAD") or "").strip() if repo else None,
    }
    if repo and prev.get("end_ref") == "worktree":
        key["worktree"] = worktree_hash(repo)
    return key


# ───────────────────────── per session ─────────────────────────

NOTABLE = ("user_msg", "edit", "test_run")


def narrative_state(path: Path, facts: dict) -> tuple[dict, str, list[str], dict]:
    """(narrative to render, ok|partial|stale|missing, first errors, how far it is behind the facts).

    partial: some entries no longer match the facts and are dropped; the rest renders."""
    behind = {"events": 0, "entries": 0}
    if not path.exists():
        return {}, "missing", [], behind
    try:
        narr = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return {}, "stale", [f"invalid JSON: {e}"], behind
    if not narr:
        return {}, "missing", [], behind
    state, notes = "ok", []
    errs, _ = build.validate(narr, facts)
    if errs:
        pruned, dropped = build.prune_invalid(narr, facts)
        if not dropped or build.validate(pruned, facts)[0]:
            return {}, "stale", errs[:3], behind
        narr, state = pruned, "partial"
        notes = [f"dropped out-of-date entry {d}" for d in dropped[:3]] + \
            ([f"… and {len(dropped) - 3} more"] if len(dropped) > 3 else [])
    mp = build.meta_path(path)
    d = build.delta(narr, facts, json.loads(mp.read_text()) if mp.exists() else None)
    behind = {"events": sum(1 for e in d["new_events"]  # edits outside the repo (scratch, caches) aren't news
                            if e["kind"] in NOTABLE and (e["kind"] != "edit" or e.get("file"))),
              "entries": len(d["stale"]) + len(d["unreviewed"])}
    return narr, state, notes, behind


def transcript_extras(records: list[dict]) -> dict:
    """Fields only the raw transcript has: Claude Code's own title and cost."""
    title = cost = None
    for r in records:
        if r.get("type") == "ai-title" and r.get("aiTitle"):
            title = r["aiTitle"]
        elif r.get("type") == "cost-state" and r.get("totalCostUSD") is not None:
            cost = round(float(r["totalCostUSD"]), 4)
    return {"ai_title": title, "cost_usd": cost}


def needs_work(state: str, behind: dict) -> bool:
    return state != "ok" or behind["events"] > 0 or behind["entries"] > 0


def summarize(main: Path, records: list[dict], facts: dict, narr: dict, state: str, errors: list[str],
              behind: dict, href: str) -> dict:
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
        "files": [{"path": p, "status": f.get("status"), "language": f.get("language"),
                   "added_lines": f.get("added_lines", 0), "removed_lines": f.get("removed_lines", 0),
                   "edits": f.get("mutation_count", 0)}
                  for p, f in sorted(facts["files"].items()) if f.get("status") != "unchanged"],
        "test_runs": len(tests), "tests_failed": sum(1 for e in tests if e.get("tests_failed")),
        "tests_last_failed": bool(tests and tests[-1].get("tests_failed")),
        "errors": sum(1 for e in events if e["kind"] == "error"),
        "errors_with_lessons": len({e["uuid"] for e in events if e["kind"] == "error"} &
                                   {l.get("event_ref") for l in narr.get("lessons") or []}),
        "tasks_total": len(tasks), "tasks_done": sum(1 for t in tasks if t["status"] == "completed"),
        "discrepancies": len(facts["discrepancies"]),
        "subagents": len(s.get("subagent_transcripts", [])),
        "progress": (narr.get("progress") or {}).get("percent") if state in ("ok", "partial") else None,
        "cost_usd": extras["cost_usd"],
        "narrative": state, "narrative_errors": errors, "narrative_behind": behind,
        "regen": regen if needs_work(state, behind) else [],
    }


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def session_site(site: dict, facts: dict) -> dict:
    """site.json's session section resolved against one session's facts, for build.render()."""
    sec = site.get("session") or {}
    return {"blocks": blocks.resolve(sec.get("blocks") or [], lambda src: blocks.session_rows(src, facts)),
            "hide": sec.get("hide") or []}


def process(main: Path, cache: Path, dist: Path, force: bool, site: dict | None = None) -> tuple[dict | None, str]:
    """(summary or None if the transcript has no human prompt, cached|built|skipped|failed)."""
    site = site or {}
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
    key = cache_key(main, cwd, sdir / "narrative.json", prev, site.get("session"))
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
        narr, state, errors, behind = narrative_state(sdir / "narrative.json", facts)
        write_if_changed(page, build.render(facts, narr, "../../index.html", session_site(site, facts)))
        summary = summarize(main, records, facts, narr, state, errors, behind, f"s/{main.stem}/index.html")
        status = "built"
    sdir.mkdir(parents=True, exist_ok=True)
    sum_file.write_text(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    end_ref = (summary or {}).get("end_ref")
    if end_ref == "worktree" and "worktree" not in key and cwd:  # first build: key it on the worktree now
        key = cache_key(main, cwd, sdir / "narrative.json", {"end_ref": "worktree"}, site.get("session"))
    key_file.write_text(json.dumps({"key": key, "end_ref": end_ref}, indent=2, sort_keys=True) + "\n")
    return summary or None, status


# ───────────────────────── index ─────────────────────────

def home_blocks(site: dict, sessions: list[dict]) -> list[dict]:
    return blocks.resolve((site.get("home") or {}).get("blocks") or [], lambda src: blocks.home_rows(src, sessions))


def render_index(proj: Path, sessions: list[dict], site: dict | None = None) -> str:
    site = site or {}
    sessions = sorted(sessions, key=lambda s: (s["start"] or "", s["id"]), reverse=True)
    cwd = next((s["cwd"] for s in sessions if s.get("cwd")), None)
    data = {"project": {"dir": str(proj), "cwd": cwd, "name": Path(cwd).name if cwd else proj.name},
            "sessions": [{k: v for k, v in s.items() if k != "files"} for s in sessions]}
    tpl = TEMPLATE.read_text(encoding="utf-8")
    title = f"{data['project']['name']}: Claude Code sessions".replace("&", "&amp;").replace("<", "&lt;")
    assets = build.site_assets(home_blocks(site, sessions), (site.get("home") or {}).get("hide") or [])
    return tpl.replace("<!--DATA-->", build.embed("project", data)).replace("<!--TITLE-->", title) \
              .replace("<!--BLOCKS-->", assets)


def check_site(site: dict, site_path: Path, mains: list[Path], cache: Path) -> int:
    """Preview: home blocks over the cached summaries, session blocks over the newest session's facts."""
    sums = [json.loads(p.read_text()) for m in mains if (p := cache / m.stem / "summary.json").exists()]
    sums = [s for s in sums if s]
    if not sums:
        print("no cached sessions yet: run project.py once without --check-site", file=sys.stderr)
        return 1
    newest = max(sums, key=lambda s: s.get("start") or "")
    facts = json.loads((cache / newest["id"] / "facts.json").read_text())
    preview = {"home": home_blocks(site, sums), f"session {newest['id'][:8]}": session_site(site, facts)["blocks"],
               "hidden": {k: (site.get(k) or {}).get("hide", []) for k in ("home", "session")}}
    print(json.dumps(preview, indent=1, ensure_ascii=False, default=str))
    print(f"{site_path}: valid", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="transcript dir (~/.claude/projects/<encoded>) or repo path (default: cwd)")
    ap.add_argument("--cache", help="per-session facts/narratives (default: $SESSION_VIZ_OUT or ~/.cache/session-viz)")
    ap.add_argument("-o", "--out", help="site output dir; the site is OUT/dist (default: CACHE/project-<encoded>)")
    ap.add_argument("--force", action="store_true", help="re-extract every session, ignoring the cache")
    ap.add_argument("--only", nargs="+", metavar="SID", help="limit the site to these session ids (prefixes ok)")
    ap.add_argument("--site", help="customizations agreed from a special-requests file (default: OUT/site.json)")
    ap.add_argument("--catalog", action="store_true",
                    help="print what site.json can use (sources, fields, block kinds, hideable sections) and exit")
    ap.add_argument("--check-site", action="store_true",
                    help="validate site.json and print its blocks resolved against the cached sessions; write nothing")
    a = ap.parse_args(argv)

    if a.catalog:
        print(json.dumps(blocks.catalog(), indent=1))
        return 0
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
    site_path = Path(a.site).expanduser() if a.site else out / "site.json"
    try:
        site = blocks.load(site_path)
    except json.JSONDecodeError as e:
        print(f"{site_path}: invalid JSON: {e}", file=sys.stderr)
        return 2
    errs = blocks.validate_site(site)
    if errs:
        print("\n".join(f"ERROR {e}" for e in errs) + f"\n\n{site_path}: {len(errs)} error(s); nothing built.",
              file=sys.stderr)
        return 2
    if a.check_site:
        return check_site(site, site_path, mains, cache)

    sessions, counts = [], {}
    for m in mains:
        try:
            s, status = process(m, cache, dist, a.force, site)
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
    write_if_changed(dist / "index.html", render_index(proj, sessions, site))

    print(", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + f" → {dist / 'index.html'}", file=sys.stderr)
    todo = [s for s in sorted(sessions, key=lambda s: s["start"] or "") if s["regen"]]
    if todo:
        print(f"\n{len(todo)} session(s) whose narrative is missing or behind:", file=sys.stderr)
        for s in todo:
            b = s["narrative_behind"]
            lag = f" (+{b['events']} events, {b['entries']} changed entries)" if b["events"] or b["entries"] else ""
            print(f"  {s['id'][:8]}  {s['narrative']:<7}{lag}  {s['title'][:60]}", file=sys.stderr)
            for err in s["narrative_errors"]:
                print(f"      {err}", file=sys.stderr)
            print(f"      in Claude Code: {s['regen'][0]}   (updates incrementally if a narrative exists)",
                  file=sys.stderr)
        print(f"Then rebuild (only changed sessions are re-extracted):\n  {todo[0]['regen'][1]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
