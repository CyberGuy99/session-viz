"""Project-site customizations (site.json): extra content blocks and removed default sections.

site.json is agreed with the user in plan mode from a special-requests file (SKILL.md, project mode):

    {"home":    {"hide": ["card.cost"], "blocks": [<block>, ...]},
     "session": {"hide": ["lessons"],   "blocks": [<block>, ...]}}

A block is computed (recomputed on every build from a data source) or authored (fixed content Claude wrote):

    {"id": "best-short", "title": "5 best sessions under an hour", "kind": "table",
     "query": {"source": "sessions", "where": [["duration_s", "<", 3600]],
               "derive": {"score": "progress - 10 * errors"}, "sort": ["-score"], "limit": 5,
               "columns": ["title", "duration_s", "progress", "score"]},
     "format": {"duration_s": "duration", "progress": "percent"}}
    {"id": "hot-files", "title": "Most-edited files", "kind": "table", "position": "top",
     "query": {"source": "files", "group_by": "path",
               "agg": {"sessions": ["count"], "edits": ["sum", "edits"]},
               "sort": ["-edits"], "limit": 10}}
    {"id": "notes", "title": "Context", "kind": "markdown", "as_of": "2026-10-06", "text": "…"}
    {"id": "owners", "kind": "table", "data": {"columns": ["area", "owner"], "rows": [["extract", "me"]]}}

Resolved blocks are plain {id, title, kind, position, columns/rows | items | text | stats} that the templates
render with templates/blocks.js.
"""
from __future__ import annotations

import ast
import json
import operator
from collections import defaultdict

KINDS = {"table", "list", "stats", "markdown"}
POSITIONS = {"top", "bottom"}
AGG_OPS = {"count", "sum", "min", "max", "mean"}
WHERE_OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt,
             ">=": operator.ge, "in": lambda a, b: a in b, "contains": lambda a, b: str(b).lower() in str(a).lower()}
FORMATS = {"duration", "percent", "usd", "date", "datetime", "int", "short_id"}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

# rows each source yields, by scope; fields are what queries may reference
SOURCES = {
    "home": {
        "sessions": ["id", "title", "date", "start", "duration_s", "turns", "files_changed", "added_lines",
                     "removed_lines", "edits", "test_runs", "tests_failed", "errors", "tasks_total", "tasks_done",
                     "subagents", "cost_usd", "branch", "progress", "narrative"],
        "files": ["path", "session", "date", "status", "language", "added_lines", "removed_lines", "edits"],
        "days": ["date", "sessions", "turns", "duration_s", "cost_usd", "files_changed", "added_lines",
                 "removed_lines"],
    },
    "session": {
        "events": ["ts", "turn", "kind", "tool", "summary", "file", "exit_code", "tests_failed", "subagent"],
        "files": ["path", "status", "language", "added_lines", "removed_lines", "edits", "symbols_changed"],
        "tasks": ["subject", "status"],
        "prompts": ["ts", "text"],
    },
}

HIDEABLE = {
    "home": {
        "search": "search box", "filters": "filter buttons", "header.cost": "total cost in the header",
        "header.narratives": "'N/M with narrative' in the header", "sessions": "the session list itself",
        "card.prompt": "first prompt under each title", "card.turns": "turns chip", "card.duration": "duration chip",
        "card.files": "files ± lines chip", "card.tests": "test result chip", "card.tasks": "tasks chip",
        "card.subagents": "subagents chip", "card.branch": "branch chip", "card.cost": "cost chip",
        "card.diagnostics": "diagnostics chip", "card.narrative": "narrative status chip",
        "card.progress": "progress bar", "card.regen": "'how to generate/update' hint",
    },
    "session": {
        "back": "'← all sessions' link", "progress": "progress bar and done/remaining lists",
        "timeline": "timeline rail", "lessons": "lesson notes in the timeline", "files": "file cards",
        "diagnostics": "diagnostics section",
    },
}


