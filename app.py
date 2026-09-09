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
  6. "Detect" -> review the red overlay. Prune strays: "Delete" (click one
     point) or "Eraser" (drag a size-adjustable square to wipe many at once,
     e.g. letters of an annotation the detector picked up).
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

VERSION = "0.4.0"

MAG_SRC = 41          # source pixels shown in the magnifier
MAG_VIEW = 205        # magnifier canvas size (px)  -> 5x
MAX_DISPLAY_PX = 30_000_000
XH = "#00cc66"        # crosshair colour, normal
XH_CAL = "#ff8c00"    # crosshair colour, calibration click pending

GUIDE = (
    "1.  Paste an image (Ctrl+V) or  File-style  Open.  Type a dataset name.\n"
    "2.  Choose export format; pick Line or Points (and a marker shape).\n"
    "3.  Pick curve colour -> click the curve / a marker. Tune the tolerance.\n"
    "4.  (optional) Set plot area -> drag a box around the axes.\n"
    "5.  Add calibration ref -> click the X reference, then the Y reference,\n"
    "    then type their values.  Do this TWICE (2 refs, 4 clicks).\n"
    "    Mouse-wheel = zoom about cursor, middle-drag = pan,\n"
    "    the magnifier (bottom-left, 5x) is for precise aiming.\n"
    "6.  Detect -> check the red overlay.  Prune strays with 'Delete'\n"
    "    (click one point) or 'Eraser' (drag a size-adjustable square to\n"
    "    wipe a whole clump at once, e.g. an annotation read as points).\n"
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
        self.mode = "normal"          # normal|pick_color|set_roi|calib_x|calib_y|delete|erase
        self.target = None            # (r,g,b)
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
        self.del_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Delete: click a red point",
                        variable=self.del_var,
                        command=lambda: self._toggle_prune("delete")).pack(
            anchor="w")
        self.erase_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Eraser: drag a square",
                        variable=self.erase_var,
                        command=lambda: self._toggle_prune("erase")).pack(
            anchor="w")
        ttk.Label(left, text="Eraser size (px)").pack(anchor="w")
        self.eraser_var = tk.IntVar(value=30)
        ttk.Scale(left, from_=6, to=400, variable=self.eraser_var,
                  orient="horizontal").pack(fill="x")
        ttk.Button(left, text="Export…", command=self.on_export).pack(
            fill="x", pady=(2, 0))

        # --- canvas ---
        cwrap = ttk.Frame(right)
        cwrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cwrap, bg="#2b2b2b", highlightthickness=0)
        hbar = ttk.Scrollbar(cwrap, orient="horizontal",
                             command=self.canvas.xview)
        vbar = ttk.Scrollbar(cwrap, orient="vertical",
                             command=self.canvas.yview)
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
        c.bind("<B2-Motion>", lambda e: c.scan_dragto(e.x, e.y, gain=1))
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
            "set_roi": "Drag a rectangle around the axes box.",
            "calib_x": "Calibration: click the X reference "
                       "(e.g. an x-axis tick).",
            "calib_y": "Calibration: now click the Y reference "
                       "(e.g. a y-axis tick).",
            "delete": "Delete mode: click a red point to remove it.",
            "erase": "Eraser: click / drag the square to wipe every point "
                     "inside it. Adjust 'Eraser size'.",
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
        self.swatch.configure(bg="#dddddd")
        self.roi = None
        self.refs.clear()
        self.calibration = None
        self.det_data = None
        self.det_px = []
        self.mode = "normal"
        self.del_var.set(False)
        self.erase_var.set(False)
        self.count_lbl.configure(text="—")
        self._update_cal_label()
        self.root.update_idletasks()
        W, H = self.base_img.size
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 300)
        self.zoom = min(cw / W, ch / H, 1.0)
        self.zoom = max(self.zoom, 0.05)
        self._render()
        self._redraw_overlays()
        self._status(f"Loaded {self.image_name}  ({W}x{H}px).  "
                     f"Pick a colour, set calibration, then Detect.")

    # ------------------------------------------------------------ rendering
    def _render(self):
        if self.base_img is None:
            return
        W, H = self.base_img.size
        zmax = (MAX_DISPLAY_PX / (W * H)) ** 0.5
        self.zoom = min(self.zoom, max(1.0, zmax))
        dw, dh = max(1, int(W * self.zoom)), max(1, int(H * self.zoom))
        rs = Image.NEAREST if self.zoom >= 1 else Image.BILINEAR
        disp = self.base_img.resize((dw, dh), rs)
        self.tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("IMG")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg, tags="IMG")
        self.canvas.tag_lower("IMG")
        self.canvas.configure(scrollregion=(0, 0, dw, dh))
        self.zoom_var.set(f"{self.zoom * 100:.0f}%")

    def _redraw_overlays(self):
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
        for (ix, iy) in self.det_px:
            c.create_oval(ix * z - 3, iy * z - 3, ix * z + 3, iy * z + 3,
                          outline="red", width=1, tags=("OV", "PT"))

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
        new = min(max(self.zoom * factor, 0.05), 40)
        if abs(new - self.zoom) < 1e-6:
            return
        self.zoom = new
        self._render()
        W, H = self.base_img.size
        sw, sh = W * self.zoom, H * self.zoom
        self.canvas.xview_moveto(max(0.0, (ix * self.zoom - event.x) / sw))
        self.canvas.yview_moveto(max(0.0, (iy * self.zoom - event.y) / sh))
        self._redraw_overlays()

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
            self._redraw_overlays()

    def on_release(self, event):
        if self.mode == "set_roi" and self._roi_start is not None:
            ix, iy = self.img_xy(event)
            x0, y0 = self._roi_start
            self.roi = tuple(map(float, (min(x0, ix), min(y0, iy),
                                         max(x0, ix), max(y0, iy))))
            self._roi_start = None
            self._set_mode("normal")
            self._redraw_overlays()
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
        self._redraw_overlays()
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
        self._redraw_overlays()

    def _clear_roi(self):
        self.roi = None
        self._redraw_overlays()
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
                data, px = core.extract_points(
                    rgb, self.calibration, self.target, tol, self.roi,
                    area=(amin, amax), shape=self.shape_var.get())
                kind = "points"
        except Exception as e:
            messagebox.showerror("Detection failed", str(e)); return
        self.det_data = data
        self.det_px = [tuple(map(float, p)) for p in px]
        self.count_lbl.configure(text=f"{len(px)} {kind}")
        self._redraw_overlays()
        self._status(f"detected {len(px)} {kind}. Review the red overlay; "
                     f"prune with Delete / Eraser, then Export.")

    def _toggle_prune(self, which):
        """`delete` and `erase` are mutually exclusive prune modes."""
        on = self.del_var.get() if which == "delete" else self.erase_var.get()
        if on:
            (self.erase_var if which == "delete" else self.del_var).set(False)
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
    root.destroy()
    ok = (n > 60 and app.calibration is not None and wrote and erased_ok
          and upd_ok)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


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
