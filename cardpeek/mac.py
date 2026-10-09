"""Card Peek as a macOS menu bar app.

Everything is set from the menu: the set (downloading it first if need be), the stream
area, card and popup size, and so on. The lookups themselves run in core.Controller,
which this drives from an NSTimer on the main thread.
"""
from __future__ import annotations

import ctypes
import os
import shlex
import subprocess
import sys
from pathlib import Path

import objc
from AppKit import (
    NSAlert, NSAlertFirstButtonReturn, NSApplication, NSApplicationActivationPolicyAccessory,
    NSAttributedString, NSBackingStoreBuffered, NSBezierPath, NSColor, NSCompositingOperationClear,
    NSCursor, NSEvent, NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSImage,
    NSImageScaleProportionallyUpOrDown, NSImageView, NSMakeRect, NSMenu, NSMenuItem, NSPanel,
    NSRectFill, NSRectFillUsingOperation, NSScreen, NSScreenSaverWindowLevel, NSStatusBar,
    NSStatusWindowLevel, NSTextField, NSVariableStatusItemLength, NSView, NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel, NSWorkspace,
)
from Foundation import (
    NSURL, NSActivityUserInitiatedAllowingIdleSystemSleep, NSBundle, NSData, NSObject,
    NSProcessInfo, NSRunLoop, NSRunLoopCommonModes, NSTimer,
)
from PyObjCTools import AppHelper

from . import __version__
from .core import (APP_DIR, CARD_SIZES, FROZEN, HOVER_DELAYS, POPUP_SIZES, Controller, Scryfall, Settings,
                   log, logger)

ASSETS = Path(__file__).parent / "assets"
MENU_ICON = "rectangle.portrait.on.rectangle.portrait.angled"  # SF Symbol: two fanned cards
SCREEN_RECORDING_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


# --------------------------------------------------------------------------- system


def screen_capture_allowed(ask: bool = False):
    """True/False: whether macOS lets this process see other apps' windows. Without
    Screen Recording permission, grabs quietly show only the wallpaper. `ask` shows the
    system prompt (macOS only shows it the first time). None: unknown."""
    try:
        cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    except (OSError, AttributeError):
        return None
    if cg.CGPreflightScreenCaptureAccess():
        return True
    if ask:
        cg.CGRequestScreenCaptureAccess()
    return False


def primary_height() -> float:
    return NSScreen.screens()[0].frame().size.height


def pointer():
    """Pointer position in the top-left-origin points that mss uses."""
    loc = NSEvent.mouseLocation()
    return int(loc.x), int(primary_height() - loc.y)


def login_item():
    """The app's own "open at login" registration, or None when running from source
    (only an app bundle can register itself)."""
    if not FROZEN:
        return None
    try:
        from ServiceManagement import SMAppService
        return SMAppService.mainAppService()
    except Exception:
        return None


def relaunch():
    """Start a fresh copy of Card Peek and quit this one (macOS only applies a new Screen
    Recording permission to a fresh process)."""
    if FROZEN:
        bundle = NSBundle.mainBundle().bundlePath()
        subprocess.Popen(["/bin/sh", "-c", f"sleep 1; /usr/bin/open -n {shlex.quote(bundle)}"])
        NSApplication.sharedApplication().terminate_(None)
    else:
        os.execv(sys.executable, [sys.executable, "-m", "cardpeek"])


def alert(title: str, text: str, buttons=("OK",), field: str | None = None):
    """A modal alert. Returns (index of the button pressed, text typed) — the text field
    only appears if `field` (its placeholder) is given."""
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    for b in buttons:
        a.addButtonWithTitle_(b)
    entry = None
    if field is not None:
        entry = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 220, 24))
        entry.setPlaceholderString_(field)
        a.setAccessoryView_(entry)
        a.window().setInitialFirstResponder_(entry)
    pressed = a.runModal() - NSAlertFirstButtonReturn
    return pressed, (entry.stringValue().strip() if entry is not None else "")


# --------------------------------------------------------------------------- popup


