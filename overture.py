#!/usr/bin/env python3
"""Map-feature packs from Overture Maps (https://overturemaps.org): its free monthly copy of OpenStreetMap on Amazon's
cloud, read with DuckDB (pip install duckdb). Only the columns and the blocks of rows needed are downloaded.

  plan    the areas: every country (with its territorial waters, so offshore ranges and pipelines stay in its pack);
          the big ones by region (US states, Canada's provinces, Germany's Länder…). Skips a week with no new release.
  build   one group (a continent): its features of the eleven kinds, put in the areas they touch, as packs
  probe   the columns of each layer and what an area has (to check a new Overture release)

  python3 overture.py plan --out plan.json [--only cy,mt] [--previous index.json] [--force] [--github-output FILE]
  python3 overture.py build --plan plan.json --group eu --out out
  python3 overture.py probe [--area west,south,east,north] [--release 2026-10-21.0]

The packs are those of build_pack.py (format 2, the same kinds and tags); build_all.py makes the index from them.
Data © OpenStreetMap contributors, Overture Maps Foundation; ODbL 1.0.
"""
import argparse, json, os, re, struct, sys, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_pack  # noqa: E402  (KINDS, KEEP, POINT_KINDS, AREA_KINDS, encode, clean, ring_area2)

BUCKET = 'https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com'
CYPRUS = (32.2, 34.5, 34.7, 35.8)
ATTRIBUTION = '© OpenStreetMap contributors, Overture Maps Foundation'
# built per region: one pack each would be too big
SPLIT = ['US', 'CA', 'RU', 'CN', 'IN', 'BR', 'AU', 'DE', 'FR', 'IT', 'GB', 'ES', 'PL', 'JP', 'ID', 'MX']
MAX_FILES = 990                      # in one release (GitHub allows 1000): one file per area, its bundle
# Overture's classes for each kind (infrastructure; military from land use, springs from water)
CLASSES = {'helipads': ['helipad', 'heliport'], 'water': ['drinking_water'], 'power': ['power_line', 'minor_line'],
           'radar': ['radar'], 'watchtowers': ['watchtower'], 'telecom': ['communication_tower', 'mobile_phone_tower'],
           'pipelines': ['pipeline'], 'dams': ['dam'], 'runways': ['runway'], 'borders': ['border_control']}
GROUPS = {'af': 'Africa', 'an': 'Antarctica', 'as': 'Asia', 'oc': 'Australia and Oceania', 'ca': 'Central America',
          'eu': 'Europe', 'na': 'North America', 'ru': 'Russia', 'sa': 'South America'}
