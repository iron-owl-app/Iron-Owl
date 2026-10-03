"""Make every Iron Owl icon from the one source logo (stdlib only).

    python tools/brand/make_icons.py                  # rebuild all icons from packaging/brand/iron-owl.svg
    python tools/brand/make_icons.py --from new.svg   # clean a new logo into packaging/brand first

Rasterizing uses Microsoft Edge in headless mode (already on every Windows 11 PC): it draws each
SVG into a canvas at the exact target size and hands back PNG bytes. Python then writes the PNG,
ICO and BMP files itself, re-encoding every PNG from its pixels (IHDR, IDAT, IEND only), so no
file can carry text metadata; it checks that at the end. Nothing is downloaded and no Python
packages are needed.

Outputs (paths relative to the repo root):
    packaging/brand/iron-owl.svg              cleaned source (no <metadata>; with --from)
    packaging/brand/iron-owl.ico              Windows icon: 16, 24, 32, 48, 64, 128, 256
    packaging/brand/wizard-image.bmp          installer welcome panel, 164x314 (24-bit)
    packaging/brand/wizard-small.bmp          installer page corner, 55x58 (24-bit)
    frontend/public/favicon.svg               the source logo
    frontend/public/favicon.ico               16, 32, 48
    frontend/public/apple-touch-icon.png      180, full-bleed (iOS rounds the corners itself)
    frontend/public/iron-owl-192.png / -512   rounded tile, transparent corners (purpose "any")
    frontend/public/iron-owl-maskable-192.png / -512   full-bleed, owl inside the 80% safe zone
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BRAND = REPO / "packaging" / "brand"
PUBLIC = REPO / "frontend" / "public"
SOURCE = BRAND / "iron-owl.svg"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

AMBER = "#F2A30F"  # the tile color in the source logo
WHITE = "#FFFFFF"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
FAVICON_SIZES = [16, 32, 48]


# ---------------------------------------------------------------- SVG variants

def clean_svg(text: str) -> str:
    """Drop <metadata> (a C2PA content credential), comments, title/desc and the c2pa namespace."""
    text = re.sub(r"<\?xml.*?\?>|<!DOCTYPE[^>]*>", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<metadata\b.*?</metadata>|<metadata\b[^>]*/>", "", text, flags=re.S)
    text = re.sub(r"<(title|desc)\b.*?</\1>", "", text, flags=re.S)
    text = re.sub(r'\s+xmlns:c2pa="[^"]*"', "", text)
    text = text.replace("></rect>", "/>").replace("></path>", "/>").replace("></circle>", "/>")
    text = text.replace("></ellipse>", "/>")
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines) + "\n"


def split_source(svg: str) -> tuple[str, str]:
    """(tile rect element, owl drawing) from the cleaned source (512x512 viewBox)."""
    m = re.search(r'<svg[^>]*>\s*(<rect[^>]*/>)(.*)</svg>', svg, flags=re.S)
    if not m or 'viewBox="0 0 512 512"' not in svg:
        sys.exit("The source SVG must be a 512x512 viewBox whose first element is the tile <rect>.")
    return m.group(1), m.group(2).strip()


def svg_doc(body: str, w: int, h: int, vb: str = "0 0 512 512", par: str = "xMidYMid meet") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{vb}" '
            f'preserveAspectRatio="{par}">{body}</svg>')


def owl_scaled(owl: str, scale: float, cx: float = 256, cy: float = 256) -> str:
    """The owl scaled about its own visual center (256, 283), placed at (cx, cy)."""
    return f'<g transform="translate({cx} {cy}) scale({scale}) translate(-256 -283)">{owl}</g>'


def variants(svg: str) -> dict[str, tuple[str, int, int]]:
    tile, owl = split_source(svg)
    rounded = tile + owl
    full_bleed = f'<rect width="512" height="512" fill="{AMBER}"/>'
    jobs: dict[str, tuple[str, int, int]] = {}
    # 16 and 24 px: the owl drawn larger and without the two tiny eye highlights and the beak,
    # which only blur into gray at that size. Everything else is the logo as drawn.
    small_owl = re.sub(r'<circle[^>]*r="11"[^>]*/>', "", owl)
    small_owl = re.sub(r'<path fill="#F2A30F"[^>]*/>', "", small_owl)
    tiny = tile.replace('rx="116"', 'rx="96"') + owl_scaled(small_owl, 1.16, 256, 262)
    for s in sorted(set(ICO_SIZES + FAVICON_SIZES + [192, 512])):
        jobs[f"tile-{s}"] = (svg_doc(tiny if s <= 24 else rounded, s, s), s, s)
    # Maskable: the launcher may crop to a circle of 80% diameter; the owl stays inside it.
    for s in (192, 512):
        jobs[f"maskable-{s}"] = (svg_doc(full_bleed + owl_scaled(owl, 0.80), s, s), s, s)
    # Apple touch: no transparency (iOS fills it black) and iOS adds its own corner rounding.
    jobs["apple-180"] = (svg_doc(full_bleed + owl_scaled(owl, 0.88), 180, 180), 180, 180)
    # Installer welcome panel 164x314: amber panel, the owl in the upper middle.
    big = f'<rect width="164" height="314" fill="{AMBER}"/>' + owl_scaled(owl, 0.34, 82, 140)
    jobs["wizard-image"] = (svg_doc(big, 164, 314, "0 0 164 314"), 164, 314)
    # Installer corner image 55x58 on the white page header: the rounded tile, 52 px.
    small = (f'<rect width="55" height="58" fill="{WHITE}"/>'
             f'<g transform="translate(1.5 3) scale({52 / 512})">{rounded}</g>')
    jobs["wizard-small"] = (svg_doc(small, 55, 58, "0 0 55 58"), 55, 58)
    return jobs


# ---------------------------------------------------------------- Edge rendering

PAGE = """<!doctype html><meta charset="utf-8"><body><pre id="out">PENDING</pre><script>
const jobs = %s;
(async () => {
  const res = {};
  for (const [name, [svg, w, h]] of Object.entries(jobs)) {
    const url = URL.createObjectURL(new Blob([svg], {type: 'image/svg+xml'}));
    const img = new Image(w, h);
    await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(name); img.src = url; });
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0, w, h);
    res[name] = c.toDataURL('image/png').split(',')[1];
  }
  document.getElementById('out').textContent = '#BEGIN#' + JSON.stringify(res) + '#END#';
})().catch(e => { document.getElementById('out').textContent = 'FAILED ' + e; });
</script>"""


def find_edge() -> str:
    for p in [os.environ.get("IRONOWL_EDGE", "")] + EDGE_CANDIDATES:
        if p and Path(p).is_file():
            return p
    sys.exit("Microsoft Edge was not found. Set IRONOWL_EDGE to msedge.exe.")


def render(jobs: dict[str, tuple[str, int, int]]) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="ironowl-icons-") as tmp:
        page = Path(tmp) / "render.html"
        page.write_text(PAGE % json.dumps(jobs), encoding="utf-8")
        cmd = [find_edge(), "--headless", "--disable-gpu", "--no-first-run", "--disable-extensions",
               f"--user-data-dir={Path(tmp) / 'profile'}", "--virtual-time-budget=15000",
               "--dump-dom", page.as_uri()]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"#BEGIN#(.*?)#END#", out, flags=re.S)
    if not m:
        sys.exit("Edge did not render the icons:\n" + out[-2000:])
    return {k: base64.b64decode(v) for k, v in json.loads(m.group(1)).items()}


# ---------------------------------------------------------------- PNG / ICO / BMP

def png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGBA or RGB, non-interlaced PNG (what Chromium's canvas writes)."""
    assert data[:8] == PNG_SIG, "not a PNG"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or ctype not in (2, 6) or interlace:
                sys.exit(f"Unexpected PNG format (depth {depth}, type {ctype}, interlace {interlace}).")
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + n
    raw, stride, prev, rows = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    px = b"".join(rows)
    if bpp == 3:
        px = b"".join(px[i:i + 3] + b"\xff" for i in range(0, len(px), 3))
    return w, h, px


