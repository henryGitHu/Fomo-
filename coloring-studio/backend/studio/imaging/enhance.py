"""Print-preparation pipeline for coloring-page line art.

Steps: flatten -> background cleanup -> controlled upscale (Lanczos, capped) ->
unsharp mask -> Otsu binarisation -> speckle removal -> line-weight adjust ->
margin padding. The original is never modified; the caller stores the output
as a separate revision and keeps it only if detail preservation is acceptable.
"""
from __future__ import annotations

import io
import shutil

import numpy as np
from PIL import Image, ImageFilter

from .quality import load_gray, otsu_threshold

MAX_UPSCALE = 2.0


def vectorization_available() -> bool:
    return shutil.which("potrace") is not None


def to_png(gray: np.ndarray | Image.Image) -> bytes:
    im = gray if isinstance(gray, Image.Image) else Image.fromarray(gray)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def adjust_line_weight(im: Image.Image, steps: int) -> Image.Image:
    """steps > 0 thickens lines, < 0 thins them (morphological min/max filters)."""
    for _ in range(abs(steps)):
        im = im.filter(ImageFilter.MinFilter(3) if steps > 0 else ImageFilter.MaxFilter(3))
    return im


def enhance(data: bytes, *, target_px: tuple[int, int] | None = None, line_weight_steps: int = 0,
            margin_fraction: float = 0.03) -> tuple[bytes, dict]:
    gray = load_gray(data)
    orig_h, orig_w = gray.shape
    report: dict = {"steps": [], "original_size": [orig_w, orig_h]}

    # 1. background cleanup: near-white -> white
    g = gray.copy()
    g[g > 225] = 255
    report["steps"].append("background_cleanup")
    im = Image.fromarray(g)

    # 2. controlled upscale toward target, capped
    factor = 1.0
    if target_px:
        factor = min(MAX_UPSCALE, max(1.0, max(target_px[0] / orig_w, target_px[1] / orig_h)))
        if factor > 1.001:
            im = im.resize((round(orig_w * factor), round(orig_h * factor)), Image.Resampling.LANCZOS)
            report["steps"].append(f"upscale_x{factor:.2f}")
    report["upscale_factor"] = round(factor, 3)

    # 3. sharpen edges
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=3))
    report["steps"].append("unsharp_mask")

    # 4. binarize to pure black/white
    arr = np.asarray(im, dtype=np.uint8)
    t = otsu_threshold(arr)
    arr = np.where(arr < t, 0, 255).astype(np.uint8)
    report["steps"].append(f"binarize_t{t}")

    # 5. speckle removal (isolated pixels)
    im = Image.fromarray(arr).filter(ImageFilter.MedianFilter(3))
    report["steps"].append("despeckle")

    # 6. line weight
    if line_weight_steps:
        im = adjust_line_weight(im, line_weight_steps)
        report["steps"].append(f"line_weight_{line_weight_steps:+d}")

    # 7. margin: if ink touches the border, pad with white so nothing is cut at trim
    arr = np.asarray(im)
    ink = arr < 128
    ys, xs = np.nonzero(ink)
    if xs.size:
        w, h = im.size
        m = int(min(w, h) * margin_fraction)
        if xs.min() < m or ys.min() < m or xs.max() > w - m or ys.max() > h - m:
            pad = m
            canvas = Image.new("L", (w + 2 * pad, h + 2 * pad), 255)
            canvas.paste(im, (pad, pad))
            im = canvas
            report["steps"].append(f"margin_pad_{pad}px")

    out = to_png(im)
    report["detail_preservation"] = detail_preservation(gray, np.asarray(im))
    report["output_size"] = list(im.size)
    report["vectorization"] = "available" if vectorization_available() else "unavailable (potrace not installed)"
    return out, report


def detail_preservation(original: np.ndarray, enhanced: np.ndarray) -> float:
    """IoU of ink between original and enhanced (resized back to original size)."""
    eh, ew = enhanced.shape
    oh, ow = original.shape
    if (eh, ew) != (oh, ow):
        enhanced = np.asarray(Image.fromarray(enhanced).resize((ow, oh), Image.Resampling.BILINEAR))
    a = original < otsu_threshold(original)
    b = enhanced < 128
    # tolerate 1px shifts: dilate both
    def dil(x):
        return np.asarray(Image.fromarray((x * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))) > 0
    a2, b2 = dil(a), dil(b)
    inter = (a2 & b2).sum()
    union = (a2 | b2).sum()
    return round(float(inter / union) if union else 1.0, 4)


def fit_on_canvas(data: bytes, px_w: int, px_h: int) -> bytes:
    """Scale (contain) onto a white canvas of exact pixel size."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("L")
        s = min(px_w / im.width, px_h / im.height)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (px_w, px_h), 255)
        canvas.paste(im, ((px_w - im.width) // 2, (px_h - im.height) // 2))
        return to_png(canvas)
