#!/usr/bin/env python3
"""Extract deterministic facts from a Claude Code session transcript.

transcript.jsonl (+ subagent transcripts) + git repo  ->  facts.json

Stdlib only. Optional: PyYAML (yaml diffs), tomli (toml on <3.11), tree_sitter_languages
(symbol diffs for js/ts/go/rs). Same input -> byte-identical output.
"""
from __future__ import annotations

import argparse
import ast
import csv
import difflib
import io
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

MUTATION_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
TRACKED_TOOLS = MUTATION_TOOLS | {"Bash", "TaskCreate", "TaskUpdate", "TodoWrite", "Read"}
TEST_RE = re.compile(r"\b(pytest|npm (run )?test|cargo test|go test|python3? -m (pytest|unittest))\b(?! --version)")
TEST_FAIL_RE = re.compile(r"\b\d+ failed\b|\b\d+ errors? in [\d.]+s|^FAILED |^FAIL\b|test result: FAILED", re.M)
EXIT_RE = re.compile(r"Exit code:? (\d+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.M)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
RENAME_RATIO = 0.8
MAX_DIFF_LINES = 2000
MAX_DATA_CHANGES = 500

STRUCTURED_EXT = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml"}
TEXT_EXT = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".rst": "text"}
TS_EXT = {".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "tsx",
          ".go": "go", ".rs": "rust"}


# ───────────────────────── helpers ─────────────────────────

def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def git(repo: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return r.stdout if r.returncode == 0 else None


def unified(before: str | None, after: str | None, name: str = "") -> tuple[str, bool]:
    lines = list(difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(),
                                      f"a/{name}", f"b/{name}", lineterm="", n=3))
    truncated = len(lines) > MAX_DIFF_LINES
    return "\n".join(lines[:MAX_DIFF_LINES]), truncated


def line_stats(before: str | None, after: str | None) -> dict:
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, (before or "").splitlines(), (after or "").splitlines(), autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return {"added_lines": added, "removed_lines": removed}


def text_of(content) -> str:
    """Flatten message/tool_result content (str | list[block]) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(text_of(b.get("content")))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def first_line(s: str, n: int = 160) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ───────────────────────── python symbols ─────────────────────────

DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _start(node) -> int:
    decs = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decs])


def _signature(node) -> str:
    decs = "".join(f"@{ast.unparse(d)} " for d in getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
        return f"{decs}class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"{decs}{prefix} {node.name}({ast.unparse(node.args)}){ret}"
    if isinstance(node, ast.AnnAssign):
        return f"{ast.unparse(node.target)}: {ast.unparse(node.annotation)}"
    return ", ".join(ast.unparse(t) for t in node.targets)


def _own_body(lines: list[str], body: list[ast.stmt], end: int, skip_doc: bool,
              hide: list[ast.AST] = ()) -> str:
    """Text of a block's body with nested defs (and `hide` nodes) replaced by placeholders."""
    stmts = body[1:] if skip_doc and body and _is_doc(body[0]) else body
    if not stmts:
        return ""
    start = _start(stmts[0])
    seg = {i: lines[i - 1] for i in range(start, end + 1) if i - 1 < len(lines)}
    for st in stmts:
        if isinstance(st, DEF_NODES) or st in hide:
            for i in range(_start(st), st.end_lineno + 1):
                seg.pop(i, None)
            name = getattr(st, "name", None) or _signature(st)
            seg[_start(st)] = f"<{type(st).__name__} {name}>"
    text = textwrap.dedent("\n".join(seg[i] for i in sorted(seg)))
    return "\n".join(l.rstrip() for l in text.splitlines()).strip("\n")


def _is_doc(st) -> bool:
    return isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant) \
        and isinstance(st.value.value, str)


