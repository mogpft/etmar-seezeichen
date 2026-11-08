import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from seamarks import geofabrik
from seamarks.geofabrik import DownloadFailed, PoliteClient, Throttled, leaf_regions, parse_retry_after


def feature(id_, parent=None):
    return {"properties": {"id": id_, "parent": parent, "urls": {"pbf": f"https://x/{id_}.pbf"}}}


class Regions(unittest.TestCase):
    def test_only_leaves_without_the_redundant_aggregates(self):
        index = {"features": [feature("europe"), feature("germany", "europe"), feature("bayern", "germany"),
                              feature("malta", "europe"), feature("dach", "europe"), feature("us", "north-america"),
                              feature("great-britain", "europe"), feature("antarctica")]}
        self.assertEqual([r.id for r in leaf_regions(index)], ["antarctica", "bayern", "dach", "malta"])


class RetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_retry_after("120"), 120.0)

    def test_http_date(self):
        now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("Sun, 08 Nov 2026 12:01:30 GMT", now), 90.0)

    def test_nonsense(self):
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))


class Server:
    """Plays a scripted list of responses; (status, headers, body)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("User-Agent")))
                status, headers, body = outer.script.pop(0) if outer.script else (500, {}, b"")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/file"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Polite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "f.pbf"
        self.waits = []

    def client(self, **kwargs):
        return PoliteClient(sleep=self.waits.append, **kwargs)

    def serve(self, script):
        server = Server(script)
        self.addCleanup(server.close)
        return server

    def test_honours_retry_after_and_counts_the_wait(self):
        server = self.serve([(429, {"Retry-After": "7"}, b""), (200, {}, b"data")])
        client = self.client()
        self.assertEqual(client.download(server.url, self.target), 4)
        self.assertEqual(self.target.read_bytes(), b"data")
        self.assertEqual(self.waits, [7.0])
        self.assertEqual(client.stats.throttle_events, 1)
        self.assertEqual(client.stats.throttle_wait_seconds, 7.0)
        self.assertIn("etmar-seezeichen", server.seen[0][1])

    def test_503_without_a_header_backs_off_exponentially(self):
        server = self.serve([(503, {}, b""), (503, {}, b""), (200, {}, b"x")])
        self.client().download(server.url, self.target)
        self.assertEqual(self.waits, [60.0, 120.0])

    def test_lasting_throttling_is_a_failure(self):
        server = self.serve([(429, {}, b"")] * 8)
        with self.assertRaises(Throttled):
            self.client(max_backoff_seconds=1).download(server.url, self.target)
        self.assertFalse(self.target.exists())

    def test_an_absurd_retry_after_is_a_failure_not_a_day_of_waiting(self):
        server = self.serve([(429, {"Retry-After": "86400"}, b"")])
        with self.assertRaises(Throttled):
            self.client().download(server.url, self.target)
        self.assertEqual(self.waits, [])

    def test_total_throttle_budget_is_enforced(self):
        server = self.serve([(429, {"Retry-After": "60"}, b"")] * 3 + [(200, {}, b"x")])
        with self.assertRaises(Throttled):
            self.client(max_total_throttle_seconds=100).download(server.url, self.target)

    def test_not_found_fails_at_once(self):
        server = self.serve([(404, {}, b"")])
        with self.assertRaises(DownloadFailed):
            self.client().download(server.url, self.target)
        self.assertEqual(len(server.seen), 1)

    def test_a_truncated_body_is_retried_and_leaves_no_file(self):
        server = self.serve([(200, {"Content-Length": "100"}, b"short"), (200, {}, b"complete")])
        client = self.client()
        client.download(server.url, self.target)
        self.assertEqual(self.target.read_bytes(), b"complete")
        self.assertEqual(client.stats.error_retries, 1)
        self.assertEqual(list(Path(self.tmp.name).glob("*.part")), [])

    def test_a_pause_lies_between_requests_but_not_before_the_first(self):
        server = self.serve([(200, {}, b"a"), (200, {}, b"b")])
        client = self.client(pause_seconds=5)
        client.download(server.url, self.target)
        self.assertEqual(self.waits, [])
        client.download(server.url, self.target)
        self.assertEqual(self.waits, [5])
        self.assertEqual(client.stats.pause_seconds, 5)

    def test_json_index(self):
        server = self.serve([(200, {}, json.dumps({"features": []}).encode())])
        self.assertEqual(self.client().get_json(server.url), {"features": []})