def png_encode(w: int, h: int, rgba: bytes) -> bytes:
    """A minimal RGBA PNG: IHDR, one IDAT, IEND. No text, EXIF, C2PA or any other chunk."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def clean_png(data: bytes) -> bytes:
    """Re-encode Edge's PNG from its pixels, so nothing but the image itself can survive."""
    return png_encode(*png_rgba(data))


def png_chunks(data: bytes) -> list[str]:
    pos, kinds = 8, []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        kinds.append(kind.decode("latin-1"))
        pos += 12 + n
    return kinds


def assert_no_metadata(paths: list[Path]) -> None:
    """Fail loudly if any written file could carry text metadata (the export leak check scans it)."""
    for path in paths:
        data = path.read_bytes()
        pngs = []
        if path.suffix == ".png":
            pngs = [data]
        elif path.suffix == ".ico":
            count = struct.unpack("<H", data[4:6])[0]
            for k in range(count):
                size, off = struct.unpack("<II", data[6 + 16 * k + 8:6 + 16 * k + 16])
                if data[off:off + 8] == PNG_SIG:
                    pngs.append(data[off:off + size])
        elif path.suffix == ".svg":
            text = data.decode("utf-8")
            for bad in ("<metadata", "<!--", "<?xml", "<title", "<desc", "c2pa"):
                if bad in text:
                    sys.exit(f"{path.name} still contains {bad}")
        for png in pngs:
            extra = set(png_chunks(png)) - {"IHDR", "IDAT", "IEND"}
            if extra:
                sys.exit(f"{path.name} has extra PNG chunks: {sorted(extra)}")