def symbols(src: str) -> dict[str, dict] | None:
    """qualname -> {kind, lineno, end_lineno, source, signature, docstring, body}. None on SyntaxError.

    Nested defs are keyed "Outer.inner". Module-level simple assignments are kind "variable".
    The pseudo-symbol "<module>" holds top-level code that is not a def/class/variable.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.splitlines()
    out: dict[str, dict] = {}

    def add(qual, kind, node, doc, body):
        start = _start(node)
        out[qual] = {
            "kind": kind, "lineno": start, "end_lineno": node.end_lineno,
            "source": "\n".join(lines[start - 1: node.end_lineno]),
            "signature": _signature(node), "docstring": doc, "body": body,
        }

    def walk(body, prefix, in_class):
        for node in body:
            if isinstance(node, DEF_NODES):
                qual = f"{prefix}{node.name}"
                if isinstance(node, ast.ClassDef):
                    kind = "class"
                else:
                    kind = ("async_" if isinstance(node, ast.AsyncFunctionDef) else "") + \
                           ("method" if in_class else "function")
                add(qual, kind, node, ast.get_docstring(node),
                    _own_body(lines, node.body, node.end_lineno, True))
                walk(node.body, qual + ".", isinstance(node, ast.ClassDef))

    walk(tree.body, "", False)
    variables = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, ast.AnnAssign) else []
        if targets and all(isinstance(t, ast.Name) for t in targets):
            variables.append(node)
            for t in targets:
                add(t.id, "variable", node, None, "\n".join(lines[node.lineno - 1: node.end_lineno]))
    module_body = _own_body(lines, tree.body, len(lines), True, hide=variables)
    if module_body or ast.get_docstring(tree):
        out["<module>"] = {"kind": "module", "lineno": 1, "end_lineno": len(lines),
                           "source": module_body, "signature": "", "docstring": ast.get_docstring(tree),
                           "body": module_body}
    return out


def _aspects(b: dict, a: dict, rename: tuple[str, str] | None = None) -> list[str]:
    sb, sa = b["signature"], a["signature"]
    if rename:
        old_leaf, new_leaf = rename[0].rsplit(".", 1)[-1], rename[1].rsplit(".", 1)[-1]
        sb = re.sub(rf"\b{re.escape(old_leaf)}\b", "\0", sb, count=1)
        sa = re.sub(rf"\b{re.escape(new_leaf)}\b", "\0", sa, count=1)
    out = []
    if sb != sa:
        out.append("signature")
    if b["body"] != a["body"]:
        out.append("body")
    if (b["docstring"] or "") != (a["docstring"] or ""):
        out.append("docstring")
    return out


def _sim(b: dict, a: dict) -> float:
    tb = f"{b['docstring'] or ''}\n{b['body']}"
    ta = f"{a['docstring'] or ''}\n{a['body']}"
    if not tb.strip() and not ta.strip():
        tb, ta = re.sub(r"^\S+ \w+", "", b["signature"]), re.sub(r"^\S+ \w+", "", a["signature"])
    return difflib.SequenceMatcher(None, tb, ta, autojunk=False).ratio()


def _parent(q: str) -> str:
    return q.rsplit(".", 1)[0] if "." in q else ""


def diff_symbols(before: str | None, after: str | None) -> list[dict] | None:
    """SymbolChange list, or None if either side fails to parse.

    status: added | removed | modified | renamed | unchanged
    aspects (modified/renamed): subset of signature, body, docstring
    rename = removed+added of same kind and (mapped) parent with body similarity > 0.8;
    children of a renamed class follow it by leaf name.
    """
    b = symbols(before) if before else {}
    a = symbols(after) if after else {}
    if b is None or a is None:
        return None
    changes: list[dict] = []

    def change(qual, status, sb, sa, aspects=None, renamed_from=None):
        c = {"qualname": qual, "status": status, "kind": (sa or sb)["kind"],
             "lineno": (sa or sb)["lineno"]}
        if status != "unchanged":
            bs, as_ = (sb or {}).get("source", ""), (sa or {}).get("source", "")
            c["before_src"], c["after_src"] = bs, as_
            c["unified_diff"] = unified(bs, as_, qual)[0]
            c["signature_before"] = (sb or {}).get("signature", "")
            c["signature_after"] = (sa or {}).get("signature", "")
        if aspects is not None:
            c["aspects"] = aspects
        if renamed_from:
            c["renamed_from"] = renamed_from
        changes.append(c)

    for q in sorted(set(b) & set(a)):
        asp = _aspects(b[q], a[q])
        change(q, "modified" if asp else "unchanged", b[q], a[q], asp or None)

    removed = sorted(set(b) - set(a), key=lambda q: (q.count("."), b[q]["lineno"], q))
    added = set(a) - set(b)
    renames: dict[str, str] = {}
    for r in removed:
        target_parent = renames.get(_parent(r), _parent(r))
        follow = f"{target_parent}.{r.rsplit('.', 1)[-1]}" if _parent(r) in renames else None
        if follow and follow in added and a[follow]["kind"] == b[r]["kind"]:
            best = follow
        else:
            cands = [(round(_sim(b[r], a[x]), 6), x) for x in sorted(added)
                     if a[x]["kind"] == b[r]["kind"] and _parent(x) == target_parent]
            cands = [c for c in cands if c[0] > RENAME_RATIO]
            best = max(cands, key=lambda c: (c[0], c[1]))[1] if cands else None
        if best:
            renames[r] = best
            added.discard(best)
            change(best, "renamed", b[r], a[best], _aspects(b[r], a[best], (r, best)), renamed_from=r)
        else:
            change(r, "removed", b[r], None)
    for q in sorted(added):
        change(q, "added", None, a[q])

    order = {"<module>": -1}
    return sorted(changes, key=lambda c: (order.get(c["qualname"], c["lineno"]), c["qualname"]))


# ───────────────────────── non-python diffs ─────────────────────────

def _flatten(obj, prefix="") -> dict[str, str]:
    out = {}
    if isinstance(obj, dict) and obj:
        for k in obj:
            out.update(_flatten(obj[k], f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "$"] = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return out


def _load_structured(fmt: str, text: str):
    if fmt == "json":
        return json.loads(text)
    if fmt == "yaml":
        import yaml  # optional
        return yaml.safe_load(text)
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # optional
    return tomllib.loads(text)


def diff_structured(fmt: str, before: str | None, after: str | None) -> list[dict]:
    """Key-path leaf diff. Raises on parse error / missing optional parser."""
    fb = _flatten(_load_structured(fmt, before)) if before else {}
    fa = _flatten(_load_structured(fmt, after)) if after else {}
    out = []
    for k in sorted(set(fb) | set(fa)):
        if k not in fa:
            out.append({"path": k, "op": "removed", "before": fb[k], "after": None})
        elif k not in fb:
            out.append({"path": k, "op": "added", "before": None, "after": fa[k]})
        elif fb[k] != fa[k]:
            out.append({"path": k, "op": "changed", "before": fb[k], "after": fa[k]})
    return out


def diff_csv(before: str | None, after: str | None) -> dict:
    rb = list(csv.reader(io.StringIO(before or "")))
    ra = list(csv.reader(io.StringIO(after or "")))
    hb, ha = (rb[0] if rb else []), (ra[0] if ra else [])
    bodyb, bodya = [tuple(r) for r in rb[1:]], [tuple(r) for r in ra[1:]]
    changed = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, bodyb, bodya, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            if len(changed) >= 3:
                break
            bi, aj = i1 + k, j1 + k
            changed.append({"op": tag,
                            "before": list(bodyb[bi]) if bi < i2 else None,
                            "after": list(bodya[aj]) if aj < j2 else None})
    return {
        "rows_before": len(bodyb), "rows_after": len(bodya),
        "cols_before": len(hb), "cols_after": len(ha),
        "header_before": hb, "header_after": ha,
        "columns_added": [c for c in ha if c not in hb],
        "columns_removed": [c for c in hb if c not in ha],
        "changed_rows": changed,
    }


def _sections(text: str | None, markdown: bool) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    title = "(preamble)"
    seen: dict[str, int] = {}
    fence = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if markdown and not fence and re.match(r"^#{1,6}\s", line):
            t = line.strip()
            seen[t] = seen.get(t, 0) + 1
            title = t if seen[t] == 1 else f"{t} #{seen[t]}"
        out.setdefault(title, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def diff_sections(before: str | None, after: str | None, markdown: bool) -> list[dict]:
    sb, sa = _sections(before, markdown), _sections(after, markdown)
    out = []
    for t in list(sb) + [t for t in sa if t not in sb]:
        if t not in sa:
            out.append({"section": t, "op": "removed", **line_stats(sb[t], None)})
        elif t not in sb:
            out.append({"section": t, "op": "added", **line_stats(None, sa[t])})
        elif sb[t] != sa[t]:
            out.append({"section": t, "op": "modified", **line_stats(sb[t], sa[t])})
    return out


TS_DEF_TYPES = {
    "function_declaration", "class_declaration", "method_definition", "interface_declaration",
    "type_alias_declaration", "lexical_declaration", "function_item", "struct_item", "enum_item",
    "impl_item", "trait_item", "method_declaration", "type_declaration",
}


def ts_symbols(lang: str, src: str) -> dict[str, dict]:
    """Best-effort top-level (and one nested level) symbols via tree_sitter_languages. Raises if unavailable."""
    from tree_sitter_languages import get_parser  # optional
    data = src.encode()
    tree = get_parser(lang).parse(data)
    out = {}

    def name_of(n):
        c = n.child_by_field_name("name")
        if c is None:
            for ch in n.named_children:
                if ch.type in ("variable_declarator", "type_spec"):
                    c = ch.child_by_field_name("name")
                    break
        return data[c.start_byte:c.end_byte].decode() if c is not None else None

    def walk(node, prefix, depth):
        for ch in node.named_children:
            target = ch.named_children[0] if ch.type == "export_statement" and ch.named_children else ch
            if target.type in TS_DEF_TYPES and (nm := name_of(target)):
                q = prefix + nm
                body = data[target.start_byte:target.end_byte].decode()
                out[q] = {"kind": target.type, "lineno": target.start_point[0] + 1,
                          "end_lineno": target.end_point[0] + 1, "source": body,
                          "signature": body.splitlines()[0] if body else "", "docstring": None, "body": body}
                if depth < 1:
                    inner = target.child_by_field_name("body")
                    if inner is not None:
                        walk(inner, q + ".", depth + 1)

    walk(tree.root_node, "", 0)
    return out


def diff_ts_symbols(lang: str, before: str | None, after: str | None) -> list[dict]:
    b = ts_symbols(lang, before) if before else {}
    a = ts_symbols(lang, after) if after else {}
    out = []
    for q in sorted(set(b) | set(a), key=lambda q: ((a.get(q) or b.get(q))["lineno"], q)):
        sb, sa = b.get(q), a.get(q)
        status = "added" if not sb else "removed" if not sa else \
            "unchanged" if sb["source"] == sa["source"] else "modified"
        c = {"qualname": q, "status": status, "kind": (sa or sb)["kind"], "lineno": (sa or sb)["lineno"]}
        if status != "unchanged":
            bs, as_ = (sb or {}).get("source", ""), (sa or {}).get("source", "")
            c.update(before_src=bs, after_src=as_, unified_diff=unified(bs, as_, q)[0],
                     signature_before=(sb or {}).get("signature", ""),
                     signature_after=(sa or {}).get("signature", ""))
            if status == "modified":
                c["aspects"] = ["signature"] * (sb["signature"] != sa["signature"]) + ["body"]
        out.append(c)
    return out


def analyze_file(path: str, before: str | None, after: str | None) -> dict:
    """Per-file diff payload. Chooses symbol / structured / csv / section / line diff by extension."""
    ext = Path(path).suffix.lower()
    res: dict = {"symbol_support": False}
    diff, trunc = unified(before, after, path)
    res.update(line_stats(before, after))

    def line_fallback(reason=None):
        res["unified_diff"], res["diff_truncated"] = diff, trunc
        if reason:
            res["fallback_reason"] = reason

    if ext == ".py":
        res["kind"], res["language"] = "code", "python"
        syms = diff_symbols(before, after)
        if syms is None:
            line_fallback("python parse error")
        else:
            res["symbol_support"] = True
            res["symbols"] = syms
    elif ext in TS_EXT:
        res["kind"], res["language"] = "code", TS_EXT[ext]
        try:
            res["symbols"] = diff_ts_symbols(TS_EXT[ext], before, after)
            res["symbol_support"] = True
        except Exception as e:  # optional dependency; never block
            line_fallback(f"tree-sitter unavailable: {type(e).__name__}")
    elif ext in STRUCTURED_EXT:
        res["kind"], res["language"] = "data", STRUCTURED_EXT[ext]
        try:
            ch = diff_structured(STRUCTURED_EXT[ext], before, after)
            res["data_changes"] = ch[:MAX_DATA_CHANGES]
            res["data_changes_truncated"] = len(ch) > MAX_DATA_CHANGES
        except Exception as e:
            line_fallback(f"{STRUCTURED_EXT[ext]} parse failed: {type(e).__name__}")
    elif ext == ".csv":
        res["kind"], res["language"] = "data", "csv"
        res["csv"] = diff_csv(before, after)
    elif ext in TEXT_EXT:
        res["kind"], res["language"] = "text", TEXT_EXT[ext]
        res["sections"] = diff_sections(before, after, TEXT_EXT[ext] == "markdown")
        line_fallback()
    else:
        res["kind"], res["language"] = "other", ext.lstrip(".") or "none"
        line_fallback()
    return res


# ───────────────────────── transcript ─────────────────────────

def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resolve_session(arg: str, cwd: str) -> Path:
    if arg != "latest":
        return Path(arg).expanduser()
    d = Path.home() / ".claude" / "projects" / encode_cwd(cwd)
    files = sorted(d.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name))
    files = [f for f in files if not f.name.startswith("agent-")]
    if not files:
        sys.exit(f"no session transcripts under {d}")
    return files[-1]


def load_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # partially-written last line of a live session
    return out


def find_subagents(main: Path, records: list[dict]) -> list[Path]:
    sid = next((r.get("sessionId") for r in records if r.get("sessionId")), main.stem)
    uuids = {r.get("uuid") for r in records if r.get("uuid")}
    cands = [p for p in main.parent.glob("*.jsonl") if p != main]
    cands += list((main.parent / main.stem / "subagents").glob("*.jsonl"))
    found = []
    for p in sorted(set(cands)):
        recs = load_jsonl(p)
        msgs = [r for r in recs if r.get("type") in ("user", "assistant")]
        if not msgs:
            continue
        linked = any(r.get("isSidechain") and r.get("sessionId") == sid for r in msgs) or \
            any(r.get("parentUuid") in uuids for r in msgs)
        if linked:
            found.append(p)
    return found


def is_human_prompt(r: dict) -> bool:
    if r.get("type") != "user" or r.get("isSidechain") or r.get("isMeta") or r.get("isCompactSummary"):
        return False
    origin = (r.get("origin") or {}).get("kind")
    if origin and origin != "human":
        return False
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    return bool(text_of(c).strip())


class Transcript:
    """Parsed main + subagent records with tool_use ⨝ tool_result pairing."""

    def __init__(self, main: Path, include_subagents: bool = True):
        self.main = main
        self.records = load_jsonl(main)
        self.subagent_files = find_subagents(main, self.records) if include_subagents else []
        all_recs = list(self.records)
        for p in self.subagent_files:
            all_recs += load_jsonl(p)
        self.all = [r for r in all_recs if r.get("type") in ("user", "assistant") and r.get("timestamp")]
        self.all.sort(key=lambda r: (r["timestamp"], r.get("uuid", "")))
        self.results = {}
        for r in self.all:
            c = (r.get("message") or {}).get("content")
            if r["type"] == "user" and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.results[b.get("tool_use_id")] = {
                            "record": r, "block": b, "is_error": bool(b.get("is_error")),
                            "text": text_of(b.get("content")), "tur": r.get("toolUseResult"),
                        }
        self.tool_calls = []
        for r in self.all:
            c = (r.get("message") or {}).get("content")
            if r["type"] == "assistant" and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in TRACKED_TOOLS:
                        self.tool_calls.append({"use": b, "record": r, "result": self.results.get(b.get("id"))})
        self.prompts = [r for r in self.all if is_human_prompt(r)]
        self.compacted = any(r.get("isCompactSummary") for r in self.records)

    def turn_at(self, ts: str) -> int:
        return sum(1 for p in self.prompts if p["timestamp"] <= ts)

    @property
    def session_id(self) -> str:
        return next((r.get("sessionId") for r in self.records if r.get("sessionId")), self.main.stem)

    @property
    def cwd(self) -> str | None:
        return next((r.get("cwd") for r in self.records if r.get("cwd")), None)


# ───────────────────────── replay ─────────────────────────

def read_seed(call: dict) -> str | None:
    """Full file content from a Read tool_result, or None if partial/unavailable."""
    res = call["result"]
    if not res or res["is_error"]:
        return None
    tur = res["tur"]
    if isinstance(tur, dict) and isinstance(tur.get("file"), dict):
        f = tur["file"]
        if f.get("startLine", 1) == 1 and f.get("numLines") == f.get("totalLines") and "content" in f:
            return f["content"]
        return None
    inp = call["use"].get("input", {})
    if inp.get("offset") or inp.get("limit"):
        return None
    lines = res["text"].splitlines()
    if not lines or not all(re.match(r"^\s*\d+[\t→]", l) for l in lines):
        return None
    return "\n".join(re.sub(r"^\s*\d+[\t→]", "", l, count=1) for l in lines) + "\n"


def apply_mutation(name: str, inp: dict, cur: str | None) -> str | None:
    """Apply one mutation to the running copy. Raises ValueError if it cannot apply."""
    if name == "Write":
        return inp.get("content", "")
    if name in ("Edit", "MultiEdit"):
        edits = inp.get("edits") if name == "MultiEdit" else [inp]
        text = cur if cur is not None else ""
        for e in edits:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            if old == "" and text == "":
                text = new
                continue
            if old not in text:
                raise ValueError("old_string not found in running copy")
            text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
        return text
    if name == "NotebookEdit":
        nb = json.loads(cur) if cur else {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        cells = nb.setdefault("cells", [])
        mode = inp.get("edit_mode", "replace")
        idx = next((i for i, c in enumerate(cells) if c.get("id") == inp.get("cell_id")), None)
        if idx is None and isinstance(inp.get("cell_number"), int):
            idx = inp["cell_number"]
        src = inp.get("new_source", "")
        if mode == "insert":
            pos = 0 if inp.get("cell_id") is None and idx is None else (idx + 1 if idx is not None else len(cells))
            cells.insert(pos, {"cell_type": inp.get("cell_type", "code"), "metadata": {},
                               "source": src, **({"outputs": [], "execution_count": None}
                                                 if inp.get("cell_type", "code") == "code" else {})})
        elif idx is None or idx >= len(cells):
            raise ValueError("notebook cell not found")
        elif mode == "delete":
            cells.pop(idx)
        else:
            cells[idx]["source"] = src
            if inp.get("cell_type"):
                cells[idx]["cell_type"] = inp["cell_type"]
        return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
    raise ValueError(f"unknown mutation {name}")


class FileReplay:
    """Seeds and replays one file's mutations; records how `before` was obtained."""

    def __init__(self, rel: str, abs_path: str, repo: str, start_sha: str | None):
        self.rel, self.abs, self.repo, self.start_sha = rel, abs_path, repo, start_sha
        self.calls: list[dict] = []
        self.reads: list[dict] = []
        self.issues: list[dict] = []

    def seed(self) -> tuple[str | None, str]:
        if self.start_sha:
            content = git(self.repo, "show", f"{self.start_sha}:{self.rel}")
            if content is not None:
                return content, "git"
        first = self.calls[0] if self.calls else None
        tur = (first or {}).get("result") and first["result"]["tur"]
        if isinstance(tur, dict) and tur.get("originalFile") is not None:
            return tur["originalFile"], "tool_original"
        first_ts = first["record"]["timestamp"] if first else "~"
        for r in self.reads:
            if r["record"]["timestamp"] <= first_ts and (s := read_seed(r)) is not None:
                return s, "read"
        return None, "new"

    def replay(self) -> tuple[str | None, str | None, str, list[str]]:
        before, source = self.seed()
        cur, applied = before, []
        for i, c in enumerate(self.calls):
            res, name = c["result"], c["use"]["name"]
            if res and res["is_error"]:
                continue
            tur = res["tur"] if res else None
            orig = tur.get("originalFile") if isinstance(tur, dict) else None
            if orig is not None and orig != cur:
                kind = "seed_mismatch" if i == 0 else "external_modification"
                self.issues.append({"kind": kind, "path": self.rel, "event": res["record"]["uuid"],
                                    "detail": "file on disk differed from replayed copy before this "
                                              f"{name}; resynced to tool-reported original"})
                if i == 0:
                    before, source = orig, "tool_original"
                cur = orig
            try:
                cur = apply_mutation(name, c["use"].get("input", {}), cur)
                applied.append(res["record"]["uuid"] if res else c["record"]["uuid"])
            except (ValueError, json.JSONDecodeError) as e:
                self.issues.append({"kind": "replay_failed", "path": self.rel,
                                    "event": (res or {"record": c["record"]})["record"]["uuid"],
                                    "detail": f"{name}: {e}"})
        return before, cur, source, applied


