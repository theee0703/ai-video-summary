"""把 script.json 和配音做成影片（雲端版：沒有原片，文字畫面＋旁白）。

用法：python3 cloud/render.py days/2026-11-23/script.json
輸出：同一個資料夾的 video.mp4，以及 frames/ 檢查用的畫面。

畫面：深色背景、上方節目標籤與日期、段落標題、中間大字中文、下方小字英文、底部資料來源與進度條。
每一句一個檔案，最後用 concat 直接接起來（不重新壓縮）。
"""
import os, sys, json, subprocess, shutil
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
PAD = 0.35          # 每句後面留白秒數
FONT_B = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 3)
FONT_R = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 3)
BG_TOP, BG_BOT = (16, 22, 36), (6, 8, 14)
ACCENT = (64, 156, 255)
SHOW_NAME = "AI 晶片日報"


def font(spec, size):
    path, idx = spec
    if not os.path.exists(path):  # 其他環境：用 fc-match 找繁中字型
        out = subprocess.run(["fc-match", "-f", "%{file}:%{index}", "Noto Sans CJK TC:style=" + ("Bold" if "Bold" in path else "Regular")],
                             capture_output=True, text=True).stdout
        path, idx = out.rsplit(":", 1); idx = int(idx)
    return ImageFont.truetype(path, size, index=idx)


def ffprobe_dur(p):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p]).strip())


def wrap(draw, text, f, maxw):
    """中文逐字斷行，英文單字不拆開。"""
    tokens, buf = [], ""
    for ch in text:
        if ch.isascii() and ch not in " ":
            buf += ch
        else:
            if buf: tokens.append(buf); buf = ""
            tokens.append(ch)
    if buf: tokens.append(buf)
    lines, cur = [], ""
    for t in tokens:
        trial = cur + t
        if draw.textlength(trial, font=f) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur.rstrip()); cur = t.lstrip()
    if cur: lines.append(cur.rstrip())
    return lines


def background():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        a = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(BG_TOP[i] * (1 - a) + BG_BOT[i] * a) for i in range(3)))
    return img


def frame(sc, slide, ln, progress, out):
    img = background(); d = ImageDraw.Draw(img)
    f_tag, f_head = font(FONT_B, 22), font(FONT_B, 34)
    f_zh, f_en, f_src = font(FONT_B, 50), font(FONT_R, 26), font(FONT_R, 18)
    # 上方標籤
    d.rounded_rectangle([40, 32, 40 + d.textlength(SHOW_NAME, font=f_tag) + 28, 70], 8, fill=ACCENT)
    d.text((54, 36), SHOW_NAME, font=f_tag, fill="white")
    d.text((W - 40, 40), sc["date"], font=f_tag, fill=(150, 160, 180), anchor="ra")
    # 段落標題
    d.rectangle([40, 104, 46, 146], fill=ACCENT)
    d.text((62, 102), slide["head"], font=f_head, fill=(220, 228, 240))
    # 中文大字＋英文小字，整塊置中
    zh = wrap(d, ln["show"], f_zh, W - 160)
    en = wrap(d, ln.get("en", ""), f_en, W - 200) if ln.get("en") else []
    hz, he = 68, 36
    total = len(zh) * hz + (18 + len(en) * he if en else 0)
    y = 170 + (H - 170 - 90 - total) // 2
    for t in zh:
        d.text((W // 2, y), t, font=f_zh, fill="white", anchor="ma"); y += hz
    y += 18
    for t in en:
        d.text((W // 2, y), t, font=f_en, fill=(160, 172, 192), anchor="ma"); y += he
    # 底部來源與進度條
    src = "資料來源：" + "、".join(s["name"] for s in sc.get("sources", [])[:3])
    d.text((40, H - 52), src, font=f_src, fill=(120, 130, 150))
    d.rectangle([0, H - 6, W, H], fill=(30, 36, 50))
    d.rectangle([0, H - 6, int(W * progress), H], fill=ACCENT)
    img.save(out, quality=92)


def main():
    script_path = sys.argv[1]
    day = os.path.dirname(os.path.abspath(script_path))
    sc = json.load(open(script_path, encoding="utf-8"))
    tts_dir, seg_dir, fr_dir = f"{day}/tts", f"{day}/segs", f"{day}/frames"
    for p in (seg_dir, fr_dir):
        os.makedirs(p, exist_ok=True)
    items = [(si, li, s, ln) for si, s in enumerate(sc["slides"], 1) for li, ln in enumerate(s["lines"], 1)]
    durs = [ffprobe_dur(f"{tts_dir}/S{si:02d}_{li:02d}.wav") + PAD for si, li, _, _ in items]
    total, acc, segs = sum(durs), 0.0, []
    for (si, li, slide, ln), dur in zip(items, durs):
        tag = f"S{si:02d}_{li:02d}"
        acc += dur
        png, seg = f"{fr_dir}/{tag}.jpg", f"{seg_dir}/{tag}.mp4"
        frame(sc, slide, ln, acc / total, png)
        if not os.path.exists(seg):
            tmp = seg[:-4] + ".tmp.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(FPS), "-i", png, "-i", f"{tts_dir}/{tag}.wav",
                            "-filter_complex", f"[0:v]format=yuv420p,loop=-1:1,trim=duration={dur:.3f}[v];[1:a]aresample=48000,apad,atrim=0:{dur:.3f}[a]",
                            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS),
                            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000", tmp], check=True)
            os.replace(tmp, seg)
        segs.append(seg)
    lst = f"{seg_dir}/list.txt"
    open(lst, "w").write("".join(f"file '{s}'\n" for s in segs))
    out = f"{day}/video.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out], check=True)
    print(f"完成：{out}，長度 {ffprobe_dur(out):.1f} 秒，共 {len(segs)} 句")


if __name__ == "__main__":
    main()
