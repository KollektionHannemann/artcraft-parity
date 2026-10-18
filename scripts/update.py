#!/usr/bin/env python3
"""Checks the Craft app repositories for changes and updates the data files.

Each run:
  1. Discovery: lists the public repositories of the GitHub account. Repositories that are not yet tracked,
     have a ROADMAP.md and whose roadmap contains a full-target row are added to apps.json.
  2. Roadmap history: partial clones (commits and trees only) are cached under .cache/repos/. Every unknown
     revision of ROADMAP.md on the main line (first parent) is parsed and appended to data/history/<app>.json.
     Each revision gets a kind: parity, estimates, content, editorial or first.
  3. Repository activity: the first-parent changesets of the last ACTIVITY_HISTORY_DAYS days are classified
     by the files they touch (code changes vs. housekeeping) and condensed into an activity level.
  4. Releases: published GitHub Releases (version, date, number of listed changes, platforms), one API call
     per repository and run. Git tags are the fallback.
  5. Output: data/overview.json and data/apps/<app>.json. Files are only written when their content changed,
     so unchanged runs produce no commits.
  6. Events: new apps and new roadmap revisions are written to .cache/events.json (read by scripts/notify.py)
     and appended to data/changelog.json.

Usage:
  python3 scripts/update.py                  # regular run
  python3 scripts/update.py --backfill 2000  # rebuild the roadmap history from scratch (no notifications)

Set GITHUB_TOKEN to raise the GitHub API rate limit (GitHub Actions provides it automatically).
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from roadmap_parser import clean, parse_roadmap  # noqa: E402

APPS_FILE = ROOT / "apps.json"
CONFIG = json.loads(APPS_FILE.read_text(encoding="utf-8"))
ORG, BRANCH, PATH = CONFIG["org"], CONFIG["branch"], CONFIG["roadmap_path"]
IGNORED = set(CONFIG.get("ignored_repos", []))
DATA = ROOT / "data"
HISTORY = DATA / "history"
APPS_DIR = DATA / "apps"
STATE = DATA / "state.json"
STATUS = DATA / "status.json"
CHANGELOG = DATA / "changelog.json"
CACHE = ROOT / ".cache"
EVENTS_FILE = CACHE / "events.json"
REPOS_CACHE = CACHE / "repos"
ETAGS_FILE = CACHE / "release-etags.json"  # not committed: GitHub's ETag changes with download counts
GIT_BASE = os.environ.get("TRACKER_GIT_BASE", "https://github.com")
TRACKER_REPO = os.environ.get("GITHUB_REPOSITORY", "KollektionHannemann/artcraft-parity")

SCHEMA = 2            # bump to rebuild all histories once (without notifications)
DEFAULT_LIMIT = 1000  # roadmap revisions listed per app and run
RECENT_DAYS = 14      # how long a newly discovered app is highlighted on the dashboard
RECHECK_DAYS = 1      # how often a repository without a Craft roadmap is checked again
MILESTONE_PCT = 95.0

# Activity model (see README, "Activity")
ACTIVITY_WINDOW_DAYS = 14      # days that count for the level
ACTIVITY_HALF_LIFE_DAYS = 3.5  # a day's weight halves every 3.5 days
ACTIVITY_HISTORY_DAYS = 180    # daily counts kept for the detail view
ACTIVITY_LEVELS = (            # (level, minimum score, minimum active days in the last 7 days)
    ("buzzing", 20.0, 5),
    ("active", 8.0, 2),
)
SAMPLES_PER_DAY = 3
RECENT_EVENTS = 40

PALETTE = ["#3d6cf0", "#ff6a4d", "#8f5cf5", "#f5a524", "#1ab394", "#e8448a", "#8bc53f",
           "#2bc48a", "#3f63e8", "#1cb8c9", "#ff7a1a", "#20b2c8", "#22a85a"]
VENDORS = {"adobe", "microsoft", "autodesk", "avid"}


# ---------- Time ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso():
    return now_utc().isoformat()


def today_utc() -> date:
    return now_utc().date()


def parse_ts(s: str) -> datetime:
    """Git and GitHub timestamps, with offset or 'Z', as aware UTC datetimes (Python 3.10 safe)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def utc_z(s: str) -> str:
    return parse_ts(s).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_recent(added) -> bool:
    if not added:
        return False
    try:
        return (today_utc() - date.fromisoformat(added)).days <= RECENT_DAYS
    except ValueError:
        return False


# ---------- GitHub access ----------

