"""Inventory completeness, format changes and source outage regressions."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update
from app_catalog import LAUNCHER_URL, DIRECTORY_URL, parse_launcher_catalog, parse_app_directory

SOURCE = (Path(__file__).parent / "fixtures/launcher-catalog.rs").read_text()


class CatalogTest(unittest.TestCase):
    def test_entire_real_launcher_catalog_includes_external_community_app(self):
        apps = parse_launcher_catalog(SOURCE)
        self.assertEqual(len(apps), 17)
        self.assertEqual(sum(a["kind"] == "official" for a in apps), 16)
        solid = next(a for a in apps if a["id"] == "solidcraft")
        self.assertEqual((solid["repo"], solid["kind"]), ("tangency-labs/solidcraft", "community"))
        self.assertEqual(next(a for a in apps if a["id"] == "craft-anything")["name"], "Craft Anything")

    def test_launcher_format_change_and_partial_parse_cannot_silently_drop_apps(self):
        for source in ("format changed", SOURCE.replace('"solidcraft",', 'ID_CONSTANT,', 1),
                       SOURCE.replace("[CraftApp; 17]", "[CraftApp; 18]")):
            with self.subTest(source=source[:40]), self.assertRaises(ValueError):
                parse_launcher_catalog(source)

    def test_app_directory_deduplicates_navigation_and_footer(self):
        apps = parse_app_directory('<a href="/apps/photocraft">PhotoCraft</a>'
                                   '<a href="/apps/photocraft">Again</a>'
                                   '<a href="/apps/craft-anything">Craft Anything</a>'
                                   '<a href="/apps">All</a>')
        self.assertEqual([a["id"] for a in apps], ["photocraft", "craft-anything"])
        with self.assertRaises(ValueError):
            parse_app_directory("<h1>Unavailable</h1>")

    def test_unavailable_catalog_reuses_saved_membership_and_reports_failure(self):
        state, errors = {}, []
        directory = '<a href="/apps/photocraft">PhotoCraft</a>'
        with mock.patch.object(update, "fetch_text", side_effect=lambda url: SOURCE if url == LAUNCHER_URL else directory):
            self.assertEqual(len(update.fetch_catalog(state, errors)), 17)
        saved = copy.deepcopy(state)
        with mock.patch.object(update, "fetch_text", side_effect=OSError("offline")):
            self.assertEqual(len(update.fetch_catalog(state, errors)), 17)
        self.assertEqual(state, saved)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("using saved catalog" in e for e in errors))

    def test_directory_additions_survive_older_launcher_catalog(self):
        with mock.patch.object(update, "fetch_text", side_effect=lambda url: SOURCE if url == LAUNCHER_URL else
                               '<a href="/apps/newcraft">NewCraft</a>'):
            apps = update.fetch_catalog({}, [])
        self.assertEqual(len(apps), 18)
        self.assertIn("newcraft", [a["id"] for a in apps])


if __name__ == "__main__":
    unittest.main()
