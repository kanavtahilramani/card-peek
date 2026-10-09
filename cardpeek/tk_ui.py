"""Card Peek in Tk: the card popup and stream area picker the Windows tray app uses, and a
small control window with the same settings for Linux (and for Windows or macOS with
CARDPEEK_UI=tk).
"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from .core import APP_DIR, IS_MAC, IS_WINDOWS, Controller, Scryfall, Settings, inside, log

if IS_WINDOWS:
    from ctypes import wintypes as wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetParent.argtypes = [wt.HWND]
    _user32.GetParent.restype = wt.HWND
    _user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wt.LONG
    _user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
    _user32.SetWindowLongW.restype = wt.LONG
    _user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
    _user32.SetLayeredWindowAttributes.restype = wt.BOOL
    _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wt.UINT]
    _user32.SetWindowPos.restype = wt.BOOL

    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80, 0x80000, 0x08000000
    HWND_TOPMOST = wt.HWND(-1)
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10


class TkPopup:
    """Borderless, always-on-top Tk window holding the card image. On Windows it never
    takes focus and lets clicks through, like the macOS popup."""

    def __init__(self, root: tk.Tk):
        p = tk.Toplevel(root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg="black")
        self.label = tk.Label(p, bd=0, bg="black")
        self.label.pack()
        self.win = p
        self.photos: dict = {}
        self.hwnd = None
        if IS_MAC:
            p.withdraw()
        else:
            # Park it off-screen rather than hiding it: moving a window never steals
            # keyboard focus from Discord, while re-showing one can.
            p.geometry("1x1+-10000+-10000")
            p.update_idletasks()
        if IS_WINDOWS:
            try:
                self.hwnd = _user32.GetParent(p.winfo_id())
                style = _user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
                # Never focused, not in Alt-Tab, and click-through. A layered window shows
                # only once its opacity is set.
                _user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                                       | WS_EX_LAYERED | WS_EX_TRANSPARENT)
                _user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 2)  # LWA_ALPHA, fully opaque
            except Exception as e:
                log(f"Couldn't set up the popup window: {e}")
                self.hwnd = None

    def show(self, key, img: Image.Image, x: int, y: int, w: int, h: int):
        k = (key, w, h)
        if k not in self.photos:
            if len(self.photos) > 40:
                self.photos.clear()
            self.photos[k] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        self.label.configure(image=self.photos[k])
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.update_idletasks()  # apply the move now; Tk can drop it otherwise
        if IS_MAC:
            self.win.deiconify()
        if self.hwnd:
            # Above other always-on-top windows too (Discord's pop-out, the taskbar).
            _user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            self.win.lift()

    def hide(self):
        if IS_MAC:
            self.win.withdraw()
        else:
            self.win.geometry("1x1+-10000+-10000")
            self.win.update_idletasks()


HOLE = "#ff00fe"  # on Windows, the selected box is drawn in this colour and shows through


def pick_area(root: tk.Tk, monitors, pointer, done) -> tk.Toplevel:
    """Dim the screens and let the user drag a box around the stream. `monitors` are mss's
    (the first spans them all). Calls done([left, top, width, height]) in screen pixels,
    or done(None) if cancelled: Esc, a right click, or a click without dragging.
    Returns the overlay window."""
    virt = monitors[0]
    ov = tk.Toplevel(root)
    ov.overrideredirect(True)
    ov.attributes("-topmost", True)
    try:
        ov.attributes("-alpha", 0.45)
    except tk.TclError:
        pass
    hole = ""
    if IS_WINDOWS:
        try:
            ov.attributes("-transparentcolor", HOLE)
            hole = HOLE
        except tk.TclError:
            pass
    ov.geometry(f"{virt['width']}x{virt['height']}+{virt['left']}+{virt['top']}")
    ov.update_idletasks()
    cv = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    px, py = pointer or (virt["left"], virt["top"])
    mon = next((m for m in monitors[1:] if m["left"] <= px < m["left"] + m["width"]
                and m["top"] <= py < m["top"] + m["height"]), monitors[1])
    cv.create_text(mon["left"] - virt["left"] + mon["width"] // 2, mon["top"] - virt["top"] + 80,
                   text="Drag a box around the video stream.   Esc or a click without dragging cancels.",
                   fill="white", font=("Segoe UI" if IS_WINDOWS else "Helvetica", 20))
    state = {}

    def finish(area):
        if not state.get("done"):
            state["done"] = True
            ov.destroy()
            done(area)

    def press(e):
        state["start"] = (e.x_root, e.y_root)
        state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="white", width=2, fill=hole)

    def drag(e):
        if "start" in state:
            sx, sy = state["start"]
            cv.coords(state["rect"], sx - virt["left"], sy - virt["top"], e.x, e.y)

    def release(e):
        if "start" in state:
            sx, sy = state["start"]
            w, h = abs(e.x_root - sx), abs(e.y_root - sy)
            finish([min(sx, e.x_root), min(sy, e.y_root), w, h] if w > 120 and h > 80 else None)

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", release)
    cv.bind("<ButtonPress-3>", lambda e: finish(None))
    ov.bind("<Escape>", lambda e: finish(None))
    ov.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    ov.focus_force()
    return ov


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings()
        self.c = Controller(self.settings, Scryfall(APP_DIR), self._changed)
        self._build_ui()
        self.c.popup = TkPopup(root)
        self.c.pointer = self._pointer
        self.c.ignore_point = self._over_control_window
        self.c.start()
        self.root.after(Controller.TICK_MS, self.tick)

    def tick(self):
        self.c.tick()
        self.root.after(Controller.TICK_MS, self.tick)

    def _pointer(self):
        p = self.root.winfo_pointerxy()
        return None if p == (-1, -1) else p  # (-1, -1): on another X screen

    def _changed(self, what):
        self.status.set(self.c.status)
        self.last.set(self.c.last)
        if what in ("deck", "sets"):
            self._fill_sets()

    def _over_control_window(self, x, y):
        r = self.root
        return inside((r.winfo_rootx(), r.winfo_rooty(), r.winfo_rootx() + r.winfo_width(),
                       r.winfo_rooty() + r.winfo_height()), x, y)

    # ---- control window

    def _build_ui(self):
        r = self.root
        r.title("Card Peek")
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self.quit)
        frm = ttk.Frame(r, padding=14)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)
        row = 0

        self.enabled = tk.BooleanVar(value=self.settings["enabled"])
        ttk.Checkbutton(frm, text="Show cards when I rest the mouse on the stream", variable=self.enabled,
                        command=lambda: self.c.set_enabled(self.enabled.get())).grid(
            row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.status = tk.StringVar(value="Starting…")
        ttk.Label(frm, textvariable=self.status, wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row += 1
        self.last = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.last, wraplength=360, foreground="#666").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1
        ttk.Label(frm, text="Set").grid(row=row, column=0, sticky="w", padx=(0, 10))
        self.set_code = tk.StringVar(value=self.settings["set"].upper())
        self.set_box = ttk.Combobox(frm, textvariable=self.set_code, width=28)
        self.set_box.grid(row=row, column=1, sticky="ew")
        self.set_box.bind("<<ComboboxSelected>>", lambda e: self._load())
        self.set_box.bind("<Return>", lambda e: self._load())
        sbtns = ttk.Frame(frm)
        sbtns.grid(row=row, column=2, sticky="w", padx=(8, 0))
        ttk.Button(sbtns, text="Load", command=self._load).pack(side="left")
        ttk.Button(sbtns, text="Update", command=lambda: self._load(refresh=True)).pack(side="left", padx=(4, 0))
        row += 1
        ttk.Label(frm, text="Pick a set, or type its Scryfall code (e.g. FRA). New sets are downloaded.",
                  foreground="#666", wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 8))
        row += 1

        self.area_text = tk.StringVar()
        ttk.Label(frm, textvariable=self.area_text).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 10))
        ttk.Button(btns, text="Select stream area", command=self.select_area).pack(side="left")
        ttk.Button(btns, text="Use whole screen", command=self.clear_area).pack(side="left", padx=(8, 0))
        row += 1

        self.card_scale = tk.DoubleVar(value=self.settings["card_scale"])
        row = self._slider(frm, row, "Card size", self.card_scale, 0.5, 2.0, lambda v: f"{v:.2f}×", "card_scale")
        ttk.Label(frm, text="Leave at 1× for MTG Arena. Change it if the streamer zooms in or out.",
                  foreground="#666", wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1
        self.popup_pct = tk.DoubleVar(value=self.settings["popup_pct"])
        row = self._slider(frm, row, "Popup height", self.popup_pct, 30, 95, lambda v: f"{v:.0f}% of screen", "popup_pct")

        self.debug = tk.BooleanVar(value=self.settings["debug"])
        ttk.Checkbutton(frm, text="Save debug snapshots of what OCR reads", variable=self.debug,
                        command=lambda: self.c.update(debug=self.debug.get())).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._update_area_text()
        self._fill_sets()

    def _slider(self, frm, row, label, var, lo, hi, fmt, key):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
        value = ttk.Label(frm, text=fmt(var.get()), width=15)

        def changed(_=None):
            value.configure(text=fmt(var.get()))
            self.settings[key] = round(var.get(), 2)

        scale = ttk.Scale(frm, from_=lo, to=hi, variable=var, command=changed, length=180)
        scale.grid(row=row, column=1, sticky="ew")
        scale.bind("<ButtonRelease-1>", lambda e: self.settings.save())
        value.grid(row=row, column=2, sticky="w", padx=(8, 0))
        return row + 1

    def _fill_sets(self):
        have = self.c.scryfall.downloaded_sets()
        codes = {d.code for d in have}
        choices = [f"{d.code.upper()}  {d.name}" for d in have]
        choices += [f"{d.code.upper()}  {d.name}  (download)" for d in self.c.draft_sets or [] if d.code not in codes]
        self.set_box.configure(values=choices)

    def _load(self, refresh=False):
        code = self.set_code.get().strip().split()[0] if self.set_code.get().strip() else ""
        if code:
            self.set_code.set(code.upper())
            self.c.load_set(code, refresh)

    def _update_area_text(self):
        a = self.settings["area"]
        self.area_text.set("Stream area: the whole screen under the pointer" if not a
                           else f"Stream area: {a[2]}×{a[3]} at ({a[0]}, {a[1]})")

    # ---- stream area

    def select_area(self):
        self.c.hide_popup()

        def done(area):
            if area:
                self.c.set_area(area)
                self._update_area_text()
        pick_area(self.root, self.c.sct.monitors, self._pointer(), done)

    def clear_area(self):
        self.c.set_area(None)
        self._update_area_text()

    def quit(self):
        self.c.quit()
        self.root.destroy()


def run():
    log("Card Peek starting (Tk)")
    root = tk.Tk()
    App(root)
    root.mainloop()