# ───────────────────────── git ─────────────────────────

def resolve_start_sha(t: Transcript, repo: str, since: str | None) -> tuple[str | None, str]:
    if since:
        sha = (git(repo, "rev-parse", since) or "").strip()
        if not sha:
            sys.exit(f"--since {since}: not a commit in {repo}")
        return sha, "--since"
    for c in t.tool_calls:
        if c["use"]["name"] == "Bash" and "rev-parse HEAD" in c["use"].get("input", {}).get("command", "") \
                and c["result"] and (m := SHA_RE.search(c["result"]["text"])):
            return m.group(0), "transcript rev-parse"
    if not t.all:
        return None, "empty transcript"
    start = parse_ts(t.all[0]["timestamp"])
    log = git(repo, "log", "--reverse", "--format=%H %aI %P", "HEAD") or ""
    head = None
    for line in log.splitlines():
        parts = line.split()
        sha, date, parents = parts[0], parts[1], parts[2:]
        if parse_ts(date) >= start:
            return (parents[0] if parents else None), "parent of first session commit"
        head = sha
    if head:
        return head, "HEAD predates session"
    return None, "no commits"


def resolve_end_ref(t: Transcript, repo: str, until: str | None) -> tuple[str | None, str]:
    """Commit holding the session's final state, or None for the working tree.

    An older session's end state is the last commit authored at/before its last record; the working
    tree would include every later session's work.
    """
    if until:
        if until == "worktree":
            return None, "--until"
        sha = (git(repo, "rev-parse", until) or "").strip()
        if not sha:
            sys.exit(f"--until {until}: not a commit in {repo}")
        return sha, "--until"
    if not t.all:
        return None, "empty transcript"
    end = parse_ts(t.all[-1]["timestamp"])
    last, later = None, False
    for line in (git(repo, "log", "--reverse", "--format=%H %aI", "HEAD") or "").splitlines():
        sha, date = line.split()[:2]
        if parse_ts(date) <= end:
            last = sha
        else:
            later = True
            break
    if later:
        return last or EMPTY_TREE, "last commit before session end (later commits exist)"
    return None, "worktree"


