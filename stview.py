#!/usr/bin/env python3
"""stview - look at a ScanTailor project's pages, content boxes and output layout.

Run it in a scan folder (or name the folder):

    python3 stview.py [FOLDER] [--port 8765] [--no-browser]

It lists the folder's *.ScanTailor files in a dropdown, serves a viewer on
http://127.0.0.1:PORT/ and re-reads the selected project whenever the file
changes on disk. Standard library only (Python 3.8+); TIFF source images are
shown only when Pillow is installed.

Three views:
  Absolute  the deskewed source page, at one scale for every page, with its
            content box, so content positions can be compared page to page.
  Unaligned the content where it sat on its scan, relative to the max content
            box as placed on that parity's scans (even/odd pages, or left/right
            halves of split spreads), before alignment and margins move it; a
            dotted box shows where ScanTailor puts it.
  Output    the page as ScanTailor's Page Layout stage will place it: the
            aggregate page size, this page's hard and soft margins, and an
            outline of the max content box (aggregate minus the usual margins).

Keys: Left/Right page (two at a time with "Step 2"), Home/End, M switches view,
click pins a guide, Esc clears it.

Shear (ScanTailor 1.2's oblique correction in Deskew) is drawn as ScanTailor applies
it, after the rotation: x' = x - tan(oblique / 2) * y, so a margin's lean moves by half the
oblique angle.
"""
import argparse
import http.server
import io
import json
import math
import mimetypes
import os
import re
import socket
import statistics
import sys
import threading
import time
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict

try:
    from PIL import Image  # optional, for TIFF sources
except ImportError:  # pragma: no cover
    Image = None

DEFAULT_MARGINS = [5.0, 5.0, 10.0, 10.0]  # ScanTailor's defaults: top, bottom, left, right (mm)
VERT = {'top': 'top', 'vcenter': 'center', 'center': 'center', 'bottom': 'bottom',
        'vauto': 'auto', 'voriginal': 'original'}
HOR = {'left': 'left', 'hcenter': 'center', 'center': 'center', 'right': 'right',
       'hauto': 'auto', 'horiginal': 'original'}


# ---------------------------------------------------------------- XML helpers
def num(el, attr, default=0.0):
    v = el.get(attr) if el is not None else None
    try:
        return float(v) if v not in (None, '') else default
    except ValueError:
        return default


def rect(el):
    return None if el is None else [num(el, 'x'), num(el, 'y'), num(el, 'width'), num(el, 'height')]


def points(el):
    if el is None:
        return None
    pts = [[num(p, 'x'), num(p, 'y')] for p in el.findall('point')]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts or None


# Affine transforms as (a, b, c, d, e, f): x' = a*x + c*y + e, y' = b*x + d*y + f
# (the order canvas setTransform() takes, and the same convention as Qt's QTransform).
def rotation(deg):
    if abs(deg / 90 - round(deg / 90)) < 1e-12:
        k = int(round(deg / 90)) % 4
        c, s = [(1, 0), (0, 1), (-1, 0), (0, -1)][k]
    else:
        t = math.radians(deg)
        c, s = math.cos(t), math.sin(t)
    return (c, s, -s, c, 0.0, 0.0)


def shear(deg):
    """ScanTailor's oblique correction (ImageTransformation::calcPostRotateXform): half the angle."""
    return (1.0, 0.0, -math.tan(math.radians(deg) / 2), 1.0, 0.0, 0.0)


def apply(m, p):
    return [m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5]]


def compose(m2, m1):
    """m2 applied after m1."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a2 * a1 + c2 * b1, b2 * a1 + d2 * b1, a2 * c1 + c2 * d1, b2 * c1 + d2 * d1,
            a2 * e1 + c2 * f1 + e2, b2 * e1 + d2 * f1 + f2)


def shifted_to_origin(m, pts):
    """m followed by the translation that puts the bounding box of m(pts) at (0, 0),
    which is how ScanTailor's ImageTransformation normalises each rotation."""
    q = [apply(m, p) for p in pts]
    return (m[0], m[1], m[2], m[3], m[4] - min(p[0] for p in q), m[5] - min(p[1] for p in q))


