r"""scantailor.py: ScanTailor project files (.ScanTailor), parsed and held in memory.

A Python port of the project-file half of ScanAnalyze (github.com/labreuer/ScanAnalyze:
ScanTailor.Xml.cs and ScanTailor.cs), for scripts on Linux. It reads classic ScanTailor
and ScanTailor Advanced projects alike.

    import sys; sys.path.insert(0, "/home/labreuer/git/scan-pipeline")
    from scantailor import Project, create_initial

    p = Project.load("/home/labreuer/scan/LLaP/LLaP.ScanTailor")
    for page in p.pages:                      # the project's page order
        page.file.name, page.sub_page         # 'i_0000_0.png', 'single' | 'left' | 'right'
        page.image.size, page.image.dpi       # Size(3032, 4782), Size(600.0, 600.0)
        page.image.rotation                   # fix-orientation: 0, 90, 180 or 270
        page.deskew.angle                     # each stage is None while the project has
        page.select_content.content_rect      #   no entry for it (see below)
        page.page_layout.hard_margins_mm
        page.output.threshold_adj

    create_initial("/home/labreuer/scan/XYZ")  # what `ScanAnalyze.exe *.png` writes there
    process("/home/labreuer/scan/XYZ/XYZ.ScanTailor")   # ScanTailor through Margins, no GUI

Batch processing (the GUI's, or process()) first gives every page ScanTailor's default
entry for every stage, so a stage that hasn't run yet can be a placeholder rather than
None: a 0x0 content box, content-size-mm of -1.

Each object is a view onto its XML element (`.element`), as in ScanAnalyze: reading a
property reads the XML, a setter changes it, and Project.save() writes the file back with
everything else as it was. Anything not modelled here is reachable through `.element`, or
Project.filter_element() for a filter's entry.
"""
from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple

EXTENSION = ".ScanTailor"
SUB_PAGES = ("single", "left", "right")
# Alignment values: ScanTailor up to 2019 (your Windows build) writes vcenter/hcenter;
# since 2020 it writes center, adds auto and original, and reads anything else as center.
ALIGN_VERT = ("top", "vcenter", "center", "bottom", "auto", "original")
ALIGN_HOR = ("left", "hcenter", "center", "right", "auto", "original")
MM_PER_INCH = 25.4


class Size(NamedTuple):
    width: float
    height: float


class Rect(NamedTuple):
    x: float
    y: float
    width: float
    height: float

    @property
    def left(self): return self.x

    @property
    def top(self): return self.y

    @property
    def right(self): return self.x + self.width

    @property
    def bottom(self): return self.y + self.height


class Margins(NamedTuple):
    left: float
    top: float
    right: float
    bottom: float


DEFAULT_MARGINS_MM = Margins(10, 5, 10, 5)   # ScanTailor's defaults


# ---------------------------------------------------------------- XML helpers

def _num(text):
    """An attribute's number: int when it is one, else float."""
    f = float(text)
    return int(f) if f.is_integer() and "." not in text and "e" not in text.lower() else f


def _fmt(v):
    """A number as ScanTailor writes it: 600, not 600.0."""
    if isinstance(v, int):
        return str(v)
    s = repr(float(v))
    return s[:-2] if s.endswith(".0") else s


def _points(el):
    return [] if el is None else [(float(p.get("x")), float(p.get("y"))) for p in el.findall("point")]


def _rect(el):
    return None if el is None else Rect(*(float(el.get(k)) for k in ("x", "y", "width", "height")))


def _size(el, w="width", h="height"):
    return None if el is None else Size(_num(el.get(w)), _num(el.get(h)))


def _required(el, path):
    found = el.find(path)
    if found is None:
        raise ValueError(f"<{el.tag} id={el.get('id')!r}> has no {path}")
    return found


# ---------------------------------------------------------------- the views

class _View:
    """An object backed by one XML element of a Project."""
    __slots__ = ("element", "project")

    def __init__(self, element, project):
        self.element = element
        self.project = project

    @property
    def id(self):
        return self.element.get("id")

    def __repr__(self):
        return f"<{type(self).__name__} id={self.id}>"


