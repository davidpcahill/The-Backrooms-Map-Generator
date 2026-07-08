#!/bin/sh
# Build the standalone Backrooms app (the GL found-footage walkthrough).
#
#   ./build_app.sh            # macOS: dist/Backrooms.app
#
# Works on Windows/Linux too (icon and bundle format adapt automatically).
set -e

pip install pyinstaller pygame-ce numpy moderngl

case "$(uname -s)" in
    Darwin) ICON="assets/icon.icns" ;;
    *)      ICON="assets/icon.png" ;;
esac

pyinstaller --noconfirm --windowed --name "Backrooms" \
    --icon "$ICON" \
    --add-data "assets:assets" \
    backrooms_gl.py

echo
echo "Done. Look in dist/ — on macOS: dist/Backrooms.app"
