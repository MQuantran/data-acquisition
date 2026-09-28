"""data_acquisition -- MVP GUI for digitizing numbers off a plot image.

Flow:
  1. Paste an image (Ctrl+V) or Open a file.
  2. Fill in dataset name, export format, extract mode (line / points),
     and for points the marker shape.
  3. "Pick curve colour" -> click the curve/markers on the canvas.
  4. (optional) "Set plot area" -> drag a rectangle around the axes box.
  5. "Add calibration ref" -> click the X reference, then the Y reference,
     then type their data values. Do this twice (4 clicks total).
     Wheel = zoom about cursor, middle-drag = pan, magnifier loupe = aim.
  6. "Detect" -> review the overlay marks. Prune strays: "Delete" (click one
     point) or "Eraser" (drag a size-adjustable square to wipe many at once,
     e.g. letters of an annotation the detector picked up). Then "Add point"
     to click in the ones Detect missed -- snaps onto the curve colour if
     one's nearby, else uses the exact click.
  7. "Export".

Run the GUI:        python app.py
Headless check:     python app.py --selftest
"""
from __future__ import annotations

import os
import sys
import datetime as _dt
import threading
import webbrowser

import numpy as np

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from PIL import Image, ImageTk, ImageGrab

import core
import updater
import appconfig

VERSION = "0.5.0"

MAG_SRC = 41          # source pixels shown in the magnifier
MAG_VIEW = 205        # magnifier canvas size (px)  -> 5x
MAX_ZOOM = 32.0       # only the visible viewport is ever rendered, so this
                      # is a usability cap, not a memory one
VIEW_MARGIN = 256     # canvas px rendered beyond each viewport edge (pan headroom)
VIEW_TOPUP_MS = 150   # after a zoom, render the margin once the wheel is idle
XH = "#00cc66"        # crosshair colour, normal
XH_CAL = "#ff8c00"    # crosshair colour, calibration click pending

GUIDE = (
    "1.  Paste an image (Ctrl+V) or  File-style  Open.  Type a dataset name.\n"
    "2.  Choose export format; pick Line or Points (and a marker shape).\n"
    "    Circle markers stacked on each other: keep 'Split overlapping\n"
    "    markers' ticked -- each marker in a merged blob is found from its\n"
    "    visible arc of edge.  Set the marker radius if auto gets it wrong.\n"
    "3.  Pick curve colour -> click the curve / a marker. Tune the tolerance.\n"
    "    Markers: use 'Snap marker' instead -> click one clean marker; shape,\n"
    "    filled/hollow/edged, colour and size are all set from it.\n"
    "4.  (optional) Set plot area -> drag a box around the axes.\n"
    "5.  Add calibration ref -> click the X reference, then the Y reference,\n"
    "    then type their values.  Do this TWICE (2 refs, 4 clicks).\n"
    "    Mouse-wheel = zoom about cursor, middle-drag = pan,\n"
    "    the magnifier (bottom-left, 5x) is for precise aiming.\n"
    "6.  Detect -> check the marks ('Mark colour: auto' picks a colour\n"
    "    that stands out from your markers).  Prune strays with 'Delete'\n"
    "    (click one point) or 'Eraser' (drag a size-adjustable square to\n"
    "    wipe a whole clump at once, e.g. an annotation read as points).\n"
    "    Then 'Add point' -> click to fill in the ones Detect missed; it\n"
    "    snaps onto the picked curve colour if one is close by, else it\n"
    "    uses the exact click.  Added points can be Deleted again too.\n"
    "7.  Export.\n\n"
    "Tip: a tight plot-area box (step 4) that excludes the legend and tick\n"
    "labels makes detection much cleaner."
)


