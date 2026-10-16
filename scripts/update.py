#!/usr/bin/env python3
"""Checks the ROADMAP.md of each Craft app for changes and updates the data files.

Each run:
  1. Discovery: lists the public repositories of the GitHub account. Repositories that are not
     yet tracked, have a ROADMAP.md and whose roadmap contains a full-target row are added to apps.json.
  2. History: partial clones (commits and trees only, no file contents) are cached under .cache/repos/.
     Every unknown commit that changed ROADMAP.md is parsed and appended to data/history/<app>.json.
  3. Output: the latest state produces data/overview.json and data/apps/<app>.json.

Usage:
  python3 scripts/update.py                 # regular run
  python3 scripts/update.py --backfill 500  # build the history from scratch

Set GITHUB_TOKEN to raise the GitHub API rate limit (GitHub Actions provides it automatically).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from roadmap_parser import parse_roadmap  # noqa: E402

APPS_FILE = ROOT / "apps.json"
CONFIG = json.loads(APPS_FILE.read_text(encoding="utf-8"))
ORG, BRANCH, PATH = CONFIG["org"], CONFIG["branch"], CONFIG["roadmap_path"]
IGNORED = set(CONFIG.get("ignored_repos", []))
DATA = ROOT / "data"
HISTORY = DATA / "history"
APPS_DIR = DATA / "apps"
STATE = DATA / "state.json"
CACHE = ROOT / ".cache" / "repos"
RECENT_DAYS = 14  # how long a newly discovered app is highlighted on the dashboard
PALETTE = ["#3d6cf0", "#ff6a4d", "#8f5cf5", "#f5a524", "#1ab394", "#e8448a", "#8bc53f",
           "#2bc48a", "#3f63e8", "#1cb8c9", "#ff7a1a", "#20b2c8", "#22a85a"]
VENDORS = {"adobe", "microsoft", "autodesk", "avid"}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


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


def discover_new_apps(errors: list) -> list:
    try:
        repos = list_repos()
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        errors.append(f"discovery: repository list not available ({exc})")
        return []

    known = {a["id"] for a in CONFIG["apps"]} | IGNORED
    added = []
    for r in repos:
        name = r["name"]
        if name in known or r.get("fork") or r.get("archived"):
            continue
        try:
            text = fetch_raw(name, r.get("default_branch") or BRANCH)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"discovery: {name} could not be checked ({exc})")
            continue
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


# ---------- Git history ----------

def git(*args, cwd=None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"git {args[0]} failed")
    return res.stdout


def ensure_clone(repo: str) -> Path:
    """Create a partial bare clone or bring an existing one up to date."""
    CACHE.mkdir(parents=True, exist_ok=True)
    gdir = CACHE / f"{repo}.git"
    url = f"https://github.com/{ORG}/{repo}.git"
    if not gdir.exists():
        git("clone", "--bare", "--filter=blob:none", "--quiet", url, str(gdir))
    else:
        git("-C", str(gdir), "fetch", "--quiet", "--filter=blob:none", "origin",
            f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}")
    return gdir


def list_commits(gdir: Path, limit: int):
    """Commits that changed ROADMAP.md, newest first."""
    out = git("-C", str(gdir), "log", f"--max-count={limit}",
              "--format=%H%x1f%cI%x1f%s", BRANCH, "--", PATH)
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date_s, msg = line.split("\x1f", 2)
        commits.append({"sha": sha, "date": date_s, "message": msg[:160]})
    return commits


def read_roadmap_at(gdir: Path, sha: str) -> str:
    return git("-C", str(gdir), "show", f"{sha}:{PATH}")


def metric_point(parsed: dict, commit: dict) -> dict:
    m = parsed["metrics"]
    full = m["full"] or {}
    return {
        "commit": commit["sha"][:7],
        "commit_full": commit["sha"],
        "commit_date": commit["date"],
        "message": commit["message"],
        "stage": parsed["stage"],
        "next_stage": parsed["next_stage"],
        "last_updated": parsed["last_updated"],
        "full_pct": full.get("pct"),
        "full_range": full.get("range"),
        "full_hours": full.get("hours"),
        "mainstream_pct": (m["mainstream"] or {}).get("pct"),
        "essentials_pct": (m["essentials"] or {}).get("pct"),
        "breadth_pct": (m["breadth"] or {}).get("pct"),
    }


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_history(app: dict, limit: int, errors: list, state: dict) -> dict:
    repo = app["id"]
    history_path = HISTORY / f"{repo}.json"
    history = load_json(history_path, [])
    skipped_map = state.setdefault("skipped", {})
    try:
        gdir = ensure_clone(repo)
        commits = list_commits(gdir, limit)
    except RuntimeError as exc:
        errors.append(f"{repo}: git access failed ({exc})")
        return {"id": repo, "changed": False, "new_revisions": 0, "history": history, "gdir": None}

    if not commits:
        errors.append(f"{repo}: no commits found for {PATH}")
        return {"id": repo, "changed": False, "new_revisions": 0, "history": history, "gdir": gdir}

    hist_shas = {h["commit_full"] for h in history}
    if repo not in skipped_map:
        # First run with state tracking. For an app that already has history, commits without a
        # data point count as skipped. A new app has no history, so everything gets parsed.
        skipped_map[repo] = [c["sha"] for c in commits if c["sha"] not in hist_shas] if history else []
    skipped = set(skipped_map[repo])
    known_before = hist_shas | skipped
    new_commits = [c for c in commits if c["sha"] not in known_before]

    for c in reversed(new_commits):  # append in chronological order
        try:
            parsed = parse_roadmap(read_roadmap_at(gdir, c["sha"]))
        except RuntimeError as exc:
            errors.append(f"{repo}: {c['sha'][:7]} could not be read ({exc})")
            continue
        point = metric_point(parsed, c)
        if point["full_pct"] is None:
            if c["sha"] == commits[0]["sha"]:
                # Warn if the newest revision has no full-target row (format changed?)
                errors.append(f"{repo}: newest roadmap has no recognizable full-target row, please check the parser")
            skipped.add(c["sha"])  # remember, so it is not parsed again
            continue
        history.append(point)

    history.sort(key=lambda h: h["commit_date"])
    save_json(history_path, history)
    skipped_map[repo] = sorted(skipped)
    # "changed" only if the newest commit was not known before and history existed already
    changed = bool(known_before) and commits[0]["sha"] not in known_before
    return {
        "id": repo,
        "changed": changed,
        "new_revisions": len(new_commits),
        "latest": commits[0],
        "history": history,
        "gdir": gdir,
    }


def value_change(history):
    """Difference of the full-target value to the last change, and the date of that change."""
    if len(history) < 2:
        return None, None
    cur = history[-1]["full_pct"]
    i = len(history) - 1
    while i > 0 and history[i - 1]["full_pct"] == cur:
        i -= 1
    if i == 0:
        return None, None
    return round(cur - history[i - 1]["full_pct"], 1), history[i]["commit_date"][:10]


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="Commit limit per app (default: 200)")
    args = ap.parse_args()
    limit = args.backfill or 200

    DATA.mkdir(exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)

    errors: list = []
    state = load_json(STATE, {"skipped": {}})
    checked_at = now_iso()

    added = discover_new_apps(errors)
    for a in added:
        print(f"new app discovered: {a['name']} ({a['id']})", file=sys.stderr)

    overview_apps = []
    for app in CONFIG["apps"]:
        repo = app["id"]
        res = update_history(app, limit, errors, state)
        history = res["history"]
        print(f"{repo:12} new revisions: {res['new_revisions']:3}  history: {len(history):3}",
              file=sys.stderr)

        latest_point = history[-1] if history else None
        parsed_latest = None
        if latest_point:
            try:
                parsed_latest = parse_roadmap(read_roadmap_at(res["gdir"], latest_point["commit_full"]))
            except (RuntimeError, AttributeError) as exc:
                errors.append(f"{repo}: detail parse failed ({exc})")

        latest = res.get("latest") or {}
        save_json(APPS_DIR / f"{repo}.json", {
            "id": repo,
            "name": app["name"],
            "adobe": app["adobe"],
            "category": app["category"],
            "color": app.get("color"),
            "added": app.get("added"),
            "repo_url": f"https://github.com/{ORG}/{repo}",
            "roadmap_url": f"https://github.com/{ORG}/{repo}/blob/{BRANCH}/{PATH}",
            "checked_at": checked_at,
            "latest_commit": latest.get("sha", "")[:7] or None,
            "latest_commit_date": latest.get("date"),
            "latest_message": latest.get("message"),
            "parsed": parsed_latest,
            "history": history,
        })

        lp = latest_point or {}
        delta, delta_since = value_change(history)
        overview_apps.append({
            "id": repo,
            "name": app["name"],
            "adobe": app["adobe"],
            "category": app["category"],
            "color": app.get("color"),
            "added": app.get("added"),
            "is_new": is_recent(app.get("added")),
            "target": (parsed_latest or {}).get("target"),
            "stage": lp.get("stage"),
            "next_stage": lp.get("next_stage"),
            "full_pct": lp.get("full_pct"),
            "full_range": lp.get("full_range"),
            "full_hours": lp.get("full_hours"),
            "full_delta": delta,
            "full_delta_since": delta_since,
            "series": [{"d": h["commit_date"][:10], "v": h["full_pct"]} for h in history],
            "order": CONFIG["apps"].index(app) + 1,
            "mainstream_pct": lp.get("mainstream_pct"),
            "essentials_pct": lp.get("essentials_pct"),
            "breadth_pct": lp.get("breadth_pct"),
            "last_updated": lp.get("last_updated") or (latest.get("date") or "")[:10] or None,
            "latest_commit_date": latest.get("date"),
            "changed_in_run": res["changed"],
            "new_revisions": res["new_revisions"],
            "history_len": len(history),
        })

    save_json(STATE, state)
    overview_apps.sort(key=lambda a: (a["full_pct"] is None, -(a["full_pct"] or 0)))
    recent = [{"id": a["id"], "name": a["name"], "adobe": a["adobe"], "added": a["added"]}
              for a in overview_apps if a["is_new"]]
    save_json(DATA / "overview.json", {
        "generated_at": checked_at,
        "source": f"https://github.com/{ORG}",
        "recent_additions": recent,
        "apps": overview_apps,
        "errors": errors,
    })
    print(f"done: {len(overview_apps)} apps, {len(added)} newly discovered, {len(errors)} notices",
          file=sys.stderr)
    for e in errors:
        print("  -", e, file=sys.stderr)


if __name__ == "__main__":
    main()
