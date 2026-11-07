"""ScanStack engine: turns a stack of scanned images into a PDF.

No GUI code lives here, so it can be tested on its own.

How a page is made:
  1. Open the scan (any frame of a multi-page TIFF), turn it upright,
     flatten transparency onto white and convert colours to sRGB.
  2. Apply the page's own rotation, then pick portrait or landscape.
  3. Optionally clean up the paper background (greyish paper becomes white,
     which looks better and compresses much smaller).
  4. Pick colour, greyscale or black & white (Auto picks colour or grey per page).
  5. Resize to exactly what is needed on the page at the chosen DPI.
  6. Encode: JPEG for colour/grey, CCITT Group 4 (the fax format) for black &
     white, which is usually 10-20x smaller than JPEG for text pages.

The PDF is written one page at a time, so a 300-page stack never needs to sit
in memory at once. The image is placed on the page by the PDF itself, so blank
margins cost nothing in file size.
"""

from __future__ import annotations

import io
import os
import re
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Iterator

from PIL import Image, ImageChops, ImageCms, ImageOps, features

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except Exception:  # pragma: no cover - only if the library is missing
    HAVE_HEIF = False

Image.MAX_IMAGE_PIXELS = 400_000_000  # big flatbed scans are fine; still stops absurd files

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".bmp", ".gif",
    ".webp", ".heic", ".heif", ".avif", ".jp2", ".pcx", ".ppm", ".pgm", ".pbm",
}

PAPER_SIZES_IN = {  # width x height in inches, portrait
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "a4": (8.27, 11.69),
}
PAPER_LABELS = {"letter": "US Letter (8.5 × 11 in)", "legal": "US Legal (8.5 × 14 in)", "a4": "A4"}

HAVE_G4 = features.check("libtiff")


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    paper: str = "letter"          # letter | legal | a4
    orientation: str = "auto"      # auto | portrait | landscape
    sizing: str = "fit"            # fit | fill | actual
    margin_in: float = 0.0         # blank border on every side, inches
    color: str = "auto"            # auto | color | gray | bw
    bw_threshold: int = 0          # 1-254, or 0 to work it out per page
    dpi: int = 200                 # pixels per inch stored in the PDF
    quality: int = 70              # JPEG quality, 1-95
    clean_background: bool = True  # turn greyish/yellowed paper white


PRESETS = {
    # name: (dpi, quality)
    "Smallest file": (150, 55),
    "Balanced": (200, 70),
    "High quality": (300, 85),
}


@dataclass
class PageSource:
    """One page in the stack: a file (and frame, for multi-page TIFFs)."""

    path: str
    frame: int = 0
    rotation: int = 0  # extra clockwise rotation chosen by the user: 0/90/180/270
    n_frames: int = 1

    @property
    def name(self) -> str:
        base = os.path.basename(self.path)
        return f"{base} (page {self.frame + 1})" if self.n_frames > 1 else base


# --------------------------------------------------------------------------- sorting


_NUM = re.compile(r"(\d+)")


def natural_key(path: str):
    """Sort 'scan 2' before 'scan 10', ignoring case."""
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in _NUM.split(name)]


def is_image_file(path: str) -> bool:
    base = os.path.basename(path)
    return (
        os.path.isfile(path)
        and not base.startswith(".")
        and os.path.splitext(base)[1].lower() in IMAGE_EXTENSIONS
    )


