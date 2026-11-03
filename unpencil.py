#!/usr/bin/env python3
"""Erase a reader's pencil strokes (underlines, margin bars) from gray book scans.

usage: unpencil.py SRC_DIR OUT_DIR [--review DIR] [--jobs N] [--only NAMES]

Each i_*.png of SRC_DIR is read; a page with pencil strokes is written to OUT_DIR under its own
name (same size, mode and resolution), with OUT_DIR/masks/<name> the pixels erased; a page with
none is not written. OUT_DIR/unpencil.tsv lists every page: strokes found, pixels erased, and
how many of those belonged to print-like components. --review DIR writes, for each changed
page, the rows it changed before (erased pixels in blue) and after, at half scale.

Pencil is told from print by shape and place first, tone second. Graphite is lighter than the
print's cores (gray ~130-225 against ~80-120 on a ScanSnap's 600 dpi gray) but no lighter than
the print's anti-aliased rims, a pale page's print reaches into its range, and a heavy pencil
line is as dark as print, so tone alone will not do. Per page: the paper (the median) and the
ink (10th percentile of the pixels under 200); "dark" is under ink + 0.30 of the way to paper.

1. Pencil streaks: unbroken runs of non-paper pixels RUN px long along a row (or a column).
   Print makes none (its letters part at every gap: on TSoSR's unmarked pages the longest run
   was 63-153 px, and those few lie in an x-height band, step 3), but for a printed rule
   (straight to a pixel, even, print-dark) or a display face's stems (thicker than STREAK_THICK).
2. Print: the dark components once the streaks' pixels lighter than print are taken out (so
   the pencil no longer joins a letter, while a letter keeps its own dark pixels where the
   pencil crosses it), print-like when their median tone is near the ink.
3. Seeds: pixels under SEED (show-through stays above ~225) at least NEAR px from print, and
   the streaks. The graphite's grain is closed into strokes; a stroke is long, thin and flat,
   not at the image's edge, and (horizontal) its centre is not inside a line of print's
   x-height band.
4. Each stroke's band is traced column by column (its seed's top and foot, carried across
   where letters hide it, smoothed, widened by PAD), and within it everything is set to paper
   but the print: print-like components that reach out of the band (letters, descenders), only
   as far as GEO px into it through the letter (a foot, a bowl's floor; not a trail of graphite
   the letter joins), and marks (a comma, a period: compact, period-sized, ink-dark), with
   their rims (darker in a band than out of it). Under the line's baseline (its mean line,
   which the pencil under it cannot reach, plus the page's x-height) only a descender (a
   vertical run of print) or a mark is kept; about the baseline, nothing between letters.
Twice over, so that a stroke drawn twice, or what a band missed, is found on the second pass.

What it cannot do: graphite pressed along a word's feet, inside the baseline's few rows, is
left as a thin line under that word (no tone, shape or place tells it from the feet there);
a short freehand tick or a mark that is no long stroke (a check, an arrow) is left.
"""
import argparse, csv, os
from multiprocessing import Pool

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

