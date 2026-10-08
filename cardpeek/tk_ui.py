"""Card Peek's control window for Windows and Linux (and macOS with CARDPEEK_UI=tk).

A stand-in until those platforms get a tray app like the macOS menu bar one; it offers
the same settings in a small window.
"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from .core import APP_DIR, IS_MAC, IS_WINDOWS, Controller, Scryfall, Settings, inside, log


class TkPopup:
    """Borderless, always-on-top Tk window holding the card image."""

    def __init__(self, root: tk.Tk):
        p = tk.Toplevel(root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg="black")
        self.label = tk.Label(p, bd=0, bg="black")
        self.label.pack()
        self.win = p
        self.photos: dict = {}
        if IS_MAC:
            p.withdraw()
        else:
            # Park it off-screen rather than hiding it: moving a window never steals
            # keyboard focus from Discord, while re-showing one can.
            p.geometry("1x1+-10000+-10000")
            p.update_idletasks()
        if IS_WINDOWS:
            try:
                hwnd = ctypes.windll.user32.GetParent(p.winfo_id())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
                # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: never focused, not in Alt-Tab
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
            except Exception:
                pass

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
        self.win.lift()

    def hide(self):
        if IS_MAC:
            self.win.withdraw()
        else:
            self.win.geometry("1x1+-10000+-10000")
            self.win.update_idletasks()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings()
        self.c = Controller(self.settings, Scryfall(APP_DIR), self._changed)
        self._build_ui()
        self.c.popup = TkPopup(root)
        self.c.pointer = root.winfo_pointerxy
        self.c.ignore_point = self._over_control_window
        self.c.start()
        self.root.after(Controller.TICK_MS, self.tick)

    def tick(self):
        self.c.tick()
        self.root.after(Controller.TICK_MS, self.tick)

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
        virt = self.c.sct.monitors[0]
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        try:
            ov.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        ov.geometry(f"{virt['width']}x{virt['height']}+{virt['left']}+{virt['top']}")
        ov.update_idletasks()
        cv = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        mon = self.c.monitor_at(*self.root.winfo_pointerxy())
        cv.create_text(mon["left"] - virt["left"] + mon["width"] // 2, mon["top"] - virt["top"] + 60,
                       text="Drag a box around the video stream. Click without dragging to cancel.",
                       fill="white", font=("Segoe UI" if IS_WINDOWS else "Helvetica", 20))
        state = {}

        def press(e):
            state["start"] = (e.x_root, e.y_root)
            state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#4fc3f7", width=3)

        def drag(e):
            if "start" in state:
                sx, sy = state["start"]
                cv.coords(state["rect"], sx - virt["left"], sy - virt["top"], e.x, e.y)

        def release(e):
            if "start" not in state:
                return
            sx, sy = state["start"]
            l, t = min(sx, e.x_root), min(sy, e.y_root)
            w, h = abs(e.x_root - sx), abs(e.y_root - sy)
            ov.destroy()
            if w > 120 and h > 80:
                self.c.set_area([l, t, w, h])
                self._update_area_text()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        ov.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

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