def expand_paths(paths: Iterable[str]) -> list[str]:
    """Files stay in the order given; each folder's images are added in natural order."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            found = []
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=natural_key)
                found.extend(os.path.join(root, f) for f in files)
            out.extend(sorted((f for f in found if is_image_file(f)), key=natural_key))
        elif is_image_file(p):
            out.append(p)
    return out


def sources_for(path: str) -> list[PageSource]:
    """One PageSource per frame (multi-page TIFFs become several pages)."""
    try:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1) if (im.format or "").upper() == "TIFF" else 1
    except Exception:
        n = 1  # still list it; it will show as unreadable
    return [PageSource(path, i, 0, n) for i in range(n)]


def interleave_duplex(pages: list) -> list:
    """For single-sided feeders: scan all fronts, flip the stack, scan all backs.

    The backs come out in reverse order, so: F1 F2 F3 B3 B2 B1 -> F1 B1 F2 B2 F3 B3.
    With an odd count the extra page is treated as a front.
    """
    half = (len(pages) + 1) // 2
    fronts, backs = pages[:half], list(reversed(pages[half:]))
    out = []
    for i, f in enumerate(fronts):
        out.append(f)
        if i < len(backs):
            out.append(backs[i])
    return out


# --------------------------------------------------------------------------- loading


_SRGB = ImageCms.createProfile("sRGB")


def _to_srgb(im: Image.Image) -> Image.Image:
    icc = im.info.get("icc_profile")
    if not icc or im.mode not in ("RGB", "RGBA", "CMYK", "L", "LA"):
        return im
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = (ImageCms.getProfileDescription(src) or "").lower()
        if "srgb" in desc and im.mode in ("RGB", "RGBA"):
            return im
        out_mode = "RGBA" if im.mode in ("RGBA", "LA") else "RGB"
        if im.mode in ("L", "LA"):
            return im  # grey profiles: leave the numbers alone
        return ImageCms.profileToProfile(im, src, _SRGB, outputMode=out_mode)
    except Exception:
        return im


def open_page(src: PageSource, max_side: int | None = None) -> Image.Image:
    """Open a page as 'RGB' or 'L', upright, on white, ready for layout.

    max_side lets previews and thumbnails load quickly (JPEG draft mode).
    """
    with Image.open(src.path) as im:
        if src.frame:
            im.seek(src.frame)
        dpi = im.info.get("dpi")
        if max_side and im.format == "JPEG":
            im.draft(im.mode if im.mode in ("RGB", "L") else "RGB", (max_side, max_side))
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode == "CMYK" or (im.mode in ("RGB", "RGBA") and im.info.get("icc_profile")):
            im = _to_srgb(im)
        # Normalise awkward modes.
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            im = im.point(lambda v: v / 256).convert("L")
        if im.mode == "P":
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        if im.mode in ("RGBA", "LA", "PA"):
            bg = Image.new("RGB", im.size, "white")
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        if im.mode == "1":
            im = im.convert("L")
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if dpi:
            im.info["dpi"] = dpi
    if src.rotation:
        dpi = im.info.get("dpi")
        im = im.rotate(-src.rotation, expand=True)
        if dpi:
            im.info["dpi"] = (dpi[1], dpi[0]) if src.rotation in (90, 270) else dpi
    if max_side and max(im.size) > max_side:
        dpi = im.info.get("dpi")
        scale = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        if dpi:
            im.info["dpi"] = (dpi[0] * scale, dpi[1] * scale)
    return im


# --------------------------------------------------------------------------- analysis


def looks_grey(im: Image.Image) -> bool:
    """True if a page has no meaningful colour (scanner tint and old paper don't count)."""
    if im.mode == "L":
        return True
    small = im.copy()
    small.thumbnail((300, 300))
    r, g, b = small.split()
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    chroma = ImageChops.subtract(hi, lo).histogram()
    total = small.width * small.height
    colourful = sum(chroma[40:])
    return colourful / total < 0.006


def otsu_threshold(grey: Image.Image) -> int:
    """Classic automatic threshold between ink and paper."""
    hist = grey.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = sum_b = 0.0
    best, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, best_t = between, t
    return max(40, min(230, best_t))


def background_levels(im: Image.Image) -> tuple[int, int] | None:
    """Find (black point, paper white) if the page has a clear paper colour.

    Turning greyish or yellowed paper white makes pages look clean when
    printed or viewed, and the flat white usually compresses a little better.

    Photos and pages without a dominant bright background return None and are
    left alone, so this never wrecks a picture.
    """
    small = im.convert("L")
    small.thumbnail((400, 400))
    hist = small.histogram()
    total = sum(hist)
    # Strongest bright band (paper) between 140 and 255.
    best_peak, best_mass = None, 0
    for v in range(140, 256):
        mass = sum(hist[max(0, v - 6): v + 7])
        if mass > best_mass:
            best_peak, best_mass = v, mass
    if best_peak is None or best_mass / total < 0.30:
        return None
    white = max(150, best_peak - 8)
    if white >= 250:
        return None  # paper is already white
    # Only the paper is lifted to white. Stretching the dark end as well
    # looks punchier but makes JPEG pages bigger, so ink is left as scanned.
    return 0, white


def _apply_levels(im: Image.Image, black: int, white: int) -> Image.Image:
    span = max(1, white - black)
    lut = [0 if v <= black else 255 if v >= white else round((v - black) * 255 / span) for v in range(256)]
    return im.point(lut * len(im.getbands()))


# --------------------------------------------------------------------------- layout


@dataclass
class PageLayout:
    page_w_pt: float
    page_h_pt: float
    box: tuple[float, float, float, float]  # x, y, w, h in points (PDF origin bottom-left)
    pixel_size: tuple[int, int]             # pixels to store for the placed image
    crop: tuple[int, int, int, int] | None  # crop of the source (fill mode)


def plan_layout(img_w: int, img_h: int, img_dpi, s: Settings) -> PageLayout:
    pw, ph = PAPER_SIZES_IN.get(s.paper, PAPER_SIZES_IN["letter"])
    landscape_img = img_w > img_h
    if s.orientation == "landscape" or (s.orientation == "auto" and landscape_img):
        pw, ph = ph, pw
    m = max(0.0, min(s.margin_in, min(pw, ph) / 3))
    bw_in, bh_in = pw - 2 * m, ph - 2 * m

    crop = None
    if s.sizing == "fill":
        scale = max(bw_in / img_w, bh_in / img_h)  # inches per source pixel
        vis_w, vis_h = bw_in / scale, bh_in / scale
        x0 = round((img_w - vis_w) / 2)
        y0 = round((img_h - vis_h) / 2)
        crop = (x0, y0, x0 + round(vis_w), y0 + round(vis_h))
        w_in, h_in = bw_in, bh_in
    else:
        scale = min(bw_in / img_w, bh_in / img_h)
        if s.sizing == "actual" and img_dpi:
            try:
                dx, dy = float(img_dpi[0]), float(img_dpi[1])
            except Exception:
                dx = dy = 0
            if dx >= 50 and dy >= 50:
                w_in, h_in = img_w / dx, img_h / dy
                if w_in <= bw_in + 0.01 and h_in <= bh_in + 0.01:
                    scale = None
        if scale is not None:
            w_in, h_in = img_w * scale, img_h * scale

    # Centre on the page.
    x_in = (pw - w_in) / 2
    y_in = (ph - h_in) / 2
    src_w = crop[2] - crop[0] if crop else img_w
    src_h = crop[3] - crop[1] if crop else img_h
    # Never store more pixels than the scan has: that only makes the file bigger.
    want_w, want_h = w_in * s.dpi, h_in * s.dpi
    f = min(1.0, src_w / want_w, src_h / want_h)
    px_w = max(1, round(want_w * f))
    px_h = max(1, round(want_h * f))
    return PageLayout(pw * 72, ph * 72, (x_in * 72, y_in * 72, w_in * 72, h_in * 72), (px_w, px_h), crop)


@dataclass
class RenderedPage:
    layout: PageLayout
    image: Image.Image   # 'RGB', 'L' or '1', already at its stored size
    kind: str            # color | gray | bw
    threshold: int | None = None


def render_page(src: PageSource, s: Settings, preview_side: int | None = None) -> RenderedPage:
    """Everything except encoding. preview_side makes a quick low-res version."""
    im = open_page(src, max_side=preview_side)
    dpi = im.info.get("dpi")  # open_page scales this along with any preview shrink
    layout = plan_layout(im.width, im.height, dpi, s)

    kind = s.color
    if kind == "auto":
        kind = "gray" if looks_grey(im) else "color"
    if kind in ("gray", "bw") and im.mode != "L":
        im = im.convert("L")

    if s.clean_background and kind != "bw":
        lv = background_levels(im)
        if lv and im.mode == "RGB":
            # Per channel, so cream or yellowed paper also turns neutral white.
            bands = []
            for band in im.split():
                blv = background_levels(band)
                bands.append(_apply_levels(band, *(blv or lv)))
            im = Image.merge("RGB", bands)
        elif lv:
            im = _apply_levels(im, *lv)

    if layout.crop:
        im = im.crop(layout.crop)
    target = layout.pixel_size
    if preview_side:
        f = min(1.0, preview_side / max(target))
        target = (max(1, round(target[0] * f)), max(1, round(target[1] * f)))
    if im.size != target:
        im = im.resize(target, Image.LANCZOS, reducing_gap=3.0)

    thr = None
    if kind == "bw":
        thr = s.bw_threshold if 0 < s.bw_threshold < 255 else otsu_threshold(im)
        im = im.point(lambda v, t=thr: 255 if v > t else 0, mode="1")
    return RenderedPage(layout, im, kind, thr)


# --------------------------------------------------------------------------- encoding


@dataclass
class EncodedPage:
    layout: PageLayout
    width: int
    height: int
    data: bytes
    pdf_filter: str            # DCTDecode | CCITTFaxDecode | FlateDecode
    colorspace: str            # DeviceRGB | DeviceGray
    bits: int
    decode_parms: str = ""
    kind: str = "color"
    name: str = ""


def _g4_bytes(im: Image.Image) -> bytes:
    """Raw CCITT Group 4 data for a 1-bit image, taken from a single-strip TIFF."""
    buf = io.BytesIO()
    im.save(buf, "TIFF", compression="group4", strip_size=((im.width + 7) // 8) * im.height)
    buf.seek(0)
    with Image.open(buf) as t:
        offsets = t.tag_v2[273]
        counts = t.tag_v2[279]
    if isinstance(offsets, int):
        offsets, counts = (offsets,), (counts,)
    if len(offsets) != 1:
        raise ValueError("expected a single TIFF strip")
    raw = buf.getvalue()
    return raw[offsets[0]: offsets[0] + counts[0]]


def encode_page(rp: RenderedPage, s: Settings, name: str = "") -> EncodedPage:
    im = rp.image
    w, h = im.size
    if rp.kind == "bw":
        if HAVE_G4:
            data = _g4_bytes(im)
            parms = f"<< /K -1 /Columns {w} /Rows {h} /BlackIs1 true >>"
            return EncodedPage(rp.layout, w, h, data, "CCITTFaxDecode", "DeviceGray", 1, parms, "bw", name)
        # Fallback: packed bits + zlib (still small, just not as small).
        data = zlib.compress(im.tobytes(), 9)  # mode "1": 1 = white, as DeviceGray expects
        return EncodedPage(rp.layout, w, h, data, "FlateDecode", "DeviceGray", 1, "", "bw", name)
    buf = io.BytesIO()
    q = max(1, min(95, int(s.quality)))
    opts = dict(quality=q, optimize=True)
    if im.mode == "RGB":
        opts["subsampling"] = "4:2:0" if q < 90 else "4:4:4"
    im.save(buf, "JPEG", **opts)
    cs = "DeviceRGB" if im.mode == "RGB" else "DeviceGray"
    return EncodedPage(rp.layout, w, h, buf.getvalue(), "DCTDecode", cs, 8, "", rp.kind, name)


def make_page(src: PageSource, s: Settings) -> EncodedPage:
    return encode_page(render_page(src, s), s, src.name)


# --------------------------------------------------------------------------- PDF writer


def _pdf_str(text: str) -> str:
    """A PDF text string (UTF-16 with BOM handles any characters)."""
    data = b"\xfe\xff" + text.encode("utf-16-be")
    return "<" + data.hex() + ">"


class PdfStreamWriter:
    """Minimal, standards-compliant PDF writer that streams page by page."""

    def __init__(self, fp, title: str = "", producer: str = "ScanStack"):
        self.fp = fp
        self.offsets: dict[int, int] = {}
        self.page_ids: list[int] = []
        self.next_id = 3  # 1 = catalog, 2 = pages tree
        self.title = title
        self.producer = producer
        self._write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def _write(self, b: bytes):
        self.fp.write(b)

    def _alloc(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    def _obj(self, oid: int, body: bytes, stream: bytes | None = None):
        self.offsets[oid] = self.fp.tell()
        self._write(f"{oid} 0 obj\n".encode())
        self._write(body)
        if stream is not None:
            self._write(b"\nstream\n")
            self._write(stream)
            self._write(b"\nendstream")
        self._write(b"\nendobj\n")

    def add_page(self, ep: EncodedPage):
        img_id, content_id, page_id = self._alloc(), self._alloc(), self._alloc()
        parms = f" /DecodeParms {ep.decode_parms}" if ep.decode_parms else ""
        img_dict = (
            f"<< /Type /XObject /Subtype /Image /Width {ep.width} /Height {ep.height}"
            f" /ColorSpace /{ep.colorspace} /BitsPerComponent {ep.bits}"
            f" /Filter /{ep.pdf_filter}{parms} /Length {len(ep.data)} >>"
        ).encode()
        self._obj(img_id, img_dict, ep.data)

        x, y, w, h = ep.layout.box
        content = f"q {w:.3f} 0 0 {h:.3f} {x:.3f} {y:.3f} cm /Im0 Do Q".encode()
        self._obj(content_id, f"<< /Length {len(content)} >>".encode(), content)

        pw, ph = ep.layout.page_w_pt, ep.layout.page_h_pt
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}]"
            f" /Resources << /XObject << /Im0 {img_id} 0 R >> /ProcSet [/PDF /ImageB /ImageC] >>"
            f" /Contents {content_id} 0 R >>"
        ).encode()
        self._obj(page_id, page)
        self.page_ids.append(page_id)

    def close(self):
        kids = " ".join(f"{p} 0 R" for p in self.page_ids)
        self._obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.page_ids)} >>".encode())
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        info_id = self._alloc()
        info = f"<< /Producer {_pdf_str(self.producer)}"
        if self.title:
            info += f" /Title {_pdf_str(self.title)}"
        info += " >>"
        self._obj(info_id, info.encode())

        xref_at = self.fp.tell()
        count = self.next_id
        lines = [f"xref\n0 {count}\n", "0000000000 65535 f \n"]
        for oid in range(1, count):
            lines.append(f"{self.offsets[oid]:010d} 00000 n \n")
        self._write("".join(lines).encode())
        self._write(
            f"trailer\n<< /Size {count} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
        )


