#!/usr/bin/env python3
"""The weekly run of the map-feature packs (GitHub Actions: .github/workflows/weekly.yml).

  plan     which areas: every country of Geofabrik's index; a country whose extract is over 2 GB is built per
           region instead (Germany, France, the US states…). Writes plan.json.
  build    one group (a continent): downloads its extracts from Geofabrik, checks them, builds their packs.
  index    index.json (and index.json.gz), the list the app reads: each area with its outline, the date of its
           data, its release and the size and count of each of its five packs. A pack's address is
           base + release + '/' + id + '-' + kind + '.amf'.
  cleanup  the weekly releases no longer needed (prints their tags).

Standard library only.

  python3 -I build_all.py plan --out plan.json [--only cyprus,malta] [--geofabrik-index FILE] [--github-output FILE]
  python3 -I build_all.py build --plan plan.json --group europe --out out [--jobs 4] [--downloads DIR] [--keep]
  python3 -I build_all.py index --plan plan.json --summaries DIR --repo OWNER/NAME --week 20261123
                                [--previous previous.json] --out index.json [--status STATUS.md]
  python3 -I build_all.py cleanup --index index.json --releases releases.json [--keep 2]
"""
import argparse, gzip, hashlib, json, os, re, socket, subprocess, sys, time, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_pack  # noqa: E402

GEOFABRIK_INDEX = 'https://download.geofabrik.de/index-v1.json'
SPLIT_OVER = 2000 * 1000 * 1000   # bytes: a bigger country extract is built per region
# Geofabrik areas without an iso3166-1 code that no country extract covers
EXTRA = ['kosovo', 'isle-of-man', 'guernsey-jersey', 'azores', 'canary-islands', 'comores', 'american-oceania']
OUTLINE_TOLERANCE = 0.05          # degrees, for the simplified outline of each area in the index
MAX_ASSETS = 1000                 # GitHub's limit of files in one release


def log(s):
    print(s, flush=True)


# IPv4 first: some of GitHub's machines have IPv6 addresses but no IPv6 route («Network is unreachable», 2026-11-18)
_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kw):
    return sorted(_getaddrinfo(*args, **kw), key=lambda a: a[0] != socket.AF_INET)


socket.getaddrinfo = _ipv4_first


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def user_agent():
    repo = os.environ.get('GITHUB_REPOSITORY')
    return 'map-features-packs/1' + (' (+https://github.com/%s)' % repo if repo else '')


def http(url, method='GET', timeout=120):
    return urllib.request.urlopen(urllib.request.Request(url, method=method, headers={'User-Agent': user_agent()}),
                                  timeout=timeout)


def head_size(url):
    for attempt in range(3):
        try:
            with http(url, 'HEAD', 60) as r:
                return int(r.headers.get('Content-Length') or 0)
        except Exception as e:
            log('  size of %s not known yet (%s)' % (url, e))
            time.sleep(5 * (attempt + 1))
    return -1


# ---------------------------------------------------------------- plan

def simplify(pts, tol):
    """Douglas-Peucker."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if l2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if best > t2:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def rings_of(geom):
    polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
    return [[(x, y) for x, y in ring[:]] for poly in polys for ring in poly]


def outline(geom):
    """The area's rings, simplified; the app tests a point against them even-odd, so holes work too."""
    rings = rings_of(geom)
    for tol, dec in ((OUTLINE_TOLERANCE, 2), (OUTLINE_TOLERANCE / 10, 3), (0, 4)):
        out = []
        for r in rings:
            s = simplify(r, tol) if tol else r
            if len(s) >= 4:
                out.append([[round(x, dec), round(y, dec)] for x, y in s])
        if out:
            return out
    return []


def bbox(geom):
    pts = [p for r in rings_of(geom) for p in r]
    return [round(min(p[0] for p in pts), 4), round(min(p[1] for p in pts), 4),
            round(max(p[0] for p in pts), 4), round(max(p[1] for p in pts), 4)]


