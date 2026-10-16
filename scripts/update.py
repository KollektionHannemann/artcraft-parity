#!/usr/bin/env python3
"""Checks the ROADMAP.md of each Craft app for changes and updates the data files.

Ablauf pro App:
  1. Partial clone (commits and trees only, no blobs) cached under .cache/repos/.
     No API quota is needed, only git access to github.com.
  2. All commits that changed ROADMAP.md are read.
  3. Each unknown commit is parsed and appended as a point to data/history/<app>.json.
  4. The latest state produces data/overview.json and data/apps/<app>.json.

Aufruf:
  python3 scripts/update.py               # regular run (new commits only)
  python3 scripts/update.py --backfill 500  # build the history from scratch
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from roadmap_parser import parse_roadmap  # noqa: E402

CONFIG = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
ORG, BRANCH, PATH = CONFIG["org"], CONFIG["branch"], CONFIG["roadmap_path"]
DATA = ROOT / "data"
HISTORY = DATA / "history"
STATE = DATA / "state.json"
APPS_DIR = DATA / "apps"
CACHE = ROOT / ".cache" / "repos"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
        sha, date, msg = line.split("\x1f", 2)
        commits.append({"sha": sha, "date": date, "message": msg[:160]})
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
        # First run with state tracking: every commit without a data point counts as skipped
        skipped_map[repo] = [c["sha"] for c in commits if c["sha"] not in hist_shas]
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
    overview_apps = []

    for app in CONFIG["apps"]:
        repo = app["id"]
        res = update_history(app, limit, errors, state)
        history = res["history"]
        print(f"{repo:12} new revisions: {res['new_revisions']:3}  history: {len(history):3}",
              file=sys.stderr)

        latest_point = history[-1] if history else None
        prev_point = history[-2] if len(history) > 1 else None

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
            "color": app.get("color"),
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
    save_json(DATA / "overview.json", {
        "generated_at": checked_at,
        "source": f"https://github.com/{ORG}",
        "apps": overview_apps,
        "errors": errors,
    })
    print(f"done: {len(overview_apps)} apps, {len(errors)} notices", file=sys.stderr)
    for e in errors:
        print("  -", e, file=sys.stderr)


if __name__ == "__main__":
    main()
