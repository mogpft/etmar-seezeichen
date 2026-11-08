"""Small real PBF files, so the tests run the same reader as production."""
from __future__ import annotations

from pathlib import Path

import osmium
from osmium.osm.mutable import Node, Relation, Way

TIMESTAMP = "2026-11-08T01:00:00Z"


def write_pbf(path: Path, nodes=(), ways=(), relations=(), timestamp: str = TIMESTAMP) -> Path:
    """nodes: (id, lon, lat, tags[, version]); ways: (id, refs, tags[, version]);
    relations: (id, [(type, ref, role)], tags[, version])."""
    header = osmium.io.Header()
    if timestamp:
        header.set("osmosis_replication_timestamp", timestamp)
    writer = osmium.SimpleWriter(str(path), overwrite=True, header=header)
    try:
        for n in nodes:
            writer.add_node(Node(id=n[0], location=(n[1], n[2]), tags=n[3], version=n[4] if len(n) > 4 else 1))
        for w in ways:
            writer.add_way(Way(id=w[0], nodes=list(w[1]), tags=w[2], version=w[3] if len(w) > 3 else 1))
        for r in relations:
            writer.add_relation(Relation(id=r[0], members=list(r[1]), tags=r[2], version=r[3] if len(r) > 3 else 1))
    finally:
        writer.close()
    return path


BUOY = {"seamark:type": "buoy_lateral"}
MARINA = {"leisure": "marina", "name": "Test Marina"}
