# scan-pipeline

Linux tools for turning a book scanned on the Windows machine (lab-i8700k) into a
ScanTailor project. The book folders stay in `~/scan`; this repo holds only the code.

- **`newscan.py`** takes the newest PDF in `D:\scan`, or the one `--pdf NAME` names, and moves
  it to `~/scan/<ACRONYM>/<ACRONYM>_source.pdf`. It then extracts the page images, writes the
  initial ScanTailor project and runs ScanTailor through Margins. `newscan.py ACRONYM`
  redoes the last two steps for a folder already in `~/scan`.
- **`scantailor.py`** reads `.ScanTailor` files into memory for other scripts: pages,
  images and each stage's settings. They can be edited and saved, and what isn't edited
  saves back unchanged. It also has `create_initial()` (what `ScanAnalyze.exe *.png`
  writes), `process()` (runs `scantailor-batch`), and the `to-v4` and `to-v3` commands
  below.
- **`scantailor-advanced/`** is a submodule: the `scan-pipeline` branch of
  [labreuer/scantailor-advanced](https://github.com/labreuer/scantailor-advanced), a fork of
  ScanTailor Advanced 1.2.1. That branch adds `scantailor-batch` (`src/batch/`), which runs
  ScanTailor's stages through Margins without the GUI. It is built from ScanTailor's own
  internal code, so it only works with the ScanTailor version the submodule pins.
- **`unpencil.py`**, **`unpencil_fix.py`** and **`unpencil_review.py`** take a reader's
  pencil (underlines, margin bars) off gray scans before ScanTailor sees them.
  **`drawn_view.py`** turns the drawing booktext makes back onto the raw scans, so the review
  can check it too (below).
- **`scantailor.py`** also gives `page_transform(page)`, ScanTailor's map from a raw scan's
  pixels to its output page's.
- **`st_lean.py`** measures how far a project's pages lean and sets the shear that takes it out;
  **`st_marks.py`** finds the marks that set its content boxes and narrows the boxes named (below,
  "Straight pages and clean boxes").
- **`st_weight.py`** measures how evenly a project's type came out and sets a threshold for each
  page to even it (below, "Even weight").
- **`stview.py`** shows in a browser, page by page, where a project's content boxes sit on the
  scans and where ScanTailor's Page Layout stage will put them (below). `~/bin/stview.py`
  links to it.

## Setup

Fedora packages for the build:

```
sudo dnf install cmake ninja-build qt6-qtbase-devel qt6-qtsvg-devel qt6-qttools-devel \
    qt6-linguist boost-devel libglvnd-devel libjpeg-turbo-devel libpng-devel \
    libtiff-devel zlib-devel
```

`newscan.py` also needs `pdfimages` (poppler-utils) and passwordless ssh to lab-i8700k.
Then:

```
cd ~/git/scan-pipeline
git submodule update --init
cmake -S scantailor-advanced -B scantailor-advanced/build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build scantailor-advanced/build
```

That builds the GUI, `scantailor-advanced/build/scantailor-advanced`, and
`scantailor-advanced/build/scantailor-batch`. `scantailor.py` looks for the batch tool
there; set `SCANTAILOR_BATCH` to use another.

## Use

```
~/git/scan-pipeline/newscan.py
~/git/scan-pipeline/newscan.py --pdf "Pattern_Breakers (Lynn).pdf"
~/git/scan-pipeline/scantailor-advanced/build/scantailor-advanced ~/scan/TGR/TGR.ScanTailor
```

`--pdf` takes a file directly in `D:\scan`, by its exact name, with or without `.pdf`; a name it
can't find gets the nearest names, and nothing moves. The move deletes the Windows original once
its SHA-256 is checked, and `D:\scan` holds more than scans, so `newscan.py </dev/null` (with or
without `--pdf`) says what would be taken and stops there.

The whole road from here to the book in the library, in order and with its commands, is the
**build-book** skill (`/build-book ACRONYM [PDF]`, source `~/booktext/build-book/`).

## Pencil in a book

A reader's pencil on gray scans (a ScanSnap's 600 dpi) comes off before ScanTailor, on the
scans themselves, so that the deskew and content boxes, ScanTailor's output and the OCR all see
the pages clean. First used on Kuhn's *Structure of Scientific Revolutions* (`~/scan/TSoSR`,
2026-11-02: 36 pages of 224).