# ---------------------------------------------------------------- project model
def parse_project(path):
    root = ET.parse(path).getroot()
    base = os.path.dirname(os.path.abspath(path))
    dirs = {d.get('id'): d.get('path') or '.' for d in root.iterfind('directories/directory')}
    files = {e.get('id'): (e.get('dirId'), e.get('name')) for e in root.iterfind('files/file')}
    images = {}
    for im in root.iterfind('images/image'):
        sz, dpi = im.find('size'), im.find('dpi')
        images[im.get('id')] = dict(file=im.get('fileId'), n=int(num(im, 'fileImage')),
                                    w=num(sz, 'width'), h=num(sz, 'height'),
                                    dpix=num(dpi, 'horizontal', 300.0), dpiy=num(dpi, 'vertical', 300.0))

    orient = {}
    for im in root.iterfind('filters/fix-orientation/image'):
        r = im.find('rotation')
        if r is not None:
            orient[im.get('id')] = num(r, 'degrees')

    deskew = {}
    for pg in root.iterfind('filters/deskew/page'):
        pr = pg.find('params')
        dep = pr.find('dependencies') if pr is not None else None
        rot = dep.find('rotation') if dep is not None else None
        deskew[pg.get('id')] = dict(angle=num(pr, 'angle'), oblique=num(pr, 'oblique'),
                                    rotation=num(rot, 'degrees') if rot is not None else None,
                                    outline=points(dep.find('page-outline')) if dep is not None else None)

    content = {}
    for pg in root.iterfind('filters/select-content/page'):
        pr = pg.find('params')
        if pr is None:
            continue
        dep = pr.find('dependencies')
        mm = pr.find('content-size-mm')
        content[pg.get('id')] = dict(
            rect=rect(pr.find('content-rect')), page=rect(pr.find('page-rect')),
            mm=[num(mm, 'width'), num(mm, 'height')] if mm is not None else None,
            routline=points(dep.find('rotated-page-outline')) if dep is not None else None)

    layout = {}
    for pg in root.iterfind('filters/page-layout/page'):
        pr = pg.find('params')
        if pr is None:
            continue
        m, a, cs = pr.find('hardMarginsMM'), pr.find('alignment'), pr.find('contentSizeMM')
        layout[pg.get('id')] = dict(
            margins=[num(m, 'top'), num(m, 'bottom'), num(m, 'left'), num(m, 'right')] if m is not None else None,
            vert=VERT.get(a.get('vert'), a.get('vert')) if a is not None else 'center',
            hor=HOR.get(a.get('hor'), a.get('hor')) if a is not None else 'center',
            null=a is not None and a.get('null') == '1',
            mm=[num(cs, 'width'), num(cs, 'height')] if cs is not None else None,
            auto=pr.get('autoMargins') == '1')

    pages = []
    for i, pg in enumerate(root.iterfind('pages/page')):
        pid, iid = pg.get('id'), pg.get('imageId')
        im = images.get(iid)
        if im is None:
            continue
        dir_id, name = files.get(im['file'], (None, '?'))
        ds, sc, lo = deskew.get(pid, {}), content.get(pid, {}), layout.get(pid)
        warn = []

        deg = ds.get('rotation')
        if deg is None:
            deg = orient.get(iid, 0.0)
        w, h = im['w'], im['h']
        m_orient = shifted_to_origin(rotation(deg), [[0, 0], [w, 0], [w, h], [0, h]])
        ow, oh = (h, w) if int(round(deg / 90)) % 2 else (w, h)
        outline = ds.get('outline') or [[0, 0], [ow, 0], [ow, oh], [0, oh]]
        m_deskew = shifted_to_origin(compose(shear(ds.get('oblique', 0.0)), rotation(ds.get('angle', 0.0))), outline)
        poly = [apply(m_deskew, p) for p in outline]
        ro = sc.get('routline')
        err = max(math.dist(p, q) for p, q in zip(poly, ro)) if ro and len(ro) == len(poly) else None
        if err is not None and err > 0.5:
            warn.append('Deskew and Select Content disagree about the page outline by %.1f px; '
                        'Select Content may need re-running.' % err)
        if abs(im['dpix'] - im['dpiy']) > 0.5:
            warn.append('Unequal horizontal/vertical DPI (%g x %g): ScanTailor rescales such images, '
                        'which this viewer ignores.' % (im['dpix'], im['dpiy']))

        cr = sc.get('rect')
        if cr is not None and (cr[2] <= 0 or cr[3] <= 0):
            cr = None
        mm = sc.get('mm')
        mmpp = (mm[0] / cr[2]) if (cr and mm and mm[0] > 0) else 25.4 / (im['dpix'] or 300.0)
        size_mm = [cr[2] * mmpp, cr[3] * mmpp] if cr else [0.0, 0.0]
        if pid not in content:
            warn.append('No Select Content parameters for this page yet.')

        if lo is None:
            warn.append('No Page Layout parameters for this page yet; ScanTailor defaults assumed.')
            lo = dict(margins=None, vert='center', hor='center', null=False, mm=None, auto=False)
        stored_mm = lo.get('mm')
        stale = bool(stored_mm and cr and (abs(stored_mm[0] - size_mm[0]) > 0.01 or abs(stored_mm[1] - size_mm[1]) > 0.01))
        if stale:
            warn.append('Page Layout still stores the old content size %.1f x %.1f mm (now %.1f x %.1f mm); '
                        'ScanTailor updates it when it reprocesses this page in Page Layout.'
                        % (stored_mm[0], stored_mm[1], size_mm[0], size_mm[1]))
        if lo.get('auto'):
            warn.append('Auto margins are on for this page; the margins shown are the stored ones.')
        if lo['vert'] in ('auto', 'original') or lo['hor'] in ('auto', 'original'):
            warn.append('"%s/%s" alignment is drawn as centred: an approximation.' % (lo['vert'], lo['hor']))

        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        pages.append(dict(
            i=i, id=pid, sub=pg.get('subPage') or 'single', image=name, fileId=im['file'], fileImage=im['n'],
            imgW=w, imgH=h, dpi=im['dpix'], rotation=deg, angle=ds.get('angle', 0.0), oblique=ds.get('oblique', 0.0),
            M=compose(m_deskew, m_orient), poly=poly, bbox=[max(xs), max(ys)],
            pageRect=sc.get('page'), content=cr, sizeMM=size_mm, storedMM=stored_mm, stale=stale, mmpp=mmpp,
            margins=lo['margins'], vert=lo['vert'], hor=lo['hor'], null=lo['null'], warn=warn))

    # ---- Page Layout: aggregate size, soft margins, placement (ScanTailor's calcSoftMarginsMM)
    counts = Counter(tuple(p['margins']) for p in pages if p['margins'])
    default = list(counts.most_common(1)[0][0]) if counts else DEFAULT_MARGINS
    for p in pages:
        p['customMargins'] = p['margins'] is not None and any(abs(a - b) > 1e-6 for a, b in zip(p['margins'], default))
        if p['margins'] is None:
            p['margins'] = list(DEFAULT_MARGINS)

    def hard(p, sizes):
        t, b, l, r = p['margins']
        return sizes[0] + l + r, sizes[1] + t + b

    aligned = [p for p in pages if not p['null']]
    hw = {p['i']: hard(p, p['sizeMM']) for p in pages}
    agg_w = max((hw[p['i']][0] for p in aligned), default=0.0)
    agg_h = max((hw[p['i']][1] for p in aligned), default=0.0)
    stored = {p['i']: hard(p, p['storedMM'] or p['sizeMM']) for p in pages}
    st_w = max((stored[p['i']][0] for p in aligned), default=0.0)
    st_h = max((stored[p['i']][1] for p in aligned), default=0.0)

    for p in pages:
        t, b, l, r = p['margins']
        cw, ch = p['sizeMM']
        hw_, hh_ = hw[p['i']]
        if p['null']:
            pw, ph, dw, dh = hw_, hh_, 0.0, 0.0
        else:
            pw, ph = agg_w, agg_h
            dw, dh = max(0.0, agg_w - hw_), max(0.0, agg_h - hh_)
        hor, vert = p['hor'], p['vert']
        sl = 0.0 if hor == 'left' else dw if hor == 'right' else dw / 2
        st_ = 0.0 if vert == 'top' else dh if vert == 'bottom' else dh / 2
        soft = [st_, dh - st_, sl, dw - sl]
        p['soft'] = soft
        p['pageWH'] = [pw, ph]
        p['hard'] = [hw_, hh_]
        p['place'] = [l + sl, t + st_, cw, ch]

    widest = sorted(aligned, key=lambda p: -hw[p['i']][0])[:5]
    tallest = sorted(aligned, key=lambda p: -hw[p['i']][1])[:5]
    t, b, l, r = default
    mb = [l, t, agg_w - l - r, agg_h - t - b]   # max content box on the output page (mm)

    # ---- Where each page's content sat before alignment. Each parity (even/odd page index,
    # or left/right halves of split spreads) gets the max box fixed on its scans the way
    # ScanTailor aligns that parity's full text pages in it (content >= 95% of the box both
    # ways; median edges or centres); other pages are then placed by their own scan position.
    def group(p):
        return p['sub'] if p['sub'] in ('left', 'right') else ('even' if p['i'] % 2 == 0 else 'odd')

    def edges(p):  # content box in source mm
        x, y, w, h = p['content']
        k = p['mmpp']
        return x * k, y * k, (x + w) * k, (y + h) * k

    def anchor(es, mode, lo, hi, size):
        if mode in ('left', 'top'):
            return statistics.median(e[lo] for e in es)
        if mode in ('right', 'bottom'):
            return statistics.median(e[hi] for e in es) - size
        return statistics.median((e[lo] + e[hi]) / 2 for e in es) - size / 2

    groups = {}
    for g in sorted(set(group(p) for p in pages)):
        mine = [p for p in pages if group(p) == g and p['content']]
        refs = [p for p in mine if p['sizeMM'][0] >= 0.95 * mb[2] and p['sizeMM'][1] >= 0.95 * mb[3]]
        full = len(refs) >= 3
        if not full:
            refs = mine
        if not refs:
            continue
        hor = Counter(p['hor'] for p in refs).most_common(1)[0][0]
        vert = Counter(p['vert'] for p in refs).most_common(1)[0][0]
        es = [edges(p) for p in refs]
        groups[g] = dict(x=anchor(es, hor, 0, 2, mb[2]), y=anchor(es, vert, 1, 3, mb[3]),
                         n=len(refs), full=full, hor=hor, vert=vert)
    x0, y0, x1, y1 = 0.0, 0.0, agg_w, agg_h
    for p in pages:
        p['group'] = group(p)
        a = groups.get(p['group'])
        if p['content'] and a:
            e = edges(p)
            nat = [mb[0] + e[0] - a['x'], mb[1] + e[1] - a['y'], p['sizeMM'][0], p['sizeMM'][1]]
            p['natural'] = nat
            p['moved'] = [p['place'][0] - nat[0], p['place'][1] - nat[1]]
            x0, y0 = min(x0, nat[0]), min(y0, nat[1])
            x1, y1 = max(x1, nat[0] + nat[2]), max(y1, nat[1] + nat[3])
        else:
            p['natural'] = p['moved'] = None
    # frame for the Unaligned view: the page plus whatever sticks out of it, within reason
    nat_bounds = [max(x0, -0.25 * agg_w), max(y0, -0.25 * agg_h), min(x1, 1.25 * agg_w), min(y1, 1.25 * agg_h)]
    return dict(
        name=os.path.basename(path), folder=base, mtime=os.path.getmtime(path),
        version=root.get('version') or '', layoutDirection=root.get('layoutDirection') or '',
        pages=pages, defaultMargins=default, aggregate=[agg_w, agg_h],
        storedAggregate=[st_w, st_h] if (abs(st_w - agg_w) > 0.01 or abs(st_h - agg_h) > 0.01) else None,
        stalePages=[p['i'] for p in pages if p['stale']],
        maxBox=mb, groups=groups, natBounds=nat_bounds,
        widest=[[p['i'], hw[p['i']][0]] for p in widest],
        tallest=[[p['i'], hw[p['i']][1]] for p in tallest],
        absMax=[max((p['bbox'][0] for p in pages), default=1.0), max((p['bbox'][1] for p in pages), default=1.0)],
        _dirs=dirs, _files=files)


