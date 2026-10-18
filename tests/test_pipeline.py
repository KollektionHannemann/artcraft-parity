"""End-to-end tests for scripts/update.py and scripts/notify.py against real, local git repositories.

Each test builds bare "origin" repositories with commits at fixed dates, points the pipeline at them
(file:// URLs, a temporary data directory, a fixed clock) and runs update.main() like the workflow does.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import notify  # noqa: E402
import update  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def roadmap(full, mainstream=60, progress=(), note="", bold=True):
    b = "**" if bold else ""
    rows = "\n".join(f"| {d} | {t} |" for d, t in progress)
    return f"""# TestCraft Roadmap

**Stage: alpha** · next: beta

> **Last reviewed:** 2026-10-10 · **Last updated:** 2026-10-12 · **Target:** Adobe Testshop 2026

{note}

**Readiness by audience**

| Audience | Ready % | Hours to ~95% |
|---|---:|---:|
| {b}Full Testshop{b} (ready for real work) | {b}~{full}%{b} (50–70%) | ~100–200 h |
| {b}Mainstream user{b} | ~{mainstream}% | ~50–90 h |

## Progress log

| Date | Change |
|---|---|
{rows}
"""


class Repo:
    """A bare origin repository plus a work tree to commit into, with controlled commit dates."""

    def __init__(self, base: Path, name: str):
        self.name = name
        self.origin = base / "origin" / "storytold" / f"{name}.git"
        self.work = base / "work" / name
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True)
        self._git(None, "init", "--bare", "-q", "-b", "main", str(self.origin))
        for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            self._git(None, "-C", str(self.origin), "config", key, "true")
        self._git(None, "init", "-q", "-b", "main", str(self.work))
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")
        self.git("remote", "add", "origin", str(self.origin))

    @staticmethod
    def _git(env, *args):
        res = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
        if res.returncode:
            raise RuntimeError(res.stderr)
        return res.stdout

    def git(self, *args, env=None):
        return self._git(env, "-C", str(self.work), *args)

    def commit(self, when: datetime, message: str, files: dict, force=False):
        for path, text in files.items():
            p = self.work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        self.git("push", "-q", *(["--force"] if force else []), "origin", "main")

    def tag(self, name: str, when: datetime):
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        env = {**os.environ, "GIT_COMMITTER_DATE": stamp}
        self.git("tag", "-a", name, "-m", name, env=env)
        self.git("push", "-q", "origin", name)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tracker-test-"))
        self.root = self.tmp / "tracker"
        (self.root / "data").mkdir(parents=True)
        self.apps = [{"id": "testcraft", "name": "TestCraft", "adobe": "Testshop", "category": "Image",
                      "color": "#3d6cf0"}]
        self.repos = {"testcraft": Repo(self.tmp, "testcraft")}
        self.remote_listing = []
        self.api_calls = []
        self.patches = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers ----------

    def configure(self, now=NOW):
        apps_file = self.root / "apps.json"
        if apps_file.exists():  # keep what discovery wrote in an earlier run
            cfg = json.loads(apps_file.read_text(encoding="utf-8"))
            known = {a["id"] for a in cfg["apps"]}
            cfg["apps"] += [a for a in self.apps if a["id"] not in known]
        else:
            cfg = {"org": "storytold", "branch": "main", "roadmap_path": "ROADMAP.md",
                   "notify": "tester", "ignored_repos": ["artcraft"], "apps": list(self.apps)}
        apps_file.write_text(json.dumps(cfg), encoding="utf-8")
        data = self.root / "data"
        values = {
            "APPS_FILE": apps_file, "CONFIG": cfg, "ORG": "storytold", "BRANCH": "main", "PATH": "ROADMAP.md",
            "IGNORED": set(cfg["ignored_repos"]), "DATA": data, "HISTORY": data / "history",
            "APPS_DIR": data / "apps", "STATE": data / "state.json", "STATUS": data / "status.json",
            "CHANGELOG": data / "changelog.json", "CACHE": self.root / ".cache",
            "EVENTS_FILE": self.root / ".cache" / "events.json", "REPOS_CACHE": self.root / ".cache" / "repos",
            "ETAGS_FILE": self.root / ".cache" / "release-etags.json",
            "GIT_BASE": f"file://{self.tmp / 'origin'}",
        }
        for p in self.patches:
            p.stop()
        self.patches = [mock.patch.object(update, k, v) for k, v in values.items()]
        self.patches.append(mock.patch.object(update, "now_utc", lambda: now))
        self.patches.append(mock.patch.object(update, "list_repos", lambda: self.remote_listing))
        self.patches.append(mock.patch.object(update, "fetch_raw", self.fake_raw))
        self.patches.append(mock.patch.object(update, "http_json_etag", self.fake_api))
        for p in self.patches:
            p.start()

    releases_api = None  # None: API unreachable (tests then use git tags)

    def fake_api(self, url, etag=None):
        if self.releases_api is None:
            raise update.urllib.error.URLError("offline test")
        self.api_calls.append((url, etag))
        data, new_etag = self.releases_api
        if new_etag == "rotating":  # like GitHub: download counts change the ETag on every call
            return data, f'W/"{len(self.api_calls)}"'
        return (None, etag) if etag == new_etag else (data, new_etag)

    def fake_raw(self, repo, ref):
        r = self.repos.get(repo)
        path = r.work / "ROADMAP.md" if r else None
        return path.read_text(encoding="utf-8") if path and path.exists() else None

    def run_update(self, *args, now=NOW):
        self.configure(now)
        with mock.patch.object(sys, "argv", ["update.py", *args]), mock.patch("sys.stderr"):
            update.main()
        return self.read(".cache/events.json")

    def read(self, rel):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def overview_app(self, app_id="testcraft"):
        return next(a for a in self.read("data/overview.json")["apps"] if a["id"] == app_id)

    def history(self, app_id="testcraft"):
        return self.read(f"data/history/{app_id}.json")

    def base_history(self, repo="testcraft", start=NOW - timedelta(days=8)):
        self.repos[repo].commit(start, "Add roadmap", {"ROADMAP.md": roadmap(50), "src/main.rs": "fn main() {}\n"})

    # ---------- 1, 2: parity vs. roadmap updates ----------

    def test_parity_change_is_a_parity_event(self):
        self.base_history()
        self.assertEqual(self.run_update(), [])  # first build of an app never notifies
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(hours=3), "Raise the full target", {"ROADMAP.md": roadmap(52)})
        events = self.run_update()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["kind"], e["full_before"], e["full_after"]), ("parity", 50.0, 52.0))
        self.assertEqual(len(notify.notifiable(events)), 1)
        lc = self.overview_app()["parity"]["last_change"]
        self.assertEqual((lc["from"], lc["to"], lc["delta"]), (50.0, 52.0, 2.0))
        self.assertEqual(lc["ts"], (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_falling_value_is_a_parity_event_too(self):
        self.base_history()
        self.run_update()
        self.repos["testcraft"].commit(NOW - timedelta(hours=1), "Re-score", {"ROADMAP.md": roadmap(47)})
        events = self.run_update()
        self.assertEqual(events[0]["kind"], "parity")
        self.assertEqual(self.overview_app()["parity"]["last_change"]["delta"], -3.0)

    def test_roadmap_change_without_parity_change(self):
        self.base_history()
        self.run_update()
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(hours=5), "Progress log", {
            "ROADMAP.md": roadmap(50, progress=[("2026-10-18", "Layer masks")])})
        r.commit(NOW - timedelta(hours=4), "Reflow", {
            "ROADMAP.md": roadmap(50, progress=[("2026-10-18", "Layer   masks")], bold=False)})
        r.commit(NOW - timedelta(hours=3), "Mainstream estimate", {
            "ROADMAP.md": roadmap(50, mainstream=63, progress=[("2026-10-18", "Layer masks")])})
        events = self.run_update()
        self.assertEqual([e["kind"] for e in events], ["content", "editorial", "estimates"])
        self.assertEqual(notify.notifiable(events), [])  # nothing changes the full target: no notification
        app = self.overview_app()
        self.assertIsNone(app["parity"]["last_change"])
        self.assertEqual(app["roadmap"]["last_update"]["kind"], "estimates")
        self.assertEqual(app["roadmap"]["revisions_7d"], 2)  # the editorial revision does not count
        self.assertEqual(len(self.read("data/changelog.json")), 3)  # the change log keeps every revision

    def test_side_branch_states_are_not_revisions(self):
        self.base_history()
        self.run_update()
        r = self.repos["testcraft"]
        r.git("checkout", "-q", "-b", "feature")
        r.commit(NOW - timedelta(hours=6), "WIP: try 58", {"ROADMAP.md": roadmap(58)})
        r.commit(NOW - timedelta(hours=5), "WIP: settle on 51", {"ROADMAP.md": roadmap(51)})
        r.git("checkout", "-q", "main")
        stamp = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
        r.git("merge", "-q", "--no-ff", "-m", "Merge pull request #7 from someone/feature", "-m", "Settle the estimate",
              "feature",
              env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        r.git("push", "-q", "origin", "main")
        events = self.run_update()
        self.assertEqual([(e["full_before"], e["full_after"], e["message"]) for e in events],
                         [(50.0, 51.0, "Settle the estimate (#7)")])  # the PR title, not the merge subject
        self.assertEqual([h["full_pct"] for h in self.history()], [50.0, 51.0])

    # ---------- 3, 4, 5: repository activity ----------

    def test_single_readme_commit_after_a_pause_is_not_activity(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=20), "Add roadmap", {"ROADMAP.md": roadmap(50), "src/a.rs": "1"})
        r.commit(NOW - timedelta(hours=1), "README: typo", {"README.md": "# TestCraft\n"})
        self.run_update()
        act = self.overview_app()["activity"]
        self.assertEqual(act["level"], "cold")
        self.assertEqual(act["changes_14d"], 0)
        self.assertEqual(act["housekeeping_7d"], 1)
        self.assertEqual(act["last_code_change"]["subject"], "Add roadmap")

    def test_single_code_commit_after_a_pause_is_only_quiet(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=20), "Add roadmap", {"ROADMAP.md": roadmap(50), "src/a.rs": "1"})
        r.commit(NOW - timedelta(hours=1), "Fix crash", {"src/a.rs": "2"})
        self.run_update()
        self.assertEqual(self.overview_app()["activity"]["level"], "quiet")

    def test_steady_code_commits_are_active_and_more_are_buzzing(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=9), "Add roadmap", {"ROADMAP.md": roadmap(50)})
        for day in range(6):
            for i in range(10):
                r.commit(NOW - timedelta(days=day, minutes=i + 1), f"Feature {day}-{i}", {"src/a.rs": f"{day}{i}"})
        self.run_update()
        act = self.overview_app()["activity"]
        self.assertEqual((act["level"], act["active_days_7d"], act["changes_7d"]), ("active", 6, 60))
        for day in range(7):
            for i in range(25):
                r.commit(NOW - timedelta(days=day, minutes=30 + i), f"More {day}-{i}", {"src/b.rs": f"{day}{i}"})
        self.run_update()
        self.assertEqual(self.overview_app()["activity"]["level"], "buzzing")

    def test_many_trivial_commits_do_not_beat_substantial_ones(self):
        self.apps.append({"id": "othercraft", "name": "OtherCraft", "adobe": "Other", "category": "x",
                          "color": "#ff6a4d"})
        self.repos["othercraft"] = Repo(self.tmp, "othercraft")
        busy, real = self.repos["testcraft"], self.repos["othercraft"]
        busy.commit(NOW - timedelta(days=9), "Add roadmap", {"ROADMAP.md": roadmap(50)})
        real.commit(NOW - timedelta(days=9), "Add roadmap", {"ROADMAP.md": roadmap(40)})
        for i in range(80):
            busy.commit(NOW - timedelta(hours=i), f"docs {i}", {"docs/guide.md": str(i)})
            busy.commit(NOW - timedelta(hours=i, minutes=1), f"ci {i}", {".github/workflows/ci.yml": str(i)})
        for day in range(4):
            real.commit(NOW - timedelta(days=day), f"Feature {day}", {"src/core.rs": str(day)})
        self.run_update()
        busy_act = self.overview_app("testcraft")["activity"]
        real_act = self.overview_app("othercraft")["activity"]
        self.assertEqual(busy_act["level"], "cold")
        self.assertGreater(real_act["score"], busy_act["score"])

    def test_activity_decays_during_a_pause(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=9), "Add roadmap", {"ROADMAP.md": roadmap(50)})
        for day in range(7):
            for i in range(30):
                r.commit(NOW - timedelta(days=day, minutes=i), f"Work {day}-{i}", {"src/a.rs": f"{day}{i}"})
        levels = []
        for later in (0, 4, 8, 15):
            self.run_update(now=NOW + timedelta(days=later))
            act = self.overview_app()["activity"]
            levels.append((act["level"], act["score"]))
        self.assertEqual([lv for lv, _ in levels], ["buzzing", "active", "quiet", "cold"])
        scores = [s for _, s in levels]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_release_bump_is_not_code_activity_and_tags_are_releases(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=20), "Add roadmap", {"ROADMAP.md": roadmap(50), "Cargo.toml": "v1"})
        r.commit(NOW - timedelta(hours=2), "Release 0.9.0", {"Cargo.toml": "v2", "Cargo.lock": "x"})
        r.tag("v0.9.0", NOW - timedelta(hours=2))
        self.run_update()
        app = self.overview_app()
        self.assertEqual(app["activity"]["level"], "cold")
        self.assertEqual(app["release"]["tag"], "v0.9.0")
        events = self.read("data/overview.json")["latest_events"]
        self.assertIn(("release", "v0.9.0"), [(e["type"], e.get("tag")) for e in events])

    def test_github_releases_with_etag_and_semver_kinds(self):
        self.base_history()
        notes = ("## What's Changed\n* Fix A by @x in https://github.com/storytold/testcraft/pull/1\n"
                 "* Add B by @y in https://github.com/storytold/testcraft/pull/2\n\n## New Contributors\n"
                 "* @y made their first contribution in https://github.com/storytold/testcraft/pull/2\n")
        rel = lambda tag, ts, **kw: {"tag_name": tag, "name": f"TestCraft {tag}", "published_at": ts,
                                     "html_url": f"https://example.test/{tag}", "body": notes, "draft": False,
                                     "prerelease": False, "assets": [{"name": "t-macos.dmg"},
                                                                     {"name": "t-linux.AppImage"}], **kw}
        self.releases_api = ([rel("v0.10.0", "2026-10-18T04:00:00Z"), rel("v0.9.1", "2026-10-16T04:00:00Z"),
                              rel("v0.9.0", "2026-10-14T04:00:00Z"), rel("v1.0.0-rc1", "2026-10-17T04:00:00Z",
                                                                          prerelease=True),
                              rel("v0.8.0", "2026-10-12T04:00:00Z", draft=True)], 'W/"e1"')
        self.run_update()
        app = self.overview_app()
        self.assertEqual(app["release"]["tag"], "v0.10.0")  # the newest non-prerelease
        r = app["release"]
        self.assertEqual((r["kind"], r["changes"], r["new_contributors"], r["platforms"]),
                         ("minor", 2, 1, ["macOS", "Linux"]))
        detail = self.read("data/apps/testcraft.json")
        self.assertEqual([x["tag"] for x in detail["releases"]], ["v0.10.0", "v1.0.0-rc1", "v0.9.1", "v0.9.0"])
        self.assertEqual([x["kind"] for x in detail["releases"]], ["minor", "major", "patch", None])
        before = (self.root / "data/overview.json").read_bytes()
        self.run_update()  # second run sends the ETag, gets 304 and reuses the cached list
        self.assertEqual(self.api_calls[-1][1], 'W/"e1"')
        self.assertEqual((self.root / "data/overview.json").read_bytes(), before)

    def test_rotating_etag_does_not_change_committed_files(self):
        self.base_history()
        rel = {"tag_name": "v0.1.0", "name": "v0.1.0", "published_at": "2026-10-17T04:00:00Z", "html_url": "https://github.com/x",
               "body": "", "draft": False, "prerelease": False, "assets": []}
        self.releases_api = ([rel], "rotating")
        self.run_update()
        before = {p: p.read_bytes() for p in (self.root / "data").rglob("*.json")}
        self.run_update()
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / "data").rglob("*.json")})
        self.assertNotIn("etag", json.dumps(self.read("data/state.json")).lower())

    def test_reference_version_change_is_recorded(self):
        self.base_history()
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(hours=2), "Target 2027",
                 {"ROADMAP.md": roadmap(48).replace("Adobe Testshop 2026", "Adobe Testshop 2027")})
        self.run_update()
        h = self.history()[-1]
        self.assertEqual((h["target"], h["target_changed"]), ("Adobe Testshop 2027", "Adobe Testshop 2026"))

    def test_activity_ranking_is_reproducible(self):
        for name in ("othercraft", "thirdcraft"):
            self.apps.append({"id": name, "name": name.title(), "adobe": "x", "category": "x", "color": "#fff"})
            self.repos[name] = Repo(self.tmp, name)
        for n, (name, r) in enumerate(self.repos.items()):
            r.commit(NOW - timedelta(days=9), "Add roadmap", {"ROADMAP.md": roadmap(50)})
            for day in range(n + 1):
                r.commit(NOW - timedelta(days=day), f"Fix {day}", {"src/a.rs": str(day)})
        self.run_update()
        first = [(a["id"], a["activity"]["score"]) for a in self.read("data/overview.json")["apps"]]
        shutil.rmtree(self.root / ".cache")
        self.run_update()
        second = [(a["id"], a["activity"]["score"]) for a in self.read("data/overview.json")["apps"]]
        self.assertEqual(first, second)
        ranked = sorted(first, key=lambda x: -x[1])
        self.assertEqual([i for i, _ in ranked], ["thirdcraft", "othercraft", "testcraft"])

    # ---------- 6: incomplete history ----------

    def test_truncated_history_is_flagged(self):
        r = self.repos["testcraft"]
        for i, v in enumerate((40, 41, 42, 43)):
            r.commit(NOW - timedelta(days=5 - i), f"rev {i}", {"ROADMAP.md": roadmap(v)})
        self.run_update("--backfill", "2")
        self.assertTrue(self.overview_app()["parity"]["truncated"])
        self.assertEqual([h["full_pct"] for h in self.history()], [42.0, 43.0])

    def test_revisions_before_the_metric_existed_are_skipped(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=6), "Early roadmap", {"ROADMAP.md": "# Roadmap\n\nPlans only.\n"})
        r.commit(NOW - timedelta(days=4), "Add readiness", {"ROADMAP.md": roadmap(30)})
        self.run_update()
        app = self.overview_app()
        self.assertEqual(app["history_len"], 1)
        self.assertEqual(app["parity"]["tracked_since"][:10], (NOW - timedelta(days=4)).date().isoformat())
        self.assertFalse(app["parity"]["truncated"])

    # ---------- 7: discovery ----------

    def test_new_repository_is_discovered_without_roadmap_events(self):
        self.base_history()
        self.run_update()
        new = Repo(self.tmp, "newcraft")
        self.repos["newcraft"] = new
        for i, v in enumerate((20, 22, 25)):
            new.commit(NOW - timedelta(days=3 - i), f"rev {i}", {"ROADMAP.md": roadmap(v)})
        no_roadmap = Repo(self.tmp, "toolbox")
        self.repos["toolbox"] = no_roadmap
        no_roadmap.commit(NOW - timedelta(days=1), "init", {"README.md": "x"})
        self.remote_listing = [{"name": "testcraft"}, {"name": "newcraft", "default_branch": "main"},
                               {"name": "toolbox"}, {"name": "artcraft"}, {"name": "forked", "fork": True}]
        events = self.run_update()
        self.assertEqual([(e["type"], e["id"]) for e in events], [("new_app", "newcraft")])
        cfg = json.loads((self.root / "apps.json").read_text())
        self.assertEqual([a["id"] for a in cfg["apps"]], ["testcraft", "newcraft"])
        self.assertEqual(self.overview_app("newcraft")["full_pct"], 25.0)
        self.assertEqual(len(notify.notifiable(events)), 1)  # new apps always notify
        self.assertEqual(self.run_update(), [])  # and only once

    # ---------- 8, 9, 10: milestone ----------

    def test_milestone_first_reached_keeps_its_real_date(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=12), "rev", {"ROADMAP.md": roadmap(90)})
        reached = NOW - timedelta(days=10)
        r.commit(reached, "Cross 95", {"ROADMAP.md": roadmap(96)})
        self.run_update()
        ms = self.overview_app()["milestone"]
        self.assertEqual(ms["reached_at"], reached.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(ms["date_known"])
        # A later rebuild must not move the milestone to "now" (no false fresh celebration)
        self.run_update("--backfill", "100", now=NOW + timedelta(days=1))
        self.assertEqual(self.overview_app()["milestone"]["reached_at"], ms["reached_at"])
        events = self.read("data/overview.json")["latest_events"]
        self.assertEqual([e["ts"] for e in events if e["type"] == "milestone"], [ms["reached_at"]])

    def test_milestone_survives_a_drop_and_counts_crossings(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=6), "rev", {"ROADMAP.md": roadmap(94)})
        first = NOW - timedelta(days=5)
        r.commit(first, "up", {"ROADMAP.md": roadmap(95)})
        r.commit(NOW - timedelta(days=4), "down", {"ROADMAP.md": roadmap(93)})
        self.run_update()
        ms = self.overview_app()["milestone"]
        self.assertFalse(ms["above_now"])
        self.assertEqual(ms["crossings"], 1)
        r.commit(NOW - timedelta(days=1), "up again", {"ROADMAP.md": roadmap(97)})
        self.run_update()
        ms = self.overview_app()["milestone"]
        self.assertEqual((ms["above_now"], ms["crossings"]), (True, 2))
        self.assertEqual(ms["reached_at"], first.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_milestone_with_unknown_date(self):
        self.repos["testcraft"].commit(NOW - timedelta(days=2), "rev", {"ROADMAP.md": roadmap(96)})
        self.run_update()
        ms = self.overview_app()["milestone"]
        self.assertEqual((ms["reached_at"], ms["date_known"]), (None, False))
        self.assertNotIn("milestone", [e["type"] for e in self.read("data/overview.json")["latest_events"]])

    # ---------- 11, 12: backfill and several events in one scan ----------

    def test_backfill_and_schema_migration_never_notify(self):
        self.base_history()
        self.run_update()
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(hours=2), "Raise", {"ROADMAP.md": roadmap(55)})
        self.assertEqual(self.run_update("--backfill", "500"), [])
        self.assertEqual([h["full_pct"] for h in self.history()], [50.0, 55.0])
        r.commit(NOW - timedelta(hours=1), "Raise again", {"ROADMAP.md": roadmap(56)})
        state = self.read("data/state.json")
        state.pop("schema")  # data written by an older version
        (self.root / "data/state.json").write_text(json.dumps(state))
        self.assertEqual(self.run_update(), [])
        self.assertEqual(self.read("data/state.json")["schema"], update.SCHEMA)
        self.assertEqual(self.run_update(), [])  # next regular run: nothing new

    def test_several_events_in_one_scan_become_one_notification(self):
        self.base_history()
        self.run_update()
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(hours=3), "up", {"ROADMAP.md": roadmap(52)})
        r.commit(NOW - timedelta(hours=2), "log", {"ROADMAP.md": roadmap(52, progress=[("2026-10-18", "x")])})
        r.commit(NOW - timedelta(hours=1), "up", {"ROADMAP.md": roadmap(53, progress=[("2026-10-18", "x")])})
        events = self.run_update()
        self.assertEqual([e["kind"] for e in events], ["parity", "content", "parity"])
        title, body = notify.build(notify.notifiable(events))
        self.assertEqual(title, "ArtCraft Parity: 2 full-target changes")
        self.assertIn("50 % → 52 %", body)
        self.assertIn("52 % → 53 %", body)

    # ---------- regressions from the code review ----------

    def test_failed_migration_is_retried_and_never_mixes_formats(self):
        self.base_history()
        self.run_update()
        legacy = [{k: v for k, v in h.items() if k in ("commit", "commit_full", "commit_date", "message", "full_pct")}
                  for h in self.history()]
        (self.root / "data/history/testcraft.json").write_text(json.dumps(legacy))  # old-format points
        state = self.read("data/state.json")
        state.pop("schema")
        (self.root / "data/state.json").write_text(json.dumps(state))
        with mock.patch.object(update, "ensure_clone", side_effect=RuntimeError("network down")):
            self.assertEqual(self.run_update(), [])
        self.assertNotIn("schema", self.read("data/state.json"))  # migration not marked as done
        self.repos["testcraft"].commit(NOW - timedelta(hours=1), "Raise", {"ROADMAP.md": roadmap(51)})
        self.assertEqual(self.run_update(), [])  # retried as a rebuild: no crash, no events
        self.assertEqual(self.read("data/state.json")["schema"], update.SCHEMA)
        self.assertEqual([h["kind"] for h in self.history()], ["first", "parity"])

    def test_malformed_revision_is_skipped(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=3), "rev", {"ROADMAP.md": roadmap(40)})
        r.commit(NOW - timedelta(days=2), "typo", {"ROADMAP.md": roadmap(41).replace("2026-10-12", "2026-02-30")})
        real = update.parse_roadmap

        def strict(text):  # stands in for any parser crash on one old revision
            if "2026-02-30" in text:
                raise ValueError("bad date")
            return real(text)
        with mock.patch.object(update, "parse_roadmap", strict):
            self.run_update()
        self.assertEqual([h["full_pct"] for h in self.history()], [40.0])
        self.assertIn("could not be parsed", " ".join(self.read("data/overview.json")["errors"]))
        self.assertIsNone(update.parse_roadmap(roadmap(41).replace("2026-10-12", "2026-02-30"))["last_updated"])

    def test_rewritten_main_line_sends_no_events(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=3), "rev", {"ROADMAP.md": roadmap(40)})
        r.commit(NOW - timedelta(days=2), "rev 2", {"ROADMAP.md": roadmap(45)})
        self.run_update()
        r.git("reset", "-q", "--hard", "HEAD~1")
        r.commit(NOW - timedelta(hours=1), "rev 2, rewritten", {"ROADMAP.md": roadmap(47)}, force=True)
        self.assertEqual(self.run_update(), [])
        self.assertEqual([h["full_pct"] for h in self.history()], [40.0, 47.0])
        self.assertIn("no longer on main", " ".join(self.read("data/overview.json")["errors"]))

    def test_late_read_keeps_main_line_order(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=3), "a", {"ROADMAP.md": roadmap(40)})
        r.commit(NOW - timedelta(days=2), "b", {"ROADMAP.md": roadmap(41)})
        r.commit(NOW - timedelta(days=1), "c", {"ROADMAP.md": roadmap(42)})
        real = update.read_blob
        calls = {"n": 0}

        def flaky(gdir, oid):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient")
            return real(gdir, oid)
        with mock.patch.object(update, "read_blob", flaky):
            self.run_update()
        self.assertEqual([h["full_pct"] for h in self.history()], [40.0, 42.0])
        self.run_update()  # b is read now and goes into its place, not after c
        self.assertEqual([h["full_pct"] for h in self.history()], [40.0, 41.0, 42.0])
        self.assertEqual(self.overview_app()["full_pct"], 42.0)

    def test_empty_release_list_falls_back_to_tags(self):
        r = self.repos["testcraft"]
        r.commit(NOW - timedelta(days=2), "rev", {"ROADMAP.md": roadmap(40)})
        r.tag("v0.1.0", NOW - timedelta(days=1))
        self.releases_api = ([], 'W/"empty"')
        self.run_update()
        self.assertEqual((self.overview_app()["release"]["tag"], self.overview_app()["release"]["source"]),
                         ("v0.1.0", "tag"))

    def test_unchanged_run_writes_nothing(self):
        self.base_history()
        self.run_update()
        before = {p: p.read_bytes() for p in (self.root / "data").rglob("*.json")}
        self.run_update()
        after = {p: p.read_bytes() for p in (self.root / "data").rglob("*.json")}
        self.assertEqual(before, after)


class ClassifierTest(unittest.TestCase):
    def test_changeset_kinds(self):
        c = update.classify_changeset
        self.assertEqual(c(["README.md"], "README: typo"), "housekeeping")
        self.assertEqual(c(["ROADMAP.md"], "Progress log"), "housekeeping")
        self.assertEqual(c([".github/workflows/ci.yml", "Cargo.lock"], "CI"), "housekeeping")
        self.assertEqual(c([], "Merge pull request #1"), "housekeeping")
        self.assertEqual(c(["Cargo.toml", "Cargo.lock"], "Release 0.9.0"), "release")
        self.assertEqual(c(["Cargo.toml", "src/lib.rs"], "Release 0.9.0"), "code")
        self.assertEqual(c(["crates/app/src/lib.rs", "ROADMAP.md"], "Feature"), "code")
        self.assertEqual(c(["locales/de.ftl"], "German catalog"), "code")
        self.assertEqual(c(["tests/fixtures/a.svg"], "Add fixture"), "code")

    def test_platforms(self):
        assets = [{"name": "app_darwin_arm64.tar.gz"}, {"name": "app-0.1-windows-x64.msi"}, {"name": "app.AppImage"}]
        self.assertEqual(update.release_entry({"tag_name": "v1.0.0", "published_at": "2026-10-18T00:00:00Z",
                                               "assets": assets})["platforms"], ["macOS", "Windows", "Linux"])
        self.assertEqual(update.release_entry({"tag_name": "v1.0.0", "published_at": "2026-10-18T00:00:00Z",
                                               "assets": assets[:1]})["platforms"], ["macOS"])

    def test_python_310_safe_timestamps(self):
        self.assertEqual(update.utc_z("2026-10-17T02:19:47Z"), "2026-10-17T02:19:47Z")
        self.assertEqual(update.utc_z("2026-10-18T18:09:14+07:00"), "2026-10-18T11:09:14Z")


if __name__ == "__main__":
    unittest.main()
