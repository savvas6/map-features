#!/usr/bin/env python3
"""Map-feature packs from Overture Maps (https://overturemaps.org): its free monthly copy of OpenStreetMap on Amazon's
cloud, read with DuckDB (pip install duckdb). Only the columns and the blocks of rows needed are downloaded.

  python3 overture.py probe [--area 32.2,34.5,34.7,35.8]   the columns of each layer, and what an area has (checks)
"""
import argparse, json, re, sys, time, urllib.request

BUCKET = 'https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com'
CYPRUS = (32.2, 34.5, 34.7, 35.8)


def log(s):
    print(s, flush=True)


def latest_release():
    x = urllib.request.urlopen(BUCKET + '/?list-type=2&prefix=release/&delimiter=/', timeout=60).read().decode()
    return sorted(re.findall(r'<Prefix>release/([^/<]+)/</Prefix>', x))[-1]


def connect():
    import duckdb
    con = duckdb.connect()
    for s in ("INSTALL spatial", "LOAD spatial", "INSTALL httpfs", "LOAD httpfs", "SET s3_region='us-west-2'",
              "SET enable_object_cache=true"):
        con.execute(s)
    return con


def layer(release, theme, kind):
    return "read_parquet('s3://overturemaps-us-west-2/release/%s/theme=%s/type=%s/*', hive_partitioning=0)" % (release, theme, kind)


def inside(b):
    return 'bbox.xmin >= %f AND bbox.xmax <= %f AND bbox.ymin >= %f AND bbox.ymax <= %f' % (b[0], b[2], b[1], b[3])


def probe(a):
    con = connect()
    import duckdb
    rel = a.release or latest_release()
    b = tuple(float(x) for x in a.area.split(',')) if a.area else CYPRUS
    log('DuckDB %s · Overture release %s · area %s' % (duckdb.__version__, rel, b))

    def show(title, sql, rows=40):
        t0 = time.time()
        try:
            r = con.execute(sql).fetchall()
        except Exception as e:
            log('\n## %s: FAILED %s' % (title, e))
            return
        log('\n## %s (%d rows, %.1f s)' % (title, len(r), time.time() - t0))
        for row in r[:rows]:
            log('  ' + json.dumps(row, ensure_ascii=False, default=str)[:700])

    layers = [('base', 'infrastructure'), ('base', 'land_use'), ('base', 'water'), ('transportation', 'segment'),
              ('divisions', 'division_area')]
    for th, ty in layers:
        show('columns of %s/%s' % (th, ty), 'SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM %s)' % layer(rel, th, ty), 60)
    inf = layer(rel, 'base', 'infrastructure')
    show('infrastructure classes here', "SELECT subtype, class, count(*) n FROM %s WHERE %s GROUP BY ALL ORDER BY n DESC" % (inf, inside(b)), 120)
    show('infrastructure samples', """
        SELECT class, subtype, names.primary, source_tags, sources[1].record_id, sources[1].update_time, ST_GeometryType(geometry)
        FROM (SELECT *, row_number() OVER (PARTITION BY class) k FROM %s WHERE %s AND class IN ('radar', 'watchtower',
              'communication_tower', 'mobile_phone_tower', 'pipeline', 'dam', 'weir', 'runway', 'border_control', 'helipad',
              'heliport', 'power_line', 'minor_line', 'drinking_water', 'height_restrictor', 'military_airport'))
        WHERE k <= 2""" % (inf, inside(b)), 60)
    lu = layer(rel, 'base', 'land_use')
    show('land use: military here', "SELECT subtype, class, count(*) n FROM %s WHERE %s AND (subtype = 'military' OR class IN ('military', 'danger_area', 'range', 'training_area', 'base', 'barracks', 'naval_base', 'bunker', 'trench')) GROUP BY ALL ORDER BY n DESC" % (lu, inside(b)))
    show('land use: military samples', "SELECT class, names.primary, source_tags, sources[1].record_id, ST_GeometryType(geometry) FROM %s WHERE %s AND subtype = 'military' LIMIT 4" % (lu, inside(b)))
    wa = layer(rel, 'base', 'water')
    show('water: springs here', "SELECT class, names.primary, source_tags, sources[1].record_id, ST_GeometryType(geometry) FROM %s WHERE %s AND class = 'spring' LIMIT 5" % (wa, inside(b)))
    seg = layer(rel, 'transportation', 'segment')
    show('roads with vehicle limits here', """
        SELECT count(*) FROM %s WHERE %s AND subtype = 'road' AND access_restrictions IS NOT NULL
        AND len(list_filter(access_restrictions, r -> r.when.vehicle IS NOT NULL)) > 0""" % (seg, inside(b)))
    show('roads with vehicle limits: samples', """
        SELECT class, names.primary, access_restrictions, road_flags, sources[1].record_id, ST_NPoints(geometry)
        FROM %s WHERE %s AND subtype = 'road' AND access_restrictions IS NOT NULL
        AND len(list_filter(access_restrictions, r -> r.when.vehicle IS NOT NULL)) > 0 LIMIT 6""" % (seg, inside(b)), 6)
    da = layer(rel, 'divisions', 'division_area')
    show('division areas here', "SELECT subtype, class, country, region, names.primary, is_land, ST_NPoints(geometry) FROM %s WHERE %s AND subtype IN ('country', 'dependency', 'region') ORDER BY subtype" % (da, inside((b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1))), 20)
    show('US regions', "SELECT count(*), min(region), max(region) FROM %s WHERE country = 'US' AND subtype = 'region'" % da)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('probe')
    s.add_argument('--area', help='west,south,east,north (default: Cyprus)')
    s.add_argument('--release', help='an Overture release, e.g. 2026-10-21.0 (default: the latest)')
    a = ap.parse_args()
    dict(probe=probe)[a.cmd](a)


if __name__ == '__main__':
    main()