def catalog() -> dict:
    """Everything a site.json may use; printed by `project.py --catalog` for plan mode."""
    return {"kinds": sorted(KINDS), "positions": sorted(POSITIONS), "agg_ops": sorted(AGG_OPS),
            "sources": SOURCES, "hideable": HIDEABLE,
            "where_ops": sorted(WHERE_OPS), "formats": sorted(FORMATS),
            "query": {"source": "<name>",
                      "derive": {"<new field>": "arithmetic on fields: + - * / ( ) numbers, e.g. "
                                                "'added_lines / (duration_s / 3600)'; missing values count as 0"},
                      "where": [["<field>", "<op>", "<value>"]],
                      "group_by": "<field>", "agg": {"<out>": ["count"] or ["sum|min|max|mean", "<field>"]},
                      "columns": ["<field or out>"], "sort": ["-<field>", "<field>"], "limit": "<int>"},
            "block": {"id": "<unique>", "title": "…", "kind": "table|list|stats|markdown", "position": "top|bottom",
                      "note": "optional caption, e.g. how 'best' is defined",
                      "query | data | text": "computed | authored", "format": {"<column>": "<format>"}}}


def _expr(src: str) -> ast.AST:
    """Parse a derive expression; only numbers, field names, + - * / and parentheses."""
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Constant,
                                 ast.USub, ast.UAdd, *_BIN)) or \
                isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError(f"not allowed in a derive expression: {type(node).__name__}")
    return tree


def _eval(node, row: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, row)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        v = row.get(node.id)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, row)
        return -v if isinstance(node.op, ast.USub) else v
    a, b = _eval(node.left, row), _eval(node.right, row)
    if isinstance(node.op, ast.Div) and b == 0:
        return None
    return _BIN[type(node.op)](a, b)


# ───────────────────────── validation ─────────────────────────

def validate_site(site: dict) -> list[str]:
    errs = []
    if not isinstance(site, dict):
        return ["site.json must be an object"]
    for k in site:
        if k not in ("home", "session", "requests"):
            errs.append(f"$.{k}: unexpected key (allowed: home, session, requests)")
    for scope in ("home", "session"):
        sec = site.get(scope) or {}
        p = f"$.{scope}"
        for k in sec:
            if k not in ("hide", "blocks"):
                errs.append(f"{p}.{k}: unexpected key (allowed: hide, blocks)")
        for h in sec.get("hide", []):
            if h not in HIDEABLE[scope]:
                errs.append(f"{p}.hide: {h!r} is not hideable. Valid: {', '.join(HIDEABLE[scope])}")
        ids = set()
        for i, b in enumerate(sec.get("blocks", [])):
            bp = f"{p}.blocks[{i}]"
            if not isinstance(b, dict):
                errs.append(f"{bp}: must be an object")
                continue
            if not b.get("id") or b["id"] in ids:
                errs.append(f"{bp}.id: required and unique")
            ids.add(b.get("id"))
            if b.get("kind") not in KINDS:
                errs.append(f"{bp}.kind: {b.get('kind')!r} not one of {sorted(KINDS)}")
            if b.get("position", "top") not in POSITIONS:
                errs.append(f"{bp}.position: not one of {sorted(POSITIONS)}")
            content = [k for k in ("query", "data", "text") if k in b]
            if len(content) != 1:
                errs.append(f"{bp}: needs exactly one of query (computed), data or text (authored)")
            elif "query" in b:
                errs += _validate_query(b["query"], scope, b.get("kind"), bp + ".query")
            elif b.get("kind") == "markdown" and "text" not in b:
                errs.append(f"{bp}: markdown blocks take 'text'")
            elif "data" in b:
                errs += _validate_data(b["data"], b.get("kind"), bp + ".data")
            for col, f in (b.get("format") or {}).items():
                if f not in FORMATS:
                    errs.append(f"{bp}.format.{col}: {f!r} not one of {sorted(FORMATS)}")
    return errs


