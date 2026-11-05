#!/usr/bin/env python3
"""How heavily each page's type came out, and a threshold for each page to even it.

usage: st_weight.py report PROJECT [--jobs N]
       st_weight.py fit PROJECT WORK_DIR [--adj LO,HI,STEP] [--min-lines N] [--tsv OUT.tsv] [--apply] [--jobs N]

ScanTailor's thresholdAdj is added to Otsu's gray level, and Otsu cuts at much the same darkness on
every page, so one setting for a book gives even thresholds, not even type: what differs is the
strokes on the paper (Jonas's *The Gnostic Religion*: a stem of 6.8 px on p. 130 and 8.7 on p. 315,
0.97 correlated with the raw scan's stem at half darkness, the two sides of a printing sheet 6%
apart). No one setting helps, the spread staying the same from -30 to +30. A setting for each page
does, and this finds it by measurement.

A page's weight can't be read off the whole page, since small type has thinner strokes and a page of
notes would read light. So each line of type is measured alone: its x-band is where the row profile
stands above BAND of its peak, its stem the interpolated median of the horizontal black runs across
the middle of that band. Run lengths are whole pixels, and a plain median jumps as it ticks over.
The book's type sizes are the peaks of the lines' x-heights, of MIN_SHARE of the lines or more, each
reaching to the valley beside it and SIZE_REACH rows at most; a line of another size (a heading)
doesn't count. A page's weight is the median of its lines, each over the median of its size.

report reads the project's output folder and says how even it is: the stem, which fit tunes, and
2 * ink / perimeter, which it doesn't. Read the second to judge a fit; the first flatters it (Jonas:
sd 4.8% to 0.2% on the stem, 5.2% to 1.1% on the other, and 4.9% to 1.0% with lines held out). It gives
the share of the variance that a 4-page cycle explains, a 2-page one barely any if the printing did it.

fit writes the book at each setting of --adj (default -30,40,5) and at the project's own median,
through copies of the project in WORK_DIR, the project itself only read. A setting WORK_DIR already
has whole is used as it stands, so clear WORK_DIR after changing the project. Lines are found at the
median setting and measured at the same rows at every setting (a line's x-band moves with the ink,
and would change its size). Each page's setting is where its weight meets the median setting's book
median, so the book's overall weight stays. --apply writes those settings into the project's page
settings, leaving ScanTailor's record of what it wrote, so that scantailor-batch --through output
rewrites the pages changed. Pages with under --min-lines lines (6) are left as they are, and listed.

A project of whole pages: output is taken to be the image's name with .tif.
"""
import argparse, os, re, subprocess, sys
from concurrent.futures import ProcessPoolExecutor
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from scantailor import BATCH, Project

Image.MAX_IMAGE_PIXELS = None
BAND = .40          # of a line's profile peak
PITCH = 70          # px: rows within which a line's peak is looked for
XH = (28, 90)       # px: an x-height, at 600 dpi
MIN_W = 900         # px: a line this wide at least
MIN_SHARE = .03     # of the lines: a type size has this many
SIZE_REACH = 3      # rows either side of a size's peak
FOUR = ndi.generate_binary_structure(2, 1)


def runs(bits):
    """The lengths of the True runs along the second axis."""
    a = np.zeros((bits.shape[0], bits.shape[1] + 2), np.int8)
    a[:, 1:-1] = bits
    d = np.diff(a, axis=1)
    return np.nonzero(d == -1)[1] - np.nonzero(d == 1)[1]


def stem_of(r):
    r = r[r >= 2]
    if len(r) < 60:
        return 0.0
    h = np.bincount(r)
    c = np.cumsum(h)
    m = int(np.searchsorted(c, len(r) / 2))
    return float(m - .5 + (len(r) / 2 - (c[m - 1] if m else 0)) / h[m])


def load(path):
    return np.asarray(Image.open(path).convert('L')) < 128


