"""The real H&E: open it (OpenSlide, pyvips fallback), serve Deep Zoom tiles on request, thumbnails,
and band-by-band reads for the whole-slide export.

Tiles use OpenSlide's DeepZoomGenerator with limit_bounds=False, so DZI coordinates are level-0 pixels,
the frame the registration's mm -> px affine maps into. Encoded tiles are kept in an LRU cache.
"""
import io
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
TILE, OVERLAP, FORMAT, QUALITY = 254, 1, "jpeg", 85


class VipsSlide:
    """Duck-typed stand-in for openslide.OpenSlide over pyvips, for plain TIFFs OpenSlide refuses
    (strip-organised or untiled). Pyramid pages are used when present; otherwise levels are lazy
    pyvips shrinks of level 0 (slow on huge untiled files, but correct)."""

    def __init__(self, path):
        import pyvips
        self._pv = pyvips
        base = pyvips.Image.new_from_file(str(path))
        levels = [base]
        n = base.get("n-pages") if "n-pages" in base.get_fields() else 1
        for p in range(1, n):
            im = pyvips.Image.new_from_file(str(path), page=p)
            if im.width < levels[-1].width and im.width / base.width > 0.001:
                levels.append(im)
        if len(levels) == 1:
            while max(levels[-1].width, levels[-1].height) > 2048:
                levels.append(levels[-1].shrink(4, 4))
        self._levels = [self._rgb(im) for im in levels]
        self.dimensions = (base.width, base.height)
        self.level_count = len(self._levels)
        self.level_dimensions = tuple((im.width, im.height) for im in self._levels)
        self.level_downsamples = tuple(base.width / im.width for im in self._levels)
        self.properties = {}
        self.associated_images = {}

    def _rgb(self, im):
        if im.interpretation not in ("srgb", "rgb", "b-w") and im.bands < 3:
            im = im.colourspace("srgb")
        if im.bands == 1:
            im = im.bandjoin([im, im])
        if im.bands > 3:
            im = im.extract_band(0, n=3)
        return im.cast("uchar") if im.format != "uchar" else im

    def get_best_level_for_downsample(self, d):
        best = 0
        for k, ds in enumerate(self.level_downsamples):
            if ds <= d * 1.0001:
                best = k
        return best

    def read_region(self, location, level, size):
        im = self._levels[level]
        ds = self.level_downsamples[level]
        x, y = int(location[0] / ds), int(location[1] / ds)
        w, h = size
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        x0, y0, x1, y1 = max(0, x), max(0, y), min(im.width, x + w), min(im.height, y + h)
        if x1 > x0 and y1 > y0:
            a = np.ndarray(buffer=im.crop(x0, y0, x1 - x0, y1 - y0).write_to_memory(), dtype=np.uint8,
                           shape=(y1 - y0, x1 - x0, 3))
            out.paste(Image.fromarray(a).convert("RGBA"), (x0 - x, y0 - y))
        return out

    def get_thumbnail(self, size):
        k = max([j for j, (w, h) in enumerate(self.level_dimensions) if w >= size[0] or h >= size[1]] or [0])
        im = self._levels[k]
        s = min(size[0] / im.width, size[1] / im.height, 1.0)
        im = im.resize(s) if s < 1 else im
        a = np.ndarray(buffer=im.write_to_memory(), dtype=np.uint8, shape=(im.height, im.width, 3))
        return Image.fromarray(a)

    def close(self):
        pass


def open_slide(path):
    """-> (slide, reader name). OpenSlide first (SVS, NDPI, MRXS, SCN, tiled TIFF...), then pyvips."""
    errors = []
    try:
        import openslide
        return openslide.OpenSlide(str(path)), "openslide"
    except Exception as e:                       # OpenSlideUnsupportedFormatError, missing library...
        errors.append(f"OpenSlide: {e}")
    try:
        return VipsSlide(path), "pyvips"
    except Exception as e:
        errors.append(f"pyvips: {e}")
    raise ValueError("Cannot read this file as a slide. " + " | ".join(errors))


def slide_info(slide, reader, path):
    props = getattr(slide, "properties", {}) or {}
    mpp = props.get("openslide.mpp-x")
    try:
        mpp = float(mpp) if mpp else None
    except ValueError:
        mpp = None
    return {"path": str(path), "name": Path(path).name, "reader": reader,
            "width": slide.dimensions[0], "height": slide.dimensions[1],
            "levels": slide.level_count, "mpp": mpp,
            "vendor": props.get("openslide.vendor", reader)}


def thumbnail(slide, max_size):
    """RGB uint8 array, white background, longest side <= max_size, plus (sx, sy) = level-0 px per thumb px."""
    img = slide.get_thumbnail((max_size, max_size))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    img = img.convert("RGB")
    W, H = slide.dimensions
    return np.asarray(img), (W / img.width, H / img.height)


class Tiler:
    """DZI for one opened slide, with an LRU cache of encoded tiles (thread-safe)."""

    def __init__(self, path, cache_tiles=3000):
        from openslide.deepzoom import DeepZoomGenerator
        self.path = Path(path)
        self.slide, self.reader = open_slide(path)
        self.dz = DeepZoomGenerator(self.slide, tile_size=TILE, overlap=OVERLAP, limit_bounds=False)
        self.cache = OrderedDict()
        self.cache_tiles = cache_tiles
        self.lock = threading.Lock()

    def dzi(self):
        return self.dz.get_dzi(FORMAT)

    def tile(self, level, col, row):
        key = (level, col, row)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        img = self.dz.get_tile(level, (col, row))     # ValueError for an out-of-range tile
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=QUALITY)
        data = buf.getvalue()
        with self.lock:
            self.cache[key] = data
            while len(self.cache) > self.cache_tiles:
                self.cache.popitem(last=False)
        return data

    def close(self):
        self.slide.close()


class BandSource:
    """Level-0 H&E read band by band at `scale` (output px per level-0 px), for export.slide_image.
    Uses the coarsest pyramid level that is still at least as fine as the output."""

    def __init__(self, path, scale):
        self.slide, _ = open_slide(path)
        W0, H0 = self.slide.dimensions
        self.scale = scale
        self.width, self.height = max(1, int(round(W0 * scale))), max(1, int(round(H0 * scale)))
        self.level = self.slide.get_best_level_for_downsample(1 / scale)
        self.ds = self.slide.level_downsamples[self.level]

    def band(self, y0, h):
        """Output rows y0 .. y0+h as an (h, width, 3) uint8 array."""
        f = 1 / (self.scale * self.ds)                       # level px per output px
        ly0, ly1 = y0 * f, (y0 + h) * f
        lw, lh = self.slide.level_dimensions[self.level]
        iy0 = int(np.floor(ly0))
        rh = min(lh, int(np.ceil(ly1)) + 1) - iy0
        reg = self.slide.read_region((0, int(round(iy0 * self.ds))), self.level, (lw, max(1, rh)))
        bg = Image.new("RGB", reg.size, (255, 255, 255))
        bg.paste(reg, mask=reg.split()[3])
        out = bg.resize((self.width, h), Image.Resampling.BILINEAR,
                        box=(0, ly0 - iy0, min(lw, self.width * f), min(reg.height, ly1 - iy0)))
        return np.asarray(out)

    def close(self):
        self.slide.close()
