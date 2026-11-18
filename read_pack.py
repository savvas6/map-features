#!/usr/bin/env python3
"""Reads map-feature packs (.amf) back, for checking: a summary, GeoJSON, a feature-by-feature comparison
with the builder's GeoJSON, and an HTML page that draws them over OpenStreetMap.

  python3 -I read_pack.py PACK.amf [PACK.amf …] [--compare-dir DIR] [--html OUT.html]

The pack format is described at the top of build_pack.py.
"""
import argparse, gzip, json, os, sys
from collections import Counter
from datetime import datetime, timezone

KINDS = ['military', 'helipads', 'water', 'limits', 'power']
OTYPES = ['node', 'way', 'relation']


class Reader:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u8(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def var(self):
        v = s = 0
        while True:
            c = self.b[self.p]
            self.p += 1
            v |= (c & 0x7f) << s
            if c < 0x80:
                return v
            s += 7

    def zz(self):
        v = self.var()
        return (v >> 1) ^ -(v & 1)


def read(path):
    b = gzip.decompress(open(path, 'rb').read())
    if b[:4] != b'AMFP':
        raise ValueError('%s is not a map-features pack' % path)
    r = Reader(b, 4)
    fmt = r.u8()
    if fmt != 1:
        raise ValueError('format %d not known' % fmt)
    kind = KINDS[r.u8() - 1]
    data_ts = r.var()
    strings = []
    for _ in range(r.var()):
        n = r.var()
        strings.append(b[r.p:r.p + n].decode('utf-8'))
        r.p += n
    cur = [0, 0]

    def pt():
        cur[0] += r.zz()
        cur[1] += r.zz()
        return (cur[0], cur[1])

    feats = []
    for _ in range(r.var()):
        flags = r.u8()
        geom, otype = flags & 3, flags >> 2
        oid = r.var()
        day = r.var()
        tags = []
        for _ in range(r.var()):
            k = strings[r.var()]
            tags.append((k, strings[r.var()]))
        if geom == 1:
            g = pt()
        elif geom == 2:
            g = [pt() for _ in range(r.var())]
        elif geom == 3:
            g = []
            for _ in range(r.var()):
                poly = []
                for _ in range(r.var()):
                    poly.append([pt() for _ in range(r.var())])
                g.append(poly)
        else:
            raise ValueError('geometry %d not known' % geom)
        feats.append(dict(geom=geom, otype=otype, id=oid, day=day, tags=tags, g=g))
    if r.p != len(b):
        raise ValueError('%d bytes left over at the end' % (len(b) - r.p))
    return dict(kind=kind, data_ts=data_ts, strings=len(strings), feats=feats)


def to_geojson(pack):
    def deg(p):
        return [p[0] / 1e6, p[1] / 1e6]
    out = []
    for f in pack['feats']:
        g = f['g']
        if f['geom'] == 1:
            geom = {'type': 'Point', 'coordinates': deg(g)}
        elif f['geom'] == 2:
            geom = {'type': 'LineString', 'coordinates': [deg(p) for p in g]}
        else:
            geom = {'type': 'MultiPolygon', 'coordinates': [[[deg(p) for p in r + r[:1]] for r in poly] for poly in g]}
        props = dict(f['tags'])
        props['@osm'] = OTYPES[f['otype']] + '/%d' % f['id']
        props['@edited'] = datetime.fromtimestamp(f['day'] * 86400, timezone.utc).strftime('%Y-%m-%d') if f['day'] else ''
        out.append({'type': 'Feature', 'geometry': geom, 'properties': props})
    return {'type': 'FeatureCollection', 'features': out}


def ring_area2(r):
    return sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1] for i in range(len(r)))


def check(pack):
    """Things that must hold in every pack: rings of 3+ points, outer counter-clockwise, holes clockwise, sane coordinates."""
    bad = []
    for f in pack['feats']:
        name = OTYPES[f['otype']] + '/%d' % f['id']
        pts = [f['g']] if f['geom'] == 1 else f['g'] if f['geom'] == 2 else [p for poly in f['g'] for r in poly for p in r]
        if any(not (-180e6 <= x <= 180e6 and -90e6 <= y <= 90e6) for x, y in pts):
            bad.append(name + ' coordinates out of range')
        if f['geom'] == 2 and len(f['g']) < 2:
            bad.append(name + ' line of %d points' % len(f['g']))
        if f['geom'] == 3:
            for poly in f['g']:
                for k, r in enumerate(poly):
                    if len(r) < 3:
                        bad.append(name + ' ring of %d points' % len(r))
                    elif (ring_area2(r) > 0) != (k == 0):
                        bad.append(name + (' outer ring turns clockwise' if k == 0 else ' hole turns counter-clockwise'))
    return bad