def _validate_query(q: dict, scope: str, kind, p: str) -> list[str]:
    errs = []
    src = q.get("source")
    if src not in SOURCES[scope]:
        return [f"{p}.source: {src!r} is not a {scope} source. Valid: {', '.join(SOURCES[scope])}"]
    if kind == "markdown":
        errs.append(f"{p}: markdown blocks are authored; use table, list or stats for queries")
    fields = set(SOURCES[scope][src])
    valid = lambda: ", ".join(sorted(fields))
    for name, src_expr in (q.get("derive") or {}).items():
        try:
            unknown = sorted({n.id for n in ast.walk(_expr(str(src_expr))) if isinstance(n, ast.Name)} - fields)
            if unknown:
                errs.append(f"{p}.derive.{name}: unknown field(s) {unknown}. Valid: {valid()}")
        except (SyntaxError, ValueError) as e:
            errs.append(f"{p}.derive.{name}: {e}; use field names, numbers, + - * / and parentheses")
        fields.add(name)
    where = q.get("where") or []
    if not isinstance(where, list) or not all(isinstance(w, list) and len(w) == 3 for w in where):
        errs.append(f"{p}.where: a list of [field, op, value], e.g. [[\"duration_s\", \"<\", 3600]]")
        where = []
    for f, op, _v in where:
        if f not in fields:
            errs.append(f"{p}.where: unknown field {f!r} for {src}. Valid: {valid()}")
        if op not in WHERE_OPS:
            errs.append(f"{p}.where: op {op!r} not one of {sorted(WHERE_OPS)}")
    if q.get("group_by") is not None and q["group_by"] not in fields:
        errs.append(f"{p}.group_by: unknown field {q['group_by']!r} for {src}")
    outs = set()
    for out, spec in (q.get("agg") or {}).items():
        outs.add(out)
        if not isinstance(spec, list) or not spec or spec[0] not in AGG_OPS:
            errs.append(f"{p}.agg.{out}: must be [op] or [op, field], op in {sorted(AGG_OPS)}")
        elif spec[0] != "count" and (len(spec) < 2 or spec[1] not in fields):
            errs.append(f"{p}.agg.{out}: {spec[0]} needs a field of {src}")
    known = (({q["group_by"]} if q.get("group_by") else set()) | outs) if q.get("agg") else fields
    for c in q.get("columns", []):
        if c not in known:
            errs.append(f"{p}.columns: {c!r} isn't available here. Valid: {', '.join(sorted(known))}")
    for s in q.get("sort", []):
        if s.lstrip("-") not in known:
            errs.append(f"{p}.sort: {s!r} isn't available here. Valid: {', '.join(sorted(known))}")
    if "limit" in q and not (isinstance(q["limit"], int) and q["limit"] > 0):
        errs.append(f"{p}.limit: positive integer")
    return errs


def _validate_data(d, kind, p: str) -> list[str]:
    if kind == "table":
        ok = isinstance(d, dict) and isinstance(d.get("columns"), list) and isinstance(d.get("rows"), list) \
            and all(isinstance(r, list) and len(r) == len(d["columns"]) for r in d["rows"])
        return [] if ok else [f"{p}: table data is {{columns: [...], rows: [[...], ...]}} with equal-length rows"]
    if kind == "list":
        return [] if isinstance(d, dict) and isinstance(d.get("items"), list) else [f"{p}: list data is {{items: [...]}}"]
    if kind == "stats":
        return [] if isinstance(d, dict) and isinstance(d.get("stats"), dict) else \
            [f"{p}: stats data is {{stats: {{label: value}}}}"]
    return [f"{p}: {kind} blocks don't take data"]


# ───────────────────────── sources ─────────────────────────

def home_rows(source: str, sessions: list[dict]) -> list[dict]:
    if source == "sessions":
        return [{**{f: s.get(f) for f in SOURCES["home"]["sessions"]}, "date": (s.get("start") or "")[:10]}
                for s in sessions]
    if source == "files":
        return [{**f, "session": s["id"][:8], "date": (s.get("start") or "")[:10]}
                for s in sessions for f in s.get("files", [])]
    days = defaultdict(lambda: defaultdict(float))
    for s in sessions:
        d = days[(s.get("start") or "")[:10]]
        d["sessions"] += 1
        for f in ("turns", "duration_s", "cost_usd", "files_changed", "added_lines", "removed_lines"):
            d[f] += s.get(f) or 0
    return [{"date": k, **{f: (round(v, 2) if f == "cost_usd" else int(v)) for f, v in d.items()}}
            for k, d in sorted(days.items())]


