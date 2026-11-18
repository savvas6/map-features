#!/usr/bin/env python3
"""Map-feature packs: the packs of one area, from an OpenStreetMap extract (.osm.pbf), and the pack format.

The weekly packs are built from Overture Maps instead (overture.py, which uses this file's KINDS, KEEP and encode());
this reader of .osm.pbf files makes the same kinds from a Geofabrik extract, with nothing to install, for checks.

The eleven kinds of the app's «Map features» sheet:
  military     military areas, firing and training ranges, bases, danger areas   landuse=military, military=*
  helipads     helipads and heliports                                              aeroway=helipad|heliport
  water        drinking water and springs (and wells, taps, water points here)    amenity=drinking_water, natural=spring…
  power        power lines; their points are the pylons and poles                 power=line|minor_line
  radar        radar sites                                                         tower:type=radar
  watchtowers  watchtowers                                                         tower:type=watchtower
  telecom      communication and mobile phone towers                              tower:type=communication
  pipelines    pipelines (oil, gas, water…), also under ground and under water     man_made=pipeline
  dams         dams                                                                waterway=dam
  runways      runways                                                             aeroway=runway
  borders      border crossings                                                    barrier=border_control

  python3 -I build_pack.py EXTRACT.osm.pbf --id cyprus --name Cyprus --out DIR [--geojson] [--verbose]

Writes DIR/<id>-<kind>.amf for each kind (gzip) and DIR/<id>.json (counts, sizes, the date of the data).
The data are © OpenStreetMap contributors, ODbL 1.0, and so are the packs.

Pack format 2 (after gunzip; little-endian varints as in protobuf, «zz» = zigzag varint):
  "AMFP"  u8 format=2  varint length + the kind's name (UTF-8: «military», «power»…)
  varint  data time (unix seconds of the OpenStreetMap data)
  varint  string count, then each string: varint byte length + UTF-8
  varint  feature count, then each feature:
    u8      geometry (bits 0-1: 1 point, 2 line, 3 polygons) | source << 2 (0 OSM node, 1 way, 2 relation, 3 other)
    varint  id (the OSM id; 0 for another source)
    varint  last edited, in days since 1970-01-01 (0 = unknown)
    varint  tag count, then varint key and value string indexes (OpenStreetMap tags)
    point:    zz dx, zz dy
    line:     varint n, then n × (zz dx, zz dy)
    polygons: varint polygon count; each: varint ring count (the outer ring first, then its holes);
              each ring: varint n, then n × (zz dx, zz dy); a ring is not closed (its last point is not the first)
  Coordinates are 1e-6 degrees (lon x, lat y), each a difference from the previous coordinate of the file
  (starting at 0,0). Features are in Morton order, so neighbours are near each other. Outer rings turn
  counter-clockwise and holes clockwise.
"""
import argparse, gzip, json, os, re, struct, sys, time, zlib
from datetime import datetime, timezone

FORMAT = 2
KINDS = ['military', 'helipads', 'water', 'power', 'radar', 'watchtowers', 'telecom', 'pipelines', 'dams', 'runways',
         'borders']
POINT_KINDS = {'helipads', 'water', 'radar', 'watchtowers', 'telecom', 'borders'}   # an area becomes its middle point
AREA_KINDS = {'military', 'dams', 'runways'}                                       # a closed way is an area, else a line
# (power and pipelines: always lines)

TRIGGER_KEYS = set([b'landuse', b'military', b'aeroway', b'amenity', b'man_made', b'natural', b'power', b'tower:type',
                    b'waterway', b'barrier'])