def plan(a):
    if a.geofabrik_index:
        idx = json.load(open(a.geofabrik_index, encoding='utf-8'))
    else:
        for attempt in range(5):
            try:
                with http(GEOFABRIK_INDEX) as r:
                    idx = json.load(r)
                break
            except Exception as e:
                if attempt == 4:
                    raise
                log('Geofabrik\'s index not got (%s); again in %d s' % (e, 15 * (attempt + 1)))
                time.sleep(15 * (attempt + 1))
    props = dict((f['properties']['id'], f['properties']) for f in idx['features'])
    geoms = dict((f['properties']['id'], f['geometry']) for f in idx['features'])
    children = defaultdict(list)
    for i, p in props.items():
        if p.get('parent'):
            children[p['parent']].append(i)
        if '/' in i:                       # the US states are us/alabama… under north-america
            children[i.split('/')[0]].append(i)
    top = set(i for i, p in props.items() if not p.get('parent'))

    def group_of(i):
        while props[i].get('parent'):
            i = props[i]['parent']
        return i

    if a.only:
        roots = [i.strip() for i in a.only.split(',') if i.strip()]
        missing = [i for i in roots if i not in props]
        if missing:
            sys.exit('not in Geofabrik\'s index: ' + ', '.join(missing))
    else:
        roots = [i for i, p in props.items() if p.get('iso3166-1:alpha2')] + [i for i in EXTRA if i in props]
    chosen, seen = [], set()

    def add(i):
        if i in seen:
            return
        seen.add(i)
        kids = sorted(set(children.get(i, [])))
        if kids:
            size = head_size(props[i]['urls']['pbf'])
            if size < 0 or size > SPLIT_OVER:
                log('%s: %s, built per region (%d)' % (i, '%.1f GB' % (size / 1e9) if size > 0 else 'size not known',
                                                      len(kids)))
                for c in kids:
                    add(c)
                return
        chosen.append(i)

    for i in roots:
        add(i)
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = dict(zip(chosen, ex.map(lambda i: head_size(props[i]['urls']['pbf']), chosen)))
    areas = []
    for i in chosen:
        p = props[i]
        areas.append(dict(id=i.replace('/', '-'), geofabrik=i, name=p['name'], group=group_of(i),
                          iso=p.get('iso3166-1:alpha2') or [], url=p['urls']['pbf'], size=sizes[i],
                          bbox=bbox(geoms[i]), outline=outline(geoms[i])))
    ids = [x['id'] for x in areas]
    dup = set(x for x in ids if ids.count(x) > 1)
    if dup:
        sys.exit('two areas with the same id: ' + ', '.join(sorted(dup)))
    week = datetime.now(timezone.utc).strftime('%Y%m%d')
    groups = sorted(set(x['group'] for x in areas))
    per = dict((g, sum(1 for x in areas if x['group'] == g)) for g in groups)
    for g in groups:
        files = per[g] * len(build_pack.KINDS)
        if files > MAX_ASSETS:
            sys.exit('%s would have %d files, more than GitHub allows in one release (%d)' % (g, files, MAX_ASSETS))
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(dict(week=week, made=now(), areas=areas), f, ensure_ascii=False, indent=0)
    gb = dict((g, sum(max(0, x['size']) for x in areas if x['group'] == g) / 1e9) for g in groups)
    log('%d areas in %d groups: %s' % (len(areas), len(groups), ', '.join('%s %d (%.1f GB)' % (g, per[g], gb[g])
                                                                          for g in groups)))
    if a.github_output:
        with open(a.github_output, 'a', encoding='utf-8') as f:
            f.write('groups=%s\nweek=%s\n' % (json.dumps(groups), week))


# ---------------------------------------------------------------- build

def file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def download(url, path, say, reuse=False):
    """Downloads an extract and checks it against Geofabrik's checksum of that very file."""
    for attempt in range(4):
        try:
            with http(url, timeout=300) as r:
                final = r.geturl()         # …/cyprus-261117.osm.pbf, the dated file behind -latest
                with http(final + '.md5', timeout=60) as m:
                    expected = m.read().decode('ascii', 'replace').split()[0]
                if reuse and os.path.exists(path) and file_md5(path) == expected:
                    say('the extract already here is the latest (%s)' % final.rsplit('/', 1)[-1])
                    return
                h, n, t0 = hashlib.md5(), 0, time.time()
                with open(path + '.part', 'wb') as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        h.update(b)
                        n += len(b)
            if h.hexdigest() != expected:
                raise IOError('checksum does not match')
            os.replace(path + '.part', path)
            say('downloaded %s, %.1f MB in %.0f s' % (final.rsplit('/', 1)[-1], n / 1e6, time.time() - t0))
            return
        except Exception as e:
            say('download failed (%s); try %d of 4' % (e, attempt + 1))
            if os.path.exists(path + '.part'):
                os.remove(path + '.part')
            time.sleep(30 * (attempt + 1))
    raise IOError('could not download ' + url)


def peak_mb():
    import resource
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(v / (1 << 20) if sys.platform == 'darwin' else v / 1024)   # bytes on macOS, KB on Linux


