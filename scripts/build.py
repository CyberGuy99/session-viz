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


# ───────────────────────── incremental narratives ─────────────────────────
#
# narrative.meta.json (beside narrative.json) records what an accepted narrative covered: the last event
# and the fingerprint of every change at that time. --delta compares it with fresh facts, so updating a
# narrative means reading only what is new or changed, and writing a merge patch instead of a rewrite.

def meta_path(narrative_path) -> Path:
    return Path(narrative_path).with_suffix(".meta.json")


def _changes(facts: dict) -> dict[str, str]:
    """Entry key → fingerprint for every changed file ("path") and changed symbol ("path::qualname")."""
    out = {}
    for path, f in facts.get("files", {}).items():
        if f.get("status") in ("unchanged", None) or not f.get("fingerprint"):
            continue
        out[path] = f["fingerprint"]
        for s in f.get("symbols", []):
            if s["status"] != "unchanged" and s.get("fingerprint"):
                out[f"{path}::{s['qualname']}"] = s["fingerprint"]
    return out


def _described(narr: dict) -> set[str]:
    keys = set()
    for path, f in (narr.get("files") or {}).items():
        keys.add(path)
        keys.update(f"{path}::{q}" for q in f.get("symbols") or {})
    return keys


def make_meta(narr: dict, facts: dict) -> dict:
    seen = _changes(facts)
    return {"covered_until": max((e["ts"] for e in facts.get("events", [])), default=None),
            "entries": {k: v for k, v in seen.items() if k in _described(narr)}, "seen": seen}


def _cited_until(narr: dict, facts: dict) -> str | None:
    """Fallback baseline for narratives without meta: the newest event they cite."""
    ts = {e["uuid"]: e["ts"] for e in facts.get("events", [])}
    text = json.dumps(narr)
    refs = set(CITE_RE.findall(text)) | {l.get("event_ref") for l in narr.get("lessons") or []}
    src = (narr.get("goal") or {}).get("source", "")
    refs.add(src[9:] if src.startswith("user_msg#") else None)
    return max((ts[u] for u in refs if u in ts), default=None)


def reconstruct_meta(narr: dict, facts: dict) -> dict | None:
    """Meta for a narrative written before meta existed: re-extract the session cut at the newest event the
    narrative cites. Edited files' content comes from transcript replay, so their fingerprints are exactly
    what the narrative was written against. None if the transcript is gone or nothing is cited."""
    s = facts.get("session") or {}
    until = _cited_until(narr, facts)
    if not until or not s.get("transcript") or not Path(s["transcript"]).exists():
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    import extract  # parsing stays in extract.py
    then, _ = extract.extract(Path(s["transcript"]), s.get("repo_root"), s.get("start_sha"), upto=until)
    return {**make_meta(narr, then), "reconstructed": True}


def delta(narr: dict, facts: dict, meta: dict | None) -> dict:
    """What an existing narrative is missing relative to fresh facts. `up_to_date` when nothing is."""
    if meta:
        until = meta.get("covered_until")
        basis = "reconstructed from the transcript at the newest cited event" if meta.get("reconstructed") else "meta"
    else:
        until, basis = _cited_until(narr, facts), "newest cited event (no meta: changed code can't be detected)"
    new_events = [e for e in facts.get("events", []) if until is None or e["ts"] > until]
    now, described = _changes(facts), _described(narr)
    old = (meta or {}).get("entries", {})
    stale = sorted(k for k, fp in old.items() if k in now and now[k] != fp)
    seen = (meta or {}).get("seen")
    if seen is None:  # no meta and no transcript to rebuild it from: only missing files are knowable
        unreviewed = sorted(k for k in now if "::" not in k and k not in described)
    else:
        unreviewed = sorted(k for k, fp in now.items() if k not in described and seen.get(k) != fp
                            and not k.endswith("::<module>"))
    errs, _ = validate(narr, facts) if narr else ([], [])
    paths = {k.split("::")[0] for k in stale + unreviewed} | {e["file"] for e in new_events if e.get("file")}
    ctx = {}
    for p in sorted(paths & set(facts.get("files", {}))):
        f = facts["files"][p]
        want = {k.split("::", 1)[1] for k in stale + unreviewed if k.startswith(p + "::")}
        ctx[p] = {k: v for k, v in f.items() if k not in ("symbols", "events")}
        if f.get("symbols"):
            ctx[p]["symbols"] = [s for s in f["symbols"] if s["qualname"] in want]
            ctx[p].pop("unified_diff", None)
    return {"up_to_date": not (new_events or stale or unreviewed or errs),
            "baseline": basis, "covered_until": until, "new_events": new_events,
            "stale": stale, "unreviewed": unreviewed, "invalid": errs,
            "progress": narr.get("progress"), "files": ctx}