# ---------------------------------------------------------------- server
class Viewer:
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        self.lock = threading.Lock()
        self.cache = {}            # name -> (mtime, data)
        self.png = OrderedDict()   # converted TIFFs

    def projects(self):
        out = []
        for n in os.listdir(self.folder):
            p = os.path.join(self.folder, n)
            if n.lower().endswith('.scantailor') and os.path.isfile(p):
                mt = os.path.getmtime(p)
                out.append(dict(name=n, mtime=mt, when=time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))))
        out.sort(key=lambda d: -d['mtime'])
        return dict(folder=self.folder, projects=out)

    def project(self, name):
        if not name or os.path.basename(name) != name or not name.lower().endswith('.scantailor'):
            raise FileNotFoundError(name)
        path = os.path.join(self.folder, name)
        mt = os.path.getmtime(path)
        with self.lock:
            hit = self.cache.get(name)
            if hit and hit[0] == mt:
                return hit[1]
        data = parse_project(path)
        with self.lock:
            self.cache[name] = (mt, data)
        return data

    def image_path(self, data, file_id):
        dir_id, name = data['_files'][file_id]
        d = data['_dirs'].get(dir_id, '.')
        base = data['folder']
        tries = []
        if os.path.isabs(d) or re.match(r'^[A-Za-z]:[\\/]', d) or d.startswith('\\\\'):
            tries.append(os.path.join(d, name))
        else:
            tries.append(os.path.join(base, d, name))
        tries += [os.path.join(base, name), os.path.join(base, os.path.basename(d.rstrip('/\\')), name)]
        for t in tries:
            if os.path.isfile(t):
                return t
        raise FileNotFoundError('image not found: %s (looked in %s)' % (name, ', '.join(os.path.dirname(t) for t in tries)))

    def image(self, name, file_id, n):
        data = self.project(name)
        path = self.image_path(data, file_id)
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp') and n == 0:
            with open(path, 'rb') as fh:
                return fh.read(), mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if Image is None:
            raise ValueError('%s: showing %s images needs Pillow (python -m pip install pillow)' % (os.path.basename(path), ext))
        key = (path, n, os.path.getmtime(path))
        with self.lock:
            if key in self.png:
                self.png.move_to_end(key)
                return self.png[key], 'image/png'
        im = Image.open(path)
        im.seek(n)
        if im.mode not in ('1', 'L', 'RGB', 'RGBA'):
            im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'PNG', compress_level=1)
        with self.lock:
            self.png[key] = buf.getvalue()
            while len(self.png) > 6:
                self.png.popitem(last=False)
        return buf.getvalue(), 'image/png'


