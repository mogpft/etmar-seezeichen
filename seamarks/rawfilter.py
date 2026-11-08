"""First pass over a regional extract: keep candidates and what they reference.

Input and output are both PBF files, so the rest of the pipeline cannot tell
which implementation ran. Production uses osmium-tool; PyosmiumFilter exists
for machines without it and is far too slow for a world run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import osmium

from .query import RAW_FILTER_EXPRESSIONS, matches_raw_filter


class RawFilterError(RuntimeError):
    pass


class OsmiumToolFilter:
    name = "osmium-tool"

    def __init__(self, executable: str = "osmium"):
        self.executable = executable

    def command(self, source: Path, target: Path) -> list:
        # Referenced objects stay in (the default): ways need their nodes and
        # relations their members to compute a bounding box later.
        return [
            self.executable, "tags-filter", "--overwrite", "--no-progress",
            "-o", str(target), str(source), *RAW_FILTER_EXPRESSIONS,
        ]

    def run(self, source: Path, target: Path) -> None:
        done = subprocess.run(self.command(source, target), capture_output=True, text=True)
        if done.returncode != 0:
            raise RawFilterError(f"osmium tags-filter failed for {source.name}: {done.stderr.strip()[-500:]}")


class PyosmiumFilter:
    name = "pyosmium"

    def run(self, source: Path, target: Path) -> None:
        src = str(source)
        relation_ways, relation_nodes = set(), set()
        for rel in osmium.FileProcessor(src, osmium.osm.RELATION):
            if matches_raw_filter("relation", dict(rel.tags)):
                for member in rel.members:
                    if member.type == "w":
                        relation_ways.add(member.ref)
                    elif member.type == "n":
                        relation_nodes.add(member.ref)
        keep_ways, keep_nodes = set(), set(relation_nodes)
        for way in osmium.FileProcessor(src, osmium.osm.WAY):
            if way.id in relation_ways or matches_raw_filter("way", dict(way.tags)):
                keep_ways.add(way.id)
                keep_nodes.update(n.ref for n in way.nodes)
        with osmium.SimpleWriter(str(target), overwrite=True) as writer:
            for node in osmium.FileProcessor(src, osmium.osm.NODE):
                if node.id in keep_nodes or (len(node.tags) and matches_raw_filter("node", dict(node.tags))):
                    writer.add(node)
            for way in osmium.FileProcessor(src, osmium.osm.WAY):
                if way.id in keep_ways:
                    writer.add(way)
            for rel in osmium.FileProcessor(src, osmium.osm.RELATION):
                if matches_raw_filter("relation", dict(rel.tags)):
                    writer.add(rel)


def make_filter(name: str):
    if name == OsmiumToolFilter.name:
        return OsmiumToolFilter()
    if name == PyosmiumFilter.name:
        return PyosmiumFilter()
    raise ValueError(f"unknown raw filter: {name}")
