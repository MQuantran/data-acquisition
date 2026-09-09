"""data_acquisition -- core digitization logic (no GUI).

Calibration, pixel -> data mapping, line / point extraction, export.
Kept import-clean of tkinter so it can be unit-tested headless:

    python core.py --selftest
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

try:
    import cv2
    _HAS_CV2 = True
except Exception:                                    # pragma: no cover
    _HAS_CV2 = False

try:
    from scipy import ndimage as _ndi
    _HAS_SCIPY = True
except Exception:                                    # pragma: no cover
    _HAS_SCIPY = False


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #

@dataclass
class AxisCal:
    """1-D map  data = m * pixel + b, evaluated in log10 space when log=True."""
    m: float
    b: float
    log: bool = False

    @classmethod
    def from_two(cls, px1: float, val1: float, px2: float, val2: float,
                 log: bool = False) -> "AxisCal":
        if px1 == px2:
            raise ValueError("the two calibration clicks fall on the same pixel "
                             "for this axis -- pick references further apart")
        if log and (val1 <= 0 or val2 <= 0):
            raise ValueError("log axis needs positive reference values")
        y1, y2 = (math.log10(val1), math.log10(val2)) if log else (val1, val2)
        m = (y2 - y1) / (px2 - px1)
        b = y1 - m * px1
        return cls(m=m, b=b, log=log)

    def to_data(self, px):
        v = self.m * np.asarray(px, dtype=float) + self.b
        return np.power(10.0, v) if self.log else v


@dataclass
class Calibration:
    x: AxisCal
    y: AxisCal
    refs: list = field(default_factory=list)          # [{px,py,X,Y}, ...]

    @classmethod
    def from_refs(cls, refs, x_log: bool = False, y_log: bool = False) -> "Calibration":
        """`refs` = 2 dicts {px, py, X, Y}.

        The two clicks of one reference: click 1 gives the pixel x that maps to
        data X, click 2 gives the pixel y that maps to data Y.
        """
        if len(refs) != 2:
            raise ValueError("need exactly 2 calibration references (4 clicks)")
        a, b = refs
        xc = AxisCal.from_two(a["px"], a["X"], b["px"], b["X"], log=x_log)
        yc = AxisCal.from_two(a["py"], a["Y"], b["py"], b["Y"], log=y_log)
        return cls(x=xc, y=yc, refs=[dict(r) for r in refs])

    def pixel_to_data(self, ix, iy):
        return self.x.to_data(ix), self.y.to_data(iy)

    def describe(self) -> list[str]:
        out = []
        for i, r in enumerate(self.refs, 1):
            out.append(f"ref{i}: px_x={r['px']:.1f}->X={r['X']:g}  "
                       f"px_y={r['py']:.1f}->Y={r['Y']:g}")
        out.append(f"x-axis {'log' if self.x.log else 'linear'}, "
                   f"y-axis {'log' if self.y.log else 'linear'}")
        return out


# --------------------------------------------------------------------------- #
# pixel selection
# --------------------------------------------------------------------------- #

def color_mask(rgb: np.ndarray, target, tol: float) -> np.ndarray:
    """rgb: HxWx3 uint8. target: (r,g,b). tol: Euclidean distance 0..441.

    Returns HxW bool mask of pixels within `tol` of `target`.
    """
    d = rgb[:, :, :3].astype(np.int32) - np.asarray(target, np.int32)[None, None, :]
    dist = np.sqrt((d * d).sum(axis=2))
    return dist <= float(tol)


def _clip_roi(roi, w, h):
    if roi is None:
        return 0, 0, w, h
    x0, y0, x1, y1 = roi
    x0, x1 = sorted((int(round(x0)), int(round(x1))))
    y0, y1 = sorted((int(round(y0)), int(round(y1))))
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


# --------------------------------------------------------------------------- #
# line extraction
# --------------------------------------------------------------------------- #

def extract_line(rgb, cal: Calibration, target, tol, roi=None, x_step=1,
                 min_run=1):
    """One (x, y) per pixel column inside `roi` where the colour mask fires,
    y = median row of the matching pixels. Returns (data Nx2, pixels Nx2)."""
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = _clip_roi(roi, w, h)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 2)), np.empty((0, 2))
    mask = color_mask(rgb[y0:y1, x0:x1], target, tol)
    step = max(1, int(x_step))
    px = []
    for cx in range(0, x1 - x0, step):
        rows = np.nonzero(mask[:, cx])[0]
        if rows.size >= min_run:
            px.append((x0 + cx, y0 + float(np.median(rows))))
    px = np.asarray(px, float).reshape(-1, 2)
    if len(px) == 0:
        return np.empty((0, 2)), px
    X, Y = cal.pixel_to_data(px[:, 0], px[:, 1])
    data = np.column_stack([X, Y])
    order = np.argsort(data[:, 0])
    return data[order], px[order]


# --------------------------------------------------------------------------- #
# point / marker extraction
# --------------------------------------------------------------------------- #

def _components(mask: np.ndarray):
    """Yield (cx, cy, area, submask) per connected component (8-connectivity)."""
    if _HAS_CV2:
        n, lbl, stats, cent = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            yield float(cent[i, 0]), float(cent[i, 1]), area, (lbl == i)
    elif _HAS_SCIPY:
        lbl, n = _ndi.label(mask)
        for i in range(1, n + 1):
            ys, xs = np.nonzero(lbl == i)
            yield float(xs.mean()), float(ys.mean()), int(xs.size), (lbl == i)
    else:                                             # pragma: no cover
        raise RuntimeError("point detection needs opencv or scipy installed")


def _shape_ok(submask: np.ndarray, shape: str) -> bool:
    """Soft marker-shape filter. Permissive: unknown / tiny blobs pass."""
    if shape == "any" or not _HAS_CV2:
        return True
    try:
        m = (submask.astype(np.uint8)) * 255
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return True
        c = max(cnts, key=cv2.contourArea)
        area = cv2.contourArea(c)
        per = cv2.arcLength(c, True)
        if per <= 1 or area <= 1:
            return True
        circ = 4.0 * math.pi * area / (per * per)
        v = len(cv2.approxPolyDP(c, 0.045 * per, True))
        if shape == "circle":
            return circ >= 0.68
        if shape == "triangle":
            return v == 3
        if shape in ("square", "diamond"):
            return v == 4
    except Exception:
        return True
    return True


def extract_points(rgb, cal: Calibration, target, tol, roi=None,
                   area=(6, 4000), shape="any"):
    """Colour-mask -> connected components -> centroids, filtered by pixel area
    and (softly) by marker shape. Returns (data Nx2, pixels Nx2)."""
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = _clip_roi(roi, w, h)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 2)), np.empty((0, 2))
    mask = color_mask(rgb[y0:y1, x0:x1], target, tol)
    amin, amax = area
    px = []
    for cx, cy, a, sub in _components(mask):
        if not (amin <= a <= amax):
            continue
        if not _shape_ok(sub, shape):
            continue
        px.append((x0 + cx, y0 + cy))
    px = np.asarray(px, float).reshape(-1, 2)
    if len(px) == 0:
        return np.empty((0, 2)), px
    X, Y = cal.pixel_to_data(px[:, 0], px[:, 1])
    data = np.column_stack([X, Y])
    order = np.argsort(data[:, 0])
    return data[order], px[order]


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

FORMATS = ["csv", "tsv", "json", "litdata-csv"]
EXT = {"csv": ".csv", "tsv": ".tsv", "json": ".json", "litdata-csv": ".csv"}


def build_meta(dataset, image_name, mode, shape, x_log, y_log, cal: Calibration,
               n) -> dict:
    m = {
        "dataset": dataset or "untitled",
        "source_image": image_name or "(pasted)",
        "extract_mode": mode,
        "x_axis": "log" if x_log else "linear",
        "y_axis": "log" if y_log else "linear",
        "n_points": n,
        "tool": "data_acquisition (MVP)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if mode == "points":
        m["marker_shape"] = shape
    if cal is not None:
        for i, line in enumerate(cal.describe()):
            m[f"calibration_{i}"] = line
    return m


def export_data(path: str, data: np.ndarray, fmt: str, meta: dict) -> None:
    fmt = fmt.lower()
    rows = [(float(x), float(y)) for x, y in np.asarray(data).reshape(-1, 2)]
    if fmt in ("csv", "tsv"):
        sep = "," if fmt == "csv" else "\t"
        out = [f"# {k}: {v}" for k, v in meta.items()]
        out.append(f"x{sep}y")
        out += [f"{x:.6g}{sep}{y:.6g}" for x, y in rows]
    elif fmt == "litdata-csv":
        # keys postprocessing/compare_lit_traces.py::parse_lit_csv requires
        req = {
            "source": meta.get("dataset", "untitled"),
            "trace": meta.get("dataset", "trace"),
            "role": "wet",
            "x_units": "?",
            "x_zero": "arbitrary",
        }
        head = dict(req)
        head.update(meta)                       # user/meta values win
        out = [f"# {k}: {v}" for k, v in head.items()]
        out.append("x,p_p0")
        out += [f"{x:.6g},{y:.6g}" for x, y in rows]
    elif fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta, "data": [{"x": x, "y": y} for x, y in rows]},
                      fh, indent=2)
        return
    else:
        raise ValueError(f"unknown export format: {fmt!r}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# --------------------------------------------------------------------------- #
# self-test (headless)
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from PIL import Image, ImageDraw
    W, H = 640, 480
    # plot box in pixels, and the data range it represents
    bx0, by0, bx1, by1 = 80, 40, 600, 420
    Xlo, Xhi, Ylo, Yhi = 0.0, 10.0, 0.0, 0.5

    def px(x, y):
        fx = (x - Xlo) / (Xhi - Xlo)
        fy = (y - Ylo) / (Yhi - Ylo)
        return bx0 + fx * (bx1 - bx0), by1 - fy * (by1 - by0)

    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([bx0, by0, bx1, by1], outline="black")
    # a known line  y = 0.05 + 0.03 x   (red)
    line = [(x, 0.05 + 0.03 * x) for x in np.linspace(0, 10, 400)]
    d.line([px(*p) for p in line], fill=(220, 20, 20), width=3)
    # known square markers (blue) at a few x
    truth_pts = [(1, 0.10), (3, 0.22), (5, 0.34), (7, 0.42), (9, 0.30)]
    for x, y in truth_pts:
        cx, cy = px(x, y)
        d.rectangle([cx - 4, cy - 4, cx + 4, cy + 4], fill=(20, 20, 220))
    rgb = np.asarray(im)

    # calibrate from the box corners: click1 (x=0 edge) -> X=0, click2 (x=10 edge) -> X=10
    refs = [
        {"px": bx0, "py": by1, "X": 0.0, "Y": 0.0},     # bottom-left
        {"px": bx1, "py": by0, "X": 10.0, "Y": 0.5},    # top-right
    ]
    cal = Calibration.from_refs(refs)

    ok = True

    ld, _ = extract_line(rgb, cal, (220, 20, 20), tol=60,
                         roi=(bx0 + 2, by0 + 2, bx1 - 2, by1 - 2), x_step=2)
    pred = 0.05 + 0.03 * ld[:, 0]
    err = float(np.abs(ld[:, 1] - pred).mean())
    print(f"line:   {len(ld):4d} pts   mean |dy| = {err:.4f}  (want < 0.006)")
    ok &= len(ld) > 100 and err < 0.006

    pd, _ = extract_points(rgb, cal, (20, 20, 220), tol=80,
                           roi=(bx0 + 2, by0 + 2, bx1 - 2, by1 - 2),
                           area=(20, 600), shape="square")
    pd = pd[np.argsort(pd[:, 0])]
    tru = np.array(sorted(truth_pts))
    if len(pd) == len(tru):
        derr = float(np.abs(pd - tru).max())
        print(f"points: {len(pd)}/{len(tru)} found   max |d| = {derr:.4f}  "
              f"(want < 0.05)")
        ok &= derr < 0.05
    else:
        print(f"points: {len(pd)}/{len(tru)} found  -- FAIL")
        ok = False

    import tempfile, os
    for fmt in FORMATS:
        p = os.path.join(tempfile.gettempdir(), f"_daq_selftest{EXT[fmt]}")
        export_data(p, pd, fmt, build_meta("t", "syn.png", "points", "square",
                                           False, False, cal, len(pd)))
        ok &= os.path.getsize(p) > 0
        os.remove(p)
    print("export: all 4 formats written")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