1. **`unpencil.py SRC OUT`** finds the strokes on each `i_*.png` and writes the pages it changed
   to `OUT` (with `OUT/masks/`, what it erased, and `OUT/unpencil.tsv`). A page with no pencil
   is not written. Its docstring says how it tells graphite from print, and what it can't do (pencil along a
   word's feet; a short tick that is no long stroke).
2. **`unpencil_review.py SRC OUT`** serves the changed pages on `127.0.0.1:8765`, what was
   erased in red, the original a key away. Box what it got wrong; the boxes are saved as
   `SRC/unpencil-review.json`.
3. **`unpencil_fix.py SRC OUT FIXES.tsv`** applies hand fixes, box by box, kept in a file so
   that they come back whenever step 1 is run again: `free` (loose graphite in a box),
   `lighter` (pixels lighter than print), `paper` (the whole box), `restore` (the scan as it
   was). Use `paper` rectangles from exact component boxes, not `free` or `lighter`
   unchecked: `free` takes i dots and periods, and `lighter` takes pale print.
4. **Install**: the originals into `SRC/marked/` (with the masks and a README giving the
   commands, so that the pages can be made again from `marked/`), the cleaned pages in their
   place; then a fresh project, `newscan.py ACRONYM`.
5. **Check**: a margin mark the script missed widens that page's content box past the text
   block's (TSoSR's p. 10: a bracket whose hook joined an "f").

