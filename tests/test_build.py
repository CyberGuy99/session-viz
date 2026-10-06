import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build  # noqa: E402

FACTS = {
    "session": {"id": "abcdef12"},
    "events": [
        {"uuid": "m1", "ts": "t1", "kind": "user_msg", "summary": "make parse return ints"},
        {"uuid": "e1", "ts": "t2", "kind": "test_run", "summary": "pytest", "exit_code": 1},
    ],
    "files": {
        "calc.py": {"status": "modified", "symbol_support": True, "symbols": [
            {"qualname": "parse", "status": "modified", "kind": "function", "lineno": 1},
            {"qualname": "total", "status": "added", "kind": "function", "lineno": 5},
            {"qualname": "keep", "status": "unchanged", "kind": "function", "lineno": 9},
        ]},
        "conf.json": {"status": "modified", "symbol_support": False, "data_changes": []},
    },
}

GOOD = {
    "goal": {"statement": "parse returns ints", "completion_indicator": "tests pass", "source": "user_msg#m1"},
    "progress": {"percent": 100, "basis": "tests pass", "done": ["parse"], "remaining": []},
    "files": {
        "calc.py": {"summary": "parser", "symbols": {
            "parse": {"was": "Returned strings.", "now": "Returns ints.",
                      "example": {"before": "parse('1') → ['1']", "after": "parse('1') → [1]"},
                      "category": "accuracy", "why": "user asked (ev:m1)"},
            "total": {"was": "", "now": "Sums.", "example": {"before": "", "after": "total('1,2') → 3"},
                      "category": "accuracy", "why": "follow-up (ev:e1)"},
        }},
        "conf.json": {"summary": "config", "data_changes": [{"what": "c=3", "why": "x (ev:e1)", "category": "accuracy"}]},
    },
    "lessons": [{"ts": "t2", "event_ref": "e1", "learned": "pytest failed → fixed"}],
}


def errors(n):
    return build.validate(n, FACTS)[0]


def test_good_passes():
    assert errors(GOOD) == []


def test_schema_error_stops_before_ref_checks():
    n = copy.deepcopy(GOOD)
    n["progress"]["percent"] = 140
    n["files"]["nope.py"] = {"summary": "x"}
    e = errors(n)
    assert len(e) == 1 and "progress.percent" in e[0]


def test_missing_citation_is_friendly():
    n = copy.deepcopy(GOOD)
    n["files"]["calc.py"]["symbols"]["parse"]["why"] = "because"
    (e,) = errors(n)
    assert "(ev:<facts.events uuid>)" in e


def test_grouped_citations():
    n = copy.deepcopy(GOOD)
    n["files"]["calc.py"]["symbols"]["parse"]["why"] = "asked, then tested (ev:m1, ev:e1)"
    assert errors(n) == []
    n["files"]["calc.py"]["symbols"]["parse"]["why"] = "asked (ev:m1, ev:nope)"
    (e,) = errors(n)
    assert "cites (ev:nope)" in e


def test_referential_errors_are_actionable():
    n = copy.deepcopy(GOOD)
    syms = n["files"]["calc.py"]["symbols"]
    syms["pars"] = syms.pop("parse")
    syms["keep"] = copy.deepcopy(syms["pars"])
    syms["total"]["was"] = "something"
    syms["total"]["why"] = "x (ev:zzz)"
    syms["total"]["example"]["after"] = "returns the sum"
    n["files"]["conf.json"]["symbols"] = {"x": copy.deepcopy(GOOD["files"]["calc.py"]["symbols"]["parse"])}
    n["files"]["calc.js"] = {"summary": "x"}
    n["lessons"][0]["ts"] = "wrong"
    n["goal"]["source"] = "user_msg#e1"
    e = "\n".join(errors(n))
    assert "Did you mean 'parse'?" in e
    assert "symbol is unchanged" in e
    assert "'was' must be \"\"" in e
    assert "cites (ev:zzz)" in e
    assert "must look runnable" in e
    assert "Did you mean 'calc.py'?" in e
    assert "must equal the event's ts 't2'" in e
    assert "is not a user_msg event" in e
    assert "no symbol support" in e


def test_mini_validator_agrees_with_jsonschema():
    schema = json.loads(build.SCHEMA.read_text())
    bad = copy.deepcopy(GOOD)
    bad["progress"]["percent"] = -1
    bad["files"]["calc.py"]["symbols"]["parse"]["category"] = "style"
    bad["extra"] = 1
    del bad["lessons"]
    mini = build.mini_validate(bad, schema, schema)
    full, _ = build.schema_errors(bad)
    assert {tuple(p) for p, _ in mini} == {tuple(p) for p, _ in full}
    assert build.mini_validate(GOOD, schema, schema) == []


def test_back_link_only_in_project_mode():
    facts = {**FACTS, "session": {"id": "abcdef12"}}
    plain, linked = build.render(facts, {}), build.render(facts, {}, "../../index.html")
    assert "<!--BACK-->" not in plain and "all sessions" not in plain
    assert '<nav class="back"><a href="../../index.html">' in linked
    assert linked.replace(linked[linked.index('<nav class="back">'):linked.index("</nav>") + 6], "") == plain


