#!/usr/bin/env bash
# install.sh — first-time macOS / Linux setup.
#
#   chmod +x scripts/install.sh
#   ./scripts/install.sh
#
# Creates a venv, installs Lukav, and on macOS creates an
# /Applications/Lukav.app stub that double-clicks straight into the
# native window mode. On Linux a Lukav.desktop entry is written under
# ~/.local/share/applications.
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "[lukav] repo: $REPO_DIR"

if [ ! -x ".venv/bin/python" ]; then
    echo "[lukav] creating venv..."
    "${PYTHON:-python3}" -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate
echo "[lukav] installing deps..."
pip install --quiet --upgrade pip
pip install --quiet -e ".[plaid,secrets,desktop]"

case "$(uname -s)" in
  Darwin)
    APP="/Applications/Lukav.app"
    echo "[lukav] writing $APP..."
    mkdir -p "$APP/Contents/MacOS"
    cat >"$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Lukav</string>
  <key>CFBundleExecutable</key><string>lukav</string>
  <key>CFBundleIdentifier</key><string>com.lukav.app</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>LSUIElement</key><false/>
</dict></plist>
EOF
    cat >"$APP/Contents/MacOS/lukav" <<EOF
#!/usr/bin/env bash
cd "$REPO_DIR"
exec ./scripts/Lukav.command
EOF
    chmod +x "$APP/Contents/MacOS/lukav" scripts/Lukav.command
    echo "[lukav] installed. Open from /Applications or Spotlight."
    ;;
  Linux)
    DESKTOP_FILE="$HOME/.local/share/applications/Lukav.desktop"
    mkdir -p "$(dirname "$DESKTOP_FILE")"
    cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=Lukav
Comment=Personal credit-card debt auditor
Exec=$REPO_DIR/scripts/Lukav.command
Path=$REPO_DIR
Terminal=false
Type=Application
Categories=Finance;
EOF
    chmod +x scripts/Lukav.command
    echo "[lukav] $DESKTOP_FILE written. Search 'Lukav' from your launcher."
    ;;
  *)
    chmod +x scripts/Lukav.command
    echo "[lukav] Run ./scripts/Lukav.command to launch."
    ;;
esac
