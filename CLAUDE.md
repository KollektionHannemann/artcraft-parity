# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

**ArtCraft Tracker**, an unofficial community dashboard that tracks the product parity of the ArtCraft "Craft" apps
(github.com/storytold) against their reference products (Photoshop, Illustrator, Premiere Pro, Word, Excel, AutoCAD,
Pro Tools, and others), plus their development activity and releases. Parity numbers come from the `ROADMAP.md` file of
each repository. The metric of record is **"Full target (ready for real work)"**, shown in the UI as
**"Ready for real work"** (short "Ready"). "Product parity" in the UI means the roadmaps' **feature breadth**
(`breadth_pct`), a second view the user can switch to. Never label the headline value "parity".
The site is an installable web app (manifest + service worker).

Deployed with GitHub Pages from `KollektionHannemann/artcraft-parity`. The UI and all documentation are in English.
Keep it that way.

## Layout

| Path | Purpose |
|---|---|
| `index.html` | The whole UI: HTML, CSS and JS in one file, no build step. Loads `data/overview.json`, `data/status.json` and `data/apps/<id>.json` via `fetch()`, and the latest workflow run from the GitHub API ("Last checked") |
| `manifest.webmanifest`, `sw.js`, `icons/` | Web app. `sw.js` is network first for HTML/JSON and marks offline copies with `x-tracker-cache: offline`. Bump `VERSION` in `sw.js` when the shell file list changes |
| `apps.json` | Source of truth for tracked apps, plus `ignored_repos` |
| `scripts/update.py` | Discovers new repos, reads the first-parent git history, classifies revisions, computes activity, reads releases, writes all data files |
| `scripts/roadmap_parser.py` | Parses one `ROADMAP.md` into metrics, dimensions, features, upcoming items and the change log |
| `data/` | **Generated.** `overview.json`, `apps/*.json`, `history/*.json`, `state.json`. Do not hand-edit |
| `.github/workflows/update.yml` | Runs `update.py` every 30 minutes, commits `data/` and `apps.json`, then runs `notify.py` |
| `scripts/notify.py` | Opens a notification issue (mentions `notify` from `apps.json`) and optionally sends ntfy push. Notifies only for new apps and full-target changes |
| `scripts/make_icons.py` | Draws the tracker mark (stdlib PNG writer) and writes `icons/`. Keep its geometry in sync with `<symbol id="logo">` in `index.html` |
| `tests/test_pipeline.py` | End-to-end tests: builds local git repos with fixed dates and runs `update.main()` against them |
| `docs/` | Screenshots used in the README |

## Commands

```bash
python3 scripts/update.py                  # regular run: discovery, new commits only
python3 scripts/update.py --backfill 2000  # rebuild history from scratch, no events (about 1 min, blobs fetched in batches)
python3 -m http.server 8000                # serve the dashboard; fetch() does not work from file://
python3 -m unittest discover -s tests -v   # pipeline tests (about 2 min)
python3 scripts/make_icons.py              # redraw icons after changing the mark
```

- Python 3.10+, standard library only. Do not add pip dependencies.
- Git is required. Partial clones are cached in `.cache/repos/` (git-ignored).
- Verify changes by:
  1. `python3 -m unittest discover -s tests` (pipeline, events, activity, releases, milestones, backfill).
  2. `node --check` on the extracted `<script>` block of `index.html`, and `node --check sw.js`.
  3. Loading the page in a browser against the generated data: cards, sorts, detail views, timeline filters, mobile
     width (no horizontal scroll at 390 px). The app's built-in preview browser cannot register service workers; test
     the PWA and offline mode in Chrome (stop the local server to go offline; DevTools network emulation does not
     apply to service-worker fetches).
  4. Running the parser against the current roadmaps (see "Changing the parser").
- Set `GITHUB_TOKEN` locally (`GITHUB_TOKEN=$(gh auth token)`) or discovery and releases hit the 60/h API limit.

## Data model

- `history/<app>.json`: one point per **first-parent** roadmap revision that yielded a full-target value, in main-line
  order (never sort by the `commit_date` string: offsets differ). Keys include `commit_full`, `commit_date` (original
  offset), `ts` (UTC, `Z`), `full_pct`, `full_prev`, `kind`, `full_range`, `full_hours`, `full_label`, `target`, `stage`,
  `mainstream_pct`, `essentials_pct`, `breadth_pct`, `scores_hash`, `text_hash`. `kind` is recomputed every run by
  `annotate()` from the point before: `first`, `parity`, `estimates`, `content` or `editorial`. `areas` (ready % per
  row of the Features table) is stored only on points where it changed; `annotate()` drops repeats and sets
  `area_moves` (`label`, `from`, `to`). The detail file's history leaves `areas` out.
- `state.json`: `schema` (bump `SCHEMA` in `update.py` to rebuild every history once, without events), `skipped.<app>`
  (commits without a full-target value, not parsed again), `truncated.<app>`, `releases.<app>` (last known release list; the ETag is kept in `.cache/release-etags.json`, never
  in `state.json`, because GitHub changes it with every download).