SEED = 215          # a pencil seed pixel is darker than this (show-through stays above ~225)
NEAR = 4            # ... and at least this far from print
RUN = 140           # a pencil streak: an unbroken run of non-paper pixels this long
RULE_STRAIGHT = 1.0 # ... unless a printed rule: its centre line within this of straight (px, sd)
RULE_EVEN = 1.0     # ... its thickness this even (px, sd), and its tone print's
STREAK_THICK = 16   # ... and no thicker than this (median, px): a display face's stems are
EDGE = 30           # a stroke this near the image's edge across it is the paper's edge
CLOSE = (5, 41)     # closing that joins graphite grain into a stroke (across, along it)
MIN_LEN = 150       # a stroke is at least this long (px at 600 dpi: 6.4 mm)
MAX_THICK = 30      # ... its median thickness at most this
MAX_ASPECT = 0.30   # ... its bounding box this flat
MIN_PX = 300        # ... with this many seed pixels
PAD = 3             # a band is widened by this about its seed's top and foot
END_PAD = 6         # ... and lengthened by this at its ends
SMOOTH = 31         # columns a band's edges are smoothed over
PRINT_MIN = 20      # a dark component this big may be print (smaller: grain, specks)
RIM = 2             # rim pixels this close to kept print are print, if darker than the rim cut
BAND_RIM = 0.35     # ... in a band, darker than this (of the ink-paper span)
REGROW = 2          # px kept print is grown back through print-dark pixels the streak took
GEO = 16            # px (through the letter) a kept letter reaches into a band from outside it
UNDER = 1.2         # x-heights under the baseline a fragment of graphite is looked for
NEIGH = 30          # px about a stroke its leftovers are looked for
MARK_EXT = 80       # px across: a seed component this big is a pencil mark (unmarked pages: <= 64)
OVERSHOOT = 3       # rows under the baseline a round letter may reach
WIN = 150           # columns a baseline is found over
SERIF = 6           # px a letter's foot (a serif) reaches beside its body
MARK_W = 25         # a mark (comma, period) inside a band, kept whole: at most this wide,
MARK_MIN = 50       # ... at least this big,
MARK_TONE = 0.12    # ... and its median this near the ink (of the ink-paper span)
PASSES = 2
EIGHT = np.ones((3, 3), bool)


def levels(a):
    paper = float(np.median(a))
    dark = a[a < 200]
    ink = float(np.percentile(dark, 10)) if dark.size > 1000 else 100.0
    span = paper - ink
    return dict(paper=paper, ink=ink, span=span, dark=ink + 0.30 * span,
                plike=ink + 0.20 * span, rim=(ink + paper) / 2)


def streaks(a, L, axis):
    """Pencil streaks along AXIS (1: rows, 0: columns): unbroken runs of non-paper pixels RUN px
    long or more. Print makes none on these scans (its letters part at every gap: the longest run
    on an unmarked page 63-153 px), but for a printed rule: straight to a pixel, even, and as dark
    as print."""
    k = (1, RUN) if axis == 1 else (RUN, 1)
    h = ndi.binary_opening(a < SEED, np.ones(k, bool))
    lab, n = ndi.label(h, structure=EIGHT)
    out = np.zeros(a.shape, bool)
    for i, sl in enumerate(ndi.find_objects(lab)):
        c = lab[sl] == i + 1
        if axis == 0:
            c = c.T
        # a display face's stem or bar (the title page's, ~25 px) is thicker than a pencil line
        if np.median(c.sum(0)[c.any(0)]) > STREAK_THICK:
            continue
        if not is_rule(c, a[sl] if axis == 1 else a[sl].T, L):
            out[sl] |= lab[sl] == i + 1
    return out


def is_rule(c, g, L):
    """Whether a streak (C, its mask along axis 1; G its pixels) is a printed rule: its centre
    line straight to a pixel, its thickness even, its tone print's."""
    cols = np.flatnonzero(c.any(0))
    ys = np.arange(c.shape[0])[:, None]
    cnt = c[:, cols].sum(0)
    ctr = (c[:, cols] * ys).sum(0) / cnt
    resid = ctr - np.polyval(np.polyfit(cols, ctr, 1), cols)
    return (resid.std() < RULE_STRAIGHT and cnt.std() < RULE_EVEN
            and float(np.median(g[c])) < L["plike"])


def running(v, n, fn):
    h = n // 2
    out = np.empty_like(v)
    for i in range(len(v)):
        w = v[max(0, i - h):i + h + 1]
        w = w[~np.isnan(w)]
        out[i] = fn(w) if w.size else np.nan
    return out


def fill_nan(v):
    idx = np.arange(len(v))
    ok = ~np.isnan(v)
    return np.interp(idx, idx[ok], v[ok]) if ok.any() else v


