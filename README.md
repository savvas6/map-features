# Map-feature packs

Offline packs of map features from [OpenStreetMap](https://www.openstreetmap.org), by way of
[Overture Maps](https://overturemaps.org), for every country, for the «Map features» layer of a map app. Each country,
or region of a big country, has eleven packs, so an app can download only the kinds it wants:

| Pack | What is in it | Overture class (OpenStreetMap tags) |
|---|---|---|
| `military` | military areas, firing and training ranges, danger areas, bases, barracks | land use `military` (`landuse=military`, `military=*`) |
| `helipads` | helipads and heliports | `helipad`, `heliport` (`aeroway=*`) |
| `water` | drinking water and springs | `drinking_water` (`amenity=drinking_water`), water `spring` (`natural=spring`) |
| `power` | power lines; their points are the pylons and poles | `power_line`, `minor_line` (`power=line`, `power=minor_line`) |
| `radar` | radar sites | `radar` (`tower:type=radar`) |
| `watchtowers` | watchtowers | `watchtower` (`tower:type=watchtower`) |
| `telecom` | communication and mobile phone towers | `communication_tower`, `mobile_phone_tower` |
| `pipelines` | pipelines, also under ground and under water | `pipeline` (`man_made=pipeline`) |
| `dams` | dams | `dam` (`waterway=dam`) |
| `runways` | runways | `runway` (`aeroway=runway`) |
| `borders` | border crossings | `border_control` (`barrier=border_control`) |

## Where the packs are

- The list of every area and its packs: [`index.json.gz`](../../releases/download/index/index.json.gz)
  (also as `index.json`), in the release «index».
- The packs of each continent are in a release named after it and the week, e.g. `eu-20261126` (`eu1`, `eu2`… when
  one release would have more than GitHub's 1000 files). The address of a pack is `base` + `release` + `/` + `id` +
  `-` + kind + `.amf`, from the index.
- [STATUS.md](STATUS.md) lists every area with the size and number of features of each pack.

The format of a pack is described at the top of [build_pack.py](build_pack.py): a small gzipped binary with the
features, their OpenStreetMap tags, their last edit date, and their shapes.

## How they are built

[.github/workflows/weekly.yml](.github/workflows/weekly.yml) runs every Thursday on GitHub's machines. Overture
publishes a new release about once a month; a week with nothing new is skipped.

1. `overture.py plan` makes the list of areas from Overture's boundaries: every country with its territorial waters
   (so offshore ranges and pipelines stay with it), and the big ones by region (the US states, Canada's provinces,
   Germany's Länder…).
2. `overture.py build`, one job per continent: reads that continent's features of the eleven kinds with DuckDB (only
   the columns and blocks of rows needed), puts each in the areas it touches, and writes their packs. The packs become
   that continent's release of the week.
3. `build_all.py index` writes the index, keeping the last packs of any area that failed this time, and removes the
   releases nobody needs any more (the last two of each are kept).

«Probe Overture» (`overture.py probe`, by hand from the Actions tab) shows the columns of each layer and what an area
has: a check for when Overture changes its schema.

`build_pack.py` makes the same packs from an OpenStreetMap extract (`.osm.pbf`, e.g. from Geofabrik), with nothing
to install, for checks on one area:

    python3 build_pack.py cyprus-latest.osm.pbf --id cy --name Cyprus --out packs --geojson
    python3 read_pack.py packs/cy-*.amf --html check.html

A test run of a few countries: Actions → Weekly packs → Run workflow, with e.g. `cy,mt`.

## Licence

The packs contain information from OpenStreetMap, made available here under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/). Map data
© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), [Overture Maps Foundation](https://docs.overturemaps.org/attribution/).
Anyone may use the packs under the same licence, giving that credit.
