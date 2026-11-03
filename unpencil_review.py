#!/usr/bin/env python3
"""Review unpencil.py's work in a browser: page through the pages it changed, the pencil it erased
shown in red (on or off), the original scan at a key, and draw boxes around what it got wrong.

usage: unpencil_review.py SRC_DIR CLEAN_DIR [--boxes FILE] [--port N]

SRC_DIR: the scans (i_*.png); CLEAN_DIR: unpencil.py's OUT_DIR (its pages, masks/ and
unpencil.tsv). The boxes are saved as you draw them to FILE (default SRC_DIR/unpencil-review.json),
in each page image's own pixels:
    {"pages": {"i_0054_0.png": [{"id", "x0", "y0", "x1", "y1", "kind", "note", "created"}]}}
The red overlay is made once into CLEAN_DIR/overlay/. Served on 127.0.0.1 only.

Keys: Left/Right or PgUp/PgDn page; R red on/off; O original scan (with its pencil) on/off;
+/- zoom, F fit width, 1 actual pixels (also Ctrl+wheel); drag on the page to draw a box;
click a box to select it, Delete removes it; Esc unselects.
"""
import argparse, csv, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Pool
from urllib.parse import unquote, urlparse

import numpy as np
from PIL import Image

RED = (210, 0, 20)


def make_overlay(job):
    """The erased pixels in red, as dark as the graphite was, on transparency."""
    src, clean, name, paper, ink = job
    out = os.path.join(clean, "overlay", name)
    if os.path.exists(out):
        return
    orig = np.asarray(Image.open(os.path.join(src, name)).convert("L")).astype(float)
    mask = np.asarray(Image.open(os.path.join(clean, "masks", name))) > 0
    rgba = np.zeros(orig.shape + (4,), np.uint8)
    rgba[mask, :3] = RED
    t = np.clip((paper - orig[mask]) / (paper - ink) * 2.0, 0.25, 1.0)
    rgba[mask, 3] = (t * 255).round().astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(out + ".part.png")
    os.replace(out + ".part.png", out)


def pages_of(clean, offset):
    """The pages unpencil.py changed, and those unpencil_fix.py changed alone (a mark the script
    missed, cleaned by hand fixes: CLEAN_DIR/unpencil-fix.log names them)."""
    fixed = set()
    log = os.path.join(clean, "unpencil-fix.log")
    if os.path.exists(log):
        fixed = {l.split("\t", 1)[0] for l in open(log) if l.strip()}
    rows = csv.DictReader(open(os.path.join(clean, "unpencil.tsv")), delimiter="\t")
    out = []
    for r in rows:
        n = r["image"]
        erased = int(r["erased_px"])
        if not erased and n in fixed and os.path.exists(os.path.join(clean, "masks", n)):
            erased = int(np.count_nonzero(np.asarray(Image.open(os.path.join(clean, "masks", n)))))
        if erased:
            out.append(dict(name=n, printed=int(n[2:6]) - offset, strokes=int(r["strokes"]),
                            erased=erased, paper=float(r["paper"]), ink=float(r["ink"])))
    return out


