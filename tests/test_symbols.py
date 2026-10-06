import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from extract import diff_symbols, symbols  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(case):
    d = FIXTURES / case
    return (d / "before.py").read_text(), (d / "after.py").read_text()


def by_name(changes):
    return {c["qualname"]: c for c in changes}


def test_symbols_shape_and_nesting():
    before, _ = load("nested_class")
    s = symbols(before)
    assert {"Parser", "Parser.__init__", "Parser.parse", "Parser.Options", "Parser.Options.describe",
            "OldName", "OldName.run", "OldName.stop"} <= set(s)
    p = s["Parser.parse"]
    assert p["kind"] == "method" and p["signature"] == "def parse(self, s)"
    assert s["Parser"]["docstring"] == "Parses things."
    assert s["Parser"]["kind"] == "class"
    assert s["Parser.parse"]["source"].startswith("    def parse")
    assert {"kind", "lineno", "end_lineno", "source", "signature", "docstring"} <= set(p)


def test_syntax_error_returns_none():
    assert symbols("def broken(:\n") is None
    assert diff_symbols("x = 1\n", "def broken(:\n") is None


def test_added():
    c = by_name(diff_symbols(*load("add")))
    assert c["total"]["status"] == "added"
    assert c["total"]["before_src"] == "" and "def total" in c["total"]["after_src"]
    assert c["parse"]["status"] == "unchanged"


def test_removed():
    c = by_name(diff_symbols(*load("remove")))
    assert c["legacy_parse"]["status"] == "removed"
    assert c["legacy_parse"]["after_src"] == ""
    assert c["parse"]["status"] == "unchanged"


def test_modified_aspects():
    c = by_name(diff_symbols(*load("modify")))
    assert c["sig_change"]["status"] == "modified" and c["sig_change"]["aspects"] == ["signature"]
    assert c["body_change"]["aspects"] == ["body"]
    assert c["doc_change"]["aspects"] == ["docstring"]
    assert c["untouched"]["status"] == "unchanged" and "before_src" not in c["untouched"]
    d = c["body_change"]["unified_diff"]
    assert d.startswith("--- a/body_change") and "+    if not s:" in d


def test_rename_detected_and_dissimilar_not_paired():
    c = by_name(diff_symbols(*load("rename")))
    r = c["read_csv_rows"]
    assert r["status"] == "renamed" and r["renamed_from"] == "load_rows"
    assert r["aspects"] == []  # only the name changed
    assert c["helper"]["status"] == "removed"
    assert c["unrelated"]["status"] == "added"
    assert "load_rows" not in c


def test_nested_class_method_change_does_not_mark_class():
    c = by_name(diff_symbols(*load("nested_class")))
    assert c["Parser.parse"]["status"] == "modified" and c["Parser.parse"]["aspects"] == ["body"]
    assert c["Parser"]["status"] == "unchanged"
    assert c["Parser.Options"]["status"] == "unchanged"
    assert c["Parser.Options.describe"]["status"] == "unchanged"


def test_renamed_class_children_follow():
    c = by_name(diff_symbols(*load("nested_class")))
    assert c["NewName"]["status"] == "renamed" and c["NewName"]["renamed_from"] == "OldName"
    assert c["NewName.run"]["status"] == "renamed" and c["NewName.run"]["aspects"] == []
    assert c["NewName.stop"]["renamed_from"] == "OldName.stop"
    assert c["NewName.stop"]["aspects"] == ["body"]
    assert not any(k.startswith("OldName") for k in c)


def test_module_and_variables():
    before = "import os\nLIMIT = 10\n\ndef f():\n    return LIMIT\n"
    after = "import os\nimport sys\nLIMIT = 20\n\ndef f():\n    return LIMIT\n"
    c = by_name(diff_symbols(before, after))
    assert c["<module>"]["status"] == "modified"
    assert c["LIMIT"]["status"] == "modified" and c["LIMIT"]["kind"] == "variable"
    assert c["f"]["status"] == "unchanged"


def test_new_and_deleted_file():
    assert all(c["status"] == "added" for c in diff_symbols(None, "def a():\n    pass\n"))
    assert all(c["status"] == "removed" for c in diff_symbols("def a():\n    pass\n", None))


@pytest.mark.parametrize("case", ["add", "remove", "modify", "rename", "nested_class"])
def test_deterministic(case):
    assert diff_symbols(*load(case)) == diff_symbols(*load(case))