def session_rows(source: str, facts: dict) -> list[dict]:
    if source == "events":
        return [{f: e.get(f) for f in SOURCES["session"]["events"]} for e in facts.get("events", [])]
    if source == "files":
        return [{"path": p, "status": f.get("status"), "language": f.get("language"),
                 "added_lines": f.get("added_lines", 0), "removed_lines": f.get("removed_lines", 0),
                 "edits": f.get("mutation_count", 0),
                 "symbols_changed": sum(1 for s in f.get("symbols", []) if s["status"] != "unchanged")}
                for p, f in sorted(facts.get("files", {}).items())]
    if source == "tasks":
        return [{"subject": t["subject"], "status": t["status"]} for t in facts.get("tasks", [])]
    return [{"ts": m["ts"], "text": m["text"].strip().splitlines()[0][:200] if m["text"].strip() else ""}
            for m in (facts.get("session") or {}).get("user_messages", [])]


# ───────────────────────── query ─────────────────────────

def _agg(op: str, vals: list):
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if op == "count":
        return len(vals)
    if not nums:
        return None
    r = {"sum": sum(nums), "min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums)}[op]
    return round(r, 2) if isinstance(r, float) else r


def _match(val, op: str, want) -> bool:
    try:
        return val is not None and bool(WHERE_OPS[op](val, want))
    except TypeError:  # e.g. comparing None-ish or str with int
        return False


def run_query(rows: list[dict], q: dict) -> tuple[list[str], list[dict]]:
    derive = {name: _expr(str(e)) for name, e in (q.get("derive") or {}).items()}
    if derive:
        rows = [dict(r) for r in rows]
        for r in rows:
            for name, tree in derive.items():
                v = _eval(tree, r)
                r[name] = round(v, 2) if isinstance(v, float) else v
    for f, op, v in q.get("where") or []:
        rows = [r for r in rows if _match(r.get(f), op, v)]
    if q.get("agg"):
        g = q.get("group_by")
        groups = defaultdict(list)
        for r in rows:
            groups[r.get(g) if g else None].append(r)
        out = []
        for key in sorted(groups, key=lambda k: (k is None, str(k))):
            row = {g: key} if g else {}
            for name, (op, *field) in q["agg"].items():
                row[name] = _agg(op, [r.get(field[0]) for r in groups[key]] if field else groups[key])
            out.append(row)
        rows, default_cols = out, ([g] if g else []) + list(q["agg"])
    else:
        default_cols = list(rows[0]) if rows else []
    for s in reversed(q.get("sort") or []):
        f, desc = s.lstrip("-"), s.startswith("-")
        rows = sorted(rows, key=lambda r: (r.get(f) is None, r.get(f) if r.get(f) is not None else 0), reverse=desc)
    if q.get("limit"):
        rows = rows[: q["limit"]]
    cols = q.get("columns") or default_cols
    return cols, [{c: r.get(c) for c in cols} for r in rows]


def resolve(blocks: list[dict], rows_for) -> list[dict]:
    """Blocks → render-ready dicts. rows_for(source) yields a source's rows."""
    out = []
    for b in blocks:
        r = {"id": b["id"], "title": b.get("title", ""), "kind": b["kind"], "position": b.get("position", "top"),
             "note": b.get("note", ""), "as_of": b.get("as_of"), "format": b.get("format") or {}}
        if "query" in b:
            cols, rows = run_query(rows_for(b["query"]["source"]), b["query"])
            if b["kind"] in ("table", "list"):  # a list renders each row as "a · b · c"
                r.update(columns=cols, rows=[[row[c] for c in cols] for row in rows])
            else:  # stats: first row's columns as tiles
                r["stats"] = rows[0] if rows else {}
        elif b["kind"] == "markdown":
            r["text"] = b["text"]
        else:
            r.update(b["data"])
        out.append(r)
    return out


def load(path) -> dict:
    from pathlib import Path
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
