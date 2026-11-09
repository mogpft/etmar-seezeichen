# Etmar seamark cells

A weekly job that extracts the world's seamarks and harbours from
[OpenStreetMap](https://www.openstreetmap.org) and publishes them as small
static JSON files, one per 1° × 1° square. The sailing app
[Etmar](https://etmar.app) reads these files to show buoys, beacons, lights,
harbours and anchorages on its chart. Anyone may use them under the terms below.

```
https://daten.etmar.app/v1/index.json
https://daten.etmar.app/v1/N54E010.json

https://mogpft.github.io/etmar-seezeichen/v1/index.json     (second source, same files)
```

**Not for navigation.** OpenStreetMap is mapped by volunteers and can be
incomplete or out of date. These files can be up to a week behind it as well.

## Where the data comes from, and its licence

All data comes from OpenStreetMap through the regional extracts published by
[Geofabrik](https://download.geofabrik.de).

**© OpenStreetMap contributors.** The published files are a derivative database
under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Each publication carries the notice as `v1/LICENSE.txt` and in the `license`
field of `index.json`. If you use the data publicly, attribute OpenStreetMap and
keep your own derivative database under the ODbL. The code in this repository
shows exactly how the data was derived, and is under the MIT licence (`LICENSE`).

## The files

Cells are named after their south-west corner: `N54E010` covers 54–55° N,
10–11° E; `S34W077` covers 34–33° S, 77–76° W. A square with no objects has no
file and no index entry.

Each cell has the shape of an Overpass API answer:

```json
{"version":0.6,"generator":"etmar-seezeichen",
 "osm3s":{"timestamp_osm_base":"2026-11-08T01:00:00Z","copyright":"…"},
 "elements":[
  {"type":"node","id":1,"lat":54.3,"lon":10.1,"tags":{"seamark:type":"buoy_lateral"}},
  {"type":"way","id":2,"center":{"lat":54.3,"lon":10.2},"tags":{"leisure":"marina"}}]}
```

`index.json` lists the cells:

```json
{"format":1,"built_at":"…","timestamp_osm_base":"…","license":"…","elements":255000,
 "cells":[{"id":"N54E010","count":1495,"bytes":578000}]}
```

`timestamp_osm_base` is the state of the *oldest* extract used, so the data is at
least that recent. An object belongs to exactly one cell, by its position. Ways and relations are
placed by the centre of their bounding box.
`report.json` next to the index records regions, timings and sizes of the build.

Selected are the objects of this Overpass query, unchanged:

```
node["seamark:type"~"^(buoy_|beacon_|light$|light_minor$|light_major$|light_vessel$|lighthouse$)"];
nwr["leisure"="marina"];
nwr["harbour"="yes"];
nwr["seamark:type"~"^(harbour|anchorage)$"];
out center tags;
```

## How a run works

`build.py` reads Geofabrik's region index and goes through the regions one at a
time: download, filter with `osmium tags-filter`, delete the download at once
(a GitHub runner has 14 GB of disk), then collect the matching objects. Objects
that appear in several regions are merged by OSM id. A cross-border relation
gets the union of its bounding boxes, the newest version decides its tags.

Regions are the childless entries of the index, because extracts are cut at
polygon borders and only their sum covers the coast. Three are skipped:
`great-britain` and `south-africa-and-lesotho` cover nothing the other regions
lack, and `us` is too large for the disk (12 GB) while adding only open ocean
beyond its parts. Other aggregates such as `dach` or `britain-and-ireland` stay,
because they reach further out to sea than their parts do. Objects whose
bounding box spans the 180° meridian are counted in the report and left out.

One parent is taken as well as its children: `central-america`, because the
Lesser Antilles, the British Virgin Islands, Cayman, Turks and Caicos and the
ABC islands have no extract of their own. Checked against Natural Earth's land
polygons, what is still in no region is remote or tiny: Kerguelen, Crozet,
South Georgia, the South Sandwich Islands, Chagos, Bouvet, Saint-Pierre et
Miquelon and Ventotene. Their parents are too large for what they would add.

### Being a good guest at Geofabrik

One download at a time, five seconds between downloads, a descriptive
User-Agent. On 429 or 503 the job waits as long as `Retry-After` says, or backs
off from one minute to fifteen. It counts pauses, waits and runtime in the
report. Throttling that lasts (eight attempts on a file, two hours in total, or
a single request to wait more than an hour) ends the run in failure: it never
carries on with less data.

### What keeps bad data from being published

A run publishes either everything or nothing.

- Any region that cannot be fetched, filtered or dated fails the run.
- A region of the previous publication that has disappeared from the index
  fails the run, unless it was split into new regions.
- More than 5 % fewer objects than the previous publication fails the run. The
  previous state comes from the `data` branch.
- With no previous publication at all, a publishing run stops before the first
  download. The first publication needs `accept_baseline`, as does publishing
  after a drop someone has reviewed.
- More than 20,000 files, or one above 25 MiB, fails the run (Cloudflare Pages
  limits). The final check also confirms that index and files agree exactly and
  that the build covered every region.
- Cloudflare Pages switches a deployment over in one step and the `data` branch
  is replaced by a single force-push, so an aborted run leaves the old data
  untouched rather than a mixture of old and new.

### Staying scheduled

GitHub disables scheduled workflows after 60 days without repository activity.
This repository rarely changes, so the last step of every run, red or green,
calls the [enable-workflow API](https://docs.github.com/en/rest/actions/workflows#enable-a-workflow).
GitHub does not document what counts as activity, and no commits are made just
to look busy. Should the schedule stop anyway, GitHub sends a notice, and
re-enabling it on the Actions tab is all it takes.

## Running it

All runs start from *Actions → Build seamark cells → Run workflow*.

| Purpose | Inputs |
|---|---|
| Quick check of the machinery, a few minutes | `regions: malta,andorra` |
| Full trial: runtime and size, publishes nothing | none |
| Full trial plus the comparison with Overpass | `compare` |
| The very first publication | `publish`, `accept_baseline` |
| Weekly run | starts by itself and publishes |

Every run stores `report.json`, the finished files and any comparison as the
artifact `seamark-build`, and prints a summary on its page.

Publishing needs these repository secrets. No other credentials are used.

| Secret | Purpose |
|---|---|
| `CLOUDFLARE_API_TOKEN` | API token with *Account → Cloudflare Pages → Edit* |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account id |

The run creates the Cloudflare Pages project `etmar-seezeichen` if it is missing.
GitHub Pages serves the root of the `data` branch.

## Checking the result

`compare.py` puts finished cells next to one live Overpass answer for each of
five areas (Kiel, Solent, Sydney, Chesapeake Bay, Antigua), comparing object set,
position and tags. Overpass is ahead of the extracts by a day or so, so a
difference counts as explained when Overpass dates the object after the cells'
data. Whatever remains is listed by object.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .
```

The tests need no network. Without `osmium-tool` installed, `build.py --raw-filter
pyosmium --local-pbf id=file.osm.pbf` runs the pipeline with a slow stand-in for
the first filter, on extracts you already have; such a build can never be
published.