NAME = ['name', 'name:en']
KEEP = {
    'military': ['military', 'landuse'] + NAME + ['military_service', 'operator', 'access', 'ref', 'opening_hours',
                                                  'description'],
    'helipads': ['aeroway'] + NAME + ['ref', 'icao', 'iata', 'operator', 'emergency', 'surface', 'lit', 'ele', 'access',
                                      'description'],
    'water': ['amenity', 'man_made', 'natural'] + NAME + ['drinking_water', 'pump', 'seasonal', 'intermittent', 'access',
                                                         'fee', 'operator', 'ele', 'description'],
    'power': ['power', 'voltage', 'cables', 'circuits', 'frequency', 'operator', 'name', 'ref', 'location'],
    'radar': ['man_made', 'tower:type'] + NAME + ['operator', 'military', 'height', 'ele', 'ref', 'description'],
    'watchtowers': ['man_made', 'tower:type'] + NAME + ['operator', 'military', 'height', 'ele', 'access',
                                                        'description'],
    'telecom': ['man_made', 'tower:type'] + NAME + ['operator', 'height', 'ele', 'ref', 'communication:mobile_phone',
                                                    'communication:radio', 'communication:television',
                                                    'communication:microwave', 'description'],
    'pipelines': ['man_made', 'substance', 'type', 'location', 'usage', 'diameter', 'pressure', 'operator'] + NAME +
                 ['ref', 'disused', 'description'],
    'dams': ['waterway'] + NAME + ['height', 'ele', 'material', 'operator', 'ref', 'description'],
    'runways': ['aeroway', 'ref'] + NAME + ['length', 'width', 'surface', 'lit', 'ele', 'operator'],
    'borders': ['barrier'] + NAME + ['local_name', 'operator', 'opening_hours', 'access', 'description'],
}


def classify(t, otype):
    """The kind of an object from its tags (bytes → bytes), or None. otype: 'n', 'w' or 'r'."""
    tower = t.get(b'tower:type')
    mil = t.get(b'military')
    if tower == b'radar':
        kind = 'radar'
    elif tower == b'watchtower':
        kind = 'watchtowers'
    elif tower == b'communication' and t.get(b'man_made') in (b'tower', b'mast'):
        kind = 'telecom'
    elif t.get(b'landuse') == b'military' or (mil is not None and mil != b'no'):
        kind = 'military'
    elif t.get(b'aeroway') in (b'helipad', b'heliport'):
        kind = 'helipads'
    elif t.get(b'aeroway') == b'runway':
        kind = 'runways'
    elif (t.get(b'amenity') in (b'drinking_water', b'water_point') or t.get(b'man_made') in (b'water_well', b'water_tap')
          or t.get(b'natural') == b'spring'):
        kind = 'water'
    elif otype == 'w' and t.get(b'power') in (b'line', b'minor_line') and t.get(b'line') not in (b'busbar', b'bay'):
        kind = 'power'
    elif otype == 'w' and t.get(b'man_made') == b'pipeline':
        kind = 'pipelines'
    elif t.get(b'waterway') == b'dam':
        kind = 'dams'
    elif t.get(b'barrier') == b'border_control':
        kind = 'borders'
    else:
        return None
    # relations: areas only, as multipolygons or boundaries (the UN buffer zone in Cyprus is a type=boundary relation)
    if otype == 'r' and (t.get(b'type') not in (b'multipolygon', b'boundary') or kind in ('power', 'pipelines')):
        return None
    return kind


# ---------------------------------------------------------------- PBF, read by hand (protobuf wire format)

def varint(buf, pos):
    b = buf[pos]
    pos += 1
    if b < 0x80:
        return b, pos
    v = b & 0x7f
    s = 7
    while True:
        b = buf[pos]
        pos += 1
        v |= (b & 0x7f) << s
        if b < 0x80:
            return v, pos
        s += 7


def fields(buf, pos=0, end=None):
    """(field number, value): an int for varints, (start, end) for bytes."""
    if end is None:
        end = len(buf)
    while pos < end:
        key, pos = varint(buf, pos)
        wt = key & 7
        if wt == 0:
            v, pos = varint(buf, pos)
            yield key >> 3, v
        elif wt == 2:
            n, pos = varint(buf, pos)
            yield key >> 3, (pos, pos + n)
            pos += n
        elif wt == 1:
            yield key >> 3, buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            yield key >> 3, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError('unsupported protobuf wire type %d' % wt)