- `overview.json`:
  - `apps`: one entry per app, sorted by full target. Per app: `parity` (`tracked_since`, `changes`, `last_change`,
    `value_since` = first revision with the current full-target value, `releases_since` = non-prerelease releases after
    it, `releases_since_capped` when the count reaches the oldest of the 12 kept releases, `revisions_since` =
    estimates/content revisions after it, `hours_since` = full-target hours then and now if they differ,
    `areas_since` = net area moves after it with `up`/`down` counts, `null` if none),
    `roadmap` (`last_update`, `revisions_7d`), `activity` (`level`, `score`, `window` = 14 daily code counts ending today),
    `release`, `milestone` (`reached_at` is the commit time of the first revision at or above 95 %, `null` if the first
    known revision was already there), compact `series`.
  - `latest_events`: full-target changes (`type: "parity"`), milestones, releases and new apps, newest first.
  - `release_waves`: the last 8 UTC days with releases (`day`, `apps` = distinct apps, `changes`, `largest`), newest
    first. Pre-releases don't count. Days older than the oldest kept release of a capped app are left out.
  - `recent_additions`: apps discovered within `RECENT_DAYS` (14). Drives the banner on the overview.
  - `errors`: notices shown at the top of the overview. Discovery, git and parser problems all end up here.
- `apps/<app>.json`: detail data, including `activity.days` (daily code/other counts for 180 days with sample subjects)
  and `releases` (version, `ts`, `kind` = major/minor/patch, `changes` = PRs listed in the notes, `platforms`).
- `.cache/events.json`: events of the current run only (new apps, roadmap revisions with `kind`). Read by `notify.py`.
  Not committed.
- `data/changelog.json`: the persistent log of those events. It keeps every revision. The filter to full-target changes
  (`full_before != full_after`) happens in `notify.py` only, so do not filter events in `update.py`.
- Activity, the 14-day window and `revisions_7d` depend on the date, so `overview.json` changes once a day even
  without new commits. Do not add values that change on every run (exact ages, download counts): each would commit.
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
- Features table: the ready column is picked by header in `area_columns()` ("ready" wins, "breadth" never counts,
  else the first percentage). The headers differ per app; all 13 parse.
- Movement below the headline (areas, hours, updates since) is read from the roadmaps. Never compute a percentage of
  our own, and never notify on it.

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
- **Full target is the headline metric** ("Ready for real work" in the UI). Product parity (feature breadth) is an
  option, never the default. Events, notifications and the 95 % milestone use the full target only. The default card
  sort is the latest release (release wave by UTC day, then changes in the release, then full target), because the
  roadmap values rarely move while releases ship several times a week.
  Other numbers support it. Do not reorder the UI
  to put them first. Activity is effort, not progress: keep it visually separate (neutral cells on cards, never the
  parity bar style). Parity ranges stay in the data but are not shown; hour estimates stay ranges.
- **Branding:** the product is "ArtCraft Tracker" with its own mark (open triangle filled to a level line) and the
  "Unofficial community project" badge in the top bar. Never use the official ArtCraft logos or app icons.
- **Animations** run once per page load (count-up, bar fill, sort FLIP, milestone sparkle once per session) and
  respect `prefers-reduced-motion`. Nothing blinks. The one loop is the title ticker in an open "What changed"
  panel: too-long titles run as an endless one-way band (text plus an `aria-hidden` copy, wrapped at the seam), with
  the scrollbar hidden. They pause on hover, touch or focus, stay scrollable by hand, and stop when the panel closes.
- **No external JS libraries.** Charts are inline SVG.
- **Be careful with the GitHub API.** Avoid per-commit API calls. Use the partial git clones. The only per-repo call is
  the Releases list (13 calls per run).
- **Don't hand-edit `data/`.** The Action commits it when something changed, and once a day as heartbeat.
- Before pushing from a local clone, run `git pull --rebase`. The bot commits data between your changes.

## Gotchas

- In zsh, `#` comments pasted after a command become arguments. Use `setopt interactive_comments`, or leave them out.
- macOS `sed -i` requires an empty suffix: `sed -i '' ...`.
- The GitHub Pages site and the bot commits share the `main` branch. A new Pages deployment happens on every push.
- Google Fonts are blocked in some sandboxes. The resulting console error is harmless.
- Python 3.10 `datetime.fromisoformat` rejects a trailing `Z`. Always parse timestamps with `parse_ts()`.
- `git log -- ROADMAP.md` without `--first-parent` returns commits from merged branches with their authoring dates. They
  were never states of main; that produced false value changes before. Keep `--first-parent --diff-merges=first-parent`.
- The first run for a newly discovered app parses the full history, because it has no state yet. This is handled by
  `if history else []` in `update_history()`. Keep that logic when you refactor.
- Scheduled GitHub Actions runs can start several minutes late, and sometimes more under load.

## When you change behavior

- Update `README.md` (sections "How it works", "Automatic discovery of new apps", "Notifications", "Repository structure").
- Events must only cover revisions newer than the stored data (`event_points` in `update_history()`). Historical
  backfill must never notify. Keep `emit_events` in `main()` for `--backfill` runs.
- If you change what `overview.json` or `<app>.json` contains, update `index.html` in the same change. The UI reads these
  files directly and silently shows `–` for missing keys.
