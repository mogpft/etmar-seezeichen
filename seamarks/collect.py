"""Second pass: turn a filtered extract into seamark items and merge regions."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

import osmium

from .query import wanted_node, wanted_nwr

BBox = Tuple[int, int, int, int]  # south, north, west, east in 1e-7 degrees
E7 = 10_000_000
HALF_TURN = 180 * E7


class Item:
    """One object. Nodes carry a degenerate bounding box."""

    __slots__ = ("kind", "osm_id", "version", "tags", "south", "north", "west", "east")

    def __init__(self, kind: str, osm_id: int, version: int, tags: dict, bbox: BBox):
        self.kind = kind
        self.osm_id = osm_id
        self.version = version
        self.tags = tags
        self.south, self.north, self.west, self.east = bbox

    @property
    def spans_antimeridian(self) -> bool:
        return self.east - self.west > HALF_TURN

    def position2(self) -> Tuple[int, int]:
        """Centre of the bounding box as (lat, lon) in 0.5e-7 degrees.

        Overpass `out center` is the middle of the bounding box, not a centroid.
        Doubled integers keep the half unit exact, so a point on a cell border
        cannot fall into the wrong cell through rounding.
        """
        return self.south + self.north, self.west + self.east


def _union(a: Optional[BBox], b: BBox) -> BBox:
    if a is None:
        return b
    return min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])


def read_filtered(path: Path, counters: Dict[str, int]) -> Iterator[Item]:
    """Yield the wanted objects of a sorted PBF (nodes, then ways, then relations)."""
    nodes: Dict[int, Tuple[int, int]] = {}
    way_boxes: Dict[int, BBox] = {}
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            if not obj.location.valid():
                continue
            lat, lon = obj.location.y, obj.location.x
            nodes[obj.id] = (lat, lon)
            if len(obj.tags):
                tags = dict(obj.tags)
                if wanted_node(tags):
                    yield Item("node", obj.id, obj.version, tags, (lat, lat, lon, lon))
        elif obj.is_way():
            box: Optional[BBox] = None
            for ref in obj.nodes:
                point = nodes.get(ref.ref)
                if point is not None:
                    box = _union(box, (point[0], point[0], point[1], point[1]))
            if box is not None:
                way_boxes[obj.id] = box
            tags = dict(obj.tags)
            if wanted_nwr(tags):
                if box is None:
                    counters["no_position"] += 1
                else:
                    yield Item("way", obj.id, obj.version, tags, box)
        elif obj.is_relation():
            tags = dict(obj.tags)
            if not wanted_nwr(tags):
                continue
            # Nodes and ways only: Overpass does not descend into sub-relations
            # when it computes the centre.
            box = None
            for member in obj.members:
                if member.type == "n" and member.ref in nodes:
                    point = nodes[member.ref]
                    box = _union(box, (point[0], point[0], point[1], point[1]))
                elif member.type == "w" and member.ref in way_boxes:
                    box = _union(box, way_boxes[member.ref])
            if box is None:
                counters["no_position"] += 1
            else:
                yield Item("relation", obj.id, obj.version, tags, box)


class Merged:
    """All items of all regions, one per OSM object.

    The same object turns up in every extract it touches. A relation that
    crosses a region border can be incomplete in each of them, so bounding
    boxes are united instead of picking one copy. Extracts are a few hours
    apart, so the highest version decides tags and node position.
    """

    def __init__(self) -> None:
        self.items: Dict[Tuple[str, int], Item] = {}

    def add(self, item: Item) -> None:
        key = (item.kind, item.osm_id)
        current = self.items.get(key)
        if current is None:
            self.items[key] = item
            return
        if item.kind == "node":
            if item.version > current.version:
                self.items[key] = item
            return
        current.south = min(current.south, item.south)
        current.north = max(current.north, item.north)
        current.west = min(current.west, item.west)
        current.east = max(current.east, item.east)
        if item.version > current.version:
            current.version, current.tags = item.version, item.tags
