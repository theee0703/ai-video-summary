#!/bin/bash
# 安裝「AI 晶片日報」每天自動執行。用法：bash ~/claude-ai-video-summary/daily/install_daily.sh
set -e
ROOT="$HOME/claude-ai-video-summary"; D="$ROOT/daily"
[ -f "$HOME/.video-worker/gemini_key" ] || { echo "找不到 Gemini 金鑰（~/.video-worker/gemini_key）"; exit 1; }
chmod +x "$D/daily.sh"
P="$HOME/Library/LaunchAgents/com.ky.video-daily.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$P" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.ky.video-daily</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$D/daily.sh</string></array>
<key>StartCalendarInterval</key><array>
<dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
<dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
<dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
</array>
<key>StandardOutPath</key><string>$HOME/.video-worker/daily.out</string>
<key>StandardErrorPath</key><string>$HOME/.video-worker/daily.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$P"
echo "已安裝：每天 4:30 自動做（沒做成會在 12:30、20:30 再試）。"
echo "影片位置：$ROOT/out/daily/，iCloud 雲碟的「AI晶片日報」資料夾也會有一份。"
echo "停用：launchctl bootout gui/\$(id -u) $P"