def merge_patch(target, patch, top: bool = True):
    """RFC 7386 merge patch (null deletes, arrays replace), plus top-level
    "lessons": {"append": [...], "remove": [event_ref, ...]} so lessons needn't be resent."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif top and k == "lessons" and isinstance(v, dict):
            drop = set(v.get("remove", []))
            kept = [l for l in out.get("lessons", []) if l.get("event_ref") not in drop]
            out["lessons"] = sorted(kept + v.get("append", []), key=lambda l: l.get("ts", ""))
        else:
            out[k] = merge_patch(out.get(k), v, False)
    return out


def prune_invalid(narr: dict, facts: dict) -> tuple[dict, list[str]]:
    """Drop the entries that fail referential checks (a file, symbol, data change or lesson), so the rest
    of a partly outdated narrative still renders. ([], dropped) is never returned for goal/progress errors:
    those can't be dropped, and the caller gets the errors back via validate()."""
    narr, dropped = json.loads(json.dumps(narr)), []
    for _ in range(50):
        serrs, _ = schema_errors(narr)
        errs = serrs or ref_errors(narr, facts)[0]
        if not errs:
            return narr, dropped
        cut = set()
        for p, _m in errs:
            if p[:1] == ["files"] and len(p) >= 2:  # a symbol / data change, else the file's symbols, else the file
                cut.add(tuple(p[:4]) if len(p) >= 4 and p[2] in ("symbols", "data_changes") else tuple(p[:3]))
            elif p[:1] == ["lessons"] and len(p) >= 2:
                cut.add(tuple(p[:2]))
            else:
                return narr, dropped  # goal/progress: not prunable
        # deepest first, and higher list indexes before lower ones
        for c in sorted(cut, key=lambda c: (len(c), [f"{x:06d}" if isinstance(x, int) else x for x in c]),
                        reverse=True):
            node = narr
            try:
                for k in c[:-1]:
                    node = node[k]
                del node[c[-1]]
            except (KeyError, IndexError, TypeError):
                continue  # already gone with its parent
            dropped.append("/".join(map(str, c)))
    return narr, dropped


# ───────────────────────── render ─────────────────────────

def embed(id_: str, obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    if len(raw.encode()) > BASE64_OVER:
        b64 = base64.b64encode(raw.encode()).decode()
        return f'<script id="{id_}" type="application/json" data-encoding="base64">{b64}</script>'
    safe = raw.replace("<", "\\u003c")  # '<' only occurs inside JSON strings; blocks </script> and <!--
    return f'<script id="{id_}" type="application/json">{safe}</script>'


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def render(facts: dict, narr: dict, back_href: str | None = None) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if "<!--DATA-->" not in tpl:
        sys.exit(f"{TEMPLATE}: missing <!--DATA--> placeholder")
    title = (narr.get("goal") or {}).get("statement") or f"Session {facts['session']['id'][:8]}"
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    back = f'<nav class="back"><a href="{_esc(back_href)}">← all sessions</a></nav>' if back_href else ""
    return tpl.replace("<!--DATA-->", embed("facts", facts) + "\n" + embed("narrative", narr)) \
              .replace("<!--TITLE-->", title[:120]).replace("<!--BACK-->", back)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("facts")
    ap.add_argument("narrative", nargs="?", help="omit or pass {} for a facts-only page")
    ap.add_argument("-o", "--out", default="dist/index.html")
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--back", help="href for a '← all sessions' link (project mode)")
    ap.add_argument("--delta", action="store_true",
                    help="print (JSON) what the narrative is missing vs these facts; write nothing")
    ap.add_argument("--apply-patch", metavar="PATCH",
                    help="merge PATCH into the narrative, validate, then write narrative + .meta.json "
                         "(nothing written on errors or with --check); renders nothing")
    a = ap.parse_args(argv)

    facts = json.loads(Path(a.facts).read_text(encoding="utf-8"))
    narr = {}
    if a.narrative and Path(a.narrative).exists():
        narr = json.loads(Path(a.narrative).read_text(encoding="utf-8") or "{}")
    if a.delta:
        mp = meta_path(a.narrative) if a.narrative else None
        meta = json.loads(mp.read_text()) if mp and mp.exists() else (reconstruct_meta(narr, facts) if narr else None)
        print(json.dumps(delta(narr, facts, meta), indent=1, ensure_ascii=False, sort_keys=True))
        return 0
    if a.apply_patch:
        if not a.narrative:
            ap.error("--apply-patch needs the narrative path to write")
        merged = merge_patch(narr, json.loads(Path(a.apply_patch).read_text(encoding="utf-8")))
        errs, warns = validate(merged, facts)
        for line in warns + errs:
            print(line, file=sys.stderr)
        if errs:
            print(f"\n{a.apply_patch}: {len(errs)} error(s) after merging; fix the patch and re-run. "
                  "Nothing written.", file=sys.stderr)
            return 2
        if not a.check:
            Path(a.narrative).write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            meta_path(a.narrative).write_text(json.dumps(make_meta(merged, facts), indent=1, sort_keys=True) + "\n")
        print(f"{a.narrative}: patched and valid ({len(warns)} warning(s))"
              + (" [--check: not written]" if a.check else ""), file=sys.stderr)
        return 0
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
    out.write_text(render(facts, narr, a.back), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