def uints(buf, r):
    pos, end = r
    out = []
    ap = out.append
    while pos < end:
        b = buf[pos]
        pos += 1
        if b < 0x80:
            ap(b)
            continue
        v = b & 0x7f
        s = 7
        while True:
            b = buf[pos]
            pos += 1
            v |= (b & 0x7f) << s
            if b < 0x80:
                break
            s += 7
        ap(v)
    return out


def sint_deltas(buf, r):
    """Packed zigzag varints, each one added to the previous (the DenseNodes and refs encoding)."""
    pos, end = r
    out = []
    ap = out.append
    acc = 0
    while pos < end:
        b = buf[pos]
        pos += 1
        if b < 0x80:
            v = b
        else:
            v = b & 0x7f
            s = 7
            while True:
                b = buf[pos]
                pos += 1
                v |= (b & 0x7f) << s
                if b < 0x80:
                    break
                s += 7
        acc += (v >> 1) ^ -(v & 1)
        ap(acc)
    return out


def int64(v):
    return v - (1 << 64) if v >= (1 << 63) else v


def zz(v):
    return (v >> 1) ^ -(v & 1)


class Pbf:
    def __init__(self, path):
        self.path = path
        self.blobs = []  # (offset, size, type)
        with open(path, 'rb') as f:
            while True:
                h = f.read(4)
                if len(h) < 4:
                    break
                (hl,) = struct.unpack('>I', h)
                hdr = f.read(hl)
                btype, size = '', 0
                for fn, v in fields(hdr):
                    if fn == 1:
                        btype = hdr[v[0]:v[1]].decode()
                    elif fn == 3:
                        size = v
                self.blobs.append((f.tell(), size, btype))
                f.seek(size, 1)
        self.f = open(path, 'rb')

    def data(self, i):
        off, size, _ = self.blobs[i]
        self.f.seek(off)
        blob = self.f.read(size)
        raw = z = None
        for fn, v in fields(blob):
            if fn == 1:
                raw = blob[v[0]:v[1]]
            elif fn == 3:
                z = blob[v[0]:v[1]]
            elif fn in (4, 5, 6, 7):
                raise ValueError('this extract uses a compression other than zlib (field %d)' % fn)
        return raw if raw is not None else zlib.decompress(z)

    def header(self):
        for i, (_, _, t) in enumerate(self.blobs):
            if t == 'OSMHeader':
                d = self.data(i)
                req, ts, bbox = [], 0, None
                for fn, v in fields(d):
                    if fn == 1:
                        b = dict((gfn, zz(gv)) for gfn, gv in fields(d, v[0], v[1]))
                        bbox = [b.get(1, 0) / 1e9, b.get(4, 0) / 1e9, b.get(2, 0) / 1e9, b.get(3, 0) / 1e9]
                    elif fn == 4:
                        req.append(d[v[0]:v[1]].decode())
                    elif fn == 32:
                        ts = int64(v)
                return req, ts, bbox
        return [], 0, None


def block(d):
    st, groups, gran, lat0, lon0, dgran = None, [], 100, 0, 0, 1000
    for fn, v in fields(d):
        if fn == 1:
            st = v
        elif fn == 2:
            groups.append(v)
        elif fn == 17:
            gran = v
        elif fn == 18:
            dgran = v
        elif fn == 19:
            lat0 = int64(v)
        elif fn == 20:
            lon0 = int64(v)
    strings = [d[a:b] for fn, (a, b) in fields(d, st[0], st[1]) if fn == 1] if st else []
    return strings, groups, gran, lat0, lon0, dgran


def group_kind(d, g):
    for fn, _ in fields(d, g[0], g[1]):
        return fn  # 1 nodes, 2 dense nodes, 3 ways, 4 relations
    return 0


def items(d, g, want):
    for fn, v in fields(d, g[0], g[1]):
        if fn == want:
            yield v


def info_time(d, r, dgran):
    for fn, v in fields(d, r[0], r[1]):
        if fn == 2:
            return int64(v) * dgran // 1000
    return 0


def q6(nano):
    """Nanodegrees to 1e-6 degrees, rounded."""
    return (nano + 500) // 1000


OFF = 1 << 31   # node coordinates are kept as one int each, (x + OFF) << 32 | (y + OFF): half the memory of a tuple


