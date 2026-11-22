#!/bin/bash
# 雲端環境檢查：ffmpeg、繁中字型、Pillow。缺什麼就裝什麼。
set -e
command -v ffmpeg >/dev/null || { (sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg) || pip install -q imageio-ffmpeg --break-system-packages; }
fc-match -f '%{family}\n' 'Noto Sans CJK TC' | grep -q 'CJK' || (sudo apt-get update -qq && sudo apt-get install -y -qq fonts-noto-cjk)
python3 -c "import PIL" 2>/dev/null || pip install -q pillow --break-system-packages
code=$(curl -s -o /dev/null -w '%{http_code}' https://generativelanguage.googleapis.com/v1beta/models)
echo "Gemini 連線狀態：$code（200 才正常）"
[ "$code" = "200" ]