def make_handler(viewer):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, cache='no-store'):
            if isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            try:
                if u.path == '/':
                    self.send(200, PAGE, 'text/html; charset=utf-8')
                elif u.path == '/api/projects':
                    self.send(200, json.dumps(viewer.projects()), 'application/json')
                elif u.path == '/api/project':
                    data = viewer.project(q.get('name', ''))
                    pub = {k: v for k, v in data.items() if not k.startswith('_')}
                    self.send(200, json.dumps(pub), 'application/json')
                elif u.path == '/img':
                    body, ctype = viewer.image(q.get('name', ''), q.get('file', ''), int(q.get('n', '0') or 0))
                    self.send(200, body, ctype, cache='max-age=300')
                else:
                    self.send(404, 'not found', 'text/plain')
            except (FileNotFoundError, KeyError) as e:
                self.send(404, str(e), 'text/plain; charset=utf-8')
            except ET.ParseError as e:
                self.send(500, 'could not parse the project (is ScanTailor saving it?): %s' % e, 'text/plain; charset=utf-8')
            except Exception as e:  # report, keep serving
                self.send(500, '%s: %s' % (type(e).__name__, e), 'text/plain; charset=utf-8')
    return Handler


def main():
    ap = argparse.ArgumentParser(description='View a ScanTailor project\'s pages, content boxes and output layout.')
    ap.add_argument('folder', nargs='?', default='.', help='folder holding the *.ScanTailor files (default: here)')
    ap.add_argument('--port', type=int, default=8765, help='first port to try (default 8765)')
    ap.add_argument('--no-browser', action='store_true', help="don't open a browser")
    a = ap.parse_args()
    viewer = Viewer(a.folder)
    if not viewer.projects()['projects']:
        print('No *.ScanTailor files in %s' % viewer.folder, file=sys.stderr)
    server = None
    for port in range(a.port, a.port + 40):
        try:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', port), make_handler(viewer))
            break
        except OSError:
            continue
    if server is None:
        sys.exit('no free port from %d' % a.port)
    url = 'http://127.0.0.1:%d/' % server.server_address[1]
    print('stview: %s  ->  %s   (Ctrl+C to stop)' % (viewer.folder, url), flush=True)
    if not a.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ScanTailor Viewer</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f4f2; --panel: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #8a8984;
  --line: #e1e0db; --canvas: #e6e5e0; --accent: #2a78d6; --content: #e34948; --pagebox: #1baf7a;
  --hilite: #fdf1d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #141413; --panel: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #8f8e88;
    --line: #2f2f2c; --canvas: #262624; --accent: #3987e5; --content: #e66767; --pagebox: #199e70;
    --hilite: #3a3220;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #141413; --panel: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #8f8e88;
  --line: #2f2f2c; --canvas: #262624; --accent: #3987e5; --content: #e66767; --pagebox: #199e70;
  --hilite: #3a3220;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--ink); font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
       display: flex; flex-direction: column; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 8px 16px;
         background: var(--panel); border-bottom: 1px solid var(--line); }
header select { max-width: 32em; font: inherit; padding: 4px 6px; background: var(--bg); color: var(--ink);
                border: 1px solid var(--line); border-radius: 6px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg button { font: inherit; padding: 4px 12px; border: 0; background: transparent; color: var(--ink2); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.nav { display: inline-flex; align-items: center; gap: 6px; }
.nav button { font: inherit; width: 30px; height: 28px; border: 1px solid var(--line); border-radius: 6px;
              background: transparent; color: var(--ink); cursor: pointer; }
.nav input { width: 4.5em; font: inherit; padding: 3px 4px; text-align: right; background: var(--bg); color: var(--ink);
             border: 1px solid var(--line); border-radius: 6px; }
label.chk { display: inline-flex; gap: 6px; align-items: center; color: var(--ink2); cursor: pointer; }
#flash { color: var(--muted); margin-left: auto; }
main { flex: 1; display: flex; min-height: 0; }
#stage { flex: 1; position: relative; min-width: 0; background: var(--canvas); }
#cv { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; }
#hud { position: absolute; left: 12px; bottom: 10px; padding: 4px 8px; border-radius: 6px; font-variant-numeric: tabular-nums;
       background: color-mix(in srgb, var(--panel) 88%, transparent); color: var(--ink2); pointer-events: none; }
#msg { position: absolute; left: 50%; top: 40%; transform: translate(-50%, -50%); max-width: 80%; padding: 10px 14px;
       border-radius: 8px; background: var(--panel); color: var(--ink); border: 1px solid var(--line); display: none; }
aside { width: 340px; flex: none; overflow-y: auto; padding: 12px 16px 24px; background: var(--panel);
        border-left: 1px solid var(--line); font-variant-numeric: tabular-nums; }
