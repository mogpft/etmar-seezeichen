import json
import tempfile
import unittest
from pathlib import Path

from seamarks.cells import cell_name, cell_name_for_degrees, element, group_by_cell, write_cells
from seamarks.collect import E7, Item
from seamarks.query import matches_raw_filter, overpass_query, wanted_node, wanted_nwr


def e7(value):
    return round(value * E7)


def node(lat, lon, osm_id=1):
    return Item("node", osm_id, 1, {"seamark:type": "buoy_lateral"}, (e7(lat), e7(lat), e7(lon), e7(lon)))


class Selection(unittest.TestCase):
    def test_node_types_follow_the_app(self):
        for value in ["buoy_lateral", "beacon_cardinal", "light", "light_minor", "light_major",
                      "light_vessel", "lighthouse", "harbour", "anchorage"]:
            self.assertTrue(wanted_node({"seamark:type": value}), value)
        for value in ["light_float", "lights", "bridge", "wreck", "harbour_basin", "anchor_berth"]:
            self.assertFalse(wanted_node({"seamark:type": value}), value)

    def test_ways_and_relations_take_only_the_harbour_clauses(self):
        self.assertFalse(wanted_nwr({"seamark:type": "buoy_lateral"}))
        self.assertTrue(wanted_nwr({"seamark:type": "anchorage"}))
        self.assertTrue(wanted_nwr({"leisure": "marina"}))
        self.assertTrue(wanted_nwr({"harbour": "yes"}))
        self.assertFalse(wanted_nwr({"leisure": "park"}))
        self.assertFalse(wanted_nwr({"harbour": "no"}))

    def test_raw_filter_is_never_narrower_than_the_selection(self):
        for tags in [{"seamark:type": "light"}, {"leisure": "marina"}, {"harbour": "yes"}, {"seamark:type": "harbour"}]:
            if wanted_node(tags):
                self.assertTrue(matches_raw_filter("node", tags), tags)
            if wanted_nwr(tags):
                self.assertTrue(matches_raw_filter("way", tags), tags)

    def test_comparison_query_has_the_clauses_of_the_app(self):
        query = overpass_query(54.2, 10.0, 54.6, 10.6)
        self.assertIn('nwr["harbour"="yes"](54.2,10.0,54.6,10.6);', query)
        self.assertIn('node["seamark:type"~"^(buoy_|beacon_|light$|light_minor$|light_major$|light_vessel$|lighthouse$)"]', query)


class CellNames(unittest.TestCase):
    def test_names_round_towards_south_west(self):
        cases = [
            ((54.32, 10.14), "N54E010"), ((-33.86, 151.2), "S34E151"), ((38.97, -76.48), "N38W077"),
            ((-0.5, -0.5), "S01W001"), ((0.5, 0.5), "N00E000"), ((54.0, 10.0), "N54E010"),
            ((-34.0, -77.0), "S34W077"), ((90.0, 180.0), "N89E179"), ((-90.0, -180.0), "S90W180"),
        ]
        for (lat, lon), expected in cases:
            self.assertEqual(cell_name(2 * e7(lat), 2 * e7(lon)), expected, (lat, lon))

    def test_a_centre_just_south_of_a_line_stays_south(self):
        self.assertEqual(cell_name(2 * e7(54.0) - 1, 2 * e7(10.0)), "N53E010")

    def test_degree_names_pad(self):
        self.assertEqual(cell_name_for_degrees(5, 7), "N05E007")


class Elements(unittest.TestCase):
    def test_centre_is_the_middle_of_the_bounding_box(self):
        item = Item("way", 7, 1, {"leisure": "marina"}, (e7(54.0), e7(54.2), e7(10.0), e7(10.4)))
        self.assertEqual(element(item)["center"], {"lat": 54.1, "lon": 10.2})

    def test_node_shape_matches_overpass(self):
        self.assertEqual(sorted(element(node(54.5, 10.5))), ["id", "lat", "lon", "tags", "type"])

    def test_an_object_across_the_antimeridian_is_counted_not_misplaced(self):
        item = Item("way", 7, 1, {"leisure": "marina"}, (e7(-17), e7(-16), e7(-179.9), e7(179.9)))
        cells, skipped = group_by_cell([item, node(54.5, 10.5)])
        self.assertEqual(skipped, 1)
        self.assertEqual(list(cells), ["N54E010"])

    def test_files_and_index_agree_and_are_deterministic(self):
        items = [node(54.5, 10.5, 2), node(54.6, 10.6, 1), node(-33.8, 151.2, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            stats = write_cells(items, first, "2026-11-08T01:00:00Z", "2026-11-08T05:00:00Z")
            write_cells(list(reversed(items)), second, "2026-11-08T01:00:00Z", "2026-11-08T05:00:00Z")
            self.assertEqual((first / "N54E010.json").read_bytes(), (second / "N54E010.json").read_bytes())
            cell = json.loads((first / "N54E010.json").read_text())
            self.assertEqual(cell["osm3s"]["timestamp_osm_base"], "2026-11-08T01:00:00Z")
            self.assertEqual([e["id"] for e in cell["elements"]], [1, 2])
            index = json.loads((first / "index.json").read_text())
            self.assertEqual(index["elements"], 3)
            self.assertEqual(index["timestamp_osm_base"], "2026-11-08T01:00:00Z")
            self.assertIn("OpenStreetMap", index["license"])
            self.assertEqual({e["id"]: e["count"] for e in index["cells"]}, {"N54E010": 2, "S34E151": 1})
            self.assertEqual(index["cells"][0]["bytes"], (first / "N54E010.json").stat().st_size)
            self.assertEqual(stats["cells"], 2)
