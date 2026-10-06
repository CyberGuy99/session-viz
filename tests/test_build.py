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


def test_embed_escapes_and_base64(monkeypatch):
    obj = {"x": "</script><!-- <b>"}
    s = build.embed("facts", obj)
    body = s.split(">", 1)[1].rsplit("</script>", 1)[0]
    assert "<" not in body
    assert json.loads(body) == obj  # escaping must stay valid JSON
    monkeypatch.setattr(build, "BASE64_OVER", 10)
    assert 'data-encoding="base64"' in build.embed("facts", {"x": "y" * 50})