# --------------------------------------------------------------------------- whole job


class Cancelled(Exception):
    pass


@dataclass
class BuildResult:
    path: str
    pages: int
    size: int
    kinds: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)  # (name, reason)


def _ordered_parallel(func, items, workers: int) -> Iterator:
    """Like map(), in order, but with at most `workers * 2` pages in flight."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = []
        it = iter(items)
        for item in it:
            pending.append(ex.submit(func, item))
            if len(pending) >= workers * 2:
                yield pending.pop(0)
        while pending:
            yield pending.pop(0)


def build_pdf(
    pages: list[PageSource],
    out_path: str,
    s: Settings,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    workers: int | None = None,
) -> BuildResult:
    """Write the PDF to a temp file and move it into place only when it's complete."""
    if not pages:
        raise ValueError("No pages to save.")
    workers = workers or max(1, min(8, (os.cpu_count() or 4) - 1))
    tmp = out_path + ".partial"
    kinds: dict[str, int] = {}
    skipped = []
    n = 0
    try:
        with open(tmp, "wb") as fp:
            title = os.path.splitext(os.path.basename(out_path))[0]
            w = PdfStreamWriter(fp, title=title)

            def job(src):
                try:
                    return src, make_page(src, s), None
                except Exception as e:  # unreadable file: skip it, report later
                    return src, None, e

            for i, fut in enumerate(_ordered_parallel(job, pages, workers)):
                if cancelled and cancelled():
                    raise Cancelled()
                src, ep, err = fut.result()
                if err is not None:
                    skipped.append((src.name, str(err) or type(err).__name__))
                else:
                    w.add_page(ep)
                    kinds[ep.kind] = kinds.get(ep.kind, 0) + 1
                    n += 1
                if progress:
                    progress(i + 1, len(pages), src.name)
            if n == 0:
                raise ValueError("None of the images could be opened.")
            w.close()
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return BuildResult(out_path, n, os.path.getsize(out_path), kinds, skipped)