class Directory(_View):
    """<directories>/<directory>: a folder holding the project's images."""

    @property
    def path(self):
        """The folder as the project stores it (forward slashes, e.g. D:/scan/LLaP)."""
        return self.element.get("path")

    @property
    def local_path(self):
        """The folder on this machine: see Project.localize()."""
        return self.project.localize(self.path)


class File(_View):
    """<files>/<file>: one image file."""

    @property
    def name(self): return self.element.get("name")

    @property
    def dir_id(self): return self.element.get("dirId")

    @property
    def directory(self): return self.project.directories[self.dir_id]

    @property
    def path(self):
        """The file's path as the project has it."""
        return f"{self.directory.path.rstrip('/')}/{self.name}"

    @property
    def local_path(self):
        return self.directory.local_path / self.name

    @property
    def image(self): return self.project._image_by_file.get(self.id)

    @property
    def pages(self):
        return self.image.pages if self.image else []

    def __repr__(self):
        return f"<File id={self.id} {self.name}>"


class Image(_View):
    """<images>/<image>: a file's image, split into 1 or 2 pages."""

    @property
    def file_id(self): return self.element.get("fileId")

    @property
    def file(self): return self.project._file_by_id.get(self.file_id)

    @property
    def sub_pages(self): return int(self.element.get("subPages"))

    @sub_pages.setter
    def sub_pages(self, n): self.element.set("subPages", str(n))

    @property
    def file_image(self): return int(self.element.get("fileImage", "0"))

    @property
    def size(self): return _size(_required(self.element, "size"))

    @property
    def dpi(self):
        return Size(*(float(_required(self.element, "dpi").get(k)) for k in ("horizontal", "vertical")))

    @property
    def pixels_per_mm(self):
        d = self.dpi
        return Size(d.width / MM_PER_INCH, d.height / MM_PER_INCH)

    @property
    def removed(self):
        """'L' or 'R' when ScanAnalyze deleted that half of a two-page image, else None."""
        return self.element.get("removed")

    @property
    def rotation(self):
        """fix-orientation: degrees the image is turned clockwise (0, 90, 180, 270)."""
        el = self.project.filter_element("fix-orientation", self.id)
        rot = None if el is None else el.find("rotation")
        return 0 if rot is None else int(rot.get("degrees"))

    @property
    def pages(self): return self.project._pages_by_image.get(self.id, [])

    def __repr__(self):
        f = self.file
        return f"<Image id={self.id} {f.name if f else '?'}>"


class Page(_View):
    """<pages>/<page>: a page, i.e. all or half of an image."""

    @property
    def image_id(self): return self.element.get("imageId")

    @property
    def image(self): return self.project._image_by_id.get(self.image_id)

    @property
    def file(self):
        img = self.image
        return img.file if img else None

    @property
    def sub_page(self): return self.element.get("subPage")

    @property
    def selected(self): return self.element.get("selected") == "selected"

    @property
    def index(self):
        """Position among its image's pages (0, or 1 for the right half)."""
        return self.image.pages.index(self)

    @property
    def number(self):
        """Position in the project's page order, from 0 (ScanAnalyze's TotalIndex,
        which indexes numbering.txt's pages)."""
        return self.project._page_number[self.id]

    @property
    def identifier(self):
        return f"{self.file.path}|{self.sub_page}"

    def _stage(self, cls, name):
        el = self.project.filter_element(name, self.id)
        return None if el is None else cls(el, self.project, self)

    @property
    def deskew(self): return self._stage(Deskew, "deskew")

    @property
    def select_content(self): return self._stage(SelectContent, "select-content")

    @property
    def page_layout(self): return self._stage(PageLayout, "page-layout")

    @property
    def output(self): return self._stage(Output, "output")

    @property
    def width_mm(self):
        """Output width: the content's width plus the left and right margins."""
        return (self.select_content.content_rect.width / self.image.pixels_per_mm.width
                + self.page_layout.hard_margins_mm.left + self.page_layout.hard_margins_mm.right)

    @property
    def height_mm(self):
        return (self.select_content.content_rect.height / self.image.pixels_per_mm.height
                + self.page_layout.hard_margins_mm.top + self.page_layout.hard_margins_mm.bottom)

    def __repr__(self):
        f = self.file
        return f"<Page id={self.id} {f.name if f else '?'} {self.sub_page}>"


