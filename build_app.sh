#!/bin/sh
# Build a standalone app bundle for the Backrooms walkthrough.
#
#   ./build_app.sh            # macOS: dist/The Backrooms.app
#
# Works on Windows/Linux too (icon and bundle format adapt automatically).
set -e

pip install pyinstaller pygame-ce numpy

case "$(uname -s)" in
    Darwin) ICON="assets/icon.icns" ;;
    *)      ICON="assets/icon.png" ;;
esac

pyinstaller --noconfirm --windowed --name "The Backrooms" \
    --icon "$ICON" \
    --add-data "assets:assets" \
    backrooms_walk.py

echo
echo "Done. Look in dist/ — on macOS: 'dist/The Backrooms.app'"
