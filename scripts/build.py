#!/usr/bin/env python3
"""Validate narrative.json against the schema + facts.json, then render dist/index.html.

    build.py facts.json narrative.json -o dist/index.html
    build.py facts.json narrative.json --check          # validate only

An empty narrative ({} or missing file) renders a facts-only page. Anything else must pass
schema/narrative.schema.json and the referential checks below, or nothing is written.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "narrative.schema.json"
TEMPLATE = ROOT / "templates" / "index.html"
CITE_GROUP_RE = re.compile(r"\(ev:[A-Za-z0-9_-]+(?:, ?ev:[A-Za-z0-9_-]+)*\)")  # (ev:a) or (ev:a, ev:b)
CITE_RE = re.compile(r"ev:([A-Za-z0-9_-]+)")
ARROW_RE = re.compile(r"→|->|=>")
BASE64_OVER = 200 * 1024


# ───────────────────────── schema ─────────────────────────

def _jp(path: list) -> str:
    return "$" + "".join(f"[{json.dumps(p)}]" if isinstance(p, str) and not p.isidentifier() or
                         isinstance(p, int) else f".{p}" for p in path)


def mini_validate(inst, schema, root, path=()) -> list[tuple[list, str]]:
    """Subset of JSON Schema used by narrative.schema.json; used when jsonschema is not installed."""
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return mini_validate(inst, node, root, path)
    errs = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}
    if t and (not isinstance(inst, types[t]) or (t == "integer" and isinstance(inst, bool))):
        return [(list(path), f"expected {t}, got {type(inst).__name__}")]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append((list(path), f"{inst!r} is not one of {schema['enum']}"))
    if isinstance(inst, str):
        if len(inst) < schema.get("minLength", 0):
            errs.append((list(path), "must not be empty" if schema["minLength"] == 1 else "too short"))
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            errs.append((list(path), f"longer than {schema['maxLength']} chars"))
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append((list(path), f"does not match pattern {schema['pattern']}"))
    if isinstance(inst, int) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append((list(path), f"must be ≥ {schema['minimum']}"))
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append((list(path), f"must be ≤ {schema['maximum']}"))
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append((list(path), f"missing required property {k!r}"))
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for k, v in inst.items():
            if k in props:
                errs += mini_validate(v, props[k], root, (*path, k))
            elif extra is False:
                errs.append((list(path), f"unexpected property {k!r} (allowed: {', '.join(props)})"))
            elif isinstance(extra, dict):
                errs += mini_validate(v, extra, root, (*path, k))
    if isinstance(inst, list) and "items" in schema:
        for i, v in enumerate(inst):
            errs += mini_validate(v, schema["items"], root, (*path, i))
    return errs


def schema_errors(narr: dict) -> tuple[list[tuple[list, str]], str | None]:
    schema = json.loads(SCHEMA.read_text())
    try:
        import jsonschema
    except ImportError:
        return mini_validate(narr, schema, schema), "jsonschema not installed; used built-in subset validator"
    v = jsonschema.Draft202012Validator(schema)
    return [(list(e.absolute_path), e.message) for e in
            sorted(v.iter_errors(narr), key=lambda e: list(map(str, e.absolute_path)))], None


# ───────────────────────── referential checks ─────────────────────────

def _suggest(name: str, options) -> str:
    opts = sorted(options)
    close = difflib.get_close_matches(name, opts, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    shown = ", ".join(opts[:12]) + (" …" if len(opts) > 12 else "")
    return f"{hint} Valid: {shown or '(none)'}"


def _clip(items: list[str], n: int = 15) -> str:
    return ", ".join(items[:n]) + (f" … ({len(items) - n} more)" if len(items) > n else "")


def _parent(q: str) -> str:
    return q.rsplit(".", 1)[0] if "." in q else ""


def _sentences(s: str) -> int:
    return len([x for x in re.split(r"(?<=[.!?])\s+", s.strip()) if x])


def ref_errors(narr: dict, facts: dict) -> tuple[list[tuple[list, str]], list[tuple[list, str]]]:
    errs, warns = [], []
    events = {e["uuid"]: e for e in facts.get("events", [])}
    user_msgs = {u for u, e in events.items() if e["kind"] == "user_msg"}

    def cites(path, text):
        for u in (u for g in CITE_GROUP_RE.findall(text or "") for u in CITE_RE.findall(g)):
            if u not in events:
                errs.append((path, f"cites (ev:{u}) which is not a facts.events uuid."
                                   + _suggest(u, events)))

    src = (narr.get("goal") or {}).get("source", "")
    if src.startswith("user_msg#") and src[9:] not in user_msgs:
        errs.append((["goal", "source"], f"{src!r} is not a user_msg event." + _suggest(src[9:], user_msgs)))

    ffiles = facts.get("files", {})
    for path, f in (narr.get("files") or {}).items():
        if path not in ffiles:
            errs.append((["files", path], "file not in facts.files." + _suggest(path, ffiles)))
            continue
        fsyms = {s["qualname"]: s for s in ffiles[path].get("symbols", [])}
        changed = {q for q, s in fsyms.items() if s["status"] != "unchanged"}
        for q, s in (f.get("symbols") or {}).items():
            p = ["files", path, "symbols", q]
            if q not in changed:
                why = "is unchanged" if q in fsyms else "does not exist"
                errs.append((p, f"symbol {why} in facts.json; only changed symbols may be described."
                                + _suggest(q, changed)))
                continue
            status = fsyms[q]["status"]
            if status == "added" and s.get("was"):
                errs.append((p + ["was"], "symbol was added; 'was' must be \"\""))
            if status == "removed" and s.get("now"):
                errs.append((p + ["now"], "symbol was removed; 'now' must be \"\""))
            for k in ("was", "now"):
                if s.get(k) and _sentences(s[k]) > 2:
                    warns.append((p + [k], f"{_sentences(s[k])} sentences; keep to ≤ 2"))
            ex = s.get("example") or {}
            for k in ("before", "after"):
                if ex.get(k) and not ARROW_RE.search(ex[k]):
                    errs.append((p + ["example", k], "example must look runnable: '<call> → <result>', "
                                                     f"e.g. \"parse('1,2') → [1, 2]\"; got {ex[k]!r}"))
            if status != "added" and not ex.get("before"):
                errs.append((p + ["example", "before"], f"symbol is {status}; give the old behaviour "
                                                        "as '<call> → <result>'"))
            cites(p + ["why"], s.get("why"))
        if f.get("symbols") and not ffiles[path].get("symbol_support"):
            errs.append((["files", path, "symbols"], "file has no symbol support in facts.json; "
                                                     "use data_changes instead"))
        for i, d in enumerate(f.get("data_changes") or []):
            cites(["files", path, "data_changes", i, "why"], d.get("why"))
        # summary-only files are a deliberate choice; otherwise a described parent covers its children
        described = set(f.get("symbols") or {})
        covered = {q for q in changed if any(q.startswith(d + ".") for d in described)}
        nested = {q for q in changed if _parent(q) in fsyms and "function" in fsyms[_parent(q)]["kind"]}
        undocumented = sorted(q for q in changed - nested - covered - described if q != "<module>")
        if undocumented and described:
            warns.append((["files", path, "symbols"], f"changed symbols without narrative: {_clip(undocumented)}"))

    missing = sorted(p for p, f in ffiles.items()
                     if f.get("status") not in ("unchanged", None) and p not in (narr.get("files") or {}))
    if missing and narr.get("files") is not None:
        warns.append((["files"], f"changed files without narrative: {_clip(missing)}"))

    for i, l in enumerate(narr.get("lessons") or []):
        ref = l.get("event_ref", "")
        if ref not in events:
            errs.append((["lessons", i, "event_ref"], f"{ref!r} is not a facts.events uuid." + _suggest(ref, events)))
        elif l.get("ts") != events[ref]["ts"]:
            errs.append((["lessons", i, "ts"], f"must equal the event's ts {events[ref]['ts']!r}"))
    return errs, warns


PATTERN_HINTS = {
    "why": "must cite evidence inline as (ev:<facts.events uuid>), e.g. \"user asked for ints (ev:898c9c13-…)\"",
    "source": "must be \"user_msg#<uuid>\" of a user_msg event (see facts.session.goal_candidate.uuid)",
}


def _friendly(path: list, msg: str) -> str:
    if "does not match" in msg and path and path[-1] in PATTERN_HINTS:
        return PATTERN_HINTS[path[-1]]
    return msg


def validate(narr: dict, facts: dict) -> tuple[list[str], list[str]]:
    serrs, note = schema_errors(narr)
    serrs = [(p, _friendly(p, m)) for p, m in serrs]
    rerrs, warns = ref_errors(narr, facts) if not serrs else ([], [])
    fmt = lambda kind, items: [f"{kind:<5} {_jp(p)}: {m}" for p, m in items]
    w = fmt("WARN", warns) + ([f"NOTE  {note}"] if note else [])
    return fmt("ERROR", serrs) + fmt("ERROR", rerrs), w


# ───────────────────────── render ─────────────────────────

def embed(id_: str, obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    if len(raw.encode()) > BASE64_OVER:
        b64 = base64.b64encode(raw.encode()).decode()
        return f'<script id="{id_}" type="application/json" data-encoding="base64">{b64}</script>'
    safe = raw.replace("<", "\\u003c")  # '<' only occurs inside JSON strings; blocks </script> and <!--
    return f'<script id="{id_}" type="application/json">{safe}</script>'


def render(facts: dict, narr: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if "<!--DATA-->" not in tpl:
        sys.exit(f"{TEMPLATE}: missing <!--DATA--> placeholder")
    title = (narr.get("goal") or {}).get("statement") or f"Session {facts['session']['id'][:8]}"
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    return tpl.replace("<!--DATA-->", embed("facts", facts) + "\n" + embed("narrative", narr)) \
              .replace("<!--TITLE-->", title[:120])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("facts")
    ap.add_argument("narrative", nargs="?", help="omit or pass {} for a facts-only page")
    ap.add_argument("-o", "--out", default="dist/index.html")
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    a = ap.parse_args(argv)

    facts = json.loads(Path(a.facts).read_text(encoding="utf-8"))
    narr = {}
    if a.narrative and Path(a.narrative).exists():
        narr = json.loads(Path(a.narrative).read_text(encoding="utf-8") or "{}")
    if narr:
        errs, warns = validate(narr, facts)
        for line in warns + errs:
            print(line, file=sys.stderr)
        if errs:
            print(f"\n{a.narrative}: {len(errs)} error(s); fix them and re-run. Nothing written.", file=sys.stderr)
            return 2
        print(f"{a.narrative}: valid ({len(warns)} warning(s))", file=sys.stderr)
    else:
        print("no narrative: rendering facts-only page", file=sys.stderr)
    if a.check:
        return 0
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(facts, narr), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