def page_transform(pg):
    """The 2x3 affine (a numpy array) from the raw image's pixels to the output page's, as
    ScanTailor made it: its deskew (page outline to rotated page outline), then the content
    box moved to where the output put it (or, with no content, the crop area).

    The same arithmetic as booktext's surya_pdf.page_transform, which drew boxes from the raw
    scan onto the output pages first. drawn_view.py uses its inverse to put a drawing back
    onto the raw scans. numpy is needed only here."""
    import numpy as np

    def pts(el):
        return [(float(q.get("x")), float(q.get("y"))) for q in el.findall("point")]
    po = pts(pg.deskew.params.find("dependencies/page-outline"))
    rpo = pts(pg.select_content.params.find("dependencies/rotated-page-outline"))
    src = np.array([po[0], po[1], po[3]])
    dst = np.array([rpo[0], rpo[1], rpo[3]])
    M = np.linalg.solve(np.column_stack([src, np.ones(3)]), dst).T
    vc = pg.select_content.content_rect
    oc = pg.output.element.find("output-params/image/content-rect")
    if vc.width and vc.height:
        M[:, 2] += (float(oc.get("x")) - vc.x, float(oc.get("y")) - vc.y)
    else:
        # no content: the crop area, the image's outline as the output has it
        ca = pts(pg.output.element.find("output-params/image/crop-area"))
        M[:, 2] += np.array(ca[0]) - M @ np.array([po[0][0], po[0][1], 1.0])
    return M


class _Stage(_View):
    """A filter's entry for one page: <filters>/<name>/<page id=...>."""
    __slots__ = ("page",)

    def __init__(self, element, project, page):
        super().__init__(element, project)
        self.page = page

    @property
    def params(self): return _required(self.element, "params")

    @property
    def mode(self): return self.params.get("mode")

    def __repr__(self):
        return f"<{type(self).__name__} of {self.page!r}>"


class Deskew(_Stage):
    """<filters>/<deskew>/<page>."""

    @property
    def angle(self): return float(self.params.get("angle"))

    @property
    def page_outline(self):
        """The page's outline in image coordinates (a right-hand page is offset to the right)."""
        return _points(self.params.find("dependencies/page-outline"))

    def page_outline_zeroed(self):
        """page_outline moved left to x = 0."""
        pts = self.page_outline
        left = min(x for x, _ in pts)
        return [(x - left, y) for x, y in pts]

    def transform(self):
        """ScanAnalyze's deskew.GetImageRotation(): (m11, m12, m21, m22, dx, dy), turning
        page_outline_zeroed() coordinates by the angle and moving them to x, y >= 0, i.e.
        into select-content's coordinates: x' = x*m11 + y*m21 + dx, y' = x*m12 + y*m22 + dy."""
        a = math.radians(self.angle)
        c, s = math.cos(a), math.sin(a)
        turned = [(x * c - y * s, x * s + y * c) for x, y in self.page_outline_zeroed()]
        return (c, s, -s, c, -min(x for x, _ in turned), -min(y for _, y in turned))

    def rotated_page_outline(self):
        """page_outline_zeroed() put through transform(); matches what ScanTailor stores as
        select-content's rotated-page-outline."""
        m11, m12, m21, m22, dx, dy = self.transform()
        return [(x * m11 + y * m21 + dx, x * m12 + y * m22 + dy) for x, y in self.page_outline_zeroed()]


class SelectContent(_Stage):
    """<filters>/<select-content>/<page>."""

    @property
    def content_rect(self): return _rect(_required(self.params, "content-rect"))

    @content_rect.setter
    def content_rect(self, r):
        """As ScanAnalyze does: content-size-mm is scaled along with the rectangle;
        page-layout's contentSizeMM is left for ScanTailor to update."""
        r = Rect(*r)
        old, mm = self.content_rect, self.content_size_mm
        if mm is not None and old.width and old.height:
            el = self.params.find("content-size-mm")
            el.set("width", _fmt(r.width / old.width * mm.width))
            el.set("height", _fmt(r.height / old.height * mm.height))
        el = self.params.find("content-rect")
        for k in ("x", "y", "width", "height"):
            el.set(k, _fmt(getattr(r, k)))

    @property
    def content_size_mm(self): return _size(self.params.find("content-size-mm"))

    @property
    def page_rect(self):
        """ScanTailor Advanced only; None in classic projects."""
        return _rect(self.params.find("page-rect"))

    @property
    def rotated_page_outline(self):
        """The deskewed page's outline, in the coordinates of content_rect."""
        return _points(self.params.find("dependencies/rotated-page-outline"))

    @property
    def is_empty(self):
        r = self.content_rect
        return r.width == 0 or r.height == 0