def ico_dib(w: int, h: int, rgba: bytes) -> bytes:
    """32-bit BGRA DIB (bottom-up) plus the 1-bit AND mask, as ICO entries under 256 px expect."""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        row = rgba[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            r, g, b, a = row[x * 4:x * 4 + 4]
            xor += bytes((b, g, r, a))
    mask_stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(w):
            if rgba[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    return header + bytes(xor) + bytes(mask)


def write_ico(path: Path, pngs: dict[int, bytes]) -> None:
    sizes = sorted(pngs)
    images = []
    for s in sizes:
        if s >= 256:
            images.append(clean_png(pngs[s]))  # PNG-compressed entry, the standard for 256 px
        else:
            w, h, px = png_rgba(pngs[s])
            images.append(ico_dib(w, h, px))
    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    for img in images:
        out += img
    path.write_bytes(bytes(out))


def write_bmp24(path: Path, png: bytes) -> None:
    """24-bit bottom-up BMP (Inno Setup's wizard images); alpha is flattened onto white."""
    w, h, px = png_rgba(png)
    stride = (w * 3 + 3) & ~3
    body = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[(y * w + x) * 4:(y * w + x) * 4 + 4]
            row += bytes(((b * a + 255 * (255 - a)) // 255, (g * a + 255 * (255 - a)) // 255,
                          (r * a + 255 * (255 - a)) // 255))
        body += row + b"\0" * (stride - len(row))
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    head = struct.pack("<2sIHHI", b"BM", 14 + len(info) + len(body), 0, 0, 14 + len(info))
    path.write_bytes(head + info + bytes(body))


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", help="a new logo SVG to clean into packaging/brand first")
    args = ap.parse_args()
    BRAND.mkdir(parents=True, exist_ok=True)
    if args.src:
        SOURCE.write_text(clean_svg(Path(args.src).read_text(encoding="utf-8")), encoding="utf-8",
                          newline="\n")
    svg = SOURCE.read_text(encoding="utf-8")
    if clean_svg(svg) != svg:
        sys.exit("The source has metadata, comments or a title: run with --from to clean it.")
    shutil.copyfile(SOURCE, PUBLIC / "favicon.svg")
    r = render(variants(svg))
    write_ico(BRAND / "iron-owl.ico", {s: r[f"tile-{s}"] for s in ICO_SIZES})
    write_ico(PUBLIC / "favicon.ico", {s: r[f"tile-{s}"] for s in FAVICON_SIZES})
    write_bmp24(BRAND / "wizard-image.bmp", r["wizard-image"])
    write_bmp24(BRAND / "wizard-small.bmp", r["wizard-small"])
    (PUBLIC / "apple-touch-icon.png").write_bytes(clean_png(r["apple-180"]))
    for s in (192, 512):
        (PUBLIC / f"iron-owl-{s}.png").write_bytes(clean_png(r[f"tile-{s}"]))
        (PUBLIC / f"iron-owl-maskable-{s}.png").write_bytes(clean_png(r[f"maskable-{s}"]))
    assert_no_metadata([SOURCE, PUBLIC / "favicon.svg", PUBLIC / "favicon.ico", PUBLIC / "apple-touch-icon.png",
                        BRAND / "iron-owl.ico"]
                       + [PUBLIC / f"iron-owl{m}-{s}.png" for m in ("", "-maskable") for s in (192, 512)])
    print("Icons written to packaging/brand and frontend/public.")


if __name__ == "__main__":
    main()
