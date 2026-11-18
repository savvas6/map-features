# Map-feature packs

Offline packs of map features from [OpenStreetMap](https://www.openstreetmap.org), built every week for every
country, for the «Map features» layer of a map app. Each country, or region of a big country, has five packs,
so an app can download only the kinds it wants:

| Pack | What is in it | OpenStreetMap tags |
|---|---|---|
| `military` | military areas, firing and training ranges, danger areas, bases, checkpoints, trenches | `landuse=military`, `military=*` |
| `helipads` | helipads and heliports | `aeroway=helipad`, `aeroway=heliport` |
| `water` | drinking water, water points, wells, taps, springs | `amenity=drinking_water`, `amenity=water_point`, `man_made=water_well`, `man_made=water_tap`, `natural=spring` |
| `limits` | roads and bridges with a weight, axle-load, height or width limit; height barriers | `highway=*` with `maxweight`, `maxaxleload`, `maxheight`, `maxwidth`… |
| `power` | power lines; their points are the pylons and poles | `power=line`, `power=minor_line` |

## Where the packs are

- The list of every area and its packs: [`index.json.gz`](../../releases/download/index/index.json.gz)
  (also as `index.json`), in the release «index».
- The packs of each continent are in a release named after it and the week, e.g. `europe-20261123`. The address
  of a pack is `base` + `release` + `/` + `id` + `-` + kind + `.amf`, from the index.
- [STATUS.md](STATUS.md) lists every area with the size and number of features of each pack.

The format of a pack is described at the top of [build_pack.py](build_pack.py): a small gzipped binary with the
features, their OpenStreetMap tags, their last edit date, and their shapes.

## How they are built

[.github/workflows/weekly.yml](.github/workflows/weekly.yml) runs every Monday on GitHub's machines:

1. `build_all.py plan` takes the countries from [Geofabrik's index](https://download.geofabrik.de). A country
   whose extract is over 2 GB (the US, Canada, Germany, France…) is built per region.
2. `build_all.py build`, one job per continent: downloads each extract from Geofabrik, checks it against its
   checksum, and builds its packs with `build_pack.py`. The packs become that continent's release of the week.
3. `build_all.py index` writes the index, keeping last week's packs for any area that failed this week, and
   removes the releases nobody needs any more (the last two weeks are kept).

The scripts need only Python 3.8 or newer, with nothing to install. To build one area yourself:

    python3 build_pack.py cyprus-latest.osm.pbf --id cyprus --name Cyprus --out packs --geojson
    python3 read_pack.py packs/cyprus-*.amf --html check.html

A test run of a few areas: Actions → Weekly packs → Run workflow, with e.g. `cyprus,malta`.

## Licence

The packs contain information from OpenStreetMap, made available here under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/). Map data
© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright). Anyone may use the packs under the same
licence, giving that credit. The extracts come from [Geofabrik](https://download.geofabrik.de).
