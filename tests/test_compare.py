import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import compare
from seamarks.cells import write_cells
from seamarks.collect import E7, Item

BOX = (54.2, 10.0, 54.6, 10.6)
BASE = "2026-11-08T01:00:00Z"


def node(osm_id, lat, lon, tags=None):
    v = (round(lat * E7), round(lat * E7), round(lon * E7), round(lon * E7))
    return Item("node", osm_id, 1, tags or {"seamark:type": "light"}, v)


def answer_node(osm_id, lat, lon, tags=None, **extra):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags or {"seamark:type": "light"}, **extra}


class CompareBox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cells = Path(self.tmp.name)
        write_cells([node(1, 54.3, 10.1), node(2, 54.4, 10.2), node(3, 54.5, 10.3), node(6, 54.35, 10.15)],
                    self.cells, BASE, BASE)

    def run_box(self, elements):
        return compare.compare_box("kiel", BOX, self.cells, {"elements": elements})

    def test_identical_sides(self):
        result = self.run_box([answer_node(1, 54.3, 10.1), answer_node(2, 54.4, 10.2),
                               answer_node(3, 54.5, 10.3), answer_node(6, 54.35, 10.15)])
        self.assertEqual((result["common"], result["unexplained"], result["differences"]), (4, 0, []))

    def test_a_recently_added_object_is_explained(self):
        result = self.run_box([answer_node(1, 54.3, 10.1), answer_node(2, 54.4, 10.2), answer_node(3, 54.5, 10.3),
                               answer_node(6, 54.35, 10.15), answer_node(4, 54.45, 10.25, timestamp="2026-11-08T10:00:00Z")])
        self.assertEqual(result["only_overpass"], 1)
        self.assertEqual(result["unexplained"], 0)

    def test_an_old_object_missing_from_the_cells_is_not_explained(self):
        result = self.run_box([answer_node(1, 54.3, 10.1), answer_node(2, 54.4, 10.2), answer_node(3, 54.5, 10.3),
                               answer_node(6, 54.35, 10.15), answer_node(4, 54.45, 10.25, timestamp="2020-01-01T00:00:00Z")])
        self.assertEqual(result["unexplained"], 1)

    def test_an_object_only_in_the_cells_stays_listed(self):
        result = self.run_box([answer_node(1, 54.3, 10.1), answer_node(2, 54.4, 10.2), answer_node(3, 54.5, 10.3)])
        self.assertEqual(result["only_cells"], 1)
        self.assertEqual(result["unexplained"], 1)
        self.assertEqual(result["differences"][0]["object"], "node/6")

    def test_a_way_centred_outside_the_box_is_not_counted(self):
        way = {"type": "way", "id": 9, "center": {"lat": 54.7, "lon": 10.1}, "tags": {"leisure": "marina"}}
        result = self.run_box([answer_node(1, 54.3, 10.1), answer_node(2, 54.4, 10.2), answer_node(3, 54.5, 10.3),
                               answer_node(6, 54.35, 10.15), way])
        self.assertEqual((result["reaching_in"], result["only_overpass"], result["unexplained"]), (1, 0, 0))

    def test_moved_and_retagged_objects_are_reported(self):
        result = self.run_box([answer_node(1, 54.301, 10.1), answer_node(2, 54.4, 10.2, {"seamark:type": "buoy_safe_water"}),
                               answer_node(3, 54.5, 10.3), answer_node(6, 54.35, 10.15)])
        self.assertEqual(sorted(d["kind"] for d in result["differences"]), ["moved", "tags differ"])
        self.assertEqual(result["tag_differences"], 1)
        self.assertGreater(result["largest_move_m"], 100)

    def test_a_box_across_a_cell_border_reads_both_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cells([node(1, 53.9, 9.5), node(2, 54.1, 10.5)], Path(tmp), BASE, BASE)
            found, stamp = compare.load_cells(Path(tmp), (53.5, 9.0, 54.5, 11.0))
            self.assertEqual(len(found), 2)
            self.assertEqual(stamp, BASE)


class QueryOverpass(unittest.TestCase):
    def serve(self, script):
        bodies = []
        replies = list(script)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                status, body = replies.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/", bodies

    def test_retries_when_overloaded_and_keeps_no_usernames(self):
        answer = {"elements": [answer_node(1, 54.3, 10.1, user="someone", uid=7, timestamp="2026-11-08T10:00:00Z")]}
        url, bodies = self.serve([(502, b"busy"), (200, json.dumps(answer).encode())])
        with mock.patch.object(compare.time, "sleep") as sleep:
            result = compare.query_overpass(url, BOX)
        sleep.assert_called_once_with(60)
        self.assertEqual(len(bodies), 2)
        self.assertIn("leisure", bodies[0])
        self.assertNotIn("user", result["elements"][0])
        self.assertNotIn("uid", result["elements"][0])
        self.assertIn("timestamp", result["elements"][0])

    def test_a_query_is_never_repeated_more_than_twice(self):
        url, bodies = self.serve([(502, b"busy")] * 3)
        with mock.patch.object(compare.time, "sleep"):
            with self.assertRaises(Exception):
                compare.query_overpass(url, BOX)
        self.assertEqual(len(bodies), 3)

    def test_a_remark_is_a_failure(self):
        url, _ = self.serve([(200, json.dumps({"elements": [], "remark": "runtime error"}).encode())])
        with self.assertRaises(RuntimeError):
            compare.query_overpass(url, BOX)
