#!/usr/bin/env python3
r"""newscan.py: take the newest scan off the Windows machine and extract its page images.

1. Reports the newest PDF in D:\scan on lab-i8700k: its name, date and size.
2. Asks for an acronym; Enter with none cancels. One already used in ~/scan (in any case) is
   refused. A name the scan inventory already gives a book on Windows is
   flagged and asks again.
3. Makes ~/scan/<ACRONYM>/ and moves the PDF into it as <ACRONYM>_source.pdf: copied
   with scp, checked by SHA-256 against Windows, and only then deleted on Windows.
4. Extracts every page image with 8 parallel pdfimages jobs, as the same files that
   PdfExtractImages.exe (Dropbox\scan\tooling\PdfExtractImages.ps1) makes on Windows:
   i_NNNN_N.png, NNNN the page number minus one and N the image's index on its page,
   turned by the page's /Rotate and tagged with the image's resolution (600 dpi for the
   ScanSnap). The pixels are the PDF's own; checked identical to PdfExtractImages.exe's
   on LLaP.
5. Writes <ACRONYM>/<ACRONYM>.ScanTailor listing those images, as `ScanAnalyze.exe *.png`
   does (scantailor.create_initial).
6. Runs ScanTailor's stages on it through Margins (deskew, content box, ...), without the
   GUI, by scantailor-batch (scantailor.process), and says how to open it in the GUI.

Usage: newscan.py            steps 1-6 for the newest scan
       newscan.py ACRONYM    steps 5-6 for ~/scan/ACRONYM's images, keeping its
                             .ScanTailor if it has one (e.g. a folder extracted earlier)
"""
import base64
import concurrent.futures
import json
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

HOST = "lab-i8700k"
WIN_DIR = r"D:\scan"
JOBS = 8
SCANS = Path.home() / "scan"  # where book folders go; the code lives elsewhere
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scantailor  # noqa: E402  (beside this script)
INVENTORY = Path("/srv/share/scans/book_inventory.tsv")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST]


def die(msg):
    sys.exit(f"newscan: {msg}")