aside h2 { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 18px 0 6px; font-weight: 600; }
aside h2:first-child { margin-top: 4px; }
.fname { font-size: 15px; font-weight: 600; word-break: break-all; }
.sub { color: var(--ink2); }
table { border-collapse: collapse; width: 100%; }
td, th { padding: 3px 6px; text-align: right; }
th { color: var(--muted); font-weight: 500; }
td:first-child, th:first-child { text-align: left; padding-left: 0; color: var(--ink2); }
tr + tr td { border-top: 1px solid var(--line); }
td.custom { background: var(--hilite); font-weight: 600; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; }
.kv div.ell { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kv div:nth-child(odd) { color: var(--ink2); }
.warn { margin: 6px 0; padding: 6px 8px; border-left: 3px solid var(--content); background: color-mix(in srgb, var(--content) 10%, transparent); }
.legend span { display: inline-flex; align-items: center; gap: 6px; margin-right: 12px; color: var(--ink2); }
.legend i { width: 18px; height: 0; border-top: 2px solid; display: inline-block; }
a.jump { color: var(--accent); cursor: pointer; text-decoration: none; }
a.jump:hover { text-decoration: underline; }
.keys { color: var(--muted); }
@media (max-width: 800px) {
  main { flex-direction: column; }
  #stage { min-height: 60vh; }
  aside { width: auto; border-left: 0; border-top: 1px solid var(--line); }
}
</style>
</head>
<body>
<header>
  <select id="proj" title="*.ScanTailor files in this folder, newest first"></select>
  <div class="seg" role="group" aria-label="View">
    <button id="mAbs" aria-pressed="false" title="Deskewed source page, same scale for every page (M)">Absolute</button>
    <button id="mNat" aria-pressed="false" title="Content where it sat on the scan, relative to its parity's max content box, before alignment and margins (M)">Unaligned</button>
    <button id="mOut" aria-pressed="true" title="As ScanTailor will lay it out (M)">Output</button>
  </div>
  <div class="nav">
    <button id="prev" title="Previous (Left)">&#9664;</button>
    <input id="pg" type="number" min="1" value="1" aria-label="Page"> <span>/ <span id="pgN">0</span></span>
    <button id="next" title="Next (Right)">&#9654;</button>
  </div>
  <label class="chk"><input type="checkbox" id="step2"> Step 2 pages (keep parity)</label>
  <span id="flash"></span>
</header>
<main>
  <div id="stage"><canvas id="cv"></canvas><div id="hud"></div><div id="msg"></div></div>
  <aside id="info"></aside>
</main>
<script>
'use strict';
const $ = s => document.querySelector(s);
const enc = encodeURIComponent;
const S = { projects: [], folder: '', name: null, data: null, idx: 0, mode: 'out', step2: false,
            guide: { abs: null, out: null }, view: null, mouse: null };
const MODES = ['abs', 'nat', 'out'];
const gkey = () => S.mode === 'abs' ? 'abs' : 'out';   // Unaligned and Output share a frame, so they share the guide
const cache = new Map();          // image key -> {bmp, err}
const H0 = new URLSearchParams(location.hash.slice(1));   // deep link: #name=…&page=…&mode=…
const CACHE_MAX = 10;

function lsGet(k, d) { try { const v = localStorage.getItem('stview:' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem('stview:' + k, JSON.stringify(v)); } catch (e) {} }
function cssv(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function f1(v, d = 1) { if (v == null || isNaN(v)) return '–'; const s = (+v).toFixed(d); return /^-0(\.0+)?$/.test(s) ? s.slice(1) : s; }
function flash(t) { const el = $('#flash'); el.textContent = t; clearTimeout(flash.t); flash.t = setTimeout(() => el.textContent = '', 4000); }
function message(t) { const m = $('#msg'); m.textContent = t || ''; m.style.display = t ? 'block' : 'none'; }

async function getJSON(u) {
  const r = await fetch(u, { cache: 'no-store' });
  if (!r.ok) throw new Error(await r.text() || r.statusText);
  return r.json();
}

// ---------------------------------------------------------------- projects
async function loadProjects(initial) {
  let r;
  try { r = await getJSON('/api/projects'); } catch (e) { flash('viewer server not reachable'); return; }
  const names = p => p.map(x => x.name + '|' + x.when).join('\n');
  const listChanged = names(r.projects) !== names(S.projects);
  S.projects = r.projects; S.folder = r.folder;
  if (listChanged) fillSelect();
  if (initial) {
    const want = H0.get('name') || lsGet('proj:' + S.folder, null);
    const pick = r.projects.find(p => p.name === want) || r.projects[0];
    if (pick) await openProject(pick.name, false);
    else message('No *.ScanTailor files in ' + S.folder);
    return;
  }
  const now = r.projects.find(p => p.name === S.name);
  if (S.data && now && Math.abs(now.mtime - S.data.mtime) > 1e-6) {
    if (await openProject(S.name, true)) flash('Reloaded ' + S.name + ' (changed on disk)');
  }
}

function fillSelect() {
  const sel = $('#proj');
  sel.innerHTML = '';
  for (const p of S.projects) {
    const o = document.createElement('option');
    o.value = p.name; o.textContent = p.name + '   (' + p.when + ')';
    sel.appendChild(o);
  }
  if (S.name) sel.value = S.name;
}

async function openProject(name, keep) {
  const prevId = keep && S.data ? S.data.pages[S.idx].id : lsGet('page:' + S.folder + '/' + name, null);
  let d;
  try { d = await getJSON('/api/project?name=' + enc(name)); }
  catch (e) { flash('Could not read ' + name + ': ' + e.message); return false; }
  S.data = d; S.name = name;
  $('#proj').value = name;
  lsSet('proj:' + S.folder, name);
  let i = d.pages.findIndex(p => p.id === prevId);
  if (!keep && H0.get('page') && H0.get('name') === name) { i = (parseInt(H0.get('page'), 10) || 1) - 1; H0.delete('page'); }
  if (i < 0) i = keep ? Math.min(S.idx, d.pages.length - 1) : 0;
  i = Math.min(i, d.pages.length - 1);
  S.idx = Math.max(0, i);
  $('#pgN').textContent = d.pages.length;
  $('#pg').max = d.pages.length;
  document.title = name.replace(/\.ScanTailor$/i, '') + ' · ScanTailor Viewer';
  message(d.pages.length ? '' : 'This project has no pages.');
  render();
  return true;
}

// ---------------------------------------------------------------- navigation
function go(delta) {
  if (!S.data) return;
  const n = S.data.pages.length;
  const i = S.idx + delta;
  if (i < 0 || i > n - 1) return;
  S.idx = i; render();
}
function goTo(i) {
  if (!S.data) return;
  S.idx = Math.max(0, Math.min(S.data.pages.length - 1, i)); render();
}
function step() { return S.step2 ? 2 : 1; }

// ---------------------------------------------------------------- images
function imgKey(p) { return p.image + '#' + p.fileImage; }
function bitmap(p) {
  const k = imgKey(p);
  let e = cache.get(k);
  if (e) { cache.delete(k); cache.set(k, e); return e; }
  e = { bmp: null, err: null };
  cache.set(k, e);
  while (cache.size > CACHE_MAX) {
    const [k0, e0] = cache.entries().next().value;
    if (e0.bmp && e0.bmp.close) e0.bmp.close();
    cache.delete(k0);
  }
  const url = '/img?name=' + enc(S.name) + '&file=' + enc(p.fileId) + '&n=' + p.fileImage;
  fetch(url)
    .then(r => r.ok ? r.blob() : r.text().then(t => { throw new Error(t || r.statusText); }))
    .then(b => {
      const th = Math.min(p.imgH, 2600), sc = th / p.imgH;
      return createImageBitmap(b, { resizeWidth: Math.max(1, Math.round(p.imgW * sc)), resizeHeight: Math.max(1, Math.round(th)), resizeQuality: 'high' })
        .catch(() => createImageBitmap(b));
    })
    .then(bmp => { e.bmp = bmp; if (current() && imgKey(current()) === k) draw(); })
    .catch(err => { e.err = String(err.message || err); if (current() && imgKey(current()) === k) draw(); });
  return e;
}
function current() { return S.data && S.data.pages[S.idx]; }
function preload() {
  const n = S.data.pages.length;
  for (const d of [step(), -step(), 1, -1, 2 * step()]) {
    const j = S.idx + d;
    if (j >= 0 && j < n) bitmap(S.data.pages[j]);
  }
}

// ---------------------------------------------------------------- drawing
function viewFor(W, H) {
  const pad = 24;
  let x0 = 0, y0 = 0, x1, y1;
  if (S.mode === 'abs') [x1, y1] = S.data.absMax;
  else if (S.mode === 'nat') [x0, y0, x1, y1] = S.data.natBounds;
  else [x1, y1] = S.data.aggregate.map((v, k) => Math.max(v, current().pageWH[k]));
  const pw = x1 - x0, ph = y1 - y0;
  const s = Math.max(1e-6, Math.min((W - 2 * pad) / pw, (H - 2 * pad) / ph));
  return { s, ox: (W - pw * s) / 2 - x0 * s, oy: (H - ph * s) / 2 - y0 * s };
}

function draw() {
  const stage = $('#stage'), cv = $('#cv');
  const dpr = window.devicePixelRatio || 1;
  const W = stage.clientWidth, H = stage.clientHeight;
  const cw = Math.round(W * dpr), ch = Math.round(H * dpr);
  if (cv.width !== cw || cv.height !== ch) { cv.width = cw; cv.height = ch; }
  const ctx = cv.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = cssv('--canvas');
  ctx.fillRect(0, 0, cw, ch);
  const p = current();
  if (!p) return;
  const v = S.view = viewFor(W, H);
  const B = [v.s * dpr, 0, 0, v.s * dpr, v.ox * dpr, v.oy * dpr];
  const px = 1 / v.s;                     // one CSS pixel, in page units
  const e = bitmap(p);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  if (S.mode === 'abs') drawAbs(ctx, p, B, e, px);
  else if (S.mode === 'nat') drawNat(ctx, p, B, e, px);
  else drawOut(ctx, p, B, e, px);
  drawGuide(ctx, dpr, W, H);
  message(e.err ? e.err : '');
  hud();
}

function strokeRect(ctx, r, color, width, dash) {
  ctx.save();
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
  ctx.strokeRect(r[0], r[1], r[2], r[3]);
  ctx.restore();
}
function label(ctx, text, x, y, px, color, align) {
  ctx.save();
  ctx.font = (11 * px) + 'px system-ui, sans-serif';
  ctx.fillStyle = color; ctx.textAlign = align || 'left'; ctx.textBaseline = 'bottom';
  ctx.fillText(text, x, y);
  ctx.restore();
}

function hasPageBox(p) {
  return !!p.pageRect && (Math.abs(p.pageRect[0]) + Math.abs(p.pageRect[1]) > 1 ||
         Math.abs(p.pageRect[2] - p.bbox[0]) > 1 || Math.abs(p.pageRect[3] - p.bbox[1]) > 1);
}
function drawAbs(ctx, p, B, e, px) {
  ctx.setTransform(...B);
  const [mw, mh] = S.data.absMax;
  strokeRect(ctx, [0, 0, mw, mh], cssv('--line'), px);
  ctx.beginPath();
  p.poly.forEach((q, k) => k ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]));
  ctx.closePath();
  ctx.fillStyle = '#fff'; ctx.fill();
  if (e.bmp) {
    ctx.save(); ctx.clip();
    ctx.transform(...p.M);
    ctx.drawImage(e.bmp, 0, 0, p.imgW, p.imgH);
    ctx.restore();
  }
  ctx.setTransform(...B);
  ctx.strokeStyle = cssv('--muted'); ctx.lineWidth = px; ctx.stroke();
  if (hasPageBox(p))
    strokeRect(ctx, p.pageRect, cssv('--pagebox'), 1.5 * px, [6 * px, 4 * px]);
  if (p.content) {
    strokeRect(ctx, p.content, cssv('--content'), 1.5 * px);
    label(ctx, Math.round(p.content[2]) + ' × ' + Math.round(p.content[3]) + ' px', p.content[0], p.content[1] - 3 * px, px, cssv('--content'));
  }
}

function drawOut(ctx, p, B, e, px) {
  ctx.setTransform(...B);
  const [W, H] = p.pageWH;
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);
  const [x, y, w, h] = p.place;
  drawContent(ctx, p, e, p.place);
  strokeRect(ctx, [0, 0, W, H], cssv('--muted'), px);
  if (p.customMargins) {
    const [t, b, l, r] = p.margins;
    strokeRect(ctx, [l, t, W - l - r, H - t - b], cssv('--muted'), px, [2 * px, 3 * px]);
  }
  const mb = S.data.maxBox;
  strokeRect(ctx, mb, cssv('--accent'), 1.5 * px, [6 * px, 4 * px]);
  if (p.content) strokeRect(ctx, [x, y, w, h], cssv('--content'), 1.5 * px);
}

