#!/usr/bin/env python3
"""Find ink that set a ScanTailor content box's edge without being the page's print, and narrow the
boxes named.

usage: st_marks.py find PROJECT REPORT.json [--crops SHEET.png] [--jobs N]
       st_marks.py apply PROJECT REPORT.json --pages PAGES [--rows PAGES] [--pad PX]
       st_marks.py lost PROJECT OLD_PROJECT OLD_OUT --pages PAGES

A box is set by the outermost ink ScanTailor's content detection takes for content, and one fleck
outside the print widens or lengthens the page, and through the page size every page in the book.
Run this on pages already straight (st_lean.py), whose output is up to date; PAGES count from 1
in the project's order, as booktext's st_margins.py's do.

find works inward from each side of each page's output (bilevel, ink only inside the box). The
outermost component of a side is a mark if it stands wholly beyond the print's local margin (the
median first or last inked column of the rows around it) and it is either a speck (TINY px of ink
or less, smaller than any letter or point of a book), or no bigger than a point and set off from
everything (SMALL px across at most, no ink within GAP px inward on its rows); or if it is under a
letter's size (BLOT px across, BLOT_INK px of ink) and wholly past the page's own text edge by
CLEAR px: a blot in the margin, where a hyphen or comma ending a line never stands wholly past
that edge. That last holds only on a justified side (an index, ragged, has no edge). It is taken
away and the next outermost tried, so the walk stops at print, and only marks that set an edge are
found. It also lists:
- groups: a row of ink off by itself at a page's head or foot, RUN_GAP px or more clear of the next,
  under RUN_H high and RUN_W wide, and not a folio (near the centre). A mark of two pieces passes
  every test above, each piece the other's neighbour (Jonas's p. 134: 14x18 and 6x6 px, 165 px under
  its last line, its box 189 px too tall).
- pads: the room between the box and the ink at each side. A mark pale enough to go at the threshold
  is not in the output, yet detection, which works from the gray, may have set the box by it: 4 or 5
  px a side as a rule, more where one stood (Jonas: flecks of gray 150-200 on paper of 235, up to 49
  px past the ink, the book's widest box among them).

Its finds are proposals. It took the i of "ing" for marks on an indented page, a letter's broken
tips for specks, and the ear of a g, so --crops draws every mark for looking at, and on the gray scan
a doubtful one settles (`raw` in each mark is its place there). Nothing is written to the project.

apply narrows the boxes of the pages named, and only those, from that report: past each side's marks
(the room the box had kept, or just inside the mark if that is nearer the print), a pad over PAD + 2
down to PAD (5), and a group cut off. It sets their detection to manual. It refuses a page whose
box has changed since the report: a report is good for one pass. --rows PAGES sets a page's sides
instead from its full rows of print alone (a reader's notes in a margin, too big for marks): rows
spanning the measure with both ends near the medians, the print's width checked against the measure.

lost compares the pages named with an earlier output of the same scan (its project and output
folder): the ink each has lost, and its largest pieces. Those should be the marks. A box narrowed
can move Otsu's threshold by a level, which shows as thousands of pixels in pieces of a few each.
"""
import argparse, json, os, statistics, sys
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from scantailor import Project, Rect, page_transform

Image.MAX_IMAGE_PIXELS = None
TINY = 40           # px of ink
SMALL = 16          # px, width and height: the smallest type's letters are more
GAP = 24            # px of clear paper inward
BLOT, BLOT_INK = 30, 300    # px across, px of ink: under a text letter's
CLEAR = 8           # px past the page's text edge (the 10th or 90th percentile of its rows' ends)
NEAR = 220          # rows either side that give the local margin
SOLID = .25         # share of the rows ending at an edge for a side to be justified
RUN_GAP = 100       # px clear of the next row of ink
RUN_H, RUN_W = 50, 150  # px: a row of ink no taller, no wider than this may be a mark
CENTRED = 120       # px off the text's centre: nearer is a folio
SHOW = 3            # px: a mark's gain under this is not reported as proposing anything


def pages_of(spec):
    """'11,26-27' -> {11, 26, 27}"""
    out = set()
    for part in filter(None, spec.split(",")):
        first, _, last = part.partition("-")
        out.update(range(int(first), int(last or first) + 1))
    return out


def local_margin(a, y0, y1, side):
    r0, r1 = max(0, y0 - NEAR), min(a.shape[0], y1 + 1 + NEAR)
    band = a[r0:r1]
    rows = band.any(1)
    if rows.sum() < 12:
        return None
    b = band[rows]
    return int(np.median(b.argmax(1))) if side == "L" else int(np.median(a.shape[1] - 1 - b[:, ::-1].argmax(1)))


