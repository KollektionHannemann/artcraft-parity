# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A static dashboard that tracks the product parity of the ArtCraft "Craft" apps (github.com/storytold) against their
reference products (Photoshop, Illustrator, Premiere Pro, Word, Excel, AutoCAD, Pro Tools, and others). All numbers come
from the `ROADMAP.md` file of each repository. The metric of record is **"Full target (ready for real work)"**.

Deployed with GitHub Pages from `KollektionHannemann/artcraft-parity`. The UI and all documentation are in English.
Keep it that way.

## Layout

| Path | Purpose |
|---|---|
| `index.html` | The whole UI: HTML, CSS and JS in one file, no build step. Loads `data/overview.json` and `data/apps/<id>.json` via `fetch()` |
| `apps.json` | Source of truth for tracked apps, plus `ignored_repos` |
| `scripts/update.py` | Discovers new repos, reads git history, writes all data files |
| `scripts/roadmap_parser.py` | Parses one `ROADMAP.md` into metrics, dimensions, features, upcoming items and the change log |
| `data/` | **Generated.** `overview.json`, `apps/*.json`, `history/*.json`, `state.json`. Do not hand-edit |
| `.github/workflows/update.yml` | Runs `update.py` every 30 minutes, commits `data/` and `apps.json`, then runs `notify.py` |
| `scripts/notify.py` | Opens a notification issue (mentions `notify` from `apps.json`) and optionally sends ntfy push. Notifies only for new apps and full-target changes |
| `docs/` | Screenshots used in the README |

## Commands

```bash
python3 scripts/update.py                  # regular run: discovery, new commits only
python3 scripts/update.py --backfill 500   # rebuild history from scratch (slow: fetches blobs)
python3 -m http.server 8000                # serve the dashboard; fetch() does not work from file://
```

- Python 3.10+, standard library only. Do not add pip dependencies.
- Git is required. Partial clones are cached in `.cache/repos/` (git-ignored).
- There is no test suite. Verify changes by:
  1. `node --check` on the extracted `<script>` block of `index.html`.
  2. Loading the page in jsdom against the generated data and checking that cards, detail views and filters render
     without errors. Playwright with Chromium also works for screenshots.
  3. Running the parser against the current roadmaps (see "Changing the parser").

## Data model

- `history/<app>.json`: one point per roadmap revision that yielded a full-target value. Keys include `commit_full`,
  `commit_date`, `full_pct`, `full_range`, `full_hours`, `stage`, `mainstream_pct`, `essentials_pct`, `breadth_pct`.
- `state.json` → `skipped.<app>`: commits that produced no full-target value. They are not parsed again. Removing an entry
  forces a re-parse of that commit on the next run.
- `overview.json`:
  - `apps`: one entry per app, sorted by full target. `changed_in_run` is true only if the newest commit was new in this run.
  - `recent_additions`: apps discovered within `RECENT_DAYS` (14). Drives the banner on the overview.
  - `errors`: notices shown at the top of the overview. Discovery, git and parser problems all end up here.
- `.cache/events.json`: events of the current run only (new apps, roadmap revisions). Read by `notify.py`. Not committed.
- `data/changelog.json`: the persistent log of those events. It keeps every revision. The filter to full-target changes
  (`full_before != full_after`) happens in `notify.py` only, so do not filter events in `update.py`.
- `data/status.json`: a heartbeat written once per day, so the repository stays active. GitHub pauses scheduled workflows in
  public repositories after 60 days without activity.
- Timestamps (`generated_at`, `updated_at`) change only when the content of a file changes. Unchanged runs produce no commit.

## Changing the parser

- Never match by line number. Roadmap files differ in layout between apps. Match by table label through
  `classify_label()`.
- Label rules, in this order:
  - rows with "remaining", "to beta" or "parity" are ignored (they are hour estimates);
  - "feature breadth" → `breadth`; "mainstream" → `mainstream`; "essentials" → `essentials`;
  - other "feature"/"dimension" rows are ignored;
  - "full" → `full`; "ready for real work" → `ready` (fallback only).
- The full-target value is the first percentage in the row. The hours are the first numeric range after it, and
  the parser prefers rows that have hours.
- After any parser change, run it against all tracked roadmaps and check that the values still match the previous run.
  Expected full-target values at the time of writing: VectorCraft 62, LightCraft 61, WordCraft 60, FilmCraft 56,
  DeckCraft 55, FigCraft 53, PdfCraft 50, GridCraft 50, DesignCraft 47, PhotoCraft 45, EffectCraft 45, SoundCraft 40,
  CADCraft 38.
- Markdown links are reduced to their text (`clean()`). Keep it that way, or labels show raw `[text](file.md)` syntax.

## Discovery of new apps

- `discover_new_apps()` lists public, non-fork, non-archived repos of the account and adds a repo to `apps.json` only if
  it has a `ROADMAP.md` **and** that roadmap contains a full-target row.
- `ignored_repos` in `apps.json` is checked first. `artcraft` (the AI studio, whose roadmap is product goals) and
  `craft-launcher` are ignored. Do not remove them.
- Repository listing uses the API (rate-limited, 60/h unauthenticated, 1000/h with `GITHUB_TOKEN`). The per-repo
  `ROADMAP.md` check uses `raw.githubusercontent.com`, which is not subject to the API limit.
- A discovered app gets `added` (ISO date), a palette color and the next position in the suite order.

## Conventions

- **Language:** English for everything the user sees and for the docs. Commit messages in English.
- **Visual style** follows the ArtCraft launcher and website: black background, 1 px hairlines, mono uppercase labels,
  white primary buttons, numbered chips, two-tone app names ("Photo" in white, "Craft" in the app color).
  Do not use ArtCraft logos or the engraved app icons.
- **Full target is the headline metric.** Other numbers support it. Do not reorder the UI to put them first.
- **No external JS libraries.** Charts are inline SVG.
- **Be careful with the GitHub API.** Avoid per-commit API calls. Use the partial git clones.
- **Don't hand-edit `data/`.** The Action commits it when something changed, and once a day as heartbeat.
- Before pushing from a local clone, run `git pull --rebase`. The bot commits data between your changes.

## Gotchas

- In zsh, `#` comments pasted after a command become arguments. Use `setopt interactive_comments`, or leave them out.
- macOS `sed -i` requires an empty suffix: `sed -i '' ...`.
- The GitHub Pages site and the bot commits share the `main` branch. A new Pages deployment happens on every push.
- Google Fonts are blocked in some sandboxes. The resulting console error is harmless.
- The first run for a newly discovered app parses the full history, because it has no state yet. This is handled by
  `if history else []` in `update_history()`. Keep that logic when you refactor.
- Scheduled GitHub Actions runs can start several minutes late, and sometimes more under load.

## When you change behavior

- Update `README.md` (sections "How it works", "Automatic discovery of new apps", "Notifications", "Repository structure").
- Events must only cover revisions newer than the stored data (`event_points` in `update_history()`). Historical
  backfill must never notify. Keep `emit_events` in `main()` for `--backfill` runs.
- If you change what `overview.json` or `<app>.json` contains, update `index.html` in the same change. The UI reads these
  files directly and silently shows `–` for missing keys.