class App:
    def __init__(self, src, clean, boxes_path, pages):
        self.src, self.clean, self.boxes_path, self.pages = src, clean, boxes_path, pages
        self.names = {p["name"] for p in pages}
        self.lock = threading.Lock()
        if os.path.exists(boxes_path):
            # one backup of what was there when this review started
            with open(boxes_path) as f, open(boxes_path + ".bak", "w") as g:
                g.write(f.read())

    def read_boxes(self):
        if not os.path.exists(self.boxes_path):
            return {"pages": {}}
        with open(self.boxes_path) as f:
            return json.load(f)

    def write_boxes(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
            raise ValueError("expected {pages: {...}}")
        for name, boxes in data["pages"].items():
            if name not in self.names or not isinstance(boxes, list):
                raise ValueError(f"bad page {name!r}")
        data = {"pages": {k: v for k, v in data["pages"].items() if v},
                "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with self.lock:
            tmp = self.boxes_path + ".part"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.boxes_path)


def handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/pages.json":
                return self.send(200, json.dumps(app.pages).encode(), "application/json")
            if path == "/boxes":
                return self.send(200, json.dumps(app.read_boxes()).encode(), "application/json")
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "img" and parts[2] in app.names:
                kind, name = parts[1], parts[2]
                where = {"orig": app.src, "clean": app.clean,
                         "red": os.path.join(app.clean, "overlay")}.get(kind)
                if where:
                    with open(os.path.join(where, name), "rb") as f:
                        return self.send(200, f.read(), "image/png", cache=True)
            self.send(404, b"not found", "text/plain")

        def do_PUT(self):
            if urlparse(self.path).path != "/boxes":
                return self.send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", 0))
                app.write_boxes(json.loads(self.rfile.read(n)))
            except (ValueError, json.JSONDecodeError) as e:
                return self.send(400, str(e).encode(), "text/plain")
            self.send(200, b'{"ok": true}', "application/json")
    return H


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Unpencil review</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#1d1f21; --panel:#26292c; --line:#3a3e42; --fg:#e4e4e4; --dim:#9aa0a6;
        --box:#1a73e8; --sel:#f9ab00; --ok:#5bb974; }
* { box-sizing: border-box; }
body { margin:0; height:100vh; display:flex; font:13px/1.4 system-ui, sans-serif;
       background:var(--bg); color:var(--fg); }
#side { width:300px; flex:none; display:flex; flex-direction:column; background:var(--panel);
        border-right:1px solid var(--line); }
#side header { padding:10px 12px; border-bottom:1px solid var(--line); }
#side h1 { font-size:14px; margin:0 0 6px; }
.row { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:4px 0; }
button { background:#33373b; color:var(--fg); border:1px solid var(--line); border-radius:4px;
         padding:3px 8px; font:inherit; cursor:pointer; }
button:hover { background:#3d4246; }
button.on { background:#5a1a1f; border-color:#a33; }
#pages { overflow:auto; flex:1; padding:4px 0; border-bottom:1px solid var(--line); }
#pages div { padding:3px 12px; cursor:pointer; display:flex; justify-content:space-between; }
#pages div:hover { background:#2f3337; }
#pages div.cur { background:#183a5c; }
#pages .n { color:var(--dim); }
#pages .b { color:var(--sel); font-weight:600; }
#boxes { flex:1.2; overflow:auto; padding:8px 12px; }
#boxes h2 { font-size:13px; margin:0 0 6px; }
.bx { border:1px solid var(--line); border-radius:4px; padding:6px; margin-bottom:6px; }
.bx.sel { border-color:var(--sel); }
.bx select, .bx textarea { width:100%; background:#1b1d1f; color:var(--fg);
         border:1px solid var(--line); border-radius:3px; font:inherit; }
.bx textarea { height:44px; resize:vertical; margin-top:4px; }
#status { padding:6px 12px; color:var(--dim); border-top:1px solid var(--line); font-size:12px; }
#help { color:var(--dim); font-size:12px; }
#view { flex:1; overflow:auto; position:relative; background:#5f6368; cursor:crosshair; }
#stage { position:relative; margin:0; }
#stage img { position:absolute; left:0; top:0; width:100%; height:100%; pointer-events:none;
             user-select:none; }
#svg { position:absolute; left:0; top:0; width:100%; height:100%; }
rect.box { fill:rgba(26,115,232,.10); stroke:var(--box); stroke-width:2.5;
           vector-effect:non-scaling-stroke; cursor:pointer; }
rect.box.sel { stroke:var(--sel); fill:rgba(249,171,0,.12); }
rect.draft { fill:none; stroke:var(--box); stroke-width:2; stroke-dasharray:6 4;
             vector-effect:non-scaling-stroke; }
#tag { position:fixed; right:14px; top:10px; background:rgba(0,0,0,.65); padding:4px 10px;
       border-radius:4px; pointer-events:none; font-weight:600; }
