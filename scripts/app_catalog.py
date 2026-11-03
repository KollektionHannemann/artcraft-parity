"""Read authoritative app inventories without executing upstream source code."""
import json
import re
from html.parser import HTMLParser

LAUNCHER_URL = "https://raw.githubusercontent.com/storytold/craft-launcher/main/crates/core/src/catalog.rs"
DIRECTORY_URL = "https://getartcraft.com/apps"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def parse_launcher_catalog(source):
    """Validate the declared SUITE count, so a format change cannot silently lose apps."""
    # Strip comments while preserving quoted strings (including URLs and escaped quotes).
    source = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                    lambda m: m[0] if m[0].startswith('"') else " ", source)
    suite = re.search(r"pub\s+const\s+SUITE\s*:\s*\[CraftApp;\s*(\d+)\s*\]\s*=\s*\[([\s\S]*?)\n\];", source)
    if not suite:
        raise ValueError("launcher SUITE declaration not recognized")
    calls = re.finditer(r'\bapp\s*\(\s*("(?:\\.|[^"\\])*")\s*,\s*("(?:\\.|[^"\\])*")'
                        r'\s*,\s*("(?:\\.|[^"\\])*")\s*,\s*\[([\s\S]*?)\]\s*,\s*"([\w.-]+/[\w.-]+)"\s*,?\s*\)',
                        suite[2])
    entries = []
    for call in calls:
        app_id, name, category = (json.loads(call[i]) for i in (1, 2, 3))
        repo = call[5]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", app_id) or not REPOSITORY.fullmatch(repo):
            raise ValueError("invalid launcher app identity")
        rgb = re.findall(r"0x([a-fA-F0-9]{2})", call[4])[:3]
        entries.append({"id": app_id, "name": name, "repo": repo, "category": category,
                        "kind": "official" if repo.startswith("storytold/") else "community",
                        **({"color": "#" + "".join(rgb)} if len(rgb) == 3 else {})})
    if not entries or len(entries) != int(suite[1]) or len({e["id"] for e in entries}) != len(entries):
        raise ValueError("launcher catalog incomplete or contains duplicate app IDs")
    return entries


class AppDirectory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.apps = {}

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        match = re.fullmatch(r"(?:https://getartcraft.com)?/apps/([a-z0-9-]+)/?", href)
        if tag == "a" and match:
            app_id = match[1]
            name = "Craft Anything" if app_id == "craft-anything" else app_id.removesuffix("craft").capitalize() + "Craft"
            self.apps[app_id] = {"id": app_id, "name": name, "repo": f"storytold/{app_id}", "kind": "official"}


def parse_app_directory(source):
    directory = AppDirectory()
    directory.feed(source)
    if not directory.apps:
        raise ValueError("official app directory contains no recognized app links")
    return list(directory.apps.values())
