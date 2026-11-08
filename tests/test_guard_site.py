import json
import tempfile
import unittest
from pathlib import Path

from seamarks import site
from seamarks.cells import write_cells
from seamarks.collect import Item
from seamarks.guard import evaluate

REPO = Path(__file__).resolve().parent.parent


def judge(elements, previous=1000, regions=("a", "b"), previous_regions=("a", "b"), parents=(), accept=False):
    return evaluate({"elements": previous}, {"region_ids": list(previous_regions)}, elements, regions, parents, accept)


class Guard(unittest.TestCase):
    def test_exactly_five_percent_passes(self):
        self.assertEqual(judge(950).status, "ok")

    def test_one_more_is_rejected(self):
        result = judge(949)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.allows_publishing)

    def test_growth_passes(self):
        self.assertEqual(judge(5000).status, "ok")

    def test_a_vanished_region_is_rejected_even_with_more_objects(self):
        result = judge(1200, regions=("a",))
        self.assertEqual(result.status, "rejected")
        self.assertIn("b", result.problems[0])

    def test_a_region_that_was_split_is_not_missing(self):
        self.assertEqual(judge(1000, regions=("a", "b-north", "b-south"), parents=("b",)).status, "ok")

    def test_no_baseline_is_not_publishable_unless_accepted(self):
        self.assertEqual(evaluate(None, None, 100, [], [], False).status, "no-baseline")
        self.assertFalse(evaluate(None, None, 100, [], [], False).allows_publishing)
        self.assertTrue(evaluate(None, None, 100, [], [], True).allows_publishing)

    def test_a_missing_previous_report_fails_closed(self):
        self.assertEqual(evaluate({"elements": 10}, None, 100, ["a"], [], False).status, "rejected")

    def test_accepting_overrides_a_real_drop(self):
        self.assertTrue(judge(10, accept=True).allows_publishing)


class Site(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "site"
        items = [Item("node", 1, 1, {"seamark:type": "light"}, (545000000, 545000000, 105000000, 105000000)),
                 Item("node", 2, 1, {"seamark:type": "light"}, (-338000000, -338000000, 1512000000, 1512000000))]
        write_cells(items, self.root / "v1", "2026-11-08T01:00:00Z", "2026-11-08T05:00:00Z")
        site.assemble(self.root, REPO / "DATA-LICENSE.txt")
        (self.root / "v1" / "report.json").write_text(json.dumps({"partial": False}))

    def test_a_complete_directory_passes(self):
        self.assertEqual(site.validate(self.root), [])

    def test_a_partial_build_cannot_be_published(self):
        (self.root / "v1" / "report.json").write_text(json.dumps({"partial": True}))
        self.assertTrue(any("partial" in p for p in site.validate(self.root)))
        self.assertEqual(site.validate(self.root, allow_partial=True), [])

    def test_a_report_without_the_flag_counts_as_partial(self):
        (self.root / "v1" / "report.json").write_text("{}")
        self.assertTrue(site.validate(self.root))

    def test_a_missing_cell_is_found(self):
        (self.root / "v1" / "N54E010.json").unlink()
        self.assertTrue(any("missing" in p for p in site.validate(self.root)))

    def test_an_unlisted_cell_is_found(self):
        (self.root / "v1" / "N01E001.json").write_text("{}")
        self.assertTrue(any("not in the index" in p for p in site.validate(self.root)))

    def test_a_truncated_cell_is_found(self):
        path = self.root / "v1" / "N54E010.json"
        path.write_bytes(path.read_bytes()[:-1])
        self.assertTrue(any("size" in p for p in site.validate(self.root)))

    def test_too_many_files(self):
        self.assertEqual(site.MAX_FILES, 20000)
        self.assertTrue(site.check_limits(self.root, max_files=2))
        self.assertEqual(site.check_limits(self.root), [])

    def test_too_large_a_file(self):
        self.assertEqual(site.MAX_FILE_BYTES, 25 * 1024 * 1024)
        with open(self.root / "v1" / "big.bin", "wb") as f:
            f.truncate(site.MAX_FILE_BYTES + 1)
        self.assertTrue(any("big.bin" in p for p in site.check_limits(self.root)))

    def test_header_rules_do_not_overlap(self):
        rules = [line for line in site.HEADERS.splitlines() if line.startswith("/")]
        self.assertEqual(rules, ["/v1/index.json", "/v1/N*", "/v1/S*"])
        self.assertEqual(site.HEADERS.count("Content-Type: application/json"), 3)
        self.assertTrue((self.root / "_headers").is_file())
        self.assertTrue((self.root / ".nojekyll").is_file())