class PageLayout(_Stage):
    """<filters>/<page-layout>/<page>."""

    @property
    def hard_margins_mm(self):
        el = _required(self.params, "hardMarginsMM")
        return Margins(*(_num(el.get(k)) for k in Margins._fields))

    @hard_margins_mm.setter
    def hard_margins_mm(self, m):
        el = _required(self.params, "hardMarginsMM")
        for k, v in zip(Margins._fields, m):
            el.set(k, _fmt(v))

    @property
    def content_size_mm(self): return _size(self.params.find("contentSizeMM"))

    @property
    def _alignment(self): return _required(self.params, "alignment")

    @property
    def alignment_vert(self): return self._alignment.get("vert")

    @alignment_vert.setter
    def alignment_vert(self, v):
        if v not in ALIGN_VERT:
            raise ValueError(f"vertical alignment must be one of {ALIGN_VERT}")
        self._alignment.set("vert", v)

    @property
    def alignment_hor(self): return self._alignment.get("hor")

    @alignment_hor.setter
    def alignment_hor(self, v):
        if v not in ALIGN_HOR:
            raise ValueError(f"horizontal alignment must be one of {ALIGN_HOR}")
        self._alignment.set("hor", v)

    @property
    def match_size(self):
        """"Match size with other pages": alignment's null="0" (ScanAnalyze's `ignore` is
        the opposite)."""
        return self._alignment.get("null") != "1"

    @match_size.setter
    def match_size(self, v): self._alignment.set("null", "0" if v else "1")


class Output(_Stage):
    """<filters>/<output>/<page>."""

    @property
    def _bw(self): return _required(self.params, "color-params/bw")

    @property
    def threshold_adj(self): return int(self._bw.get("thresholdAdj"))

    @threshold_adj.setter
    def threshold_adj(self, v): self._bw.set("thresholdAdj", str(int(v)))

    @property
    def color_mode(self): return _required(self.params, "color-params").get("colorMode")

    @property
    def dpi(self):
        el = _required(self.params, "dpi")
        return Size(float(el.get("horizontal")), float(el.get("vertical")))


# ---------------------------------------------------------------- the project

# filter name -> the child that holds one entry, keyed by its id
_FILTERS = {"fix-orientation": "image", "page-split": "image", "deskew": "page",
            "select-content": "page", "page-layout": "page", "output": "page"}