def test_embed_escapes_and_base64(monkeypatch):
    obj = {"x": "</script><!-- <b>"}
    s = build.embed("facts", obj)
    body = s.split(">", 1)[1].rsplit("</script>", 1)[0]
    assert "<" not in body
    assert json.loads(body) == obj  # escaping must stay valid JSON
    monkeypatch.setattr(build, "BASE64_OVER", 10)
    assert 'data-encoding="base64"' in build.embed("facts", {"x": "y" * 50})


# ───────────── incremental narratives ─────────────

def fp_facts():
    f = copy.deepcopy(FACTS)
    for path, entry in f["files"].items():
        entry["fingerprint"] = f"fp-{path}"
        for s in entry.get("symbols", []):
            if s["status"] != "unchanged":
                s["fingerprint"] = f"fp-{s['qualname']}"
    return f


def grown(facts):
    """The session went on: parse changed again, a new symbol and a new event appeared."""
    f = copy.deepcopy(facts)
    syms = f["files"]["calc.py"]["symbols"]
    next(s for s in syms if s["qualname"] == "parse")["fingerprint"] = "fp-parse-v2"
    syms.append({"qualname": "mean", "status": "added", "kind": "function", "lineno": 12, "fingerprint": "fp-mean"})
    f["files"]["calc.py"]["fingerprint"] = "fp-calc-v2"
    f["events"].append({"uuid": "e2", "ts": "t3", "kind": "edit", "file": "calc.py", "summary": "Edit calc.py"})
    return f


def test_delta_up_to_date_then_reports_only_what_changed():
    facts = fp_facts()
    meta = build.make_meta(GOOD, facts)
    assert build.delta(GOOD, facts, meta)["up_to_date"]
    d = build.delta(GOOD, grown(facts), meta)
    assert not d["up_to_date"] and [e["uuid"] for e in d["new_events"]] == ["e2"]
    assert d["stale"] == ["calc.py", "calc.py::parse"] and d["unreviewed"] == ["calc.py::mean"]
    assert [s["qualname"] for s in d["files"]["calc.py"]["symbols"]] == ["parse", "mean"]  # only what to read
    assert "conf.json" not in d["files"]


def test_delta_without_meta_uses_newest_citation():
    d = build.delta(GOOD, grown(fp_facts()), None)
    assert d["covered_until"] == "t2" and [e["uuid"] for e in d["new_events"]] == ["e2"]
    assert d["stale"] == [] and d["baseline"].startswith("newest cited event")


def test_merge_patch_and_lesson_append():
    patch = {"progress": {"percent": 80}, "files": {"conf.json": None, "calc.py": {"symbols": {"total": None}}},
             "lessons": {"append": [{"ts": "t1", "event_ref": "m1", "learned": "x"}], "remove": ["e1"]}}
    out = build.merge_patch(GOOD, patch)
    assert out["progress"]["percent"] == 80 and out["progress"]["basis"] == "tests pass"
    assert "conf.json" not in out["files"] and set(out["files"]["calc.py"]["symbols"]) == {"parse"}
    assert [l["event_ref"] for l in out["lessons"]] == ["m1"]
    assert GOOD["progress"]["percent"] == 100  # input untouched


def test_prune_invalid_keeps_the_rest():
    bad = copy.deepcopy(GOOD)
    bad["files"]["gone.py"] = {"summary": "x"}
    bad["files"]["calc.py"]["symbols"]["keep"] = bad["files"]["calc.py"]["symbols"]["parse"]
    bad["lessons"].append({"ts": "t9", "event_ref": "nope", "learned": "y"})
    pruned, dropped = build.prune_invalid(bad, FACTS)
    assert build.validate(pruned, FACTS)[0] == []
    assert sorted(dropped) == ["files/calc.py/symbols/keep", "files/gone.py", "lessons/1"]
    assert pruned["files"]["calc.py"]["symbols"].keys() == {"parse", "total"}


def test_apply_patch_cli_writes_narrative_and_meta(tmp_path, capsys):
    facts = fp_facts()
    fp, np_, pp = tmp_path / "facts.json", tmp_path / "narrative.json", tmp_path / "patch.json"
    fp.write_text(json.dumps(facts))
    pp.write_text(json.dumps(GOOD))  # a first narrative is just a patch onto {}
    assert build.main([str(fp), str(np_), "--apply-patch", str(pp)]) == 0
    assert json.loads(np_.read_text()) == GOOD
    assert json.loads(build.meta_path(np_).read_text())["entries"]["calc.py::parse"] == "fp-parse"

    pp.write_text(json.dumps({"files": {"calc.py": {"symbols": {"keep": GOOD["files"]["calc.py"]["symbols"]["parse"]}}}}))
    before = np_.read_text()
    assert build.main([str(fp), str(np_), "--apply-patch", str(pp)]) == 2 and np_.read_text() == before

    capsys.readouterr()
    assert build.main([str(fp), str(np_), "--delta"]) == 0
    assert json.loads(capsys.readouterr().out)["up_to_date"]