def http_json(url: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "artcraft-parity-tracker"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_json_etag(url: str, etag=None):
    """GET with If-None-Match. Returns (data, etag); data is None for 304 Not Modified."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "artcraft-parity-tracker"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag
        raise


def fetch_raw(repo: str, ref: str):
    """Text of ROADMAP.md at a ref, or None if the file does not exist there."""
    url = f"https://raw.githubusercontent.com/{ORG}/{repo}/{ref}/{PATH}"
    req = urllib.request.Request(url, headers={"User-Agent": "artcraft-parity-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def list_repos() -> list:
    """All public repositories of the account (works for organizations and user accounts)."""
    for base in (f"https://api.github.com/orgs/{ORG}/repos", f"https://api.github.com/users/{ORG}/repos"):
        repos, page = [], 1
        try:
            while True:
                batch = http_json(f"{base}?type=public&per_page=100&page={page}")
                repos.extend(batch)
                if len(batch) < 100:
                    return repos
                page += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # not an organization, try the user endpoint
            raise
    raise RuntimeError(f"account {ORG} not found")


# ---------- Discovery of new apps ----------

def pretty_name(repo: str) -> str:
    if repo.lower().endswith("craft") and len(repo) > 5:
        return repo[:-5].capitalize() + "Craft"
    return repo.capitalize()


def reference_name(target) -> str:
    """'Adobe Photoshop 2026 (27.11.0)' -> 'Photoshop'."""
    if not target:
        return "Unknown"
    words = []
    for w in re.sub(r"\(.*?\)", " ", target).split():
        lw = w.lower().strip(",")
        if not words and lw in VENDORS:
            continue
        if lw == "for":
            break
        if re.match(r"^v?\d", w):
            continue
        words.append(w)
    return " ".join(words) or "Unknown"


def discover_new_apps(state: dict, errors: list) -> list:
    try:
        repos = list_repos()
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        errors.append(f"discovery: repository list not available ({exc})")
        return []

    checked = state.setdefault("discovery_checked", {})
    known = {a["id"] for a in CONFIG["apps"]} | IGNORED
    added = []
    for r in repos:
        name = r["name"]
        if name in known or r.get("fork") or r.get("archived"):
            continue
        last = checked.get(name)
        if last and (today_utc() - date.fromisoformat(last)).days < RECHECK_DAYS:
            continue  # checked recently without result
        try:
            text = fetch_raw(name, r.get("default_branch") or BRANCH)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"discovery: {name} could not be checked ({exc})")
            continue
        checked[name] = today_utc().isoformat()
        if text is None:
            continue  # no roadmap, not an app we track
        parsed = parse_roadmap(text)
        if parsed["metrics"]["full"] is None:
            continue  # has a ROADMAP.md, but not a Craft app roadmap (e.g. product goals)
        entry = {
            "id": name,
            "name": pretty_name(name),
            "adobe": reference_name(parsed.get("target")),
            "category": "Other",
            "color": PALETTE[len(CONFIG["apps"]) % len(PALETTE)],
            "added": today_utc().isoformat(),
        }
        CONFIG["apps"].append(entry)
        known.add(name)
        added.append(entry)

    if added:
        APPS_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


# ---------- Git access ----------

def git(*args, cwd=None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"git {args[0]} failed")
    return res.stdout


def ensure_clone(repo: str) -> Path:
    """Create a partial bare clone (no file contents) or bring an existing one up to date."""
    REPOS_CACHE.mkdir(parents=True, exist_ok=True)
    gdir = REPOS_CACHE / f"{repo}.git"
    url = f"{GIT_BASE}/{ORG}/{repo}.git"
    if not gdir.exists():
        git("clone", "--bare", "--filter=blob:none", "--quiet", url, str(gdir))
    else:
        git("-C", str(gdir), "fetch", "--quiet", "--filter=blob:none", "origin",
            f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}", "+refs/tags/*:refs/tags/*")
    return gdir


def prefetch_blobs(gdir: Path, oids: list):
    """Fetch the given blobs in a few requests instead of one lazy request per blob.

    Best effort: if the server refuses, reading the blob later fetches it on demand.
    """
    oids = sorted({o for o in oids if o})
    for i in range(0, len(oids), 400):
        try:
            git("-C", str(gdir), "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--quiet", "--no-tags",
                "--no-write-fetch-head", "--filter=blob:none", "origin", *oids[i:i + 400])
        except RuntimeError:
            return


MERGE_PR = re.compile(r"^Merge pull request #(\d+) from \S+")


def pretty_subject(subject: str, body: str) -> str:
    """GitHub merge commits say "Merge pull request #12 from user/branch"; the PR title is the first body line."""
    m = MERGE_PR.match(subject)
    title = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return f"{title} (#{m.group(1)})" if m and title else subject


def parse_log(out: str):
    """Records written with --format=%x1e%H%x1f%cI%x1f%s%x1f%b%x1d, followed by --raw or --name-only lines."""
    for block in out.split("\x1e")[1:]:
        head, _, rest = block.partition("\x1d")
        sha, date_s, subject, body = head.split("\x1f", 3)
        yield sha, date_s, pretty_subject(subject, body)[:160], [ln for ln in rest.split("\n") if ln.strip()]


def list_commits(gdir: Path, limit: int):
    """Revisions of ROADMAP.md on the main line, newest first.

    --first-parent keeps the states the branch actually had. Commits inside merged branches are not
    revisions of main; their date is when they were written, not when they were published.
    """
    out = git("-C", str(gdir), "log", f"--max-count={limit}", "--first-parent", "--diff-merges=first-parent",
              "--raw", "--no-abbrev", "--format=%x1e%H%x1f%cI%x1f%s%x1f%b%x1d", BRANCH, "--", PATH)
    commits = []
    for sha, date_s, msg, lines in parse_log(out):
        raw = [ln for ln in lines if ln.startswith(":")]
        blob = raw[0].split()[3] if raw else None
        commits.append({"sha": sha, "date": date_s, "message": msg,
                        "blob": None if not blob or set(blob) == {"0"} else blob})
    return commits


def read_roadmap_at(gdir: Path, sha: str) -> str:
    return git("-C", str(gdir), "show", f"{sha}:{PATH}")


def read_blob(gdir: Path, oid: str) -> str:
    return git("-C", str(gdir), "cat-file", "blob", oid)


# ---------- Roadmap revisions ----------

HEADER_META = re.compile(r"last reviewed|last updated|\*\*change:\*\*", re.I)
NOISE = re.compile(r"[\s|:\-–·,.;()>#]+")


def text_fingerprint(text: str) -> str:
    """Hash of the roadmap text without formatting and without the header metadata line.

    Two revisions with the same fingerprint differ only in whitespace, Markdown formatting,
    punctuation or the "Last updated / Change" header.
    """
    body = "\n".join(ln for ln in text.splitlines() if not HEADER_META.search(ln))
    norm = NOISE.sub(" ", clean(body).lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def scores_fingerprint(parsed: dict) -> str:
    """Hash of every estimate in the roadmap except the full-target value itself."""
    m = parsed["metrics"]
    payload = {
        "stage": parsed["stage"],
        "metrics": {k: [(v or {}).get("pct"), (v or {}).get("hours")] for k, v in m.items() if k != "full"},
        "full_hours": (m["full"] or {}).get("hours"),
        "dimensions": parsed["dimensions"],
        "features": parsed["features"],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def metric_point(parsed: dict, commit: dict, text: str) -> dict:
    m = parsed["metrics"]
    full = m["full"] or {}
    return {
        "commit": commit["sha"][:7],
        "commit_full": commit["sha"],
        "commit_date": commit["date"],
        "ts": utc_z(commit["date"]),
        "message": commit["message"],
        "stage": parsed["stage"],
        "next_stage": parsed["next_stage"],
        "last_updated": parsed["last_updated"],
        "full_pct": full.get("pct"),
        "full_range": full.get("range"),
        "full_hours": full.get("hours"),
        "full_label": full.get("label"),
        "target": parsed.get("target"),
        "mainstream_pct": (m["mainstream"] or {}).get("pct"),
        "essentials_pct": (m["essentials"] or {}).get("pct"),
        "breadth_pct": (m["breadth"] or {}).get("pct"),
        "scores_hash": scores_fingerprint(parsed),
        "text_hash": text_fingerprint(text),
    }


def annotate(history: list):
    """Set full_prev, kind and label_changed on every point, from the point before it.

    kind:
      first      the first revision with a full-target value (nothing to compare against)
      parity     the full-target value changed
      estimates  another estimate changed (audiences, hours, dimensions, features, stage)
      content    the text changed (new progress-log lines, plans, descriptions)
      editorial  only formatting or the header metadata changed
    Recomputed on every run, so the classification is reproducible from the stored hashes.
    """
    prev = None
    for h in history:
        h["full_prev"] = prev["full_pct"] if prev else None
        if prev is None:
            kind = "first"
        elif h["full_pct"] != prev["full_pct"]:
            kind = "parity"
        elif h.get("scores_hash") != prev.get("scores_hash"):
            kind = "estimates"
        elif h.get("text_hash") != prev.get("text_hash"):
            kind = "content"
        else:
            kind = "editorial"
        h["kind"] = kind
        if prev is not None and kind == "parity" and h.get("full_label") != prev.get("full_label"):
            h["label_changed"] = True  # the row was renamed in the same revision, possibly a method change
        else:
            h.pop("label_changed", None)
        if prev is not None and h.get("target") and prev.get("target") and h["target"] != prev["target"]:
            h["target_changed"] = prev["target"]  # the reference product (version) moved
        else:
            h.pop("target_changed", None)
        prev = h


def update_history(app: dict, limit: int, errors: list, state: dict, rebuild: bool) -> dict:
    repo = app["id"]
    history_path = HISTORY / f"{repo}.json"
    history = [] if rebuild else load_json(history_path, [])
    for h in history:
        h.setdefault("ts", utc_z(h["commit_date"]))  # points written before the ts field existed
    initial = not history  # app without any history yet (new app, first build or rebuild)
    skipped_map = state.setdefault("skipped", {})
    truncated_map = state.setdefault("truncated", {})
    if rebuild:
        skipped_map.pop(repo, None)
    empty = {"id": repo, "changed": False, "new_revisions": 0, "history": history, "gdir": None,
             "event_points": [], "initial": initial, "truncated": bool(truncated_map.get(repo)), "failed": True}
    try:
        gdir = ensure_clone(repo)
        commits = list_commits(gdir, limit)
    except RuntimeError as exc:
        errors.append(f"{repo}: git access failed ({exc})")
        return empty

    if not commits:
        errors.append(f"{repo}: no commits found for {PATH}")
        return {**empty, "gdir": gdir}

    position = {c["sha"]: i for i, c in enumerate(commits)}  # 0 = newest
    rewritten = False
    if history and len(commits) < limit:  # the listing is complete, so a missing point really left main
        kept = [h for h in history if h["commit_full"] in position]
        if len(kept) != len(history):
            errors.append(f"{repo}: {len(history) - len(kept)} stored revisions are no longer on {BRANCH} "
                          f"(history rewritten); they were dropped and this run sends no roadmap events")
            history[:] = kept
            rewritten = True
    hist_shas = {h["commit_full"] for h in history}
    if repo not in skipped_map:
        # An app with history but no state yet: commits without a data point count as skipped.
        # An app without history has nothing to compare against, so everything gets parsed.
        skipped_map[repo] = [c["sha"] for c in commits if c["sha"] not in hist_shas] if history else []
    skipped = set(skipped_map[repo])
    known_before = hist_shas | skipped
    new_commits = [c for c in commits if c["sha"] not in known_before]
    if initial:
        truncated_map[repo] = len(commits) >= limit  # older revisions exist that were not read

    newest_before = max((parse_ts(h["commit_date"]) for h in history), default=None)
    prefetch_blobs(gdir, [c["blob"] for c in new_commits])
    event_points = []  # the subset that may notify (newer than anything we had before)
    for c in reversed(new_commits):  # main-line order, oldest first
        if not c["blob"]:
            skipped.add(c["sha"])  # ROADMAP.md was deleted in this commit
            continue
        try:
            text = read_blob(gdir, c["blob"])
        except RuntimeError as exc:
            errors.append(f"{repo}: {c['sha'][:7]} could not be read ({exc})")
            continue
        try:
            parsed = parse_roadmap(text)
            point = metric_point(parsed, c, text)
        except Exception as exc:  # one malformed revision must not stop the run
            errors.append(f"{repo}: {c['sha'][:7]} could not be parsed ({type(exc).__name__}: {exc})")
            skipped.add(c["sha"])
            continue
        if point["full_pct"] is None:
            if c["sha"] == commits[0]["sha"]:
                errors.append(f"{repo}: newest roadmap has no recognizable full-target row, please check the parser")
            skipped.add(c["sha"])  # remember, so it is not parsed again
            continue
        history.append(point)
        if newest_before is not None and parse_ts(c["date"]) > newest_before:
            event_points.append(point)

    # Main-line order, also when an older revision is read later (after a read error); points older than
    # the listing limit keep their place at the start.
    history.sort(key=lambda h: -position.get(h["commit_full"], len(commits)))
    if rewritten:
        event_points = []
    annotate(history)
    save_json(history_path, history)
    skipped_map[repo] = sorted(skipped)
    return {
        "id": repo,
        "changed": bool(known_before) and commits[0]["sha"] not in known_before,
        "new_revisions": len(new_commits),
        "latest": commits[0],
        "history": history,
        "gdir": gdir,
        "event_points": event_points,
        "initial": initial,
        "truncated": bool(truncated_map.get(repo)),
        "failed": False,
    }


def parity_summary(history: list, truncated: bool) -> dict:
    changes = [h for h in history if h.get("kind") == "parity"]
    last = changes[-1] if changes else None
    return {
        "tracked_since": history[0]["ts"] if history else None,
        "changes": len(changes),
        "truncated": truncated,
        "last_change": None if not last else {
            "ts": last["ts"],
            "from": last["full_prev"],
            "to": last["full_pct"],
            "delta": round(last["full_pct"] - last["full_prev"], 1),
            "commit": last["commit"],
            "commit_full": last["commit_full"],
            "message": last["message"],
            "label_changed": bool(last.get("label_changed")),
        },
    }


def roadmap_summary(history: list, now: datetime) -> dict:
    def ref(h):
        return None if not h else {"ts": h["ts"], "kind": h["kind"], "commit": h["commit"],
                                   "commit_full": h["commit_full"], "message": h["message"]}
    substantive = [h for h in history if h.get("kind") in ("parity", "estimates", "content")]
    cutoff = now - timedelta(days=7)
    return {
        "revisions": len(history),
        "revisions_7d": sum(1 for h in substantive if parse_ts(h["ts"]) >= cutoff),
        "last_revision": ref(history[-1] if history else None),
        "last_update": ref(substantive[-1] if substantive else None),
        "self_reported": history[-1].get("last_updated") if history else None,
    }


def milestone_summary(history: list):
    """First time the full target reached MILESTONE_PCT, from the stored history.

    The date is the commit that first reported the value, so a rebuild never moves it to "now".
    If the very first known revision is already at or above the threshold, the real date is unknown.
    """
    first = None
    crossings = 0
    for i, h in enumerate(history):
        prev = history[i - 1]["full_pct"] if i else None
        if h["full_pct"] >= MILESTONE_PCT and (prev is None or prev < MILESTONE_PCT):
            crossings += 1
            if first is None:
                first = (h, prev is None)
    if not first:
        return None
    h, unknown = first
    return {
        "threshold": MILESTONE_PCT,
        "reached_at": None if unknown else h["ts"],
        "date_known": not unknown,
        "commit": h["commit"],
        "value": h["full_pct"],
        "above_now": history[-1]["full_pct"] >= MILESTONE_PCT,
        "crossings": crossings,
    }


# ---------- Repository activity ----------

HOUSEKEEPING_FILES = re.compile(
    r"(^roadmap\.md$|\.(md|markdown|txt|rst)$|^docs?/|^\.github/|^\.gitlab-ci\.yml$|(^|/)\.[a-z]|"
    r"(^|/)(license|licence|notice|authors|contributors|codeowners)[^/]*$|"
    r"(^|/)(cargo\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock)$)",
    re.I)
VERSION_FILES = re.compile(
    r"(^|/)(cargo\.(toml|lock)|package(-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|pyproject\.toml|"
    r"tauri\.conf\.json|version(\.txt)?|changelog(\.md)?)$", re.I)
RELEASE_SUBJECT = re.compile(r"^(release|bump (the )?version|version bump|chore\(release\))\b", re.I)
SEMVER_TAG = re.compile(r"^v?\d+\.\d+(\.\d+)?([-+][0-9a-z.\-]+)?$", re.I)


def classify_changeset(files: list, subject: str) -> str:
    """'release', 'housekeeping' or 'code' for one changeset on the main line.

    housekeeping: only documentation, the roadmap, CI configuration, dotfiles or lockfiles
    release:      a version bump ("Release 0.9.0") that touches only version files
    code:         everything else (source, tests, translations, assets, build configuration)
    """
    if RELEASE_SUBJECT.match(subject) and files and all(VERSION_FILES.search(f) for f in files):
        return "release"
    if all(HOUSEKEEPING_FILES.search(f) for f in files):
        return "housekeeping"  # includes changesets without file changes
    return "code"


def read_changesets(gdir: Path, since, max_count=None) -> list:
    args = ["-C", str(gdir), "log", BRANCH, "--first-parent", "--diff-merges=first-parent", "--name-only",
            "--format=%x1e%H%x1f%cI%x1f%s%x1f%b%x1d"]
    if since is not None:
        args.append(f"--since={since.isoformat()}")
    if max_count:
        args.append(f"--max-count={max_count}")
    out = git(*args)
    return [{"sha": sha, "ts": utc_z(date_s), "subject": subject, "kind": classify_changeset(files, subject)}
            for sha, date_s, subject, files in parse_log(out)]


def activity_level(day_counts: dict, today: date) -> dict:
    """Score and level from code changesets per UTC day.

    score = sum over the last ACTIVITY_WINDOW_DAYS days of 0.5^(age / half-life) * log2(1 + changesets)
    The logarithm dampens bursts (300 merges on one day weigh about as much as 3 a day for a week),
    the decay makes the score fall during pauses, and the level also requires activity on several days.
    """
    score, active7, n7, n14 = 0.0, 0, 0, 0
    for d, n in day_counts.items():
        age = (today - d).days
        if n <= 0 or age < 0 or age >= ACTIVITY_WINDOW_DAYS:
            continue
        score += 0.5 ** (age / ACTIVITY_HALF_LIFE_DAYS) * math.log2(1 + n)
        n14 += n
        if age < 7:
            active7 += 1
            n7 += n
    level = "quiet" if n14 else "cold"
    for name, min_score, min_days in ACTIVITY_LEVELS:
        if score >= min_score and active7 >= min_days:
            level = name
            break
    return {"level": level, "score": round(score, 1), "active_days_7d": active7,
            "changes_7d": n7, "changes_14d": n14}


def repo_activity(gdir, now: datetime, errors: list, repo: str):
    if gdir is None:
        return None
    try:
        sets = read_changesets(gdir, now - timedelta(days=ACTIVITY_HISTORY_DAYS))
        code = [c for c in sets if c["kind"] == "code"]
        last_code = code[0] if code else None
        if last_code is None:  # nothing in the window: look further back for the last code change
            older = [c for c in read_changesets(gdir, None, 500) if c["kind"] == "code"]
            last_code = older[0] if older else None
        first_commit = git("-C", str(gdir), "log", BRANCH, "--first-parent", "--reverse", "--format=%cI")
        first_ts = first_commit.split("\n", 1)[0].strip()
    except RuntimeError as exc:
        errors.append(f"{repo}: repository activity could not be read ({exc})")
        return None

    days = {}
    for c in sets:
        d = days.setdefault(c["ts"][:10], {"d": c["ts"][:10], "code": 0, "other": 0, "samples": []})
        if c["kind"] == "code":
            d["code"] += 1
            if len(d["samples"]) < SAMPLES_PER_DAY:
                d["samples"].append({"commit": c["sha"][:7], "subject": c["subject"]})
        else:
            d["other"] += 1
    today = now.date()
    level = activity_level({date.fromisoformat(k): v["code"] for k, v in days.items()}, today)
    window = [(today - timedelta(days=i)).isoformat() for i in range(ACTIVITY_WINDOW_DAYS - 1, -1, -1)]
    return {
        **level,
        "housekeeping_7d": sum(v["other"] for k, v in days.items()
                               if (today - date.fromisoformat(k)).days < 7),
        "last_code_change": None if not last_code else {
            "ts": last_code["ts"], "commit": last_code["sha"][:7], "commit_full": last_code["sha"],
            "subject": last_code["subject"]},
        "last_commit_ts": sets[0]["ts"] if sets else None,
        "repo_since": utc_z(first_ts) if first_ts else None,
        "window": [days.get(k, {}).get("code", 0) for k in window],  # oldest first, ends today
        "days": sorted(days.values(), key=lambda v: v["d"]),
    }


CHANGE_LINE = re.compile(r"^\s*[*-]\s+.+\s+in\s+https://github\.com/\S+/pull/\d+", re.M)
NEW_CONTRIBUTOR = re.compile(r"^\s*[*-]\s+@\S+\s+made their first contribution", re.M)
PLATFORMS = (("macOS", re.compile(r"mac|darwin|\.dmg$", re.I)),
             ("Windows", re.compile(r"(^|[-_.])win(dows|32|64)?([-_.]|$)|\.msi$|\.exe$", re.I)),
             ("Linux", re.compile(r"linux|appimage|\.deb$|\.rpm$|flatpak", re.I)),
             ("Web", re.compile(r"web|wasm", re.I)))
RELEASES_KEPT = 12


def semver(tag: str):
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", tag or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def release_kinds(releases: list):
    """'major', 'minor' or 'patch' from the version step to the previous stable release (newest first).

    Pre-releases are never the base of comparison, so "1.0.0-rc1" does not turn "0.10.0" into a major step.
    """
    for i, r in enumerate(releases):
        cur = semver(r["tag"])
        prev = next((semver(o["tag"]) for o in releases[i + 1:] if semver(o["tag"]) and not o.get("prerelease")),
                    None)
        if not cur or not prev:
            r["kind"] = None
        elif cur[0] != prev[0]:
            r["kind"] = "major"
        elif cur[1] != prev[1]:
            r["kind"] = "minor"
        else:
            r["kind"] = "patch"


def release_entry(rel: dict) -> dict:
    body = rel.get("body") or ""
    platforms = [name for name, rx in PLATFORMS if any(rx.search(a.get("name", "")) for a in rel.get("assets", []))]
    return {
        "tag": rel["tag_name"],
        "name": rel.get("name") or rel["tag_name"],
        "ts": utc_z(rel.get("published_at") or rel.get("created_at")),
        "url": rel.get("html_url"),
        "changes": sum(1 for ln in CHANGE_LINE.findall(body)
                       if not NEW_CONTRIBUTOR.match(ln)) or None,  # pull requests listed in the notes
        "new_contributors": len(NEW_CONTRIBUTOR.findall(body)) or None,
        "platforms": platforms,
        "prerelease": bool(rel.get("prerelease")),
        "source": "release",
    }


def fetch_releases(repo: str, state: dict, errors: list):
    """Published GitHub Releases, newest first, or the last known list if the API is not reachable.

    state.json keeps only the extracted list (stable while nothing is released). The ETag lives in .cache:
    GitHub's response includes download counts, so its ETag changes on almost every call and would
    otherwise make state.json change, and the workflow commit, on every run.
    """
    cache = state.setdefault("releases", {})
    etags = load_json(ETAGS_FILE, {})
    url = f"https://api.github.com/repos/{ORG}/{repo}/releases?per_page=30"
    try:
        data, etag = http_json_etag(url, etags.get(repo) if repo in cache else None)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        if repo not in cache:
            errors.append(f"{repo}: releases not available ({exc}), using git tags")
        return cache.get(repo)
    if data is None:  # 304 Not Modified
        return cache.get(repo)
    rels = [release_entry(r) for r in data if not r.get("draft") and r.get("tag_name")]
    rels.sort(key=lambda r: r["ts"], reverse=True)
    cache[repo] = rels[:RELEASES_KEPT]
    if etag:
        etags[repo] = etag
        save_json(ETAGS_FILE, etags)
    return cache[repo]


def read_releases(gdir, errors: list, repo: str, state=None) -> list:
    published = fetch_releases(repo, state, errors) if state is not None else None
    if published:  # an empty list (no GitHub Releases) falls back to tags too
        release_kinds(published)
        return published
    if gdir is None:
        return []
    try:
        out = git("-C", str(gdir), "for-each-ref", "refs/tags", "--sort=-creatordate",
                  "--format=%(refname:short)%1f%(creatordate:iso-strict)")
    except RuntimeError as exc:
        errors.append(f"{repo}: tags could not be read ({exc})")
        return []
    releases = []
    for line in out.splitlines():
        tag, _, ts = line.partition("\x1f")
        if SEMVER_TAG.match(tag) and ts:
            releases.append({"tag": tag, "name": tag, "ts": utc_z(ts), "url": None, "changes": None,
                             "new_contributors": None, "platforms": [], "prerelease": False, "source": "tag"})
    releases = releases[:RELEASES_KEPT]
    release_kinds(releases)
    return releases


# ---------- JSON helpers ----------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_if_changed(path: Path, body: dict, stamp_key: str) -> bool:
    """Write body plus a timestamp only if the content (ignoring the timestamp) changed."""
    old = load_json(path, None)
    if old is not None and {k: v for k, v in old.items() if k != stamp_key} == body:
        return False
    out = dict(body)
    out[stamp_key] = now_iso()
    save_json(path, out)
    return True


def compact_series(history: list) -> list:
    """Points where the value changed, plus the first and last point (enough for the card trend)."""
    out = []
    for i, h in enumerate(history):
        if i == 0 or i == len(history) - 1 or h["full_pct"] != history[i - 1]["full_pct"]:
            out.append({"t": h["ts"], "v": h["full_pct"]})
    return out


def suite_events(apps: list) -> list:
    """Newest relevant events across all apps: parity changes, milestones, releases, new apps."""
    events = []
    for a in apps:
        base = {"id": a["id"], "name": a["name"]}
        for h in a.pop("_parity_points"):
            events.append({**base, "type": "parity", "ts": h["ts"], "from": h["full_prev"], "to": h["full_pct"],
                           "delta": round(h["full_pct"] - h["full_prev"], 1), "commit": h["commit"],
                           "message": h["message"]})
        ms = a.get("milestone")
        if ms and ms.get("reached_at"):
            events.append({**base, "type": "milestone", "ts": ms["reached_at"], "value": ms["value"],
                           "threshold": ms["threshold"], "commit": ms["commit"]})
        for r in a.pop("_releases")[:3]:
            events.append({**base, "type": "release", "ts": r["ts"], "tag": r["tag"], "kind": r.get("kind"),
                           "changes": r.get("changes"), "url": r.get("url"), "prerelease": r.get("prerelease")})
        if a.get("is_new") and a.get("added"):
            events.append({**base, "type": "new_app", "ts": a["added"] + "T00:00:00Z", "adobe": a["adobe"]})
    events.sort(key=lambda e: (e["ts"], e["type"] == "milestone", e["id"]), reverse=True)
    return events[:RECENT_EVENTS]


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="rebuild the roadmap history from scratch, reading up to N revisions per app")
    args = ap.parse_args()

    for d in (DATA, APPS_DIR, HISTORY, CACHE):
        d.mkdir(parents=True, exist_ok=True)

    errors: list = []
    events: list = []
    state = load_json(STATE, {"skipped": {}})
    migrate = state.get("schema") != SCHEMA
    rebuild = bool(args.backfill) or migrate
    limit = args.backfill or DEFAULT_LIMIT
    emit_events = not rebuild  # a rebuild re-reads old revisions; it must not ping anyone
    if migrate:
        print(f"data schema {state.get('schema')} -> {SCHEMA}: rebuilding all histories, no roadmap events",
              file=sys.stderr)
    now = now_utc()

    added = discover_new_apps(state, errors)
    for a in added:
        print(f"new app discovered: {a['name']} ({a['id']})", file=sys.stderr)
        events.append({"type": "new_app", "id": a["id"], "name": a["name"], "adobe": a["adobe"]})

    overview_apps = []
    failed: list = []
    for app in CONFIG["apps"]:
        repo = app["id"]
        res = update_history(app, limit, errors, state, rebuild)
        history = res["history"]
        print(f"{repo:12} new revisions: {res['new_revisions']:4}  history: {len(history):4}", file=sys.stderr)

        # Roadmap events only for apps that already had history (not for a new app's first build)
        if emit_events and not res["initial"]:
            for p in res["event_points"]:
                events.append({
                    "type": "roadmap_update",
                    "id": repo,
                    "name": app["name"],
                    "commit": p["commit"],
                    "commit_url": f"https://github.com/{ORG}/{repo}/commit/{p['commit_full']}",
                    "date": p["ts"][:10],
                    "message": p["message"],
                    "kind": p["kind"],
                    "full_before": p.get("full_prev"),
                    "full_after": p["full_pct"],
                    "stage": p["stage"],
                })

        latest_point = history[-1] if history else None
        parsed_latest = None
        if latest_point and res["gdir"] is not None:
            try:
                parsed_latest = parse_roadmap(read_roadmap_at(res["gdir"], latest_point["commit_full"]))
            except RuntimeError as exc:
                errors.append(f"{repo}: detail parse failed ({exc})")

        activity = repo_activity(res["gdir"], now, errors, repo)
        releases = read_releases(res["gdir"], errors, repo, state)
        if res.get("failed"):
            failed.append(repo)
            previous = load_json(APPS_DIR / f"{repo}.json", {})  # keep the last known state instead of blanks
            activity = activity or previous.get("activity")
            releases = releases or previous.get("releases") or []
            res["latest"] = {"sha": previous.get("latest_commit") or "", "date": previous.get("latest_commit_date"),
                             "message": previous.get("latest_message")}
        parity = parity_summary(history, res["truncated"])
        roadmap = roadmap_summary(history, now)
        milestone = milestone_summary(history) if history else None
        latest = res.get("latest") or {}
        detail = {
            "id": repo,
            "name": app["name"],
            "adobe": app["adobe"],
            "category": app["category"],
            "color": app.get("color"),
            "added": app.get("added"),
            "repo_url": f"https://github.com/{ORG}/{repo}",
            "roadmap_url": f"https://github.com/{ORG}/{repo}/blob/{BRANCH}/{PATH}",
            "latest_commit": latest.get("sha", "")[:7] or None,
            "latest_commit_date": latest.get("date"),
            "latest_message": latest.get("message"),
            "parsed": parsed_latest,
            "parity": parity,
            "roadmap": roadmap,
            "milestone": milestone,
            "activity": activity,
            "releases": releases,
            "history": [{k: v for k, v in h.items() if k not in ("scores_hash", "text_hash")} for h in history],
        }
        save_if_changed(APPS_DIR / f"{repo}.json", detail, "updated_at")

        lp = latest_point or {}
        overview_apps.append({
            "id": repo,
            "name": app["name"],
            "adobe": app["adobe"],
            "category": app["category"],
            "color": app.get("color"),
            "added": app.get("added"),
            "is_new": is_recent(app.get("added")),
            "target": (parsed_latest or {}).get("target"),
            "order": CONFIG["apps"].index(app) + 1,
            "stage": lp.get("stage"),
            "next_stage": lp.get("next_stage"),
            "full_pct": lp.get("full_pct"),
            "full_range": lp.get("full_range"),
            "full_hours": lp.get("full_hours"),
            "mainstream_pct": lp.get("mainstream_pct"),
            "essentials_pct": lp.get("essentials_pct"),
            "breadth_pct": lp.get("breadth_pct"),
            "parity": parity,
            "roadmap": roadmap,
            "milestone": milestone,
            "activity": None if not activity else {k: v for k, v in activity.items() if k != "days"},
            "release": next((r for r in releases if not r["prerelease"]), releases[0] if releases else None),
            "series": compact_series(history),
            "history_len": len(history),
            "_parity_points": [h for h in history if h.get("kind") == "parity"],
            "_releases": releases,
        })

    if migrate and failed:
        # Keep the old schema, so the next run rebuilds again instead of mixing old and new history files
        errors.append(f"rebuild incomplete ({', '.join(failed)}), it runs again next time")
    else:
        state["schema"] = SCHEMA
    save_json(STATE, state)
    overview_apps.sort(key=lambda a: (a["full_pct"] is None, -(a["full_pct"] or 0)))
    latest_events = suite_events(overview_apps)
    recent = [{"id": a["id"], "name": a["name"], "adobe": a["adobe"], "added": a["added"]}
              for a in overview_apps if a["is_new"]]
    overview_changed = save_if_changed(DATA / "overview.json", {
        "source": f"https://github.com/{ORG}",
        "tracker_repo": TRACKER_REPO,
        "milestone_pct": MILESTONE_PCT,
        "activity_window_days": ACTIVITY_WINDOW_DAYS,
        "recent_additions": recent,
        "latest_events": latest_events,
        "apps": overview_apps,
        "errors": errors,
    }, "generated_at")

    # Heartbeat: one status commit per day keeps the repository active (GitHub pauses schedules
    # in public repositories after 60 days without activity). Runs with changes write it anyway.
    status = load_json(STATUS, {})
    heartbeat_due = status.get("heartbeat_date") != today_utc().isoformat()
    if events or added or heartbeat_due or overview_changed:
        save_json(STATUS, {"heartbeat_date": today_utc().isoformat(), "checked_at": now.isoformat(),
                           "events_in_last_run": len(events)})

    save_json(EVENTS_FILE, events)
    if events:
        log = load_json(CHANGELOG, [])
        log = (log + [{"checked_at": now.isoformat(), **e} for e in events])[-500:]
        save_json(CHANGELOG, log)

    print(f"done: {len(overview_apps)} apps, {len(added)} newly discovered, "
          f"{len(events)} events, {len(errors)} notices", file=sys.stderr)
    for e in errors:
        print("  -", e, file=sys.stderr)


if __name__ == "__main__":
    main()