function drawContent(ctx, p, e, r) {   // the page's content, clipped, with its content box at r (mm)
  if (!p.content || !e.bmp) return;
  const [x, y, w, h] = r;
  ctx.save();
  ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
  ctx.transform(p.mmpp, 0, 0, p.mmpp, x - p.content[0] * p.mmpp, y - p.content[1] * p.mmpp);
  ctx.transform(...p.M);
  ctx.drawImage(e.bmp, 0, 0, p.imgW, p.imgH);
  ctx.restore();
}

function drawNat(ctx, p, B, e, px) {
  ctx.setTransform(...B);
  const [W, H] = S.data.aggregate;
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);
  strokeRect(ctx, [0, 0, W, H], cssv('--muted'), px);
  if (p.natural) drawContent(ctx, p, e, p.natural);
  strokeRect(ctx, S.data.maxBox, cssv('--accent'), 1.5 * px, [6 * px, 4 * px]);
  if (p.content) strokeRect(ctx, p.place, cssv('--content'), px, [2 * px, 3 * px]);
  if (p.natural) strokeRect(ctx, p.natural, cssv('--content'), 1.5 * px);
}

function drawGuide(ctx, dpr, W, H) {
  const g = S.guide[gkey()], v = S.view;
  if (!g || !v) return;
  const sx = (v.ox + g.x * v.s) * dpr, sy = (v.oy + g.y * v.s) * dpr;
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.strokeStyle = cssv('--accent'); ctx.lineWidth = Math.max(1, dpr); ctx.globalAlpha = 0.9;
  ctx.beginPath();
  ctx.moveTo(0, Math.round(sy) + 0.5); ctx.lineTo(W * dpr, Math.round(sy) + 0.5);
  ctx.moveTo(Math.round(sx) + 0.5, 0); ctx.lineTo(Math.round(sx) + 0.5, H * dpr);
  ctx.stroke();
  ctx.restore();
}

