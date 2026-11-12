"""Read authoritative app inventories without executing upstream source code."""
import json
import re
from html.parser import HTMLParser

LAUNCHER_URL = "https://raw.githubusercontent.com/storytold/craft-launcher/main/crates/core/catalog.toml"
DIRECTORY_URL = "https://getartcraft.com/apps"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


LAUNCHER_KEYS = ("id", "name", "repo", "kind", "tagline", "color")
TABLE_HEADER = re.compile(r"\[\[?\s*([A-Za-z0-9_.-]+)\s*\]\]?")


def parse_launcher_catalog(source):
    """Read the top-level keys of each [[apps]] table in the launcher's catalog.toml.

    Python 3.10 has no tomllib, so this reads only the single-line string and integer-array keys the
    tracker needs and skips sub-tables like [apps.details]. Every app must carry all of them, so a
    format change raises instead of silently losing apps.
    """
    entries, table = [], None
    for line in source.splitlines():
        line = line.strip()
        header = TABLE_HEADER.fullmatch(line)
        if header:
            table = {} if line.startswith("[[") and header[1] == "apps" else None
            if table is not None:
                entries.append(table)
            continue
        key = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if table is not None and key and key[1] in LAUNCHER_KEYS:
            try:
                table[key[1]] = json.loads(key[2])
            except json.JSONDecodeError:
                raise ValueError(f"launcher catalog value for {key[1]} not recognized") from None
    if not entries:
        raise ValueError("launcher catalog contains no [[apps]] tables")
    apps = []
    for app in entries:
        if set(LAUNCHER_KEYS) - set(app) - {"color"}:
            raise ValueError("launcher catalog app is missing id, name, repo, kind or tagline")
        app_id, repo, kind = app["id"], app["repo"], app["kind"]
        if not (isinstance(app_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", app_id)
                and isinstance(repo, str) and REPOSITORY.fullmatch(repo) and kind in ("official", "community")):
            raise ValueError("invalid launcher app identity")
        rgb = app.get("color")
        color = ({"color": "#" + "".join(f"{c:02x}" for c in rgb)}
                 if isinstance(rgb, list) and len(rgb) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in rgb)
                 else {})
        apps.append({"id": app_id, "name": app["name"], "repo": repo, "category": app["tagline"],
                     "kind": kind, **color})
    if len({a["id"].lower() for a in apps}) != len(apps):
        raise ValueError("launcher catalog contains duplicate app IDs")
    return apps


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