def measure(ink, t, b, x0, x1):
    """A line's stem and 2 * ink / perimeter."""
    h = b - t
    m = max(3, h // 5)
    stem = stem_of(runs(ink[t + m:b - m, x0:x1]))
    ln = ink[max(0, t - int(.7 * h)):min(ink.shape[0], b + int(.5 * h)), x0:x1]
    per = int((ln & ~ndi.binary_erosion(ln, FOUR, border_value=0)).sum())
    return stem, 2.0 * ln.sum() / per if per else 0.0


def find_lines(path):
    """A page's lines, (top, bottom, x0, x1), with their stems and strokes."""
    ink = load(path)
    p = ndi.uniform_filter1d(ink.sum(1).astype(float), 3)
    peak = ndi.maximum_filter1d(p, PITCH)
    lab, _ = ndi.label((p > BAND * peak) & (peak > 150))
    out = []
    for s in ndi.find_objects(lab):
        t, b = s[0].start, s[0].stop
        if not XH[0] <= b - t <= XH[1]:
            continue
        m = max(3, (b - t) // 5)
        cols = np.nonzero(ink[t + m:b - m].any(0))[0]
        if not len(cols) or cols[-1] + 1 - cols[0] < MIN_W:
            continue
        x0, x1 = int(cols[0]), int(cols[-1]) + 1
        stem, stroke = measure(ink, t, b, x0, x1)
        if stem:
            out.append((t, b, x0, x1, stem, stroke))
    return out


def stems_at(a):
    path, lines = a
    ink = load(path)
    return [measure(ink, t, b, x0, x1)[0] for t, b, x0, x1, *_ in lines]


def sizes(xh):
    """The book's type sizes from its lines' x-heights: (first, last) rows of each."""
    h = np.bincount(xh, minlength=XH[1] + 3).astype(float)
    peaks = [x for x in range(XH[0], XH[1] + 1)
             if h[x] >= MIN_SHARE * len(xh) and h[x] >= h[x - 1] and h[x] > h[x + 1]]
    out = []
    for i, x in enumerate(peaks):
        lo = x - SIZE_REACH if i == 0 else max(x - SIZE_REACH, peaks[i - 1] + int(np.argmin(h[peaks[i - 1]:x])) + 1)
        hi = x + SIZE_REACH if i == len(peaks) - 1 else min(x + SIZE_REACH, x + int(np.argmin(h[x:peaks[i + 1]])))
        out.append((lo, hi))
    return out


def size_medians(pages, col):
    """Each type size with the median of a column of its lines."""
    all_ = [l for ls in pages.values() for l in ls]
    S = sizes(np.array([l[1] - l[0] for l in all_]))
    return [(lo, hi, float(np.median([l[col] for l in all_ if lo <= l[1] - l[0] <= hi]))) for lo, hi in S]


def size_of(h, S):
    return next((m for lo, hi, m in S if lo <= h <= hi), None)


def read_project(path):
    P = Project.load(path)
    names = [os.path.splitext(p.file.name)[0] + '.tif' for p in P.pages]
    return P, names


def page_settings(s):
    """The output stage's pages in a project's text: (id, setting), the setting that which comes first
    in a page, its settings; the second is the record of what was written."""
    i = s.index('<output>')
    return i, [(m.group(1), int(re.search(r'thresholdAdj="(-?\d+)"', m.group(0)).group(1)))
               for m in re.finditer(r'<page id="(\d+)">.*?</page>', s[i:], flags=re.S)]


def share(y, g):
    """The share of y's variance that the groups g explain."""
    return 1 - sum(((y[g == k] - y[g == k].mean()) ** 2).sum() for k in set(g)) / ((y - y.mean()) ** 2).sum()


def report(args):
    P, names = read_project(args.project)
    s = open(args.project, encoding='utf-8').read()
    folder = os.path.dirname(os.path.abspath(args.project))
    out = os.path.join(folder, re.search(r'outputDirectory="([^"]*)"', s).group(1))
    have = [n for n in names if os.path.exists(os.path.join(out, n))]
    with ProcessPoolExecutor(args.jobs) as ex:
        pages = dict(zip(have, ex.map(find_lines, [os.path.join(out, n) for n in have])))
    for col, label in ((4, 'stem, which fit tunes'), (5, '2 * ink / perimeter, which it does not')):
        S = size_medians(pages, col)
        idx, w = [], []
        for k, n in enumerate(have):
            v = [l[col] / m for l in pages[n] if (m := size_of(l[1] - l[0], S))]
            if len(v) >= args.min_lines:
                idx.append(k)
                w.append(float(np.median(v)))
        w, idx = np.array(w), np.array(idx)
        print(f'{label}: {len(w)} pages, sizes {[(a, b) for a, b, _ in S]}')
        print('  sd %.1f%%, p5-p95 %.1f%%, lightest %s %.3f, heaviest %s %.3f' % (
            100 * w.std(), 100 * np.subtract(*np.percentile(w, [95, 5])),
            have[idx[w.argmin()]], w.min(), have[idx[w.argmax()]], w.max()))
        print('  share of the variance: a 4-page cycle %.0f%%, a 2-page one %.0f%%' % (
            100 * share(w, idx % 4), 100 * share(w, idx % 2)))


def variant(project, new, adj, jobs):
    """The book at one setting, by a copy of the project."""
    folder = os.path.dirname(os.path.abspath(project))
    out = os.path.abspath(os.path.join(new, 'out'))
    os.makedirs(out, exist_ok=True)
    s = open(project, encoding='utf-8').read()
    s = re.sub(r'thresholdAdj="-?\d+"', f'thresholdAdj="{adj}"', s)
    s = re.sub(r'(<directory id="\d+" path=")([^"]*)(")',
               lambda m: m[1] + escape(os.path.normpath(os.path.join(folder, m[2]))) + m[3], s)
    s, n = re.subn(r'outputDirectory="[^"]*"', f'outputDirectory="{escape(out)}"', s, count=1)
    assert n == 1
    proj = os.path.join(new, 'project.ScanTailor')
    open(proj, 'w', encoding='utf-8').write(s)
    subprocess.run([str(BATCH), '--through', 'output', '-q', '--jobs', str(jobs), proj,
                    os.path.join(new, 'project.after.ScanTailor')], check=True)


def solve(A, w):
    """The setting at which a page's weight, by setting, meets 1: stems only grow with the threshold."""
    a = [x for x in A if x in w]
    y = np.maximum.accumulate([w[x] for x in a])
    if y[0] >= 1 or y[-1] <= 1:
        return float(a[0] if y[0] >= 1 else a[-1]), True
    return float(np.interp(1, y + np.arange(len(y)) * 1e-9, a)), False


def apply_settings(project, adj):
    """adj: {page id: setting}, written to each page's settings and not to its record."""
    raw = open(project, 'rb').read().decode('utf-8')
    i = raw.index('<output>')
    changed = 0

    def page(m):
        nonlocal changed
        a = adj.get(m.group(1))
        if a is None:
            return m.group(0)
        new, n = re.subn(r'thresholdAdj="-?\d+"', f'thresholdAdj="{a}"', m.group(0), count=1)
        assert n == 1
        changed += new != m.group(0)
        return new
    tail = re.sub(r'<page id="(\d+)">.*?</page>', page, raw[i:], flags=re.S)
    open(project, 'wb').write((raw[:i] + tail).encode('utf-8'))
    return changed


def fit(args):
    P, names = read_project(args.project)
    s = open(args.project, encoding='utf-8').read()
    base = int(np.median([v for _, v in page_settings(s)[1]]))
    lo, hi, step = (int(x) for x in args.adj.split(','))
    A = sorted(set(range(lo, hi + 1, step)) | {base})
    d = lambda a: os.path.join(args.work, f't{a:+d}', 'out')
    for a in A:
        if len([f for f in os.listdir(d(a)) if f.endswith('.tif')] if os.path.isdir(d(a)) else []) < len(names):
            print(f'writing the book at {a:+d}', file=sys.stderr, flush=True)
            variant(args.project, os.path.dirname(d(a)), a, args.jobs)
    with ProcessPoolExecutor(args.jobs) as ex:
        pages = dict(zip(names, ex.map(find_lines, [os.path.join(d(base), n) for n in names])))
        S = size_medians(pages, 4)
        W = {n: {} for n in names}
        for a in A:
            for n, st in zip(names, ex.map(stems_at, [(os.path.join(d(a), n), pages[n]) for n in names])):
                v = [x / m for x, l in zip(st, pages[n]) if x and (m := size_of(l[1] - l[0], S))]
                if len(v) >= args.min_lines:
                    W[n][a] = float(np.median(v))
    adj, ends, left = {}, [], []
    for n in names:
        if base in W[n]:
            x, end = solve(A, W[n])
            adj[n] = int(round(x))
            if end:
                ends.append(n)
        else:
            left.append(n)
    v = np.array(list(adj.values()))
    w = np.array([W[n][base] for n in adj])
    print(f'{len(adj)} of {len(names)} pages; sizes {[(a, b) for a, b, _ in S]}; the book at {base:+d}')
    print('weight at %+d: sd %.1f%%, p5-p95 %.1f%%' % (base, 100 * w.std(), 100 * np.subtract(*np.percentile(w, [95, 5]))))
    print('settings: %+d to %+d, p5 %+.0f, median %+.0f, p95 %+.0f' % (v.min(), v.max(), *np.percentile(v, [5, 50, 95])))
    if ends:
        print(f'{len(ends)} pages at the end of the sweep, so not reached: {ends[:8]}{" ..." if len(ends) > 8 else ""}')
    if left:
        print(f'too few lines, left as they are: {left}')
    if args.tsv:
        with open(args.tsv, 'w') as f:
            for n, a in adj.items():
                f.write(f'{n}\t{a}\n')
    if args.apply:
        ids = {str(p.id): os.path.splitext(p.file.name)[0] + '.tif' for p in P.pages}
        n = apply_settings(args.project, {i: adj[m] for i, m in ids.items() if m in adj})
        print(f'{n} pages changed in {args.project}; now scantailor-batch --through output --jobs 16 -q PROJECT')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('report', 'fit'):
        p = sub.add_parser(name)
        p.add_argument('project')
        if name == 'fit':
            p.add_argument('work')
            p.add_argument('--adj', default='-30,40,5')
            p.add_argument('--tsv')
            p.add_argument('--apply', action='store_true')
        p.add_argument('--min-lines', type=int, default=6)
        p.add_argument('--jobs', type=int, default=16)
    args = ap.parse_args()
    (report if args.cmd == 'report' else fit)(args)


if __name__ == '__main__':
    main()