class Project:
    """A .ScanTailor file in memory. Project.load(path) to read one."""

    def __init__(self, root, path=None, *, prefix=b"", suffix=b"", crlf=False, tight=False):
        if root.tag != "project":
            raise ValueError(f"not a ScanTailor project: the root element is <{root.tag}>")
        self.root = root
        self.path = Path(path) if path else None
        # how the file was written, so that save() writes it the same way: what came
        # before and after <project>, CRLF line ends, and empty elements as <x/> (what
        # .NET writes, i.e. files ScanAnalyze saved) rather than <x /> (ScanTailor)
        self._prefix, self._suffix, self._crlf, self._tight = prefix, suffix, crlf, tight
        self.reindex()

    @classmethod
    def load(cls, path):
        data = Path(path).read_bytes()
        start, end = data.find(b"<"), data.rfind(b">") + 1
        root = ET.fromstring(data)
        return cls(root, path, prefix=data[:start], suffix=data[end:], crlf=b"\r\n" in data,
                   tight=b"/>" in data and b" />" not in data)

    def reindex(self):
        """Rebuild the lookups; call after adding or removing elements by hand."""
        r = self.root
        self.directories = {e.get("id"): Directory(e, self) for e in r.findall("directories/directory")}
        self.files = [File(e, self) for e in r.findall("files/file")]
        self.images = [Image(e, self) for e in r.findall("images/image")]
        self.pages = [Page(e, self) for e in r.findall("pages/page")]
        self._file_by_id = {f.id: f for f in self.files}
        self._image_by_id = {i.id: i for i in self.images}
        self._image_by_file = {i.file_id: i for i in self.images}
        self._pages_by_image = defaultdict(list)
        for p in self.pages:
            self._pages_by_image[p.image_id].append(p)
        self._page_by_id = {p.id: p for p in self.pages}
        self._page_number = {p.id: n for n, p in enumerate(self.pages)}
        self._filters = {name: {e.get("id"): e for e in r.findall(f"filters/{name}/{child}")}
                         for name, child in _FILTERS.items()}

    # --- attributes of <project>
    @property
    def output_directory(self): return self.root.get("outputDirectory")

    @property
    def layout_direction(self): return self.root.get("layoutDirection")

    @property
    def version(self): return self.root.get("version")

    # --- lookups
    def page(self, page_id):
        return self._page_by_id.get(page_id)

    def file(self, name):
        """The first file of that name (names can repeat across directories)."""
        return next((f for f in self.files if f.name == name), None)

    def filter_element(self, filter_name, entry_id):
        """A filter's entry for an image (fix-orientation, page-split) or a page (deskew,
        select-content, page-layout, output), or None if ScanTailor hasn't made one."""
        return self._filters[filter_name].get(entry_id)

    def localize(self, stored):
        """A directory the project names, as a path here. A relative one (ScanTailor 1.2 and
        later write "." and "out") is relative to the project file's folder. An absolute one
        that doesn't exist here (a project made on Windows, say) is moved, if it is at or
        under the project's original folder (taken to be outputDirectory's parent), to the
        same place under this file's folder."""
        windows_absolute = len(stored) > 1 and stored[1] == ":"
        p = Path(stored)
        if not p.is_absolute() and not windows_absolute:
            return Path(os.path.normpath(self.path.absolute().parent / p)) if self.path else p
        if p.is_dir() or self.path is None:
            return p
        out = (self.output_directory or "").replace("\\", "/").rstrip("/")
        base = out.rsplit("/", 1)[0] if "/" in out else ""
        s = stored.replace("\\", "/").rstrip("/")
        if base and (s.lower() == base.lower() or s.lower().startswith(base.lower() + "/")):
            return self.path.absolute().parent.joinpath(*[x for x in s[len(base):].split("/") if x])
        return p

    # --- saving
    def save(self, path=None, backup=None):
        """Write the project (to its own file by default). With backup="Kind", first keep
        the file as it was in "<name> (Kind).ScanTailor", unless that already exists: what
        ScanAnalyze's SaveWithBackup does."""
        path = Path(path or self.path)
        if backup and path.exists():
            b = path.with_name(f"{path.stem} ({backup}){path.suffix}")
            if not b.exists():
                shutil.copy2(path, b)
        text = ET.tostring(self.root, encoding="unicode")
        if self._tight:
            text = text.replace(" />", "/>")   # ElementTree escapes > in values, so only tags end so
        if self._crlf:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(self._prefix + text.encode("utf-8") + self._suffix)
        os.replace(tmp, path)
        self.path = path

    def __repr__(self):
        return f"<Project {self.path or '(unsaved)'}: {len(self.files)} files, {len(self.pages)} pages>"


# ---------------------------------------------------------------- processing

# scantailor-batch: ScanTailor Advanced's stages without its GUI (src/batch/main.cpp in the
# scantailor-advanced submodule beside this file). SCANTAILOR_BATCH overrides where it is.
BATCH = Path(os.environ.get("SCANTAILOR_BATCH", Path(__file__).resolve().parent
                            / "scantailor-advanced/build/scantailor-batch"))
STAGES = ("fix-orientation", "page-split", "deskew", "select-content", "page-layout")