Some letters can't be cleaned on the scan: those with pencil fused to them, and those where
the cleaner erased print (a g's loop) and the scan was put back there. List them in a stamp
list (TSoSR's `TSoSR.unpencil-stamp.tsv`: image, box in the raw scan's pixels, why). The
drawing stage then draws them anew from the book's own glyphs: `glyphs.py pdf --zones` in
`~/booktext`, with the boxes mapped onto the output pages by `page_transform`.

The review continues on the drawing:

6. **Draw** with `glyphs.py pdf --zones ZONES.tsv --red`, which shows in red the ink the
   drawing kept as the scan has it.
7. **`drawn_view.py PROJECT DRAWING.pdf OUT DRAWN`** turns each changed page of that drawing
   back onto its raw scan. **`unpencil_review.py SRC OUT --drawn DRAWN`** then shows it at
   key D, in the same pixels as the cleaned page, its original and the boxes.
8. **Look for leftovers.** Pencil the drawing kept shows red where the cleaner's masks touch
   it. Box what is wrong, add those boxes to the stamp list, and draw again.

On TSoSR the stamp list's mapping, the leftovers search and the join from review boxes to the
stamp list were still per-book scripts (`runs/TSoSR/work/zones.py`, `leftovers.py`). TSoSR's
own record of the whole run, with every command, is `~/scan/TSoSR/marked/README.md`.

## Straight pages and clean boxes: `st_lean.py`, `st_marks.py`

A sheet-fed scan can come out sheared, its rows of print level and its margins leaning, and one
fleck in a margin sets a content box and, through it, the page size of the whole book. Both tools
read the project's output pages, so run them after `scantailor-batch --through output`, in this order,
before Page Layout's margins are settled. The whole account, with what they found on Jonas's
*The Gnostic Religion*, is in `~/booktext/ocr/README.md`.

```
st_lean.py PROJECT [--tsv OUT.tsv]    # what each page leans; full rows of print alone
st_lean.py PROJECT --apply            # oblique on pages at --min degrees (0.25) or more
scantailor-batch --through output --jobs 16 -q PROJECT   # and again till none is left

st_marks.py find PROJECT REPORT.json --crops SHEET.png   # proposals: look at each
st_marks.py apply PROJECT REPORT.json --pages 193,231 --rows 73,78
scantailor-batch --through output --jobs 16 -q PROJECT
st_marks.py lost PROJECT OLD_PROJECT OLD_OUT --pages 193,231   # what went
st_marks.py find PROJECT NEW_REPORT.json
st_marks.py edges PROJECT [--tsv OUT.tsv]    # what find misses: places to look at
```

PAGES count from 1 in the project's order. ScanTailor shears by tan(angle / 2), so a page leaning L
takes an oblique of 2L; its automatic oblique did that book no good. `st_lean.py` keeps a fit only
over enough rows and a small residual, and leaves the rest alone and listed. A page it shears has its
hand-set box put back to automatic, so boxes come after it. `st_marks.py find` writes nothing, and takes
real print for marks now and then (the i of "ing" on an indented page, the ear of a g), so `apply`
touches only pages named, and refuses a report older than the box. `--rows` sets a page's sides from
its full rows of print, for a reader's notes in a margin. Their docstrings say how each decides.

`edges` reads a box against its ink on all four sides (`find`'s pads are left and right only, and pencil
too pale for the threshold leaves a box standing past the ink), lists boxes on pages with no ink, and
ink well past the text's edge on a run or two of rows, where a reader's pencil joins the print. Run on
the boxes of Strauss's *Continual Permutations of Action* before they were cut, it listed 19 of 287 pages,
among them all 8 that were cut by hand or emptied; on Jonas's, 32 of 373. Most of the rest are print, and
the smallest flecks are left to `find`, so run both and look at what they list.

## Even weight: `st_weight.py`

One threshold for a book gives even thresholds, not even type: Otsu cuts at much the same darkness on
every page, and what differs is the strokes on the paper. Jonas's *The Gnostic Religion* runs from 6.8 px
of stem to 8.7, a page's weight following the raw scan's stem at half darkness (0.97), the two sides of a
printing sheet 6% apart, and no one setting helps: the spread is the same from -30 to +30.

```
st_weight.py report PROJECT                   # how even the output is, and whether a 4-page cycle explains it
st_weight.py fit PROJECT WORK_DIR --tsv OUT.tsv   # the book at each setting, in copies: 45 s each
st_weight.py fit PROJECT WORK_DIR --apply     # the same sweep; writes each page's setting
scantailor-batch --through output --jobs 16 -q PROJECT
st_weight.py report PROJECT
```

Run it last of ScanTailor's steps, after `st_lean.py`, `st_marks.py` and the margins, since a page's box
sets its Otsu level, and before the line detector. It moves nothing on the page. It measures lines, not
pages, against the median of their own type size, which it finds from the lines' x-heights, so a page of
notes doesn't read light. The book's overall weight stays where the project's median setting has it.
On Jonas: settings -28 to +26, 343 of 366 pages changed, 2 * ink / perimeter's sd 5.2% to 1.3%, the 4-page
cycle's share of the variance 22% to 1%, letters about even (221 fusions undone, 87 fragments mended; 168
fusions made, 106 fragments lost). Judge a fit by that second measure, as `report` gives it: the stem is
what was tuned and flatters it. It can't reach a page's insides, where top to bottom can drift 10%. The
docstring says how it decides.

## Alignment and margins: `stview.py`

```
cd ~/scan/GtS && stview.py
```

It serves a viewer on `127.0.0.1:8765` (or the next free port) and opens it in the browser. A
dropdown lists the folder's `*.ScanTailor` files, newest first. The chosen one is read again
whenever it changes on disk, so a change saved in ScanTailor shows a moment later. It needs only
the standard library, and Pillow for TIFF scans.

Three views (key M):

- **Absolute**: the deskewed scan, at one scale for every page, with its content box, to compare
  where the content sits from page to page.
- **Unaligned**: the content where it sat on its scan, against the max content box as placed on
  that parity's scans, and a dotted box where ScanTailor will put it. The side panel says how far
  ScanTailor moves it.
- **Output**: the page as Page Layout will make it: the page size, this page's hard and soft
  margins, and the max content box (the page less the usual margins).

