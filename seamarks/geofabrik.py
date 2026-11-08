"""The region list and a deliberately polite download client."""
from __future__ import annotations

import email.utils
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Set

# Same list as index-v1.json, without the polygons nobody here reads.
INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
USER_AGENT = "etmar-seezeichen/1 (weekly seamark build; https://github.com/mogpft/etmar-seezeichen)"

# Childless in the index, but not needed. Other childless aggregates (dach,
# alps, sea, britain-and-ireland, us-south, ...) stay in on purpose: they reach
# further out to sea than their parts, as a comparison of the region polygons
# shows, for example in the German Bight and the English Channel.
#   us: 12 GB against a 14 GB disk, and beyond its parts it adds open ocean only.
#   great-britain, south-africa-and-lesotho: nothing their neighbours lack.
EXCLUDED_REGIONS = {"us", "great-britain", "south-africa-and-lesotho"}


class DownloadFailed(RuntimeError):
    pass


class Throttled(DownloadFailed):
    """The server keeps asking us to back off. Never treated as 'less data'."""


@dataclass
class Region:
    id: str
    url: str


def parent_ids(index: dict) -> Set[str]:
    return {f["properties"]["parent"] for f in index["features"] if f["properties"].get("parent")}


def leaf_regions(index: dict) -> List[Region]:
    parents = parent_ids(index)
    leaves = []
    for feature in index["features"]:
        properties = feature["properties"]
        if properties["id"] in parents or properties["id"] in EXCLUDED_REGIONS:
            continue
        url = properties.get("urls", {}).get("pbf")
        if url:
            leaves.append(Region(properties["id"], url))
    return sorted(leaves, key=lambda r: r.id)


@dataclass
class ClientStats:
    requests: int = 0
    bytes: int = 0
    download_seconds: float = 0.0
    pause_seconds: float = 0.0
    throttle_events: int = 0
    throttle_wait_seconds: float = 0.0
    error_retries: int = 0
    error_wait_seconds: float = 0.0
    largest_bytes: int = 0


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Seconds to wait from a Retry-After header (delta seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


@dataclass
class PoliteClient:
    """One request at a time, a pause between requests, and it listens to 429/503."""

    pause_seconds: float = 5.0
    max_attempts: int = 8
    base_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 900.0
    max_single_wait_seconds: float = 3600.0
    max_total_throttle_seconds: float = 7200.0
    timeout_seconds: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    stats: ClientStats = field(default_factory=ClientStats)
    _used: bool = False

    def _pace(self) -> None:
        if self._used and self.pause_seconds > 0:
            self.sleep(self.pause_seconds)
            self.stats.pause_seconds += self.pause_seconds
        self._used = True

    def _throttled(self, url: str, status: int, retry_after: Optional[str], attempt: int) -> None:
        wait = parse_retry_after(retry_after)
        if wait is None:
            wait = min(self.base_backoff_seconds * 2 ** (attempt - 1), self.max_backoff_seconds)
        if wait > self.max_single_wait_seconds:
            raise Throttled(f"{url}: status {status}, asked to wait {wait:.0f} s")
        if self.stats.throttle_wait_seconds + wait > self.max_total_throttle_seconds:
            raise Throttled(f"{url}: throttled for {self.stats.throttle_wait_seconds:.0f} s in total, giving up")
        self.stats.throttle_events += 1
        self.stats.throttle_wait_seconds += wait
        print(f"  status {status}, waiting {wait:.0f} s", flush=True)
        self.sleep(wait)

    def _retry_after_error(self, attempt: int) -> None:
        wait = min(30.0 * attempt, self.max_backoff_seconds)
        self.stats.error_retries += 1
        self.stats.error_wait_seconds += wait
        self.sleep(wait)

    def _fetch(self, url: str, sink) -> int:
        """Runs one logical request with retries; sink(response) returns bytes read."""
        self._pace()
        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            self.stats.requests += 1
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return sink(response)
            except urllib.error.HTTPError as error:
                last_problem = f"status {error.code}"
                if error.code in (429, 503):
                    self._throttled(url, error.code, error.headers.get("Retry-After"), attempt)
                    continue
                if 500 <= error.code < 600:
                    self._retry_after_error(attempt)
                    continue
                raise DownloadFailed(f"{url}: status {error.code}")
            except (urllib.error.URLError, socket.timeout, ConnectionError, http.client.HTTPException) as error:
                last_problem = str(error)
                self._retry_after_error(attempt)
        kind = Throttled if last_problem in ("status 429", "status 503") else DownloadFailed
        raise kind(f"{url}: gave up after {self.max_attempts} attempts ({last_problem})")

    def get_json(self, url: str) -> dict:
        holder = {}

        def sink(response) -> int:
            body = response.read()
            holder["value"] = json.loads(body)
            return len(body)

        self._fetch(url, sink)
        return holder["value"]

    def download(self, url: str, target: Path) -> int:
        partial = target.with_name(target.name + ".part")
        started = time.monotonic()

        def sink(response) -> int:
            expected = response.headers.get("Content-Length")
            received = 0
            with open(partial, "wb") as out:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
            if expected is not None and received != int(expected):
                raise http.client.HTTPException(f"truncated: {received} of {expected} bytes")
            return received

        try:
            size = self._fetch(url, sink)
            os.replace(partial, target)
        finally:
            if partial.exists():
                partial.unlink()
        self.stats.bytes += size
        self.stats.largest_bytes = max(self.stats.largest_bytes, size)
        self.stats.download_seconds += time.monotonic() - started
        return size
