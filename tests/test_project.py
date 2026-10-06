"""project.py on a synthetic transcript dir: index, per-session pages, narrative states, cache."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import project  # noqa: E402
from test_extract import T, setup_repo  # noqa: E402


def session(repo, hour, prompt, content):
    t = T(repo)
    t.prompt(prompt)
    t.tool("Write", {"file_path": str(repo / "calc.py"), "content": content})
    for r in t.lines:
        r["timestamp"] = r["timestamp"].replace("T00:", f"T{hour:02d}:")
        r["sessionId"] = f"S{hour}"
    return t


def make_project(tmp_path):
    repo = setup_repo(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    session(repo, 1, "first: make X", "X = 1\n").write(proj / "aaa.jsonl")
    session(repo, 2, "second: make Y", "Y = 2\n").write(proj / "bbb.jsonl")
    (repo / "calc.py").write_text("Y = 2\n")
    (proj / "agent-zzz.jsonl").write_text("")  # legacy subagent file: never a session
    noprompt = T(repo)
    noprompt._rec("assistant", [{"type": "text", "text": "hi"}])
    noprompt.write(proj / "ccc.jsonl")
    return repo, proj


def run(proj, tmp_path, *extra):
    return project.main(["--project", str(proj), "--cache", str(tmp_path / "cache"),
                         "-o", str(tmp_path / "site"), *extra])


def index_data(tmp_path):
    html = (tmp_path / "site" / "dist" / "index.html").read_text()
    raw = html.split('<script id="project" type="application/json">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)


def narrative_for(tmp_path, sid):
    facts = json.loads((tmp_path / "cache" / sid / "facts.json").read_text())
    um = next(e for e in facts["events"] if e["kind"] == "user_msg")
    return {"goal": {"statement": f"goal of {sid}", "completion_indicator": "done", "source": f"user_msg#{um['uuid']}"},
            "progress": {"percent": 40, "basis": "judged", "done": [], "remaining": ["rest"]},
            "files": {}, "lessons": []}


def test_index_pages_and_narrative_states(tmp_path, capsys):
    _, proj = make_project(tmp_path)
    assert run(proj, tmp_path) == 0
    data = index_data(tmp_path)
    assert [s["id"] for s in data["sessions"]] == ["bbb", "aaa"]  # newest first; agent-*/prompt-less excluded
    for s in data["sessions"]:
        page = tmp_path / "site" / "dist" / s["href"]
        assert page.exists() and 'href="../../index.html"' in page.read_text()
        assert s["narrative"] == "missing" and s["regen"][0] == f"/session-viz --session {proj / (s['id'] + '.jsonl')}"
    assert "/session-viz --session" in capsys.readouterr().err

    (tmp_path / "cache" / "aaa" / "narrative.json").write_text(json.dumps(narrative_for(tmp_path, "aaa")))
    (tmp_path / "cache" / "bbb" / "narrative.json").write_text(json.dumps({**narrative_for(tmp_path, "bbb"),
                                                                           "files": {"nope.py": {"summary": "x"}}}))
    run(proj, tmp_path)
    by = {s["id"]: s for s in index_data(tmp_path)["sessions"]}
    assert by["aaa"]["narrative"] == "ok" and by["aaa"]["progress"] == 40 and by["aaa"]["regen"] == []
    assert "goal of aaa" in (tmp_path / "site" / "dist" / "s" / "aaa" / "index.html").read_text()
    assert by["bbb"]["narrative"] == "stale" and "nope.py" in by["bbb"]["narrative_errors"][0]
    assert "goal of bbb" not in (tmp_path / "site" / "dist" / "s" / "bbb" / "index.html").read_text()


def test_cache_hits_and_selective_rebuild(tmp_path, capsys):
    _, proj = make_project(tmp_path)
    run(proj, tmp_path)
    pages = {sid: tmp_path / "site" / "dist" / "s" / sid / "index.html" for sid in ("aaa", "bbb")}
    before = {sid: p.stat().st_mtime_ns for sid, p in pages.items()}
    capsys.readouterr()
    run(proj, tmp_path)
    assert capsys.readouterr().err.startswith("3 cached")  # aaa, bbb + the prompt-less ccc
    assert {sid: p.stat().st_mtime_ns for sid, p in pages.items()} == before

    with open(proj / "aaa.jsonl", "a") as fh:
        fh.write("\n")
    os.utime(proj / "aaa.jsonl", ns=(1, 1))
    run(proj, tmp_path)
    assert capsys.readouterr().err.startswith("1 built, 2 cached")


def test_deterministic_site(tmp_path):
    _, proj = make_project(tmp_path)
    run(proj, tmp_path)
    snap = {p: p.read_bytes() for p in (tmp_path / "site").rglob("*") if p.is_file()}
    run(proj, tmp_path, "--force")
    assert {p: p.read_bytes() for p in (tmp_path / "site").rglob("*") if p.is_file()} == snap


def test_only_limits_site(tmp_path):
    _, proj = make_project(tmp_path)
    run(proj, tmp_path)
    run(proj, tmp_path, "--only", "bb")
    assert [s["id"] for s in index_data(tmp_path)["sessions"]] == ["bbb"]
    assert not (tmp_path / "site" / "dist" / "s" / "aaa").exists()