HTML = '''<!doctype html><html><head><meta charset="utf-8"><title>Map features check</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{height:100%;margin:0}body{font:13px -apple-system,sans-serif}
.lg{background:#fff;padding:6px 8px;border-radius:6px;line-height:1.5;box-shadow:0 1px 4px #0004}
.lg i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-1px;border-radius:2px}
.leaflet-popup-content{font-size:12px;line-height:1.4}</style></head><body><div id="m"></div><script>
const D = __DATA__;
const C = {military:'#e0353b', helipads:'#2f6fe4', water:'#0a84ff', limits:'#ff7a00', power:'#9c7a00'};
const m = L.map('m', {preferCanvas: true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(m);
const all = [], ctl = {};
for (const [kind, fc] of Object.entries(D)) {
  const lay = L.geoJSON(fc, {
    style: () => kind === 'military' ? {color: C[kind], weight: 1.5, fillOpacity: .18}
                : {color: C[kind], weight: kind === 'limits' ? 5 : 2, opacity: .9},
    pointToLayer: (f, ll) => L.circleMarker(ll, {radius: kind === 'helipads' ? 6 : 4, color: '#fff', weight: 1,
                                                  fillColor: C[kind], fillOpacity: 1}),
    onEachFeature: (f, l) => l.bindPopup('<b>' + kind + '</b><br>' + Object.entries(f.properties)
        .map(([k, v]) => k + ' = ' + String(v).replace(/</g, '&lt;')).join('<br>'))
  }).addTo(m);
  all.push(lay);
  ctl['<i style="background:' + C[kind] + '"></i>' + kind + ' (' + fc.features.length + ')'] = lay;
}
L.control.layers(null, ctl, {collapsed: false}).addTo(m);
m.fitBounds(L.featureGroup(all).getBounds());
</script></body></html>'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('packs', nargs='+')
    ap.add_argument('--compare-dir', help="folder with the builder's <id>-<kind>.geojson (build_pack.py --geojson)")
    ap.add_argument('--html', help='write a page that draws the packs over OpenStreetMap')
    a = ap.parse_args()
    data, failed = {}, 0
    for path in a.packs:
        pack = read(path)
        fc = to_geojson(pack)
        kinds = Counter((OTYPES[f['otype']], ['', 'point', 'line', 'polygons'][f['geom']]) for f in pack['feats'])
        nv = sum(1 if f['geom'] == 1 else len(f['g']) if f['geom'] == 2 else sum(len(r) for p in f['g'] for r in p)
                 for f in pack['feats'])
        print('%-22s %-9s %5d features %7d points %5d strings · data %s · %s' % (
            os.path.basename(path), pack['kind'], len(pack['feats']), nv, pack['strings'],
            datetime.fromtimestamp(pack['data_ts'], timezone.utc).strftime('%Y-%m-%d %H:%M'),
            ', '.join('%s %s %d' % (o, g, n) for (o, g), n in sorted(kinds.items()))))
        bad = check(pack)
        for b in bad[:10]:
            print('   BAD ' + b)
        failed += len(bad)
        if a.compare_dir:
            ref_path = os.path.join(a.compare_dir, os.path.basename(path)[:-4] + '.geojson')
            ref = json.load(open(ref_path, encoding='utf-8'))
            same = len(ref['features']) == len(fc['features'])
            diffs = 0
            for x, y in zip(ref['features'], fc['features']):
                if json.dumps(x, sort_keys=True) != json.dumps(y, sort_keys=True):
                    diffs += 1
                    if diffs <= 3:
                        print('   DIFF %s' % x['properties'].get('@osm'))
            print('   compared with %s: %s' % (os.path.basename(ref_path),
                                                'identical' if same and not diffs else 'DIFFERENT (%d)' % diffs))
            failed += 0 if same and not diffs else 1
        data[pack['kind']] = fc
    if a.html:
        with open(a.html, 'w', encoding='utf-8') as f:
            f.write(HTML.replace('__DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':'))))
        print('page: ' + a.html)
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