def strokes(seed, printed=None):
    """Long thin strokes along axis 1 of SEED: [(x0, x1, top[], foot[])], each band's top and foot
    per column from x0 to x1. With PRINTED (the print, same axes), a stroke whose centre lies in
    a line of print's x-height band is no underline."""
    joined = ndi.binary_closing(seed, structure=np.ones(CLOSE, bool)) | seed
    lab, n = ndi.label(joined, structure=EIGHT)
    out = []
    for k, sl in enumerate(ndi.find_objects(lab), 1):
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if w < MIN_LEN or h > MAX_ASPECT * w:
            continue
        if sl[0].start < EDGE or sl[0].stop > seed.shape[0] - EDGE:
            continue                                    # the paper's edge, the scan's border
        sub = (lab[sl] == k) & seed[sl]
        if sub.sum() < MIN_PX:
            continue
        top = np.full(w, np.nan)
        foot = np.full(w, np.nan)
        ys = np.arange(h)
        for x in np.flatnonzero(sub.any(0)):
            yy = ys[sub[:, x]]
            top[x], foot[x] = yy.min(), yy.max()
        if np.nanmedian(foot - top + 1) > MAX_THICK:
            continue
        t = fill_nan(running(top, SMOOTH, np.min)) + sl[0].start
        f = fill_nan(running(foot, SMOOTH, np.max)) + sl[0].start
        if printed is not None and in_xheight(printed, sl[1], float(np.median((t + f) / 2))):
            continue
        out.append((sl[1].start, sl[1].stop, t, f))
    return out


def in_xheight(printed, cols, yc):
    """Whether row YC lies inside the x-height band of a line of print over columns COLS: the rows
    about it where the print's profile is at least half its peak."""
    y0 = max(0, int(yc) - 80)
    prof = printed[y0:int(yc) + 81, cols].sum(1).astype(float)
    if prof.max() < 0.05 * (cols.stop - cols.start):
        return False
    i = int(yc) - y0
    if prof[i] < 0.5 * prof.max():
        return False
    # the run of rows about YC at half the peak or more: YC is inside it, not at its edge
    lo = i
    while lo > 0 and prof[lo - 1] >= 0.5 * prof.max():
        lo -= 1
    hi = i
    while hi < len(prof) - 1 and prof[hi + 1] >= 0.5 * prof.max():
        hi += 1
    return lo + 4 <= i <= hi - 4


def band_mask(shape, found, vertical):
    m = np.zeros(shape, bool)
    along = shape[0] if vertical else shape[1]
    for x0, x1, t, f in found:
        xs = np.arange(x0, x1)
        xe = np.arange(max(0, x0 - END_PAD), min(along, x1 + END_PAD))
        te, fe = np.interp(xe, xs, t), np.interp(xe, xs, f)
        for x, a, b in zip(xe, te, fe):
            y0 = max(0, int(np.floor(a)) - PAD)
            y1 = int(np.ceil(b)) + PAD + 1
            if vertical:
                m[x, y0:y1] = True
            else:
                m[y0:y1, x] = True
    return m


