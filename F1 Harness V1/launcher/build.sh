#!/bin/bash
# 构建桌面 App：~/Desktop/F1 Harness.app；可选 --autostart 装开机自启
set -euo pipefail
L="$(cd "$(dirname "$0")" && pwd)"
APP="${HOME}/Desktop/F1 Harness.app"
AGENT="${HOME}/Library/LaunchAgents/com.f1hermes.v1.plist"

rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
clang -O2 -Wall -o "${APP}/Contents/MacOS/F1Harness" "${L}/main.c"
cp "${L}/F1HERMESV1.icns" "${APP}/Contents/Resources/"
printf 'APPL????' > "${APP}/Contents/PkgInfo"
cat > "${APP}/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>F1 Harness</string>
  <key>CFBundleDisplayName</key><string>F1 Harness</string>
  <key>CFBundleIdentifier</key><string>com.f1hermes.v1</string>
  <key>CFBundleExecutable</key><string>F1Harness</string>
  <key>CFBundleIconFile</key><string>F1HERMESV1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
EOF
xattr -cr "${APP}"
codesign --force --deep --sign - "${APP}"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "${APP}"

if [[ "${1:-}" == "--autostart" ]]; then
  "${L}/autostart.sh"
fi
echo "已构建：${APP}"