CONTINENT = {'AD': 'eu', 'AE': 'as', 'AF': 'as', 'AI': 'ca', 'AL': 'eu', 'AM': 'as', 'AO': 'af', 'AQ': 'an',
             'AR': 'sa', 'AS': 'oc', 'AT': 'eu', 'AU': 'oc', 'AW': 'ca', 'AX': 'eu', 'AZ': 'as', 'BA': 'eu',
             'BD': 'as', 'BE': 'eu', 'BF': 'af', 'BG': 'eu', 'BH': 'as', 'BI': 'af', 'BJ': 'af', 'BL': 'ca',
             'BM': 'na', 'BN': 'as', 'BO': 'sa', 'BQ': 'ca', 'BR': 'sa', 'BS': 'ca', 'BT': 'as', 'BV': 'an',
             'BW': 'af', 'BY': 'eu', 'BZ': 'ca', 'CA': 'na', 'CC': 'oc', 'CD': 'af', 'CF': 'af', 'CG': 'af',
             'CH': 'eu', 'CI': 'af', 'CK': 'oc', 'CL': 'sa', 'CM': 'af', 'CN': 'as', 'CO': 'sa', 'CR': 'ca',
             'CU': 'ca', 'CV': 'af', 'CW': 'ca', 'CX': 'oc', 'CY': 'eu', 'CZ': 'eu', 'DE': 'eu', 'DJ': 'af',
             'DK': 'eu', 'DO': 'ca', 'DZ': 'af', 'EC': 'sa', 'EE': 'eu', 'EG': 'af', 'EH': 'af', 'ER': 'af',
             'ES': 'eu', 'ET': 'af', 'FI': 'eu', 'FJ': 'oc', 'FK': 'sa', 'FM': 'oc', 'FO': 'eu', 'FR': 'eu',
             'GA': 'af', 'GB': 'eu', 'GE': 'eu', 'GF': 'eu', 'GG': 'eu', 'GH': 'af', 'GI': 'eu', 'GL': 'na',
             'GM': 'af', 'GN': 'af', 'GP': 'ca', 'GQ': 'af', 'GR': 'eu', 'GS': 'an', 'GT': 'ca', 'GU': 'oc',
             'GW': 'af', 'GY': 'sa', 'HK': 'as', 'HM': 'an', 'HN': 'ca', 'HR': 'eu', 'HT': 'ca', 'HU': 'eu',
             'ID': 'as', 'IE': 'eu', 'IL': 'as', 'IM': 'eu', 'IN': 'as', 'IO': 'as', 'IQ': 'as', 'IR': 'as',
             'IS': 'eu', 'IT': 'eu', 'JE': 'eu', 'JM': 'ca', 'JO': 'as', 'JP': 'as', 'KE': 'af', 'KG': 'as',
             'KH': 'as', 'KI': 'oc', 'KM': 'af', 'KP': 'as', 'KR': 'as', 'KW': 'as', 'KY': 'ca', 'KZ': 'as',
             'LA': 'as', 'LB': 'as', 'LI': 'eu', 'LK': 'as', 'LR': 'af', 'LS': 'af', 'LT': 'eu', 'LU': 'eu',
             'LV': 'eu', 'LY': 'af', 'MA': 'af', 'MC': 'eu', 'MD': 'eu', 'ME': 'eu', 'MF': 'ca', 'MG': 'af',
             'MH': 'oc', 'MK': 'eu', 'ML': 'af', 'MM': 'as', 'MN': 'as', 'MO': 'as', 'MP': 'oc', 'MQ': 'ca',
             'MR': 'af', 'MS': 'ca', 'MT': 'eu', 'MU': 'af', 'MV': 'as', 'MW': 'af', 'MX': 'na', 'MY': 'as',
             'MZ': 'af', 'NA': 'af', 'NC': 'oc', 'NE': 'af', 'NF': 'oc', 'NG': 'af', 'NI': 'ca', 'NL': 'eu',
             'NO': 'eu', 'NP': 'as', 'NR': 'oc', 'NU': 'oc', 'NZ': 'oc', 'OM': 'as', 'PA': 'ca', 'PE': 'sa',
             'PF': 'oc', 'PG': 'oc', 'PH': 'as', 'PK': 'as', 'PL': 'eu', 'PM': 'na', 'PN': 'oc', 'PR': 'na',
             'PS': 'as', 'PT': 'eu', 'PW': 'oc', 'PY': 'sa', 'QA': 'as', 'RE': 'af', 'RO': 'eu', 'RS': 'eu',
             'RU': 'ru', 'RW': 'af', 'SB': 'oc', 'SC': 'af', 'SD': 'af', 'SE': 'eu', 'SG': 'as', 'SH': 'af',
             'SI': 'eu', 'SJ': 'eu', 'SK': 'eu', 'SL': 'af', 'SM': 'eu', 'SN': 'af', 'SO': 'af', 'SR': 'sa',
             'SS': 'af', 'ST': 'af', 'SV': 'ca', 'SX': 'ca', 'SY': 'as', 'SZ': 'af', 'TC': 'ca', 'TD': 'af',
             'TF': 'an', 'TG': 'af', 'TH': 'as', 'TJ': 'as', 'TK': 'oc', 'TL': 'as', 'TM': 'as', 'TN': 'af',
             'TO': 'oc', 'TR': 'eu', 'TV': 'oc', 'TW': 'as', 'TZ': 'af', 'UA': 'eu', 'UG': 'af', 'UM': 'oc',
             'US': 'na', 'UY': 'sa', 'UZ': 'as', 'VA': 'eu', 'VE': 'sa', 'VG': 'ca', 'VI': 'na', 'VN': 'as',
             'VU': 'oc', 'WF': 'oc', 'WS': 'oc', 'XK': 'eu', 'YE': 'as', 'YT': 'af', 'ZA': 'af', 'ZM': 'af',
             'ZW': 'af'}


