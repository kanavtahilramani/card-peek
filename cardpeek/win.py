"""Card Peek as a Windows tray app.

The icon in the taskbar's notification area carries the same menu as the macOS menu bar
app: the set (downloading it first if need be), the stream area, card and popup size,
and so on. The lookups themselves run in core.Controller.

Two threads. Tk, on the main thread, drives the Controller every TICK_MS and owns the
card popup, the stream area picker and the set code dialog. The tray icon has a thread
of its own with a plain Win32 message loop, so an open menu never holds up lookups. The
tray thread only reads the app's state; whatever changes it is handed to the Tk thread
through a queue.
"""
from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes as wt
from datetime import date
from functools import partial
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageChops, ImageDraw

from . import __version__
from .core import (APP_DIR, CARD_SIZES, FROZEN, HOVER_DELAYS, POPUP_SIZES, Controller, Scryfall, Settings, inside,
                   log, logger)
from .tk_ui import TkPopup, pick_area

ASSETS = Path(__file__).parent / "assets"
CLASS_NAME = "CardPeekTray"   # the tray window's class, which a second copy looks for
MUTEX_NAME = "CardPeek"       # held while Card Peek runs
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE = "Card Peek"

# --------------------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("guidItem", GUID)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):  # noqa: N801
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, argtypes
    return f


P = ctypes.POINTER
RegisterClassExW = _fn(user32, "RegisterClassExW", wt.ATOM, P(WNDCLASSEXW))
CreateWindowExW = _fn(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
                      wt.LPVOID)
DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
DestroyWindow = _fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)
GetMessageW = _fn(user32, "GetMessageW", wt.BOOL, P(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
TranslateMessage = _fn(user32, "TranslateMessage", wt.BOOL, P(wt.MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, P(wt.MSG))
PostMessageW = _fn(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
PostQuitMessage = _fn(user32, "PostQuitMessage", None, ctypes.c_int)
RegisterWindowMessageW = _fn(user32, "RegisterWindowMessageW", wt.UINT, wt.LPCWSTR)
ChangeWindowMessageFilterEx = _fn(user32, "ChangeWindowMessageFilterEx", wt.BOOL, wt.HWND, wt.UINT, wt.DWORD,
                                  wt.LPVOID)
FindWindowW = _fn(user32, "FindWindowW", wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
AllowSetForegroundWindow = _fn(user32, "AllowSetForegroundWindow", wt.BOOL, wt.DWORD)
GetCursorPos = _fn(user32, "GetCursorPos", wt.BOOL, P(wt.POINT))
SetTimer = _fn(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID)
KillTimer = _fn(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
CreatePopupMenu = _fn(user32, "CreatePopupMenu", wt.HMENU)
AppendMenuW = _fn(user32, "AppendMenuW", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
DestroyMenu = _fn(user32, "DestroyMenu", wt.BOOL, wt.HMENU)
TrackPopupMenuEx = _fn(user32, "TrackPopupMenuEx", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                       wt.HWND, wt.LPVOID)
GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
CreateIconIndirect = _fn(user32, "CreateIconIndirect", wt.HICON, P(ICONINFO))
DestroyIcon = _fn(user32, "DestroyIcon", wt.BOOL, wt.HICON)
MessageBoxW = _fn(user32, "MessageBoxW", ctypes.c_int, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT)
Shell_NotifyIconW = _fn(shell32, "Shell_NotifyIconW", wt.BOOL, wt.DWORD, P(NOTIFYICONDATAW))
Shell_NotifyIconGetRect = _fn(shell32, "Shell_NotifyIconGetRect", ctypes.c_long, P(NOTIFYICONIDENTIFIER),
                              P(wt.RECT))
CreateDIBSection = _fn(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, P(BITMAPINFOHEADER), wt.UINT,
                       P(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
CreateBitmap = _fn(gdi32, "CreateBitmap", wt.HBITMAP, ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, wt.LPVOID)
DeleteObject = _fn(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
CreateMutexW = _fn(kernel32, "CreateMutexW", wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", wt.HANDLE)
try:  # Windows 10 1607 and later
    GetDpiForWindow = _fn(user32, "GetDpiForWindow", wt.UINT, wt.HWND)
    GetSystemMetricsForDpi = _fn(user32, "GetSystemMetricsForDpi", ctypes.c_int, ctypes.c_int, wt.UINT)
except AttributeError:
    GetDpiForWindow = GetSystemMetricsForDpi = None
try:  # Windows 8 and later
    SetProcessInformation = _fn(kernel32, "SetProcessInformation", wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID,
                                wt.DWORD)
except AttributeError:
    SetProcessInformation = None

WM_NULL, WM_DESTROY, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION = 0x0, 0x2, 0x10, 0x11, 0x16
WM_SETTINGCHANGE, WM_CONTEXTMENU, WM_DISPLAYCHANGE, WM_TIMER, WM_DPICHANGED = 0x1A, 0x7B, 0x7E, 0x113, 0x2E0
WM_APP = 0x8000
WM_TRAY = WM_APP + 1        # the icon was clicked
WM_REFRESH = WM_APP + 2     # from the Tk thread: the icon or its tooltip changed
WM_SHOW_MENU = WM_APP + 3   # from a second copy of Card Peek: show the menu
WM_STOP = WM_APP + 4        # from the Tk thread: remove the icon and end the thread
NIN_SELECT, NIN_KEYSELECT = 0x400, 0x401
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x80
NOTIFYICON_VERSION_4 = 4
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x10, 0x800
TPM_LEFTALIGN, TPM_RIGHTBUTTON, TPM_RIGHTALIGN, TPM_BOTTOMALIGN = 0x0, 0x2, 0x8, 0x20
TPM_NONOTIFY, TPM_RETURNCMD = 0x80, 0x100
SM_MENUDROPALIGNMENT, SM_CXSMICON = 40, 49
MB_ICONWARNING, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x30, 0x40, 0x10000, 0x40000
WS_POPUP, WS_EX_TOOLWINDOW = 0x80000000, 0x80
ERROR_ALREADY_EXISTS, ERROR_CLASS_ALREADY_EXISTS = 183, 1410
MSGFLT_ALLOW = 1
ASFW_ANY = 0xFFFFFFFF
RETRY_TIMER = 1


def cursor_pos():
    """The pointer in screen pixels, or None when Windows won't say (e.g. on the lock
    screen or while a UAC prompt is up)."""
    pt = wt.POINT()
    return (pt.x, pt.y) if GetCursorPos(ctypes.byref(pt)) else None


def message(title: str, text: str, icon: int = MB_ICONINFORMATION):
    """A message box on a thread of its own, so lookups carry on while it's up."""
    threading.Thread(target=MessageBoxW, args=(None, text, title, icon | MB_SETFOREGROUND | MB_TOPMOST),
                     daemon=True, name="message").start()


def keep_full_speed():
    """Windows may run background apps on efficiency cores or at a low clock speed
    ("EcoQoS"), which would make lookups sluggish. Opt out, as the macOS app does from
    App Nap."""
    state = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0)  # EXECUTION_SPEED: off
    if SetProcessInformation and not SetProcessInformation(GetCurrentProcess(), 4,  # ProcessPowerThrottling
                                                           ctypes.byref(state), ctypes.sizeof(state)):
        log(f"Couldn't opt out of power throttling ({ctypes.get_last_error()}).")


def taskbar_is_light() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def show_in_explorer(path: Path):
    subprocess.Popen(f'explorer /select,"{path}"')


# ---- start with Windows


def starts_with_windows() -> bool:
    if startup_command() is None:
        return False
    # Settings > Apps > Startup and Task Manager keep their own on/off switch for it.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data = winreg.QueryValueEx(k, RUN_VALUE)[0]
        return not (isinstance(data, bytes) and data[:1] and data[0] & 1)  # odd: switched off there
    except OSError:
        return True


def startup_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VALUE)[0]
    except OSError:
        return None


def follow_startup_entry():
    """Card Peek is a single .exe that can live anywhere, and a new version is a new file.
    If it's set to start with Windows, start this copy: the one that's being used."""
    command = startup_command()
    if command and command != f'"{sys.executable}"':
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"')
            log(f"Start with Windows now starts {sys.executable}")
        except OSError as e:
            log(f"Couldn't update Start with Windows: {e}")


def set_starts_with_windows(on: bool):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
    try:  # forget a switch-off in Settings, so the choice made here is the one that counts
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
    except OSError:
        pass


# --------------------------------------------------------------------------- tray icon


def tray_glyph(size: int, light_taskbar: bool, dim: bool = False, progress: float | None = None) -> Image.Image:
    """The tray icon: two fanned cards, like the macOS menu bar's SF Symbol, drawn for the
    taskbar's colour at the exact size it shows icons at. Dimmed while Card Peek isn't
    looking up cards; a progress bar along the bottom while a download runs."""
    k = 8  # supersampling
    n = size * k
    stroke = round(max(1.25, size / 12) * k)
    w, h, r = 0.48 * n, 0.68 * n, 0.09 * n

    def card(cx, cy, angle):
        box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        outline, solid = Image.new("L", (n, n), 0), Image.new("L", (n, n), 0)
        ImageDraw.Draw(outline).rounded_rectangle(box, radius=r, outline=255, width=stroke)
        ImageDraw.Draw(solid).rounded_rectangle(box, radius=r, fill=255)
        if angle:
            outline = outline.rotate(angle, resample=Image.BICUBIC, center=(cx, cy))
        return outline, solid

    back, _ = card(0.38 * n, 0.50 * n, 16)
    front, front_solid = card(0.62 * n, 0.53 * n, 0)
    mask = ImageChops.lighter(ImageChops.subtract(back, front_solid), front)  # the front card hides the back
    if progress is not None:
        bar = max(3, round(size * 0.17)) * k
        ImageDraw.Draw(mask).rectangle((0, n - bar - k, n, n), fill=0)
        ImageDraw.Draw(mask).rectangle((0, n - bar, n - 1, n - 1), fill=100)
        if round(progress * size):
            ImageDraw.Draw(mask).rectangle((0, n - bar, round(progress * size) * k - 1, n - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    if dim:
        mask = mask.point(lambda v: round(v * 0.42))
    img = Image.new("RGBA", (size, size), (28, 28, 28, 0) if light_taskbar else (255, 255, 255, 0))
    img.putalpha(mask)
    return img


def hicon(img: Image.Image) -> int:
    """A Windows icon handle for an RGBA image. Free it with DestroyIcon."""
    w, h = img.size
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)  # top-down BGRA
    bits = ctypes.c_void_p()
    color = CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not color:
        raise ctypes.WinError(ctypes.get_last_error())
    data = img.convert("RGBA").tobytes("raw", "BGRA")
    ctypes.memmove(bits, data, len(data))
    mask = CreateBitmap(w, h, 1, 1, (ctypes.c_ubyte * ((w + 15) // 16 * 2 * h))())
    try:
        icon = CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
    finally:
        DeleteObject(color)
        DeleteObject(mask)
    if not icon:
        raise ctypes.WinError(ctypes.get_last_error())
    return icon


class Menu:
    """A menu to show, built fresh each time it opens. Items are (title, action, checked,
    enabled), a (title, Menu) submenu, or None for a separator. Titles use & for the
    keyboard shortcut letter; text that may hold an & of its own goes through esc()."""

    def __init__(self):
        self.items = []

    def add(self, title, action=None, checked=False, enabled=True):
        self.items.append((title, action, checked, enabled and action is not None))

    def label(self, title):
        self.add(esc(title))

    def sub(self, title) -> Menu:
        menu = Menu()
        self.items.append((title, menu))
        return menu

    def separator(self):
        self.items.append(None)


def esc(text: str) -> str:
    return text.replace("&", "&&")


def build_menu(menu: Menu, actions: list) -> int:
    """Turn a Menu into a Win32 menu. Each item's action is appended to `actions`; the
    item's command ID is its position there, plus one."""
    h = CreatePopupMenu()
    for item in menu.items:
        if item is None:
            AppendMenuW(h, MF_SEPARATOR, 0, None)
        elif isinstance(item[1], Menu):
            AppendMenuW(h, MF_POPUP, build_menu(item[1], actions), item[0])
        else:
            title, action, checked, enabled = item
            actions.append(action)
            AppendMenuW(h, MF_STRING | (MF_CHECKED if checked else 0) | (0 if enabled else MF_GRAYED),
                        len(actions), title)
    return h


_trays: dict[int, Tray] = {}


@WNDPROC
def _wndproc(hwnd, msg, wparam, lparam):
    tray = _trays.get(hwnd)
    if tray is not None:
        try:
            result = tray.handle(msg, wparam, lparam)
            if result is not None:
                return result
        except Exception:
            logger.exception("Tray message failed")
    return DefWindowProcW(hwnd, msg, wparam, lparam)


_class_registered = False


def _register_class():
    global _class_registered
    if not _class_registered:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _wndproc
        wc.hInstance = GetModuleHandleW(None)
        wc.lpszClassName = CLASS_NAME
        if not RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(ctypes.get_last_error())
        _class_registered = True


class Tray(threading.Thread):
    """The notification area icon and its menu, on a thread of their own. `app` provides
    menu(), call(fn) to run fn on the Tk thread, quit(), and its Controller as c."""

    def __init__(self, app):
        super().__init__(daemon=True, name="tray")
        self.app = app
        self.hwnd = None
        self.ready = threading.Event()
        self.added = False
        self.menu_open = False
        self.menu_closed = 0.0
        self.want = (None, True, "Card Peek")  # (progress, dim, tooltip) to show
        self.shown = None                      # (icon size, light taskbar, progress step, dim, tooltip) shown
        self.icon = None
        self.old_icon = None                   # replaced, and freed once the shell has the new one
        self.taskbar_created = 0
        self._warned = False

    def run(self):
        try:
            _register_class()
            self.hwnd = CreateWindowExW(WS_EX_TOOLWINDOW, CLASS_NAME, "Card Peek", WS_POPUP, 0, 0, 0, 0,
                                        None, None, GetModuleHandleW(None), None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            _trays[self.hwnd] = self
            # Explorer announces itself when it (re)starts; the icon has to be added again.
            self.taskbar_created = RegisterWindowMessageW("TaskbarCreated")
            for m in (self.taskbar_created, WM_SHOW_MENU):
                ChangeWindowMessageFilterEx(self.hwnd, m, MSGFLT_ALLOW, None)
            self._add()
        except Exception:
            logger.exception("Couldn't create the tray icon")
            self.hwnd = None
            return
        finally:
            self.ready.set()
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    # ---- from the Tk thread

    def update(self, progress, dim, tip):
        self.want = (progress, dim, tip)
        if self.hwnd:
            PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def stop(self):
        if self.hwnd and self.is_alive():
            PostMessageW(self.hwnd, WM_STOP, 0, 0)
            self.join(3)

    # ---- on the tray thread

    def handle(self, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == NIN_KEYSELECT and time.monotonic() - self.menu_closed < 0.4:
                return 0  # Enter on the icon sends this twice
            if event in (WM_CONTEXTMENU, NIN_SELECT, NIN_KEYSELECT):
                self.show_menu(ctypes.c_short(wparam & 0xFFFF).value, ctypes.c_short((wparam >> 16) & 0xFFFF).value)
            return 0
        if msg == WM_REFRESH:
            self._refresh()
            return 0
        if msg == WM_SHOW_MENU:
            self.show_menu(*self._icon_anchor())
            return 0
        if msg == self.taskbar_created and msg:
            self.added = False
            self._add()
            return 0
        if msg == WM_TIMER and wparam == RETRY_TIMER:
            if not self.added:
                self._add()
            return 0
        if msg in (WM_SETTINGCHANGE, WM_DPICHANGED, WM_DISPLAYCHANGE):
            self._refresh()  # the taskbar's colour or the icon size may have changed
            if msg == WM_DISPLAYCHANGE:
                self.app.call(self.app.c.screens_changed)
            return None
        if msg == WM_CLOSE:  # e.g. taskkill /im CardPeek-*.exe
            self.app.call(self.app.quit)
            return 0
        if msg == WM_ENDSESSION:
            if wparam:
                self.app.call(self.app.quit)
            return 0
        if msg == WM_STOP:
            DestroyWindow(self.hwnd)
            return 0
        if msg == WM_DESTROY:
            Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
            if self.icon:
                DestroyIcon(self.icon)
                self.icon = None
            self._free_old_icon()
            _trays.pop(self.hwnd, None)
            PostQuitMessage(0)
            return 0
        return None

    def _nid(self, flags=0):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _render(self):
        """Draw the icon for the current state if it differs from the one shown. Returns
        True if it changed."""
        progress, dim, tip = self.want
        size = GetSystemMetrics(SM_CXSMICON)
        if GetSystemMetricsForDpi:  # the size for the taskbar's display, not the one Card Peek started on
            size = GetSystemMetricsForDpi(SM_CXSMICON, GetDpiForWindow(self.hwnd)) or size
        step = None if progress is None else round(progress * size)
        key = (size, taskbar_is_light(), step, dim, tip)
        if key == self.shown and self.icon:
            return False
        if key[:4] != (self.shown or (None,) * 5)[:4] or not self.icon:
            old, self.icon = self.icon, hicon(tray_glyph(size, key[1], dim, None if step is None else step / size))
            if old:
                self._free_old_icon()
                self.old_icon = old
        self.shown = key
        return True

    def _add(self):
        self.shown = None
        self._render()
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.icon
        nid.szTip = self.want[2][:127]
        # Explorer can be slow to answer at sign-in and say no to an icon it did add.
        if Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)) or Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            nid.uVersion = NOTIFYICON_VERSION_4
            Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
            self.added = True
            self._free_old_icon()
            KillTimer(self.hwnd, RETRY_TIMER)
        else:
            self.added = False
            if not self._warned:
                log("Couldn't add the tray icon yet; trying again every few seconds.")
                self._warned = True
            SetTimer(self.hwnd, RETRY_TIMER, 3000, None)

    def _refresh(self):
        if not self._render() or not self.added:
            return
        nid = self._nid(NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.hIcon = self.icon
        nid.szTip = self.want[2][:127]
        Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        self._free_old_icon()

    def _free_old_icon(self):
        if self.old_icon:
            DestroyIcon(self.old_icon)
            self.old_icon = None

    def _icon_anchor(self):
        """Where to show the menu when it wasn't opened from the icon: at the icon, or at
        the pointer if the icon is tucked away."""
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize, ident.hWnd, ident.uID = ctypes.sizeof(NOTIFYICONIDENTIFIER), self.hwnd, 1
        rect = wt.RECT()
        if Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) == 0 and rect.right > rect.left:
            return (rect.left + rect.right) // 2, rect.top
        return cursor_pos() or (0, 0)

    def show_menu(self, x, y):
        if self.menu_open:
            return
        self.menu_open = True
        self.app.call(self.app.c.hide_popup)
        actions, hmenu, chosen = [], None, 0
        try:
            hmenu = build_menu(self.app.menu(), actions)
            # The menu only closes on a click elsewhere if Card Peek is in the foreground.
            SetForegroundWindow(self.hwnd)
            align = TPM_RIGHTALIGN if GetSystemMetrics(SM_MENUDROPALIGNMENT) else TPM_LEFTALIGN
            chosen = TrackPopupMenuEx(hmenu, align | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD
                                      | TPM_NONOTIFY, x, y, self.hwnd, None)
            PostMessageW(self.hwnd, WM_NULL, 0, 0)
        except Exception:
            logger.exception("The menu failed")
        finally:
            if hmenu:
                DestroyMenu(hmenu)  # and its submenus
            self.menu_open = False
            self.menu_closed = time.monotonic()
        if 0 < chosen <= len(actions) and actions[chosen - 1]:
            self.app.call(actions[chosen - 1])


# --------------------------------------------------------------------------- dialogs


def ask_set_code(root: tk.Tk, monitor, done) -> tk.Toplevel:
    """The "Other Set Code…" dialog. Calls done(code), or done(None) if cancelled."""
    d = tk.Toplevel(root)
    d.withdraw()
    d.title("Load a Set")
    d.resizable(False, False)
    d.attributes("-topmost", True)
    s = root.winfo_fpixels("1i") / 96  # Tk scales fonts with the display, not padding
    frm = ttk.Frame(d, padding=round(16 * s))
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Load a set by its code", font=("Segoe UI Semibold", 11)).pack(anchor="w")
    ttk.Label(frm, text="Use the code Scryfall shows for the set, e.g. FRA for Reality Fracture (it's in the "
                        "address of the set's page: scryfall.com/sets/fra). Card Peek downloads the set if it "
                        "doesn't have it yet.",
              wraplength=round(340 * s), justify="left").pack(anchor="w", pady=(round(6 * s), round(10 * s)))
    code = tk.StringVar()
    entry = ttk.Entry(frm, textvariable=code)
    entry.pack(fill="x")
    buttons = ttk.Frame(frm)
    buttons.pack(anchor="e", pady=(round(14 * s), 0))
    closed = []

    def close(value):
        if not closed:
            closed.append(value)
            d.destroy()
            done(value)

    ttk.Button(buttons, text="Load", default="active", command=lambda: close(code.get().strip())).pack(side="left")
    ttk.Button(buttons, text="Cancel", command=lambda: close(None)).pack(side="left", padx=(round(8 * s), 0))
    d.bind("<Return>", lambda e: close(code.get().strip()))
    d.bind("<Escape>", lambda e: close(None))
    d.protocol("WM_DELETE_WINDOW", lambda: close(None))
    d.update_idletasks()
    w, h = d.winfo_reqwidth(), d.winfo_reqheight()
    d.geometry(f"+{monitor['left'] + (monitor['width'] - w) // 2}+{monitor['top'] + (monitor['height'] - h) // 3}")
    d.deiconify()
    d.focus_force()
    entry.focus_set()
    return d


WELCOME = (
    "Card Peek runs in the notification area, at the right end of the taskbar: look for the "
    "two-cards icon. Windows may tuck new icons away under the ^ arrow; drag it onto the taskbar "
    "to keep it in view.\n\n"
    "Click the icon to pick the set being drafted and the part of the screen the stream is in.\n\n"
    "Card Peek is getting the OCR engine and {set} ready now. Then rest your pointer on a card in "
    "the stream to see it in full.")

ABOUT = (
    "Card Peek {version}\n\n"
    "Rest your pointer on a Magic card in a video stream and the full card pops up beside it.\n\n"
    "Card data and images from Scryfall. Text recognition by RapidOCR.\n\n"
    "Card Peek is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed "
    "by Wizards. Portions of the materials used are property of Wizards of the Coast. "
    "©Wizards of the Coast LLC.")


# --------------------------------------------------------------------------- the app


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.calls: queue.SimpleQueue = queue.SimpleQueue()
        self.settings = Settings()
        self.c = Controller(self.settings, Scryfall(APP_DIR), self._changed)
        self.c.popup = TkPopup(root)
        self.c.pointer = cursor_pos
        self.c.ignore_point = self._ignore
        self.picker = None    # the stream area overlay, while it's up
        self.dialog = None    # the set code dialog, while it's up
        self.asked_for = None  # a set the user chose from the menu, until it loads or fails
        self.quitting = False
        self.tray = Tray(self)
        self.tray.start()
        self.tray.ready.wait(10)

    def start(self):
        first_run = self.settings.first_run
        if FROZEN:
            follow_startup_entry()
        self.c.start()
        self._refresh_tray()
        self.root.after(Controller.TICK_MS, self._tick)
        if first_run:
            self.settings.save()
            message("Card Peek is running", WELCOME.format(set=self.settings["set"].upper()))

    def call(self, fn, *args, **kwargs):
        """Run fn on the Tk thread (for the tray thread)."""
        self.calls.put(partial(fn, *args, **kwargs))

    def _tick(self):
        while not self.quitting:
            try:
                fn = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logger.exception("Menu command failed")
        if self.quitting:
            return
        self.c.tick()
        self.root.after(Controller.TICK_MS, self._tick)

    def _ignore(self, x, y):
        """Don't look up cards under Card Peek's own menu or windows."""
        if self.tray.menu_open or self.picker is not None:
            return True
        d = self.dialog
        return d is not None and inside((d.winfo_rootx(), d.winfo_rooty(), d.winfo_rootx() + d.winfo_width(),
                                         d.winfo_rooty() + d.winfo_height()), x, y)

    def _changed(self, what):
        self._refresh_tray()
        if what == "deck" and self.asked_for:
            self.asked_for = None
        elif what == "error" and self.asked_for:
            self.asked_for = None
            message("Couldn't load that set", self.c.error, MB_ICONWARNING)

    def _refresh_tray(self):
        c = self.c
        self.tray.update(c.progress, not (c.ready and self.settings["enabled"]), "Card Peek\n" + c.status)

    # ---- the menu (built on the tray thread; its actions run on the Tk thread)

    def menu(self) -> Menu:
        c, s, m = self.c, self.settings, Menu()
        for line in c.status.split("\n"):
            m.label(line)
        if c.last and s["debug"]:
            m.label(c.last[:90])
        m.separator()
        m.add("&Peek at Cards", self.toggle_enabled, checked=s["enabled"])
        m.separator()

        # The set: downloaded ones first, then recent ones that can be downloaded.
        current = c.loading or (c.deck.info.code if c.deck else s["set"])
        label = c.deck.label if c.deck and c.deck.info.code == current else current.upper()
        sets = m.sub(f"&Set: {esc(label)}")
        downloaded = c.scryfall.downloaded_sets()
        have = {d.code for d in downloaded}
        if downloaded:
            sets.label("Downloaded")
            for d in downloaded:
                sets.add(esc(f"{d.name} ({d.code.upper()})"), partial(self.choose_set, d.code),
                         checked=d.code == current)
            sets.separator()
        sets.label("Download from Scryfall")
        if c.draft_sets is None:
            sets.label("Getting the list of sets…")
        else:
            today = date.today().isoformat()
            for d in c.draft_sets:
                if d.code not in have:
                    soon = " (previews)" if d.released and d.released > today else ""
                    sets.add(esc(f"{d.name} ({d.code.upper()}){soon}"), partial(self.choose_set, d.code),
                             checked=d.code == current)
        sets.add("&Other Set Code…", self.other_set)
        sets.separator()
        sets.add(f"&Update {esc(label)} from Scryfall", self.refresh_set, enabled=c.deck is not None and not c.loading)

        area = m.sub("Stream &Area")
        a = s["area"]
        area.add("&Whole Screen", partial(c.set_area, None), checked=not a)
        if a:
            area.add(f"{a[2]} × {a[3]} at ({a[0]}, {a[1]})", self.select_area, checked=True)
        area.separator()
        area.add("&Select Stream Area…", self.select_area)

        size = m.sub("&Card Size")
        for v in sorted(set(CARD_SIZES) | {s["card_scale"]}):
            note = " (MTG Arena)" if v == 1.0 else ""
            size.add(f"{v:.0%}{note}", partial(c.update, card_scale=float(v)), checked=abs(v - s["card_scale"]) < 1e-6)
        size.separator()
        size.label("Change if the streamer zooms in or out")

        popup = m.sub("P&opup Size")
        for v in sorted(set(POPUP_SIZES) | {s["popup_pct"]}):
            popup.add(f"{v:.0f}% of screen height", partial(c.update, popup_pct=float(v)), checked=v == s["popup_pct"])

        delay = m.sub("&Hover Delay")
        for v, name in HOVER_DELAYS:
            delay.add(f"{name} ({v * 1000:.0f} ms)", partial(c.update, dwell=float(v)), checked=abs(v - s["dwell"]) < 1e-6)

        m.separator()
        if FROZEN:  # only the installed app can start itself
            m.add("Start with &Windows", self.toggle_start_with_windows, checked=starts_with_windows())
        trouble = m.sub("&Troubleshooting")
        trouble.add("&Save Snapshots of What OCR Reads", self.toggle_debug, checked=s["debug"])
        trouble.add("Show Sna&pshots", partial(show_in_explorer, APP_DIR / "debug"), enabled=(APP_DIR / "debug").exists())
        trouble.add("Show &Log", partial(show_in_explorer, APP_DIR / "cardpeek.log"))
        m.add("&About Card Peek", self.about)
        m.add("E&xit", self.quit)
        return m

    # ---- menu actions

    def toggle_enabled(self):
        self.c.set_enabled(not self.settings["enabled"])
        self._refresh_tray()

    def choose_set(self, code):
        if self.c.deck and self.c.deck.info.code == code and not self.c.loading:
            return
        self.asked_for = code
        self.c.load_set(code)

    def other_set(self):
        if self.dialog is not None:
            self.dialog.focus_force()
            return

        def done(code):
            self.dialog = None
            if not code:
                return
            if not code.isalnum() or len(code) > 6:
                message("That isn't a set code", "Set codes are 3–5 letters or digits, like FRA or EOE.",
                        MB_ICONWARNING)
                return
            self.asked_for = code.lower()
            self.c.load_set(code)
        self.dialog = ask_set_code(self.root, self.c.monitor_at(*(cursor_pos() or (0, 0))), done)

    def refresh_set(self):
        if self.c.deck:
            self.asked_for = self.c.deck.info.code
            self.c.load_set(self.c.deck.info.code, refresh=True)

    def select_area(self):
        if self.picker is not None:
            return
        self.c.hide_popup()

        def done(area):
            self.picker = None
            if area:
                self.c.set_area(area)
        self.picker = pick_area(self.root, self.c.sct.monitors, cursor_pos(), done)

    def toggle_start_with_windows(self):
        try:
            set_starts_with_windows(not starts_with_windows())
        except OSError as e:
            logger.exception("Start with Windows failed")
            message("Couldn't change Start with Windows", str(e), MB_ICONWARNING)

    def toggle_debug(self):
        self.c.update(debug=not self.settings["debug"])

    def about(self):
        message("About Card Peek", ABOUT.format(version=__version__))

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.c.quit()
        self.tray.stop()
        self.root.quit()


def already_running() -> bool:
    """True if Card Peek is already running in this sign-in session; it then gets a nudge
    to show its menu, which is what someone opening it again is after."""
    global _mutex
    _mutex = CreateMutexW(None, False, MUTEX_NAME)  # held until Card Peek exits
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return False
    hwnd = FindWindowW(CLASS_NAME, None)
    if hwnd:
        AllowSetForegroundWindow(ASFW_ANY)
        PostMessageW(hwnd, WM_SHOW_MENU, 0, 0)
    return True


def run():
    if already_running():
        log("Card Peek is already running; showing its menu.")
        return
    log(f"Card Peek {__version__} starting")
    keep_full_speed()
    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(default=str(ASSETS / "CardPeek.ico"))  # for the dialogs
    except tk.TclError:
        pass
    app = App(root)
    if not app.tray.hwnd:
        MessageBoxW(None, "Card Peek couldn't put its icon in the taskbar, so there'd be no way to use it. "
                          f"The log may say why: {APP_DIR / 'cardpeek.log'}", "Card Peek", MB_ICONWARNING)
        return
    app.start()
    try:
        root.mainloop()
    finally:
        app.tray.stop()
    root.destroy()


# --------------------------------------------------------------------------- self-test


def self_test() -> list[str]:
    """Exercise the Windows UI without anyone at the keyboard: the tray window and icon in
    every state, the whole menu, the popup, the stream area picker and the set code
    dialog. CI runs it in the packaged app, where a missing Tk file or a mistyped Win32
    call would otherwise only show up on someone's PC. Returns the problems found."""
    problems = []
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    try:
        if not app.tray.hwnd:
            return ["the tray window wasn't created"]
        if not app.tray.added:
            log("Note: no tray icon was added (is Explorer running?).")

        actions = []
        DestroyMenu(build_menu(app.menu(), actions))
        if len(actions) < 20:
            problems.append(f"the menu has only {len(actions)} items")

        for size in (16, 20, 24, 32):
            for state in ({}, {"dim": True}, {"progress": 0.4}):
                for light in (False, True):
                    DestroyIcon(hicon(tray_glyph(size, light, **state)))
        app.tray.update(0.42, False, "Card Peek\nSelf-test")
        for _ in range(100):
            if app.tray.shown and app.tray.shown[-1] == "Card Peek\nSelf-test":
                break
            time.sleep(0.02)
        else:
            problems.append("the tray icon didn't update")

        app.c.popup.show("test", Image.new("RGB", (488, 680), "#777"), 40, 40, 244, 340)
        root.update()
        if (app.c.popup.win.winfo_width(), app.c.popup.win.winfo_height()) != (244, 340):
            problems.append(f"the popup is {app.c.popup.win.winfo_width()}×{app.c.popup.win.winfo_height()}")
        app.c.popup.hide()

        picked = []
        mon = app.c.sct.monitors[1]
        ov = pick_area(root, app.c.sct.monitors, (mon["left"] + 10, mon["top"] + 10), picked.append)
        root.update()
        cv = ov.winfo_children()[0]
        x0, y0 = mon["left"] + 100, mon["top"] + 100
        vx, vy = app.c.sct.monitors[0]["left"], app.c.sct.monitors[0]["top"]
        for event, dx in (("<ButtonPress-1>", 0), ("<B1-Motion>", 200), ("<ButtonRelease-1>", 300)):
            cv.event_generate(event, x=x0 - vx + dx, y=y0 - vy + dx // 2, rootx=x0 + dx, rooty=y0 + dx // 2,
                              when="now")
        root.update()
        if picked != [[x0, y0, 300, 150]]:
            problems.append(f"the stream area picker gave {picked}")

        answers = []
        d = ask_set_code(root, mon, answers.append)
        root.update()
        d.event_generate("<Escape>", when="now")
        root.update()
        if answers != [None]:
            problems.append(f"the set code dialog gave {answers}")

        starts_with_windows()
    except Exception as e:
        logger.exception("UI self-test failed")
        problems.append(f"UI: {type(e).__name__}: {e}")
    finally:
        app.tray.stop()
        root.destroy()
    log(f"UI checked: tray icon {'added' if app.tray.added else 'not added'}, menu, popup, area picker, dialog.")
    return problems
