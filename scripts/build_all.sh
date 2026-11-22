#!/usr/bin/env bash
set -euo pipefail

# Build both the Linux AppImage and Windows .exe in parallel.
# Logs are written to build/logs/ while live progress is reported.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/build/logs"
OUT_DIR="$ROOT_DIR/artifacts"
mkdir -p "$LOG_DIR" "$OUT_DIR"

APPIMAGE_LOG="$LOG_DIR/build_appimage.log"
WINEXE_LOG="$LOG_DIR/build_windows_exe.log"

echo "Starting parallel builds..."
echo "  AppImage log:    $APPIMAGE_LOG"
echo "  Windows EXE log: $WINEXE_LOG"

"$ROOT_DIR/scripts/build_appimage.sh" >"$APPIMAGE_LOG" 2>&1 &
PID_APPIMAGE=$!

"$ROOT_DIR/scripts/build_windows_exe.sh" >"$WINEXE_LOG" 2>&1 &
PID_WINEXE=$!

secs=0
while kill -0 "$PID_APPIMAGE" 2>/dev/null || kill -0 "$PID_WINEXE" 2>/dev/null; do
  s_app="done"
  s_win="done"
  kill -0 "$PID_APPIMAGE" 2>/dev/null && s_app="running"
  kill -0 "$PID_WINEXE" 2>/dev/null && s_win="running"
  printf "\r[%3ds] AppImage: %-7s | Windows EXE: %-7s" "$secs" "$s_app" "$s_win"
  sleep 3
  secs=$((secs + 3))
done
echo ""

RC_APP=0
RC_WIN=0
wait "$PID_APPIMAGE" || RC_APP=$?
wait "$PID_WINEXE" || RC_WIN=$?

if [[ $RC_APP -ne 0 ]]; then
  echo "ERROR: AppImage build failed (exit $RC_APP). Last 40 lines of $APPIMAGE_LOG:"
  tail -n 40 "$APPIMAGE_LOG"
fi

if [[ $RC_WIN -ne 0 ]]; then
  echo "ERROR: Windows EXE build failed (exit $RC_WIN). Last 40 lines of $WINEXE_LOG:"
  tail -n 40 "$WINEXE_LOG"
fi

if [[ $RC_APP -ne 0 || $RC_WIN -ne 0 ]]; then
  exit 1
fi

echo "Both builds succeeded:"
ls -lh "$OUT_DIR/HOI4FocusGUI-x86_64.AppImage" "$OUT_DIR/HOI4FocusGUI-windows-x86_64.exe"