def clean_pass(a, L):
    """One pass: (pixels to erase or None, strokes found, print-like pixels among them)."""
    dark = a < L["dark"]
    pen = streaks(a, L, 1) | streaks(a, L, 0)
    # the streaks taken out of the print where lighter than it: a letter the pencil crosses keeps
    # its own dark pixels (a bowl's thin foot), but the pencil no longer joins it
    lab, nl = ndi.label(dark & ~(ndi.binary_dilation(pen) & (a >= L["plike"])), structure=EIGHT)
    ok = np.zeros(nl + 1, bool)
    area = np.zeros(nl + 1)
    med = np.full(nl + 1, 255.0)
    if nl:
        ids = np.arange(1, nl + 1)
        area[1:] = ndi.sum(np.ones(a.shape, np.int32), lab, ids)
        med[1:] = ndi.median(a, lab, ids)
        ok[1:] = (area[1:] >= PRINT_MIN) & (med[1:] < L["plike"])
    printed = ok[lab]
    seed = ((a < SEED) & ~ndi.binary_dilation(printed, iterations=NEAR)) | pen
    found_h = strokes(seed, printed)
    found_v = strokes(seed.T)
    found_m = marks_of(seed)
    n = len(found_h) + len(found_v) + len(found_m)
    if not n:
        return None, 0, 0
    band_h = band_mask(a.shape, found_h, False)
    band = band_h | band_mask(a.shape, found_v, True)
    for sl, c in found_m:
        band[sl] |= c
    # near a stroke (NEIGH px), whatever the band left of the pencil: its seed, and (below)
    # dark fragments under the baseline that belong to no letter
    nbhd = ndi.binary_dilation(band_h, iterations=NEIGH) if found_h else np.zeros(a.shape, bool)
    hit = np.unique(lab[band & printed])
    hit = hit[hit > 0]
    keep = np.zeros(nl + 1, bool)
    mark = np.zeros(nl + 1, bool)
    if hit.size:
        outside = ndi.sum(~band, lab, hit)
        objs = ndi.find_objects(lab)
        hh = np.array([objs[i - 1][0].stop - objs[i - 1][0].start for i in hit])
        ww = np.array([objs[i - 1][1].stop - objs[i - 1][1].start for i in hit])
        # a mark (a comma, a period, a semicolon's foot) is compact, period-sized and as dark as
        # solid ink, unlike a blob of heavy graphite; kept whole
        compact = ((hh >= 0.6 * ww) & (area[hit] >= MARK_MIN) & (ww <= MARK_W)
                   & (med[hit] < L["ink"] + MARK_TONE * L["span"]))
        keep[hit[(outside > 0) | compact]] = True
        mark[hit[compact]] = True
    kept = keep[lab]
    # a kept letter's pixels in a band: only those a short way (GEO px, through the letter) from
    # its part outside the band, a foot or a bowl's floor; not a trail of dark graphite the
    # letter joins, running along the band
    kept = (ndi.binary_dilation(kept & ~band, iterations=GEO, mask=kept) | (kept & ~band)
            | mark[lab])
    dk = a < L["plike"]
    keep_px = grow(kept, dk, a, L)
    # in a band a letter's rim must be darker than outside it: graphite running along a row of
    # feet is as dark as a rim's paler half
    keep_px &= ~band | kept | (a < L["ink"] + BAND_RIM * L["span"])
    # under the baseline (to UNDER x-heights below it) a dark component lying wholly there is
    # graphite: every letter reaches out of it (a p, y or g of the line above, its loop whole; an
    # ascender of the line below), so its pixels are kept, and a mark's (a comma); the rest not,
    # in the band and near it
    under, gaps = below_baseline(dk, found_h, a.shape,
                                 x_height(dk, ndi.binary_dilation(band, iterations=10)))
    inside = np.zeros(nl + 1, bool)
    if under.any():
        inside[1:] = ndi.sum(under, lab, np.arange(1, nl + 1)) == area[1:]
    # a mark wholly under the baseline is no period or comma but a dot where the pencil was set down
    mark &= ~inside
    marks = grow(mark[lab], dk, a, L)
    frag = np.zeros(a.shape, bool)
    if (band & under).any() or (nbhd & under).any():
        anchored = ~inside[lab] & (lab > 0)
        z = band & under
        desc = grow(anchored & (keep_px | ~band), dk, a, L) & (keep_px | ~band)
        keep_px = (keep_px & ~z) | ((desc | marks) & z)
        frag = nbhd & ~band & inside[lab] & (lab > 0)
    # about the baseline, between letters (no letter's body over the column, SERIF px about):
    # a trail of pencil joining the letters' feet, not print
    keep_px = (keep_px & ~(band & gaps)) | (marks & band & gaps)
    erase = band & ~keep_px & (a < L["paper"] - 6)
    # near a stroke, what the band left: its seed (pencil-toned, away from print), the dark
    # fragments wholly under the baseline, the trail between letters' feet
    rest = (ndi.binary_dilation(seed & ~pen, iterations=1) | frag | (gaps & ~printed)
            | ndi.binary_dilation(frag, iterations=RIM)) & nbhd & ~band & ~marks
    erase |= rest & (a < L["paper"] - 6)
    return erase, n, int((erase & printed).sum())