def resource_path(rel: str) -> str:
    """Path to a bundled data file, dev or PyInstaller-frozen."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _console_print(msg: str) -> None:
    """Print to a console if there is one; a windowed .exe has no stdout."""
    try:
        if sys.stdout is not None:
            print(msg)
            sys.stdout.flush()
            return
    except Exception:
        pass
    try:
        r = tk.Tk(); r.withdraw()
        messagebox.showinfo("data_acquisition", msg)
        r.destroy()
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = appconfig.load()
        self._upd_busy = False
        root.title(f"data_acquisition {VERSION}  --  plot digitizer")
        root.geometry(self.cfg.get("window_geometry") or "1180x760")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            root.iconbitmap(resource_path(os.path.join("assets", "icon.ico")))
        except Exception:
            pass

        # ---- state ----
        self.base_img: Image.Image | None = None
        self.image_name: str | None = None
        self.tkimg = None
        self.zoom = 1.0
        self._view = None             # (zoom, x0, y0, x1, y1, margin) of the rendered tile
        self._view_job = None         # pending margin top-up (after id)
        self._photos = {}             # tile size -> reusable PhotoImage
        self._sprite = None           # ((col, halo), RGBA point-mark stamp)
        self.mode = "normal"          # normal|pick_color|snap_marker|set_roi|calib_x|calib_y|delete|erase|add
        self.target = None            # (r,g,b)
        self.snap_template = None     # core.MarkerTemplate from "Snap marker"
        self._bg_cache = None         # (image, background rgb) for auto marks
        self.roi = None               # (x0,y0,x1,y1) in image px
        self._roi_start = None
        self.refs: list[dict] = []    # calibration references
        self._cal_px = None
        self.calibration: core.Calibration | None = None
        self.det_data = None          # Nx2 ndarray
        self.det_px: list[tuple] = [] # list of (ix,iy)

        self._build_menu()
        self._build_ui()
        self._bind()
        self._refresh_controls()
        self._status("Paste an image (Ctrl+V) or use  Open.")

        if self.cfg.get("check_updates_at_startup", True):
            self.root.after(1500, lambda: self._check_updates(startup=True))

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        mb = tk.Menu(self.root)
        filem = tk.Menu(mb, tearoff=0)
        filem.add_command(label="Open image…", command=self.on_open,
                          accelerator="Ctrl+O")
        filem.add_command(label="Paste image", command=self.on_paste,
                          accelerator="Ctrl+V")
        filem.add_separator()
        filem.add_command(label="Export…", command=self.on_export,
                          accelerator="Ctrl+S")
        filem.add_separator()
        filem.add_command(label="Quit", command=self.root.destroy)
        mb.add_cascade(label="File", menu=filem)
        helpm = tk.Menu(mb, tearoff=0)
        helpm.add_command(label="Quick guide", command=self._show_guide)
        helpm.add_command(label="About", command=self._show_about)
        helpm.add_separator()
        helpm.add_command(label="Check for updates…",
                          command=self._check_updates)
        self._startup_upd_var = tk.BooleanVar(
            value=bool(self.cfg.get("check_updates_at_startup", True)))
        helpm.add_checkbutton(label="Check for updates at startup",
                              variable=self._startup_upd_var,
                              command=self._toggle_startup_check)
        mb.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=mb)
        self.root.bind("<Control-o>", lambda e: self.on_open())
        self.root.bind("<Control-s>", lambda e: self.on_export())

    def _show_about(self):
        messagebox.showinfo(
            f"data_acquisition {VERSION}",
            "data_acquisition -- plot digitizer\n"
            f"version {VERSION}\n\n"
            "Paste a plot image, calibrate the axes with the magnifier, and "
            "pull the numbers back out as CSV / TSV / JSON.\n\n"
            "UniMelb Mech Eng -- cryogenic SSN group.\n"
            "Digitizing runs offline -- no image or export data leaves this "
            "machine. 'Check for updates' (Help menu) is the only feature that "
            "uses the network: it reads the public releases list at\n"
            f"github.com/{updater.REPO}.")

    def _show_guide(self):
        top = tk.Toplevel(self.root)
        top.title("Quick guide")
        top.transient(self.root)
        txt = tk.Text(top, width=74, height=20, wrap="word", padx=10, pady=10)
        txt.insert("1.0", GUIDE)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        ttk.Button(top, text="Close", command=top.destroy).pack(pady=6)

    # ------------------------------------------------------------- updates
    def _toggle_startup_check(self):
        on = bool(self._startup_upd_var.get())
        self.cfg = appconfig.save(check_updates_at_startup=on)

    def _check_updates(self, startup: bool = False):
        """Ask GitHub (in a worker thread) whether a newer release exists."""
        if self._upd_busy:
            return
        self._upd_busy = True
        if not startup:
            self._status("Checking for updates…")

        def work():
            res = updater.check_for_update(VERSION)
            try:
                self.root.after(0, lambda: self._update_done(res, startup))
            except tk.TclError:
                pass          # window closed before the check returned

        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, res: "updater.UpdateResult", startup: bool):
        self._upd_busy = False

        if res.status == "update":
            notes = res.notes or ""
            if len(notes) > 800:
                notes = notes[:800].rstrip() + "\n…"
            msg = (f"Version {res.latest} is available — you have {VERSION}.\n\n"
                   + (notes + "\n\n" if notes else "")
                   + "Open the download page in your browser?")
            self._status(f"Update {res.latest} available — Help ▸ Check for updates.")
            if messagebox.askyesno("Update available", msg):
                webbrowser.open(res.page_url or res.download_url
                                or "https://github.com/" + updater.REPO
                                + "/releases/latest")
            return

        if startup:
            # On startup, stay quiet unless there is actually an update.
            self._status("Up to date." if res.status == "current" else
                         "Paste an image (Ctrl+V) or use  Open.")
            return

        if res.status == "current":
            messagebox.showinfo(
                "No update", f"You have the latest version ({VERSION}).")
        elif res.status == "offline":
            messagebox.showinfo("Check for updates", res.message)
        else:  # no_release / error
            messagebox.showwarning("Check for updates", res.message)
        self._status("")

    def _on_close(self):
        try:
            appconfig.save(window_geometry=self.root.geometry())
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = self.root
        left = ttk.Frame(root, padding=8)
        left.pack(side="left", fill="y")
        right = ttk.Frame(root)
        right.pack(side="right", fill="both", expand=True)

        def hdr(txt):
            ttk.Label(left, text=txt, font=("", 9, "bold")).pack(
                anchor="w", pady=(10, 2))

        # --- source ---
        hdr("1. Image")
        rowf = ttk.Frame(left); rowf.pack(fill="x")
        ttk.Button(rowf, text="Paste (Ctrl+V)", command=self.on_paste).pack(
            side="left", fill="x", expand=True)
        ttk.Button(rowf, text="Open…", command=self.on_open).pack(
            side="left")
        ttk.Label(left, text="Dataset name").pack(anchor="w", pady=(6, 0))
        self.name_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.name_var).pack(fill="x")

        # --- export / mode ---
        hdr("2. Output")
        ttk.Label(left, text="Export format").pack(anchor="w")
        self.fmt_var = tk.StringVar(value=core.FORMATS[0])
        ttk.OptionMenu(left, self.fmt_var, core.FORMATS[0],
                       *core.FORMATS).pack(fill="x")

        ttk.Label(left, text="Extract").pack(anchor="w", pady=(6, 0))
        self.mode_var = tk.StringVar(value="line")
        mrow = ttk.Frame(left); mrow.pack(fill="x")
        ttk.Radiobutton(mrow, text="Line", value="line",
                        variable=self.mode_var,
                        command=self._refresh_controls).pack(side="left")
        ttk.Radiobutton(mrow, text="Points", value="points",
                        variable=self.mode_var,
                        command=self._refresh_controls).pack(side="left")

        ttk.Label(left, text="Marker shape").pack(anchor="w")
        self.shape_var = tk.StringVar(value="any")
        self.shape_menu = ttk.OptionMenu(
            left, self.shape_var, "any",
            "any", "circle", "square", "triangle", "diamond")
        self.shape_menu.pack(fill="x")

        # --- colour ---
        hdr("3. Curve colour")
        crow = ttk.Frame(left); crow.pack(fill="x")
        self.pick_btn = ttk.Button(crow, text="Pick curve colour",
                                   command=lambda: self._set_mode("pick_color"))
        self.pick_btn.pack(side="left", fill="x", expand=True)
        self.swatch = tk.Label(crow, text="   ", bg="#dddddd", relief="sunken",
                               width=4)
        self.swatch.pack(side="left", padx=4)
        # one click on a marker: shape + style + colours + size, all set
        ttk.Button(left, text="Snap marker (click one clean marker)",
                   command=lambda: self._set_mode("snap_marker")).pack(fill="x")
        self.snap_lbl = ttk.Label(left, text="", wraplength=230,
                                  foreground="#555555")
        self.snap_lbl.pack(anchor="w")
        ttk.Label(left, text="Colour tolerance").pack(anchor="w")
        self.tol_var = tk.IntVar(value=60)
        ttk.Scale(left, from_=5, to=180, variable=self.tol_var,
                  orient="horizontal").pack(fill="x")

        self.roi_btn = ttk.Button(left, text="Set plot area (drag)",
                                  command=lambda: self._set_mode("set_roi"))
        self.roi_btn.pack(fill="x", pady=(4, 0))
        ttk.Button(left, text="Clear plot area",
                   command=self._clear_roi).pack(fill="x")

        # --- line / point tuning ---
        self.tune = ttk.Frame(left)
        self.tune.pack(fill="x", pady=(4, 0))
        self.step_row = ttk.Frame(self.tune)
        ttk.Label(self.step_row, text="Line x-step (px)").pack(anchor="w")
        self.step_var = tk.IntVar(value=2)
        ttk.Scale(self.step_row, from_=1, to=12, variable=self.step_var,
                  orient="horizontal").pack(fill="x")
        self.area_row = ttk.Frame(self.tune)
        ttk.Label(self.area_row, text="Marker area min / max (px)").pack(
            anchor="w")
        self.amin_var = tk.IntVar(value=6)
        self.amax_var = tk.IntVar(value=4000)
        ttk.Scale(self.area_row, from_=1, to=300, variable=self.amin_var,
                  orient="horizontal").pack(fill="x")
        ttk.Scale(self.area_row, from_=100, to=20000, variable=self.amax_var,
                  orient="horizontal").pack(fill="x")
        # circle markers only: split stacked / overlapping markers
        self.split_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.area_row,
                        text="Split overlapping markers (circle)",
                        variable=self.split_var).pack(anchor="w")
        rrow = ttk.Frame(self.area_row); rrow.pack(fill="x")
        ttk.Label(rrow, text="Marker radius px (0 = auto)").pack(side="left")
        self.mr_var = tk.DoubleVar(value=0.0)
        ttk.Spinbox(rrow, from_=0, to=100, increment=0.5, width=6,
                    textvariable=self.mr_var).pack(side="right")

        # --- calibration ---
        hdr("4. Calibration  (2 refs = 4 clicks)")
        ttk.Button(left, text="Add calibration ref",
                   command=self._start_calib).pack(fill="x")
        self.cal_lbl = ttk.Label(left, text="0 / 2 references")
        self.cal_lbl.pack(anchor="w")
        lrow = ttk.Frame(left); lrow.pack(fill="x")
        self.xlog_var = tk.BooleanVar(value=False)
        self.ylog_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lrow, text="X log", variable=self.xlog_var,
                        command=self._rebuild_cal).pack(side="left")
        ttk.Checkbutton(lrow, text="Y log", variable=self.ylog_var,
                        command=self._rebuild_cal).pack(side="left")
        ttk.Button(left, text="Clear calibration",
                   command=self._clear_calib).pack(fill="x")

        # --- detect / export ---
        hdr("5. Detect  &  export")
        self.detect_btn = ttk.Button(left, text="Detect", command=self.on_detect)
        self.detect_btn.pack(fill="x")
        self.count_lbl = ttk.Label(left, text="—")
        self.count_lbl.pack(anchor="w")
        # colour of the detection marks: 'auto' = farthest from the tracked
        # marker colour AND the background, so marks never vanish on markers
        mkrow = ttk.Frame(left); mkrow.pack(fill="x")
        ttk.Label(mkrow, text="Mark colour").pack(side="left")
        self.markcol_var = tk.StringVar(value="auto")
        mk = ttk.Combobox(mkrow, textvariable=self.markcol_var, width=9,
                          state="readonly",
                          values=["auto", "red", "black", "cyan", "magenta"])
        mk.pack(side="right")
        mk.bind("<<ComboboxSelected>>", lambda e: self._redraw_overlays())
        self.del_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Delete: click a marked point",
                        variable=self.del_var,
                        command=lambda: self._toggle_edit_mode("delete")).pack(
            anchor="w")
        self.erase_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Eraser: drag a square",
                        variable=self.erase_var,
                        command=lambda: self._toggle_edit_mode("erase")).pack(
            anchor="w")
        ttk.Label(left, text="Eraser size (px)").pack(anchor="w")
        self.eraser_var = tk.IntVar(value=30)
        ttk.Scale(left, from_=6, to=400, variable=self.eraser_var,
                  orient="horizontal").pack(fill="x")
        self.add_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Add point: click to add (snaps to curve)",
                        variable=self.add_var,
                        command=lambda: self._toggle_edit_mode("add")).pack(
            anchor="w")
        ttk.Button(left, text="Export…", command=self.on_export).pack(
            fill="x", pady=(2, 0))

        # --- canvas ---
        cwrap = ttk.Frame(right)
        cwrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cwrap, bg="#2b2b2b", highlightthickness=0)
        # scrollbars re-render the viewport tile right after scrolling
        hbar = ttk.Scrollbar(cwrap, orient="horizontal", command=lambda *a: (
            self.canvas.xview(*a), self._render_view(VIEW_MARGIN)))
        vbar = ttk.Scrollbar(cwrap, orient="vertical", command=lambda *a: (
            self.canvas.yview(*a), self._render_view(VIEW_MARGIN)))
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        cwrap.rowconfigure(0, weight=1)
        cwrap.columnconfigure(0, weight=1)

        bottom = ttk.Frame(right); bottom.pack(fill="x")
        self.mag = tk.Canvas(bottom, width=MAG_VIEW, height=MAG_VIEW,
                             bg="black", highlightthickness=1,
                             highlightbackground="#666")
        self.mag.pack(side="left", padx=4, pady=4)
        info = ttk.Frame(bottom); info.pack(side="left", fill="x", expand=True)
        self.zoom_var = tk.StringVar(value="100%")
        ttk.Label(info, textvariable=self.zoom_var).pack(anchor="w")
        self.coord_var = tk.StringVar(value="cursor:  —")
        ttk.Label(info, textvariable=self.coord_var,
                  font=("Consolas", 9)).pack(anchor="w")
        self.status_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x")

    def _bind(self):
        c = self.canvas
        c.bind("<Motion>", self.on_motion)
        c.bind("<Button-1>", self.on_click)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<Button-4>", lambda e: self.on_wheel(e, 120))
        c.bind("<Button-5>", lambda e: self.on_wheel(e, -120))
        c.bind("<ButtonPress-2>", lambda e: c.scan_mark(e.x, e.y))
        c.bind("<B2-Motion>", self.on_pan)
        c.bind("<Configure>", lambda e: self._render_view(VIEW_MARGIN))
        self.root.bind("<Control-v>", self.on_paste)
        self.root.bind("<Control-V>", self.on_paste)

    # ------------------------------------------------------------ helpers
    def _status(self, txt):
        self.status_var.set(txt)

    def _refresh_controls(self):
        pts = self.mode_var.get() == "points"
        state = "normal" if pts else "disabled"
        self.shape_menu.configure(state=state)
        if pts:
            self.step_row.pack_forget()
            self.area_row.pack(fill="x")
        else:
            self.area_row.pack_forget()
            self.step_row.pack(fill="x")

    def _set_mode(self, m):
        if self.base_img is None:
            messagebox.showinfo("No image", "Load an image first.")
            return
        self.mode = m
        hints = {
            "pick_color": "Click the curve / a marker to sample its colour.",
            "snap_marker": "Click ONE isolated marker (anywhere on it, or just "
                           "next to it). Shape, style, colour and size are "
                           "read off it and set for Points detection.",
            "set_roi": "Drag a rectangle around the axes box.",
            "calib_x": "Calibration: click the X reference "
                       "(e.g. an x-axis tick).",
            "calib_y": "Calibration: now click the Y reference "
                       "(e.g. a y-axis tick).",
            "delete": "Delete mode: click a marked point to remove it.",
            "erase": "Eraser: click / drag the square to wipe every point "
                     "inside it. Adjust 'Eraser size'.",
            "add": "Add point: click on the plot. Snaps onto the nearest "
                  "curve-colour pixel if one is close by, else uses the "
                  "exact click. Added points can be removed again with "
                  "Delete, same as detected ones.",
            "normal": "",
        }
        self._status(hints.get(m, ""))

    def img_xy(self, event):
        x = self.canvas.canvasx(event.x) / self.zoom
        y = self.canvas.canvasy(event.y) / self.zoom
        return x, y

    # ------------------------------------------------------------ image io
    def on_paste(self, event=None):
        try:
            obj = ImageGrab.grabclipboard()
        except Exception as e:                         # pragma: no cover
            messagebox.showerror("Paste failed", str(e)); return
        if isinstance(obj, list) and obj:
            try:
                self._load(Image.open(obj[0]), os.path.basename(obj[0]))
            except Exception as e:
                messagebox.showerror("Paste failed", str(e))
        elif obj is not None and hasattr(obj, "size"):
            self._load(obj, None)
        else:
            messagebox.showinfo("Paste", "No image found on the clipboard.")

    def on_open(self):
        fp = filedialog.askopenfilename(
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff"),
                       ("All files", "*.*")])
        if fp:
            try:
                self._load(Image.open(fp), os.path.basename(fp))
            except Exception as e:
                messagebox.showerror("Open failed", str(e))

    def _load(self, img: Image.Image, name):
        self.base_img = img.convert("RGB")
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.image_name = name or f"pasted_{ts}.png"
        if not self.name_var.get():
            self.name_var.set(os.path.splitext(self.image_name)[0])
        self.target = None
        self.snap_template = None
        self.swatch.configure(bg="#dddddd")
        if hasattr(self, "snap_lbl"):
            self.snap_lbl.configure(text="")
        self.roi = None
        self.refs.clear()
        self.calibration = None
        self.det_data = None
        self.det_px = []
        self.mode = "normal"
        self.del_var.set(False)
        self.erase_var.set(False)
        self.add_var.set(False)
        self.count_lbl.configure(text="—")
        self._update_cal_label()
        self.root.update_idletasks()
        W, H = self.base_img.size
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 300)
        self.zoom = min(cw / W, ch / H, 1.0)
        self.zoom = max(self.zoom, 0.05)
        self._render()
        self._redraw_overlays(marks=False)
        self._status(f"Loaded {self.image_name}  ({W}x{H}px).  "
                     f"Pick a colour, set calibration, then Detect.")

    # ------------------------------------------------------------ rendering
    # The scrollregion always spans the whole zoomed image (canvas coords =
    # image px * zoom), so img_xy / overlays / scrollbars never see the
    # tiling. Only the visible viewport (+ VIEW_MARGIN) is actually resized
    # into a PhotoImage and placed at its canvas offset -- the cost of a
    # zoom step no longer grows with zoom^2.
    def _set_zoom_region(self):
        """Clamp the zoom and size the scrollregion to the zoomed image."""
        W, H = self.base_img.size
        self.zoom = min(self.zoom, MAX_ZOOM)
        dw, dh = max(1, int(W * self.zoom)), max(1, int(H * self.zoom))
        self.canvas.configure(scrollregion=(0, 0, dw, dh))
        self.zoom_var.set(f"{self.zoom * 100:.0f}%")

    def _render(self):
        if self.base_img is None:
            return
        self._set_zoom_region()
        self._render_view(0, force=True)
        self._schedule_view_topup()

    def _render_view(self, margin=0, force=False):
        """Render the image tile covering the viewport (+ `margin` canvas px).

        No-op when the current tile, at this zoom, already covers the
        viewport (and at least `margin` of headroom was asked for before).
        """
        if self.base_img is None:
            return
        c = self.canvas
        W, H = self.base_img.size
        z = self.zoom
        dw, dh = max(1, int(W * z)), max(1, int(H * z))
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1 or ch <= 1:                   # not mapped yet (e.g. selftest)
            cw, ch = c.winfo_reqwidth(), c.winfo_reqheight()
        vx0, vy0 = int(c.canvasx(0)), int(c.canvasy(0))
        need = (max(0, vx0), max(0, vy0), min(dw, vx0 + cw), min(dh, vy0 + ch))
        v = self._view
        if (not force and v is not None and v[0] == z and v[5] >= margin
                and v[1] <= need[0] and v[2] <= need[1]
                and v[3] >= need[2] and v[4] >= need[3]):
            return
        # Keep the tile size fixed so the (cached) PhotoImage can be reused
        # -- building a new one costs ~2x a paste: near an image edge the
        # tile slides inward instead of shrinking, and a zoomed image
        # narrower / shorter than the window is padded with the canvas
        # colour to the window size (unless it's small: a fresh small
        # PhotoImage is cheaper than a window-sized paste).
        tw, th = min(cw + 2 * margin, dw), min(ch + 2 * margin, dh)
        if 2 * tw * th > cw * ch:
            tw, th = max(tw, cw), max(th, ch)
        x0 = max(0, min(vx0 - margin, dw - tw))
        y0 = max(0, min(vy0 - margin, dh - th))
        x1, y1 = x0 + tw, y0 + th
        if z >= 1:
            # canvas px cx shows source px floor(cx / z) -- exactly the pixel
            # img_xy() reports for a click there (tiny eps: fp at boundaries)
            eps = 1e-6
            aff = (1 / z, 0, (x0 - 0.5) / z + eps, 0, 1 / z, (y0 - 0.5) / z + eps)
            try:
                # Pillow internals: transform straight into a single memory
                # block, which ImageTk.PhotoImage.paste can hand to Tk as
                # is (it otherwise makes a full copy first: ~5 ms/frame)
                tile = Image.new("RGB", (1, 1))._new(
                    Image.core.new_block("RGB", (tw, th)))
                tile.im.transform((0, 0, tw, th), self.base_img.im,
                                  Image.Transform.AFFINE, aff,
                                  Image.Resampling.NEAREST, 1)
            except Exception:                    # other Pillow: public API
                tile = self.base_img.transform((tw, th), Image.AFFINE, aff,
                                               resample=Image.NEAREST)
        else:
            ex, ey = min(x1, dw), min(y1, dh)
            tile = Image.new("RGB", (tw, th))
            tile.paste(self.base_img.resize(
                (ex - x0, ey - y0), Image.BILINEAR,
                box=(x0 / z, y0 / z, min(W, ex / z), min(H, ey / z))))
        # blank whatever lies past the zoomed image's edge (padding)
        bg = tuple(v >> 8 for v in c.winfo_rgb(c.cget("bg")))
        if x1 > dw:
            tile.paste(bg, (dw - x0, 0, tw, th))
        if y1 > dh:
            tile.paste(bg, (0, dh - y0, tw, th))
        self._view = (z, x0, y0, x1, y1, margin)
        self._show_tile(tile, x0, y0)

    def _show_tile(self, tile, x0, y0):
        """Stamp the point marks onto a fresh tile and put it on screen.

        Marks are baked into the tile rather than being 4 canvas items per
        point: Tk redraws every visible item on each scroll / zoom, which
        for 300 points was ~20 ms per frame on its own. Stamped in place
        (no unmarked copy kept); a point edit re-renders the tile instead.
        """
        c = self.canvas
        if self.det_px:
            col, halo = self._mark_colours()
            sp = self._mark_sprite(col, halo)
            r = sp.width // 2
            p = np.asarray(self.det_px, float) * self.zoom
            p = np.floor(p - (x0, y0)).astype(int)
            inb = ((p[:, 0] > -r - 1) & (p[:, 0] < tile.width + r)
                   & (p[:, 1] > -r - 1) & (p[:, 1] < tile.height + r))
            for (px, py) in p[inb]:
                tile.paste(sp, (int(px) - r, int(py) - r), sp)
        self.tkimg = self._tile_photo(tile.size)
        self.tkimg.paste(tile)
        if c.find_withtag("IMG"):
            c.itemconfigure("IMG", image=self.tkimg)
            c.coords("IMG", x0, y0)
        else:
            c.create_image(x0, y0, anchor="nw", image=self.tkimg, tags="IMG")
        c.tag_lower("IMG")

    def _mark_sprite(self, col, halo):
        """RGBA stamp of one point mark: halo ring, ring, centre cross.

        Same look as the old canvas items (8 px ring + 3 px halo in the
        opposite shade, 4 px centre cross), centred on pixel (r, r).
        """
        key = (col, halo)
        if self._sprite is None or self._sprite[0] != key:
            from PIL import ImageDraw
            s = Image.new("RGBA", (13, 13), (0, 0, 0, 0))
            d = ImageDraw.Draw(s)
            k = 6
            # halo underneath in the opposite shade keeps the mark readable
            # on any mix of marker / background / gridline pixels
            d.ellipse([k - 5, k - 5, k + 5, k + 5], outline=halo, width=3)
            d.ellipse([k - 4, k - 4, k + 4, k + 4], outline=col, width=1)
            d.line([(k - 1, k), (k + 2, k)], fill=col)
            d.line([(k, k - 1), (k, k + 2)], fill=col)
            self._sprite = (key, s)
        return self._sprite[1]

    def _tile_photo(self, size):
        """A reusable PhotoImage of `size` (the 2 most recent sizes kept).

        Pasting into a PhotoImage Tk is already displaying is ~2x cheaper
        than building a new one (Tk updates its display instance in place
        instead of allocating one). Tile sizes alternate between viewport
        and viewport + margin, so each cached photo is pinned by a hidden
        canvas item to keep its display instance alive while not shown.
        """
        p = self._photos.pop(size, None)
        fresh = p is None
        if fresh:
            p = ImageTk.PhotoImage("RGB", size)
        self._photos[size] = p                   # (re)insert = most recent
        while len(self._photos) > 2:
            del self._photos[next(iter(self._photos))]
            fresh = True
        if fresh:
            c = self.canvas
            c.delete("IMGKEEP")
            for q in self._photos.values():
                c.create_image(0, 0, anchor="nw", image=q, state="hidden",
                               tags="IMGKEEP")
        return p

    def _schedule_view_topup(self):
        """Add the pan margin once zooming pauses (keeps wheel ticks cheap)."""
        if self._view_job is not None:
            self.root.after_cancel(self._view_job)

        def run():
            self._view_job = None
            self._render_view(VIEW_MARGIN)
        self._view_job = self.root.after(VIEW_TOPUP_MS, run)

    def _redraw_overlays(self, marks=True):
        """ROI box + calibration lines as canvas items; `marks` also re-stamps
        the point marks (they are baked into the image tile)."""
        c = self.canvas
        c.delete("OV")
        z = self.zoom
        if self.roi:
            x0, y0, x1, y1 = (v * z for v in self.roi)
            c.create_rectangle(x0, y0, x1, y1, outline="#2ca02c",
                               dash=(5, 3), width=2, tags="OV")
        sr = c.cget("scrollregion").split()
        if len(sr) == 4:
            _, _, W2, H2 = map(float, sr)
            for r in self.refs:
                c.create_line(r["px"] * z, 0, r["px"] * z, H2,
                              fill="#3b7dd8", dash=(2, 2), tags="OV")
                c.create_line(0, r["py"] * z, W2, r["py"] * z,
                              fill="#3b7dd8", dash=(2, 2), tags="OV")
        if marks and self._view is not None:     # marks live in the tile
            self._render_view(0, force=True)     # viewport now, margin later
            self._schedule_view_topup()

    def _mark_colours(self):
        """(mark colour, halo colour) as Tk hex strings."""
        name = self.markcol_var.get()
        if name == "auto":
            avoid = [self.target]
            if self.base_img is not None:
                if self._bg_cache is None or self._bg_cache[0] is not self.base_img:
                    small = np.asarray(self.base_img.convert("RGB").resize(
                        (min(400, self.base_img.width),
                         min(300, self.base_img.height))))
                    self._bg_cache = (self.base_img, core._background_rgb(small))
                avoid.append(self._bg_cache[1])
            name = core.best_mark_colour(avoid)
        rgb = core.MARK_COLOURS[name]
        lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        halo = "#000000" if lum > 140 else "#ffffff"
        return "#%02x%02x%02x" % rgb, halo

    # ------------------------------------------------------------ mouse
    def on_motion(self, event):
        if self.base_img is None:
            return
        c = self.canvas
        x, y = c.canvasx(event.x), c.canvasy(event.y)
        c.delete("XH")
        sr = c.cget("scrollregion").split()
        if len(sr) == 4:
            _, _, W2, H2 = map(float, sr)
            col = XH_CAL if self.mode in ("calib_x", "calib_y") else XH
            c.create_line(x, 0, x, H2, fill=col, tags="XH")
            c.create_line(0, y, W2, y, fill=col, tags="XH")
        ix, iy = x / self.zoom, y / self.zoom
        c.delete("ERASER")
        if self.mode == "erase":
            h = self.eraser_var.get() / 2.0 * self.zoom
            c.create_rectangle(x - h, y - h, x + h, y + h,
                               outline="#ff2d2d", width=2, dash=(3, 2),
                               tags="ERASER")
        self._update_magnifier(ix, iy)

    def on_wheel(self, event, delta=None):
        if self.base_img is None:
            return
        d = delta if delta is not None else event.delta
        ix, iy = self.img_xy(event)
        factor = 1.25 if d > 0 else 0.8
        new = min(max(self.zoom * factor, 0.05), MAX_ZOOM)
        if abs(new - self.zoom) < 1e-6:
            return
        self.zoom = new
        self._set_zoom_region()
        W, H = self.base_img.size
        sw, sh = W * self.zoom, H * self.zoom
        self.canvas.xview_moveto(max(0.0, (ix * self.zoom - event.x) / sw))
        self.canvas.yview_moveto(max(0.0, (iy * self.zoom - event.y) / sh))
        # tile for the NEW view only (no margin), margin added once idle
        self._render_view(0, force=True)
        self._schedule_view_topup()
        self._redraw_overlays(marks=False)

    def on_pan(self, event):
        """Middle-drag pan; re-tile only when the view leaves the tile."""
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self._render_view(VIEW_MARGIN)

    def on_click(self, event):
        if self.base_img is None:
            return
        ix, iy = self.img_xy(event)
        W, H = self.base_img.size
        ixc, iyc = min(max(ix, 0), W - 1), min(max(iy, 0), H - 1)

        if self.mode == "pick_color":
            self.target = tuple(int(v) for v in
                                self.base_img.getpixel((int(ixc), int(iyc))))
            self.swatch.configure(bg="#%02x%02x%02x" % self.target)
            self._set_mode("normal")
            self._status(f"colour = rgb{self.target}")
        elif self.mode == "snap_marker":
            self._snap_marker_at(ixc, iyc)
        elif self.mode == "set_roi":
            self._roi_start = (ixc, iyc)
        elif self.mode == "calib_x":
            self._cal_px = ix
            self._set_mode("calib_y")
        elif self.mode == "calib_y":
            self._finish_calib_ref(ix, iy)
        elif self.mode == "delete":
            self._delete_near(ix, iy)
        elif self.mode == "erase":
            self._erase_at(ix, iy)
        elif self.mode == "add":
            self._add_point_at(ixc, iyc)

    def on_drag(self, event):
        if self.mode == "erase":
            ix, iy = self.img_xy(event)
            self._erase_at(ix, iy)
            self.on_motion(event)            # keep the square following the cursor
            return
        if self.mode == "set_roi" and self._roi_start is not None:
            ix, iy = self.img_xy(event)
            x0, y0 = self._roi_start
            self.roi = (x0, y0, ix, iy)
            self._redraw_overlays(marks=False)

    def on_release(self, event):
        if self.mode == "set_roi" and self._roi_start is not None:
            ix, iy = self.img_xy(event)
            x0, y0 = self._roi_start
            self.roi = tuple(map(float, (min(x0, ix), min(y0, iy),
                                         max(x0, ix), max(y0, iy))))
            self._roi_start = None
            self._set_mode("normal")
            self._redraw_overlays(marks=False)
            self._status(f"plot area set: {tuple(round(v) for v in self.roi)}")

    # ------------------------------------------------------------ magnifier
    def _update_magnifier(self, ix, iy):
        if self.base_img is None:
            return
        s, half = MAG_SRC, MAG_SRC // 2
        W, H = self.base_img.size
        x0, y0 = int(round(ix)) - half, int(round(iy)) - half
        crop = Image.new("RGB", (s, s), (30, 30, 30))
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1, sy1 = min(W, x0 + s), min(H, y0 + s)
        if sx1 > sx0 and sy1 > sy0:
            crop.paste(self.base_img.crop((sx0, sy0, sx1, sy1)),
                       (sx0 - x0, sy0 - y0))
        view = crop.resize((MAG_VIEW, MAG_VIEW), Image.NEAREST)
        self._mag_tk = ImageTk.PhotoImage(view)
        m = self.mag
        m.delete("all")
        m.create_image(0, 0, anchor="nw", image=self._mag_tk)
        cc = MAG_VIEW // 2
        m.create_line(cc, 0, cc, MAG_VIEW, fill="#00ff88")
        m.create_line(0, cc, MAG_VIEW, cc, fill="#00ff88")
        p = MAG_VIEW / s
        m.create_rectangle(cc - p / 2, cc - p / 2, cc + p / 2, cc + p / 2,
                           outline="red")
        if self.calibration is not None:
            try:
                X, Y = self.calibration.pixel_to_data(ix, iy)
                self.coord_var.set(f"cursor:  x = {float(X):.5g}"
                                   f"    y = {float(Y):.5g}"
                                   f"    (px {ix:.0f},{iy:.0f})")
            except Exception:
                self.coord_var.set(f"cursor:  px {ix:.0f},{iy:.0f}")
        else:
            self.coord_var.set(f"cursor:  px {ix:.0f},{iy:.0f}  "
                               f"(calibrate to see data coords)")

    # ------------------------------------------------------------ calibration
    def _start_calib(self):
        if self.base_img is None:
            messagebox.showinfo("No image", "Load an image first.")
            return
        if len(self.refs) >= 2:
            messagebox.showinfo("Calibration",
                                "Two references already set. Clear first to redo.")
            return
        self._set_mode("calib_x")

    def _finish_calib_ref(self, ix, iy):
        px = self._cal_px
        py = iy
        self.mode = "normal"
        xv = simpledialog.askfloat(
            "Calibration value",
            "X value at the FIRST click  (accepts 1e-3 etc.):",
            parent=self.root)
        if xv is None:
            self._status("calibration ref cancelled"); return
        yv = simpledialog.askfloat(
            "Calibration value",
            "Y value at the SECOND click:", parent=self.root)
        if yv is None:
            self._status("calibration ref cancelled"); return
        self.refs.append({"px": float(px), "py": float(py),
                          "X": float(xv), "Y": float(yv)})
        self._cal_px = None
        self._update_cal_label()
        self._rebuild_cal()
        self._redraw_overlays(marks=False)
        if len(self.refs) < 2:
            self._status("first reference stored. Add the second "
                         "(Add calibration ref).")
        else:
            self._status("calibration complete." if self.calibration
                         else "calibration failed -- see message.")

    def _rebuild_cal(self):
        self.calibration = None
        if len(self.refs) == 2:
            try:
                self.calibration = core.Calibration.from_refs(
                    self.refs, self.xlog_var.get(), self.ylog_var.get())
            except Exception as e:
                messagebox.showerror("Calibration", str(e))

    def _update_cal_label(self):
        self.cal_lbl.configure(text=f"{len(self.refs)} / 2 references")

    def _clear_calib(self):
        self.refs.clear()
        self.calibration = None
        self._cal_px = None
        self.mode = "normal"
        self._update_cal_label()
        self._redraw_overlays(marks=False)

    def _clear_roi(self):
        self.roi = None
        self._redraw_overlays(marks=False)
        self._status("plot area cleared (using whole image).")

    # ------------------------------------------------------------ detect
    def on_detect(self):
        if self.base_img is None or self.target is None \
                or self.calibration is None:
            messagebox.showinfo(
                "Not ready",
                "Need: an image, a picked colour, and 2 calibration refs.")
            return
        rgb = np.asarray(self.base_img)
        tol = float(self.tol_var.get())
        try:
            if self.mode_var.get() == "line":
                data, px = core.extract_line(
                    rgb, self.calibration, self.target, tol, self.roi,
                    x_step=int(self.step_var.get()))
                kind = "samples"
            else:
                amin = int(self.amin_var.get())
                amax = max(amin + 1, int(self.amax_var.get()))
                split = (self.split_var.get()
                         and self.shape_var.get() == "circle")
                try:
                    mr = float(self.mr_var.get())
                except (tk.TclError, ValueError):
                    mr = 0.0
                data, px = core.extract_points(
                    rgb, self.calibration, self.target, tol, self.roi,
                    area=(amin, amax), shape=self.shape_var.get(),
                    split_overlaps=split, marker_r=mr if mr > 0 else None)
                kind = "points" + (" (overlaps split)" if split else "")
        except Exception as e:
            messagebox.showerror("Detection failed", str(e)); return
        self.det_data = data
        self.det_px = [tuple(map(float, p)) for p in px]
        self.count_lbl.configure(text=f"{len(px)} {kind}")
        self._redraw_overlays()
        self._status(f"detected {len(px)} {kind}. Review the marks; "
                     f"prune with Delete / Eraser, then Export.")

    def _toggle_edit_mode(self, which):
        """`delete`, `erase`, `add` are mutually exclusive point-edit modes."""
        edit_vars = {"delete": self.del_var, "erase": self.erase_var,
                    "add": self.add_var}
        on = edit_vars[which].get()
        if on and which == "add" and self.calibration is None:
            messagebox.showinfo(
                "Not ready", "Add calibration (2 references) before adding "
                             "points by hand.")
            edit_vars[which].set(False)
            return
        if on:
            for k, v in edit_vars.items():
                if k != which:
                    v.set(False)
            self.mode = which
        else:
            self.mode = "normal"
            self.canvas.delete("ERASER")
        self._set_mode(self.mode)

    def _keep_mask(self, keep) -> None:
        """Apply a boolean keep-array to the detected points + overlay."""
        keep = np.asarray(keep, bool)
        removed = int((~keep).sum())
        if removed == 0:
            return
        self.det_px = [p for p, k in zip(self.det_px, keep) if k]
        self.det_data = self.det_data[keep]
        self.count_lbl.configure(text=f"{len(self.det_px)} pts "
                                      f"(-{removed})")
        self._redraw_overlays()

    def _delete_near(self, ix, iy):
        if not self.det_px:
            return
        d = [((p[0] - ix) ** 2 + (p[1] - iy) ** 2, i)
             for i, p in enumerate(self.det_px)]
        d.sort()
        if d and d[0][0] <= (14 / self.zoom) ** 2:
            keep = np.ones(len(self.det_px), bool)
            keep[d[0][1]] = False
            self._keep_mask(keep)

    def _erase_at(self, ix, iy):
        """Wipe every detected point inside the square eraser at (ix, iy)."""
        if not self.det_px:
            return
        h = self.eraser_var.get() / 2.0
        p = np.asarray(self.det_px, float)
        keep = ~((np.abs(p[:, 0] - ix) <= h) & (np.abs(p[:, 1] - iy) <= h))
        self._keep_mask(keep)

    def _snap_marker_at(self, ix, iy):
        """Cut out the clicked marker and configure Points detection from it.

        Colour picking sees one colour of a marker (face OR edge); snapping
        takes the whole marker, classifies shape (circle / square / diamond /
        triangle) and style (filled / hollow / edged), and sets: the colour
        to track (face; the stroke for hollow), marker shape, marker radius
        (circle splitting), and the area limits.
        """
        rgb = np.asarray(self.base_img.convert("RGB"))
        try:
            t = core.sample_marker(rgb, ix, iy)
        except Exception as e:
            messagebox.showerror("Snap failed", str(e)); return
        self._set_mode("normal")
        if t is None:
            self._status("snap: nothing but background near the click -- "
                         "click on a marker.")
            return
        self.snap_template = t
        self.target = tuple(int(v) for v in t.detect_rgb)
        self.swatch.configure(bg="#%02x%02x%02x" % self.target)
        self.mode_var.set("points")
        self._refresh_controls()
        # merged / unrecognised snap: its shape + size would mislead the
        # detector -- keep the colour, leave shape/size to the user.
        # (a merely tiny marker still classifies shape reliably)
        trust = t.confident or t.size < 9 and t.shape != "other"
        if trust:
            self.shape_var.set(t.shape)
            self.mr_var.set(round(t.r, 1) if t.shape == "circle" else 0.0)
            a = max(1.0, t.area)
            self.amin_var.set(max(1, int(0.3 * a)))
            self.amax_var.set(int(max(self.amin_var.get() + 1, 3 * a)))
        self.snap_lbl.configure(text="snapped: " + t.describe())
        self._status("snap: " + t.describe() + (
            ".  Now Detect." if trust else
            ".  Colour set; shape/size NOT changed -- snap a cleaner marker "
            "or set them by hand."))

    def _add_point_at(self, ix, iy):
        """Manually add one point at image pixel (ix, iy).

        If a curve colour has been picked, the click snaps to the nearest
        matching-colour pixel within a small window (same precision as
        auto-detect); otherwise it uses the exact click. Used to fill in
        points Detect missed, after pruning the wrong ones with Delete /
        Eraser -- the new point lands in the same `det_px` / `det_data`
        arrays as detected ones, so Delete removes it the same way too.
        """
        if self.calibration is None:
            return
        sx, sy = ix, iy
        snapped = False
        if self.target is not None:
            rgb = np.asarray(self.base_img)
            hit = core.snap_point(rgb, self.target, float(self.tol_var.get()),
                                  ix, iy)
            if hit is not None:
                sx, sy = hit
                snapped = True
        X, Y = self.calibration.pixel_to_data(sx, sy)
        X, Y = float(X), float(Y)
        if self.det_data is None or len(self.det_px) == 0:
            self.det_px = [(sx, sy)]
            self.det_data = np.array([[X, Y]], dtype=float)
        else:
            self.det_px.append((sx, sy))
            self.det_data = np.vstack([self.det_data, [X, Y]])
        order = np.argsort(self.det_data[:, 0])
        self.det_data = self.det_data[order]
        self.det_px = [self.det_px[i] for i in order]
        self.count_lbl.configure(text=f"{len(self.det_px)} pts (+1 manual)")
        self._redraw_overlays()
        self._status(f"added point  x={X:.5g}  y={Y:.5g}"
                     + ("  (snapped to curve)" if snapped else "  (exact click)"))

    # ------------------------------------------------------------ export
    def on_export(self):
        if self.det_data is None or len(self.det_data) == 0:
            messagebox.showinfo("Nothing to export", "Run Detect first.")
            return
        fmt = self.fmt_var.get()
        ext = core.EXT[fmt]
        fp = filedialog.asksaveasfilename(
            defaultextension=ext,
            initialfile=(self.name_var.get() or "dataset") + ext,
            filetypes=[(fmt, "*" + ext), ("All files", "*.*")])
        if not fp:
            return
        meta = core.build_meta(
            self.name_var.get(), self.image_name, self.mode_var.get(),
            self.shape_var.get(), self.xlog_var.get(), self.ylog_var.get(),
            self.calibration, len(self.det_data))
        try:
            core.export_data(fp, self.det_data, fmt, meta)
        except Exception as e:
            messagebox.showerror("Export failed", str(e)); return
        self._status(f"exported {len(self.det_data)} rows -> {fp}")
        messagebox.showinfo("Exported",
                            f"{len(self.det_data)} rows written to\n{fp}")


# --------------------------------------------------------------------------- #
# headless smoke test
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception as e:
        print(f"SKIP (no display: {e})")
        return 0
    from PIL import ImageDraw
    app = App(root)
    app._upd_busy = True          # keep the selftest fully offline
    W, H = 600, 400
    bx0, by0, bx1, by1 = 70, 30, 560, 360
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([bx0, by0, bx1, by1], outline="black")
    for x in np.linspace(0, 10, 300):
        y = 0.1 + 0.02 * x
        fx = bx0 + x / 10 * (bx1 - bx0)
        fy = by1 - y / 0.4 * (by1 - by0)
        d.ellipse([fx - 1, fy - 1, fx + 1, fy + 1], fill=(210, 20, 20))
    app._load(im, "syn.png")
    app.target = (210, 20, 20)
    app.mode_var.set("line")
    app.refs = [{"px": bx0, "py": by1, "X": 0.0, "Y": 0.0},
                {"px": bx1, "py": by0, "X": 10.0, "Y": 0.4}]
    app._rebuild_cal()
    app.roi = (bx0 + 2, by0 + 2, bx1 - 2, by1 - 2)
    app.tol_var.set(70)
    app.on_detect()
    n = len(app.det_px)
    app._update_magnifier(bx0 + 5.0, by1 - 5.0)          # exercise loupe path
    app._redraw_overlays()
    # exercise the square eraser: wipe a 60 px box mid-trace
    mid = app.det_px[len(app.det_px) // 2]
    app.eraser_var.set(60)
    app._erase_at(mid[0], mid[1])
    n_after = len(app.det_px)
    erased_ok = 0 < (n - n_after) <= 40
    n = n_after
    # exercise "Add point": re-add (a couple px off) what was just erased --
    # should snap back onto the curve colour, not the exact off-target click
    add_x_expect, _ = app.calibration.pixel_to_data(mid[0], mid[1])
    app._add_point_at(mid[0] + 2, mid[1] - 1)
    n_after_add = len(app.det_px)
    add_snap_ok = (n_after_add == n_after + 1 and float(np.min(np.abs(
        app.det_data[:, 0] - float(add_x_expect)))) < 0.05)
    # fallback path: no curve colour picked -> exact click, no snapping
    saved_target, app.target = app.target, None
    click_x, click_y = bx0 + 40, by1 - 40
    exp_x, _ = app.calibration.pixel_to_data(click_x, click_y)
    app._add_point_at(click_x, click_y)
    add_manual_ok = (len(app.det_px) == n_after_add + 1 and float(np.min(np.abs(
        app.det_data[:, 0] - float(exp_x)))) < 1e-6)
    app.target = saved_target
    n = len(app.det_px)
    # "Snap marker": edged circle (red face, black edge) + a hollow triangle
    im2 = Image.new("RGB", (300, 200), "white")
    d2 = ImageDraw.Draw(im2)
    d2.ellipse([60, 60, 84, 84], fill=(210, 30, 30), outline=(0, 0, 0), width=2)
    d2.polygon([(200, 60), (188, 84), (212, 84)], outline=(30, 30, 200))
    saved = (app.base_img, app.target)
    app.base_img = im2
    app._snap_marker_at(62, 72)                      # click ON the black edge
    snap_c = (app.shape_var.get() == "circle" and app.mode_var.get() == "points"
              and app.snap_template.style == "edged"
              and abs(app.target[0] - 210) < 30 and app.target[1] < 60)
    app._snap_marker_at(200, 76)                     # click INSIDE the hollow
    snap_t = (app.shape_var.get() == "triangle"
              and app.snap_template.style == "hollow")
    app.base_img, app.target = saved
    app.mode_var.set("line")
    print(f"snap marker: edged circle {'ok' if snap_c else 'FAIL'}   "
          f"hollow triangle {'ok' if snap_t else 'FAIL'}")
    print(f"add point (snap to curve): {'ok' if add_snap_ok else 'FAIL'}   "
          f"add point (no-target fallback): {'ok' if add_manual_ok else 'FAIL'}")
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "_daq_gui_selftest.csv")
    core.export_data(p, app.det_data, "csv", core.build_meta(
        "syn", "syn.png", "line", "any", False, False, app.calibration, n))
    wrote = os.path.getsize(p) > 0
    os.remove(p)
    # updater pure-logic path (no network)
    upd_ok = (updater.is_newer("9.9.9", VERSION)
              and not updater.is_newer(VERSION, VERSION))
    print(f"updater logic {'ok' if upd_ok else 'FAIL'}")
    print(f"GUI selftest: {n} samples after erase "
          f"(calibration {'ok' if app.calibration else 'FAIL'}, "
          f"eraser {'ok' if erased_ok else 'FAIL'}, "
          f"export {'ok' if wrote else 'FAIL'})")
    cal_ok = app.calibration is not None
    view_ok, view_msg = _selftest_view(app)          # loads its own image
    print(f"viewport render: {'ok' if view_ok else 'FAIL'} {view_msg}")
    root.destroy()
    ok = (n > 60 and cal_ok and wrote and erased_ok
          and add_snap_ok and add_manual_ok and upd_ok and snap_c and snap_t
          and view_ok)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _selftest_view(app) -> tuple[bool, str]:
    """Viewport tiling: at several zooms / scroll positions, the image pixel
    img_xy() reports under a canvas point is the expected one, AND the tile
    on screen shows that very source pixel there; point marks land on the
    right spot too. Loads its own image (resets the app state)."""
    import types
    W, H = 160, 120
    yy, xx = np.mgrid[0:H, 0:W]
    arr = np.stack([xx * 3 % 256, yy * 2, (xx * 7 + yy * 13) % 256],
                   -1).astype(np.uint8)
    app._load(Image.fromarray(arr), "grid.png")
    c = app.canvas
    cw, ch = c.winfo_width(), c.winfo_height()
    if cw <= 1 or ch <= 1:                   # withdrawn root: as _render_view
        cw, ch = c.winfo_reqwidth(), c.winfo_reqheight()
    checked = 0
    for z in (0.6, 1.0, 1.7, 2.5, 4.0, 7.3, 16.0, MAX_ZOOM):
        app.zoom = z
        app._render()
        dw, dh = int(W * z), int(H * z)
        for frac in (0.0, 0.37, 0.8):
            c.xview_moveto(frac)
            c.yview_moveto(frac)
            app._render_view(VIEW_MARGIN)            # as scrollbars / pan do
            ox = round(c.xview()[0] * dw)
            oy = round(c.yview()[0] * dh)
            tx, ty = (int(v) for v in c.coords("IMG"))
            for (x, y) in ((0, 0), (cw // 3, ch // 2), (cw - 1, ch - 1),
                           (17, ch // 5)):
                ix, iy = app.img_xy(types.SimpleNamespace(x=x, y=y))
                if abs(ix - (ox + x) / z) > 1e-9 or abs(iy - (oy + y) / z) > 1e-9:
                    return False, f"img_xy z={z} scroll={frac} at ({x},{y})"
                if z < 1 or ix >= W or iy >= H:  # bilinear / off-image: skip
                    continue
                try:
                    got = c.tk.call(str(app.tkimg), "get",
                                    ox + x - tx, oy + y - ty)
                except tk.TclError:                  # point not on the tile
                    return False, f"no tile under z={z} scroll={frac} ({x},{y})"
                got = tuple(int(v) for v in (got.split() if isinstance(got, str)
                                             else got))
                want = tuple(int(v) for v in arr[int(iy), int(ix)])
                if got != want:
                    return False, (f"pixel z={z} scroll={frac} at ({x},{y}): "
                                   f"shows {got}, source {want}")
                checked += 1
    # a point mark: its centre cross is drawn on the marked pixel
    app.zoom = 4.0
    app._render()
    c.xview_moveto(0.0)
    c.yview_moveto(0.0)
    app.det_px = [(20.3, 10.6)]
    app._redraw_overlays()
    tx, ty = (int(v) for v in c.coords("IMG"))
    got = c.tk.call(str(app.tkimg), "get", int(20.3 * 4) - tx, int(10.6 * 4) - ty)
    got = "#%02x%02x%02x" % tuple(int(v) for v in (
        got.split() if isinstance(got, str) else got))
    if got != app._mark_colours()[0]:
        return False, f"mark centre shows {got}"
    app.det_px = []
    return checked > 30, f"({checked} pixels checked)"


def main():
    if "--version" in sys.argv or "-V" in sys.argv:
        _console_print(f"data_acquisition {VERSION}")
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
