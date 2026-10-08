# Card Peek

Rest your mouse on a Magic card in a video stream (MTG Arena over Discord, Twitch, YouTube…) and the full card pops up beside it.

Card Peek reads the card name under the pointer with OCR, matches it against the cards in the set being drafted, and shows the card image. It's built for Limited: it snapshots everything that can be opened in a set's boosters (the set, its bonus sheets and its Special Guests) from [Scryfall](https://scryfall.com), so hovering never touches the network.

## Install (macOS)

Card Peek runs on Macs with Apple silicon and macOS 14 or later.

1. Download `CardPeek-<version>.dmg` from the [latest release](../../releases/latest), open it, and drag **Card Peek** to **Applications**.
2. Open Card Peek. It lives in the menu bar (the two-cards icon). There's no Dock icon and no window.
3. When macOS asks, allow **Screen Recording** (System Settings → Privacy & Security → Screen & System Audio Recording). Card Peek needs it to read card names off the screen. Nothing is recorded or sent anywhere. After allowing it, choose **Relaunch Card Peek** from the menu.

On first launch Card Peek downloads its OCR engine (~90 MB, once) and the default set, Reality Fracture (~300 card images, ~80 MB). The menu shows progress, and the menu bar icon shows a percentage while a download runs.

## Use

Rest the pointer on a card in the stream. The popup stays up while the pointer is on that card.

Everything is set from the menu bar menu:

| Menu | What it does |
|---|---|
| **Peek at Cards** | Turn lookups on or off |
| **Set** | Switch between downloaded sets, or pick a recent set (or type any Scryfall set code) to download it. **Update … from Scryfall** re-downloads the card list, e.g. once a new set is fully previewed |
| **Stream Area** | Drag a box around the stream, or use the whole screen under the pointer |
| **Card Size** | Leave at 100% for MTG Arena; change it if the streamer zooms in or out |
| **Popup Size** | Height of the card popup, as a share of the screen |
| **Hover Delay** | How long the pointer must rest before a lookup |
| **Open at Login** | Start Card Peek when you log in |
| **Troubleshooting** | Save snapshots of what OCR reads, and find them and the log |

The set code is the one Scryfall uses (shown in the address of the set's page, e.g. `scryfall.com/sets/fra`).

## Files

Everything Card Peek downloads lives in `~/.cardpeek/`: `models/` (OCR engine), `sets/` (card lists), `images/` (card images), `settings.json`, `cardpeek.log`, and `debug/` if snapshots are on. Delete the folder to start fresh.

## Run from source

Needs Python 3.10+.

```sh
git clone git@github.com:kanavtahilramani/card-peek.git
cd card-peek
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 card_peek.py             # or: python3 -m cardpeek
python3 -m cardpeek --self-test  # OCR end to end on a made-up board, no screen needed
```

On macOS this runs the menu bar app; give Screen Recording permission to the terminal you run it from. Elsewhere (or with `CARDPEEK_UI=tk`) it opens a small control window with the same settings.

## Build and release (macOS)

```sh
pip install -r packaging/requirements-mac.txt   # exact versions, PyInstaller included
packaging/build_mac.sh                          # dist/Card Peek.app + dist/CardPeek-<version>.dmg
```

`build_mac.sh` builds the app with PyInstaller (`packaging/CardPeek.spec`), signs it (`packaging/sign_mac.sh`), runs the self-test inside the built app, makes the DMG, and notarizes and staples it. Without `CODESIGN_IDENTITY` it signs ad hoc, which only runs on the Mac that built it. See the top of the script for the signing and notarization settings.

The OCR models aren't in the app. Card Peek downloads them on first launch from RapidOCR's model hub and checks each against a SHA-256 pinned in `cardpeek/core.py`. That keeps the DMG at about 60 MB instead of about 115 MB.

GitHub Actions (`.github/workflows/macos.yml`) builds and self-tests every push and pull request. Pushing a tag that matches the version in `cardpeek/__init__.py` (e.g. `v1.0.0`) also signs with the Developer ID, notarizes, and publishes the DMG as a GitHub release. The workflow needs these repository secrets; `packaging/set_ci_secrets.sh` sets them:

| Secret | What |
|---|---|
| `MACOS_CERT_P12`, `MACOS_CERT_PASSWORD` | Developer ID Application certificate and private key (base64 .p12) and its password |
| `NOTARY_KEY_P8`, `NOTARY_KEY_ID`, `NOTARY_ISSUER` | App Store Connect API key (base64 .p8), its key ID and issuer ID |

To release: bump `__version__` in `cardpeek/__init__.py`, commit, then `git tag v<version> && git push --tags`.

App icon: `python packaging/make_icon.py` redraws `cardpeek/assets/CardPeek.icns`.

## Windows

A Windows build is planned but not done yet. The OCR, matching and Scryfall code (`cardpeek/core.py`) is platform-neutral, and `cardpeek/tk_ui.py` runs from source on Windows, but it hasn't been tested there recently. Still to do for a Windows release:

- Test on real x64 Windows: per-monitor DPI, the popup never taking focus from Discord, multi-monitor setups.
- A system tray app to match the macOS menu bar app.
- A `windows-latest` job in GitHub Actions: PyInstaller build, self-test, and an installer (e.g. Inno Setup).
- Optionally, code signing (e.g. Azure Trusted Signing) so SmartScreen doesn't warn.
