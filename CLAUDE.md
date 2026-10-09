# Card Peek: notes for Claude

A macOS menu bar app (Python, PyInstaller) that reads Magic card names off a video stream with OCR and pops up the full card. `cardpeek/core.py` is platform-neutral; `cardpeek/mac.py` is the menu bar app; `cardpeek/tk_ui.py` is the stand-in UI for Windows/Linux. See README.md for the full picture.

## Releasing a new version

1. Bump `__version__` in `cardpeek/__init__.py` (e.g. `1.0.1`) and commit it on `main`.
2. Tag and push both:
   ```sh
   git tag -a v1.0.1 -m "Card Peek 1.0.1"
   git push origin main v1.0.1
   ```
3. `.github/workflows/macos.yml` takes it from there (about 5 minutes): build, sign with the Developer ID, self-test the built app, notarize, staple, and publish `CardPeek-<version>.dmg` as a GitHub release with generated notes. It fails early if the tag doesn't match `__version__`.
4. Check the release: `gh run watch`, then download the DMG from the release page and run `spctl --assess --type open --context context:primary-signature -v` on it. It should say `source=Notarized Developer ID`.

The repo is public, so Actions minutes (macOS included) are free.

Signing and notarization credentials:
- **CI:** the repository secrets `MACOS_CERT_P12`, `MACOS_CERT_PASSWORD`, `NOTARY_KEY_P8`, `NOTARY_KEY_ID` and `NOTARY_ISSUER`, set with `packaging/set_ci_secrets.sh`. That script asks for a password, so it has to run in a real terminal, not through Claude Code's `!` prompt.
- **Local:** `NOTARY_PROFILE=cardpeek CODESIGN_IDENTITY="Developer ID Application: …" packaging/build_mac.sh`. The `cardpeek` notarytool profile lives in the login keychain.

## Checks before pushing

- `python -m cardpeek --self-test`: OCR end to end on a synthetic board. CI runs it from source and inside the built app.
- `packaging/build_mac.sh` with no environment does an ad-hoc signed build, which is enough to check that packaging still works.

## Keep private things out of the public repo

- Commits must use the GitHub noreply address `9064912+kanavtahilramani@users.noreply.github.com`; it's set in this repo's git config. Never commit with a personal email.
- Don't commit or publish local paths, logs, `~/.cardpeek` contents, other projects' details, certificates, `.p12`/`.p8` files or key IDs. `build/` and `dist/` are git-ignored.

## Still to do

- A Windows x64 build (tray app, a `windows-latest` CI job, an installer, optional signing). See the "Windows" section of README.md.