def process(project, through="page-layout", backup=None, jobs=None, quiet=False):
    """Run ScanTailor's stages on every page of a project, up to and including `through`
    (page-layout is the GUI's Margins), as its batch processing does, and write the result
    over the project; returns it loaded. Pages keep the settings they have; others get the
    default profile's, so nothing already set is lost. With backup="Kind", the project as
    it was is first kept as "<name> (Kind).ScanTailor", unless that exists; note that a
    glob like *.ScanTailor then matches it too. Version 3 projects (your Windows
    ScanTailor's, and create_initial's) are written back as version 4, with paths relative
    to the project file, as ScanTailor 1.2 does; that ScanTailor can't open version 3."""
    project = Path(project)
    if through not in STAGES:
        raise ValueError(f"through must be one of {STAGES}")
    if not BATCH.exists():
        raise FileNotFoundError(f"{BATCH} isn't built; build it with "
                                f"cmake --build {BATCH.parent} --target scantailor-batch")
    if not project.exists():
        raise FileNotFoundError(project)
    if backup:
        b = project.with_name(f"{project.stem} ({backup}){project.suffix}")
        if not b.exists():
            shutil.copy2(project, b)
    cmd = [str(BATCH), "--through", through]
    if jobs:
        cmd += ["--jobs", str(jobs)]
    if quiet:
        cmd.append("-q")
    subprocess.run(cmd + [str(project)], check=True)
    return Project.load(project)


# ---------------------------------------------------------------- version 3 -> 4

def to_version_4(path, freeze_deskew=True):
    """Convert a version 3 project (the Windows ScanTailor's, ScanAnalyze's) to version 4,
    which ScanTailor 1.2 and later require, keeping its results. See README.md. In place,
    after copying it to "<name>.ScanTailor.v3" (a name *.ScanTailor doesn't match), unless
    that exists. Returns a list of warnings.

    - version="4";
    - each image folder and the output folder as they are here (Project.localize), and the
      output folder made if missing, since the GUI otherwise asks to relink;
    - deskew's oblique (shear) correction set to manual where the page has no setting,
      which 1.2 would otherwise read as automatic;
    - with freeze_deskew, auto deskew angles set to manual, as 1.2 recomputes them (some
      come out slightly different) and then re-detects automatic content boxes, which
      would lose ScanAnalyze's adjustments."""
    path = Path(path)
    p = Project.load(path)
    if p.version != "3":
        raise ValueError(f"{path} is version {p.version or '(none)'}, not 3")
    warnings = []
    p.root.set("version", "4")
    for d in p.directories.values():
        local = d.local_path
        if not local.is_dir():
            warnings.append(f"image folder {d.path} isn't here (looked for {local}); "
                            f"ScanTailor will ask to relink it")
        d.element.set("path", local.as_posix())
    if p.output_directory:
        out = p.localize(p.output_directory)
        p.root.set("outputDirectory", out.as_posix())
        if out.parent.is_dir():
            out.mkdir(exist_ok=True)
    for e in p.root.iterfind("filters/deskew/page/params"):
        if e.get("oblique-mode") is None and e.get("autoOblique") is None:
            e.set("oblique-mode", "manual")
        if freeze_deskew and e.get("mode") != "manual":
            e.set("mode", "manual")
    backup = path.with_name(path.name + ".v3")
    if not backup.exists():
        shutil.copy2(path, backup)
    p.save()
    return warnings


# What ScanTailor Advanced 1.2 (version 4) writes that the 2018 Windows build and ScanAnalyze
# (version 3) never did: attributes, by the element that carries them, and elements.
V4_ATTRIBUTES = {"deskew": ("algoContentBased",), "params": ("oblique", "oblique-mode"),
                 "dewarping-options": ("postDeskewAngle",),
                 "color-or-grayscale": ("fillOutsidePageBox", "wienerCoef", "wienerWinSize")}
V4_ELEMENTS = ("automask", "source_file")


