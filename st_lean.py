#!/usr/bin/env python3
"""How far each page of a ScanTailor project leans, and its shear set to take that out.

usage: st_lean.py PROJECT [--tsv OUT.tsv] [--min DEG] [--apply] [--jobs N]

Rows of print can stand level on a page whose margins lean: the page is sheared, a parallelogram,
which rotation cannot mend (Jonas's *The Gnostic Religion*, from a sheet feeder: baselines within
0.04 degrees, margins leaning up to 0.9). ScanTailor's Deskew corrects this by its Oblique angle,
but shears by tan(angle / 2), so a margin's lean moves by half the angle set: a lean L is taken
out by an oblique of 2L. Its automatic oblique is no help on such a book (Jonas: 93 pages better,
118 worse), so the angle is measured here and set by hand.

The lean is read from the output pages (the project's output folder, which must be up to date).
Only rows of ink spanning the book's measure count, within 3% of the median content box's width:
a heading, a short line or an indented paragraph has no margin to tell. Each such row's midpoint
is fitted against its height, outliers dropped, which needs neither margin to be whole. A page
with too few such rows is tried on its own commonest row width (a page of indented quotation). A
fit is kept only over FIT_SPAN rows of the page, FIT_ROWS rows of print or more, its residual under
FIT_RES px; the rest are listed as not measured, and their shear is left alone (a page of verse
fitted to 190 px gave 3.3 degrees, nothing like its lean).

--apply adds 2L to the oblique of every measured page leaning --min degrees or more (0.25), in
manual mode. A page so changed whose content box was set by hand goes back to automatic detection
and is listed: its box was for the page unsheared, and has to be set again (st_marks.py). Then run
scantailor-batch --through output, and this again to see the result; a second --apply adds to what
is there, so a page over- or under-corrected comes right the same way.
"""
import argparse, csv, math, os, statistics, sys
from multiprocessing import Pool

import numpy as np
from PIL import Image

from scantailor import Project

Image.MAX_IMAGE_PIXELS = None
BAND = .03          # a row spans the measure within this share of it
OWN_BAND = 30       # px either side of a page's own commonest width
FIT_ROWS, FIT_SPAN, FIT_RES = 120, 900, 6.0


def fit(y, mid):
    keep = np.ones(len(y), bool)
    for _ in range(5):
        b, c = np.polyfit(y[keep], mid[keep], 1)
        r = mid - (c + b * y)
        s = max(4, 2 * np.median(np.abs(r[keep])))
        keep = np.abs(r) < s
    return dict(lean=math.degrees(math.atan(b)), rows=int(keep.sum()), span=float(y[keep].max() - y[keep].min()),
                res=float(np.median(np.abs(r[keep]))))


def good(f):
    return f is not None and f["rows"] >= FIT_ROWS and f["span"] >= FIT_SPAN and f["res"] <= FIT_RES


def page(job):
    k, fn, measure = job
    a = np.array(Image.open(fn).convert("1")) == 0
    rows = np.where(a.any(1))[0]
    if len(rows) < FIT_ROWS:
        return k, None
    first = a[rows].argmax(1)
    last = a.shape[1] - 1 - a[rows][:, ::-1].argmax(1)
    width = last - first

    def over(mask, how):
        if mask.sum() < FIT_ROWS:
            return None
        f = fit(rows[mask].astype(float), (first[mask] + last[mask]) / 2.0)
        f["how"] = how
        return f if good(f) else None

    f = over(np.abs(width - measure) <= BAND * measure, "measure")
    if f is None:
        big = width[width > measure / 2]
        if len(big) >= FIT_ROWS:
            h, e = np.histogram(big, bins=np.arange(measure / 2, measure * 1.1, 24))
            f = over(np.abs(width - (e[h.argmax()] + 12)) <= OWN_BAND, "own width")
    return k, f


def output_folder(path, p):
    out = p.root.get("outputDirectory") or "out"
    return out if os.path.isabs(out) else os.path.join(os.path.dirname(os.path.abspath(path)), out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--tsv")
    ap.add_argument("--min", type=float, default=.25, help="degrees: --apply leaves pages leaning less")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--jobs", type=int, default=16)
    a = ap.parse_args()
    p = Project.load(a.project)
    folder = output_folder(a.project, p)
    live = [(k, pg) for k, pg in enumerate(p.pages) if not pg.select_content.is_empty]
    measure = statistics.median(pg.select_content.content_rect.width for _, pg in live)
    jobs = [(k, os.path.join(folder, os.path.splitext(pg.file.name)[0] + ".tif"), measure) for k, pg in live]
    with Pool(a.jobs) as pool:
        got = dict(pool.map(page, jobs, chunksize=4))
    fits = {k: f for k, f in got.items() if f}
    leans = sorted(abs(f["lean"]) for f in fits.values())
    print(f"{len(fits)} of {len(live)} pages measured; measure {measure:.0f} px; lean median {statistics.median(leans):.3f}, "
          f"max {leans[-1]:.3f} degrees; {sum(x >= a.min for x in leans)} at {a.min} or more, "
          f"{sum(1 for f in fits.values() if f['how'] != 'measure')} by their own width")
    print("not measured (project order, from 1):", ",".join(str(k + 1) for k in got if not got[k]))
    if a.tsv:
        with open(a.tsv, "w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow("page image oblique lean rows span residual how".split())
            for k, pg in enumerate(p.pages):
                if k in fits:
                    v = fits[k]
                    w.writerow([k + 1, pg.file.name, pg.deskew.params.get("oblique"), round(v["lean"], 3), v["rows"],
                                round(v["span"]), round(v["res"], 1), v["how"]])
    todo = {k: f for k, f in fits.items() if abs(f["lean"]) >= a.min}
    for k, f in sorted(todo.items(), key=lambda t: -abs(t[1]["lean"]))[:12]:
        print(f"  {k + 1} {p.pages[k].file.name} lean {f['lean']:.2f} ({f['rows']} rows, {f['how']})")
    if not a.apply:
        return
    boxes = []
    for k, f in todo.items():
        pg = p.pages[k]
        d = pg.deskew.params
        d.set("oblique", str(round(float(d.get("oblique") or 0) + 2 * f["lean"], 3)))
        d.set("oblique-mode", "manual")
        sc = pg.select_content
        if sc.params.get("contentDetectionMode") == "manual":
            sc.params.set("contentDetectionMode", "auto")
            boxes.append(k + 1)
    p.save()
    print(f"{a.project}: oblique set on {len(todo)} pages; boxes back to automatic, to be set again: "
          + (",".join(map(str, boxes)) or "none"))


if __name__ == "__main__":
    main()
