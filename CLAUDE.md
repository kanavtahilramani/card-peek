# Card Peek: notes for Claude

A macOS menu bar app and Windows tray app (Python, PyInstaller) that reads Magic card names off a video stream with OCR and pops up the full card. `cardpeek/core.py` is platform-neutral; `cardpeek/mac.py` is the menu bar app; `cardpeek/win.py` is the Windows tray app (Win32 through ctypes, plus the Tk popup and stream area picker from `cardpeek/tk_ui.py`, which is also the stand-in UI for Linux). Both platforms use the same RapidOCR models; Windows' own OCR was measured and is far worse on stream video (see README.md). See README.md for the full picture.

## Releasing a new version

1. Bump `__version__` in `cardpeek/__init__.py` (e.g. `1.0.1`) and commit it on `main`.
2. Tag and push both:
   ```sh
   git tag -a v1.0.1 -m "Card Peek 1.0.1"
   git push origin main v1.0.1
   ```
3. `.github/workflows/build.yml` takes it from there (about 10 minutes). The macOS job builds, signs with the Developer ID, self-tests the built app, notarizes and staples; the Windows job builds the standalone `.exe` and self-tests it; once both pass, the release job publishes `CardPeek-<version>.dmg` and `CardPeek-<version>.exe` as a GitHub release with generated notes. Both build jobs fail early if the tag doesn't match `__version__`.
4. Check the release: `gh run watch`, then download the DMG from the release page and run `spctl --assess --type open --context context:primary-signature -v` on it. It should say `source=Notarized Developer ID`. On Windows, download the `.exe` and open it.

The repo is public, so Actions minutes (macOS and Windows included) are free.

Signing and notarization credentials:
- **CI:** the repository secrets `MACOS_CERT_P12`, `MACOS_CERT_PASSWORD`, `NOTARY_KEY_P8`, `NOTARY_KEY_ID` and `NOTARY_ISSUER`, set with `packaging/set_ci_secrets.sh`. That script asks for a password, so it has to run in a real terminal, not through Claude Code's `!` prompt.
- **Local:** `NOTARY_PROFILE=cardpeek CODESIGN_IDENTITY="Developer ID Application: …" packaging/build_mac.sh`. The `cardpeek` notarytool profile lives in the login keychain.

## Checks before pushing

- `python -m cardpeek --self-test`: OCR end to end on a synthetic board, and on Windows the tray icon, menu, popup, area picker and set code dialog too (a tray icon and a few windows flash up). CI runs it from source and inside the built apps.
- `packaging/build_mac.sh` with no environment does an ad-hoc signed build, which is enough to check that packaging still works.
- On Windows, `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` builds `dist\CardPeek-<version>.exe` and self-tests it.

## Keep private things out of the public repo

- Commits must use the GitHub noreply address `9064912+kanavtahilramani@users.noreply.github.com`; it's set in this repo's git config (in a fresh clone: `git config user.email 9064912+kanavtahilramani@users.noreply.github.com`). Never commit with a personal email.
- Don't commit or publish local paths, logs, `~/.cardpeek` or `%LOCALAPPDATA%\CardPeek` contents, other projects' details, certificates, `.p12`/`.p8` files or key IDs. `build/` and `dist/` are git-ignored.

## Still to do

- Code-sign the Windows `.exe` (e.g. Azure Artifact Signing, or SignPath's free plan for open-source projects) so SmartScreen stops warning on first run.
- The careful text detector takes ~1.2 s on a fast x64 CPU (it only runs when the quick pass finds no name, mostly on far-side cards). DirectML (`onnxruntime-directml`) could put it on the GPU on Windows.
