"""用 Gemini 語音合成，替 script.json 的每一句配音，並自動檢查英文人名／公司名有沒有念成英文。

用法：python3 cloud/tts.py days/2026-11-23/script.json [--dry]
  --dry：不呼叫 Gemini，產生靜音檔（只測畫面用）。

金鑰：雲端 Default 環境的「網路密鑰」會自動幫連到 generativelanguage.googleapis.com 的請求帶上金鑰，
程式本身不碰金鑰。若有設環境變數 GEMINI_API_KEY 才會自己帶。

注意：Gemini 語音合成會把附加的風格指示也念出來，所以只送要念的純文字。
"""
import os, re, sys, json, base64, wave, time, urllib.request, urllib.error

API = "https://generativelanguage.googleapis.com/v1beta"
TTS_MODEL = os.environ.get("GM_MODEL", "models/gemini-3.8-flash-tts")
VOICE = os.environ.get("GM_VOICE", "Charon")
H = {"Content-Type": "application/json"}
if os.environ.get("GEMINI_API_KEY"):
    H["x-goog-api-key"] = os.environ["GEMINI_API_KEY"]


def call(path, body, timeout=180):
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body else None, headers=H)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def retry(fn, tag):
    for k in range(8):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            wait = 15 * (k + 1)
            print(f"{tag} HTTP {e.code}，{wait} 秒後重試", flush=True)
            time.sleep(wait)
        except Exception as e:  # 網路斷線等
            print(f"{tag} {type(e).__name__}，10 秒後重試", flush=True)
            time.sleep(10)
    raise RuntimeError(f"{tag} 連續失敗")


def tts(text, out):
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    r = call(f"{TTS_MODEL}:generateContent", body)
    b = base64.b64decode(r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    if b[:4] == b"RIFF":
        open(out, "wb").write(b)
    else:  # 24 kHz 16-bit 單聲道 PCM
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b)


_asr_model = None
def asr_model():
    global _asr_model
    if _asr_model is None:
        ms = [m["name"] for m in call("models?pageSize=200", None, 60)["models"]]
        cand = [m for m in ms if "flash" in m and not any(x in m for x in ("tts", "image", "live", "embed", "audio"))]
        _asr_model = sorted(cand, key=lambda n: (("lite" in n), ("preview" in n), n))[-1] if cand else "models/gemini-2.5-flash"
    return _asr_model


def transcribe(path):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {"contents": [{"parts": [{"inlineData": {"mimeType": "audio/wav", "data": b64}},
                                    {"text": "逐字聽寫這段音檔。中文用繁體字；聽到英文就照英文拼寫，聽到中文音譯就寫中文字。只輸出聽寫文字。"}]}]}
    r = call(f"{asr_model()}:generateContent", body, 120)
    return r["candidates"][0]["content"]["parts"][0]["text"]


def names_in(text):
    """抓要用英文念的詞：句子裡的英文單字（兩個字母以上）。"""
    return [w for w in re.findall(r"[A-Za-z][A-Za-z.\-']+", text) if len(w) >= 2]


def score(transcript, names):
    t = transcript.lower()
    return sum(1 for n in names if n.lower().strip(".") in t)


def silent(out, sec):
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\0\0" * int(24000 * sec))


def main():
    script_path = sys.argv[1]
    dry = "--dry" in sys.argv
    day = os.path.dirname(os.path.abspath(script_path))
    out_dir = os.path.join(day, "tts"); os.makedirs(out_dir, exist_ok=True)
    sc = json.load(open(script_path, encoding="utf-8"))
    report = []
    for si, slide in enumerate(sc["slides"], 1):
        for li, ln in enumerate(slide["lines"], 1):
            tag = f"S{si:02d}_{li:02d}"
            out = f"{out_dir}/{tag}.wav"
            if os.path.exists(out):
                continue
            if dry:
                silent(out, max(2.0, len(ln["say"]) * 0.22)); continue
            names = names_in(ln["say"])
            best, best_s, takes = None, -1, 0
            for take in range(3 if names else 1):
                tmp = f"{out_dir}/{tag}.take{take}.wav"
                retry(lambda: tts(ln["say"], tmp), tag); takes += 1
                time.sleep(2)
                if not names:
                    best = tmp; break
                heard = retry(lambda: transcribe(tmp), tag + " 聽寫")
                s = score(heard, names)
                if s > best_s:
                    best, best_s = tmp, s
                if s == len(names):
                    break
            os.replace(best, out)
            for f in os.listdir(out_dir):
                if f.startswith(tag + ".take"):
                    os.remove(os.path.join(out_dir, f))
            ok = (not names) or best_s == len(names)
            report.append({"line": tag, "say": ln["say"], "names": names, "takes": takes, "names_ok": ok})
            print(f"{tag} 完成（試 {takes} 次）" + ("" if ok else f"  ⚠ 英文名可能念成中文：{names}"), flush=True)
    json.dump(report, open(f"{out_dir}/report.json", "w"), ensure_ascii=False, indent=1)
    bad = [r for r in report if not r["names_ok"]]
    print(f"配音完成。英文名念不好的句子：{len(bad)} 句" + ("" if not bad else "：" + "、".join(r["line"] for r in bad)))


if __name__ == "__main__":
    main()