</style></head>
<body>
<div id="side">
  <header>
    <h1>Unpencil review</h1>
    <div class="row">
      <button id="prev" title="Left / PgUp">&larr;</button>
      <span id="pos"></span>
      <button id="next" title="Right / PgDn">&rarr;</button>
    </div>
    <div class="row">
      <button id="red" title="R">Red (R)</button>
      <button id="orig" title="O">Original (O)</button>
    </div>
    <div class="row">
      <button id="zout" title="-">&minus;</button><button id="zin" title="+">+</button>
      <button id="fit" title="F">Fit (F)</button><button id="one" title="1">1:1 (1)</button>
      <span id="zoom" class="n"></span>
    </div>
    <div id="help">Drag on the page to box an issue. Click a box to select; Delete removes it.</div>
  </header>
  <div id="pages"></div>
  <div id="boxes"></div>
  <div id="status">loading&hellip;</div>
</div>
<div id="view"><div id="stage">
  <img id="i-clean" alt=""><img id="i-orig" alt=""><img id="i-red" alt="">
  <svg id="svg" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none"></svg>
</div></div>
<div id="tag"></div>
<script>
"use strict";
const KINDS = ["pencil left", "print erased or damaged", "other"];
const $ = id => document.getElementById(id);
const view = $("view"), stage = $("stage"), svg = $("svg");
let pages = [], data = {pages: {}}, cur = 0, zoom = 0, W = 0, H = 0;
let red = true, orig = false, sel = null, saveTimer = null;

