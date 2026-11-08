"""Which OpenStreetMap objects belong in the seamark cells.

This mirrors, clause for clause, the Overpass query the Etmar app used:

    (
      node["seamark:type"~"<NODE_TYPE_PATTERN>"](bbox);
      nwr["leisure"="marina"](bbox);
      nwr["harbour"="yes"](bbox);
      nwr["seamark:type"~"^(harbour|anchorage)$"](bbox);
    );
    out center tags;

The app parses these files with the code it used for Overpass responses, so a
change here changes what sailors see on their chart.
"""
from __future__ import annotations

import re

NODE_TYPE_PATTERN = "^(buoy_|beacon_|light$|light_minor$|light_major$|light_vessel$|lighthouse$)"
HARBOUR_TYPE_PATTERN = "^(harbour|anchorage)$"

_NODE_TYPE = re.compile(NODE_TYPE_PATTERN)
_HARBOUR_TYPE = re.compile(HARBOUR_TYPE_PATTERN)

# The coarse first pass. Deliberately wider than the query for nodes: the exact
# patterns are applied afterwards, on far fewer objects.
RAW_FILTER_EXPRESSIONS = [
    "n/seamark:type",
    "nwr/leisure=marina",
    "nwr/harbour=yes",
    "nwr/seamark:type=harbour,anchorage",
]

OVERPASS_TEMPLATE = """[out:json][timeout:90];
(
  node["seamark:type"~"{node_pattern}"]({bbox});
  nwr["leisure"="marina"]({bbox});
  nwr["harbour"="yes"]({bbox});
  nwr["seamark:type"~"{harbour_pattern}"]({bbox});
);
out center meta;
"""


def wanted_nwr(tags: dict) -> bool:
    """The three clauses that apply to nodes, ways and relations alike."""
    seamark_type = tags.get("seamark:type")
    if seamark_type is not None and _HARBOUR_TYPE.search(seamark_type):
        return True
    return tags.get("leisure") == "marina" or tags.get("harbour") == "yes"


def wanted_node(tags: dict) -> bool:
    seamark_type = tags.get("seamark:type")
    if seamark_type is not None and _NODE_TYPE.search(seamark_type):
        return True
    return wanted_nwr(tags)


def matches_raw_filter(kind: str, tags: dict) -> bool:
    """What RAW_FILTER_EXPRESSIONS select, for the pyosmium stand-in."""
    if kind == "node" and "seamark:type" in tags:
        return True
    return wanted_nwr(tags)


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    """The app's query for one box, with edit dates so a difference can be dated."""
    return OVERPASS_TEMPLATE.format(
        node_pattern=NODE_TYPE_PATTERN,
        harbour_pattern=HARBOUR_TYPE_PATTERN,
        bbox=f"{south},{west},{north},{east}",
    )