def ps_quote(s):
    """A PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def powershell(script):
    """Run a PowerShell script on the Windows machine and return its stdout."""
    script = ("$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
              "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script)
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    p = subprocess.run(SSH + [f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode == 255:
        raise RuntimeError(f"can't reach {HOST} by ssh (is the Windows machine on, or asleep?):"
                           f" {p.stderr.strip()}")
    if p.returncode != 0:
        err = "\n".join(l for l in p.stderr.splitlines() if not l.startswith("#< CLIXML"))
        raise RuntimeError(f"PowerShell on {HOST} failed ({p.returncode}): {(p.stdout + err).strip()}")
    return p.stdout


def ask_yes(prompt):
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def human(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "bytes" else f"{n} bytes"
        n /= 1000


def count(k, word):
    return f"{k} {word}{'s' * (k != 1)}"


def ago(minutes):
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} minute{'s' * (minutes != 1)} ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // (24 * 60)} days ago"


# ---------------------------------------------------------------- 1. the newest PDF

def latest_pdf():
    out = powershell(f"""
        $f = Get-ChildItem -LiteralPath {ps_quote(WIN_DIR)} -File |
             Where-Object {{ $_.Extension -eq '.pdf' }} |
             Sort-Object LastWriteTime | Select-Object -Last 1
        if ($f) {{
            [pscustomobject]@{{
                name = $f.Name; size = $f.Length
                date = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss (ddd)')
                minutes = [int][Math]::Floor(((Get-Date) - $f.LastWriteTime).TotalMinutes)
            }} | ConvertTo-Json -Compress
        }}""").strip()
    return json.loads(out) if out else None


# ---------------------------------------------------------------- 2. the acronym

def inventory_entry(acronym):
    """(Windows folder, title) of a book the inventory files under this name, if any."""
    try:
        lines = INVENTORY.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        cols = line.split("\t")
        folder = cols[0].strip()
        if folder.rsplit("\\", 1)[-1].lower() == acronym.lower():
            return folder, (cols[2].strip() if len(cols) > 2 else "")
    return None


def ask_acronym():
    while True:
        acronym = input("Acronym (Enter to cancel): ").strip()
        if not acronym:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", acronym):
            print("  Letters, digits, _ and - only.")
            continue
        taken = [e.name for e in SCANS.iterdir() if e.name.lower() == acronym.lower()]
        if taken:
            print(f"  {SCANS / taken[0]} already exists; pick another.")
            continue
        entry = inventory_entry(acronym)
        if entry and not ask_yes(f"  The inventory already has {entry[0]}: {entry[1] or '(untitled)'}."
                                 f" Use {acronym} anyway?"):
            continue
        return acronym


# ---------------------------------------------------------------- 3. the move

def sha256_local(path):
    p = subprocess.run(["sha256sum", "--", str(path)], capture_output=True, text=True, check=True)
    return p.stdout.split()[0].upper()


def move_pdf(pdf, dest_dir, acronym):
    """Copy the PDF into dest_dir as <acronym>_source.pdf, check it, delete the Windows one.

    Returns (local path, warning or None). On a failed copy or check, removes what it made
    here, leaves Windows untouched and exits."""
    win_path = f"{WIN_DIR}\\{pdf['name']}"
    part = dest_dir / f".{acronym}_source.pdf.part"
    final = dest_dir / f"{acronym}_source.pdf"
    remote = "/" + win_path.replace("\\", "/")                # /D:/scan/<name>
    remote = re.sub(r"([][*?\\])", r"\\\1", remote)          # scp globs remote names
    try:
        print(f"Copying {pdf['name']} ...", flush=True)
        p = subprocess.run(["scp", "-q", "-p", "-o", "BatchMode=yes", f"{HOST}:{remote}", str(part)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"scp failed: {p.stderr.strip()}")
        size = part.stat().st_size
        if size != pdf["size"]:
            raise RuntimeError(f"copied {size:,} bytes, but Windows listed {pdf['size']:,}")
        digest = sha256_local(part)
        # Hash on Windows and delete in one call, so what's deleted is what was checked.
        out = powershell(f"""
            $p = {ps_quote(win_path)}
            $h = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
            if ($h -ne '{digest}') {{ "MISMATCH $h"; exit 0 }}
            try {{ Remove-Item -LiteralPath $p; 'DELETED' }} catch {{ "KEPT $($_.Exception.Message)" }}
            """).strip()
    except (Exception, KeyboardInterrupt) as e:
        part.unlink(missing_ok=True)
        dest_dir.rmdir()
        if isinstance(e, KeyboardInterrupt):
            die(f"cancelled; removed {dest_dir}, and {win_path} is untouched.")
        die(f"{e}\nRemoved {dest_dir}; {win_path} is untouched.")
    if out.startswith("MISMATCH"):
        part.unlink()
        dest_dir.rmdir()
        die(f"the copy's SHA-256 ({digest}) differs from Windows's ({out.split()[1]}); is the scan"
            f" still being written? Removed {dest_dir}; {win_path} is untouched.")
    part.rename(final)
    final.chmod(0o644)
    if out == "DELETED":
        print(f"Moved to {final} (SHA-256 checked); deleted {win_path} on Windows.")
        return final, None
    warning = (f"{win_path} was copied and checked, but couldn't be deleted on Windows"
               f" ({out.removeprefix('KEPT ')}); delete it there yourself.")
    print(f"Copied to {final} (SHA-256 checked).\n  WARNING: {warning}")
    return final, warning


# ---------------------------------------------------------------- 4. the extraction

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def page_rotations(pdf, pages):
    out = run("pdfinfo", "-f", "1", "-l", str(pages), str(pdf))
    return {int(m[1]): int(m[2]) % 360
            for m in re.finditer(r"^Page\s+(\d+)\s+rot:\s+(\d+)", out, re.M)}


def set_png_dpi(path, xdpi, ydpi):
    """Set a PNG's pHYs chunk (resolution) in place, or insert one before the image data."""
    data = struct.pack(">IIB", round(xdpi / 0.0254), round(ydpi / 0.0254), 1)
    chunk = data + struct.pack(">I", zlib.crc32(b"pHYs" + data))
    with open(path, "r+b") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"{path} is not a PNG")
        pos = 8
        while True:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                raise ValueError(f"{path} has no image data")
            length, kind = struct.unpack(">I4s", header)
            if kind == b"pHYs" and length == 9:
                f.write(chunk)
                return
            if kind == b"IDAT":
                break
            pos += 12 + length
        f.seek(pos)
        rest = f.read()
        f.seek(pos)
        f.write(struct.pack(">I", 9) + b"pHYs" + chunk + rest)


