# scan-pipeline

Linux tools for turning a book scanned on the Windows machine (lab-i8700k) into a
ScanTailor project. The book folders stay in `~/scan`; this repo holds only the code.

- **`newscan.py`** takes the newest PDF in `D:\scan` and moves it to
  `~/scan/<ACRONYM>/<ACRONYM>_source.pdf`. It then extracts the page images, writes the
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
~/git/scan-pipeline/scantailor-advanced/build/scantailor-advanced ~/scan/TGR/TGR.ScanTailor
```

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
