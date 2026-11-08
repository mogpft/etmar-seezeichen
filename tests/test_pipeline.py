"""Whole builds on tiny real PBF files, served over a real local HTTP server."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from seamarks.pipeline import Config, run
from seamarks.site import validate
from tests.helpers import BUOY, MARINA, write_pbf


class Mirror:
    """Serves an index and some PBF files; records and counts every request."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.requests = []
        self.gone = set()
        self.source_alive_during_later_request = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                name = self.path.lstrip("/")
                if name == "index.json":
                    body = json.dumps(outer.index).encode()
                elif name in outer.gone or not (outer.directory / name).is_file():
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                else:
                    body = (outer.directory / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.index = {"features": [
            {"properties": {"id": "north", "parent": "world", "urls": {"pbf": f"{self.base}/north.osm.pbf"}}},
            {"properties": {"id": "south", "parent": "world", "urls": {"pbf": f"{self.base}/south.osm.pbf"}}},
            {"properties": {"id": "world", "urls": {"pbf": f"{self.base}/world.osm.pbf"}}},
        ]}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.www = self.dir / "www"
        self.www.mkdir()
        # A buoy both regions contain, one each of their own, and a marina way
        # whose nodes each extract knows only half of.
        write_pbf(self.www / "north.osm.pbf",
                  nodes=[(1, 10.5, 54.5, BUOY), (2, 10.6, 54.6, BUOY), (5, 10.0, 54.0, {})],
                  relations=[(9, [("n", 5, "")], MARINA)], timestamp="2026-11-08T03:00:00Z")
        write_pbf(self.www / "south.osm.pbf",
                  nodes=[(1, 10.5, 54.5, BUOY), (3, 151.2, -33.8, BUOY), (6, 10.4, 54.2, {})],
                  relations=[(9, [("n", 6, "")], MARINA)], timestamp="2026-11-08T01:00:00Z")
        self.mirror = Mirror(self.www)
        self.addCleanup(self.mirror.close)
        self.out = self.dir / "out"
        self.work = self.dir / "work"

    def config(self, **kwargs):
        return Config(out=self.out, workdir=self.work, raw_filter="pyosmium",
                      index_url=f"{self.mirror.base}/index.json", sleep=lambda _: None, **kwargs)

    def previous(self, elements, regions=("north", "south")):
        directory = self.dir / "previous"
        directory.mkdir(exist_ok=True)
        (directory / "index.json").write_text(json.dumps({"elements": elements}))
        (directory / "report.json").write_text(json.dumps({"region_ids": list(regions)}))
        return directory

    def report(self):
        return json.loads((self.out / "report.json").read_text())

    def test_a_good_build_is_complete_and_consistent(self):
        self.assertEqual(run(self.config(previous=self.previous(4))), 0)
        site = self.out / "site"
        self.assertEqual(validate(site), [])
        index = json.loads((site / "v1" / "index.json").read_text())
        self.assertEqual(index["elements"], 4)  # buoy deduplicated across regions
        # The oldest extract speaks for the whole build.
        self.assertEqual(index["timestamp_osm_base"], "2026-11-08T01:00:00Z")
        report = self.report()
        self.assertFalse(report["partial"])
        self.assertEqual(report["guard"]["status"], "ok")
        self.assertEqual(report["region_ids"], ["north", "south"])
        cell = json.loads((site / "v1" / "N54E010.json").read_text())
        relation = [e for e in cell["elements"] if e["type"] == "relation"][0]
        self.assertEqual(relation["center"], {"lat": 54.1, "lon": 10.2})  # both halves united

    def test_sources_are_gone_after_the_run(self):
        self.assertEqual(run(self.config(previous=self.previous(4))), 0)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_downloads_are_strictly_one_after_the_other_and_counted(self):
        run(self.config(previous=self.previous(4)))
        self.assertEqual(self.mirror.requests, ["/index.json", "/north.osm.pbf", "/south.osm.pbf"])
        client = self.report()["client"]
        self.assertEqual(client["pause_seconds"], 10.0)  # before each file, none before the index
        self.assertIn("throttle_wait_seconds", client)
        self.assertIn("runtime_seconds", self.report())

    def test_a_missing_region_publishes_nothing(self):
        self.mirror.gone.add("south.osm.pbf")
        self.assertEqual(run(self.config(previous=self.previous(4))), 1)
        self.assertFalse((self.out / "site").exists())
        self.assertIn("south", self.report()["failure"])
        self.assertEqual(list(self.work.glob("*.pbf")), [])

    def test_a_drop_over_five_percent_publishes_nothing(self):
        self.assertEqual(run(self.config(previous=self.previous(5))), 1)  # 4 of 5
        self.assertFalse((self.out / "site").exists())
        self.assertTrue((self.out / "site.rejected" / "v1" / "index.json").exists())
        self.assertEqual(self.report()["guard"]["status"], "rejected")

    def test_a_drop_can_be_accepted_on_purpose(self):
        self.assertEqual(run(self.config(previous=self.previous(5), accept_baseline=True)), 0)
        self.assertTrue((self.out / "site").exists())

    def test_a_region_vanished_from_the_index_publishes_nothing(self):
        self.assertEqual(run(self.config(previous=self.previous(4, ("north", "south", "east")))), 1)
        self.assertFalse((self.out / "site").exists())

    def test_require_baseline_fails_before_any_download(self):
        self.assertEqual(run(self.config(require_baseline=True)), 1)
        self.assertEqual(self.mirror.requests, [])
        self.assertIn("previous publication", self.report()["failure"])

    def test_without_a_baseline_a_trial_run_still_builds(self):
        self.assertEqual(run(self.config()), 0)
        self.assertEqual(self.report()["guard"]["status"], "no-baseline")

    def test_a_chosen_subset_is_marked_partial_and_unpublishable(self):
        self.assertEqual(run(self.config(only=["north"])), 0)
        self.assertTrue(self.report()["partial"])
        self.assertTrue(any("partial" in p for p in validate(self.out / "site")))

    def test_local_sources_are_never_deleted(self):
        source = self.www / "north.osm.pbf"
        self.assertEqual(run(self.config(local_pbf={"north": source})), 0)
        self.assertTrue(source.exists())

    def test_running_out_of_time_is_red(self):
        self.assertEqual(run(self.config(previous=self.previous(4), max_runtime_seconds=-1)), 1)
        self.assertFalse((self.out / "site").exists())
        self.assertIn("out of time", self.report()["failure"])

    def test_a_region_without_timestamp_is_red(self):
        write_pbf(self.www / "south.osm.pbf", nodes=[(3, 151.2, -33.8, BUOY)], timestamp="")
        self.assertEqual(run(self.config(previous=self.previous(4))), 1)
        self.assertIn("timestamp", self.report()["failure"])