def walk(a, lab, comps, side):
    marks = []
    H, W = a.shape
    rows = np.where(a.any(1))[0]
    firsts, lasts = a[rows].argmax(1), W - 1 - a[rows][:, ::-1].argmax(1)
    edge_l, edge_r = float(np.percentile(firsts, 10)), float(np.percentile(lasts, 90))
    solid_l = (np.abs(firsts - edge_l) <= 12).mean() >= SOLID
    solid_r = (np.abs(lasts - edge_r) <= 12).mean() >= SOLID
    while True:
        live = [c for c in comps if not c.get("gone")]
        if not live:
            break
        if side in "LR":
            c = min(live, key=lambda c: c["x0"]) if side == "L" else max(live, key=lambda c: c["x1"])
            m = local_margin(a, c["y0"], c["y1"], side)
            beyond = m is not None and (c["x1"] < m if side == "L" else c["x0"] > m)
            ry0, ry1 = max(0, c["y0"] - 6), c["y1"] + 7
            win = a[ry0:ry1, c["x1"] + 1:c["x1"] + 1 + GAP] if side == "L" else a[ry0:ry1, max(0, c["x0"] - GAP):c["x0"]]
        else:
            c = min(live, key=lambda c: c["y0"]) if side == "T" else max(live, key=lambda c: c["y1"])
            ink = np.where(a.any(1))[0]
            order = ink[ink > c["y1"]] if side == "T" else ink[ink < c["y0"]]
            beyond = len(order) > 0 and (order[0] - c["y1"] > GAP if side == "T" else c["y0"] - order[-1] > GAP)
            win = np.zeros((0, 0), bool)
        setoff = not win.any()
        big = max(c["y1"] - c["y0"], c["x1"] - c["x0"]) + 1
        past = side in "LR" and (solid_l and c["x1"] < edge_l - CLEAR if side == "L" else solid_r and c["x0"] > edge_r + CLEAR)
        if (beyond and (c["area"] <= TINY or (big <= SMALL and setoff))) or (past and big <= BLOT and c["area"] <= BLOT_INK):
            a[lab == c["i"]] = False
            c["gone"] = True
            marks.append(dict(side=side, x0=c["x0"], y0=c["y0"], x1=c["x1"], y1=c["y1"], area=c["area"]))
        else:
            break
    return marks


def row_runs(a):
    on = a.any(1).astype(int)
    d = np.diff(np.r_[0, on, 0])
    return list(zip(np.where(d == 1)[0].tolist(), np.where(d == -1)[0].tolist()))


def groups(a):
    b, out = a.copy(), []
    for side in "BT":
        while True:
            runs = row_runs(b)
            if len(runs) < 3:
                break
            (y0, y1), other = (runs[-1], runs[-2]) if side == "B" else (runs[0], runs[1])
            clear = y0 - other[1] if side == "B" else other[0] - y1
            xs = np.where(b[y0:y1].any(0))[0]
            all_x = np.where(b.any(0))[0]
            mid = (all_x.min() + all_x.max()) / 2
            if (clear >= RUN_GAP and y1 - y0 <= RUN_H and xs.max() - xs.min() < RUN_W
                    and abs((xs.min() + xs.max()) / 2 - mid) > CENTRED):
                out.append(dict(side=side, x0=int(xs.min()), y0=int(y0), x1=int(xs.max()), y1=int(y1 - 1), clear=int(clear),
                                area=int(b[y0:y1].sum()), inner=int(other[1] - 1 if side == "B" else other[0])))
                b[y0:y1] = False
            else:
                break
    return out


def page(job):
    k, fn, name, rect, box = job
    a = np.array(Image.open(fn).convert("1")) == 0
    if a.sum() < 200:
        return dict(k=k, name=name, blank=True)
    lab, n = ndi.label(a, structure=np.ones((3, 3)))
    areas = ndi.sum(a, lab, range(1, n + 1))
    comps = [dict(i=i + 1, x0=s[1].start, x1=s[1].stop - 1, y0=s[0].start, y1=s[0].stop - 1, area=int(areas[i]))
             for i, s in enumerate(ndi.find_objects(lab))]
    ys, xs = np.where(a)
    old = [int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())]
    grp = groups(a)
    marks = []
    for side in "LRTB":
        marks += walk(a, lab, comps, side)
    ys, xs = np.where(a)
    new = [int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())]
    return dict(k=k, name=name, blank=False, rect=rect, box=box, ink=old, print=new, marks=marks, groups=grp,
                pads=[old[0] - box[0], box[0] + box[1] - 1 - old[1]])


def output_folder(path, p):
    out = p.root.get("outputDirectory") or "out"
    return out if os.path.isabs(out) else os.path.join(os.path.dirname(os.path.abspath(path)), out)


def image(folder, pg):
    return os.path.join(folder, os.path.splitext(pg.file.name)[0] + ".tif")


