"""data_acquisition -- core digitization logic (no GUI).

Calibration, pixel -> data mapping, line / point extraction, export.
Kept import-clean of tkinter so it can be unit-tested headless:

    python core.py --selftest
"""
from __future__ import annotations

import functools
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
    # compare the integer squared distance with tol^2: no sqrt, and d2 is a
    # whole number so d2 <= tol^2  <=>  d2 <= floor(tol^2)
    if tol < 0:
        return np.zeros(rgb.shape[:2], bool)
    t = [int(v) for v in target[:3]]
    lim = math.floor(float(tol) ** 2)
    if _HAS_CV2 and rgb.dtype == np.uint8:
        # per-channel (v - t)^2 by table lookup, summed over channels in
        # float32 (exact: whole numbers < 2^24)
        lut = (np.arange(256, dtype=np.float32)[:, None]
               - np.asarray(t, np.float32)) ** 2
        sq = cv2.LUT(np.ascontiguousarray(rgb[:, :, :3]), lut.reshape(1, 256, 3))
        return cv2.transform(sq, np.ones((1, 3), np.float32)) <= lim
    d2 = None
    for c in range(3):
        d = rgb[:, :, c].astype(np.int32) - t[c]
        d *= d
        d2 = d if d2 is None else d2 + d
    return d2 <= lim