function toPage(ev) {
  const r = $('#cv').getBoundingClientRect(), v = S.view;
  return { x: (ev.clientX - r.left - v.ox) / v.s, y: (ev.clientY - r.top - v.oy) / v.s };
}
function hud() {
  const m = S.mouse, g = S.guide[gkey()], abs = S.mode === 'abs';
  const u = abs ? ' px' : ' mm', d = abs ? 0 : 1;
  const parts = [{ abs: 'Absolute · deskewed source px', nat: 'Unaligned · mm on the output page', out: 'Output · mm on the output page' }[S.mode]];
  if (m) parts.push('x ' + f1(m.x, d) + ', y ' + f1(m.y, d) + u);
  if (g) parts.push('guide x ' + f1(g.x, d) + ', y ' + f1(g.y, d) + (m ? '  (Δ ' + f1(m.x - g.x, d) + ', ' + f1(m.y - g.y, d) + ')' : '') + ' · Esc clears');
  else parts.push('click to pin a guide');
  $('#hud').textContent = parts.join('   ·   ');
}

// ---------------------------------------------------------------- info panel
function info() {
  const d = S.data, p = current();
  if (!p) { $('#info').innerHTML = ''; return; }
  const [t, b, l, r] = p.margins, [st, sb, sl, sr] = p.soft, dm = d.defaultMargins;
  const [W, H] = p.pageWH, [x, y, w, h] = p.place;
  const cls = k => Math.abs(p.margins[k] - dm[k]) > 1e-6 ? ' class="custom" title="usual: ' + f1(dm[k], 2) + ' mm"' : '';
  const row = (n, k, hard, soft, total) => '<tr><td>' + n + '</td><td' + cls(k) + '>' + f1(hard, 2) + '</td><td>' + f1(soft, 2) + '</td><td>' + f1(total, 2) + '</td></tr>';
  const vname = { top: 'top', center: 'centre', bottom: 'bottom' }[p.vert] || p.vert;
  const hname = { left: 'left', center: 'centre', right: 'right' }[p.hor] || p.hor;
  const jump = (i, txt) => '<a class="jump" data-i="' + i + '">' + esc(txt) + '</a>';
  const nm = i => d.pages[i].image.replace(/\.[^.]+$/, '') + (d.pages[i].sub !== 'single' ? ' ' + d.pages[i].sub : '');
  let s = '';
  s += '<h2>Page</h2><div class="fname">' + esc(p.image) + (p.sub !== 'single' ? ' <span class="sub">(' + esc(p.sub) + ')</span>' : '') + '</div>';
  s += '<div class="sub">' + (S.idx + 1) + ' of ' + d.pages.length + ' · index ' + S.idx + ' (' + (S.idx % 2 ? 'odd' : 'even') + ') · page id ' + esc(p.id) + '</div>';
  s += '<h2>Alignment</h2><div>' + (p.null ? '<b>Not aligned with other pages</b> (page sized to its own content and margins)' :
        'Vertical <b>' + vname + '</b> · horizontal <b>' + hname + '</b>') + '</div>';
  if (p.moved) {
    const g = d.groups[p.group] || {};
    const mv = (v, neg, pos) => Math.abs(v) < 0.05 ? null : f1(Math.abs(v), 1) + ' mm ' + (v < 0 ? neg : pos);
    const parts = [mv(p.moved[1], 'up', 'down'), mv(p.moved[0], 'left', 'right')].filter(Boolean);
    s += '<div style="margin-top:4px">ScanTailor moves the content <b>' + (parts.length ? parts.join(', ') : 'less than 0.1 mm') + '</b> from where it sat on the scan.</div>' +
         '<div class="sub">Measured against the max content box placed on the ' + esc(p.group) + ' pages\' scans as ScanTailor aligns ' +
         (g.full ? 'their ' + g.n + ' full text pages' : 'their ' + g.n + ' pages (too few full text pages)') + ' (' + esc(g.vert) + ' / ' + esc(g.hor) + ').</div>';
  }
  s += '<h2>Margins (mm)</h2><table><tr><th></th><th>Hard</th><th>Soft</th><th>To edge</th></tr>' +
       row('Top', 0, t, st, y) + row('Bottom', 1, b, sb, H - y - h) + row('Left', 2, l, sl, x) + row('Right', 3, r, sr, W - x - w) + '</table>';
  s += '<div class="sub" style="margin-top:4px">Hard margins are set in Page Layout; soft margins are what ScanTailor adds to fill the aggregate page.' +
       (p.customMargins ? ' Highlighted: differs from the usual ' + dm.map(v => f1(v, 1)).join('/') + ' (T/B/L/R).' : '') + '</div>';
  s += '<h2>Content box</h2><div class="kv">' +
       '<div>Size</div><div>' + (p.content ? Math.round(p.content[2]) + ' × ' + Math.round(p.content[3]) + ' px · ' + f1(p.sizeMM[0], 1) + ' × ' + f1(p.sizeMM[1], 1) + ' mm' : 'none (blank page)') + '</div>' +
       (p.content ? '<div>At (source)</div><div>x ' + Math.round(p.content[0]) + ', y ' + Math.round(p.content[1]) + ' px</div>' : '') +
       '<div>Output page</div><div>' + f1(W, 1) + ' × ' + f1(H, 1) + ' mm</div>' +
       '<div>Deskew</div><div>' + f1(p.angle, 3) + '°' + (p.oblique ? ' · oblique ' + f1(p.oblique, 3) + '°' : '') + (p.rotation ? ' · orientation ' + p.rotation + '°' : '') + '</div>' +
       (p.pageRect ? '<div>Page box</div><div>' + p.pageRect.map(v => Math.round(v)).join(', ') + ' px</div>' : '') +
       '<div>Image</div><div>' + Math.round(p.imgW) + ' × ' + Math.round(p.imgH) + ' px @ ' + Math.round(p.dpi) + ' dpi</div></div>';
  if (p.warn.length) s += '<h2>Notes</h2>' + p.warn.map(t => '<div class="warn">' + esc(t) + '</div>').join('');
  s += '<h2>Legend</h2><div class="legend">' + (S.mode === 'nat'
        ? '<span><i style="border-color:var(--content)"></i>content, before alignment</span><span><i style="border-color:var(--content);border-top-style:dotted"></i>where ScanTailor puts it</span>' +
          '<span><i style="border-color:var(--accent);border-top-style:dashed"></i>max content box</span>'
        : S.mode === 'out'
        ? '<span><i style="border-color:var(--content)"></i>content</span><span><i style="border-color:var(--accent);border-top-style:dashed"></i>max content box</span>' +
          (p.customMargins ? '<span><i style="border-color:var(--muted);border-top-style:dotted"></i>this page\'s hard margins</span>' : '')
        : '<span><i style="border-color:var(--content)"></i>content</span><span><i style="border-color:var(--muted)"></i>page outline</span>' +
          (hasPageBox(p) ? '<span><i style="border-color:var(--pagebox);border-top-style:dashed"></i>page box</span>' : '')) + '</div>';
  s += '<h2>Project</h2><div class="kv">' +
       '<div>Aggregate page</div><div>' + f1(d.aggregate[0], 1) + ' × ' + f1(d.aggregate[1], 1) + ' mm</div>' +
       '<div>Max content box</div><div>' + f1(d.maxBox[2], 1) + ' × ' + f1(d.maxBox[3], 1) + ' mm</div>' +
       '<div>Usual margins</div><div>' + d.defaultMargins.map(v => f1(v, 1)).join(' / ') + ' (T/B/L/R)</div>' +
       '<div>Widest</div><div class="ell">' + d.widest.slice(0, 3).map(([i, v]) => f1(v, 1) + ' ' + jump(i, nm(i))).join('<br>') + '</div>' +
       '<div>Tallest</div><div class="ell">' + d.tallest.slice(0, 3).map(([i, v]) => f1(v, 1) + ' ' + jump(i, nm(i))).join('<br>') + '</div></div>' +
       '<div class="sub" style="margin-top:4px">Widest and tallest are content plus hard margins (mm); the largest sets the aggregate page.</div>';
  if (d.storedAggregate) {
    s += '<div class="warn">From the content sizes Page Layout has stored, the aggregate page is ' + f1(d.storedAggregate[0], 1) + ' × ' + f1(d.storedAggregate[1], 1) +
         ' mm. These pages still store old sizes: ' + d.stalePages.slice(0, 12).map(i => jump(i, nm(i))).join(', ') + (d.stalePages.length > 12 ? ' …' : '') + '</div>';
  }
  s += '<h2>Keys</h2><div class="keys">← → page (two at a time with Step 2) · Home / End · M cycles the views · click pins a guide · Esc clears it</div>';
  $('#info').innerHTML = s;
}

