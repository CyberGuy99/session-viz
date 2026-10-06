# session-viz

Claude Code skill: session transcript → single-file HTML report of what changed, why, and what was learned. See `architecture.md` for the design, `SKILL.md` for the runtime flow, `decisions.md` for the why.

## Install

```bash
ln -s ~/session-viz ~/.claude/skills/session-viz
```

Then in any repo: "visualize this session" or `/session-viz`.

## Manual run

```bash
OUT=~/.cache/session-viz/demo
python3 scripts/extract.py --session latest -o $OUT/facts.json      # add --since <sha> after /compact
# write $OUT/narrative.json (Claude does this inside the skill), or use {} for facts-only
python3 scripts/build.py $OUT/facts.json $OUT/narrative.json -o $OUT/dist/index.html
scripts/publish.sh $OUT/dist <session-id>
```

## Whole project

```bash
python3 scripts/project.py                 # sessions of the cwd's project; --project <repo|transcript dir>
# → ~/.cache/session-viz/project-<encoded-cwd>/dist/index.html (+ s/<session-id>/index.html)
scripts/publish.sh ~/.cache/session-viz/project-<encoded-cwd>/dist project-<name>
```

Every session gets a report. Narratives come from `~/.cache/session-viz/<session-id>/narrative.json` (written by the skill); sessions without one render facts-only, and the script prints the `/session-viz --session <path>` command for each. Re-runs only re-extract sessions whose transcript, narrative, git HEAD (or working tree, for sessions that end at it) or scripts changed.

## Hosting

Reports are private: Cloudflare Workers static assets behind Cloudflare Access (free tier: up to 50 users). **Not GitHub Pages**: on GitHub Pro, Pages from a private repo is still served at a public URL; access-controlled Pages needs Enterprise Cloud. (PLAN.md said Cloudflare Pages; wrangler ≥ 4.148 redirects Pages to Workers, see decisions.md.)

URLs, where `<sub>` is your account's workers.dev subdomain:
- per session: `https://<session-id>-session-viz.<sub>.workers.dev` (Worker version preview alias)
- latest (`--production`): `https://session-viz.<sub>.workers.dev`

One-time setup:

1. `npm i -g wrangler && wrangler login`
2. **workers.dev subdomain.** Dashboard → Workers & Pages. If it asks you to choose a subdomain, pick one (it's account-wide and appears in every URL). Then:
   `mkdir -p ~/.config/session-viz && echo SUBDOMAIN=<sub> >> ~/.config/session-viz/config`
3. **Create the Worker** with a placeholder page (publish.sh won't deploy anything until Access is verified, so this is done once by hand): `wrangler deploy` with `assets.directory` pointing at a one-line HTML page, `name: session-viz`, `preview_urls: true`.
4. **Zero Trust team** (first time only). Dashboard → Zero Trust. Choose a team name and the Free plan. Cloudflare may ask for a payment method even on Free.
5. **Turn on Access for both hostnames.** Workers & Pages → session-viz → Settings → Domains & Routes:
   - `workers.dev` row → enable Cloudflare Access
   - `Preview URLs` row → enable Cloudflare Access
   Each creates an Access application.
6. **Restrict the policy to you, on both applications.** Step 5 creates two Access applications with *independent* policies, and per-session reports live on the Preview URLs one. If the main URL lets you in but a report link says "That account does not have access", the preview application's policy is the one to fix. Zero Trust → Access → Applications → open each application → Policies → edit: Action **Allow**, Include → **Emails** → `rushilcd@umd.edu`. Remove any broader rule. Login methods: **One-time PIN**.
   - Observed 2026-10-06: the one-click setup's login page offers only **"Sign in with: Cloudflare"** (Cloudflare-account login + the email allow-list); there is no PIN option in that flow. Allowed emails therefore need a Cloudflare account. For email one-time PIN, use the manual application below and pick One-time PIN as its login method.
   - *Manual alternative to 5–6:* Access → Applications → Add → Self-hosted, with two public hostnames, `session-viz.<sub>.workers.dev` and `*-session-viz.<sub>.workers.dev`, and the same policy.
7. **Verify** (publish.sh does this too, and refuses to deploy otherwise). Both should print `302` and a `…cloudflareaccess.com/…` URL:
   ```bash
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://session-viz.<sub>.workers.dev
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://probe-session-viz.<sub>.workers.dev
   ```

Each `publish.sh` run uploads `dist/` (never the repo) as a new Worker version with preview alias `<session-id>`. Before uploading, it checks that both the main host and a random preview host redirect to Access. After uploading, it checks every URL it printed and exits 3 if any is public.

Alternatives: inside Cowork / claude.ai, publish `dist/index.html` as an Artifact (private until shared); or just open `dist/index.html` from disk.

## Test

```bash
pytest -q
python3 scripts/extract.py --session latest --verify   # replay fidelity vs working tree
```
