#!/usr/bin/env python3
"""Builds the seamark cells. See README.md."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from seamarks import geofabrik
from seamarks.pipeline import Config, run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--workdir", type=Path, default=Path("work"))
    parser.add_argument("--previous", type=Path, help="directory with the last published index.json and report.json")
    parser.add_argument("--require-baseline", action="store_true",
                        help="fail at once if there is no previous publication")
    parser.add_argument("--accept-baseline", action="store_true",
                        help="publish even without, or against, the previous publication")
    parser.add_argument("--only", default="", help="comma-separated region ids; such a build can never be published")
    parser.add_argument("--local-pbf", action="append", default=[], metavar="ID=PATH",
                        help="use a local extract instead of downloading; such a build can never be published")
    parser.add_argument("--raw-filter", choices=["osmium-tool", "pyosmium"], default="osmium-tool")
    parser.add_argument("--filtered-cache", type=Path, help="keep and reuse filtered extracts here (for development)")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between two downloads")
    parser.add_argument("--max-runtime-minutes", type=float, default=330.0)
    parser.add_argument("--list-regions", action="store_true", help="print the plan and exit")
    args = parser.parse_args()

    if args.list_regions:
        regions = geofabrik.leaf_regions(geofabrik.PoliteClient().get_json(geofabrik.INDEX_URL))
        print("\n".join(r.id for r in regions))
        print(f"{len(regions)} regions", file=sys.stderr)
        return 0

    local = {}
    for entry in args.local_pbf:
        region, _, path = entry.partition("=")
        local[region] = Path(path)
    if args.filtered_cache:
        args.filtered_cache.mkdir(parents=True, exist_ok=True)
    return run(Config(
        out=args.out, workdir=args.workdir, previous=args.previous,
        require_baseline=args.require_baseline, accept_baseline=args.accept_baseline,
        only=[r for r in args.only.split(",") if r], local_pbf=local, raw_filter=args.raw_filter,
        filtered_cache=args.filtered_cache, pause_seconds=args.pause,
        max_runtime_seconds=args.max_runtime_minutes * 60,
    ))


if __name__ == "__main__":
    sys.exit(main())