def to_version_3(path, windows_folder=None, out=None):
    """Convert a version 4 project (ScanTailor Advanced 1.2's) to version 3, which the Windows
    ScanTailor (the 2018 build) and ScanAnalyze read, keeping its results. Writes `out` (by
    default the project in place, after copying it to "<name>.v4", unless that exists).
    Returns a list of warnings.

    - version="3";
    - the image folders and the output folder as absolute Windows paths under windows_folder
      (by default D:\\scan\\<the project's folder's name>), written as the Windows build writes
      them: "D:/scan/AS" for an image folder, "D:\\scan\\AS\\out" for the output folder; a
      folder that isn't the project's folder or under it is left as it is, with a warning;
    - alignment "center" written "hcenter" and "vcenter" (ScanAnalyze's Enum.Parse refuses
      "center"); "auto" and "original", which version 3 lacks, the same, with a warning;
    - what only version 4 writes dropped (V4_ATTRIBUTES, V4_ELEMENTS): deskew's oblique (shear)
      correction among them, with a warning for each page whose shear wasn't 0, since the
      Windows build will draw it unsheared."""
    path = Path(path)
    p = Project.load(path)
    if p.version != "4":
        raise ValueError(f"{path} is version {p.version or '(none)'}, not 4")
    home = path.resolve().parent
    win = (windows_folder or rf"D:\scan\{home.name}").replace("/", "\\").rstrip("\\")
    warnings = []

    def windows(stored):
        local = Path(stored) if Path(stored).is_absolute() else home / stored
        try:
            rel = local.resolve().relative_to(home)
        except ValueError:
            warnings.append(f"folder {stored} isn't under the project's folder; left as it is")
            return None
        return win if str(rel) == "." else win + "\\" + str(rel).replace("/", "\\")

    p.root.set("version", "3")
    for d in p.directories.values():
        w = windows(d.path)
        if w is not None:
            d.element.set("path", w.replace("\\", "/"))
    if p.output_directory:
        w = windows(p.output_directory)
        if w is not None:
            p.root.set("outputDirectory", w)
    for el in p.root.iter("alignment"):
        for key, centre in (("hor", "hcenter"), ("vert", "vcenter")):
            v = el.get(key)
            if v in ("auto", "original"):
                warnings.append(f"alignment {key}={v} has no version 3 form; written {centre}")
                el.set(key, centre)
            elif v == "center":
                el.set(key, centre)
    sheared = 0
    for e in p.root.iterfind("filters/deskew/page/params"):
        try:
            sheared += abs(float(e.get("oblique", "0"))) > 1e-9
        except ValueError:
            pass
    if sheared:
        warnings.append(f"{sheared} pages had shear correction (oblique), which version 3 lacks: "
                        f"the Windows build will draw them unsheared")
    for el in p.root.iter():
        for a in V4_ATTRIBUTES.get(el.tag, ()):
            el.attrib.pop(a, None)
        for child in [c for c in el if c.tag in V4_ELEMENTS]:
            el.remove(child)
    if out is None:
        backup = path.with_name(path.name + ".v4")
        if not backup.exists():
            shutil.copy2(path, backup)
        out = path
    p.save(out)
    return warnings


# ---------------------------------------------------------------- the initial project

def image_size_dpi(path):
    """(width, height, dpi) of an image, as ScanAnalyze's GetImageMetadata gets them through
    GDI+: dpi is the horizontal resolution, rounded to 0.1, and 96 when the file has none."""
    with open(path, "rb") as f:
        head = f.read(8)
        if head == b"\x89PNG\r\n\x1a\n":
            width = height = None
            dpi = 96.0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                length, kind = struct.unpack(">I4s", hdr)
                if kind == b"IHDR":
                    width, height = struct.unpack(">II", f.read(8))
                    f.seek(length - 8 + 4, 1)
                elif kind == b"pHYs":
                    x, _, unit = struct.unpack(">IIB", f.read(9))
                    if unit == 1:
                        dpi = x * 0.0254
                    f.seek(4, 1)
                elif kind == b"IDAT":
                    break
                else:
                    f.seek(length + 4, 1)
            if width is None:
                raise ValueError(f"{path}: PNG without IHDR")
            return width, height, round(dpi, 1)
    from PIL import Image as PILImage           # other formats: rare here
    with PILImage.open(path) as im:
        return im.width, im.height, round(float(im.info.get("dpi", (96, 96))[0]), 1)