def extract_range(pdf, tmp, dest, first, last, rotations):
    """pdfimages one page range into tmp, then name each page's images as PdfExtractImages.exe does.

    Returns ([(page, final name)], [problem])."""
    rows = []
    for line in run("pdfimages", "-list", "-f", str(first), "-l", str(last), str(pdf)).splitlines()[2:]:
        c = line.split()
        if len(c) >= 6:   # page num type ... x-ppi y-ppi size ratio
            rows.append((int(c[0]), int(c[1]), c[2], c[-4], c[-3]))
    root = tmp / f"r{first}"
    run("pdfimages", "-png", "-p", "-f", str(first), "-l", str(last), str(pdf), str(root))

    done, problems, index = [], [], {}
    for page, num, kind, xppi, yppi in rows:
        src = tmp / f"r{first}-{page:03d}-{num:03d}.png"
        if kind != "image":       # soft masks, stencils: PdfExtractImages.exe skips them too
            continue
        if not src.exists():
            problems.append(f"page {page}: pdfimages wrote no PNG for image {num}")
            continue
        k = index[page] = index.get(page, -1) + 1
        name = f"i_{page - 1:04d}_{k}.png"
        try:
            xdpi, ydpi = float(xppi), float(yppi)
        except ValueError:
            xdpi = ydpi = 0
        if xdpi <= 0 or ydpi <= 0:
            xdpi = ydpi = 600     # what PdfExtractImages.exe always writes
        rot = rotations.get(page, 0)
        if rot:
            from PIL import Image
            with Image.open(src) as im:
                turned = im.transpose({90: Image.Transpose.ROTATE_270,
                                       180: Image.Transpose.ROTATE_180,
                                       270: Image.Transpose.ROTATE_90}[rot])
            if rot != 180:
                xdpi, ydpi = ydpi, xdpi
            turned.save(src, dpi=(round(xdpi), round(ydpi)))
        else:
            set_png_dpi(src, round(xdpi), round(ydpi))
        os.replace(src, dest / name)
        done.append((page, name))
    return done, problems


def extract_images(pdf, dest):
    info = run("pdfinfo", str(pdf))
    m = re.search(r"^Pages:\s+(\d+)", info, re.M)
    if not m:
        raise RuntimeError("pdfinfo found no page count")
    pages = int(m[1])
    rotations = page_rotations(pdf, pages)
    step = max(1, math.ceil(pages / (JOBS * 4)))     # small ranges keep all 8 jobs busy
    ranges = [(a, min(a + step - 1, pages)) for a in range(1, pages + 1, step)]
    tmp = dest / ".extracting"
    tmp.mkdir()
    print(f"Extracting images from {count(pages, 'page')} with {JOBS} jobs ...", flush=True)
    start = time.monotonic()
    done, problems, finished = [], [], 0
    with concurrent.futures.ThreadPoolExecutor(JOBS) as pool:
        futures = {pool.submit(extract_range, pdf, tmp, dest, a, b, rotations): (a, b)
                   for a, b in ranges}
        for fut in concurrent.futures.as_completed(futures):
            a, b = futures[fut]
            try:
                d, p = fut.result()
            except Exception as e:
                d, p = [], [f"pages {a}-{b}: {e}"]
            done += d
            problems += p
            finished += b - a + 1
            print(f"\r  {finished}/{pages} pages", end="", flush=True)
    print()
    shutil.rmtree(tmp)   # leftovers: masks and anything pdfimages wrote but we didn't name
    per_page = {}
    for page, _ in done:
        per_page[page] = per_page.get(page, 0) + 1
    none = [p for p in range(1, pages + 1) if p not in per_page]
    several = [p for p, n in sorted(per_page.items()) if n > 1]
    print(f"Done in {time.monotonic() - start:.1f} s: {count(len(done), 'image')} from"
          f" {count(pages, 'page')}"
          f" in {dest}/")
    if rot_pages := sorted(p for p, r in rotations.items() if r):
        print(f"  turned by /Rotate: {len(rot_pages)} pages ({brief(rot_pages)})")
    if several:
        print(f"  pages with more than one image: {brief(several)}")
    if none:
        print(f"  pages with no image: {brief(none)}")
    for p in problems:
        print(f"  PROBLEM: {p}")
    return not problems