def git_changes(repo: str, start_sha: str | None, end_sha: str | None = None) -> dict[str, str]:
    """path -> A|M|D|R from start_sha to end_sha (default: working tree, plus untracked as A)."""
    out = {}
    base = start_sha or EMPTY_TREE
    for line in (git(repo, "diff", "--name-status", "--no-renames", base, *([end_sha] if end_sha else []))
                 or "").splitlines():
        status, _, path = line.partition("\t")
        out[path] = status[0]
    if end_sha is None:
        for path in (git(repo, "ls-files", "--others", "--exclude-standard") or "").splitlines():
            out[path] = "A"
    return out


def final_content(repo: str, rel: str, end_sha: str | None) -> str | None:
    """File content at the session's end: `end_sha:rel`, or the working tree. None if absent.
    Raises UnicodeDecodeError for binary content."""
    if end_sha is None:
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
    r = subprocess.run(["git", "-C", repo, "show", f"{end_sha}:{rel}"], capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else None  # bytes: keep \r\n as on disk


def is_ignored(repo: str, rel: str) -> bool:
    r = subprocess.run(["git", "-C", repo, "check-ignore", "-q", rel], capture_output=True)
    return r.returncode == 0


# ───────────────────────── facts ─────────────────────────

def exit_code_of(res: dict | None) -> int | None:
    if not res:
        return None
    if not res["is_error"]:
        return 0
    m = EXIT_RE.search(res["text"])
    return int(m.group(1)) if m else 1


def build_tasks(t: Transcript) -> list[dict]:
    tasks: dict[str, dict] = {}
    order: list[str] = []

    def upsert(tid, subject=None):
        if tid not in tasks:
            tasks[tid] = {"id": tid, "subject": subject or "", "status": "pending", "history": []}
            order.append(tid)
        if subject:
            tasks[tid]["subject"] = subject
        return tasks[tid]

    for c in t.tool_calls:
        name, inp, res = c["use"]["name"], c["use"].get("input", {}), c["result"]
        ts, uuid = c["record"]["timestamp"], (res or {"record": c["record"]})["record"]["uuid"]
        if res and res["is_error"]:
            continue
        if name == "TaskCreate":
            tur = (res or {}).get("tur") or {}
            tid = str((tur.get("task") or {}).get("id") or tur.get("id") or
                      (re.search(r"#(\d+)", (res or {}).get("text", "")) or [None, None])[1] or
                      f"t{len(order) + 1}")
            task = upsert(tid, inp.get("subject"))
            task["history"].append({"ts": ts, "status": "pending", "uuid": uuid})
        elif name == "TaskUpdate":
            tid = str(inp.get("taskId") or inp.get("id") or "")
            if not tid:
                continue
            task = upsert(tid, inp.get("subject"))
            if inp.get("status") and inp["status"] != task["status"]:
                task["status"] = inp["status"]
                task["history"].append({"ts": ts, "status": inp["status"], "uuid": uuid})
        elif name == "TodoWrite":
            for td in inp.get("todos", []):
                key = td.get("content", "")
                tid = next((k for k in order if tasks[k]["subject"] == key and k.startswith("todo")), None) \
                    or f"todo{len([k for k in order if k.startswith('todo')]) + 1}"
                task = upsert(tid, key)
                st = td.get("status", "pending")
                if not task["history"] or st != task["status"]:
                    task["status"] = st
                    task["history"].append({"ts": ts, "status": st, "uuid": uuid})
    return [tasks[k] for k in order]


def extract(session: Path, repo: str | None, since: str | None, until: str | None = None) -> tuple[dict, dict]:
    t = Transcript(session)
    repo = repo or t.cwd or os.getcwd()
    top = (git(repo, "rev-parse", "--show-toplevel") or "").strip()
    repo_root = os.path.realpath(top or repo)
    start_sha, sha_basis = resolve_start_sha(t, repo_root, since) if top else (None, "not a git repo")
    end_sha, end_basis = resolve_end_ref(t, repo_root, until) if top else (None, "not a git repo")
    discrepancies: list[dict] = []
    if t.compacted and not since:
        discrepancies.append({"kind": "compacted_without_since", "path": None, "event": None,
                              "detail": "transcript was /compact-ed; edits before compaction are "
                                        "missing unless --since <sha> is given"})

    # events + per-file grouping
    events: list[dict] = []
    replays: dict[str, FileReplay] = {}
    outside, ignored = set(), set()

    def rel_of(p: str) -> str | None:
        ap = os.path.realpath(os.path.join(t.cwd or repo_root, os.path.expanduser(p)))
        if ap == repo_root or not ap.startswith(repo_root + os.sep):
            outside.add(ap)
            return None
        return os.path.relpath(ap, repo_root)

    def replay_for(rel: str) -> FileReplay:
        if rel not in replays:
            replays[rel] = FileReplay(rel, os.path.join(repo_root, rel), repo_root, start_sha)
        return replays[rel]

    for p in t.prompts:
        text = text_of(p["message"].get("content"))
        events.append({"uuid": p["uuid"], "ts": p["timestamp"], "turn": t.turn_at(p["timestamp"]),
                       "kind": "user_msg", "summary": first_line(text)})

    for c in t.tool_calls:
        name, inp, res = c["use"]["name"], c["use"].get("input", {}), c["result"]
        ts = c["record"]["timestamp"]
        uuid = (res or {"record": c["record"]})["record"]["uuid"]
        ev = {"uuid": uuid, "ts": ts, "turn": t.turn_at(ts), "tool": name,
              "subagent": bool(c["record"].get("isSidechain"))}
        if name == "Read":
            rel = inp.get("file_path") and rel_of(inp["file_path"])
            if rel:
                replay_for(rel).reads.append(c)
            continue
        if name in MUTATION_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            rel = rel_of(path) if path else None
            if rel and is_ignored(repo_root, rel):
                ignored.add(rel)
                rel = None
            if rel:
                replay_for(rel).calls.append(c)
            shown = rel or path
            if name in ("Edit", "MultiEdit"):
                edits = inp.get("edits") if name == "MultiEdit" else [inp]
                rm = sum(len((e.get("old_string") or "").splitlines()) for e in edits)
                ad = sum(len((e.get("new_string") or "").splitlines()) for e in edits)
                summary = f"{name} {shown} (−{rm} +{ad})"
            elif name == "Write":
                summary = f"Write {shown} ({len(inp.get('content', '').splitlines())} lines)"
            else:
                summary = f"NotebookEdit {shown}"
            ev.update(kind="edit", file=rel, summary=summary)
            if res and res["is_error"]:
                ev.update(kind="error", summary=f"{name} failed on {shown}: {first_line(res['text'], 120)}")
        elif name == "Bash":
            cmd = inp.get("command", "")
            ev["summary"] = first_line(inp.get("description") or cmd, 160)
            ev["command"] = cmd if len(cmd) <= 500 else cmd[:499] + "…"
            ev["exit_code"] = exit_code_of(res)
            if TEST_RE.search(cmd):
                ev["kind"] = "test_run"
                ev["output_tail"] = "\n".join((res or {}).get("text", "").splitlines()[-15:])
                # pipes (`| tail`) hide the exit code, so also read the runner's summary line
                ev["tests_failed"] = bool(ev["exit_code"]) or bool(TEST_FAIL_RE.search((res or {}).get("text", "")))
            elif res and res["is_error"]:
                ev["kind"] = "error"
                ev["output_tail"] = "\n".join(res["text"].splitlines()[-15:])
            else:
                ev["kind"] = "bash"
        else:  # task tools: represented in facts.tasks, not the timeline
            continue
        events.append(ev)
    events.sort(key=lambda e: (e["ts"], e["uuid"]))

    # files
    git_files = git_changes(repo_root, start_sha, end_sha) if top else {}
    files: dict[str, dict] = {}
    verify: list[dict] = []
    edit_events: dict[str, list[str]] = {}
    for e in events:
        if e.get("file"):
            edit_events.setdefault(e["file"], []).append(e["uuid"])

    for rel in sorted(set(replays) | set(git_files)):
        rp = replays.get(rel)
        if rel not in git_files and not (rp and rp.calls):
            continue  # only ever Read: not a change
        try:
            disk = final_content(repo_root, rel, end_sha)
        except (UnicodeDecodeError, IsADirectoryError):
            files[rel] = {"path": rel, "binary": True, "git_status": git_files.get(rel),
                          "status": {"A": "added", "D": "deleted"}.get(git_files.get(rel), "modified"),
                          "events": edit_events.get(rel, [])}
            continue
        if rp and rp.calls:
            before, after, seed_source, applied = rp.replay()
            discrepancies.extend(rp.issues)
            replayed = True
            match = after == disk
            verify.append({"path": rel, "match": match})
            if not match:
                where = f"commit {end_sha[:8]}" if end_sha else "the working tree"
                discrepancies.append({"kind": "replay_mismatch", "path": rel, "event": None,
                                      "detail": f"replayed content differs from {where} "
                                                "(edited outside Write/Edit tools, or after the session)"})
        else:
            before = git(repo_root, "show", f"{start_sha}:{rel}") if start_sha else None
            after, seed_source, applied, replayed = disk, "git" if before is not None else "new", [], False
        if rel not in git_files:
            discrepancies.append({"kind": "transcript_only", "path": rel, "event": None,
                                  "detail": "edited in the transcript but unchanged in git"})
        elif not (rp and rp.calls):
            discrepancies.append({"kind": "git_only", "path": rel, "event": None,
                                  "detail": f"git status {git_files[rel]} but no Write/Edit tool call "
                                            "(changed via Bash or outside the session)"})
        status = "added" if before is None and after is not None else \
            "deleted" if after is None else "unchanged" if before == after else "modified"
        entry = {"path": rel, "status": status, "git_status": git_files.get(rel), "seed_source": seed_source,
                 "replayed": replayed, "mutation_count": len(applied), "events": edit_events.get(rel, [])}
        entry.update(analyze_file(rel, before, after))
        files[rel] = entry

    ts_all = [r["timestamp"] for r in t.all]
    prompts = [{"uuid": p["uuid"], "ts": p["timestamp"], "text": text_of(p["message"].get("content"))}
               for p in t.prompts]
    session = {
        "id": t.session_id, "transcript": str(session), "cwd": t.cwd, "repo_root": repo_root,
        "subagent_transcripts": [str(p) for p in t.subagent_files],
        "start": ts_all[0] if ts_all else None, "end": ts_all[-1] if ts_all else None,
        "duration_s": int((parse_ts(ts_all[-1]) - parse_ts(ts_all[0])).total_seconds()) if ts_all else 0,
        "turns": len(t.prompts), "goal_candidate": prompts[0] if prompts else None,
        "user_messages": prompts, "start_sha": start_sha, "start_sha_basis": sha_basis,
        "end_ref": end_sha or "worktree", "end_ref_basis": end_basis,
        "compacted": t.compacted, "outside_repo_files": sorted(outside), "ignored_files": sorted(ignored),
        "git_branch": next((r.get("gitBranch") for r in t.records if r.get("gitBranch")), None),
    }
    facts = {"schema_version": 1, "session": session, "files": files, "tasks": build_tasks(t),
             "events": events,
             "discrepancies": sorted(discrepancies, key=lambda d: (d["path"] or "", d["kind"], d["event"] or ""))}
    return facts, {"verify": verify}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", default="latest", help="path to .jsonl, or 'latest' for this cwd")
    ap.add_argument("--since", help="commit to treat as the session's starting state (required after /compact)")
    ap.add_argument("--until", help="commit holding the session's final state, or 'worktree' "
                                    "(default: last commit before session end if later commits exist, "
                                    "else worktree)")
    ap.add_argument("--repo", help="git repo root (default: transcript cwd)")
    ap.add_argument("-o", "--out", help="write facts.json here (default: stdout)")
    ap.add_argument("--verify", action="store_true",
                    help="report replay fidelity vs the end state (--until); "
                         "exit 1 if < 95%% of replayed files match")
    ap.add_argument("--print-path", action="store_true", help="print the resolved transcript path and exit")
    a = ap.parse_args(argv)

    session = resolve_session(a.session, a.repo or os.getcwd())
    if a.print_path:
        print(session)
        return 0
    facts, meta = extract(session, a.repo, a.since, a.until)
    blob = json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(blob, encoding="utf-8")
    elif not a.verify:
        sys.stdout.write(blob)

    if a.verify:
        v = meta["verify"]
        ok = sum(x["match"] for x in v)
        for x in v:
            print(f"{'ok  ' if x['match'] else 'DIFF'} {x['path']}", file=sys.stderr)
        pct = 100.0 * ok / len(v) if v else 100.0
        print(f"replay fidelity: {ok}/{len(v)} = {pct:.1f}%  "
              f"(discrepancies: {len(facts['discrepancies'])})", file=sys.stderr)
        return 0 if pct >= 95.0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
