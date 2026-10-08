# Card Peek

Rest your mouse on a Magic card in a video stream (MTG Arena over Discord, Twitch, YouTube…) and the full card pops up beside it.

Card Peek reads the card name under the pointer with OCR, matches it against the cards in the set being drafted, and shows the card image. It's built for Limited: on first start it snapshots everything that can be opened in the set's boosters (the set, its bonus sheets and its Special Guests) from [Scryfall](https://scryfall.com), so hovering never touches the network.

Runs on macOS, Windows and Linux (X11). Needs Python 3.10+.

## Install

```sh
git clone git@github.com:kanavtahilramani/card-peek.git
cd card-peek
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```sh
python3 card_peek.py                 # Reality Fracture (FRA)
python3 card_peek.py --set EOE       # any set, by its Scryfall code
python3 card_peek.py --refresh       # re-download the set (e.g. after spoilers finish)
```

1. **First start** downloads the set's card list and images (~300 cards, ~80 MB, ~10 s) plus a one-time OCR model. Later starts take under a second.
2. Click **Select stream area** and drag a box around the stream (or use the whole screen).
3. Rest the pointer on a card. The popup stays up while the pointer is on that card.

The set code is the one Scryfall uses (shown in the URL of the set's page, e.g. `scryfall.com/sets/fra`). Card Peek adds the set's bonus sheets (e.g. OTJ brings in OTP and BIG) and the Special Guests released alongside it.

### macOS

Allow Screen Recording for the app you launch Card Peek from (Terminal, iTerm, VS Code…) in **System Settings → Privacy & Security → Screen & System Audio Recording**, then quit and reopen that app. Without it, Card Peek only sees your wallpaper.

## Options

| Flag | Default | What it does |
|---|---|---|
| `--set CODE` | `FRA` | Set being drafted |
| `--refresh` | off | Re-download the set snapshot instead of reusing it |
| `--dwell SECONDS` | `0.12` | How long the pointer must rest before a lookup |

In the window: **Card size** (change only if the streamer zooms the game), **Popup height**, and **Save debug snapshots**, which writes what OCR read to `~/.cardpeek/debug/`.

## Files

Everything downloaded lives in `~/.cardpeek/`, not in the repo: `sets/` (card lists), `images/` (card images), `settings.json`. Delete the folder to start fresh. The OCR models are stored by RapidOCR inside its own package folder.