def log(s):
    print(s, flush=True)


def latest_release():
    for attempt in range(4):
        try:
            x = urllib.request.urlopen(BUCKET + '/?list-type=2&prefix=release/&delimiter=/', timeout=60).read().decode()
            return sorted(re.findall(r'<Prefix>release/([^/<]+)/</Prefix>', x))[-1]
        except Exception as e:
            if attempt == 3:
                raise
            log('the list of Overture releases not got (%s); again' % e)
            time.sleep(15 * (attempt + 1))


def connect():
    import duckdb
    con = duckdb.connect()
    for s in ("INSTALL spatial", "LOAD spatial", "INSTALL httpfs", "LOAD httpfs", "SET s3_region='us-west-2'",
              "SET enable_object_cache=true", "SET http_retries=8"):
        con.execute(s)
    return con


def layer(release, theme, kind):
    return "read_parquet('s3://overturemaps-us-west-2/release/%s/theme=%s/type=%s/*', hive_partitioning=0)" % (release, theme, kind)


def inside(b):
    return 'bbox.xmin >= %f AND bbox.xmax <= %f AND bbox.ymin >= %f AND bbox.ymax <= %f' % (b[0], b[2], b[1], b[3])


def touching(b):
    return 'bbox.xmin <= %f AND bbox.xmax >= %f AND bbox.ymin <= %f AND bbox.ymax >= %f' % (b[2], b[0], b[3], b[1])