def snap_point(rgb: np.ndarray, target, tol: float, ix: float, iy: float,
              search: int = 6):
    """Nearest colour-mask pixel to (ix, iy) within a `search`-px square.

    Used by manual "Add point" clicks: a click near the curve snaps onto it
    (same colour-mask logic as auto-detection), instead of taking the exact,
    slightly-off pixel the user happened to click. Returns (sx, sy) in image
    pixel coordinates, or None if no matching-colour pixel is within range
    (caller should fall back to the raw click position).
    """
    h, w = rgb.shape[:2]
    x0, x1 = max(0, int(ix) - search), min(w, int(ix) + search + 1)
    y0, y1 = max(0, int(iy) - search), min(h, int(iy) + search + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    mask = color_mask(rgb[y0:y1, x0:x1], target, tol)
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    d2 = (xs + x0 - ix) ** 2 + (ys + y0 - iy) ** 2
    k = int(np.argmin(d2))
    return float(xs[k] + x0), float(ys[k] + y0)


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
    """Yield (cx, cy, area, submask, (x0, y0)) per connected component
    (8-connectivity).

    `submask` is cropped to the component's bounding box plus a 1-px frame
    (clipped to the image) with top-left corner (x0, y0) -- a whole-image
    mask per blob costs more than all the per-blob work on it. cx, cy are in
    full-mask coordinates.
    """
    H, W = mask.shape[:2]
    if _HAS_CV2:
        n, lbl, stats, cent = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            bx, by, bw, bh, area = (int(v) for v in stats[i])
            y0, x0 = max(0, by - 1), max(0, bx - 1)
            sub = lbl[y0:min(H, by + bh + 1), x0:min(W, bx + bw + 1)] == i
            yield float(cent[i, 0]), float(cent[i, 1]), area, sub, (x0, y0)
    elif _HAS_SCIPY:
        lbl, n = _ndi.label(mask)
        for i, sl in enumerate(_ndi.find_objects(lbl), 1):
            y0, x0 = max(0, sl[0].start - 1), max(0, sl[1].start - 1)
            sub = lbl[y0:min(H, sl[0].stop + 1), x0:min(W, sl[1].stop + 1)] == i
            ys, xs = np.nonzero(sub)
            yield (float(xs.mean() + x0), float(ys.mean() + y0), int(xs.size),
                   sub, (x0, y0))
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


# --------------------------------------------------------------------------- #
# overlapping circle markers: split merged blobs by partial-arc evidence
# --------------------------------------------------------------------------- #
# Stacked markers of one colour merge into one connected blob, which the plain
# centroid path turns into ONE point (or drops, if it's too big / not round).
# Here each merged blob is explained as a union of circles of the *known*
# marker radius: every edge pixel votes for the centres a radius-r circle
# through it could have (fixed-radius Hough = convolution with a ring), and a
# centre is accepted when enough of its circumference is backed by real edge
# (`min_arc`). Accept the best, delete the edge it explains, repeat until the
# blob is covered. Tests + synthetic cases: tests/marker_overlap/.

def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Fill enclosed holes (hollow markers -> discs)."""
    if _HAS_CV2:
        pad = np.ones((mask.shape[0] + 2, mask.shape[1] + 2), np.uint8)
        pad[1:-1, 1:-1] = ~mask            # 1-px frame = guaranteed outside
        _, lbl = cv2.connectedComponents(pad, connectivity=4)
        return (lbl != lbl[0, 0])[1:-1, 1:-1]
    return _ndi.binary_fill_holes(mask)


def _circularity(sub: np.ndarray) -> float:
    if not _HAS_CV2:
        return 1.0
    cnts, _ = cv2.findContours(sub.astype(np.uint8), cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return 0.0
    c = max(cnts, key=cv2.contourArea)
    a, p = cv2.contourArea(c), cv2.arcLength(c, True)
    return 4.0 * math.pi * a / (p * p) if p > 1 else 0.0


def _circle_3pt(p):
    """Vectorised circumcircle of point triplets p (N,3,2) -> (cx, cy, r)."""
    (ax, ay), (bx, by), (cx, cy) = p[:, 0].T, p[:, 1].T, p[:, 2].T
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    d = np.where(np.abs(d) < 1e-9, np.nan, d)
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, np.hypot(ax - ux, ay - uy)


@functools.lru_cache(maxsize=1024)
def _ransac_draws(ns: tuple, tries: int) -> np.ndarray:
    """Point-index triplets for RANSAC round len(ns) of _arc_radius: what a
    fresh default_rng(0) draws after rounds over ns[:-1] points. Cached --
    outlines of equal length recur across blobs, the generator setup doesn't
    need to. Read-only."""
    rng = np.random.default_rng(0)
    for n in ns:
        i = rng.integers(0, n, (tries, 3))
    i.flags.writeable = False
    return i


def _arc_radius(sub: np.ndarray, max_arcs: int = 4, tries: int = 150,
                max_pts: int | None = None):
    """Radii of the circular arcs making up one blob's outline: [(r, n_px)].

    A union of equal discs is bounded by radius-r arcs; a fit that spans a
    junction between two discs reads flatter (larger). Sequential RANSAC:
    best-supported circle, drop its inliers, repeat; keep arcs >= 60 deg.
    A LOW percentile of these (estimate_marker) then gives r even when no
    marker in the image stands alone. Long arcs only: short pixel-staircase
    arcs bias the fit small. `max_pts` caps the work on a huge outline (a
    whole line-joined series) by fitting every k-th outline pixel only.
    """
    if not _HAS_CV2:
        return []
    cnts, _ = cv2.findContours(
        cv2.copyMakeBorder(sub.astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT,
                           value=0), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return []
    pts = max(cnts, key=len)[:, 0, :].astype(float)
    if len(pts) < 16:
        return []
    if max_pts and len(pts) > max_pts:
        pts = pts[::-(-len(pts) // max_pts)]
    ext = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])))
    ns = ()                                        # deterministic draws
    out = []
    for _ in range(max_arcs):
        n = len(pts)
        if n < 12:
            break
        ns += (n,)
        i = _ransac_draws(ns, tries)
        ux, uy, r = _circle_3pt(pts[i])
        ok = np.isfinite(r) & (r > 1.5) & (r < 0.75 * ext + 2)
        if not ok.any():
            break
        ux, uy, r = ux[ok], uy[ok], r[ok]
        # inliers |dist - r| < 0.8, compared squared (r > 1.5 here)
        d2 = pts[None, :, 0] - ux[:, None]
        dy = pts[None, :, 1] - uy[:, None]
        d2 *= d2
        dy *= dy
        d2 += dy
        inl = (d2 > ((r - 0.8) ** 2)[:, None]) & (d2 < ((r + 0.8) ** 2)[:, None])
        k = int(np.argmax(inl.sum(1)))
        sel = inl[k]
        x, y = pts[sel, 0], pts[sel, 1]
        if len(x) < 10:
            break
        # refine on inliers: algebraic circle fit, about the inlier mean
        mx, my = x.mean(), y.mean()
        A = np.column_stack([x - mx, y - my, np.ones_like(x)])
        b = A[:, 0] ** 2 + A[:, 1] ** 2
        try:
            p, q, cc = np.linalg.solve(A.T @ A, A.T @ b)
        except np.linalg.LinAlgError:
            (p, q, cc), *_ = np.linalg.lstsq(A, b, rcond=None)
        rf = math.sqrt(max(0.0, cc + (p / 2) ** 2 + (q / 2) ** 2))
        xc, yc = mx + p / 2, my + q / 2
        ang = np.sort(np.arctan2(y - yc, x - xc))
        gaps = np.diff(np.concatenate([ang, ang[:1] + 2 * np.pi]))
        span = 2 * np.pi - gaps.max()                    # angular coverage
        if span >= math.radians(60):
            out.append((rf, int(sel.sum())))
        pts = pts[~sel]
    return out


@dataclass
class MarkerModel:
    """What one isolated marker looks like in the colour mask."""
    area: float          # filled (hole-filled) area, px^2
    r: float             # equivalent radius sqrt(area/pi), px
    hollow: bool         # open circle (ring) vs filled disc
    stroke: float        # ring stroke width (hollow only), px
    n_isolated: int      # how many isolated markers the estimate came from


def estimate_marker(mask: np.ndarray, r_hint: float | None = None,
                    quick: bool = False) -> MarkerModel | None:
    """Learn the single-marker size from the round, isolated blobs in `mask`.

    Takes the median area over components that are circular (>= 0.8) and
    whose size agrees with the outline-arc radius (_arc_radius) -- merged
    pairs are elongated, near-coincident stacks are round but too big. If
    nothing is isolated (everything stacked), uses the arc radius alone.
    `r_hint` (px) overrides r. `quick`: bound the arc-radius work for an
    interactive check -- outline arcs from at most 16 blobs spread evenly
    over the image, each outline thinned to <= 400 px.
    """
    filled = _fill_holes(mask)
    # markers of one plot rasterise to a handful of distinct pixel shapes:
    # outline measures are computed once per shape (keyed on its bytes)
    circ_of, arcs_of = {}, {}
    rows = []
    for _, _, a, sub, (x0, y0) in _components(filled):
        if a < 8:
            continue
        key = (sub.shape, sub.tobytes())
        if key not in circ_of:
            circ_of[key] = _circularity(sub)
        own = mask[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]][sub]
        rows.append((a, circ_of[key], own.sum() / a, sub, key))
    if not rows:
        return None
    # outline-arc radius: the same for a lone marker and for any stack of
    # them, so it anchors which "round" blobs are real singles vs stacks
    r_in = None
    arc_rows = rows
    if quick and len(rows) > 16:
        arc_rows = [rows[i] for i in np.linspace(0, len(rows) - 1, 16).astype(int)]
    arcs = []
    for row in arc_rows:
        if row[4] not in arcs_of:
            arcs_of[row[4]] = _arc_radius(row[3], max_pts=400 if quick else None)
        arcs += arcs_of[row[4]]
    if arcs:
        # low percentile: junction-spanning arcs only ever read too big
        r_in = float(np.percentile([a[0] for a in arcs], 20)) + 0.5  # px centre -> edge
    iso = [r for r in rows if r[1] >= 0.80]
    if r_in is not None:
        a_in = math.pi * r_in * r_in
        iso = [r for r in iso if 0.7 * a_in <= r[0] <= 1.35 * a_in]
    if iso:
        area = float(np.median([r[0] for r in iso]))
        fill = float(np.median([r[2] for r in iso]))
    elif r_in is not None:
        area = math.pi * r_in * r_in
        fill = float(np.median([r[2] for r in rows]))
    else:
        return None
    r = math.sqrt(area / math.pi)
    if r_hint:
        r = float(r_hint)
        area = math.pi * r * r
    hollow = fill < 0.75
    stroke = r - math.sqrt(max(0.0, 1.0 - fill)) * r if hollow else 0.0
    return MarkerModel(area=area, r=r, hollow=hollow, stroke=max(1.0, stroke),
                       n_isolated=len(iso))


# kernels are cached (one marker radius per image, many blobs) and read-only

@functools.lru_cache(maxsize=64)
def _ring_kernel(radius: float, half_width: float):
    R = int(math.ceil(radius + half_width + 1))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    d = np.hypot(xx, yy)
    k = (np.abs(d - radius) <= half_width).astype(np.float32)
    k /= k.sum()
    k.flags.writeable = False
    return k


@functools.lru_cache(maxsize=64)
def _disc_kernel(radius: float):
    R = int(math.ceil(radius + 1))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    k = (np.hypot(xx, yy) <= radius).astype(np.float32)
    k /= k.sum()
    k.flags.writeable = False
    return k


def _conv(img: np.ndarray, k: np.ndarray) -> np.ndarray:
    if _HAS_CV2:
        return cv2.filter2D(img.astype(np.float32), -1, k,
                            borderType=cv2.BORDER_CONSTANT)
    return _ndi.correlate(img.astype(np.float32), k, mode="constant")


def split_circles(blob: np.ndarray, model: MarkerModel, support=None,
                  min_arc: float = 0.10, max_n: int = 200):
    """Explain one merged blob (bool mask, raw colour mask of the component)
    as a set of circles of radius model.r. Returns [(cx, cy, arc_score)].

    `support`: bool mask of pixels a hidden part of a marker may lie under
    (e.g. other-colour markers drawn on top); defaults to the blob itself.

    A candidate centre is accepted when (a) >= `min_arc` of its circumference
    is backed by edge evidence nobody else has claimed, and (b) its disc
    explains blob area no accepted circle covers yet -- (b) is what keeps
    false positives out. After the greedy pass every circle is re-fitted to
    the edge pixels it owns (fixes near-coincident pairs, where the first
    guess lands between the two), then the leftovers are searched again.
    """
    r = model.r
    pad = int(math.ceil(r)) + 3
    blob = np.pad(blob, pad)
    filled = _fill_holes(blob)
    # holes closed by support count too: a ring cut by the plot-area edge
    sup = filled if support is None else _fill_holes(blob | np.pad(support, pad))

    if model.hollow:
        # the ring stroke itself is the evidence, sampled at mid-stroke radius
        ev0 = blob.copy()
        r_ev, hw = r - model.stroke / 2.0, max(0.75, model.stroke / 2.0)
    else:
        # the blob outline is the evidence (inner-edge pixels)
        er = (cv2.erode(filled.astype(np.uint8), np.ones((3, 3), np.uint8))
              if _HAS_CV2 else _ndi.binary_erosion(filled))
        ev0 = filled & ~er.astype(bool)
        r_ev, hw = r - 0.5, 1.0
    ring = _ring_kernel(r_ev, hw)
    # a centre is only plausible if its disc lies (almost) inside blob+support
    valid0 = _conv(sup, _disc_kernel(max(1.0, r - 1.0))) >= 0.90
    H, W = blob.shape
    ey, ex = np.nonzero(ev0)
    # what a set of circles must explain, and the size of one marker in those
    # units. Filled: blob area. Hollow: stroke pixels -- two rings a few px
    # apart barely change the filled area but leave a whole stroke unexplained.
    if model.hollow:
        tgt = ev0
        unit = max(1.0, float(_ring_kernel(r_ev, hw).astype(bool).sum()))
    else:
        tgt = filled
        unit = model.area
    min_new = 0.08 * unit
    # no second centre this close to an accepted one. Filled: a pair this
    # close is one blob with no usable edge between. Hollow: both strokes stay
    # visible, so pairs down to ~a stroke width apart are separable.
    excl = max(1.5, 1.5 * model.stroke) if model.hollow else 0.35 * r

    # every per-circle mask below (disc, ring, exclusion zone, split search)
    # lies within a few px of r of its centre: evaluate it on that window
    # only, as squared distance against squared radii -- same pixels as the
    # whole-canvas np.hypot version, a fraction of the work.
    ring_lo = r_ev - hw - 0.75                     # |d - r_ev| <= hw + 0.75
    ring2 = (ring_lo * ring_lo if ring_lo > 0 else -1.0, (r_ev + hw + 0.75) ** 2)
    disc2 = (r + 0.5) ** 2                         # d <= r + 0.5
    reach_ring = max(r + 0.5, r_ev + hw + 0.75)
    ax_ = np.arange(max(H, W), dtype=float)

    def local(cx, cy, reach):
        """Window of all pixels within `reach` of (cx, cy) as a slice pair,
        plus their squared distance to (cx, cy); empty when off the canvas."""
        x0, x1 = max(0, math.floor(cx - reach)), min(W, math.ceil(cx + reach) + 1)
        y0, y1 = max(0, math.floor(cy - reach)), min(H, math.ceil(cy + reach) + 1)
        x0, y0 = min(x0, W), min(y0, H)
        x1, y1 = max(x1, x0), max(y1, y0)
        d2 = (ax_[y0:y1, None] - cy) ** 2 + (ax_[None, x0:x1] - cx) ** 2
        return (slice(y0, y1), slice(x0, x1)), d2

    def on_ring(d2):
        return (d2 >= ring2[0]) & (d2 <= ring2[1])

    def explained(circles):
        m = np.zeros((H, W), bool)
        for cx, cy, _ in circles:
            w, d2 = local(cx, cy, reach_ring)
            m[w] |= on_ring(d2)
        return m

    def coverage(circles):
        if model.hollow:
            return explained(circles)
        m = np.zeros((H, W), bool)
        for cx, cy, _ in circles:
            w, d2 = local(cx, cy, r + 0.5)
            m[w] |= d2 <= disc2
        return m

    def greedy(circles):
        ev = (ev0 & ~explained(circles)).astype(np.float32)
        valid = valid0.copy()
        covered = coverage(circles)
        for cx, cy, _ in circles:
            w, d2 = local(cx, cy, excl)
            valid[w][d2 < excl * excl] = False
        added = []
        left = int((tgt & ~covered).sum())     # target px nobody covers yet
        while len(circles) + len(added) < max_n:
            if left < 0.10 * unit:
                break                          # blob fully explained
            acc = _conv(ev, ring)
            acc *= valid
            k = int(np.argmax(acc))
            cy, cx = divmod(k, W)
            score = float(acc[cy, cx])
            if score < min_arc:
                break
            # sub-pixel: weighted centroid of the peak neighbourhood
            y0, y1 = max(0, cy - 2), min(H, cy + 3)
            x0, x1 = max(0, cx - 2), min(W, cx + 3)
            wgt = np.clip(acc[y0:y1, x0:x1] - 0.7 * score, 0, None)
            if wgt.sum() > 0:
                fx = float((wgt * ax_[None, x0:x1]).sum() / wgt.sum())
                fy = float((wgt * ax_[y0:y1, None]).sum() / wgt.sum())
            else:
                fx, fy = float(cx), float(cy)
            w, d2 = local(fx, fy, reach_ring)
            ring_px = on_ring(d2)
            disc = ring_px if model.hollow else (d2 <= disc2)
            # explained edge is spent either way, so one arc can't vote twice
            ev[w][ring_px] = 0
            valid[w][d2 < excl * excl] = False
            new = int((tgt[w] & disc & ~covered[w]).sum())
            if new < min_new:
                continue                       # adds nothing the blob lacks
            added.append((fx, fy, score))
            covered[w] |= disc
            left -= new
        return added

    exf, eyf = ex.astype(float), ey.astype(float)
    ez = exf + 1j * eyf                            # edge pixels as x + iy

    def refine_many(sets, iters=4):
        """Fixed-r circle fit of each centre to the edge pixels it owns, for
        several independent circle sets of equal size at once (try_splits'
        candidates). Returns the refined sets.

        Points are complex x + iy. In each set every edge pixel belongs to
        the nearest ring, and each circle's 2-parameter Gauss-Newton step is
        solved in closed form from per-circle sums (np.add.reduceat) instead of
        one lstsq per circle: with unit Jacobian rows u = ux + i*uy,
        J^T J = [[a, b], [b, c]] is carried by cnt = a + c and
        A = sum(u^2) = (a - c) + 2ib, and the step z = sx + i*sy solving
        J^T J [sx, sy] = -J^T res is 2 (cnt g - A conj(g)) / (cnt^2 - |A|^2),
        g = -sum(u * res)."""
        K, n = len(sets), len(sets[0]) if sets else 0
        if n == 0 or ex.size == 0:
            return sets
        C = np.array([[c[0] + 1j * c[1] for c in cs] for cs in sets]).ravel()
        for _ in range(iters):
            # |distance to each ring|, in place (hypot on complex is slow)
            D = exf[None, :, None] - C.real.reshape(K, 1, n)
            dy = eyf[None, :, None] - C.imag.reshape(K, 1, n)
            D *= D
            dy *= dy
            D += dy
            np.sqrt(D, out=D)
            D -= r_ev
            np.abs(D, out=D)
            own = np.argmin(D, axis=2)                # (set, pixel) -> circle
            ks, ps = np.nonzero(D.min(axis=2) <= hw + 1.5)
            po = ks * n + own[ks, ps]                 # flat circle id per pixel
            cnt = np.bincount(po, minlength=K * n)
            fit = cnt >= 6
            if not fit.any():
                break                                 # nothing can move
            # pixels of the circles being fitted, grouped by circle
            sel = fit[po]
            order = np.argsort(po[sel], kind="stable")
            q = ez[ps[sel]][order]
            fid = np.nonzero(fit)[0]
            nf = cnt[fid]
            grp = np.repeat(np.arange(len(fid)), nf)
            starts = np.concatenate([[0], np.cumsum(nf)[:-1]])
            nf = nf.astype(float)
            nf2 = nf * nf
            P = C[fid]
            B = np.empty((len(q), 2), complex)        # per pixel: u^2, u*res
            with np.errstate(divide="ignore", invalid="ignore"):
                for _ in range(3):                    # Gauss-Newton
                    d = P[grp] - q
                    dist = np.abs(d) + 1e-9
                    u = np.divide(d, dist, out=d)     # Jacobian rows
                    np.multiply(u, u, out=B[:, 0])
                    np.multiply(u, dist - r_ev, out=B[:, 1])
                    A, h = np.add.reduceat(B, starts).T   # h = -g
                    den = nf2 - (A * A.conj()).real   # 4 det(J^T J)
                    z = (A * h.conj() - nf * h) * 2.0 / den
                    for j in np.nonzero(~(den > 4e-9 * nf2))[0]:
                        on = grp == j                 # degenerate: min-norm
                        J = np.column_stack([u[on].real, u[on].imag])
                        sx, sy = np.linalg.lstsq(J, r_ev - dist[on], rcond=None)[0]
                        z[j] = sx + 1j * sy
                    zf = z.view(float)                # clip x and y steps
                    np.clip(zf, -1.0, 1.0, out=zf)
                    P += z
            # never let a refit walk a centre off the plausible region
            iy, ix = np.rint(P.imag).astype(int), np.rint(P.real).astype(int)
            keep = (iy >= 0) & (iy < H) & (ix >= 0) & (ix < W)
            keep[keep] = valid0[iy[keep], ix[keep]]
            keep &= np.abs(P - C[fid]) < 0.5 * r
            C[fid[keep]] = P[keep]
        C = C.reshape(K, n)
        return [[(float(C[k, j].real), float(C[k, j].imag), cs[j][2])
                 for j in range(n)] for k, cs in enumerate(sets)]

    def refine(circles):
        return refine_many([circles])[0]

    def cost(circles):
        """Unexplained blob area + disc area spilling outside blob/support."""
        cov = coverage(circles)
        return float((tgt & ~cov).sum() + (cov & ~sup).sum())

    def try_splits(circles):
        """Near-coincident pair -> ONE circle sitting between them, and the
        exclusion zone stops greedy from placing the second. So: for each
        circle, try two circles along the axis of the blob area it leaves
        unexplained; keep the split if it clearly lowers cost()."""
        changed = False
        cov = coverage(circles)
        left = tgt & ~cov
        cost0 = float(left.sum() + (cov & ~sup).sum())    # = cost(circles)
        for j in range(len(circles)):
            cx, cy, s = circles[j]
            (ys, xs), d2 = local(cx, cy, 1.8 * r)
            near = left[ys, xs] & (d2 <= (1.8 * r) ** 2)
            if near.sum() < 0.04 * unit:
                continue
            ly, lx = np.nonzero(near)
            ly, lx = ly + ys.start, lx + xs.start
            v = np.column_stack([lx - cx, ly - cy]).astype(float)
            u = np.linalg.eigh(v.T @ v)[1][:, -1]          # principal axis
            ext = np.abs(v @ u).max() - r
            best, best_c = cost0, None
            trials = []
            for off in (0.3, 0.5, 0.7):
                dd = max(0.5, off * max(ext, 0.5))
                trial = [c for k, c in enumerate(circles) if k != j]
                pair = [(cx + dd * u[0], cy + dd * u[1], s),
                        (cx - dd * u[0], cy - dd * u[1], s)]
                if max(ext, 0) < 0.25 * r and (v @ u).mean() != 0:
                    # lopsided leftover: keep one centre, add one beyond it
                    m = np.sign((v @ u).mean())
                    pair = [(cx - m * 0.3 * dd * u[0], cy - m * 0.3 * dd * u[1], s),
                            (cx + m * 2 * dd * u[0], cy + m * 2 * dd * u[1], s)]
                trials.append(trial + pair)
            for cand in refine_many(trials):          # all three in one pass
                c = cost(cand)
                if c < best - 0.05 * unit:
                    best, best_c = c, cand
            if best_c is not None:
                return best_c, True
        return circles, changed

    circles = greedy([])
    for _ in range(3):
        circles = refine(circles)
        more = greedy(circles)
        if not more:
            break
        circles = circles + more
    for _ in range(max_n):
        circles, changed = try_splits(circles)
        if not changed:
            break
    return [(cx - pad, cy - pad, s) for cx, cy, s in circles]


def _background_rgb(rgb: np.ndarray):
    # most common colour at 5 bits/channel: key = r5 << 10 | g5 << 5 | b5
    # (ties -> lowest key). A 32^3 histogram, flattened, is indexed by key.
    if _HAS_CV2 and rgb.dtype == np.uint8:
        hist = cv2.calcHist([np.ascontiguousarray(rgb[:, :, :3])], [0, 1, 2],
                            None, [32, 32, 32], [0, 256, 0, 256, 0, 256])
    else:
        q = (rgb[:, :, :3] // 8).astype(np.int32)
        key = (q[:, :, 0] << 10) | (q[:, :, 1] << 5) | q[:, :, 2]
        hist = np.bincount(key.ravel(), minlength=1 << 15)
    v = int(np.argmax(hist))
    return (((v >> 10) & 31) * 8 + 4, ((v >> 5) & 31) * 8 + 4, (v & 31) * 8 + 4)


def _strip_thin_lines(mask: np.ndarray, r_hint=None):
    """'o-' plots: a same-colour line joins every marker into one blob.

    Estimate the thinnest stroke width from the distance-transform ridge and
    remove it with a morphological opening -- but only if the markers left
    behind are clearly (>= 2.5x) wider than that stroke and the
    line actually joined blobs (opening multiplies the blob count). Plain
    scatter / hollow rings are left untouched.
    Returns (mask, n_pixels_removed, model): `model` is estimate_marker() of
    the stripped mask (None when nothing was stripped).
    """
    if not _HAS_CV2 or mask.sum() < 50:
        return mask, 0, None
    m8 = mask.astype(np.uint8)
    dt = cv2.distanceTransform(m8, cv2.DIST_L2, 3)
    # (dt > 0 exactly on the mask)
    ridge = mask & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)) - 1e-6)
    if ridge.sum() < 20:
        return mask, 0, None
    half = float(np.median(dt[ridge]))            # line dominates the ridge
    k = int(2 * math.ceil(half) + 3)              # one px margin over the line
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    opened = cv2.morphologyEx(m8, cv2.MORPH_OPEN, ker).astype(bool)
    removed = int((mask & ~opened).sum())
    # signature of a joining line: cutting it multiplies the blob count
    n0 = cv2.connectedComponents(m8, connectivity=8)[0] - 1
    n1 = cv2.connectedComponents(opened.astype(np.uint8), connectivity=8)[0] - 1
    if n1 < 1.5 * n0 + 2:
        return mask, 0, None                      # no joining line present
    model = estimate_marker(opened, r_hint)
    if model is None or model.hollow or model.r < 2.5 * half + 1:
        return mask, 0, None                      # markers not separable by width
    return opened, removed, model


def extract_circles(rgb, target, tol, roi=None, r_hint=None, min_arc=0.10,
                    amin=6, occlusion_aware=True):
    """Circle-marker detection that splits overlapping / stacked markers.

    Returns (pixels Nx2, info dict). Isolated round blobs -> centroid (exact);
    anything bigger or non-round -> split_circles().
    """
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = _clip_roi(roi, w, h)
    info = {"model": None, "n_split_blobs": 0}
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 2)), info
    sub_rgb = rgb[y0:y1, x0:x1]
    mask = color_mask(sub_rgb, target, tol)
    mask, info["line_stripped_px"], model = _strip_thin_lines(mask, r_hint)
    if model is None:                               # else: already fitted
        model = estimate_marker(mask, r_hint)
    info["model"] = model
    if model is None:
        return np.empty((0, 2)), info
    # work on a canvas grown by a margin: outside the plot-area box is
    # "unknown", so a marker cut by the box edge may continue there
    M = int(math.ceil(model.r)) + 4
    mask = np.pad(mask, M)
    outside = np.ones_like(mask)
    outside[M:-M, M:-M] = False
    foreign = outside
    if occlusion_aware:
        bg = _background_rgb(sub_rgb)
        foreign = foreign | np.pad(~color_mask(sub_rgb, bg, 60), M) & ~mask
    x0, y0 = x0 - M, y0 - M
    filled = _fill_holes(mask)
    px = []
    if _HAS_CV2:
        n, lbl, stats, cent = cv2.connectedComponentsWithStats(
            filled.astype(np.uint8), connectivity=8)
        comps = [(float(cent[i, 0]), float(cent[i, 1]),
                  int(stats[i, cv2.CC_STAT_AREA]),
                  (int(stats[i, 0]), int(stats[i, 1]),
                   int(stats[i, 2]), int(stats[i, 3])), i) for i in range(1, n)]
    else:
        lbl, n = _ndi.label(filled)
        comps = []
        for i, sl in enumerate(_ndi.find_objects(lbl), 1):
            ys, xs = np.nonzero(lbl == i)
            comps.append((xs.mean(), ys.mean(), xs.size,
                          (sl[1].start, sl[0].start, sl[1].stop - sl[1].start,
                           sl[0].stop - sl[0].start), i))
    A1 = model.area
    for cx, cy, a, (bx, by, bw, bh), i in comps:
        if a < max(amin, 0.20 * A1):
            continue
        comp = lbl[by:by + bh, bx:bx + bw] == i
        if 0.65 * A1 <= a <= 1.30 * A1 and _circularity(comp) >= 0.80:
            px.append((x0 + cx, y0 + cy))           # a clean single marker
            continue
        m = mask[by:by + bh, bx:bx + bw] & comp
        pad = int(math.ceil(model.r)) + 2
        fy0, fx0 = max(0, by - pad), max(0, bx - pad)
        fy1, fx1 = min(mask.shape[0], by + bh + pad), min(mask.shape[1], bx + bw + pad)
        big = np.zeros((fy1 - fy0, fx1 - fx0), bool)
        big[by - fy0:by - fy0 + bh, bx - fx0:bx - fx0 + bw] = m
        sup = foreign[fy0:fy1, fx0:fx1]
        m, ox, oy = big, fx0, fy0
        info["n_split_blobs"] += 1
        for fx, fy, _s in split_circles(m, model, support=sup, min_arc=min_arc):
            px.append((x0 + ox + fx, y0 + oy + fy))
    return np.asarray(px, float).reshape(-1, 2), info


# --------------------------------------------------------------------------- #
# overlay colour: draw detections in whatever stands out most
# --------------------------------------------------------------------------- #

MARK_COLOURS = {"red": (255, 0, 0), "black": (0, 0, 0), "cyan": (0, 220, 255),
                "magenta": (255, 0, 255), "yellow": (255, 230, 0),
                "white": (255, 255, 255)}


def colour_distance(a, b) -> float:
    """'Redmean' RGB distance -- cheap, closer to perceived difference than
    plain Euclidean (weights R/B by how red the pair is)."""
    r1, g1, b1 = (float(v) for v in a[:3])
    r2, g2, b2 = (float(v) for v in b[:3])
    rm = (r1 + r2) / 2.0
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return math.sqrt((2 + rm / 256) * dr * dr + 4 * dg * dg
                     + (2 + (255 - rm) / 256) * db * db)


def best_mark_colour(avoid, candidates=None) -> str:
    """Name of the candidate colour farthest from EVERY colour in `avoid`
    (e.g. the tracked marker colour and the plot background): maximise the
    worst-case distance."""
    cands = candidates or MARK_COLOURS
    avoid = [a for a in avoid if a is not None]
    if not avoid:
        return "red"
    return max(cands, key=lambda n: min(colour_distance(cands[n], a)
                                        for a in avoid))


# --------------------------------------------------------------------------- #
# "Snap marker": click one marker -> shape, style, colours, size
# --------------------------------------------------------------------------- #
# Picking a colour only sees the face (or the edge) of a marker. Snapping cuts
# out the whole marker -- every non-background pixel connected to the click --
# and reads everything the detector needs off it in one go.

MARKER_SHAPES = ["circle", "square", "diamond", "triangle", "other"]


@dataclass
class MarkerTemplate:
    shape: str                 # one of MARKER_SHAPES
    style: str                 # filled | hollow | edged (face != edge colour)
    face_rgb: tuple | None     # interior colour (None when hollow)
    edge_rgb: tuple            # outline colour
    detect_rgb: tuple          # the colour detection should track
    r: float                   # radius of the detect_rgb mask (px), for r_hint
    size: float                # outer bounding size (px), for display
    area: float                # detect_rgb mask area (px^2)
    fills: dict                # shape-fit scores, for debugging / display
    center: tuple              # (x, y) image px of the snapped marker
    confident: bool            # False: merged with a neighbour / odd shape
    note: str = ""             # why confidence is low, for the UI

    def describe(self) -> str:
        s = f"{self.shape}, {self.style}"
        if self.style == "edged":
            s += f" (face rgb{self.face_rgb}, edge rgb{self.edge_rgb})"
        else:
            s += f" rgb{self.detect_rgb}"
        s += f", ~{self.size:.0f}px"
        if not self.confident:
            s += f"  [low confidence: {self.note or 'snap an isolated marker'}]"
        return s


def classify_shape(sil: np.ndarray):
    """Shape of one silhouette (bool mask, holes filled) -> (name, fills).

    Each shape fills its OWN minimal enclosing figure best:
        circle   : area / min enclosing circle   (circle 1, square 0.64, tri 0.41)
        square   : area / min-area rectangle     (square 1, circle 0.79, tri 0.5)
        triangle : area / min enclosing triangle (tri 1, circle 0.60, square 0.5)
    so the winner is the shape. Square vs diamond = rectangle tilt (~0 / ~45 deg).
    """
    cnts, _ = cv2.findContours(np.pad(sil, 1).astype(np.uint8),
                               cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(cnts, key=cv2.contourArea)
    area = float(sil.sum())                        # pixel count, not polygon
    (_, _), rc = cv2.minEnclosingCircle(c)
    # contour runs through pixel centres: grow enclosing figures by ~0.5 px
    circ = area / (math.pi * (rc + 0.5) ** 2)
    (_, _), (w, h), ang = cv2.minAreaRect(c)
    rect = area / max(1.0, (w + 1) * (h + 1))
    tri_a, _ = cv2.minEnclosingTriangle(c.astype(np.float32))
    per = cv2.arcLength(c, True)
    tri = area / max(1.0, tri_a + 0.5 * per)       # same ~0.5 px growth
    fills = {"circle": round(circ, 3), "square": round(rect, 3),
             "triangle": round(tri, 3)}
    # circles also fill their bounding rect at 0.785 -- only call it a square
    # when it beats the circle clearly
    best = max(fills, key=fills.get)
    if fills[best] < 0.72:
        return "other", fills                      # plus, x, star, merged blob
    if best == "square":
        tilt = abs(((ang % 90) + 45) % 90 - 45)    # 0 = axis aligned, 45 = diamond
        return ("diamond" if tilt > 22.5 else "square"), fills
    return best, fills


def _soft_silhouette(sub, bg, sil, up: int = 4, exclude=None):
    """Sub-pixel silhouette for shape classification.

    A 7-px circle and a 7-px square differ only in their antialiased corner
    pixels. Treat distance-from-background as coverage (alpha), upsample it
    `up`x with cubic interpolation, cut at 50 % -- corners come back.
    `exclude`: non-background pixels that are NOT this marker (zero alpha).
    """
    ys, xs = np.nonzero(sil)
    y0, y1 = max(0, ys.min() - 2), min(sil.shape[0], ys.max() + 3)
    x0, x1 = max(0, xs.min() - 2), min(sil.shape[1], xs.max() + 3)
    crop = sub[y0:y1, x0:x1].astype(float)
    d = np.sqrt(((crop - np.array(bg, float)) ** 2).sum(2))
    region = cv2.dilate(sil[y0:y1, x0:x1].astype(np.uint8),
                        np.ones((3, 3), np.uint8)).astype(bool)
    if exclude is not None:                        # e.g. a line cut off it
        region &= ~exclude[y0:y1, x0:x1]
    ref = np.percentile(d[sil[y0:y1, x0:x1]], 90)
    alpha = np.where(region, np.clip(d / max(ref, 1.0), 0, 1), 0).astype(np.float32)
    big = cv2.resize(alpha, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    sil_up = _fill_holes(big >= 0.5).astype(np.uint8)
    # shave ~1 px protrusions (line stubs, JPEG specks): the enclosing
    # circle/rect/triangle fits are sensitive to anything sticking out
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * up + 1, 2 * up + 1))
    return cv2.morphologyEx(sil_up, cv2.MORPH_OPEN, ker).astype(bool)


def sample_marker(rgb: np.ndarray, ix: float, iy: float, win: int = 60,
                  bg_tol: float = 60, snap: int = 6,
                  check_plot: bool = True) -> MarkerTemplate | None:
    """Cut out the marker under / near image pixel (ix, iy) and describe it.

    Background = most common colour in a window around the click; the marker
    = the connected non-background blob at (or within `snap` px of) the click.
    Returns None if nothing non-background is near the click.
    """
    if not _HAS_CV2:
        raise RuntimeError("snap marker needs opencv")
    H, W = rgb.shape[:2]
    for half in (win, 2 * win, 4 * win):
        x0, y0 = max(0, int(ix) - half), max(0, int(iy) - half)
        x1, y1 = min(W, int(ix) + half + 1), min(H, int(iy) + half + 1)
        sub = rgb[y0:y1, x0:x1, :3]
        bg = _background_rgb(sub)
        fg = ~color_mask(sub, bg, bg_tol)
        n, lbl = cv2.connectedComponents(fg.astype(np.uint8), connectivity=8)
        cx, cy = int(ix) - x0, int(iy) - y0
        inside = 0 <= cy < lbl.shape[0] and 0 <= cx < lbl.shape[1]
        k = lbl[cy, cx] if inside else 0
        if k == 0 and inside:                      # inside a hollow marker?
            f2 = _fill_holes(fg)
            if f2[cy, cx]:
                n2, l2 = cv2.connectedComponents(f2.astype(np.uint8), connectivity=8)
                ring = (l2 == l2[cy, cx]) & fg
                ks = np.unique(lbl[ring])
                ks = ks[ks > 0]
                if ks.size:
                    k = int(ks[np.argmax([(lbl == q).sum() for q in ks])])
        if k == 0:                                 # clicked just off it: snap
            ys, xs = np.nonzero(fg[max(0, cy - snap):cy + snap + 1,
                                   max(0, cx - snap):cx + snap + 1])
            if xs.size == 0:
                return None
            j = int(np.argmin((xs + max(0, cx - snap) - cx) ** 2
                              + (ys + max(0, cy - snap) - cy) ** 2))
            k = lbl[ys[j] + max(0, cy - snap), xs[j] + max(0, cx - snap)]
        blob = lbl == k
        ys, xs = np.nonzero(blob)
        touches = (xs.min() == 0 or ys.min() == 0 or xs.max() == blob.shape[1] - 1
                   or ys.max() == blob.shape[0] - 1)
        if not touches or half == 4 * win:
            break
    # 'o-' plots: a line runs through the marker -- cut it off first. Thin
    # parts = ridge much narrower than the blob's thickest point; open them
    # away and keep the piece nearest the click.
    b8 = blob.astype(np.uint8)
    dt = cv2.distanceTransform(b8, cv2.DIST_L2, 3)
    ridge = blob & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)) - 1e-6)
    if ridge.sum() >= 5:
        half = float(np.median(dt[ridge]))
        if half < 0.35 * dt.max():
            k = int(2 * math.ceil(half) + 3)
            op = cv2.morphologyEx(b8, cv2.MORPH_OPEN, cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (k, k))).astype(bool)
            if op.any():
                n3, l3 = cv2.connectedComponents(op.astype(np.uint8), connectivity=8)
                yy3, xx3 = np.nonzero(op)
                j = int(np.argmin((xx3 - cx) ** 2 + (yy3 - cy) ** 2))
                blob = l3 == l3[yy3[j], xx3[j]]
    sil = _fill_holes(blob)
    shape, fills = classify_shape(_soft_silhouette(sub, bg, sil,
                                                   exclude=fg & ~sil))

    # colours: outer band = edge, deep interior = face
    k3 = np.ones((3, 3), np.uint8)
    inner = cv2.erode(sil.astype(np.uint8), k3, iterations=2).astype(bool)
    band = sil & ~inner
    deep = cv2.erode(sil.astype(np.uint8), k3,
                     iterations=max(3, int(0.25 * math.sqrt(sil.sum() / math.pi)))
                     ).astype(bool)
    if deep.sum() < 4:
        deep = inner if inner.sum() >= 4 else sil

    def solid(px_):                                # drop antialiased fringe
        d = np.sqrt(((px_.astype(float) - np.array(bg)) ** 2).sum(1))
        # antialiased pixel = a*colour + (1-a)*bg: the truest are the ones
        # farthest from bg (thin diagonal strokes have no fully solid pixel)
        keep = px_[d >= 0.9 * d.max()] if len(px_) else px_
        return tuple(int(v) for v in np.median(keep, axis=0))

    edge = solid(sub[band])
    interior = sub[deep]
    hollow_frac = color_mask(interior[None], bg, bg_tol)[0].mean()
    if hollow_frac > 0.5:
        style, face, detect = "hollow", None, edge
    else:
        face = tuple(int(v) for v in np.median(interior[~color_mask(
            interior[None], bg, bg_tol)[0]], axis=0))
        diff = math.dist(face, edge)
        style = "edged" if diff > 60 else "filled"
        detect = face                              # the face is the big, clean area
    # size of what detection will actually see (face mask for edged markers)
    dmask = color_mask(sub, detect, 60) & sil
    if style == "hollow":
        dmask = _fill_holes(dmask)
    area = float(dmask.sum())
    r = math.sqrt(area / math.pi) if area else 0.0
    ys, xs = np.nonzero(sil)
    size = float(max(np.ptp(xs), np.ptp(ys)) + 1)
    # a snapped pair of merged markers is elongated; single markers aren't
    w_, h_ = np.ptp(xs) + 1, np.ptp(ys) + 1
    note = ""
    if shape == "other":
        note = "shape not recognised (plus / x / star, or merged markers)"
    elif max(w_, h_) >= 1.5 * min(w_, h_):
        note = "elongated -- probably 2+ merged markers"
    elif size < 9:
        note = "marker tiny -- check filled/hollow and colour"
    if check_plot and not note:
        # is this ONE marker? compare with the size the whole plot suggests
        full = color_mask(rgb, detect, 60)
        if style == "hollow":
            full = _fill_holes(full)
        m = estimate_marker(full, quick=True)
        if m is not None and area > 1.6 * m.area:
            note = (f"looks like ~{area / m.area:.0f} merged markers "
                    f"(plot's single marker ~{2 * m.r:.0f}px) -- snap a "
                    f"cleaner one or set shape/size by hand")
    confident = not note
    return MarkerTemplate(shape=shape, style=style, face_rgb=face, edge_rgb=edge,
                          detect_rgb=detect, r=r, size=size, area=area,
                          fills=fills,
                          center=(float(xs.mean() + x0), float(ys.mean() + y0)),
                          confident=bool(confident), note=note)


def extract_points(rgb, cal: Calibration, target, tol, roi=None,
                   area=(6, 4000), shape="any", split_overlaps=False,
                   marker_r=None, min_arc=0.10):
    """Colour-mask -> connected components -> centroids, filtered by pixel area
    and (softly) by marker shape. Returns (data Nx2, pixels Nx2).

    shape="circle" with split_overlaps=True uses extract_circles(): merged /
    stacked markers are split into one point each (area max is then ignored).
    """
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = _clip_roi(roi, w, h)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 2)), np.empty((0, 2))
    if split_overlaps and shape == "circle":
        px, _ = extract_circles(rgb, target, tol, roi, r_hint=marker_r,
                                min_arc=min_arc, amin=area[0])
        if len(px) == 0:
            return np.empty((0, 2)), px
        X, Y = cal.pixel_to_data(px[:, 0], px[:, 1])
        data = np.column_stack([X, Y])
        order = np.argsort(data[:, 0])
        return data[order], px[order]
    mask = color_mask(rgb[y0:y1, x0:x1], target, tol)
    amin, amax = area
    px = []
    for cx, cy, a, sub, _ in _components(mask):
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

    # snap_point: click near (not exactly on) a marker -> should land on it
    tx, ty = px(5, 0.34)                         # the (5, 0.34) blue marker
    snapped = snap_point(rgb, (20, 20, 220), 80, tx + 3, ty - 2, search=8)
    snap_ok = snapped is not None and abs(snapped[0] - tx) <= 4 and abs(snapped[1] - ty) <= 4
    print(f"snap_point: {'ok' if snap_ok else 'FAIL'}  "
          f"(clicked ({tx + 3:.0f},{ty - 2:.0f}), snapped {snapped})")
    ok &= snap_ok
    far = snap_point(rgb, (20, 20, 220), 80, 5, 5, search=6)
    print(f"snap_point (nothing nearby): {'ok' if far is None else 'FAIL'}")
    ok &= far is None

    # overlapping circles: 2 isolated + a chain of 3 + a near-coincident pair
    im2 = Image.new("RGB", (W, H), "white")
    d2 = ImageDraw.Draw(im2)
    rr = 9
    ctr = [(150, 150), (450, 300),                       # isolated
           (250, 250), (262, 256), (274, 250),           # chain, ~0.7r apart
           (380, 120), (384, 123)]                       # 0.55r apart
    for cx, cy in ctr:
        d2.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(200, 30, 30))
    _, sp = extract_points(np.asarray(im2), cal, (200, 30, 30), 60,
                           shape="circle", split_overlaps=True)
    _, op = extract_points(np.asarray(im2), cal, (200, 30, 30), 60,
                           shape="any")
    hit = sum(1 for c in ctr if len(sp) and
              np.hypot(*(sp - np.array(c)).T).min() <= 0.5 * rr)
    print(f"overlap split: {hit}/{len(ctr)} markers, {len(sp)} detections "
          f"(plain blob path: {len(op)})")
    ok &= hit == len(ctr) and len(sp) == len(ctr)

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
