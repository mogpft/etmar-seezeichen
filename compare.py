#!/usr/bin/env python3
"""Checks finished cells against one Overpass answer per test box.

Each box costs exactly one query (retried only if the server is overloaded).
Overpass selects a way when any of its nodes is inside the box; the cells hold
it by its centre. So both sides are compared by position: what lies in the box.

A difference counts as explained when Overpass dates the object after the
cells' data was taken. An object found only in the cells may have been deleted
since, but Overpass cannot show that, so those stay listed for a human.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from seamarks.cells import cell_name_for_degrees
from seamarks.geofabrik import USER_AGENT
from seamarks.query import overpass_query

ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_BOXES = ["kiel", "solent", "sydney", "chesapeake", "antigua"]
BOXES: Dict[str, Tuple[float, float, float, float]] = {  # south, west, north, east
    "kiel": (54.2, 10.0, 54.6, 10.6),
    "belt": (54.6, 10.0, 54.9, 11.0),
    "solent": (50.6, -1.6, 50.9, -1.0),
    "sydney": (-33.95, 151.1, -33.7, 151.35),
    "chesapeake": (38.8, -76.6, 39.2, -76.3),
    "antigua": (16.95, -61.95, 17.2, -61.65),
}
TRANSIENT = (429, 502, 503, 504)
MOVED_METRES = 5.0


def position(element: dict) -> Optional[Tuple[float, float]]:
    if "lat" in element:
        return element["lat"], element["lon"]
    if "center" in element:
        return element["center"]["lat"], element["center"]["lon"]
    return None


def in_box(point: Tuple[float, float], box) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def metres(a, b) -> float:
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def load_cells(cells_dir: Path, box) -> Tuple[Dict, str]:
    index = json.loads((cells_dir / "index.json").read_text(encoding="utf-8"))
    listed = {entry["id"] for entry in index["cells"]}
    found = {}
    for lat in range(math.floor(box[0]), math.ceil(box[2])):
        for lon in range(math.floor(box[1]), math.ceil(box[3])):
            name = cell_name_for_degrees(lat, lon)
            if name not in listed:
                continue
            document = json.loads((cells_dir / f"{name}.json").read_text(encoding="utf-8"))
            for element in document["elements"]:
                found[(element["type"], element["id"])] = element
    return found, index["timestamp_osm_base"]


def query_overpass(endpoint: str, box) -> dict:
    body = urllib.parse.urlencode({"data": overpass_query(*box)}).encode()
    for attempt in range(3):
        request = urllib.request.Request(endpoint, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                answer = json.loads(response.read())
            if "remark" in answer:
                raise RuntimeError(f"Overpass remark: {answer['remark']}")
            for element in answer["elements"]:  # nobody's username belongs in a report
                element.pop("user", None)
                element.pop("uid", None)
            return answer
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT or attempt == 2:
                raise
            print(f"  Overpass status {error.code}, waiting 60 s", flush=True)
            time.sleep(60)
    raise RuntimeError("unreachable")


def compare_box(name: str, box, cells_dir: Path, answer: dict) -> dict:
    cells_all, base = load_cells(cells_dir, box)
    cells = {k: v for k, v in cells_all.items() if in_box(position(v), box)}
    overpass, reaching_in = {}, 0
    for element in answer["elements"]:
        point = position(element)
        if point is None:
            continue
        if in_box(point, box):
            overpass[(element["type"], element["id"])] = element
        else:
            reaching_in += 1  # selected by a node in the box, centred outside it

    differences = []

    def note(kind, key, element, detail=""):
        stamp = element.get("timestamp")
        differences.append({
            "kind": kind, "object": f"{key[0]}/{key[1]}", "name": element.get("tags", {}).get("name"),
            "edited": stamp, "explained": bool(stamp and stamp > base), "detail": detail,
        })

    for key in sorted(set(overpass) - set(cells)):
        note("only in overpass", key, overpass[key])
    for key in sorted(set(cells) - set(overpass)):
        note("only in cells", key, cells[key])
    largest_move = 0.0
    tag_differences = 0
    for key in sorted(set(cells) & set(overpass)):
        moved = metres(position(cells[key]), position(overpass[key]))
        largest_move = max(largest_move, moved)
        if moved > MOVED_METRES:
            note("moved", key, overpass[key], f"{moved:.0f} m")
        if cells[key].get("tags") != overpass[key].get("tags"):
            tag_differences += 1
            note("tags differ", key, overpass[key])
    unexplained = sum(not d["explained"] for d in differences)
    return {
        "box": name, "overpass_total": len(answer["elements"]), "reaching_in": reaching_in,
        "overpass_in_box": len(overpass), "cells_in_box": len(cells),
        "common": len(set(cells) & set(overpass)),
        "only_overpass": len(set(overpass) - set(cells)), "only_cells": len(set(cells) - set(overpass)),
        "largest_move_m": round(largest_move, 2), "tag_differences": tag_differences,
        "unexplained": unexplained, "cells_timestamp": base,
        "overpass_timestamp": answer.get("osm3s", {}).get("timestamp_osm_base"),
        "differences": differences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", type=Path, required=True, help="directory holding index.json and the cells")
    parser.add_argument("--boxes", default=",".join(DEFAULT_BOXES))
    parser.add_argument("--overpass-json", action="append", default=[], metavar="BOX=PATH",
                        help="use a saved Overpass answer for this box instead of asking")
    parser.add_argument("--cache-dir", type=Path, help="save answers here and reuse them")
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    saved = dict(entry.split("=", 1) for entry in args.overpass_json)
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    results, failed = [], False
    asked = False
    for name in args.boxes.split(","):
        box = BOXES[name]
        path = Path(saved[name]) if name in saved else (args.cache_dir / f"overpass-{name}.json" if args.cache_dir else None)
        if path is not None and path.is_file():
            answer = json.loads(path.read_text(encoding="utf-8"))
        else:
            if asked:
                time.sleep(15)
            asked = True
            answer = query_overpass(args.endpoint, box)
            if path is not None:
                path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        result = compare_box(name, box, args.cells, answer)
        results.append(result)
        allowed = max(2, result["overpass_in_box"] // 100)
        verdict = "ok" if result["unexplained"] <= allowed else "FAILED"
        failed = failed or verdict != "ok"
        print(f"{name:11} overpass {result['overpass_in_box']:5} cells {result['cells_in_box']:5} "
              f"common {result['common']:5} only-overpass {result['only_overpass']:3} only-cells {result['only_cells']:3} "
              f"max-move {result['largest_move_m']:6.2f} m tag-diffs {result['tag_differences']:3} "
              f"unexplained {result['unexplained']:3} (reaching in, not counted: {result['reaching_in']}) {verdict}")
        for difference in result["differences"]:
            print(f"    {difference['kind']}: {difference['object']} {difference['name'] or ''} "
                  f"edited {difference['edited']} {'(newer than the cells)' if difference['explained'] else ''} {difference['detail']}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