def release_date(rel):
    return datetime.strptime(rel[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- plan

def guess_group(lon, lat):
    if lat < -60:
        return 'an'
    if lon < -30:
        return 'na' if lat > 15 else 'ca' if lat > 7 else 'sa'
    if lon < 60:
        return 'eu' if lat > 36 else 'af'
    return 'oc' if lat < -10 or lon > 165 else 'as'


def slug(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')
    return s or 'area'


def plan(a):
    con = connect()
    rel = a.release or latest_release()
    prev = {}
    if a.previous and os.path.exists(a.previous) and os.path.getsize(a.previous) > 2:
        prev = json.load(open(a.previous, encoding='utf-8'))
    week = datetime.now(timezone.utc).strftime('%Y%m%d')
    if not a.only and not a.force and prev.get('overture') == rel:
        log('Overture %s was built already (index of %s): nothing to do' % (rel, prev.get('built', '?')[:10]))
        if a.github_output:
            with open(a.github_output, 'a', encoding='utf-8') as f:
                f.write('skip=true\ngroups=[]\nweek=%s\n' % week)
        return
    only = sorted(set(x.strip().upper() for x in a.only.split(',') if x.strip())) if a.only else None
    da = layer(rel, 'divisions', 'division_area')
    sel = (" AND country IN (%s)" % ','.join("'%s'" % c for c in only)) if only else ''
    t0 = time.time()
    rows = con.execute("""
        SELECT country, class, coalesce(names.common['en'], names.primary) AS name,
               ST_AsText(ST_SimplifyPreserveTopology(geometry, 0.01)),
               ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.05)),
               bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax
        FROM %s WHERE subtype IN ('country', 'dependency') AND country IS NOT NULL%s""" % (da, sel)).fetchall()
    best = {}
    for r in rows:   # its territorial waters (class maritime) where Overture has them, else its land
        c = r[0]
        if c not in best or (r[1] == 'maritime' and best[c][1] != 'maritime'):
            best[c] = r
    split = [c for c in SPLIT if c in best]
    regions = []
    if split:
        regions = con.execute("""
            SELECT country, region, coalesce(names.common['en'], names.primary) AS name,
                   ST_AsText(ST_SimplifyPreserveTopology(ST_Buffer(ST_SimplifyPreserveTopology(geometry, 0.01), 0.03), 0.01)),
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.05)),
                   bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax
            FROM %s WHERE subtype = 'region' AND class = 'land' AND country IN (%s)""" % (
            da, ','.join("'%s'" % c for c in split))).fetchall()
    log('%d countries, %d regions of %d big countries · %.0f s' % (len(best), len(regions), len(split), time.time() - t0))

    def outline(gj):
        g = json.loads(gj)
        polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']] if g['type'] == 'Polygon' else []
        return [[[round(x, 2), round(y, 2)] for x, y in ring] for poly in polys for ring in poly if len(ring) >= 4]

    areas = []
    for c, r in sorted(best.items()):
        if c in split:
            continue
        areas.append(dict(id=c.lower(), name=r[2], iso=[c], country=c, group=CONTINENT.get(c) or guess_group((r[5] + r[7]) / 2, (r[6] + r[8]) / 2),
                          bbox=[round(r[5], 4), round(r[6], 4), round(r[7], 4), round(r[8], 4)], outline=outline(r[4]), join=r[3]))
    country_name = dict((c, r[2]) for c, r in best.items())
    for r in regions:
        c = r[0]
        rid = (r[1] or (c + '-' + slug(r[2]))).lower()
        areas.append(dict(id=rid, name='%s (%s)' % (r[2], country_name.get(c, c)), iso=[c], country=c,
                          group=CONTINENT.get(c) or guess_group((r[5] + r[7]) / 2, (r[6] + r[8]) / 2),
                          bbox=[round(r[5], 4), round(r[6], 4), round(r[7], 4), round(r[8], 4)], outline=outline(r[4]), join=r[3]))
    ids = [x['id'] for x in areas]
    dup = sorted(set(i for i in ids if ids.count(i) > 1))
    if dup:
        sys.exit('two areas with the same id: ' + ', '.join(dup))
    # releases of at most MAX_FILES files: eu, or eu1, eu2… for a big group
    per = 1
    groups = sorted(set(x['group'] for x in areas))
    for g in groups:
        mine = sorted((x for x in areas if x['group'] == g), key=lambda x: x['id'])
        n = (len(mine) * per + MAX_FILES - 1) // MAX_FILES
        for i, x in enumerate(mine):
            x['chunk'] = g if n == 1 else '%s%d' % (g, i * n // len(mine) + 1)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(dict(overture=rel, week=week, made=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                       attribution=ATTRIBUTION, areas=areas), f, ensure_ascii=False)
    log('Overture %s · %d areas: %s · plan %d KB' % (rel, len(areas), ', '.join(
        '%s %d' % (g, sum(1 for x in areas if x['group'] == g)) for g in groups), os.path.getsize(a.out) // 1024))
    if a.github_output:
        with open(a.github_output, 'a', encoding='utf-8') as f:
            f.write('skip=false\ngroups=%s\nweek=%s\n' % (json.dumps(groups), week))


# ---------------------------------------------------------------- build

def wkb(b, pos=0):
    """The shapes of a WKB geometry: ('point', (x, y)), ('line', [(x, y)…]), ('polygon', [ring…]); multi ones split."""
    e = '<' if b[pos] == 1 else '>'
    t = struct.unpack_from(e + 'I', b, pos + 1)[0]
    pos += 5
    dims = 2
    if t & 0x80000000:
        dims += 1
    if t & 0x40000000:
        dims += 1
    if t & 0x20000000:
        pos += 4
    t &= 0x0FFFFFFF
    if t >= 3000:
        dims, t = 4, t - 3000
    elif t >= 1000:
        dims, t = 3, t % 1000
    step = 8 * dims
    out = []

    def pts(pos):
        n = struct.unpack_from(e + 'I', b, pos)[0]
        pos += 4
        p = [struct.unpack_from(e + 'dd', b, pos + i * step) for i in range(n)]
        return p, pos + n * step

    if t == 1:
        x, y = struct.unpack_from(e + 'dd', b, pos)
        out.append(('point', (x, y)))
        pos += step
    elif t == 2:
        p, pos = pts(pos)
        out.append(('line', p))
    elif t == 3:
        nr = struct.unpack_from(e + 'I', b, pos)[0]
        pos += 4
        rings = []
        for _ in range(nr):
            p, pos = pts(pos)
            rings.append(p)
        out.append(('polygon', rings))
    elif t in (4, 5, 6, 7):
        n = struct.unpack_from(e + 'I', b, pos)[0]
        pos += 4
        for _ in range(n):
            sub, pos = wkb(b, pos)
            out.extend(sub)
    return out, pos


def q(v):
    return int(round(v * 1e6))


def to_features(kind, rec, upd, ds, name, tags, geom):
    """The pack features (build_pack's form) of one Overture row."""
    t = dict((k.encode('utf-8'), (v or '').encode('utf-8')) for k, v in (tags or {}).items())
    if name and b'name' not in t:
        t[b'name'] = name.encode('utf-8')
    otype, oid = 3, 0
    m = re.match(r'^([nwr])(\d+)', rec or '')
    if m and (ds or 'OpenStreetMap') == 'OpenStreetMap':
        otype, oid = 'nwr'.index(m.group(1)), int(m.group(2))
    ts = 0
    if upd:
        try:
            ts = int(datetime.fromisoformat(upd.replace('Z', '+00:00')).timestamp())
        except ValueError:
            pass
    shapes, _ = wkb(geom)
    out = []
    polys = []
    for typ, g in shapes:
        if typ == 'point':
            out.append(dict(otype=otype, id=oid, t=t, ts=ts, geom=1, g=(q(g[0]), q(g[1]))))
        elif typ == 'line':
            line = build_pack.clean([(q(x), q(y)) for x, y in g])
            if len(line) >= 2:
                out.append(dict(otype=otype, id=oid, t=t, ts=ts, geom=2, g=line))
        elif typ == 'polygon' and kind in build_pack.AREA_KINDS:
            poly = []
            for k, ring in enumerate(g):
                r = build_pack.clean([(q(x), q(y)) for x, y in ring])
                if len(r) >= 2 and r[0] == r[-1]:
                    r = r[:-1]
                if len(r) < 3:
                    continue
                ccw = build_pack.ring_area2(r + r[:1]) > 0
                if (k == 0) != ccw:
                    r = r[::-1]
                if k == 0 or poly:
                    poly.append(r)
            if poly:
                polys.append(poly)
        elif typ == 'polygon' and g and len(g[0]) >= 2:   # an area of a line kind: its outline
            line = build_pack.clean([(q(x), q(y)) for x, y in g[0]])
            if len(line) >= 2:
                out.append(dict(otype=otype, id=oid, t=t, ts=ts, geom=2, g=line))
    if polys:
        out.append(dict(otype=otype, id=oid, t=t, ts=ts, geom=3, g=polys))
    return out


def build(a):
    p = json.load(open(a.plan, encoding='utf-8'))
    rel = p['overture']
    areas = [x for x in p['areas'] if x['group'] == a.group]
    if not areas:
        sys.exit('no areas in group ' + a.group)
    os.makedirs(a.out, exist_ok=True)
    data_ts = int(release_date(rel).timestamp())
    con = connect()
    t0 = time.time()
    con.execute('CREATE TABLE areas (id VARCHAR, geom GEOMETRY, xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)')
    con.executemany('INSERT INTO areas VALUES (?, ST_GeomFromText(?), ?, ?, ?, ?)',
                    [(x['id'], x['join'], x['bbox'][0], x['bbox'][1], x['bbox'][2], x['bbox'][3]) for x in areas])
    b = (min(x['bbox'][0] for x in areas) - 0.1, min(x['bbox'][1] for x in areas) - 0.1,
         max(x['bbox'][2] for x in areas) + 0.1, max(x['bbox'][3] for x in areas) + 0.1)
    kind_of = dict((c, k) for k, cs in CLASSES.items() for c in cs)
    case = 'CASE ' + ' '.join("WHEN class = '%s' THEN '%s'" % (c, k) for c, k in kind_of.items()) + ' END'
    cols = ("names.primary AS name, source_tags AS tags, sources[1].record_id AS rec, sources[1].update_time AS upd, "
            "sources[1].dataset AS ds, geometry, bbox")
    con.execute("CREATE TABLE raw AS SELECT %s AS kind, %s FROM %s WHERE %s AND class IN (%s)" % (
        case, cols, layer(rel, 'base', 'infrastructure'), touching(b), ','.join("'%s'" % c for c in kind_of)))
    log('infrastructure: %d · %.0f s' % (con.execute('SELECT count(*) FROM raw').fetchone()[0], time.time() - t0))
    con.execute("INSERT INTO raw SELECT 'military', %s FROM %s WHERE %s AND subtype = 'military'" % (
        cols, layer(rel, 'base', 'land_use'), touching(b)))
    con.execute("INSERT INTO raw SELECT 'water', %s FROM %s WHERE %s AND class = 'spring'" % (
        cols, layer(rel, 'base', 'water'), touching(b)))
    log('with military areas and springs: %d · %.0f s' % (con.execute('SELECT count(*) FROM raw').fetchone()[0], time.time() - t0))
    con.execute("""CREATE TABLE j AS SELECT a.id AS area, r.kind, r.rec, r.upd, r.ds, r.name, r.tags,
                   ST_AsWKB(CASE WHEN r.kind IN (%s) THEN ST_PointOnSurface(r.geometry) ELSE r.geometry END) AS g
                   FROM raw r JOIN areas a ON r.bbox.xmin <= a.xmax AND r.bbox.xmax >= a.xmin AND r.bbox.ymin <= a.ymax
                   AND r.bbox.ymax >= a.ymin AND ST_Intersects(a.geom, r.geometry)""" % (
        ','.join("'%s'" % k for k in build_pack.POINT_KINDS)))
    log('in the areas: %d · %.0f s' % (con.execute('SELECT count(*) FROM j').fetchone()[0], time.time() - t0))

    by_id = dict((x['id'], x) for x in areas)
    summaries = {}

    def summary(aid):
        if aid not in summaries:
            x = by_id[aid]
            summaries[aid] = dict(id=aid, name=x['name'], format=build_pack.FORMAT, bbox=x['bbox'],
                                  data=release_date(rel).strftime('%Y-%m-%dT%H:%M:%SZ'), overture=rel,
                                  built=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), kinds={})
        return summaries[aid]

    def write(aid, kind, feats):
        raw, n, nv = build_pack.encode(kind, feats, data_ts)
        gz = build_pack.gzip.compress(raw, 9, mtime=0)
        fname = '%s-%s.amf' % (aid, kind)
        with open(os.path.join(a.out, fname), 'wb') as f:
            f.write(gz)
        summary(aid)['kinds'][kind] = dict(file=fname, count=n, points=nv, size=len(gz), raw=len(raw))

    cur = con.execute('SELECT area, kind, rec, upd, ds, name, tags, g FROM j ORDER BY area, kind')
    key, feats = None, []
    while True:
        rows = cur.fetchmany(20000)
        for area, kind, rec, upd, ds, name, tags, g in rows:
            if (area, kind) != key:
                if key:
                    write(key[0], key[1], feats)
                key, feats = (area, kind), []
            feats.extend(to_features(kind, rec, upd, ds, name, tags, bytes(g)))
        if not rows:
            break
    if key:
        write(key[0], key[1], feats)
    for x in areas:   # every area has its eleven packs, empty or not
        for k in build_pack.KINDS:
            if k not in summary(x['id'])['kinds']:
                write(x['id'], k, [])
    releases = {}
    for x in areas:
        s = summaries[x['id']]
        bundle(a.out, s)
        s['release'] = '%s-%s' % (x['chunk'], p['week'])
        with open(os.path.join(a.out, x['id'] + '.json'), 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
        releases.setdefault(s['release'], []).append(s['bundle'])
    with open(os.path.join(a.out, 'releases.json'), 'w', encoding='utf-8') as f:
        json.dump(releases, f)
    for tag, files in releases.items():
        size = sum(os.path.getsize(os.path.join(a.out, n)) for n in files)
        with open(os.path.join(a.out, 'NOTES-%s.md' % tag), 'w', encoding='utf-8') as f:
            f.write('Map-feature packs of %s, week of %s, from Overture Maps %s: %d areas, %.1f MB. %s; ODbL 1.0.\n' % (
                GROUPS.get(a.group, a.group), p['week'], rel, len(files), size / 1e6, ATTRIBUTION))
    total = sum(v['size'] for s in summaries.values() for v in s['kinds'].values())
    log('%s: %d areas, %.1f MB of packs in %d release(s) · %.0f s' % (a.group, len(areas), total / 1e6, len(releases),
                                                                     time.time() - t0))


def bundle(out, s):
    """An area's eleven packs in one file, <id>.amfb, so a release holds one file per area (GitHub's limits are of
    files and of uploads): "AMFB" u8 1, varint count, then each pack's kind name, offset and size (varints, the name with
    its length first), then the packs one after the other. The index has the offsets too: the app asks for the bytes of
    the kinds wanted only (an HTTP Range request) and cuts them out of the whole file should a server send all of it."""
    parts = []
    for k in build_pack.KINDS:
        v = s['kinds'][k]
        path = os.path.join(out, v['file'])
        parts.append((k, open(path, 'rb').read()))
        os.remove(path)
    def head(offsets):
        o = build_pack.Out()
        o.b += b'AMFB'
        o.u8(1)
        o.var(len(parts))
        for (k, data), off in zip(parts, offsets):
            n = k.encode('utf-8')
            o.var(len(n))
            o.b += n
            o.var(off)
            o.var(len(data))
        return bytes(o.b)
    offsets = [0] * len(parts)
    for _ in range(4):   # (the header's size depends on the offsets, which depend on it)
        h = head(offsets)
        pos, new = len(h), []
        for _, data in parts:
            new.append(pos)
            pos += len(data)
        if new == offsets:
            break
        offsets = new
    h = head(offsets)
    name = s['id'] + '.amfb'
    with open(os.path.join(out, name), 'wb') as f:
        f.write(h)
        for _, data in parts:
            f.write(data)
    for (k, data), off in zip(parts, offsets):
        s['kinds'][k]['offset'] = off
        s['kinds'][k]['file'] = name
    s['bundle'] = name


# ---------------------------------------------------------------- probe

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
    s = sub.add_parser('plan')
    s.add_argument('--out', required=True)
    s.add_argument('--only', default='', help='countries (ISO codes), comma-separated, for a test run')
    s.add_argument('--previous', help='the index now published: a release already built is skipped')
    s.add_argument('--force', action='store_true', help='build even if this Overture release was built already')
    s.add_argument('--release', help='an Overture release, e.g. 2026-10-21.0 (default: the latest)')
    s.add_argument('--github-output')
    s = sub.add_parser('build')
    s.add_argument('--plan', required=True)
    s.add_argument('--group', required=True)
    s.add_argument('--out', required=True)
    s = sub.add_parser('probe')
    s.add_argument('--area', help='west,south,east,north (default: Cyprus)')
    s.add_argument('--release', help='an Overture release, e.g. 2026-10-21.0 (default: the latest)')
    a = ap.parse_args()
    dict(plan=plan, build=build, probe=probe)[a.cmd](a)


if __name__ == '__main__':
    main()
