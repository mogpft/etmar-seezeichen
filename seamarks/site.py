"""The publishable directory: cells, headers, limits and a final consistency check.

The same tree goes to Cloudflare Pages and to the `data` branch for GitHub Pages.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import List

MAX_FILES = 20_000  # Cloudflare Pages, free plan
MAX_FILE_BYTES = 25 * 1024 * 1024

# Rules must not overlap: Cloudflare joins the values of every matching rule
# with a comma, which would corrupt Cache-Control. Hence one rule per cell
# hemisphere instead of /v1/*. The index changes the moment a build lands; a
# cell lags harmlessly, since data only changes weekly.
HEADERS = """/v1/index.json
  Content-Type: application/json
  Cache-Control: public, max-age=3600

/v1/N*
  Content-Type: application/json
  Cache-Control: public, max-age=21600

/v1/S*
  Content-Type: application/json
  Cache-Control: public, max-age=21600
"""


def assemble(root: Path, data_license: Path) -> None:
    """Adds what a host needs around root/v1."""
    shutil.copyfile(data_license, root / "v1" / "LICENSE.txt")
    (root / "_headers").write_text(HEADERS, encoding="utf-8")
    (root / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve as is


def check_limits(root: Path, max_files: int = MAX_FILES, max_file_bytes: int = MAX_FILE_BYTES) -> List[str]:
    files = [p for p in root.rglob("*") if p.is_file()]
    problems = []
    if len(files) > max_files:
        problems.append(f"{len(files)} files, the host allows {max_files}")
    for path in files:
        size = path.stat().st_size
        if size > max_file_bytes:
            problems.append(f"{path.relative_to(root)} has {size} bytes, the host allows {max_file_bytes}")
    return problems


def validate(root: Path, allow_partial: bool = False) -> List[str]:
    """Everything that must hold before a directory may be published."""
    v1 = root / "v1"
    if not (v1 / "index.json").is_file():
        return ["v1/index.json is missing"]
    problems = check_limits(root)
    index = json.loads((v1 / "index.json").read_text(encoding="utf-8"))
    listed = {}
    for entry in index["cells"]:
        listed[f"{entry['id']}.json"] = entry
    present = {p.name for p in v1.glob("*.json")} - {"index.json", "report.json"}
    if present - set(listed):
        problems.append(f"cells not in the index: {sorted(present - set(listed))[:5]}")
    if set(listed) - present:
        problems.append(f"cells in the index but missing: {sorted(set(listed) - present)[:5]}")
    total = 0
    for name in sorted(present & set(listed)):
        path = v1 / name
        entry = listed[name]
        if path.stat().st_size != entry["bytes"]:
            problems.append(f"{name}: size differs from the index")
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if len(document["elements"]) != entry["count"]:
            problems.append(f"{name}: object count differs from the index")
        if document["osm3s"]["timestamp_osm_base"] != index["timestamp_osm_base"]:
            problems.append(f"{name}: timestamp differs from the index")
        total += entry["count"]
    if total != index["elements"]:
        problems.append(f"index says {index['elements']} objects, cells hold {total}")
    if not (v1 / "LICENSE.txt").is_file():
        problems.append("v1/LICENSE.txt is missing")
    if "license" not in index:
        problems.append("the index has no license field")
    report_path = v1 / "report.json"
    if not report_path.is_file():
        problems.append("v1/report.json is missing")
    elif not allow_partial and json.loads(report_path.read_text(encoding="utf-8")).get("partial", True):
        problems.append("this is a partial build, which must never be published")
    return problems


def main() -> int:
    problems = validate(Path(sys.argv[1]))
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