def brief(nums, limit=12):
    s = ", ".join(map(str, nums[:limit]))
    return s + (f", ... ({len(nums)} in all)" if len(nums) > limit else "")


# ---------------------------------------------------------------- main

def make_project(folder):
    """Steps 5-6: <folder>/<folder>.ScanTailor for the images in folder, unless it has one,
    then ScanTailor's stages through Margins on it. Returns success."""
    project = folder / f"{folder.name}{scantailor.EXTENSION}"
    if project.exists():
        print(f"Using the existing {project}.")
    else:
        try:
            scantailor.create_initial(folder)
        except FileNotFoundError as e:
            print(f"No project written: {e}")
            return False
        print(f"Wrote {project} (ScanAnalyze's initial project).")
    # ScanTailor's GUI asks to relink a project whose output folder is missing, so make it,
    # as move-sourcepdf did on Windows; scantailor-batch then caches thumbnails in it too.
    (folder / "out").mkdir(exist_ok=True)
    print("Running ScanTailor through Margins ...", flush=True)
    try:
        p = scantailor.process(project)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"ScanTailor processing failed: {e}")
        return False
    angles = [pg.deskew.angle for pg in p.pages if pg.deskew]
    blank = [pg.file.name for pg in p.pages if pg.select_content and pg.select_content.is_empty]
    print(f"Processed {count(len(p.pages), 'page')}: deskewed {min(angles):+.2f}° to {max(angles):+.2f}°"
          + (f"; no content found on {count(len(blank), 'page')} ({brief(blank, 6)})" if blank else "")
          + f".\nOpen it with: {shlex.quote(str(scantailor.BATCH.with_name('scantailor-advanced')))}"
            f" {shlex.quote(str(project))}")
    return True


def main(args):
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    if args:
        folder = SCANS / args[0]
        if not folder.is_dir():
            die(f"{folder} isn't a folder")
        return 0 if make_project(folder) else 1
    try:
        pdf = latest_pdf()
    except RuntimeError as e:
        die(str(e))
    if not pdf:
        print(f"No PDFs in {WIN_DIR} on {HOST}.")
        return 0
    print(f"Newest PDF in {WIN_DIR} on {HOST}:\n"
          f"  name  {pdf['name']}\n"
          f"  date  {pdf['date']}, {ago(pdf['minutes'])}\n"
          f"  size  {human(pdf['size'])} ({pdf['size']:,} bytes)")
    acronym = ask_acronym()
    if not acronym:
        print("Cancelled; nothing moved.")
        return 0
    dest = SCANS / acronym
    dest.mkdir()
    local, warning = move_pdf(pdf, dest, acronym)
    try:
        ok = extract_images(local, dest)
    except (Exception, KeyboardInterrupt) as e:
        what = "cancelled" if isinstance(e, KeyboardInterrupt) else f"failed: {e}"
        print(f"\nImage extraction {what}\n  The PDF is at {local}; {dest} may hold some of its"
              f" images.")
        ok = False
    if ok:
        ok = make_project(dest)
    else:
        print(f"No {acronym}{scantailor.EXTENSION} written, since the extraction had problems.")
    if warning:
        print(f"WARNING: {warning}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(130)