class MacPopup:
    """Native popup: sharp on Retina, click-through, never takes focus, and shows over
    full-screen apps."""

    def __init__(self):
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 10, 10), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary
                                     | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setBackgroundColor_(NSColor.blackColor())
        panel.setHasShadow_(True)
        view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        panel.setContentView_(view)
        self.panel, self.view = panel, view
        self.images: dict = {}

    def show(self, key, img, x: int, y: int, w: int, h: int):
        nsimg = self.images.get(key)
        if nsimg is None:
            if len(self.images) > 40:
                self.images.clear()
            import io
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            data = buf.getvalue()
            nsimg = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
            self.images[key] = nsimg
        self.view.setImage_(nsimg)
        # Cocoa measures from the bottom-left of the primary screen, with y pointing up.
        self.panel.setFrame_display_(NSMakeRect(x, primary_height() - y - h, w, h), True)
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)


# --------------------------------------------------------------------------- stream area


class KeyableWindow(NSWindow):
    """Borderless windows can't take key events (Escape) unless they say so."""

    def canBecomeKeyWindow(self):
        return True


class AreaView(NSView):
    """Dims a screen and lets the user drag out the stream area on it."""

    def isFlipped(self):
        return False

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.crosshairCursor())

    @objc.python_method
    def _point(self, event):
        return self.convertPoint_fromView_(event.locationInWindow(), None)

    @objc.python_method
    def _rect(self):
        (x0, y0), (x1, y1) = self.start, self.end
        return NSMakeRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    def mouseDown_(self, event):
        self.start = self.end = self._point(event)
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        if getattr(self, "start", None) is not None:
            self.end = self._point(event)
            self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        if getattr(self, "start", None) is None:
            return
        self.end = self._point(event)
        rect = self.window().convertRectToScreen_(self._rect())
        self.picker.finish(rect)

    def keyDown_(self, event):
        if event.keyCode() == 53:  # Escape
            self.picker.finish(None)

    def drawRect_(self, dirty):
        bounds = self.bounds()
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.45).set()
        NSRectFill(bounds)
        start = getattr(self, "start", None)
        if start is not None:
            rect = self._rect()
            NSColor.clearColor().set()
            NSRectFillUsingOperation(rect, NSCompositingOperationClear)
            NSColor.controlAccentColor().set()
            path = NSBezierPath.bezierPathWithRect_(rect)
            path.setLineWidth_(3)
            path.stroke()
        if self.hint:
            text = NSAttributedString.alloc().initWithString_attributes_(self.hint, {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(20, 0.3),
                NSForegroundColorAttributeName: NSColor.whiteColor()})
            size = text.size()
            text.drawAtPoint_(((bounds.size.width - size.width) / 2, bounds.size.height - 120))


class AreaPicker:
    """One dimmed overlay per screen; drag a box on any of them. Calls `done(area)` with
    (left, top, width, height) in top-left-origin points, or None if cancelled."""

    HINT = "Drag a box around the video stream.   Esc or a click without dragging cancels."

    def __init__(self, done):
        self.done = done
        self.windows = []
        for screen in NSScreen.screens():
            frame = screen.frame()
            win = KeyableWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setLevel_(NSScreenSaverWindowLevel)
            win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                       | NSWindowCollectionBehaviorFullScreenAuxiliary)
            win.setReleasedWhenClosed_(False)
            view = AreaView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
            view.picker, view.start, view.hint = self, None, self.HINT
            win.setContentView_(view)
            self.windows.append(win)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        under = next((w for w in self.windows if _contains(w.frame(), NSEvent.mouseLocation())), self.windows[0])
        for win in self.windows:
            win.orderFrontRegardless()
        under.makeKeyAndOrderFront_(None)
        under.makeFirstResponder_(under.contentView())

    def finish(self, rect):
        for win in self.windows:
            win.orderOut_(None)
        self.windows = []
        area = None
        if rect is not None and rect.size.width > 120 and rect.size.height > 80:
            top = primary_height() - rect.origin.y - rect.size.height
            area = [int(rect.origin.x), int(top), int(rect.size.width), int(rect.size.height)]
        self.done(area)


def _contains(frame, point) -> bool:
    return (frame.origin.x <= point.x < frame.origin.x + frame.size.width
            and frame.origin.y <= point.y < frame.origin.y + frame.size.height)


