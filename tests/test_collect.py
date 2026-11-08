import shutil
import tempfile
import unittest
from pathlib import Path

from seamarks.cells import element
from seamarks.collect import Item, Merged, read_filtered
from seamarks.rawfilter import OsmiumToolFilter, PyosmiumFilter
from tests.helpers import BUOY, MARINA, write_pbf


def collect(path):
    counters = {"no_position": 0}
    merged = Merged()
    for item in read_filtered(path, counters):
        merged.add(item)
    return merged, counters


class RawFilterAndCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def build(self):
        source = write_pbf(
            self.dir / "src.osm.pbf",
            nodes=[
                (1, 10.5, 54.5, BUOY),
                (2, 10.5, 54.5, {"amenity": "bench"}),            # unrelated, dropped
                (3, 10.0, 54.0, {}),                              # way corner
                (4, 10.4, 54.2, {"barrier": "gate"}),             # way corner with foreign tags
                (5, 11.0, 55.0, {}),                              # relation member way
                (6, 11.2, 55.4, {}),
                (7, 12.0, 56.0, {}),                              # relation member node
                (8, 10.1, 54.1, {"seamark:type": "wreck"}),       # seamark, not in the query
            ],
            ways=[
                (10, [3, 4], MARINA),
                (11, [5, 6], {"building": "yes"}),                # only referenced by the relation
                (12, [3, 5], {"highway": "path"}),                # unrelated, dropped
            ],
            relations=[(20, [("w", 11, "outer"), ("n", 7, "")], {"harbour": "yes"}),
                       (21, [("w", 12, "outer")], {"type": "route"})],
        )
        target = self.dir / "filtered.osm.pbf"
        PyosmiumFilter().run(source, target)
        return target

    def test_only_the_query_results_remain_with_overpass_centres(self):
        merged, counters = collect(self.build())
        self.assertEqual(set(merged.items), {("node", 1), ("way", 10), ("relation", 20)})
        self.assertEqual(element(merged.items[("way", 10)])["center"], {"lat": 54.1, "lon": 10.2})
        # Way 11 (55.0..55.4, 11.0..11.2) plus node 7 (56.0, 12.0).
        self.assertEqual(element(merged.items[("relation", 20)])["center"], {"lat": 55.5, "lon": 11.5})
        self.assertEqual(counters["no_position"], 0)

    @unittest.skipUnless(shutil.which("osmium"), "osmium-tool is not installed")
    def test_osmium_tool_and_the_stand_in_agree(self):
        stand_in = self.build()
        tool = self.dir / "tool.osm.pbf"
        OsmiumToolFilter().run(self.dir / "src.osm.pbf", tool)
        ours, theirs = collect(stand_in)[0].items, collect(tool)[0].items
        self.assertEqual(set(ours), set(theirs))
        for key in ours:
            self.assertEqual(element(ours[key]), element(theirs[key]))

    def test_foreign_tags_on_a_referenced_node_do_not_sneak_in(self):
        merged, _ = collect(self.build())
        self.assertNotIn(("node", 4), merged.items)

    def test_a_relation_without_located_members_is_counted(self):
        path = write_pbf(self.dir / "r.osm.pbf", relations=[(30, [("w", 999, "outer")], MARINA)])
        merged, counters = collect(path)
        self.assertEqual(merged.items, {})
        self.assertEqual(counters["no_position"], 1)

    def test_osmium_tool_command_keeps_references_and_all_clauses(self):
        command = OsmiumToolFilter("osmium").command(Path("a.pbf"), Path("b.pbf"))
        self.assertEqual(command[:2], ["osmium", "tags-filter"])
        self.assertNotIn("-R", command)
        self.assertNotIn("--omit-referenced", command)
        for clause in ["n/seamark:type", "nwr/leisure=marina", "nwr/harbour=yes", "nwr/seamark:type=harbour,anchorage"]:
            self.assertIn(clause, command)


class Merging(unittest.TestCase):
    def test_a_clipped_relation_gets_the_union_of_its_boxes(self):
        merged = Merged()
        merged.add(Item("relation", 5, 3, {"leisure": "marina"}, (540000000, 541000000, 100000000, 101000000)))
        merged.add(Item("relation", 5, 3, {"leisure": "marina"}, (541000000, 542000000, 101000000, 102000000)))
        self.assertEqual(element(merged.items[("relation", 5)])["center"], {"lat": 54.1, "lon": 10.1})
        self.assertEqual(len(merged.items), 1)

    def test_the_newest_version_of_a_node_wins_whichever_region_comes_first(self):
        old = Item("node", 5, 2, {"a": "old"}, (10, 10, 20, 20))
        new = Item("node", 5, 3, {"a": "new"}, (11, 11, 21, 21))
        for order in ([old, new], [new, old]):
            merged = Merged()
            for item in order:
                merged.add(Item(item.kind, item.osm_id, item.version, item.tags, (item.south, item.north, item.west, item.east)))
            self.assertEqual(merged.items[("node", 5)].tags, {"a": "new"})
            self.assertEqual(merged.items[("node", 5)].south, 11)