def _attr(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def create_initial(folder, pattern="*.png", overwrite=False):
    """Write <folder>/<folder's name>.ScanTailor listing the images in folder that match
    pattern, as `ScanAnalyze.exe *.png` does (ScanTailor.SaveInitialFile): one directory,
    then a file, an image and a single page for each image, with ids counted up from 1,
    and empty filters for ScanTailor to fill in. The text is ScanAnalyze's to the byte
    (CRLF line ends, no XML declaration, no final newline); the folder's path is this
    machine's. Returns the file's path.

    Unlike ScanAnalyze, it won't overwrite an existing project unless overwrite=True."""
    folder = Path(folder).resolve()
    names = sorted((e.name for e in folder.iterdir()
                    if e.is_file() and fnmatch(e.name.lower(), pattern.lower())),
                   key=lambda n: (n.upper(), n))          # NTFS's order, as GetFiles gives
    if not names:
        raise FileNotFoundError(f"no files matching {pattern!r} in {folder}")
    target = folder / f"{folder.name}{EXTENSION}"
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists")

    ids = iter(range(1, 1_000_000_000))
    dir_id = next(ids)
    files, images, pages = [], [], []
    for name in names:
        width, height, dpi = image_size_dpi(folder / name)
        file_id, image_id, page_id = next(ids), next(ids), next(ids)
        files.append(f'\n    <file dirId="{dir_id}" id="{file_id}" name="{_attr(name)}"/>')
        images.append(f'\n    <image subPages="1" fileImage="0" fileId="{file_id}" id="{image_id}">'
                      f'\n      <size width="{width}" height="{height}"/>'
                      f'\n      <dpi vertical="{_fmt(dpi)}" horizontal="{_fmt(dpi)}"/>'
                      f'\n    </image>')
        pages.append(f'\n    <page imageId="{image_id}" subPage="single" id="{page_id}"/>')
    join = lambda parts: "".join(parts).lstrip()
    text = (f'<project outputDirectory="{_attr(folder / "out")}" layoutDirection="LTR" version="3">\n'
            f'  <directories>\n'
            f'    <directory path="{_attr(folder.as_posix())}" id="{dir_id}"/>\n'
            f'  </directories>\n'
            f'  <files>\n    {join(files)}\n  </files>\n'
            f'  <images>\n    {join(images)}\n  </images>\n'
            f'  <pages>\n    {join(pages)}\n  </pages>\n'
            f'  <filters>\n'
            f'    <fix-orientation/>\n'
            f'    <page-split defaultLayoutType="single-uncut" />\n'
            f'    <deskew/>\n'
            f'    <select-content/>\n'
            f'    <page-layout/>\n'
            f'  </filters>\n'
            f'</project>')
    target.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return target


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="ScanTailor project tools (see README.md).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("to-v4", help="convert version 3 projects to version 4, keeping their results")
    c.add_argument("--auto-deskew", action="store_true",
                   help="leave automatic deskew angles automatic (ScanTailor 1.2 may then "
                        "recompute some, and re-detect those pages' content boxes)")
    c.add_argument("projects", nargs="+", metavar="PROJECT")
    c3 = sub.add_parser("to-v3", help="convert version 4 projects to version 3, for the Windows "
                        "ScanTailor and ScanAnalyze, keeping their results")
    c3.add_argument("--windows-folder", help=r"the project's folder on Windows (default D:\scan\<its folder's name>)")
    c3.add_argument("-o", "--out", help="write the converted project here (one project only) "
                    "instead of over it")
    c3.add_argument("projects", nargs="+", metavar="PROJECT")
    a = ap.parse_args()
    if a.cmd == "to-v3" and a.out and len(a.projects) > 1:
        ap.error("-o takes one project")
    status = 0
    for project in a.projects:
        try:
            if a.cmd == "to-v4":
                for w in to_version_4(project, freeze_deskew=not a.auto_deskew):
                    print(f"{project}: WARNING: {w}")
                print(f"{project}: converted to version 4 (the original is {Path(project).name}.v3)")
            else:
                for w in to_version_3(project, a.windows_folder, a.out):
                    print(f"{project}: WARNING: {w}")
                print(f"{project}: converted to version 3" + (f", written to {a.out}" if a.out
                      else f" (the original is {Path(project).name}.v4)"))
        except (OSError, ValueError, ET.ParseError) as e:
            print(f"{project}: {e}", file=sys.stderr)
            status = 1
    sys.exit(status)
