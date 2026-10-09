# dmgbuild settings for the Card Peek DMG: the app and an Applications link side by side on
# the background from packaging/make_icon.py, which has an arrow from one to the other.
# Run from the top of the repo (packaging/build_mac.sh does):
#   dmgbuild -s packaging/dmg_settings.py -D app="dist/Card Peek.app" "Card Peek" out.dmg
# The window size and positions match DMG_SIZE, DMG_APP and DMG_APPLICATIONS in make_icon.py.
import os.path

app = defines["app"]  # noqa: F821  (dmgbuild provides `defines`)

filesystem = "APFS"
format = "ULMO"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = "cardpeek/assets/CardPeek.icns"  # the mounted disk's icon
background = "packaging/dmg/background.png"  # dmgbuild adds background@2x.png beside it
window_rect = ((200, 200), (640, 400))
default_view = "icon-view"
icon_size = 112
text_size = 13
icon_locations = {os.path.basename(app): (170, 190), "Applications": (470, 190)}
