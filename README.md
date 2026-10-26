<div align="center">

# ArtCraft Tracker

**How ready is each Craft app for real work, and how fast is it moving?**

An unofficial community dashboard for the [storytold](https://github.com/storytold) Craft apps. For each app it shows
how ready it is for real work compared with its reference product, its product parity, its development activity and
its releases. It reads the repositories every 30 minutes and can be installed as a web app.

[![Update workflow](https://github.com/KollektionHannemann/artcraft-parity/actions/workflows/update.yml/badge.svg?branch=main)](https://github.com/KollektionHannemann/artcraft-parity/actions/workflows/update.yml)
[![Live dashboard](https://img.shields.io/badge/live-GitHub%20Pages-5b8cff)](https://kollektionhannemann.github.io/artcraft-parity/)

</div>

![Overview of the ArtCraft Tracker](docs/overview.png)

## What it shows

- **Ready for real work** per app, the headline number: the project's own estimate of how much of the reference product
  a professional could replace with the app in daily work ("Full target (ready for real work)" in each `ROADMAP.md`).
- **Product parity** as a second view: the roadmaps' feature breadth, how many of the reference product's features
  exist at all, ready or not (VectorCraft: 88 % product parity, 62 % ready for real work). "Show" above the cards, the
  ranking and the detail chart switch between both; sorting works by either.
- **Latest update** on the start page: the newest change of "ready for real work" or milestone, and the newest release.
- **Activity level** per app (Buzzing, Active, Quiet, Cold), from the code changes merged in the last 14 days.
- **Cards** sorted by the latest release by default. The apps ship in suite-wide waves within minutes of each other,
  so the sort groups releases by UTC day, puts the newest wave first, and within a wave sorts by the number of changes
  in the release, then by ready for real work. Other sorts are the latest change of ready for real work, ready for
  real work, product parity, activity, suite order and name. When releases shipped after the current ready value was
  set, the card says so ("Value from 10 Oct · 9 releases since"). "What changed" on each card opens the latest value
  change, roadmap update, code change and release.
- **Detail view** per app: the four kinds of update side by side, a chart of ready for real work or product parity
  (zoomed or 0–100), daily code
  activity, one timeline for all update types with filters, releases, audiences, dimensions, features and the
  roadmap's own progress log.
- **95 % milestone:** the first time an app reports at least 95 % ready for real work, it gets a short celebration and a gold outline for
  seven days, then a permanent marker.
- **Installable web app** with offline support; offline copies are always labelled as saved data.
- **Notifications** for new apps and changes of "ready for real work", as a GitHub issue and optionally as an ntfy push.

![Detail view for FigCraft](docs/detail.png)

## Tracked apps

The Craft apps are open-source alternatives to commercial desktop software. The tracker follows each one against its
reference product.

| App | Open-source alternative to | Category |
|---|---|---|
| [PhotoCraft](https://github.com/storytold/photocraft) | Photoshop | Image |
| [VectorCraft](https://github.com/storytold/vectorcraft) | Illustrator | Vector |
| [FilmCraft](https://github.com/storytold/filmcraft) | Premiere Pro | Video |
| [LightCraft](https://github.com/storytold/lightcraft) | Lightroom | Photo |
| [PdfCraft](https://github.com/storytold/pdfcraft) | Acrobat Pro | PDF |
| [EffectCraft](https://github.com/storytold/effectcraft) | After Effects | Motion |
| [DesignCraft](https://github.com/storytold/designcraft) | InDesign | Layout |
| [FigCraft](https://github.com/storytold/figcraft) | Figma | Interface |
| [WordCraft](https://github.com/storytold/wordcraft) | Word | Text |
| [SoundCraft](https://github.com/storytold/soundcraft) | Pro Tools | Audio |
| [DeckCraft](https://github.com/storytold/deckcraft) | PowerPoint | Presentation |
| [CADCraft](https://github.com/storytold/cadcraft) | AutoCAD | CAD |
| [GridCraft](https://github.com/storytold/gridcraft) | Excel | Spreadsheet |

## Update types

The tracker keeps three kinds of update apart. One commit can belong to more than one; each is counted once per kind.

| Type | What it is | Source |
|---|---|---|
| **Ready update** | The "ready for real work" (full-target) value changed, up or down. Republishing the same value is not one. | `ROADMAP.md` on the main branch |
| **Roadmap update** | The roadmap changed without a new value. Each revision is classified as *estimates* (another number changed: audiences, hours, dimensions, features, stage), *content* (the text changed) or *editorial* (only formatting or the header date changed). Editorial revisions are hidden in the UI. | `ROADMAP.md` on the main branch |
| **Repository update** | Code changes merged into main, and published releases. | first-parent git history, GitHub Releases |

Every point keeps its commit and its commit time (`ts`, UTC). The time a run noticed it is kept separately
(`checked_at` in the change log).

Findings from the real repositories that shaped this model (October 2026):

- The repositories are 9 to 19 days old, and the roadmaps have reported the full target only since 10 Oct 2026.
  Longer windows (30/90/180 days) carry no information yet, so activity uses 7 and 14 days.
- Of about 1,900 roadmap revisions on main, 5 changed the full target. Almost all others add progress-log lines,
  which is why a roadmap change usually leaves "ready for real work" unchanged.
- `git log -- ROADMAP.md` without `--first-parent` also returns commits inside merged branches. Their values were
  never published on main, and their dates are when they were written. The tracker reads the main line only.
- About 93 % of the changesets on main touch code; there are no bot accounts. Releases are tagged across the suite
  within minutes of each other and come with notes that list every merged pull request.

## Activity

Activity measures effort, not progress. It counts **code changesets** on the main branch: pull-request merges and
direct commits, each once. Changesets that only touch documentation, the roadmap, CI files, dotfiles or lockfiles,
and version bumps such as "Release 0.9.0", don't count.

```
score = Σ over the last 14 UTC days of 0.5^(age in days / 3.5) × log2(1 + code changesets that day)
```

| Level | Rule |
|---|---|
| Buzzing | score ≥ 20 and code changes on at least 5 of the last 7 days |
| Active | score ≥ 8 and code changes on at least 2 of the last 7 days |
| Quiet | some code change in the last 14 days |
| Cold | no code change in the last 14 days |

Why this shape: the logarithm keeps one huge day from outweighing a steady week (300 merges on one day score about
as much as 3 a day for a week), the half-life of 3.5 days lets the level fall during a pause (Buzzing → Active after
about 4 idle days, Quiet after a week, Cold after two), and the day requirement stops a single burst from reaching
the top levels. A README commit after two quiet weeks stays Cold; one code fix makes the app Quiet. The ranking by
activity sorts by score, then by changesets in 14 days, then by name.

Limits: changesets are not equal in size or value, a squash merge and a 40-commit merge both count once, and the
score says nothing about quality or about parity. Thresholds were set against the real histories and are constants
at the top of `scripts/update.py`.

## Releases

Each run asks the GitHub Releases API once per repository (13 calls per run, well within the 1,000 per hour of the
workflow token). For each release the tracker keeps the version, publish time, link, platforms (from the asset names), the number of
pull requests listed in the notes and the version step from the previous stable release (major, minor, patch).
Pre-releases are shown but never used as the base of a version step. If the API is unreachable, git tags are used.
Download counts are not stored, because they change on every run.

The roadmap's reference product (`Target:`, for example "Adobe Illustrator 2026 (30.x)") is stored with each revision;
when it changes, the timeline shows it.

## How it works

1. `scripts/update.py` keeps partial clones of each repository (commits and trees only). Reading the history needs no
   API calls; file contents are fetched only for new roadmap revisions, in one batch.
2. New first-parent revisions of `ROADMAP.md` are parsed and appended to `data/history/<app>.json`, then every
   revision gets its kind from the one before it. Revisions without a full-target value are remembered in
   `data/state.json` and not parsed again.
3. The changesets of the last 180 days are classified by the files they touch and condensed into the activity level.
   Merge commits are shown with their pull-request title.
4. Releases come from the GitHub Releases API (see above); the last known list stays in `data/state.json` in case
   the API is unreachable.
5. `data/overview.json` and `data/apps/<app>.json` are written only when their content changed, so a run without
   changes makes no commit. `index.html` reads them directly.

"Last checked" in the dashboard is the newest successful run of the workflow, read from the GitHub API in the
browser. A run without changes commits nothing, so the data files can't tell. If the API is not reachable, the page
falls back to the last recorded check in `data/status.json`.

## Notifications

Every run collects events: new apps and roadmap revisions that are newer than the data already stored. All of them are
logged in `data/changelog.json`. Only new apps and revisions that change the full-target value notify. If there are
notifying events, the workflow:

1. opens an issue in this repository titled, for example, `ArtCraft Parity: 1 new app, 2 full-target changes`. The
   issue mentions your account, so GitHub sends you an email;
2. optionally sends a push notification through [ntfy.sh](https://ntfy.sh) if the repository secret `NTFY_TOPIC` is set.
   Subscribe to the same topic in the ntfy app on your phone.

Several changes detected in one run are grouped into one issue. Rebuilding the history (`--backfill`, or the one-time
rebuild after a data schema change) never notifies.

To add the ntfy push, pick a random topic name, subscribe to it in the ntfy app, and add it under
*Settings → Secrets and variables → Actions → New repository secret* with the name `NTFY_TOPIC`.

## Automatic discovery of new apps

Every run checks the public repositories of the GitHub account for new Craft apps:

- A repository is **added automatically** if it is not yet tracked, is not a fork or archived, has a `ROADMAP.md`,
  and that roadmap contains a full-target row. Repositories with a roadmap of a different kind (for example product
  goals) are skipped.
- The new app is appended to `apps.json` and tracked from then on. Its `added` date is recorded.
- For 14 days after discovery, the dashboard shows a banner on the overview page and a *New app* tag on the card.
- Repositories listed under `ignored_repos` in `apps.json` are never added. The account's `artcraft` and `craft-launcher`
  repositories are ignored by default.

Discovery and releases need the GitHub API. In GitHub Actions, the built-in token is passed automatically.

## Web app

`manifest.webmanifest`, `sw.js` and `icons/` make the site installable. The service worker loads the page and the
data from the network first and keeps the last good copy for offline use. A copy served offline is marked, and the
page then shows an offline notice with the time the data was saved. When the connection is back, the page reloads
the data. Icons and the manifest are cached first. Requests to other sites (GitHub API, fonts, analytics) are not
touched. The icons are drawn by `scripts/make_icons.py`.

## Repository structure

| Path | Content |
|---|---|
| `index.html` | The dashboard: overview, ranking, cards and detail view |
| `manifest.webmanifest`, `sw.js`, `icons/` | Web app manifest, service worker and icons |
| `apps.json` | Tracked apps (reference product, category, color, suite order) and `ignored_repos` |
| `scripts/update.py` | Discovery, roadmap history, activity, releases, data files and events |
| `scripts/roadmap_parser.py` | Parses a `ROADMAP.md` into metrics, dimensions, features and the change log |
| `scripts/notify.py` | Opens the notification issue and sends the optional ntfy message |
| `scripts/make_icons.py` | Draws the tracker mark and writes the icons |
| `tests/test_pipeline.py` | End-to-end tests against local git repositories |
| `data/overview.json` | Data for the overview page, including the latest events across all apps |
| `data/apps/<app>.json` | Current state, history, activity and releases per app |
| `data/history/<app>.json` | One entry per roadmap revision on the main line |
| `data/state.json` | Skipped revisions, the last known release lists and the data schema version |
| `data/changelog.json` | Log of new apps and roadmap revisions (last 500 events) |
| `data/status.json` | Daily heartbeat, which keeps the repository active |
| `.github/workflows/update.yml` | Check every 30 minutes, commit changed data, send notifications |
| `docs/` | Screenshots used in this README, and `social.png`, the link preview image |
| `robots.txt`, `sitemap.xml` | Hints for search engines |

## Run locally

```bash
python3 scripts/update.py
```

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000 (`fetch()` needs a web server, not `file://`). To rebuild the history from scratch,
run `python3 scripts/update.py --backfill 2000`; it sends no notifications. Set `GITHUB_TOKEN` locally to avoid the
unauthenticated API limit of 60 requests per hour.

```bash
python3 -m unittest discover -s tests -v
```

Requirements: Python 3.10 or newer and Git. No other dependencies.

## Publish with GitHub Pages

1. Push this repository to GitHub.
2. Under *Settings → Pages*, set the source to *Deploy from a branch*, branch `main`, folder `/ (root)`.
3. Under *Settings → Actions → General → Workflow permissions*, choose *Read and write permissions*, so the action can
   commit data.
4. Run *Actions → Update roadmap parity → Run workflow* once. After that, it runs on its own.

## Limitations

- All parity values are self-assessments by the projects. Activity and releases are signals from the git history,
  not measurements of features.
- The parser relies on table labels. If a roadmap renames the full-target row, the dashboard shows a notice instead of
  silently dropping the value. A renamed row in a parity revision is flagged as a possible method change.
- History starts with the first revision that reports the full target. If the history was cut off (see
  `parity.truncated`), the detail view says so; a milestone already reached before that has no date.
- Changesets are counted, not weighed. A large refactor and a one-line fix count the same.

## Disclaimer

This is an independent, unofficial community project. It is not affiliated with or endorsed by ArtCraft or
Storyteller.ai. Product names such as Photoshop, Illustrator, Premiere Pro, Lightroom, Acrobat, After Effects,
InDesign, Figma, Word, PowerPoint, Excel, AutoCAD and Pro Tools are trademarks of their respective owners. The tracker
uses its own mark and no ArtCraft logos or icons.
