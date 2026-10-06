"""site.json: validation, queries (where/derive/agg/sort/limit), resolution."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import blocks  # noqa: E402

SESSIONS = [
    {"id": "a" * 8, "title": "fast fix", "start": "2026-10-01T10:00:00Z", "duration_s": 1200, "progress": 100,
     "errors": 0, "cost_usd": 1.5, "files": [{"path": "x.py", "edits": 2}]},
    {"id": "b" * 8, "title": "big build", "start": "2026-10-01T12:00:00Z", "duration_s": 5000, "progress": 90,
     "errors": 3, "cost_usd": 9.0, "files": [{"path": "x.py", "edits": 5}, {"path": "y.py", "edits": 1}]},
    {"id": "c" * 8, "title": "flaky", "start": "2026-10-02T09:00:00Z", "duration_s": 2400, "progress": 80,
     "errors": 4, "cost_usd": None, "files": []},
]
rows = lambda src: blocks.home_rows(src, SESSIONS)  # noqa: E731

BEST = {"id": "best", "title": "5 best sessions under an hour", "kind": "table",
        "query": {"source": "sessions", "where": [["duration_s", "<", 3600]],
                  "derive": {"score": "progress - 10 * errors"}, "sort": ["-score"], "limit": 5,
                  "columns": ["title", "duration_s", "score"]},
        "format": {"duration_s": "duration"}}


def test_best_runs_under_an_hour():
    (b,) = blocks.resolve([BEST], rows)
    assert b["columns"] == ["title", "duration_s", "score"]
    assert b["rows"] == [["fast fix", 1200, 100], ["flaky", 2400, 40]]  # big build is over an hour
    assert b["format"] == {"duration_s": "duration"}


def test_group_agg_and_stats_and_days():
    hot = {"id": "hot", "kind": "table", "query": {"source": "files", "group_by": "path",
           "agg": {"sessions": ["count"], "edits": ["sum", "edits"]}, "sort": ["-edits"]}}
    tot = {"id": "tot", "kind": "stats", "query": {"source": "sessions",
           "agg": {"n": ["count"], "cost": ["sum", "cost_usd"], "avg": ["mean", "duration_s"]}}}
    h, t = blocks.resolve([hot, tot], rows)
    assert h["rows"] == [["x.py", 2, 7], ["y.py", 1, 1]]
    assert t["stats"] == {"n": 3, "cost": 10.5, "avg": 2866.67}  # None cost skipped, not counted as 0
    days = blocks.home_rows("days", SESSIONS)
    assert [(d["date"], d["sessions"]) for d in days] == [("2026-10-01", 2), ("2026-10-02", 1)]


def test_where_ops_and_list_kind():
    q = {"id": "l", "kind": "list", "query": {"source": "sessions", "columns": ["title"],
         "where": [["title", "contains", "BIG"], ["errors", ">=", 1], ["progress", "in", [90, 100]]]}}
    assert blocks.resolve([q], rows)[0]["rows"] == [["big build"]]


def test_derive_is_arithmetic_only():
    bad = {**BEST, "query": {**BEST["query"], "derive": {"x": "__import__('os').system('true')"}}}
    errs = blocks.validate_site({"home": {"blocks": [bad]}})
    assert errs and "not allowed" in errs[0]
    unknown = {**BEST, "query": {**BEST["query"], "derive": {"x": "progres * 2"}}}
    assert "unknown field(s) ['progres']" in blocks.validate_site({"home": {"blocks": [unknown]}})[0]
    div0 = {"id": "d", "kind": "table", "query": {"source": "sessions", "derive": {"r": "errors / 0"},
            "columns": ["r"]}}
    assert blocks.resolve([div0], rows)[0]["rows"][0] == [None]


def test_validation_messages_name_alternatives():
    site = {"home": {"hide": ["card.costs"], "blocks": [
        {"id": "a", "kind": "chart", "query": {"source": "session"}},
        {"id": "a", "kind": "table", "query": {"source": "sessions", "where": [["duration", "<", 1]]}},
        {"id": "b", "kind": "table", "data": {"columns": ["x"], "rows": [[1, 2]]}},
        {"id": "c", "kind": "table", "query": {"source": "sessions"}, "format": {"x": "money"}}]},
        "session": {"blocks": [{"id": "s", "kind": "table", "query": {"source": "sessions"}}]}}
    errs = "\n".join(blocks.validate_site(site))
    for needle in ["'card.costs' is not hideable. Valid:", "'chart' not one of", "is not a home source. Valid: sessions",
                   "blocks[1].id: required and unique", "unknown field 'duration'", "equal-length rows",
                   "'money' not one of", "'sessions' is not a session source"]:
        assert needle in errs, needle
    assert blocks.validate_site({"home": {"blocks": [BEST]}}) == []


def test_authored_blocks_pass_through():
    md = {"id": "m", "kind": "markdown", "text": "**hi**", "as_of": "2026-10-06"}
    tbl = {"id": "t", "kind": "table", "data": {"columns": ["a"], "rows": [[1]]}}
    assert blocks.validate_site({"home": {"blocks": [md, tbl]}}) == []
    m, t = blocks.resolve([md, tbl], rows)
    assert m["text"] == "**hi**" and m["as_of"] == "2026-10-06" and t["rows"] == [[1]]


def test_catalog_lists_every_source_and_hideable():
    c = blocks.catalog()
    assert set(c["sources"]["home"]) == {"sessions", "files", "days"} and "card.cost" in c["hideable"]["home"]
    assert "<" in c["where_ops"] and "duration" in c["formats"]
