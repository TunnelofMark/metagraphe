"""Re-crop figure/table regions from the PDF's embedded scan at native resolution.

Display-space bbox (pt, page as shown) -> unrotated page space -> scan pixel coordinates.
- JBIG2 / 1-bit pages: saved as 1-bit PNG (lossless, original pixels).
- JPEG pages: lossless `jpegtran -crop` (no re-encoding; the crop origin snaps to the 8/16 px
  MCU grid, so it may start up to 15 px further up/left). Falls back to PIL q=95 if jpegtran is missing.
- Rotated pages: the crop is rotated to match the displayed orientation.
"""
import io
import shutil
import subprocess
import pymupdf
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

JPEGTRAN = shutil.which("jpegtran")


class NativePage:
    def __init__(self, doc, pno, fallback_dpi=300):
        self.page = doc[pno - 1]
        imgs = self.page.get_images(full=True)
        self.doc = doc
        self.mb = self.page.mediabox          # unrotated page size
        self._pil = None
        self.fallback_render = False
        if len(imgs) == 1:
            self.xref, _, self.W, self.H, self.bpc, _, _, _, self.filter, *_ = imgs[0]
        else:
            # Multi-image or vector-only page: gracefully fall back to high-DPI page render
            # instead of failing the entire book.
            self.fallback_render = True
            self.xref = None
            self.filter = "RenderFallback"
            self.bpc = 8
            self._fallback_dpi = fallback_dpi
            pix = self.page.get_pixmap(dpi=fallback_dpi, colorspace=pymupdf.csRGB, alpha=False)
            self.W, self.H = pix.width, pix.height
            self._pil = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def raw_jpeg(self):
        return self.doc.xref_stream_raw(self.xref)

    def pil(self):
        if self._pil is None:
            if self.filter == "DCTDecode":
                self._pil = Image.open(io.BytesIO(self.raw_jpeg()))
            else:
                pix = pymupdf.Pixmap(self.doc, self.xref)
                im = Image.open(io.BytesIO(pix.tobytes("png")))
                self._pil = im.convert("1") if self.bpc == 1 else im
        return self._pil

    def to_pixels(self, bbox, margin_pt=0.0):
        """Display-space bbox -> (x0, y0, x1, y1) in scan pixels (clamped)."""
        r = pymupdf.Rect(bbox) + (-margin_pt, -margin_pt, margin_pt, margin_pt)
        if self.fallback_render:
            # Fallback pixmap is already rendered in display space (rotation applied)
            r.normalize()
            sx, sy = self.W / self.page.rect.width, self.H / self.page.rect.height
        else:
            r = r * self.page.derotation_matrix           # to unrotated space (y down from top)
            r.normalize()
            sx, sy = self.W / self.mb.width, self.H / self.mb.height
        x0, y0 = max(0, int(r.x0 * sx)), max(0, int(r.y0 * sy))
        x1, y1 = min(self.W, int(round(r.x1 * sx))), min(self.H, int(round(r.y1 * sy)))
        return x0, y0, x1, y1

    def dpi(self):
        if self.fallback_render:
            return self._fallback_dpi
        return round(self.W / (self.mb.width / 72))

    def crop(self, bbox, out_stem, margin_pt=2.0):
        """Write the crop; returns dict(path, w, h, dpi, lossless)."""
        x0, y0, x1, y1 = self.to_pixels(bbox, margin_pt)
        rot = self.page.rotation
        if not self.fallback_render and self.filter == "DCTDecode" and JPEGTRAN and rot == 0:
            import os
            path = f"{out_stem}.jpg"
            src = f"{out_stem}.__src.jpg"
            try:
                open(src, "wb").write(self.raw_jpeg())
                subprocess.run([JPEGTRAN, "-copy", "none", "-crop", f"{x1 - x0}x{y1 - y0}+{x0}+{y0}",
                                "-outfile", path, src], check=True)
                w, h = Image.open(path).size
                return {"path": path, "w": w, "h": h, "dpi": self.dpi(), "lossless": True}
            except (subprocess.CalledProcessError, OSError):
                # Truncated / corrupt embedded JPEG: fall back cleanly to 300 dpi PyMuPDF render crop
                if os.path.exists(path):
                    os.remove(path)
                self.fallback_render = True
                self._fallback_dpi = 300
                self._im = None
                pix = self.page.get_pixmap(dpi=self._fallback_dpi)
                self.W, self.H = pix.width, pix.height
                self._im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                x0, y0, x1, y1 = self.to_pixels(bbox, margin_pt)
            finally:
                if os.path.exists(src):
                    os.remove(src)
        im = self.pil().crop((x0, y0, x1, y1))
        if rot and not self.fallback_render:
            im = im.rotate(-rot, expand=True)       # PIL rotates counter-clockwise
        if self.filter == "DCTDecode":
            path = f"{out_stem}.jpg"
            im.save(path, quality=95, subsampling=0)
            lossless = False
        else:
            path = f"{out_stem}.png"
            im.save(path, optimize=True)
            lossless = not self.fallback_render
        info = {"path": path, "w": im.width, "h": im.height, "dpi": self.dpi(), "lossless": lossless}
        if self.fallback_render:
            info["fallback_render"] = True
        return info
