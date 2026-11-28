#!/bin/bash
# 每天由 launchd 叫起來（凌晨 4:30，沒做成的話中午 12:30、晚上 8:30 再試）。今天做好了就直接結束。
ROOT="$HOME/claude-ai-video-summary"; D="$ROOT/daily"; W="$HOME/.video-worker"; V="$W/venv-daily"; PY="$V/bin/python"
export PATH="$HOME/.deno/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TODAY=$(date +%F)
[ -f "$D/days/$TODAY/DONE" ] && exit 0
LOCK="$W/daily.lock"
if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +300 2>/dev/null)" ]; then rmdir "$LOCK"; fi
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT
mkdir -p "$D/logs"; LOG="$D/logs/$TODAY.log"
{
  echo "==== $(date)"
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  [ -x "$PY" ] || "$UV" venv -q --python 3.11 "$V"
  VIRTUAL_ENV="$V" "$UV" pip install -q -U "yt-dlp[default]" imageio-ffmpeg mlx-whisper 2>&1 | tail -2
  command -v deno >/dev/null || curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
  caffeinate -i "$PY" "$D/run_daily.py" "$@"
  rc=$?
  echo "==== 結束 rc=$rc $(date)"
  if [ $rc -ne 0 ]; then
    osascript -e "display notification \"今天的影片沒做成，稍後會再試。紀錄：daily/logs/$TODAY.log\" with title \"AI 晶片日報失敗\""
  fi
} >> "$LOG" 2>&1
