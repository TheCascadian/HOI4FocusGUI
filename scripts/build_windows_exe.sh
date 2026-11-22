#!/usr/bin/env bash
set -euo pipefail

# Automated local Linux -> Windows (.exe) builder using Wine + Windows Python + PyInstaller.
# Requires: wine (64-bit) and curl.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build"
DIST_DIR="$ROOT_DIR/dist"
OUT_DIR="$ROOT_DIR/artifacts"
APP_NAME="HOI4FocusGUI"
PY_VER="3.12.8"
PY_INSTALLER="python-${PY_VER}-amd64.exe"
PY_URL="https://www.python.org/ftp/python/${PY_VER}/${PY_INSTALLER}"

export WINEPREFIX="$BUILD_DIR/.wine-win64"
export WINEARCH="win64"
export WINEDEBUG="-all"

if ! command -v wine >/dev/null 2>&1; then
  echo "Error: 'wine' is not installed. Install Wine (e.g. sudo apt install wine64 / sudo pacman -S wine) and re-run."
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "Error: 'curl' is not installed."
  exit 1
fi

mkdir -p "$BUILD_DIR" "$OUT_DIR"
cd "$ROOT_DIR"

# Locate or bootstrap Windows Python inside the Wine prefix
WIN_PY=""
find_win_python() {
  local cand
  for cand in \
    "$WINEPREFIX/drive_c/Python312/python.exe" \
    "$WINEPREFIX/drive_c/users/${USER:-root}/AppData/Local/Programs/Python/Python312/python.exe" \
    "$WINEPREFIX/drive_c/Program Files/Python312/python.exe"; do
    if [[ -f "$cand" ]]; then
      WIN_PY="$cand"
      return 0
    fi
  done
  return 1
}

wait_with_heartbeat() {
  local label="$1"
  shift
  "$@" &
  local pid=$!
  local secs=0
  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  -> %s... (%ds elapsed)" "$label" "$secs"
    sleep 2
    secs=$((secs + 2))
  done
  wait "$pid"
  printf "\r  -> %s... done (%ds)      \n" "$label" "$secs"
}

if ! find_win_python; then
  echo "[1/4] Bootstrapping Windows Python ${PY_VER} in Wine prefix ($WINEPREFIX)"
  echo "  -> Initializing Wine prefix (a 'Wine configuration' popup may appear briefly)..."
  wait_with_heartbeat "Initializing Wine prefix" wineboot --init
  if [[ ! -f "$BUILD_DIR/$PY_INSTALLER" ]]; then
    echo "  -> Downloading $PY_INSTALLER..."
    curl -fL --progress-bar "$PY_URL" -o "$BUILD_DIR/$PY_INSTALLER"
  else
    echo "  -> Using cached $BUILD_DIR/$PY_INSTALLER"
  fi
  wait_with_heartbeat "Installing Windows Python ${PY_VER} silently" \
    wine "$BUILD_DIR/$PY_INSTALLER" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir="C:\\Python312"
  wineserver -w >/dev/null 2>&1 || true
  if ! find_win_python; then
    echo "Error: Windows Python installation failed in $WINEPREFIX."
    exit 1
  fi
else
  echo "[1/4] Reusing cached Windows Python at $WIN_PY"
fi

echo "  -> Verifying Wine Python: $(wine "$WIN_PY" --version 2>/dev/null | tr -d '\r')"

echo "[2/4] Installing Windows Python dependencies"
wine "$WIN_PY" -m pip install --upgrade pip
wine "$WIN_PY" -m pip install -r requirements.txt pyinstaller

echo "[3/4] Building Windows executable with PyInstaller"
mkdir -p "$BUILD_DIR/win64-work" "$DIST_DIR/win64"
WIN_ROOT="$(winepath -w "$ROOT_DIR" | tr -d '\r')"
wine "$WIN_PY" -m PyInstaller --noconfirm --clean --windowed --onefile \
  --name "$APP_NAME" \
  --workpath "$BUILD_DIR/win64-work" \
  --distpath "$DIST_DIR/win64" \
  --specpath "$BUILD_DIR/win64-work" \
  --paths "$WIN_ROOT\\source" \
  --add-data "$WIN_ROOT\\source\\_assets;_assets" \
  --add-data "$WIN_ROOT\\source\\fonts;fonts" \
  --add-data "$WIN_ROOT\\source\\version.txt;." \
  "$WIN_ROOT\\source\\_focusGUI.py"

echo "[4/4] Packaging artifact"
cp -f "$DIST_DIR/win64/${APP_NAME}.exe" "$OUT_DIR/${APP_NAME}-windows-x86_64.exe"
ls -lh "$OUT_DIR/${APP_NAME}-windows-x86_64.exe"
echo "Done: $OUT_DIR/${APP_NAME}-windows-x86_64.exe"