function store(k, v) { try { localStorage.setItem("unpencil." + k, JSON.stringify(v)); } catch (e) {} }
function recall(k, d) { try { const v = localStorage.getItem("unpencil." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }

function boxesOf(name) { return data.pages[name] || (data.pages[name] = []); }
function page() { return pages[cur]; }

function status(s) { $("status").textContent = s; }
function save() {
  clearTimeout(saveTimer);
  status("saving…");
  saveTimer = setTimeout(async () => {
    try {
      const r = await fetch("/boxes", {method: "PUT", headers: {"Content-Type": "application/json"},
                                      body: JSON.stringify(data)});
      status(r.ok ? "saved " + new Date().toLocaleTimeString() : "save failed: " + await r.text());
    } catch (e) { status("save failed: " + e); }
  }, 300);
}

function setZoom(z, cx, cy) {
  // keep the image point under (cx, cy) of the view where it is
  const vr = view.getBoundingClientRect();
  if (cx === undefined) { cx = vr.width / 2; cy = vr.height / 2; }
  const ix = (view.scrollLeft + cx) / (zoom || 1), iy = (view.scrollTop + cy) / (zoom || 1);
  zoom = Math.max(0.05, Math.min(3, z));
  stage.style.width = (W * zoom) + "px"; stage.style.height = (H * zoom) + "px";
  view.scrollLeft = ix * zoom - cx; view.scrollTop = iy * zoom - cy;
  $("zoom").textContent = Math.round(zoom * 100) + "%";
  store("zoom", zoom);
}
function fit() { if (W) { setZoom((view.clientWidth - 20) / W); view.scrollLeft = 0; } }

function layers() {
  $("i-orig").style.visibility = orig ? "visible" : "hidden";
  $("i-clean").style.visibility = orig ? "hidden" : "visible";
  $("i-red").style.visibility = red ? "visible" : "hidden";
  $("red").classList.toggle("on", red); $("orig").classList.toggle("on", orig);
  $("tag").textContent = (orig ? "ORIGINAL" : "CLEANED") + (red ? " + red" : "");
  store("red", red); store("orig", orig);
}

function show(i, keepScroll) {
  cur = (i + pages.length) % pages.length; sel = null;
  const p = page(), keep = [view.scrollLeft, view.scrollTop];
  $("pos").textContent = `${cur + 1} / ${pages.length}  ·  p. ${p.printed}`;
  const img = $("i-clean");
  img.onload = () => {
    const first = !W;
    W = img.naturalWidth; H = img.naturalHeight;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    if (first && !recall("zoom", 0)) fit(); else setZoom(zoom || recall("zoom", 0.3), 0, 0);
    if (keepScroll) { view.scrollLeft = keep[0]; view.scrollTop = keep[1]; } else view.scrollTop = 0;
    draw();
  };
  img.src = "/img/clean/" + p.name;
  $("i-orig").src = "/img/orig/" + p.name;
  $("i-red").src = "/img/red/" + p.name;
  // fetch the next page's images ahead
  const n = pages[(cur + 1) % pages.length].name;
  for (const k of ["clean", "orig", "red"]) (new Image()).src = `/img/${k}/${n}`;
  store("cur", cur);
  list(); draw();
}

function list() {
  const el = $("pages"); el.innerHTML = "";
  pages.forEach((p, i) => {
    const d = document.createElement("div");
    const nb = (data.pages[p.name] || []).length;
    d.className = i === cur ? "cur" : "";
    d.innerHTML = `<span>p. ${p.printed} <span class="n">${p.name.slice(0, 6)} · ${p.strokes} stroke${p.strokes === 1 ? "" : "s"}</span></span>` +
                  (nb ? `<span class="b">${nb} box${nb === 1 ? "" : "es"}</span>` : "");
    d.onclick = () => show(i);
    el.appendChild(d);
    if (i === cur) d.scrollIntoView({block: "nearest"});
  });
}

function draw() {
  const p = page(); if (!p) return;
  svg.innerHTML = "";
  for (const b of boxesOf(p.name)) {
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", b.x0); r.setAttribute("y", b.y0);
    r.setAttribute("width", b.x1 - b.x0); r.setAttribute("height", b.y1 - b.y0);
    r.setAttribute("class", "box" + (b.id === sel ? " sel" : ""));
    r.addEventListener("mousedown", e => { e.stopPropagation(); select(b.id); });
    svg.appendChild(r);
  }
  panel();
}

function panel() {
  const p = page(), bs = boxesOf(p.name), el = $("boxes");
  el.innerHTML = `<h2>Boxes on p. ${p.printed} (${bs.length})</h2>`;
  if (!bs.length) el.innerHTML += `<div class="n" style="color:var(--dim)">None yet.</div>`;
  bs.forEach((b, k) => {
    const d = document.createElement("div");
    d.className = "bx" + (b.id === sel ? " sel" : "");
    const opts = KINDS.map(x => `<option${x === b.kind ? " selected" : ""}>${x}</option>`).join("");
    d.innerHTML = `<div class="row" style="justify-content:space-between"><b>#${k + 1}</b>
        <span><button data-a="go">show</button> <button data-a="del">delete</button></span></div>
        <select>${opts}</select><textarea placeholder="note (optional)"></textarea>`;
    const ta = d.querySelector("textarea"); ta.value = b.note || "";
    ta.oninput = () => { b.note = ta.value; save(); };
    ta.onfocus = () => { if (sel !== b.id) { sel = b.id; drawRects(); markPanel(); } };
    d.querySelector("select").onchange = e => { b.kind = e.target.value; save(); };
    d.querySelector('[data-a="del"]').onclick = () => remove(b.id);
    d.querySelector('[data-a="go"]').onclick = () => { select(b.id); reveal(b); };
    d.dataset.id = b.id;
    el.appendChild(d);
  });
}
function drawRects() {
  for (const r of svg.querySelectorAll("rect.box")) r.classList.remove("sel");
  const bs = boxesOf(page().name);
  svg.querySelectorAll("rect.box").forEach((r, k) => { if (bs[k] && bs[k].id === sel) r.classList.add("sel"); });
}
function markPanel() {
  for (const d of document.querySelectorAll(".bx")) d.classList.toggle("sel", d.dataset.id === sel);
}
function select(id) { sel = id; drawRects(); markPanel();
  const d = document.querySelector(`.bx[data-id="${id}"]`); if (d) d.scrollIntoView({block: "nearest"}); }
function reveal(b) {
  view.scrollLeft = (b.x0 + b.x1) / 2 * zoom - view.clientWidth / 2;
  view.scrollTop = (b.y0 + b.y1) / 2 * zoom - view.clientHeight / 2;
}
function remove(id) {
  const p = page(); data.pages[p.name] = boxesOf(p.name).filter(b => b.id !== id);
  if (sel === id) sel = null;
  draw(); list(); save();
}

// drawing a box
let drag = null;
function imgPt(e) {
  const r = stage.getBoundingClientRect();
  return [Math.max(0, Math.min(W, (e.clientX - r.left) / zoom)),
          Math.max(0, Math.min(H, (e.clientY - r.top) / zoom))];
}
view.addEventListener("mousedown", e => {
  if (e.button !== 0 || !W) return;
  const [x, y] = imgPt(e);
  const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  r.setAttribute("class", "draft"); svg.appendChild(r);
  drag = {x, y, r}; e.preventDefault();
});
window.addEventListener("mousemove", e => {
  if (!drag) return;
  const [x, y] = imgPt(e);
  drag.r.setAttribute("x", Math.min(x, drag.x)); drag.r.setAttribute("y", Math.min(y, drag.y));
  drag.r.setAttribute("width", Math.abs(x - drag.x)); drag.r.setAttribute("height", Math.abs(y - drag.y));
});
window.addEventListener("mouseup", e => {
  if (!drag) return;
  const [x, y] = imgPt(e), d = drag; drag = null; d.r.remove();
  if (Math.abs(x - d.x) * zoom < 6 || Math.abs(y - d.y) * zoom < 6) { sel = null; drawRects(); markPanel(); return; }
  const b = {id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
             x0: Math.round(Math.min(x, d.x)), y0: Math.round(Math.min(y, d.y)),
             x1: Math.round(Math.max(x, d.x)), y1: Math.round(Math.max(y, d.y)),
             kind: recall("kind", KINDS[0]), note: "", created: new Date().toISOString()};
  boxesOf(page().name).push(b); sel = b.id;
  draw(); list(); save();
});
document.addEventListener("change", e => { if (e.target.tagName === "SELECT") store("kind", e.target.value); });

view.addEventListener("wheel", e => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const r = view.getBoundingClientRect();
  setZoom(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX - r.left, e.clientY - r.top);
}, {passive: false});

document.addEventListener("keydown", e => {
  if (["TEXTAREA", "SELECT", "INPUT"].includes(e.target.tagName)) { if (e.key === "Escape") e.target.blur(); return; }
  const k = e.key;
  if (k === "ArrowRight" || k === "PageDown") show(cur + 1);
  else if (k === "ArrowLeft" || k === "PageUp") show(cur - 1);
  else if (k === "r" || k === "R") { red = !red; layers(); }
  else if (k === "o" || k === "O") { orig = !orig; layers(); }
  else if (k === "+" || k === "=") setZoom(zoom * 1.25);
  else if (k === "-" || k === "_") setZoom(zoom / 1.25);
  else if (k === "f" || k === "F") fit();
  else if (k === "1") setZoom(1);
  else if ((k === "Delete" || k === "Backspace") && sel) remove(sel);
  else if (k === "Escape") { sel = null; drawRects(); markPanel(); }
  else return;
  e.preventDefault();
});
$("prev").onclick = () => show(cur - 1); $("next").onclick = () => show(cur + 1);
$("red").onclick = () => { red = !red; layers(); }; $("orig").onclick = () => { orig = !orig; layers(); };
$("zin").onclick = () => setZoom(zoom * 1.25); $("zout").onclick = () => setZoom(zoom / 1.25);
$("fit").onclick = fit; $("one").onclick = () => setZoom(1);

(async () => {
  pages = await (await fetch("/pages.json")).json();
  data = await (await fetch("/boxes")).json();
  if (!data.pages) data.pages = {};
  red = recall("red", true); orig = recall("orig", false); zoom = recall("zoom", 0);
  layers();
  const n = Object.values(data.pages).reduce((s, b) => s + b.length, 0);
  status(n ? `${n} box${n === 1 ? "" : "es"} loaded` : "no boxes yet");
  show(Math.min(recall("cur", 0), pages.length - 1));
})();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("clean")
    ap.add_argument("--boxes")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--offset", type=int, default=11, help="printed page = image number - N")
    a = ap.parse_args()
    src, clean = os.path.abspath(a.src), os.path.abspath(a.clean)
    boxes = a.boxes or os.path.join(src, "unpencil-review.json")
    pages = pages_of(clean, a.offset)
    os.makedirs(os.path.join(clean, "overlay"), exist_ok=True)
    with Pool(8) as pool:
        pool.map(make_overlay, [(src, clean, p["name"], p["paper"], p["ink"]) for p in pages])
    app = App(src, clean, boxes, pages)
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), handler(app))
            break
        except OSError:
            continue
    else:
        sys.exit("no free port")
    print(f"{len(pages)} pages; boxes saved to {boxes}", flush=True)
    print(f"open http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
