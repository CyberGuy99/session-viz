# Project: session-viz

## Stack
- Python 3.10+ (stdlib; optional: jsonschema, tree-sitter, PyYAML), vanilla JS/HTML
- No package manager required; wrangler (npm) for deploy only
- Deploy target: Cloudflare Workers static assets behind Cloudflare Access (Pages is redirected to Workers by wrangler ≥ 4.148)

## Conventions
- scripts/ are CLIs with argparse; no side effects outside --out paths
- facts.json is deterministic: same input → byte-identical output (sort keys)
- Tests: pytest, fixtures under tests/fixtures/; every symbol-diff case gets a fixture
- Project files are edited with Write/Edit tools, not Bash heredocs, so this repo's own sessions replay cleanly

## Commands
- Test: `pytest -q`
- Extract: `python3 scripts/extract.py --session latest -o facts.json`
- Build: `python3 scripts/build.py facts.json narrative.json -o dist/index.html`
- Publish: `scripts/publish.sh dist <session-id>`
- Replay check (§7.1): `python3 scripts/extract.py --session latest --verify`

## Architecture notes
- extract.py owns transcript parsing + symbol diffs; build.py owns rendering; Claude owns narrative.json only
- Edit replay must seed from git before falling back to transcript Reads — see decisions.md
- After /compact, early edits exist only in git; require --since
- See architecture.md, glossary.md, decisions.md

## Do NOT
- Deploy to GitHub Pages (public URL)
- Let build.py accept narrative.json that fails schema
- Fetch anything external from index.html
- Append decisions.md entries without a date
