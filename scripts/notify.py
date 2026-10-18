#!/usr/bin/env python3
"""Sends a notification for the events of the last update run (.cache/events.json).

Only new apps and roadmap revisions that change the full-target value notify. Every other revision
is still recorded in data/changelog.json by update.py, but does not open an issue.

Channels:
  1. GitHub issue in this repository that mentions the account from apps.json ("notify").
     The mention makes GitHub send an email notification to that account.
  2. Optional push notification via ntfy.sh, if the NTFY_TOPIC environment variable is set.

Environment:
  GH_TOKEN      token for the GitHub CLI (set to secrets.GITHUB_TOKEN in the workflow)
  GITHUB_REPOSITORY  set automatically by GitHub Actions
  NTFY_TOPIC    optional ntfy topic name (kept as a repository secret)
  DRY_RUN=1     print the message instead of sending it

If sending fails, the script exits with an error. The workflow run then shows as failed,
which also triggers a GitHub notification.
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
EVENTS = ROOT / ".cache" / "events.json"
REPO = os.environ.get("GITHUB_REPOSITORY", "KollektionHannemann/artcraft-parity")
OWNER, REPO_NAME = REPO.split("/", 1)
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def pct(v) -> str:
    return f"{v:g} %" if v is not None else "–"


def full_target_changed(e: dict) -> bool:
    return e.get("full_before") != e.get("full_after")


def notifiable(events: list) -> list:
    return [e for e in events
            if e["type"] == "new_app" or (e["type"] == "roadmap_update" and full_target_changed(e))]


def build(events: list):
    new = [e for e in events if e["type"] == "new_app"]
    upd = [e for e in events if e["type"] == "roadmap_update"]

    parts = []
    if new:
        parts.append(plural(len(new), "new app"))
    if upd:
        parts.append(plural(len(upd), "full-target change"))
    title = "ArtCraft Parity: " + ", ".join(parts)

    account = CONFIG.get("notify", OWNER)
    lines = [f"@{account}, changes were detected in the storytold repositories.", ""]
    if new:
        lines.append("### New apps")
        for e in new:
            lines.append(f"- **{e['name']}** (reference: {e['adobe']}), added to tracking as `{e['id']}`")
        lines.append("")
    if upd:
        lines.append("### Full-target changes")
        for e in upd:
            before, after = e.get("full_before"), e.get("full_after")
            change = f"{pct(before)} → {pct(after)}"
            stage = f" · {e['stage']}" if e.get("stage") else ""
            lines.append(f"- **{e['name']}** · {e['date']} · {change}{stage} · "
                         f"[`{e['commit']}`]({e['commit_url']}) {e['message']}")
        lines.append("")
    lines.append(f"Dashboard: https://{OWNER.lower()}.github.io/{REPO_NAME}/")
    return title, "\n".join(lines)


def send_issue(title: str, body: str):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        path = f.name
    subprocess.run(["gh", "issue", "create", "-R", REPO, "--title", title, "--body-file", path],
                   check=True)


def send_ntfy(topic: str, title: str, body: str):
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Markdown": "yes", "Click": f"https://github.com/{REPO}/issues"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def main():
    events = json.loads(EVENTS.read_text(encoding="utf-8")) if EVENTS.exists() else []
    if not events:
        print("no events in this run, nothing to send")
        return
    total = len(events)
    events = notifiable(events)
    if not events:
        print(f"{total} events in this run, none changes the full target, nothing to send")
        return
    title, body = build(events)
    if DRY_RUN:
        print(title)
        print()
        print(body)
        return
    sent = False
    if os.environ.get("GH_TOKEN"):
        send_issue(title, body)
        print("GitHub issue created (mention notification sent)")
        sent = True
    if os.environ.get("NTFY_TOPIC"):
        send_ntfy(os.environ["NTFY_TOPIC"], title, body)
        print("ntfy notification sent")
        sent = True
    if not sent:
        print("no notification channel configured (GH_TOKEN or NTFY_TOPIC)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
