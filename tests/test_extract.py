"""End-to-end extract() on a synthetic transcript + temp git repo."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract  # noqa: E402


class T:
    """Builds a minimal Claude Code transcript."""

    def __init__(self, cwd):
        self.cwd, self.lines, self.n = str(cwd), [], 0

    def _rec(self, type_, content, **extra):
        self.n += 1
        r = {"type": type_, "uuid": f"u{self.n:03d}", "timestamp": f"2026-01-01T00:{self.n:02d}:00.000Z",
             "sessionId": "S", "cwd": self.cwd, "message": {"role": type_, "content": content}, **extra}
        self.lines.append(r)
        return r

    def prompt(self, text):
        return self._rec("user", text, origin={"kind": "human"})

    def tool(self, name, inp, result="ok", is_error=False, tur=None):
        tid = f"toolu_{self.n}"
        self._rec("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}])
        extra = {"toolUseResult": tur} if tur is not None else {}
        return self._rec("user", [{"type": "tool_result", "tool_use_id": tid, "content": result,
                                   "is_error": is_error}], **extra)

    def write(self, path):
        path.write_text("\n".join(json.dumps(r) for r in self.lines) + "\n")
        return path


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def setup_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("def parse(s):\n    return s.split(',')\n")
    (repo / "conf.json").write_text('{"a": 1, "b": {"c": 2}}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init", "--date", "2025-01-01T00:00:00Z")
    return repo


def test_replay_events_tasks_discrepancies(tmp_path):
    repo = setup_repo(tmp_path)
    t = T(repo)
    t.prompt("Make parse return ints")
    t.tool("TaskCreate", {"subject": "ints"}, "Task #1 created", tur={"task": {"id": "1"}})
    t.tool("Edit", {"file_path": str(repo / "calc.py"), "old_string": "nope", "new_string": "x"},
           "<tool_use_error>String to replace not found</tool_use_error>", is_error=True)
    t.tool("Edit", {"file_path": str(repo / "calc.py"), "old_string": "return s.split(',')",
                    "new_string": "return [int(x) for x in s.split(',')]"})
    t.tool("Bash", {"command": "pytest -q"}, "1 failed\nExit code 1", is_error=True)
    t.tool("Edit", {"file_path": str(repo / "calc.py"), "old_string": "def parse(s):\n",
                    "new_string": "def parse(s):\n    if not s:\n        return []\n"})
    t.tool("Write", {"file_path": str(repo / "new.md"), "content": "# New\n\nhello\n"})
    t.tool("Edit", {"file_path": str(repo / "conf.json"), "old_string": '"c": 2', "new_string": '"c": 3, "d": 4'})
    t.tool("TaskUpdate", {"taskId": "1", "status": "completed"})
    t.prompt("thanks")

    # apply the same changes to disk (what the tools would have done) + one Bash-only change
    (repo / "calc.py").write_text("def parse(s):\n    if not s:\n        return []\n"
                                  "    return [int(x) for x in s.split(',')]\n")
    (repo / "new.md").write_text("# New\n\nhello\n")
    (repo / "conf.json").write_text('{"a": 1, "b": {"c": 3, "d": 4}}\n')
    (repo / "bash_made.txt").write_text("hi\n")

    facts, meta = extract.extract(t.write(tmp_path / "S.jsonl"), str(repo), None)

    assert facts["session"]["turns"] == 2
    assert facts["session"]["start_sha_basis"] == "HEAD predates session"
    assert all(v["match"] for v in meta["verify"])

    calc = facts["files"]["calc.py"]
    assert calc["seed_source"] == "git" and calc["mutation_count"] == 2
    parse = {s["qualname"]: s for s in calc["symbols"]}["parse"]
    assert parse["status"] == "modified" and parse["aspects"] == ["body"]

    conf = facts["files"]["conf.json"]
    assert {(c["path"], c["op"]) for c in conf["data_changes"]} == {("b.c", "changed"), ("b.d", "added")}
    assert facts["files"]["new.md"]["status"] == "added"
    assert facts["files"]["new.md"]["sections"][0]["op"] == "added"

    kinds = [e["kind"] for e in facts["events"]]
    assert kinds.count("user_msg") == 2 and "test_run" in kinds and kinds.count("error") == 1
    test_ev = next(e for e in facts["events"] if e["kind"] == "test_run")
    assert test_ev["exit_code"] == 1 and test_ev["turn"] == 1

    # u001 prompt, u002/u003 TaskCreate use/result, ... u016/u017 TaskUpdate use/result
    assert facts["tasks"] == [{"id": "1", "subject": "ints", "status": "completed", "history": [
        {"ts": "2026-01-01T00:02:00.000Z", "status": "pending", "uuid": "u003"},
        {"ts": "2026-01-01T00:16:00.000Z", "status": "completed", "uuid": "u017"}]}]

    d = {(x["kind"], x["path"]) for x in facts["discrepancies"]}
    assert d == {("git_only", "bash_made.txt")}


def test_resync_on_external_modification(tmp_path):
    repo = setup_repo(tmp_path)
    t = T(repo)
    t.prompt("go")
    t.tool("Bash", {"command": "sed -i s/split/rsplit/ calc.py"})
    t.tool("Edit", {"file_path": str(repo / "calc.py"), "old_string": "def parse(s):",
                    "new_string": "def parse(s: str):"},
           tur={"originalFile": "def parse(s):\n    return s.rsplit(',')\n"})
    (repo / "calc.py").write_text("def parse(s: str):\n    return s.rsplit(',')\n")
    facts, meta = extract.extract(t.write(tmp_path / "S.jsonl"), str(repo), None)
    assert meta["verify"] == [{"path": "calc.py", "match": True}]
    assert [x["kind"] for x in facts["discrepancies"]] == ["seed_mismatch"]


def test_read_only_files_are_not_changes(tmp_path):
    repo = setup_repo(tmp_path)
    (repo / ".gitignore").write_text("shots/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore", "--date", "2025-01-02T00:00:00Z")
    (repo / "shots").mkdir()
    (repo / "shots" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\x00")
    t = T(repo)
    t.prompt("go")
    t.tool("Read", {"file_path": str(repo / "calc.py")}, "     1\tdef parse(s):\n")
    t.tool("Read", {"file_path": str(repo / "shots" / "a.png")}, "[image]")
    facts, _ = extract.extract(t.write(tmp_path / "S.jsonl"), str(repo), None)
    assert facts["files"] == {}


def test_byte_identical_output(tmp_path):
    repo = setup_repo(tmp_path)
    t = T(repo)
    t.prompt("go")
    t.tool("Write", {"file_path": str(repo / "calc.py"), "content": "X = 1\n"})
    (repo / "calc.py").write_text("X = 1\n")
    p = t.write(tmp_path / "S.jsonl")
    dump = lambda: json.dumps(extract.extract(p, str(repo), None)[0], sort_keys=True, indent=2)
    assert dump() == dump()


def test_historical_session_uses_end_commit(tmp_path):
    repo = setup_repo(tmp_path)
    t = T(repo)
    t.prompt("ints")
    new = "def parse(s):\n    return [int(x) for x in s.split(',')]\n"
    t.tool("Write", {"file_path": str(repo / "calc.py"), "content": new})
    t.prompt("thanks")  # session ends 2026-01-01T00:04
    (repo / "calc.py").write_text(new)
    git(repo, "commit", "-qam", "ints", "--date", "2026-01-01T00:03:30Z")
    session_end = git_out(repo, "rev-parse", "HEAD")
    # a later session's work, committed and uncommitted
    (repo / "conf.json").write_text('{"a": 2}\n')
    git(repo, "commit", "-qam", "later", "--date", "2026-02-01T00:00:00Z")
    (repo / "calc.py").write_text("X = 1\n")

    p = t.write(tmp_path / "S.jsonl")
    facts, meta = extract.extract(p, str(repo), None)
    s = facts["session"]
    assert s["end_ref"] == session_end and s["end_ref_basis"].startswith("last commit before")
    assert set(facts["files"]) == {"calc.py"} and meta["verify"] == [{"path": "calc.py", "match": True}]
    assert facts["discrepancies"] == []

    facts, meta = extract.extract(p, str(repo), None, "worktree")
    assert facts["session"]["end_ref"] == "worktree"
    assert {(x["kind"], x["path"]) for x in facts["discrepancies"]} == \
        {("replay_mismatch", "calc.py"), ("git_only", "conf.json")}


def test_untracked_input_read_but_never_edited_predates_session(tmp_path):
    repo = setup_repo(tmp_path)
    (repo / "PLAN.md").write_text("# Plan\n")  # untracked before the session
    t = T(repo)
    t.prompt("Read PLAN.md and go")
    t.tool("Read", {"file_path": str(repo / "PLAN.md")}, "     1\t# Plan\n",
           tur={"file": {"content": "# Plan\n", "startLine": 1, "numLines": 1, "totalLines": 1}})
    t.tool("Bash", {"command": "echo hi > made.txt"})
    t.tool("Read", {"file_path": str(repo / "made.txt")}, "     1\thi\n",
           tur={"file": {"content": "hi\n", "startLine": 1, "numLines": 1, "totalLines": 1}})
    (repo / "SPEC.md").write_text("spec\n")  # untracked input shown via Bash
    t.tool("Bash", {"command": "cat SPEC.md && ls"}, "spec\nPLAN.md\nSPEC.md")
    t.tool("Bash", {"command": "printf 'x\\n' > out.txt && cat out.txt"}, "x")  # written by Bash: not pre-existing
    (repo / "out.txt").write_text("x\n")
    (repo / "made.txt").write_text("hi\n")
    facts, _ = extract.extract(t.write(tmp_path / "S.jsonl"), str(repo), None)
    assert facts["session"]["pre_existing_files"] == ["PLAN.md", "SPEC.md"] and "PLAN.md" not in facts["files"]
    # created by Bash before its first Read: a real change of this session
    assert {(x["kind"], x["path"]) for x in facts["discrepancies"]} == {("git_only", "made.txt"),
                                                                        ("git_only", "out.txt")}


def test_empty_new_string_deletes_line():
    assert extract.apply_mutation("Edit", {"old_string": "b = 2", "new_string": ""}, "a = 1\nb = 2\nc = 3\n") == \
        "a = 1\nc = 3\n"
    assert extract.apply_mutation("Edit", {"old_string": "b = 2\n", "new_string": ""}, "a = 1\nb = 2\nc = 3\n") == \
        "a = 1\nc = 3\n"


def test_chained_command_failure_is_not_a_test_failure(tmp_path):
    repo = setup_repo(tmp_path)
    t = T(repo)
    t.prompt("go")
    t.tool("Bash", {"command": "pytest -q | tail -1 && python3 -c 'boom'"}, "34 passed in 1s\nNameError\nExit code 1",
           is_error=True)
    t.tool("Bash", {"command": "pytest -q"}, "ImportError while collecting\nExit code 2", is_error=True)
    facts, _ = extract.extract(t.write(tmp_path / "S.jsonl"), str(repo), None)
    assert [e["tests_failed"] for e in facts["events"] if e["kind"] == "test_run"] == [False, True]


def git_out(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def test_csv_and_structured_helpers():
    c = extract.diff_csv("a,b\n1,2\n3,4\n", "a,b,c\n1,2,0\n3,4,0\n5,6,0\n")
    assert c["rows_before"] == 2 and c["rows_after"] == 3 and c["columns_added"] == ["c"]
    assert len(c["changed_rows"]) == 3
    s = extract.diff_structured("json", '{"x": [1, 2]}', '{"x": [1]}')
    assert s == [{"path": "x[1]", "op": "removed", "before": "2", "after": None}]


def test_large_repetitive_diff_uses_git_and_is_fast():
    import time
    before = "\n".join(["  0,"] * 20000 + ["}"])
    after = "\n".join(["  0,"] * 19000 + ["  1,"] * 500 + ["}"])
    assert extract._too_big_for_difflib(before, after)
    t0 = time.time()
    stats = extract.line_stats(before, after)
    diff, _ = extract.unified(before, after, "r.json")
    assert time.time() - t0 < 10
    assert stats == {"added_lines": 500, "removed_lines": 1000}
    assert diff.startswith("--- a/r.json\n+++ b/r.json\n@@")


def test_git_output_with_binary_bytes_does_not_crash(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    git_out(tmp_path, "add", "x.png")
    out = extract.git(str(tmp_path), "show", ":x.png")
    assert out is not None and out.startswith("�PNG")