def unpack(v):
    return ((v >> 32) - OFF, (v & 0xFFFFFFFF) - OFF)


# ---------------------------------------------------------------- reading the extract

def read_extract(path, log):
    pbf = Pbf(path)
    req, data_ts, bbox = pbf.header()
    bad = [r for r in req if r not in ('OsmSchema-V0.6', 'DenseNodes')]
    if bad:
        raise ValueError('unsupported extract features: %s' % ', '.join(bad))
    nblobs = len(pbf.blobs)
    kinds_of_blob = [set() for _ in range(nblobs)]
    ways, rels = [], []          # matched ways and multipolygon relations
    way_refs = {}                # way id -> node ids, for every way we need
    t0 = time.time()
    # pass 1: ways and relations
    for i in range(nblobs):
        if pbf.blobs[i][2] != 'OSMData':
            continue
        d = pbf.data(i)
        strings, groups, gran, lat0, lon0, dgran = block(d)
        trig = set(j for j, s in enumerate(strings) if s in TRIGGER_KEYS)
        for g in groups:
            gk = group_kind(d, g)
            kinds_of_blob[i].add(gk)
            if gk == 3:
                for r in items(d, g, 3):
                    wid = 0
                    keys = vals = refs = info = None
                    for fn, v in fields(d, r[0], r[1]):
                        if fn == 1:
                            wid = v
                        elif fn == 2:
                            keys = v
                        elif fn == 3:
                            vals = v
                        elif fn == 4:
                            info = v
                        elif fn == 8:
                            refs = v
                    if keys is None:
                        continue
                    ks = uints(d, keys)
                    if not any(k in trig for k in ks):
                        continue
                    t = dict(zip((strings[k] for k in ks), (strings[v] for v in uints(d, vals))))
                    kind = classify(t, 'w')
                    if kind is None:
                        continue
                    nodes = sint_deltas(d, refs) if refs else []
                    way_refs[wid] = nodes
                    ways.append((wid, kind, t, info_time(d, info, dgran) if info else 0))
            elif gk == 4:
                for r in items(d, g, 4):
                    rid = 0
                    keys = vals = roles = mems = types = info = None
                    for fn, v in fields(d, r[0], r[1]):
                        if fn == 1:
                            rid = v
                        elif fn == 2:
                            keys = v
                        elif fn == 3:
                            vals = v
                        elif fn == 4:
                            info = v
                        elif fn == 8:
                            roles = v
                        elif fn == 9:
                            mems = v
                        elif fn == 10:
                            types = v
                    if keys is None:
                        continue
                    ks = uints(d, keys)
                    if not any(k in trig for k in ks):
                        continue
                    t = dict(zip((strings[k] for k in ks), (strings[v] for v in uints(d, vals))))
                    kind = classify(t, 'r')
                    if kind is None or mems is None:
                        continue
                    ids = sint_deltas(d, mems)
                    tps = uints(d, types) if types else [0] * len(ids)
                    rls = [strings[x] for x in uints(d, roles)] if roles else [b''] * len(ids)
                    members = [(m, rl) for m, tp, rl in zip(ids, tps, rls) if tp == 1]
                    rels.append((rid, kind, t, info_time(d, info, dgran) if info else 0, members))
    log('pass 1 · ways and relations: %d ways, %d multipolygons · %.1f s' % (len(ways), len(rels), time.time() - t0))
    # pass 2: the member ways of the multipolygons
    need_ways = set(m for rel in rels for m, _ in rel[4] if m not in way_refs)
    if need_ways:
        t0 = time.time()
        for i in range(nblobs):
            if 3 not in kinds_of_blob[i]:
                continue
            d = pbf.data(i)
            _, groups, _, _, _, _ = block(d)
            for g in groups:
                if group_kind(d, g) != 3:
                    continue
                for r in items(d, g, 3):
                    wid = 0
                    refs = None
                    for fn, v in fields(d, r[0], r[1]):
                        if fn == 1:
                            wid = v
                        elif fn == 8:
                            refs = v
                    if wid in need_ways:
                        way_refs[wid] = sint_deltas(d, refs) if refs else []
        log('pass 2 · member ways: %d of %d found · %.1f s' % (len(need_ways & set(way_refs)), len(need_ways),
                                                               time.time() - t0))
    # pass 3: nodes, for the coordinates we need and the tagged points
    need = set()
    for nodes in way_refs.values():
        need.update(nodes)
    coords = {}
    points = []
    t0 = time.time()
    seen = 0
    for i in range(nblobs):
        if not (kinds_of_blob[i] & {1, 2}):
            continue
        d = pbf.data(i)
        strings, groups, gran, lat0, lon0, dgran = block(d)
        trig = set(j for j, s in enumerate(strings) if s in TRIGGER_KEYS)
        for g in groups:
            gk = group_kind(d, g)
            if gk == 2:
                for r in items(d, g, 2):
                    idr = latr = lonr = kvr = infor = None
                    for fn, v in fields(d, r[0], r[1]):
                        if fn == 1:
                            idr = v
                        elif fn == 5:
                            infor = v
                        elif fn == 8:
                            latr = v
                        elif fn == 9:
                            lonr = v
                        elif fn == 10:
                            kvr = v
                    ids = sint_deltas(d, idr)
                    lats = sint_deltas(d, latr)
                    lons = sint_deltas(d, lonr)
                    seen += len(ids)
                    for j, nid in enumerate(ids):
                        if nid in need:
                            coords[nid] = ((q6(lon0 + gran * lons[j]) + OFF) << 32) | (q6(lat0 + gran * lats[j]) + OFF)
                    if kvr is None or not trig:
                        continue
                    kv = uints(d, kvr)
                    if not any(k in trig for k in kv):
                        continue
                    times = None
                    j = p = 0
                    n = len(kv)
                    while p < n:
                        if kv[p] == 0:
                            p += 1
                            j += 1
                            continue
                        start = p
                        while kv[p] != 0:
                            p += 2
                        pairs = kv[start:p]
                        ks = pairs[0::2]
                        if any(k in trig for k in ks):
                            t = dict(zip((strings[k] for k in ks), (strings[v] for v in pairs[1::2])))
                            kind = classify(t, 'n')
                            if kind is not None:
                                if times is None:
                                    times = [0] * len(ids)
                                    if infor is not None:
                                        for fn, v in fields(d, infor[0], infor[1]):
                                            if fn == 2:
                                                times = [x * dgran // 1000 for x in sint_deltas(d, v)]
                                points.append((ids[j], kind, t, times[j] if j < len(times) else 0,
                                               (q6(lon0 + gran * lons[j]), q6(lat0 + gran * lats[j]))))
                        p += 1
                        j += 1
            elif gk == 1:
                for r in items(d, g, 1):
                    nid = 0
                    keys = vals = info = None
                    lat = lon = 0
                    for fn, v in fields(d, r[0], r[1]):
                        if fn == 1:
                            nid = zz(v)
                        elif fn == 2:
                            keys = v
                        elif fn == 3:
                            vals = v
                        elif fn == 4:
                            info = v
                        elif fn == 8:
                            lat = zz(v)
                        elif fn == 9:
                            lon = zz(v)
                    seen += 1
                    xy = (q6(lon0 + gran * lon), q6(lat0 + gran * lat))
                    if nid in need:
                        coords[nid] = ((xy[0] + OFF) << 32) | (xy[1] + OFF)
                    if keys is not None:
                        ks = uints(d, keys)
                        if any(k in trig for k in ks):
                            t = dict(zip((strings[k] for k in ks), (strings[v] for v in uints(d, vals))))
                            kind = classify(t, 'n')
                            if kind is not None:
                                points.append((nid, kind, t, info_time(d, info, dgran) if info else 0, xy))
    log('pass 3 · %d nodes read, %d of %d needed found, %d tagged points · %.1f s' % (
        seen, len(coords), len(need), len(points), time.time() - t0))
    return dict(data_ts=data_ts, bbox=bbox, ways=ways, rels=rels, way_refs=way_refs, coords=coords, points=points)


# ---------------------------------------------------------------- shapes

def ring_area2(r):
    """Twice the signed area of a closed ring (first point == last)."""
    a = 0
    for i in range(len(r) - 1):
        a += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]
    return a


