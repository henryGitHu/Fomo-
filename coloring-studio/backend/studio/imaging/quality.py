"""Objective image measurements for coloring-page quality assurance.

All metrics are computed from pixels (numpy) — no AI judgement is involved
here. AI/subjective evaluation, when enabled, is stored separately.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

MIN_PPI = 300


def load_gray(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        return np.asarray(im.convert("L"), dtype=np.uint8)


def otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_all = np.dot(np.arange(256), hist)
    w_b = np.cumsum(hist)
    sum_b = np.cumsum(hist * np.arange(256))
    w_f = total - w_b
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (sum_b * w_f - (sum_all - sum_b) * w_b) ** 2 / (w_b * w_f)
    between[~np.isfinite(between)] = 0
    t = int(np.argmax(between))
    return t if 30 < t < 230 else 128


def laplacian_var(gray: np.ndarray) -> float:
    g = gray.astype(np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def dhash(gray: np.ndarray, size: int = 16) -> str:
    im = Image.fromarray(gray).resize((size + 1, size), Image.Resampling.LANCZOS)
    a = np.asarray(im, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).ravel()
    return "".join("1" if b else "0" for b in bits)


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def ink_mask(gray: np.ndarray) -> np.ndarray:
    return gray < 128


def stroke_width(ink: np.ndarray) -> float:
    """Average stroke width ≈ 2 * ink area / boundary length."""
    if ink.sum() == 0:
        return 0.0
    edges = (ink[:, 1:] != ink[:, :-1]).sum() + (ink[1:, :] != ink[:-1, :]).sum()
    return float(2 * ink.sum() / max(edges, 1))


def placement_ppi(px_w: int, px_h: int, box_w_in: float, box_h_in: float) -> float:
    """PPI when the image is scaled to fit (contain) the placement box."""
    scale_in = min(box_w_in / px_w, box_h_in / px_h)  # inches per pixel
    return 1.0 / scale_in


def analyze(data: bytes, *, box_in: tuple[float, float] | None = None, native_px: tuple[int, int] | None = None,
            anchor: dict | None = None, other_hashes: dict[str, str] | None = None) -> dict:
    gray = load_gray(data)
    h, w = gray.shape
    ink = ink_mask(gray)
    ink_cov = float(ink.mean())
    mid = float(((gray > 40) & (gray < 215)).mean())
    border = max(4, int(min(w, h) * 0.02))
    frame = np.concatenate([gray[:border].ravel(), gray[-border:].ravel(), gray[:, :border].ravel(),
                            gray[:, -border:].ravel()])
    border_white = float((frame > 235).mean())
    nonink = gray[~ink]
    bg_white = float((nonink > 235).mean()) if nonink.size else 0.0
    inkvals = gray[ink]
    contrast = float(nonink.mean() - inkvals.mean()) if (inkvals.size and nonink.size) else 0.0
    sharp = laplacian_var(gray)
    sw = stroke_width(ink)
    ys, xs = np.nonzero(ink)
    if xs.size:
        bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        cx, cy = float(xs.mean() / w), float(ys.mean() / h)
    else:
        bbox, cx, cy = None, 0.5, 0.5
    edge_density = float(((ink[:, 1:] != ink[:, :-1]).mean() + (ink[1:, :] != ink[:-1, :]).mean()) / 2)
    touch = bool(bbox and (bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= w - 2 or bbox[3] >= h - 2))
    hsh = dhash(gray)

    metrics = {"width": w, "height": h, "ink_coverage": round(ink_cov, 4), "midtone_fraction": round(mid, 4),
               "border_white_fraction": round(border_white, 4), "background_white_fraction": round(bg_white, 4),
               "contrast": round(contrast, 1), "sharpness_laplacian_var": round(sharp, 1),
               "stroke_width_px": round(sw, 2), "edge_density": round(edge_density, 5), "ink_bbox": bbox,
               "ink_centroid": [round(cx, 3), round(cy, 3)], "touches_edge": touch, "dhash": hsh}

    checks: list[dict] = []

    def check(cid, ok, msg, mandatory=False, warn_only=False, value=None):
        checks.append({"id": cid, "status": "pass" if ok else ("warn" if warn_only else "fail"),
                       "mandatory": mandatory, "message": msg, "value": value})

    if box_in:
        ppi = placement_ppi(w, h, *box_in)
        metrics["effective_ppi"] = round(ppi, 1)
        check("print_resolution", ppi >= MIN_PPI, f"Effective {ppi:.0f} PPI at print size (target ≥ {MIN_PPI})",
              mandatory=True, value=round(ppi, 1))
        if native_px:
            nppi = placement_ppi(native_px[0], native_px[1], *box_in)
            metrics["native_ppi"] = round(nppi, 1)
            metrics["upscale_factor"] = round(w / native_px[0], 3)
            check("native_resolution", nppi >= 200,
                  f"Generated at {nppi:.0f} PPI before upscaling (x{w / native_px[0]:.2f})", warn_only=True,
                  value=round(nppi, 1))
    check("white_background", bg_white >= 0.97 and border_white >= 0.97,
          f"{bg_white * 100:.1f}% of background is white; border {border_white * 100:.1f}%", mandatory=True)
    check("no_grayscale", mid <= 0.02, f"{mid * 100:.2f}% mid-tone pixels (target ≤ 2%)", mandatory=True)
    check("contrast", contrast >= 150, f"Tonal range {contrast:.0f}", mandatory=False)
    check("line_presence", 0.01 <= ink_cov <= 0.35, f"Ink coverage {ink_cov * 100:.1f}% (1–35%)", mandatory=True)
    check("open_coloring_space", ink_cov <= 0.25, f"{(1 - ink_cov) * 100:.0f}% open space", warn_only=True)
    check("stroke_weight", sw >= 3.0, f"Average stroke ≈ {sw:.1f}px", warn_only=True)
    check("not_cropped", not touch, "Artwork stays inside the image edges" if not touch else
          "Artwork touches the image edge (possible cropping)", warn_only=True)
    check("sharpness", sharp >= 500, f"Edge sharpness {sharp:.0f}", warn_only=True)

    if other_hashes:
        dup = [pid for pid, oh in other_hashes.items() if oh and hamming(hsh, oh) <= 12]
        metrics["near_duplicates"] = dup
        check("unique", not dup, "No near-duplicate pages" if not dup else f"Looks like {len(dup)} other page(s)",
              mandatory=True)

    # Scores (0-100) — objective, derived from the measurements above.
    line_clarity = max(0.0, 100 - mid * 2500) * 0.6 + min(100.0, sharp / 50) * 0.4
    centered = 1 - min(1.0, (abs(cx - 0.5) + abs(cy - 0.5)) * 1.5)
    composition = 100 * (0.6 * centered + 0.4 * (0 if touch else 1))
    ideal = 1 - min(1.0, abs(ink_cov - 0.10) / 0.15)
    coloring = 100 * (0.5 * ideal + 0.3 * min(1.0, sw / 6) + 0.2 * min(1.0, bg_white))
    scores = {"line_clarity": round(line_clarity, 1), "composition": round(composition, 1),
              "coloring_suitability": round(coloring, 1)}
    if anchor:
        scores["style_consistency"] = style_consistency(metrics, anchor)
        check("style_consistency", scores["style_consistency"] >= 60,
              f"Style match {scores['style_consistency']:.0f}/100 vs book style anchor", warn_only=True)
    metrics["scores"] = scores
    metrics["checks"] = checks
    metrics["passed"] = all(c["status"] != "fail" for c in checks if c["mandatory"])
    metrics["text_detection"] = "not performed (no OCR engine configured)"
    return metrics


def style_consistency(m: dict, anchor: dict) -> float:
    """Compare stroke weight, ink coverage and detail density with the anchor (0-100)."""
    def rel(a, b):
        if not b:
            return 0.0
        return min(1.0, abs(a - b) / b)
    sw = rel(m["stroke_width_px"] * 1000 / max(m["width"], 1),
             anchor.get("stroke_width_px", 1) * 1000 / max(anchor.get("width", 1), 1))
    cov = rel(m["ink_coverage"], anchor.get("ink_coverage", 0.1))
    det = rel(m["edge_density"], anchor.get("edge_density", 0.01))
    return round(100 * (1 - (0.45 * sw + 0.3 * cov + 0.25 * det)), 1)


def anchor_from(metrics_list: list[dict]) -> dict:
    keys = ["stroke_width_px", "ink_coverage", "edge_density", "width"]
    return {k: float(np.mean([m[k] for m in metrics_list])) for k in keys} if metrics_list else {}