function render() {
  if (!S.data) return;
  const p = current();
  $('#pg').value = S.idx + 1;
  $('#mAbs').setAttribute('aria-pressed', S.mode === 'abs');
  $('#mNat').setAttribute('aria-pressed', S.mode === 'nat');
  $('#mOut').setAttribute('aria-pressed', S.mode === 'out');
  if (p) lsSet('page:' + S.folder + '/' + S.name, p.id);
  try { history.replaceState(null, '', '#' + new URLSearchParams({ name: S.name, page: S.idx + 1, mode: S.mode })); } catch (e) {}
  info();
  draw();
  if (p) preload();
}

// ---------------------------------------------------------------- events
$('#proj').addEventListener('change', e => { openProject(e.target.value, false); e.target.blur(); });
function setMode(m) { S.mode = m; lsSet('mode', m); render(); }
$('#mAbs').addEventListener('click', e => { setMode('abs'); e.target.blur(); });
$('#mNat').addEventListener('click', e => { setMode('nat'); e.target.blur(); });
$('#mOut').addEventListener('click', e => { setMode('out'); e.target.blur(); });
$('#prev').addEventListener('click', e => { go(-step()); e.currentTarget.blur(); });
$('#next').addEventListener('click', e => { go(step()); e.currentTarget.blur(); });
$('#step2').addEventListener('change', e => { S.step2 = e.target.checked; lsSet('step2', S.step2); e.target.blur(); });
$('#pg').addEventListener('change', e => { goTo((parseInt(e.target.value, 10) || 1) - 1); });
$('#pg').addEventListener('keydown', e => { if (e.key === 'Enter') { goTo((parseInt(e.target.value, 10) || 1) - 1); e.target.blur(); } });
$('#info').addEventListener('click', e => { const a = e.target.closest('a.jump'); if (a) goTo(+a.dataset.i); });
document.addEventListener('keydown', e => {
  const t = e.target;
  if (t && (t.tagName === 'SELECT' || (t.tagName === 'INPUT' && t.type !== 'checkbox'))) return;
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown') { go(step()); e.preventDefault(); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { go(-step()); e.preventDefault(); }
  else if (e.key === 'Home') { goTo(S.step2 ? S.idx % 2 : 0); e.preventDefault(); }
  else if (e.key === 'End' && S.data) {
    const n = S.data.pages.length;
    goTo(S.step2 ? n - 1 - ((n - 1 - S.idx) % 2) : n - 1); e.preventDefault();
  }
  else if (e.key === 'm' || e.key === 'M') setMode(MODES[(MODES.indexOf(S.mode) + (e.shiftKey ? 2 : 1)) % 3]);
  else if (e.key === 'Escape') { S.guide[gkey()] = null; draw(); }
});
const cv = $('#cv');
cv.addEventListener('mousemove', e => { if (S.view) { S.mouse = toPage(e); hud(); } });
cv.addEventListener('mouseleave', () => { S.mouse = null; hud(); });
cv.addEventListener('click', e => { if (S.view) { S.guide[gkey()] = toPage(e); draw(); } });
new ResizeObserver(() => draw()).observe($('#stage'));
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => draw());

S.mode = H0.get('mode') || lsGet('mode', 'out');
if (!MODES.includes(S.mode)) S.mode = 'out';
S.step2 = !!lsGet('step2', false);
$('#step2').checked = S.step2;
loadProjects(true);
setInterval(() => loadProjects(false), 2000);
</script>
</body>
</html>
'''

if __name__ == '__main__':
    main()
