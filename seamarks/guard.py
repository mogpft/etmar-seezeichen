"""Decides whether a finished build may replace the published one."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

MAX_DROP = 0.05


@dataclass
class GuardResult:
    status: str  # "ok", "accepted", "no-baseline" or "rejected"
    previous_elements: Optional[int] = None
    problems: List[str] = field(default_factory=list)

    @property
    def allows_publishing(self) -> bool:
        return self.status in ("ok", "accepted")


def load_previous(directory: Optional[Path]):
    """Returns (index, report) of the last publication; (None, None) if absent."""
    if directory is None or not (directory / "index.json").is_file():
        return None, None
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None
    return index, report


def evaluate(previous_index: Optional[dict], previous_report: Optional[dict], elements: int,
             regions: Iterable[str], parent_regions: Iterable[str], accept_baseline: bool) -> GuardResult:
    if previous_index is None:
        return GuardResult("accepted" if accept_baseline else "no-baseline")
    before = int(previous_index["elements"])
    problems = []
    if elements < before * (1 - MAX_DROP):
        problems.append(f"object count fell from {before} to {elements}, more than {MAX_DROP:.0%}")
    known = set(regions) | set(parent_regions)  # a region that was split still exists
    if previous_report is None:
        problems.append("the previous report is missing, so its regions cannot be compared")
    else:
        vanished = sorted(set(previous_report.get("region_ids", [])) - known)
        if vanished:
            problems.append(f"regions of the previous run are gone: {', '.join(vanished)}")
    if not problems:
        return GuardResult("ok", before)
    return GuardResult("accepted" if accept_baseline else "rejected", before, problems)