def output_box(pg):
    """the box's left edge and width on the output page, from the record of what was written"""
    oc = pg.output.element.find("output-params/image/content-rect")
    return (float(oc.get("x")), float(oc.get("width"))) if oc is not None else None


def gain(r):
    return (r["print"][0] - r["ink"][0]) + (r["ink"][1] - r["print"][1])


def crops(res, folder, p, out):
    cells = []
    items = [(gain(r), r, m) for r in res if not r.get("blank") and gain(r) >= SHOW for m in r["marks"] if m["side"] in "LR"]
    items += [(0, r, m) for r in res if not r.get("blank") for m in r["groups"]]
    for g, r, m in sorted(items, key=lambda t: (-t[0], t[1]["k"])):
        a = np.array(Image.open(image(folder, p.pages[r["k"]])).convert("L"))
        x0 = max(0, m["x0"] - (70 if m["side"] == "L" else 250))
        y0 = max(0, m["y0"] - (225 if m["side"] == "B" else 75))
        c = Image.fromarray(a[y0:y0 + (310 if m["side"] == "B" else 160), x0:x0 + 330]).convert("RGB")
        d = ImageDraw.Draw(c)
        d.rectangle((m["x0"] - x0 - 7, m["y0"] - y0 - 7, m["x1"] - x0 + 7, m["y1"] - y0 + 7), outline=(255, 0, 0), width=2)
        d.text((2, 2), f"{r['k'] + 1} {m['side']} {g} px", fill=(0, 0, 255))
        cells.append(c)
    if not cells:
        return
    cols, W, H = 7, 336, max(c.height for c in cells) + 6
    sheet = Image.new("RGB", (cols * W, ((len(cells) + cols - 1) // cols) * H), (128, 128, 128))
    for n, c in enumerate(cells):
        sheet.paste(c, ((n % cols) * W, (n // cols) * H))
    sheet.save(out)
    print(f"{len(cells)} marks drawn in {out}")


def find(a):
    p = Project.load(a.project)
    folder = output_folder(a.project, p)
    jobs = []
    for k, pg in enumerate(p.pages):
        if pg.select_content.is_empty:
            continue
        box = output_box(pg)
        if box is None:
            sys.exit(f"{pg.file.name} has no record of an output: run scantailor-batch --through output first")
        r = pg.select_content.content_rect
        jobs.append((k, image(folder, pg), pg.file.name, (r.x, r.y, r.width, r.height), box))
    with Pool(a.jobs) as pool:
        res = pool.map(page, jobs, chunksize=2)
    for r in res:
        if not r.get("blank"):
            M = page_transform(p.pages[r["k"]])
            inv = np.linalg.inv(np.vstack([M, [0, 0, 1]]))
            for m in r["marks"] + r["groups"]:
                m["raw"] = [int(v) for v in (inv @ np.array([m["x0"], m["y0"], 1.0]))[:2]]
    json.dump(res, open(a.report, "w"))
    live = [r for r in res if not r.get("blank")]
    print(f"{len(live)} pages with ink. Boxes by width: {[(int(r['rect'][2]), r['k'] + 1) for r in sorted(live, key=lambda r: -r['rect'][2])[:6]]}")
    print("marks setting an edge:")
    for r in sorted(live, key=lambda r: -gain(r)):
        if gain(r) >= SHOW:
            print(f"  {r['k'] + 1} {r['name']}: {gain(r)} px,", [(m["side"], m["x1"] - m["x0"] + 1, m["y1"] - m["y0"] + 1, m["area"]) for m in r["marks"] if m["side"] in "LR"])
    print("rows of ink alone at a head or foot:", [(r["k"] + 1, g["side"], g["y1"] - g["y0"] + 1, g["clear"]) for r in live for g in r["groups"]])
    print("box past the ink by more than 7 px:", [(r["k"] + 1, r["pads"]) for r in live if max(r["pads"]) > 7])
    print("box within a pixel of the ink:", [(r["k"] + 1, r["pads"]) for r in live if min(r["pads"]) < 1])
    if a.crops:
        crops(res, folder, p, a.crops)


def rows_edges(fn, measure):
    a = np.array(Image.open(fn).convert("1")) == 0
    rows = np.where(a.any(1))[0]
    f = a[rows].argmax(1)
    l = a.shape[1] - 1 - a[rows][:, ::-1].argmax(1)
    w = l - f
    m = np.abs(w - measure) <= .03 * measure
    n = m & (np.abs(f - np.median(f[m])) <= 24) & (np.abs(l - np.median(l[m])) <= 24)
    return int(f[n].min()), int(l[n].max())


def apply(a):
    p = Project.load(a.project)
    folder = output_folder(a.project, p)
    report = {r["k"]: r for r in json.load(open(a.report)) if not r.get("blank")}
    measure = statistics.median(pg.select_content.content_rect.width for pg in p.pages if not pg.select_content.is_empty)
    plain, by_rows = pages_of(a.pages), pages_of(a.rows)
    if plain & by_rows:
        sys.exit(f"named twice: {sorted(plain & by_rows)}")
    plan = []
    for n in sorted(plain | by_rows):
        k = n - 1
        r = report.get(k)
        if r is None:
            sys.exit(f"page {n} is not in {a.report}")
        sc = p.pages[k].select_content
        c = sc.content_rect
        if [c.x, c.y, c.width, c.height] != list(r["rect"]):
            sys.exit(f"page {n}'s box has changed since {a.report}: run find again")
        o, pr = r["ink"], r["print"]
        pl, pa = r["pads"]
        bl, br = o[0] - pl, o[1] + pa        # the box on the output page
        if n in by_rows:
            nl, nr = rows_edges(image(folder, p.pages[k]), measure)
            if abs(nr - nl + 1 - measure) > .03 * measure:
                sys.exit(f"page {n}: full rows give a print {nr - nl + 1} px wide against a measure of {measure:.0f}: set it by hand")
            sl, sr = round(nl - a.pad - bl), round(br - (nr + a.pad))
            st = sb = 0
        else:
            left = [m for m in r["marks"] if m["side"] == "L"]
            right = [m for m in r["marks"] if m["side"] == "R"]
            sl = sr = 0
            if pr[0] > o[0] and left:
                sl = round(max(pr[0] - max(pl, 0), max(m["x1"] for m in left) + 1) - bl)
            elif pl > a.pad + 2:
                sl = round(pl - a.pad)
            if pr[1] < o[1] and right:
                sr = round(br - min(pr[1] + max(pa, 0), min(m["x0"] for m in right) - 1))
            elif pa > a.pad + 2:
                sr = round(pa - a.pad)
            foot = [g["inner"] for g in r["groups"] if g["side"] == "B"]
            head = [g["inner"] for g in r["groups"] if g["side"] == "T"]
            sb = o[3] - min(foot) if foot else 0
            st = max(head) - o[2] if head else 0
        plan.append((n, k, c, max(sl, 0), max(sr, 0), max(st, 0), max(sb, 0)))
    for n, k, c, sl, sr, st, sb in plan:
        sc = p.pages[k].select_content
        sc.content_rect = Rect(c.x + sl, c.y + st, c.width - sl - sr, c.height - st - sb)
        sc.params.set("contentDetectionMode", "manual")
        print(f"  {n} {p.pages[k].file.name}: {int(c.width)} x {int(c.height)} -> {int(c.width - sl - sr)} x {int(c.height - st - sb)}")
    p.save()
    print(f"{a.project}: {len(plan)} boxes narrowed. Run scantailor-batch --through output, then find again with a new report.")


def lost(a):
    new = Project.load(a.project)
    old = Project.load(a.old_project)
    folder = output_folder(a.project, new)
    for n in sorted(pages_of(a.pages)):
        k = n - 1
        dx, dy = (page_transform(new.pages[k]) - page_transform(old.pages[k]))[:, 2]
        dx, dy = int(round(dx)), int(round(dy))
        was = np.array(Image.open(image(a.old_out, old.pages[k])).convert("1")) == 0
        now = np.array(Image.open(image(folder, new.pages[k])).convert("1")) == 0
        moved = np.zeros_like(now)
        ys, xs = np.where(was)
        on = (xs + dx >= 0) & (xs + dx < now.shape[1]) & (ys + dy >= 0) & (ys + dy < now.shape[0])
        moved[ys[on] + dy, xs[on] + dx] = True
        gone = moved & ~now
        lab, m = ndi.label(gone, structure=np.ones((3, 3)))
        sizes = sorted((int(v) for v in ndi.sum(gone, lab, range(1, m + 1))), reverse=True)[:5] if m else []
        print(f"  {n} {new.pages[k].file.name}: {int(gone.sum())} px lost, {int((~on).sum())} off the page, largest pieces {sizes}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("project")
    f.add_argument("report")
    f.add_argument("--crops")
    f.add_argument("--jobs", type=int, default=16)
    f.set_defaults(run=find)
    g = sub.add_parser("apply")
    g.add_argument("project")
    g.add_argument("report")
    g.add_argument("--pages", default="")
    g.add_argument("--rows", default="")
    g.add_argument("--pad", type=int, default=5)
    g.set_defaults(run=apply)
    h = sub.add_parser("lost")
    h.add_argument("project")
    h.add_argument("old_project")
    h.add_argument("old_out")
    h.add_argument("--pages", required=True)
    h.set_defaults(run=lost)
    a = ap.parse_args()
    a.run(a)


if __name__ == "__main__":
    main()