def marks_of(seed):
    """Pencil marks that are no long flat stroke (a bracket, an asterisk, a stroke's hooked end):
    the seed's components, its grain closed, MARK_EXT px across or more. On TSoSR's unmarked
    pages none was more than 64 px across. [(slice, its pixels widened by NEAR + 2)]"""
    j = ndi.binary_closing(seed, np.ones((5, 5), bool)) | seed
    lab, n = ndi.label(j, structure=EIGHT)
    out = []
    H, W = seed.shape
    for k, sl in enumerate(ndi.find_objects(lab), 1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if max(h, w) < MARK_EXT:
            continue
        if (sl[0].start < EDGE or sl[1].start < EDGE or sl[0].stop > H - EDGE
                or sl[1].stop > W - EDGE):
            continue                    # the paper's edge, a folded corner's shadow
        g = NEAR + 2
        big = (slice(max(0, sl[0].start - g), min(H, sl[0].stop + g)),
               slice(max(0, sl[1].start - g), min(W, sl[1].stop + g)))
        c = ndi.binary_dilation(lab[big] == k, iterations=g)
        out.append((big, c))
    return out


def grow(kept, dk, a, L):
    """Kept print grown back where the streak took it (a letter's foot or bowl the pencil runs
    through: through pixels as dark as print, REGROW px), and its rim."""
    kept = ndi.binary_dilation(kept, iterations=REGROW, mask=dk) | kept
    return kept | (ndi.binary_dilation(kept, iterations=RIM) & (a < L["rim"]))


def runs_of(v):
    """[(start, stop)] of the True runs of a 1-d bool array."""
    e = np.flatnonzero(np.diff(np.concatenate(([0], v.astype(np.int8), [0]))))
    return list(zip(e[::2], e[1::2]))


def x_height(dk, avoid):
    """The page's x-height, as its lines of print show it away from the pencil: in strips of 300
    columns, each line's rows from the first to the last where the print's profile is at least
    half the line's peak (the mean line to the baseline); the median. None if the page has too few lines."""
    m = dk & ~avoid
    hs = []
    for c0 in range(0, m.shape[1] - 150, 300):
        prof = m[:, c0:c0 + 300].sum(1).astype(float)
        if prof.max() < 30:
            continue
        for s, e in runs_of(prof >= 0.15 * prof.max()):
            seg = prof[s:e]
            if e - s < 20:
                continue
            hi = np.flatnonzero(seg >= 0.5 * seg.max())
            if 15 <= hi[-1] + 1 - hi[0] <= 90:
                hs.append(hi[-1] + 1 - hi[0])
    return float(np.median(hs)) if len(hs) >= 8 else None


def below_baseline(dk, found, shape, xh):
    """The rows under the baseline (+OVERSHOOT) of the line of print each horizontal stroke
    underlines, window by window (the scans are not deskewed). The baseline is the line's mean
    line (the top of its x-height band, where the print's profile over the rows above the stroke
    first reaches half its peak, which the pencil under the line cannot reach) plus the page's
    x-height. Where there is no print above, or the stroke stands above the baseline so found
    (through the letters), nothing. Also (the second mask) the rows about the baseline in the
    columns between letters."""
    m = np.zeros(shape, bool)
    g = np.zeros(shape, bool)
    if xh is None:
        return m, g
    for x0, x1, t, f in found:
        for c0 in range(x0, x1, WIN):
            c1 = min(x1, c0 + WIN)
            tt, ff = t[c0 - x0:c1 - x0], f[c0 - x0:c1 - x0]
            yc = float(np.median((tt + ff) / 2))
            r0, r1 = max(0, int(yc) - 90), max(0, int(yc) - 8)
            prof = dk[r0:r1, c0:c1].sum(1).astype(float)
            if prof.size == 0 or prof.max() < 0.15 * (c1 - c0):
                continue
            lines = [(s, e) for s, e in runs_of(prof >= 0.15 * prof.max()) if e - s >= 8]
            if not lines:
                continue
            s, e = lines[-1]                                # the nearest line above the stroke
            hi = np.flatnonzero(prof[s:e] >= 0.5 * prof[s:e].max())
            yb = int(round(r0 + s + hi[0] + xh))
            if yb > yc + 4:
                continue
            m[yb + OVERSHOOT:yb + int(UNDER * xh), c0:c1] = True
            # the columns between letters: no print over them in the x-height band, SERIF px about
            body = dk[max(0, yb - int(xh) + 4):max(0, yb - 4), c0:c1].any(0)
            body = ndi.binary_dilation(body, iterations=SERIF)
            g[max(0, yb - 4):yb + OVERSHOOT, c0:c1] |= ~body[None, :]
    return m, g


def clean(a):
    """(cleaned image or None, erase mask, strokes found, print-like pixels erased, levels)."""
    L = levels(a)
    out = a.copy()
    erase = np.zeros(a.shape, bool)
    n = lost = 0
    for _ in range(PASSES):
        e, k, lc = clean_pass(out, L)
        if not k or not e.any():
            break
        erase |= e
        out[e] = int(round(L["paper"]))
        n += k
        lost += lc
    if not erase.any():
        return None, None, 0, 0, L
    return out, erase, n, lost, L


def review(a, out, erase, path):
    ys, xs = np.nonzero(erase)
    rows = np.unique(ys)
    groups, start, prev = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - prev > 60:
            groups.append((start, prev))
            start = r
        prev = r
    groups.append((start, prev))
    x0, x1 = max(0, xs.min() - 60), min(a.shape[1], xs.max() + 60)
    panels = []
    for g0, g1 in groups:
        y0, y1 = max(0, g0 - 70), min(a.shape[0], g1 + 40)
        mark = np.stack([a[y0:y1, x0:x1]] * 3, -1).copy()
        mark[erase[y0:y1, x0:x1]] = (0, 90, 255)
        after = np.stack([out[y0:y1, x0:x1]] * 3, -1)
        panels += [mark, after, np.full((12, x1 - x0, 3), 255, np.uint8)]
    img = Image.fromarray(np.concatenate(panels).astype(np.uint8))
    img.resize((img.width // 2, img.height // 2), Image.LANCZOS).save(path)


def one(job):
    src, out_dir, review_dir = job
    name = os.path.basename(src)
    im = Image.open(src)
    a = np.asarray(im.convert("L"))
    out, erase, n, lost, L = clean(a)
    px = int(erase.sum()) if erase is not None else 0
    if px:
        Image.fromarray(out).save(os.path.join(out_dir, name), dpi=im.info.get("dpi", (600, 600)))
        Image.fromarray(erase).save(os.path.join(out_dir, "masks", name))
        if review_dir:
            review(a, out, erase, os.path.join(review_dir, name))
    return name, n, px, lost, round(L["paper"]), round(L["ink"]), round(L["dark"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--review")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "masks"), exist_ok=True)
    if a.review:
        os.makedirs(a.review, exist_ok=True)
    names = sorted(f for f in os.listdir(a.src) if f.startswith("i_") and f.endswith(".png"))
    if a.only:
        names = [n for n in names if n in set(a.only)]
    with Pool(a.jobs) as p:
        rows = p.map(one, [(os.path.join(a.src, n), a.out, a.review) for n in names])
    with open(os.path.join(a.out, "unpencil.tsv"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["image", "strokes", "erased_px", "printlike_px_erased", "paper", "ink", "dark_cut"])
        w.writerows(rows)
    ch = [r for r in rows if r[2]]
    print(f"{len(rows)} pages, {len(ch)} changed, {sum(r[2] for r in ch)} px erased, "
          f"{sum(r[3] for r in ch)} of them in print-like components")


if __name__ == "__main__":
    main()