def centroid(r):
    x0, y0 = r[0]
    a = cx = cy = 0.0
    for i in range(len(r) - 1):
        x1, y1 = r[i][0] - x0, r[i][1] - y0
        x2, y2 = r[i + 1][0] - x0, r[i + 1][1] - y0
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    if abs(a) < 1e-9:
        n = len(r)
        return (sum(p[0] for p in r) // n, sum(p[1] for p in r) // n)
    return (int(round(x0 + cx / (3 * a))), int(round(y0 + cy / (3 * a))))


def inside(pt, r):
    x, y = pt
    c = False
    for i in range(len(r) - 1):
        x1, y1 = r[i]
        x2, y2 = r[i + 1]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def join_rings(ways):
    """Closed rings (node id lists, first == last) from the member ways of a multipolygon."""
    ways = [w for w in ways if len(w) >= 2]
    ends = {}
    for i, w in enumerate(ways):
        ends.setdefault(w[0], []).append(i)
        ends.setdefault(w[-1], []).append(i)
    used = [False] * len(ways)
    rings = []
    for i, w in enumerate(ways):
        if used[i]:
            continue
        used[i] = True
        ring = list(w)
        while ring[0] != ring[-1]:
            tail = ring[-1]
            nxt = next((j for j in ends.get(tail, ()) if not used[j]), None)
            if nxt is None:
                break
            used[nxt] = True
            wj = ways[nxt]
            ring.extend(wj[1:] if wj[0] == tail else wj[-2::-1])
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def polygons_of(rings):
    """[[outer, hole, …], …] from closed coordinate rings, nesting found by area and point tests."""
    rings = sorted(rings, key=lambda r: -abs(ring_area2(r)))
    depth, owner, polys = [], [], []
    for i, r in enumerate(rings):
        probe = ((r[0][0] + r[1][0]) / 2.0, (r[0][1] + r[1][1]) / 2.0)
        parent = None
        for j in range(i - 1, -1, -1):
            if inside(probe, rings[j]):
                parent = j
                break
        dp = depth[parent] + 1 if parent is not None else 0
        depth.append(dp)
        if dp % 2 == 0:
            owner.append(len(polys))
            polys.append([r])
        else:
            owner.append(owner[parent])
            polys[owner[parent]].append(r)
    out = []
    for p in polys:
        q = []
        for k, r in enumerate(p):
            ccw = ring_area2(r) > 0
            if (k == 0) != ccw:
                r = r[::-1]
            q.append(r[:-1])  # stored open
        out.append(q)
    return out


def clean(line):
    """Drops repeated points (two nodes at the same place after rounding)."""
    out = [line[0]]
    for p in line[1:]:
        if p != out[-1]:
            out.append(p)
    return out


def make_features(ex, log, vlog=lambda s: None):
    coords = ex['coords']
    feats = {k: [] for k in KINDS}
    dropped = {k: 0 for k in KINDS}
    why = []
    for nid, kind, t, ts, xy in ex['points']:
        feats[kind].append(dict(otype=0, id=nid, t=t, ts=ts, geom=1, g=xy))
    for wid, kind, t, ts in ex['ways']:
        pts = [unpack(coords[n]) for n in ex['way_refs'][wid] if n in coords]
        refs = ex['way_refs'][wid]
        if len(pts) < 2:
            dropped[kind] += 1
            why.append('%s way/%d: %d of %d nodes' % (kind, wid, len(pts), len(refs)))
            continue
        closed = refs[0] == refs[-1] and len(refs) >= 4 and len(pts) == len(refs)
        if kind in AREA_KINDS and closed:
            feats[kind].append(dict(otype=1, id=wid, t=t, ts=ts, geom=3, g=polygons_of([pts])))
        elif kind in POINT_KINDS:
            c = centroid(pts) if closed else pts[len(pts) // 2]
            feats[kind].append(dict(otype=1, id=wid, t=t, ts=ts, geom=1, g=c))
        else:
            line = clean(pts)
            if len(line) < 2:
                dropped[kind] += 1
                continue
            feats[kind].append(dict(otype=1, id=wid, t=t, ts=ts, geom=2, g=line))
    for rid, kind, t, ts, members in ex['rels']:
        rings = []
        for ring in join_rings([ex['way_refs'].get(m, []) for m, _ in members]):
            pts = [unpack(coords[n]) for n in ring if n in coords]
            if len(pts) == len(ring):
                rings.append(pts)
        if not rings:
            dropped[kind] += 1
            why.append('%s relation/%d: no closed ring from %d member ways' % (kind, rid, len(members)))
            continue
        polys = polygons_of(rings)
        if kind not in POINT_KINDS:
            feats[kind].append(dict(otype=2, id=rid, t=t, ts=ts, geom=3, g=polys))
        else:
            biggest = max(polys, key=lambda p: abs(ring_area2(p[0] + p[0][:1])))
            feats[kind].append(dict(otype=2, id=rid, t=t, ts=ts, geom=1, g=centroid(biggest[0] + biggest[0][:1])))
    for k in KINDS:
        if dropped[k]:
            log('  %s: %d dropped (no complete shape in the extract)' % (k, dropped[k]))
    for w in why:
        vlog('    ' + w)
    return feats


# ---------------------------------------------------------------- writing

def spread(v):
    v &= 0xFFFF
    v = (v | (v << 8)) & 0x00FF00FF
    v = (v | (v << 4)) & 0x0F0F0F0F
    v = (v | (v << 2)) & 0x33333333
    v = (v | (v << 1)) & 0x55555555
    return v


def first_xy(f):
    g = f['g']
    if f['geom'] == 1:
        return g
    if f['geom'] == 2:
        return g[0]
    return g[0][0][0]


def morton(f):
    x, y = first_xy(f)
    return spread((x + 180000000) * 65535 // 360000000) | (spread((y + 90000000) * 65535 // 180000000) << 1)


def tags_out(kind, t):
    out = []
    for k in KEEP[kind]:
        v = t.get(k.encode())
        if v is None:
            continue
        s = v.decode('utf-8', 'replace').strip()
        if k == 'description' and len(s) > 300:
            s = s[:297].rstrip() + '…'
        if s:
            out.append((k, s))
    return out


class Out:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v):
        self.b.append(v)

    def var(self, v):
        b = self.b
        while v >= 0x80:
            b.append((v & 0x7f) | 0x80)
            v >>= 7
        b.append(v)

    def zz(self, v):
        self.var((v << 1) if v >= 0 else ((-v) << 1) - 1)


def encode(kind, feats, data_ts):
    feats = sorted(feats, key=morton)
    tags = [tags_out(kind, f['t']) for f in feats]
    freq = {}
    for tl in tags:
        for k, v in tl:
            freq[k] = freq.get(k, 0) + 1
            freq[v] = freq.get(v, 0) + 1
    strings = sorted(freq, key=lambda s: (-freq[s], s))
    index = dict((s, i) for i, s in enumerate(strings))
    o = Out()
    o.b += b'AMFP'
    o.u8(FORMAT)
    name = kind.encode('utf-8')
    o.var(len(name))
    o.b += name
    o.var(max(0, int(data_ts)))
    o.var(len(strings))
    for s in strings:
        e = s.encode('utf-8')
        o.var(len(e))
        o.b += e
    o.var(len(feats))
    cx = cy = 0
    nverts = 0
    for f, tl in zip(feats, tags):
        o.u8(f['geom'] | (f['otype'] << 2))
        o.var(f['id'])
        o.var(max(0, f['ts'] // 86400))
        o.var(len(tl))
        for k, v in tl:
            o.var(index[k])
            o.var(index[v])
        g = f['geom']
        if g == 1:
            x, y = f['g']
            o.zz(x - cx)
            o.zz(y - cy)
            cx, cy = x, y
            nverts += 1
        elif g == 2:
            o.var(len(f['g']))
            for x, y in f['g']:
                o.zz(x - cx)
                o.zz(y - cy)
                cx, cy = x, y
            nverts += len(f['g'])
        else:
            o.var(len(f['g']))
            for poly in f['g']:
                o.var(len(poly))
                for ring in poly:
                    o.var(len(ring))
                    for x, y in ring:
                        o.zz(x - cx)
                        o.zz(y - cy)
                        cx, cy = x, y
                    nverts += len(ring)
    return bytes(o.b), len(feats), nverts


def geojson(kind, feats):
    def deg(p):
        return [p[0] / 1e6, p[1] / 1e6]
    out = []
    for f in sorted(feats, key=morton):
        g = f['g']
        if f['geom'] == 1:
            geom = {'type': 'Point', 'coordinates': deg(g)}
        elif f['geom'] == 2:
            geom = {'type': 'LineString', 'coordinates': [deg(p) for p in g]}
        else:
            geom = {'type': 'MultiPolygon', 'coordinates': [[[deg(p) for p in r + r[:1]] for r in poly] for poly in g]}
        props = dict(tags_out(kind, f['t']))
        props['@osm'] = ['node', 'way', 'relation'][f['otype']] + '/%d' % f['id']
        props['@edited'] = datetime.fromtimestamp(f['ts'], timezone.utc).strftime('%Y-%m-%d') if f['ts'] else ''
        out.append({'type': 'Feature', 'geometry': geom, 'properties': props})
    return {'type': 'FeatureCollection', 'features': out}


def build(pbf, area_id, name, out, geojson_too=False, log=print, vlog=None):
    """Builds the five packs of one area into the folder out; returns the summary (also written as <id>.json)."""
    if not re.match(r'^[a-z0-9_-]+$', area_id):
        raise ValueError('the id may only have a-z, 0-9, _ and -: %r' % area_id)
    os.makedirs(out, exist_ok=True)
    t_start = time.time()
    ex = read_extract(pbf, log)
    feats = make_features(ex, log, vlog or (lambda s: None))
    summary = dict(id=area_id, name=name, format=FORMAT, bbox=ex['bbox'],
                   data=datetime.fromtimestamp(ex['data_ts'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                   if ex['data_ts'] else None,
                   built=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), kinds={})
    for kind in KINDS:
        raw, n, nv = encode(kind, feats[kind], ex['data_ts'])
        gz = gzip.compress(raw, 9, mtime=0)
        fname = '%s-%s.amf' % (area_id, kind)
        with open(os.path.join(out, fname), 'wb') as f:
            f.write(gz)
        summary['kinds'][kind] = dict(file=fname, count=n, points=nv, size=len(gz), raw=len(raw))
        log('%-9s %6d features %8d points  %9d bytes (%d unzipped)' % (kind, n, nv, len(gz), len(raw)))
        if geojson_too:
            with open(os.path.join(out, '%s-%s.geojson' % (area_id, kind)), 'w', encoding='utf-8') as f:
                json.dump(geojson(kind, feats[kind]), f, ensure_ascii=False)
    summary['seconds'] = round(time.time() - t_start, 1)
    with open(os.path.join(out, area_id + '.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    log('total %d bytes · %.1f s' % (sum(k['size'] for k in summary['kinds'].values()), summary['seconds']))
    return summary


def main():
    ap = argparse.ArgumentParser(description='Builds the map-feature packs of one area from an .osm.pbf extract.')
    ap.add_argument('pbf')
    ap.add_argument('--id', required=True, help='short id of the area, e.g. cyprus')
    ap.add_argument('--name', required=True, help='name of the area, e.g. Cyprus')
    ap.add_argument('--out', required=True)
    ap.add_argument('--geojson', action='store_true', help='also write each kind as GeoJSON, for checking')
    ap.add_argument('--verbose', action='store_true', help='also list what was dropped and why')
    a = ap.parse_args()

    def log(s):
        print(s, flush=True)

    try:
        build(a.pbf, a.id, a.name, a.out, a.geojson, log, log if a.verbose else None)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
