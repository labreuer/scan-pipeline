r"""drawn_view.py: a drawing's pages turned back onto the raw scans, for unpencil_review.py's
drawn view (--drawn DIR, key D).

    python3 -I ~/git/scan-pipeline/drawn_view.py PROJECT.ScanTailor DRAWING.pdf NAMES_DIR OUT_DIR

The pages done are the i_*.png in NAMES_DIR (the review's CLEAN_DIR, say: the pages the
cleaner changed). Each one's page in DRAWING.pdf is rendered at its page image's own
resolution, so that a pixel of the render is a pixel of ScanTailor's output page. It is then
warped onto the raw scan's pixels by the inverse of ScanTailor's own transform
(scantailor.page_transform), nearest neighbour. Boxes drawn on it in the review app are thus in
the raw scan's pixels, as the cleaner's masks, its fixes file and the stamp list are.
OUT_DIR/i_*.png is the result, the raw scan's size, white where the page has nothing.

The PDF's page k (from 1) is the project's k-th page, as the booktext build writes them
(surya_pdf.py pdf, then glyphs.py pdf): a PDF with another number of pages is refused.

Made from a drawing by `glyphs.py pdf --red`, the ink the drawing kept as the scan has it is
red. That is what the review looks for: pencil the cleaner left, which the drawing kept.

Needs numpy, Pillow and PyMuPDF.
"""
import argparse
import glob
import os
import sys

import numpy as np
import pymupdf
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scantailor  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="the book's .ScanTailor project")
    ap.add_argument("pdf", help="the drawing (glyphs.py pdf, with --red to see what it kept)")
    ap.add_argument("names", help="a folder whose i_*.png name the pages to do")
    ap.add_argument("out", help="where the pages go, in the raw scans' pixels")
    a = ap.parse_args()

    p = scantailor.Project.load(a.project)
    doc = pymupdf.open(a.pdf)
    if len(doc) != len(p.pages):
        sys.exit(f"{a.pdf} has {len(doc)} pages, the project {len(p.pages)}: "
                 "the PDF's page k must be the project's k-th page")
    idx = {pg.file.name: (k, pg) for k, pg in enumerate(p.pages)}
    os.makedirs(a.out, exist_ok=True)
    for path in sorted(glob.glob(os.path.join(a.names, "i_*.png"))):
        name = os.path.basename(path)
        if name not in idx:
            sys.exit(f"{name} is not a page of {a.project}")
        k, pg = idx[name]
        page = doc[k]
        imgs = page.get_images()
        if not imgs:
            sys.exit(f"{a.pdf} page {k + 1} has no page image to take the resolution from")
        zoom = imgs[0][2] / page.rect.width                     # the page image's pixels per point
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        raw = Image.open(pg.file.local_path)
        M = scantailor.page_transform(pg)                       # raw pixels -> output pixels
        warped = im.transform(raw.size, Image.AFFINE, tuple(np.asarray(M).flatten()),
                              resample=Image.NEAREST, fillcolor=(255, 255, 255))
        warped.save(os.path.join(a.out, name))
        print(name, "pdf", k + 1, im.size, "->", raw.size)


if __name__ == "__main__":
    main()
