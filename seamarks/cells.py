"""Writes the 1-degree cells and their index."""
from __future__ import annotations

import gzip
import json
import statistics
from pathlib import Path
from typing import Dict, List, Tuple

from .collect import E7, Item

LICENSE_NOTICE = "© OpenStreetMap contributors, ODbL 1.0, https://opendatacommons.org/licenses/odbl/1-0/"
OVERPASS_COPYRIGHT = (
    "The data included in this document is from www.openstreetmap.org. "
    "The data is made available under ODbL."
)
INDEX_FORMAT = 1
_ORDER = {"node": 0, "way": 1, "relation": 2}


def cell_name_for_degrees(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def cell_name(lat2: int, lon2: int) -> str:
    """Cell of a position given in 0.5e-7 degrees; south-west corner names it."""
    # Exactly 90 N and 180 E have no cell of their own to the north or east.
    lat = min(lat2 // (2 * E7), 89)
    lon = min(lon2 // (2 * E7), 179)
    return cell_name_for_degrees(lat, lon)


def element(item: Item) -> dict:
    lat2, lon2 = item.position2()
    lat, lon = round(lat2 / (2 * E7), 7), round(lon2 / (2 * E7), 7)
    tags = dict(sorted(item.tags.items()))
    if item.kind == "node":
        return {"type": "node", "id": item.osm_id, "lat": lat, "lon": lon, "tags": tags}
    return {"type": item.kind, "id": item.osm_id, "center": {"lat": lat, "lon": lon}, "tags": tags}


def group_by_cell(items: List[Item]) -> Tuple[Dict[str, List[Item]], int]:
    """Returns the cells and how many objects had no usable position."""
    cells: Dict[str, List[Item]] = {}
    skipped = 0
    for item in items:
        if item.spans_antimeridian:
            # The min/max box of such an object is meaningless; its centre
            # would land on the far side of the planet.
            skipped += 1
            continue
        cells.setdefault(cell_name(*item.position2()), []).append(item)
    return cells, skipped


def write_cells(items: List[Item], v1_dir: Path, timestamp: str, built_at: str) -> dict:
    """Writes every cell and index.json; returns size statistics."""
    v1_dir.mkdir(parents=True, exist_ok=True)
    cells, skipped = group_by_cell(items)
    entries, gzip_sizes = [], []
    for name in sorted(cells):
        members = sorted(cells[name], key=lambda i: (_ORDER[i.kind], i.osm_id))
        document = {
            "version": 0.6,
            "generator": "etmar-seezeichen",
            "osm3s": {"timestamp_osm_base": timestamp, "copyright": OVERPASS_COPYRIGHT},
            "elements": [element(i) for i in members],
        }
        data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (v1_dir / f"{name}.json").write_bytes(data)
        entries.append({"id": name, "count": len(members), "bytes": len(data)})
        gzip_sizes.append(len(gzip.compress(data, 9)))
    total = sum(e["count"] for e in entries)
    index = {
        "format": INDEX_FORMAT,
        "built_at": built_at,
        "timestamp_osm_base": timestamp,
        "license": LICENSE_NOTICE,
        "elements": total,
        "cells": entries,
    }
    data = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (v1_dir / "index.json").write_bytes(data)
    counts = [e["count"] for e in entries] or [0]
    sizes = [e["bytes"] for e in entries] or [0]
    return {
        "elements": total,
        "antimeridian_skipped": skipped,
        "cells": len(entries),
        "bytes_total": sum(sizes),
        "bytes_max": max(sizes),
        "gzip_bytes_total": sum(gzip_sizes),
        "gzip_bytes_max": max(gzip_sizes or [0]),
        "elements_max": max(counts),
        "elements_median": statistics.median(counts),
        "index_bytes": len(data),
        "index_gzip_bytes": len(gzip.compress(data, 9)),
    }
