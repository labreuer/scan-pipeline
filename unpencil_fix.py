#!/usr/bin/env python3
"""Hand fixes on unpencil.py's output: what its rules leave or should not have touched, fixed box
by box, kept in a file so that they are applied again after unpencil.py is run again.

usage: unpencil_fix.py SRC_DIR CLEAN_DIR FIXES.tsv

FIXES.tsv: image, op, x0, y0, x1, y1, note (the box in the image's pixels; '#' comments). Ops:
  free     erase every component of non-paper pixels lying wholly inside the box and smaller than
           a letter (under MAX_H px high): leftover graphite that touches no print
  lighter  erase the box's pixels lighter than the print (its 'plike' cut, or the number in note
           written cut=N): pale pencil beside letters
  paper    erase the whole box
  restore  put the box back as the scan has it (undo the cleaner there, pencil and all)
Each changed page is written back into CLEAN_DIR (a page unpencil.py did not change is copied from
SRC_DIR first), its mask in CLEAN_DIR/masks updated (restored pixels taken out of it) and its
overlay in CLEAN_DIR/overlay removed, so that unpencil_review.py draws it again.
CLEAN_DIR/unpencil-fix.log lists what each fix changed.
"""
import argparse, csv, os, shutil

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

SEED = 215      # non-paper (as unpencil.py)
MAX_H = 22      # px: a component this high or more is a letter's size, not a leftover (x-height ~37)


def levels(a):
    paper = float(np.median(a))
    dark = a[a < 200]
    ink = float(np.percentile(dark, 10)) if dark.size > 1000 else 100.0
    return paper, ink, ink + 0.20 * (paper - ink)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("clean")
    ap.add_argument("fixes")
    a = ap.parse_args()
    rows = [r for r in csv.reader(open(a.fixes), delimiter="\t") if r and not r[0].startswith("#")]
    by_page = {}
    for r in rows:
        by_page.setdefault(r[0], []).append(r)
    log = open(os.path.join(a.clean, "unpencil-fix.log"), "w")
    os.makedirs(os.path.join(a.clean, "masks"), exist_ok=True)
    for name, fixes in sorted(by_page.items()):
        dst = os.path.join(a.clean, name)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(a.src, name), dst)
        im = Image.open(dst)
        img = np.asarray(im.convert("L")).copy()
        orig = np.asarray(Image.open(os.path.join(a.src, name)).convert("L"))
        mpath = os.path.join(a.clean, "masks", name)
        paper, ink, plike = levels(orig)
        fill = int(round(paper))
        for r in fixes:
            op = r[1]
            x0, y0, x1, y1 = (int(v) for v in r[2:6])
            note = r[6] if len(r) > 6 else ""
            s = (slice(y0, y1), slice(x0, x1))
            before = img[s].copy()
            if op == "free":
                # components of the whole page, so that one reaching out of the box is seen whole
                lab, n = ndi.label(img < SEED, structure=np.ones((3, 3), bool))
                ids = np.unique(lab[s]); ids = ids[ids > 0]
                objs = ndi.find_objects(lab)
                for i in ids:
                    sl = objs[i - 1]
                    if (sl[0].start >= y0 and sl[0].stop <= y1 and sl[1].start >= x0 and sl[1].stop <= x1
                            and sl[0].stop - sl[0].start < MAX_H):
                        c = ndi.binary_dilation(lab[sl] == i, iterations=1) & (img[sl] < paper - 6)
                        img[sl][c] = fill
            elif op == "lighter":
                cut = float(note.split("cut=")[1].split()[0]) if "cut=" in note else plike
                sub = img[s]
                sub[(sub >= cut) & (sub < paper - 6)] = fill
            elif op == "paper":
                sub = img[s]
                sub[sub < paper - 6] = fill
            elif op == "restore":
                img[s] = orig[s]
            else:
                raise SystemExit(f"unknown op {op!r} in {r}")
            changed = before != img[s]
            print(f"{name}\t{op}\t{x0},{y0},{x1},{y1}\t{int(changed.sum())} px\t{note}", file=log)
        # the mask is what now differs from the scan: the cleaner's erasures, the fixes', less
        # what was restored
        mask = img != orig
        Image.fromarray(img).save(dst, dpi=im.info.get("dpi", (600, 600)))
        Image.fromarray(mask).save(mpath)
        ov = os.path.join(a.clean, "overlay", name)
        if os.path.exists(ov):
            os.remove(ov)
    log.close()
    print(f"{len(rows)} fixes on {len(by_page)} pages; log in {os.path.join(a.clean, 'unpencil-fix.log')}")


if __name__ == "__main__":
    main()