Left/Right go page by page, or two at a time with **Step 2** to stay on one side of the spread.
A click pins a guide line, kept between Unaligned and Output; Esc clears it. The side panel gives
the page's alignment, its margins (hard, soft and to the page's edge, with a page's own margins
marked), its content box and any warnings.

What it works out, from the project file alone:

- **The layout** is ScanTailor's own arithmetic, not its output files. The page size is the
  largest content box plus hard margins among the aligned pages, and each page's alignment splits
  what is left over into its soft margins. So it shows what the current settings will give before
  Output is run. Page Layout keeps each page's content size from when it last processed that page;
  the viewer uses Select Content's, and warns on pages where the two differ.
- **Unaligned** needs to know where the max box sits on the scans. For each parity (even and odd
  page index, or the left and right halves of split spreads) it takes the full text pages, those
  whose content box is at least 95% of the max box both ways, and places the box as their most
  common alignment places them: at the median of their top edges, for pages aligned to the top.
  Each page then shows where its own content sat against that box. A full text page hardly moves:
  on GtS (142 even and 145 odd full pages) the median move is 0, and 95% of them move less than
  2 mm across and 0.9 mm up or down, from how each page lay on the scanner. An opener aligned to
  the bottom shows how far ScanTailor moves it down.
- **Shear** (ScanTailor 1.2's oblique correction in Deskew) is drawn as ScanTailor applies it, after
  the rotation, x' = x - tan(oblique / 2) * y, and the side panel gives the angle. On TGR's 167 sheared
  pages the outline it computes is ScanTailor's. `auto` and `original` alignment are drawn as centred,
  with a warning.

Checked 2026-11-04:

- **Deskew**: on every page without shear in the 103 project files in `~/scan` and
  `/srv/share/scans` (30,510 pages; versions 3, 4 and none; split spreads, and pages turned 90°
  and 180°), the page outline it computes is ScanTailor's `rotated-page-outline` within 0.01 px.
  The 853 pages left are TGR's three projects' pages with shear.
- **Layout**: on every project in `~/scan` with output (GtS, ISM's two, SMEB, TSoSR, WCT: 1,067
  pages), its page size and content position are the ones ScanTailor made the output with (each
  page's `output-params`), within half a pixel. The exception is GtS (aligned), whose output was
  made before its alignment was changed.

First used 2026-10-31 on GtS, to choose which pages to align to the top, the bottom or the
centre: `~/scan/GtS/GtS (aligned).ScanTailor`.

## Updating ScanTailor

Changes to ScanTailor itself go on the fork's `scan-pipeline` branch: commit them in the
submodule and push, then commit the submodule's new version here. To take in a new
upstream release:

```
cd ~/git/scan-pipeline/scantailor-advanced
git remote add upstream https://github.com/ScanTailor-Advanced/scantailor-advanced.git  # once
git fetch upstream && git rebase upstream/master
cmake --build build && git push --force-with-lease
cd .. && git commit -am "Update ScanTailor Advanced"
```

After an update, check `scantailor-batch` on a project, since upstream may have changed
the internal code it is built from.

The branch's changes to ScanTailor itself, besides `scantailor-batch`:

- **New pages follow the profile's auto oblique** (77c13452; see "Converting ScanTailor
  projects" below).
- **Savitzky-Golay smoothing off by default** (c8be5973), for a page with no output settings
  yet, in the GUI and in `scantailor-batch`. Upstream has it on. It blurs the grayscale before
  the threshold. On *What Coleridge Thought* (600 dpi) it thickened every stroke, by 6% of the
  scanner's ink on 1-bit scans and 17% on gray ones, and it wiped out thin parts: the pages
  showed periods for commas and colons for semicolons. With it off, the 1-bit pages keep the
  scanner's ink within 1% and every comma. A project whose pages already have output settings
  keeps them. To turn it off there, change the pages' settings alone, then run `--through output`
  again:
  `sed -i '/<output-params>/,/<\/output-params>/!s/savitzkyGolaySmoothing="1"/savitzkyGolaySmoothing="0"/g' PROJECT`.
  Each page's `<output-params>` records what its file was made with, and ScanTailor writes a page
  again only where its settings differ from that record. A `sed` over the whole file changes the
  record too, and `--through output` then writes nothing (*Governing the Soul*: none of 352 pages).

## Converting ScanTailor projects from version 3 to 4

The ScanTailor Advanced in the `scantailor-advanced` submodule (v1.2.1) writes
`version="4"` on the first line of a `.ScanTailor` file. It refuses any other version
with:

> The project file is not compatible with the current application version.

ScanTailor Advanced wrote version 3 from 2018 through v1.1.3. 1.2.0 (May 2026) moved to 4.
Version 3 files include:

- projects made by the ScanTailor Advanced on lab-i8700k (a July 2018 build), such as the
  folders `copy-raw-scans` brings into `/srv/share/scans`;
- the initial project `ScanAnalyze.exe *.png` writes, and `scantailor.create_initial()`
  (step 5 of `newscan.py`).

### How

```
python3 ~/git/scan-pipeline/scantailor.py to-v4 /srv/share/scans/LLaP/LLaP.ScanTailor
```

This converts the file in place. The original is kept as `LLaP.ScanTailor.v3`, a name a
`*.ScanTailor` glob won't match. That matters because ScanTailor opens only the first
file a glob gives it. `to-v4` takes several projects at once. It refuses files that are
already version 4 and files with no version (see below).

It changes four things:

1. **Version:** `version="3"` becomes `version="4"`.
2. **Folders:** the image folders and the output folder are set to where they are now.
   A project made on Windows names `D:/scan/LLaP` and `D:\scan\LLaP\out`. Any folder at or
   under the project's original folder (the output folder's parent) is moved to the same
   place under the project file's folder. `out/` is created if it's missing; otherwise
   ScanTailor opens with its relinking dialog. If an image folder can't be found, `to-v4`
   warns, and ScanTailor will ask you to relink it.
3. **Oblique:** each deskew entry with no oblique (shear) setting gets
   `oblique-mode="manual"`.
4. **Deskew:** each automatic deskew angle is set to manual, so it stays as computed.
   Pass `--auto-deskew` to skip this.

#### Projects ScanTailor hasn't processed yet

For a project with no processing in it yet, such as one `create_initial` wrote, the
version number is the only thing to change:

```
sed -i '1s/ version="3"/ version="4"/' TGR.ScanTailor
```

`newscan.py` doesn't need this. Its last step runs `scantailor-batch`, which reads a
version 3 project as version 4 and saves it as 4.

### Why not just change the number?

A project ScanTailor has already processed changes when 1.2 processes it again. That
happens with batch processing, and also whenever you view a page in the GUI at Deskew or
a later stage.

I measured this on LLaP's project from Windows: 284 pages, with content boxes adjusted in
ScanAnalyze. I converted it three ways, then ran every stage through Margins with
`scantailor-batch`:

| Converted with | Pages given shear correction | Content boxes moved > 5 px | Deskew angles changed |
|---|---|---|---|
| version and folders only | 189 | 138 | 19 |
| + oblique set to manual | 0 | 4 | 19 |
| + deskew set to manual (`to-v4`) | 0 | 0 | 0 |

There are two causes, both in ScanTailor 1.2's own code:

- **Oblique correction is new in 1.2.** A deskew entry without an oblique setting is
  read with the deskew's own mode (`deskew/Params.cpp`, `obliqueModeFromXml`), so an
  automatic deskew also gets automatic shear correction. The 2018 build never did this.
  Shear changes the page geometry, so automatic content boxes are detected again. A box
  adjusted in ScanAnalyze is still marked automatic, so the adjustment is lost.
- **1.2 recomputes automatic deskew angles.** It compares the stored angle with a fresh,
  zeroed one (`deskew/Task.cpp`), so any nonzero automatic angle is recomputed. On 19 of
  LLaP's pages its finder came out up to 0.0625° different. That again re-detects those
  pages' content boxes.

Freezing the angles keeps exactly what Windows computed. To have 1.2 deskew a page again,
set it back to Auto in the GUI's Deskew stage (Apply to… for many pages), or convert with
`--auto-deskew`.

Related: 1.2.1 also gives every *new* page automatic shear correction, whatever the
default profile says. `deskew/Utils.cpp` `buildDefaultParams()` hard-codes `MODE_AUTO`.
Commit 04cb7364 had made it follow the profile; commit 9c347201 put `MODE_AUTO` back.
The submodule's `scan-pipeline` branch fixes this (77c13452): new pages follow the
profile, whose auto oblique option is off by default, as the Windows build never
sheared. A project processed before the fix keeps its shear correction until it is
processed again from a fresh initial project.

### After converting

- **Windows ScanTailor:** it can't open the converted file, because it wants version 3.
  The `.v3` copy is the original.
- **How 1.2 saves:** when 1.2 saves the project, it writes paths relative to the project
  file and alignment as `center`, where older versions wrote `vcenter`/`hcenter`.
  ScanAnalyze's `Enum.Parse` doesn't recognise `center`. `scantailor.py` reads both.
- **Order matters:** convert a processed Windows project with `to-v4` *before* running
  `scantailor-batch` on it. The batch tool does change 1 by itself, but not changes 3
  and 4.

### Projects with no version

Classic Scan Tailor, which isn't ScanTailor Advanced, writes no version attribute. Among
the 85 book folders checked (D: A–H, plus a sample of E:), 47 main projects are like
this, along with their variants: 197 of 470 files. Theirs is a different format, and
ScanTailor 1.2 refuses those files too. `to-v4` leaves them alone, and converting them
hasn't been tried.

### What was checked (2026-10-31)

- **All version 3 files:** all 272 in a copy of your files converted. Every image folder
  ends up absolute and found, and every output folder exists.
- **LLaP after conversion:** converting with `to-v4` and then processing changed nothing
  in deskew, content boxes, content sizes or margins, on all 284 pages.
- **The GUI:** it opens the converted LLaP at its saved page (36 of 284), and a converted
  initial project, without the relinking dialog.
- **`scantailor.py`:** all 469 complete projects still load and save back byte-identical.

## Converting version 4 back to 3, for Windows

```
python3 ~/git/scan-pipeline/scantailor.py to-v3 GtS/GtS.ScanTailor [--windows-folder 'D:\scan\GtS'] [-o OUT]
```

For a project processed here (ScanTailor Advanced 1.2.1 writes version 4) that the Windows ScanTailor
(the 2018 build) and ScanAnalyze are to open. In place, keeping the original as `<name>.v4`, unless
`-o` names where to write. It changes:

1. **Version:** `version="4"` becomes `version="3"`.
2. **Folders:** the image folder and the output folder become absolute Windows paths under the
   project's folder on Windows (by default `D:\scan\<folder name>`), written as the Windows build
   writes them: `path="D:/scan/GtS"`, `outputDirectory="D:\scan\GtS\out"`.
3. **Alignment:** `center` becomes `hcenter` / `vcenter` (ScanAnalyze's `Enum.Parse` refuses
   `center`); `auto` and `original`, which version 3 lacks, the same, with a warning.
4. **What only version 4 writes** is dropped: the deskew's `oblique` and `oblique-mode` (shear;
   a page whose shear wasn't 0 is warned of, since the Windows build will draw it unsheared),
   `algoContentBased`, `postDeskewAngle`, `fillOutsidePageBox`, `wienerCoef`, `wienerWinSize`, and
   the `automask` and `source_file` elements. Checked against a Windows project (AS): the converted
   file then holds no element or attribute the Windows one doesn't.

First used 2026-10-31 for GtS: the folder copied to `D:\scan\GtS` by tar over ssh
(`tar -cf - GtS | ssh lab-i8700k "tar -xf - -C D:/scan"`, 3.2 GB in 28 s, every file's size checked
with `copy-raw-scans`' `remote_listings`), then its three project files converted and copied over
the version 4 ones. Not yet opened in the Windows build.
