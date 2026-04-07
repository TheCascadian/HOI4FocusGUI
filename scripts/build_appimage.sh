#!/usr/bin/env bash
set -euo pipefail

# Easy-to-read builder for Linux testing artifacts.
# It creates a PyInstaller bundle, wraps it into an AppDir,
# then turns that AppDir into an AppImage.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build"
DIST_DIR="$ROOT_DIR/dist"
APPDIR="$BUILD_DIR/AppDir"
OUT_DIR="$ROOT_DIR/artifacts"
APP_NAME="HOI4FocusGUI"
APPIMAGE_NAME="${APP_NAME}-x86_64.AppImage"
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
VENV_DIR="$BUILD_DIR/.venv-appimage"

mkdir -p "$BUILD_DIR" "$OUT_DIR"

cd "$ROOT_DIR"

echo "[1/6] Installing Python build dependencies"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

echo "[2/6] Building PyInstaller one-folder app"
pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --add-data "source/_assets:_assets" \
  --add-data "source/fonts:fonts" \
  --add-data "source/version.txt:." \
  source/_focusGUI.py

echo "[3/6] Preparing AppDir layout"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/$APP_NAME"

cp -a "$DIST_DIR/$APP_NAME/." "$APPDIR/usr/lib/$APP_NAME/"

# AppRun is the AppImage entry point. It only needs to forward execution
# to the bundled PyInstaller binary.
cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/usr/lib/HOI4FocusGUI/HOI4FocusGUI" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cp "$ROOT_DIR/packaging/linux/hoi4focusgui.desktop" "$APPDIR/hoi4focusgui.desktop"
cp "$ROOT_DIR/source/_assets/mutex.png" "$APPDIR/hoi4focusgui.png"

# Also install desktop metadata inside usr/share for compatibility.
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps" "$APPDIR/usr/share/metainfo"
cp "$ROOT_DIR/packaging/linux/hoi4focusgui.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT_DIR/source/_assets/mutex.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/hoi4focusgui.png"
cp "$ROOT_DIR/packaging/linux/io.github.cpntodd.HOI4FocusGUI.metainfo.xml" "$APPDIR/usr/share/metainfo/"

echo "[4/6] Fetching appimagetool (if needed)"
if [[ ! -x "$APPIMAGETOOL" ]]; then
  curl -L "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" -o "$APPIMAGETOOL"
  chmod +x "$APPIMAGETOOL"
fi

echo "[5/6] Creating AppImage"
rm -f "$OUT_DIR/$APPIMAGE_NAME"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" "$APPDIR" "$OUT_DIR/$APPIMAGE_NAME"

# Generate a fallback launcher for environments where AppImage cannot mount
# due to missing libfuse.so.2. Users can double-click this script instead.
cat > "$OUT_DIR/Launch-HOI4FocusGUI.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APPIMAGE="$HERE/HOI4FocusGUI-x86_64.AppImage"
if [[ ! -x "$APPIMAGE" ]]; then
  echo "AppImage not found: $APPIMAGE"
  exit 1
fi
exec env APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGE" "$@"
EOF
chmod +x "$OUT_DIR/Launch-HOI4FocusGUI.sh"

cat > "$OUT_DIR/README-LAUNCH.txt" <<'EOF'
If HOI4FocusGUI-x86_64.AppImage does not launch when double-clicked,
your system likely lacks libfuse.so.2 (common on newer distros).

Use this fallback launcher instead:
  Launch-HOI4FocusGUI.sh

This launcher runs the AppImage in extract-and-run mode, which bypasses
the FUSE runtime dependency.
EOF

cat > "$OUT_DIR/Launch-HOI4FocusGUI.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=HOI4 Focus GUI (No-FUSE Launcher)
Comment=Launch HOI4 Focus GUI in extract-and-run mode
Exec=sh -c '"$(dirname "%k")/Launch-HOI4FocusGUI.sh"'
Icon=applications-development
Terminal=false
Categories=Development;
EOF
chmod +x "$OUT_DIR/Launch-HOI4FocusGUI.desktop"

echo "[6/6] Done"
echo "AppImage created at: $OUT_DIR/$APPIMAGE_NAME"
echo "No-FUSE launcher: $OUT_DIR/Launch-HOI4FocusGUI.sh"
