"""One complete build: regions in, a publishable directory (or a red run) out."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import osmium

from . import geofabrik, guard, site
from .cells import write_cells
from .collect import Merged, read_filtered
from .rawfilter import make_filter

REPO_ROOT = Path(__file__).resolve().parent.parent


class BuildFailure(RuntimeError):
    pass


@dataclass
class Config:
    out: Path
    workdir: Path
    previous: Optional[Path] = None
    require_baseline: bool = False
    accept_baseline: bool = False
    only: List[str] = field(default_factory=list)
    local_pbf: Dict[str, Path] = field(default_factory=dict)
    raw_filter: str = "osmium-tool"
    index_url: str = geofabrik.INDEX_URL
    pause_seconds: float = 5.0
    max_runtime_seconds: float = 330 * 60
    filtered_cache: Optional[Path] = None
    sleep: object = time.sleep


def header_timestamp(path: Path) -> str:
    reader = osmium.io.Reader(str(path), osmium.osm.NOTHING)
    try:
        value = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not value:
        raise BuildFailure(f"{path.name} carries no replication timestamp")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan(config: Config, client: geofabrik.PoliteClient):
    """Returns (regions, parent region ids, is_partial)."""
    if config.local_pbf:
        regions = [geofabrik.Region(k, str(v)) for k, v in sorted(config.local_pbf.items())]
        return regions, set(), True
    index = client.get_json(config.index_url)
    regions = geofabrik.leaf_regions(index)
    if not regions:
        raise BuildFailure("the region index lists no regions")
    parents = geofabrik.parent_ids(index)
    if config.only:
        known = {r.id: r for r in regions}
        missing = [r for r in config.only if r not in known]
        if missing:
            raise BuildFailure(f"unknown regions: {', '.join(missing)}")
        return [known[r] for r in config.only], parents, True
    return regions, parents, False


def _process_regions(config: Config, client, regions, report) -> Merged:
    merged = Merged()
    raw = make_filter(config.raw_filter)
    config.workdir.mkdir(parents=True, exist_ok=True)
    counters = {"no_position": 0}
    started = time.monotonic()
    timings = report["timings"]
    timestamps = []
    for number, region in enumerate(regions, 1):
        if time.monotonic() - started > config.max_runtime_seconds:
            raise BuildFailure(f"out of time after {number - 1} of {len(regions)} regions")
        safe = region.id.replace("/", "_")
        local = config.local_pbf.get(region.id)
        source = local if local is not None else config.workdir / f"{safe}.osm.pbf"
        cached = config.filtered_cache / f"{safe}.filtered.osm.pbf" if config.filtered_cache else None
        filtered = cached if cached is not None else config.workdir / f"{safe}.filtered.osm.pbf"
        entry = {"id": region.id}
        print(f"[{number}/{len(regions)}] {region.id}", flush=True)
        try:
            if local is None:
                entry["bytes"] = client.download(region.url, source)
                report["min_free_disk_bytes"] = min(
                    report["min_free_disk_bytes"], shutil.disk_usage(config.workdir).free)
            elif not source.is_file():
                raise BuildFailure(f"region file is missing: {region.id}")
            timestamps.append(header_timestamp(source))
            entry["timestamp"] = timestamps[-1]
            if cached is None or not cached.exists():
                tick = time.monotonic()
                raw.run(source, filtered)
                entry["filter_seconds"] = round(time.monotonic() - tick, 1)
                timings["filter_seconds"] += entry["filter_seconds"]
        finally:
            # 14 GB of disk: a downloaded extract never outlives its filter pass.
            if local is None and source.exists():
                source.unlink()
        tick = time.monotonic()
        count = 0
        for item in read_filtered(filtered, counters):
            merged.add(item)
            count += 1
        timings["collect_seconds"] += time.monotonic() - tick
        entry["matched"] = count
        report["regions"].append(entry)
        if cached is None:
            filtered.unlink()
    report["no_position"] = counters["no_position"]
    report["timestamp_osm_base"] = min(timestamps)
    report["timestamp_newest"] = max(timestamps)
    return merged


def run(config: Config) -> int:
    started = time.monotonic()
    config.out.mkdir(parents=True, exist_ok=True)
    built_at = _utc_now()
    report: dict = {
        "built_at": built_at, "partial": bool(config.only or config.local_pbf), "failure": None,
        "regions": [], "region_ids": [], "timings": {"filter_seconds": 0.0, "collect_seconds": 0.0},
        "min_free_disk_bytes": shutil.disk_usage(config.out).free,
    }
    client = geofabrik.PoliteClient(pause_seconds=config.pause_seconds, sleep=config.sleep)
    rejected = None
    try:
        previous_index, previous_report = guard.load_previous(config.previous)
        if config.require_baseline and previous_index is None:
            # Before the first download: no point spending Geofabrik's bandwidth.
            raise BuildFailure("no previous publication to compare against; "
                               "use accept_baseline for the very first run")
        regions, parents, partial = _plan(config, client)
        report["partial"] = partial
        report["regions_total"] = len(regions)
        merged = _process_regions(config, client, regions, report)
        if len(report["regions"]) != len(regions):
            raise BuildFailure("not every region was processed")
        report["region_ids"] = sorted(r["id"] for r in report["regions"])
        items = list(merged.items.values())
        kinds = {"node": 0, "way": 0, "relation": 0}
        for item in items:
            kinds[item.kind] += 1
        report["kinds"] = kinds

        temporary = config.out / "site.tmp"
        shutil.rmtree(temporary, ignore_errors=True)
        report["output"] = write_cells(items, temporary / "v1", report["timestamp_osm_base"], built_at)
        site.assemble(temporary, REPO_ROOT / "DATA-LICENSE.txt")

        verdict = guard.evaluate(previous_index, previous_report, report["output"]["elements"],
                                 report["region_ids"], parents, config.accept_baseline)
        report["guard"] = asdict(verdict)
        problems = list(verdict.problems) if verdict.status == "rejected" else []
        problems += site.check_limits(temporary)
        report["site_files"] = sum(1 for p in temporary.rglob("*") if p.is_file())
        if problems:
            rejected = config.out / "site.rejected"
            shutil.rmtree(rejected, ignore_errors=True)
            temporary.rename(rejected)
            raise BuildFailure("; ".join(problems))
        final = config.out / "site"
        shutil.rmtree(final, ignore_errors=True)
        temporary.rename(final)
        _finish_report(report, client, started)
        (final / "v1" / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        return 0
    except (BuildFailure, geofabrik.DownloadFailed, RuntimeError) as failure:
        report["failure"] = f"{type(failure).__name__}: {failure}"
        print(f"::error::{report['failure']}", flush=True)
        return 1
    finally:
        _finish_report(report, client, started)
        (config.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        _print_summary(report)


def _finish_report(report: dict, client, started: float) -> None:
    report["runtime_seconds"] = round(time.monotonic() - started, 1)
    report["client"] = asdict(client.stats)
    for key, value in report["timings"].items():
        report["timings"][key] = round(value, 1)


def _print_summary(report: dict) -> None:
    client, output = report["client"], report.get("output", {})
    lines = [
        f"**Result:** {'failed: ' + report['failure'] if report['failure'] else 'ok'}"
        f"{' (partial build)' if report['partial'] else ''}",
        f"- Runtime: {report['runtime_seconds'] / 60:.1f} min, {len(report['regions'])} of "
        f"{report.get('regions_total', '?')} regions",
        f"- Downloaded: {client['bytes'] / 1e9:.1f} GB in {client['download_seconds'] / 60:.1f} min, "
        f"largest {client['largest_bytes'] / 1e9:.1f} GB, lowest free disk "
        f"{report['min_free_disk_bytes'] / 1e9:.1f} GB",
        f"- Waiting: {client['pause_seconds'] / 60:.1f} min pauses, "
        f"{client['throttle_wait_seconds'] / 60:.1f} min throttled ({client['throttle_events']} times), "
        f"{client['error_retries']} retries after errors",
        f"- Filtering: {report['timings']['filter_seconds'] / 60:.1f} min, "
        f"collecting: {report['timings']['collect_seconds'] / 60:.1f} min",
    ]
    if output:
        lines += [
            f"- Objects: {output['elements']} in {output['cells']} cells, data as of "
            f"{report.get('timestamp_osm_base')}",
            f"- Size: {output['bytes_total'] / 1e6:.1f} MB raw, {output['gzip_bytes_total'] / 1e6:.1f} MB gzip; "
            f"largest cell {output['elements_max']} objects, {output['gzip_bytes_max'] / 1024:.0f} KiB gzip",
        ]
    guard_result = report.get("guard")
    if guard_result:
        lines.append(f"- Guard: {guard_result['status']}, previous {guard_result['previous_elements']} "
                     f"{'; '.join(guard_result['problems'])}")
    text = "\n".join(lines)
    print(text, flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(text + "\n")