def do_area(area, out, downloads, keep):
    def say(s):
        log('[%s] %s' % (area['id'], s))
    os.makedirs(downloads, exist_ok=True)
    path = os.path.join(downloads, area['id'] + '.osm.pbf')
    download(area['url'], path, say, reuse=keep)
    try:
        s = build_pack.build(path, area['id'], area['name'], out, log=say)
        s['extract'] = os.path.getsize(path)
        s['peak_mb'] = peak_mb()
        say('peak memory %d MB' % s['peak_mb'])
        with open(os.path.join(out, area['id'] + '.json'), 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
        return s
    finally:
        if not keep and os.path.exists(path):
            os.remove(path)


def area(a):
    """One area, run by build in a process of its own: should it run out of memory, only it fails."""
    p = json.load(open(a.plan, encoding='utf-8'))
    do_area(next(x for x in p['areas'] if x['id'] == a.id), a.out, a.downloads, a.keep)


def build(a):
    p = json.load(open(a.plan, encoding='utf-8'))
    areas = [x for x in p['areas'] if x['group'] == a.group]
    if not areas:
        sys.exit('no areas in group ' + a.group)
    areas.sort(key=lambda x: -x.get('size', 0))   # the biggest first, so the workers end together
    os.makedirs(a.out, exist_ok=True)
    downloads = a.downloads or os.path.join(os.path.dirname(os.path.abspath(a.out)), 'downloads')
    t0 = time.time()
    done, failed = [], []

    def run(x):
        summary = os.path.join(a.out, x['id'] + '.json')
        if os.path.exists(summary):
            os.remove(summary)
        cmd = [sys.executable, '-I', os.path.abspath(__file__), 'area', '--plan', os.path.abspath(a.plan), '--id', x['id'],
               '--out', os.path.abspath(a.out), '--downloads', os.path.abspath(downloads)] + (['--keep'] if a.keep else [])
        try:
            r = subprocess.run(cmd, timeout=a.area_minutes * 60)
        except subprocess.TimeoutExpired:
            return x, None, 'took over %d minutes' % a.area_minutes
        if r.returncode == 0 and os.path.exists(summary):
            return x, json.load(open(summary, encoding='utf-8')), None
        return x, None, 'out of memory?' if r.returncode in (-9, 137) else 'exit code %d' % r.returncode

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for f in as_completed([ex.submit(run, x) for x in areas]):
            x, s, why = f.result()
            if s:
                done.append((x, s))
            else:
                failed.append((x, why))
                log('[%s] FAILED: %s' % (x['id'], why))
    done.sort(key=lambda d: d[0]['name'].lower())
    total = sum(k['size'] for _, s in done for k in s['kinds'].values())
    with open(os.path.join(a.out, 'NOTES.md'), 'w', encoding='utf-8') as f:
        f.write('Map-feature packs of %s, week of %s: %d areas, %.1f MB. Map data © OpenStreetMap contributors, '
                'ODbL 1.0; extracts by Geofabrik.\n' % (a.group, p['week'], len(done), total / 1e6))
        if failed:
            f.write('\nNot built this week (the previous packs stay in the index): %s\n'
                    % ', '.join(x['name'] for x, _ in failed))
    log('%s: %d built, %d failed, %.1f MB of packs, %.0f min' % (a.group, len(done), len(failed), total / 1e6,
                                                                 (time.time() - t0) / 60))
    if not done:
        sys.exit(1)


# ---------------------------------------------------------------- index, status, cleanup

def index(a):
    p = json.load(open(a.plan, encoding='utf-8'))
    prev = {}
    if a.previous and os.path.exists(a.previous) and os.path.getsize(a.previous) > 2:
        prev = json.load(open(a.previous, encoding='utf-8'))
    prev_areas = dict((x['id'], x) for x in prev.get('areas', []))
    areas, fresh, kept, seen = [], 0, 0, set()
    for x in p['areas']:
        seen.add(x['id'])
        sp = os.path.join(a.summaries, x['id'] + '.json')
        if os.path.exists(sp):
            s = json.load(open(sp, encoding='utf-8'))
            areas.append(dict(id=x['id'], name=x['name'], group=x['group'], iso=x['iso'], bbox=x['bbox'],
                              outline=x['outline'], data=s['data'],
                              release=s.get('release') or '%s-%s' % (x.get('chunk') or x['group'], a.week),
                              sizes=[s['kinds'][k]['size'] for k in build_pack.KINDS],
                              offsets=[s['kinds'][k].get('offset', 0) for k in build_pack.KINDS],
                              file=s.get('bundle'),
                              counts=[s['kinds'][k]['count'] for k in build_pack.KINDS]))
            if x.get('local'):
                areas[-1]['local'] = x['local']
            fresh += 1
        elif x['id'] in prev_areas:
            areas.append(prev_areas[x['id']])
            kept += 1
    for i, x in prev_areas.items():       # areas of earlier weeks that this run did not cover (a test run)
        if i not in seen:
            areas.append(x)
            kept += 1
    areas.sort(key=lambda x: x['name'].lower())
    areas = [x for x in areas if len(x.get('sizes', [])) == len(build_pack.KINDS)]   # (not those of an older format)
    overture = p.get('overture')
    out = dict(format=build_pack.FORMAT, built=now(), kinds=build_pack.KINDS,
               base='https://github.com/%s/releases/download/' % a.repo,
               attribution=p.get('attribution') or '© OpenStreetMap contributors',
               license='ODbL 1.0, https://opendatacommons.org/licenses/odbl/',
               source=('Overture Maps %s, https://overturemaps.org' % overture) if overture else
               'OpenStreetMap extracts by Geofabrik, https://download.geofabrik.de', areas=areas)
    if overture:
        out['overture'] = overture
    raw = json.dumps(out, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    with open(a.out, 'wb') as f:
        f.write(raw)
    with open(a.out + '.gz', 'wb') as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    log('index: %d areas (%d new this week, %d from earlier weeks), %d bytes, %d gzipped' % (
        len(areas), fresh, kept, len(raw), os.path.getsize(a.out + '.gz')))
    if a.status:
        with open(a.status, 'w', encoding='utf-8') as f:
            f.write(status_md(out))


def status_md(ix):
    def size(n):
        return '%.1f MB' % (n / 1e6) if n >= 1e6 else '%d KB' % max(1, round(n / 1000))
    total = sum(sum(x['sizes']) for x in ix['areas'])
    lines = ['# Map-feature packs', '',
             'Index of %s · %d areas · %s in all · map data %s, %s.' % (
                 ix['built'][:10], len(ix['areas']), size(total), ix['attribution'], ix['license'].split(',')[0]), '',
             'The size of each pack, and its number of features in brackets.', '',
             '| Area | Data of | ' + ' | '.join(k.capitalize() for k in ix['kinds']) + ' |',
             '|---|---|' + '---:|' * len(ix['kinds'])]
    for x in ix['areas']:
        cells = ['%s (%d)' % (size(n), c) for n, c in zip(x['sizes'], x['counts'])]
        lines.append('| %s | %s | %s |' % (x['name'], (x.get('data') or '')[:10], ' | '.join(cells)))
    return '\n'.join(lines) + '\n'


def cleanup(a):
    ix = json.load(open(a.index, encoding='utf-8'))
    used = set(x['release'] for x in ix['areas'])
    by_group = defaultdict(list)
    for r in json.load(open(a.releases, encoding='utf-8')):
        m = re.match(r'^(.+)-(\d{8})$', r['tagName'])
        if m:
            by_group[m.group(1)].append(r['tagName'])
    for g, tags in sorted(by_group.items()):
        tags.sort(reverse=True)
        for t in tags[0 if g.startswith('test') else a.keep:]:   # (a test run's releases: gone once nothing uses them)
            if t not in used:
                print(t)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('plan')
    s.add_argument('--out', required=True)
    s.add_argument('--only', default='', help='Geofabrik ids, comma-separated, for a test run')
    s.add_argument('--geofabrik-index', help='a copy of index-v1.json instead of downloading it')
    s.add_argument('--github-output')
    s = sub.add_parser('build')
    s.add_argument('--plan', required=True)
    s.add_argument('--group', required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--jobs', type=int, default=4)
    s.add_argument('--downloads', help='where the extracts go while they are built (default: beside --out)')
    s.add_argument('--keep', action='store_true', help='keep the extracts, and use one already there if it is the latest')
    s.add_argument('--area-minutes', type=int, default=120, help='an area that takes longer fails')
    s = sub.add_parser('area', help='(used by build) one area in a process of its own')
    s.add_argument('--plan', required=True)
    s.add_argument('--id', required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--downloads', required=True)
    s.add_argument('--keep', action='store_true')
    s = sub.add_parser('index')
    s.add_argument('--plan', required=True)
    s.add_argument('--summaries', required=True)
    s.add_argument('--repo', required=True)
    s.add_argument('--week', required=True)
    s.add_argument('--previous')
    s.add_argument('--out', required=True)
    s.add_argument('--status')
    s = sub.add_parser('cleanup')
    s.add_argument('--index', required=True)
    s.add_argument('--releases', required=True, help='gh release list --json tagName --limit 1000')
    s.add_argument('--keep', type=int, default=2)
    a = ap.parse_args()
    dict(plan=plan, build=build, area=area, index=index, cleanup=cleanup)[a.cmd](a)


if __name__ == '__main__':
    main()