def estimate_size(pages: list[PageSource], s: Settings, sample: int = 4,
                  cancelled: Callable[[], bool] | None = None) -> int | None:
    """Encode a few evenly spaced pages and extrapolate the PDF size."""
    if not pages:
        return None
    k = min(sample, len(pages))
    idx = sorted({round(i * (len(pages) - 1) / max(1, k - 1)) for i in range(k)})
    total = got = 0
    for i in idx:
        if cancelled and cancelled():
            return None
        try:
            total += len(make_page(pages[i], s).data) + 600
            got += 1
        except Exception:
            continue
    if not got:
        return None
    return round(total / got * len(pages)) + 1000


# --------------------------------------------------------------------------- self-test


def self_test(tmpdir: str) -> list[str]:
    """Make a few synthetic scans, build PDFs in every mode, check them. Returns problems."""
    from PIL import ImageDraw

    problems = []
    paths = []
    for i, (mode, size) in enumerate([("RGB", (2550, 3300)), ("L", (1700, 2200)), ("RGB", (3300, 2550))]):
        im = Image.new(mode, size, (236, 232, 222) if mode == "RGB" else 230)
        d = ImageDraw.Draw(im)
        for y in range(200, size[1] - 200, 90):
            d.rectangle((200, y, size[0] - 300, y + 30), fill=(30, 30, 30) if mode == "RGB" else 30)
        if i == 2:
            d.ellipse((900, 600, 1900, 1600), fill=(200, 40, 40))
        p = os.path.join(tmpdir, f"scan {i + 1}.jpg" if i != 1 else "scan 10.png")
        im.save(p, dpi=(300, 300))
        paths.append(p)
    tif = os.path.join(tmpdir, "scan 3.tif")
    frames = [Image.new("L", (850, 1100), 255), Image.new("L", (850, 1100), 200)]
    frames[0].save(tif, save_all=True, append_images=frames[1:], compression="tiff_lzw")
    if HAVE_HEIF:
        hp = os.path.join(tmpdir, "scan 4.heic")
        Image.new("RGB", (800, 1000), (250, 250, 250)).save(hp, quality=80)

    files = expand_paths([tmpdir])
    names = [os.path.basename(f) for f in files]
    if names[:2] != ["scan 1.jpg", "scan 3.jpg"] or names[-1] != "scan 10.png":
        problems.append(f"sort order wrong: {names}")
    srcs = [ps for f in files for ps in sources_for(f)]
    want_pages = len(files) + 1  # the TIFF has two pages
    if len(srcs) != want_pages:
        problems.append(f"expected {want_pages} pages, got {len(srcs)}")

    for colour in ("auto", "color", "gray", "bw"):
        for sizing in ("fit", "fill", "actual"):
            out = os.path.join(tmpdir, f"out-{colour}-{sizing}.pdf")
            try:
                r = build_pdf(srcs, out, replace(Settings(), color=colour, sizing=sizing))
                with open(out, "rb") as f:
                    data = f.read()
                if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
                    problems.append(f"{colour}/{sizing}: not a valid PDF")
                if r.pages != len(srcs):
                    problems.append(f"{colour}/{sizing}: {r.pages} pages")
            except Exception as e:
                problems.append(f"{colour}/{sizing}: {e!r}")
    if not HAVE_G4:
        problems.append("Group 4 (libtiff) missing: black & white PDFs will be larger")
    return problems