# --------------------------------------------------------------------------- menu bar app


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, note):
        try:
            self._start()
        except Exception:
            logger.exception("Startup failed")
            raise

    @objc.python_method
    def _start(self):
        app = NSApplication.sharedApplication()
        if not FROZEN and (ASSETS / "CardPeek.icns").exists():
            app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(ASSETS / "CardPeek.icns")))
        # App Nap throttles timers in apps without visible windows, which would turn the
        # 20 ms pointer check into seconds.
        self.activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "Watching the pointer for card lookups")

        self.settings = Settings()
        first_run = self.settings.first_run
        self.controller = Controller(self.settings, Scryfall(APP_DIR), self._changed)
        self.controller.popup = MacPopup()
        self.controller.pointer = pointer
        self.menu_open = False
        self.controller.ignore_point = lambda x, y: self.menu_open
        self.picker = None
        self.asked_for = None  # a set the user chose from the menu, until it loads or fails
        self.can_capture = screen_capture_allowed()

        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        button = self.item.button()
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(MENU_ICON, "Card Peek")
        icon.setTemplate_(True)
        button.setImage_(icon)
        button.setImagePosition_(2)  # NSImageLeft: the icon, then any progress text
        button.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(0, 0))
        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self)
        self.menu.setAutoenablesItems_(False)
        self.item.setMenu_(self.menu)

        self.controller.start()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            Controller.TICK_MS / 1000, self, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        self._refresh_button()

        if first_run:
            self.settings.save()
            AppHelper.callLater(0.5, self._welcome)
        elif self.can_capture is False:
            log("Screen Recording permission is off.")

    @objc.python_method
    def _welcome(self):
        alert("Card Peek is in your menu bar",
              "Look for the two-cards icon at the top right of your screen: that's where you pick the "
              "set being drafted and the part of the screen the stream is in.\n\n"
              f"Card Peek is getting the OCR engine and {self.settings['set'].upper()} ready now. Then rest "
              "your pointer on a card in the stream to see it in full.\n\n"
              "Next, macOS will ask to let Card Peek record the screen. It needs that to read card names; "
              "nothing is recorded or sent anywhere.")
        if self.can_capture is False:
            screen_capture_allowed(ask=True)

    # ---- state changes

    def tick_(self, timer):
        self.controller.tick()

    @objc.python_method
    def _changed(self, what):
        self._refresh_button()
        if self.menu_open:
            self._fill_status()
        if what == "deck" and self.asked_for:
            self.asked_for = None
        elif what == "error" and self.asked_for:
            self.asked_for = None
            AppHelper.callAfter(alert, "Couldn't load that set", self.controller.error)

    @objc.python_method
    def _refresh_button(self):
        c = self.controller
        button = self.item.button()
        p = c.progress
        button.setTitle_(f" {p:.0%}" if p is not None else "")
        working = c.ready and self.settings["enabled"] and self.can_capture is not False
        button.setAppearsDisabled_(not working)
        button.setToolTip_("Card Peek\n" + c.status)

    # ---- the menu

    def menuWillOpen_(self, menu):
        if menu is self.menu:
            self.menu_open = True
            self.controller.hide_popup()

    def menuDidClose_(self, menu):
        if menu is self.menu:
            self.menu_open = False

    def menuNeedsUpdate_(self, menu):
        if menu is not self.menu:
            return
        try:
            self._build_menu()
        except Exception:
            logger.exception("Building the menu failed")

    @objc.python_method
    def _add(self, menu, title, action=None, value=None, checked=False, enabled=True, key=""):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self)
        if value is not None:
            item.setRepresentedObject_(value)
        item.setState_(1 if checked else 0)
        item.setEnabled_(enabled and action is not None)
        menu.addItem_(item)
        return item

    @objc.python_method
    def _submenu(self, menu, title):
        item = self._add(menu, title)
        item.setEnabled_(True)
        sub = NSMenu.alloc().initWithTitle_(title)
        sub.setAutoenablesItems_(False)
        item.setSubmenu_(sub)
        return sub

    @staticmethod
    def _header(menu, title):
        if hasattr(NSMenuItem, "sectionHeaderWithTitle_"):
            menu.addItem_(NSMenuItem.sectionHeaderWithTitle_(title))
        else:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            item.setEnabled_(False)
            menu.addItem_(item)

    @objc.python_method
    def _fill_status(self):
        lines = self.controller.status.split("\n")
        if self.can_capture is False:
            lines.append("Screen Recording is off, so Card Peek can't see the stream.")
        for i, item in enumerate(self.status_items):
            item.setTitle_(lines[i] if i < len(lines) else "")
            item.setHidden_(i >= len(lines))

    @objc.python_method
    def _build_menu(self):
        c, s, menu = self.controller, self.settings, self.menu
        menu.removeAllItems()

        self.status_items = [self._add(menu, "") for _ in range(3)]
        self._fill_status()
        if c.last and s["debug"]:
            self._add(menu, c.last[:90])
        menu.addItem_(NSMenuItem.separatorItem())

        self._add(menu, "Peek at Cards", "toggleEnabled:", checked=s["enabled"], key="p")
        menu.addItem_(NSMenuItem.separatorItem())

        # The set: downloaded ones first, then recent ones that can be downloaded.
        current = c.loading or (c.deck.info.code if c.deck else s["set"])
        label = c.deck.label if c.deck and c.deck.info.code == current else current.upper()
        sets = self._submenu(menu, f"Set: {label}")
        downloaded = c.scryfall.downloaded_sets()
        have = {d.code for d in downloaded}
        if downloaded:
            self._header(sets, "Downloaded")
            for d in downloaded:
                self._add(sets, f"{d.name} ({d.code.upper()})", "chooseSet:", d.code, checked=d.code == current)
        self._header(sets, "Download from Scryfall")
        if c.draft_sets is None:
            self._add(sets, "Getting the list of sets…")
        else:
            for d in c.draft_sets:
                if d.code in have:
                    continue
                soon = " (previews)" if d.released and d.released > _today() else ""
                self._add(sets, f"{d.name} ({d.code.upper()}){soon}", "chooseSet:", d.code,
                          checked=d.code == current)
        self._add(sets, "Other Set Code…", "otherSet:")
        sets.addItem_(NSMenuItem.separatorItem())
        refresh = self._add(sets, f"Update {label} from Scryfall", "refreshSet:", enabled=c.deck is not None and not c.loading)
        refresh.setToolTip_("Download the card list again, e.g. once all of a new set's cards are previewed.")

        area = self._submenu(menu, "Stream Area")
        a = s["area"]
        self._add(area, "Whole Screen", "wholeScreen:", checked=not a)
        if a:
            self._add(area, f"{a[2]} × {a[3]} at ({a[0]}, {a[1]})", "selectArea:", checked=True)
        area.addItem_(NSMenuItem.separatorItem())
        self._add(area, "Select Stream Area…", "selectArea:")

        size = self._submenu(menu, "Card Size")
        for v in sorted(set(CARD_SIZES) | {s["card_scale"]}):
            note = " (MTG Arena)" if v == 1.0 else ""
            self._add(size, f"{v:.0%}{note}", "setCardSize:", v, checked=abs(v - s["card_scale"]) < 1e-6)
        self._header(size, "Change if the streamer zooms in or out")

        popup = self._submenu(menu, "Popup Size")
        for v in sorted(set(POPUP_SIZES) | {s["popup_pct"]}):
            self._add(popup, f"{v:.0f}% of screen height", "setPopupSize:", v, checked=v == s["popup_pct"])

        delay = self._submenu(menu, "Hover Delay")
        for v, name in HOVER_DELAYS:
            self._add(delay, f"{name} ({v * 1000:.0f} ms)", "setHoverDelay:", v, checked=abs(v - s["dwell"]) < 1e-6)

        if self.can_capture is False:
            menu.addItem_(NSMenuItem.separatorItem())
            self._add(menu, "Allow Screen Recording…", "openScreenRecording:")
            self._add(menu, "Relaunch Card Peek", "relaunch:")

        menu.addItem_(NSMenuItem.separatorItem())
        service = login_item()
        if service is not None:
            self._add(menu, "Open at Login", "toggleLogin:", checked=service.status() == 1)
        trouble = self._submenu(menu, "Troubleshooting")
        self._add(trouble, "Save Snapshots of What OCR Reads", "toggleDebug:", checked=s["debug"])
        self._add(trouble, "Show Snapshots", "showFolder:", str(APP_DIR / "debug"),
                  enabled=(APP_DIR / "debug").exists())
        self._add(trouble, "Show Log", "showFolder:", str(APP_DIR / "cardpeek.log"))
        self._add(menu, "About Card Peek", "about:")
        self._add(menu, "Quit Card Peek", "quit:", key="q")

    # ---- menu actions

    def toggleEnabled_(self, sender):
        self.controller.set_enabled(not self.settings["enabled"])
        self._refresh_button()

    def chooseSet_(self, sender):
        code = sender.representedObject()
        if self.controller.deck and self.controller.deck.info.code == code and not self.controller.loading:
            return
        self.asked_for = code
        self.controller.load_set(code)

    def otherSet_(self, sender):
        pressed, code = alert("Load a set by its code",
                              "Use the code Scryfall shows for the set, e.g. FRA for Reality Fracture "
                              "(it's in the address of the set's page: scryfall.com/sets/fra). "
                              "Card Peek downloads the set if it doesn't have it yet.",
                              ("Load", "Cancel"), field="Set code")
        if pressed == 0 and code:
            if not code.isalnum() or len(code) > 6:
                alert("That isn't a set code", "Set codes are 3–5 letters or digits, like FRA or EOE.")
                return
            self.asked_for = code.lower()
            self.controller.load_set(code)

    def refreshSet_(self, sender):
        if self.controller.deck:
            self.asked_for = self.controller.deck.info.code
            self.controller.load_set(self.controller.deck.info.code, refresh=True)

    def wholeScreen_(self, sender):
        self.controller.set_area(None)

    def selectArea_(self, sender):
        self.controller.hide_popup()

        def done(area):
            self.picker = None
            if area:
                self.controller.set_area(area)
        self.picker = AreaPicker(done)

    def setCardSize_(self, sender):
        self.controller.update(card_scale=float(sender.representedObject()))

    def setPopupSize_(self, sender):
        self.controller.update(popup_pct=float(sender.representedObject()))

    def setHoverDelay_(self, sender):
        self.controller.update(dwell=float(sender.representedObject()))

    def openScreenRecording_(self, sender):
        if screen_capture_allowed(ask=True) is False:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(SCREEN_RECORDING_SETTINGS))

    def relaunch_(self, sender):
        self.controller.quit()
        relaunch()

    def toggleLogin_(self, sender):
        service = login_item()
        try:
            if service.status() == 1:
                ok, err = service.unregisterAndReturnError_(None)
            else:
                ok, err = service.registerAndReturnError_(None)
            if not ok:
                alert("Couldn't change Open at Login", str(err.localizedDescription() if err else ""))
        except Exception as e:
            logger.exception("Open at Login failed")
            alert("Couldn't change Open at Login", str(e))

    def toggleDebug_(self, sender):
        self.controller.update(debug=not self.settings["debug"])

    def showFolder_(self, sender):
        path = sender.representedObject()
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, "")

    def about_(self, sender):
        app = NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        credits = NSAttributedString.alloc().initWithString_attributes_(
            "Card data and images from Scryfall. Text recognition by RapidOCR.\n"
            "Card Peek is unofficial Fan Content permitted under the Fan Content Policy. "
            "Not approved/endorsed by Wizards. Portions of the materials used are property of "
            "Wizards of the Coast. ©Wizards of the Coast LLC.",
            {NSFontAttributeName: NSFont.systemFontOfSize_(10),
             NSForegroundColorAttributeName: NSColor.secondaryLabelColor()})
        app.orderFrontStandardAboutPanelWithOptions_({
            "ApplicationName": "Card Peek", "ApplicationVersion": __version__, "Version": "",
            "Credits": credits})

    def quit_(self, sender):
        NSApplication.sharedApplication().terminate_(None)

    def applicationWillTerminate_(self, note):
        self.controller.quit()


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def run():
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    # No Dock icon: menu bar apps are "accessory" apps, which is also what lets the popup
    # appear over another app's full-screen Space (full-screen Discord).
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    log(f"Card Peek {__version__} starting")
    AppHelper.runEventLoop(installInterrupt=not FROZEN)
