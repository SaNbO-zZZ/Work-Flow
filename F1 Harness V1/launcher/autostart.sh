#!/bin/bash
# 安装开机自启（登录后打开 F1 Harness.app）；不会重建 App，已授予的系统权限不受影响
set -euo pipefail
APP="${HOME}/Desktop/F1 Harness.app"
AGENT="${HOME}/Library/LaunchAgents/com.f1hermes.v1.plist"
mkdir -p "${HOME}/Library/LaunchAgents"
cat > "${AGENT}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.f1hermes.v1</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>${APP}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
plutil -lint "${AGENT}"
launchctl bootout "gui/$(id -u)/com.f1hermes.v1" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${AGENT}"
echo "已安装开机自启：${AGENT}"
