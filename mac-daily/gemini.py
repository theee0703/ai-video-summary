"""Gemini 共用工具：寫稿（文字模型）、配音、聽寫。金鑰讀 ~/.video-worker/gemini_key，不會印出。"""
import os, re, json, base64, wave, time, urllib.request, urllib.error

API = "https://generativelanguage.googleapis.com/v1beta"
_kp = os.path.expanduser("~/.video-worker/gemini_key")
KEY = open(_kp).read().strip() if os.path.exists(_kp) else ""
H = {"x-goog-api-key": KEY, "Content-Type": "application/json"}
TTS_MODEL = os.environ.get("GM_TTS_MODEL", "models/gemini-3.8-flash-tts")
VOICE = os.environ.get("GM_VOICE", "Charon")


def _call(path, body=None, timeout=600):
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body is not None else None, headers=H)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def retry(fn, tag, n=7):
    for k in range(n):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            msg = e.read()[:300].decode("utf-8", "ignore")
            wait = 20 * (k + 1)
            print(f"  {tag}：HTTP {e.code}，{wait} 秒後重試（{msg[:120]}）", flush=True)
            if e.code in (400, 403, 404):
                raise
            time.sleep(wait)
        except Exception as e:
            print(f"  {tag}：{type(e).__name__} {str(e)[:120]}，15 秒後重試", flush=True)
            time.sleep(15)
    raise RuntimeError(f"{tag} 連續失敗")


_models = None
def models():
    global _models
    if _models is None:
        ms = _call("models?pageSize=300", timeout=60)["models"]
        _models = [m["name"] for m in ms if "generateContent" in m.get("supportedGenerationMethods", [])]
    return _models


def text_models():
    """寫稿用模型：優先 pro，其次 flash（去掉語音、圖片、即時等特殊版）。"""
    bad = ("tts", "image", "live", "embed", "audio", "lite", "learnlm", "gemma", "robotics", "computer", "customtools", "tools", "thinking")
    ok = [m for m in models() if "gemini" in m and not any(b in m for b in bad)]
    def key(n):
        v = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
        ver = (int(v.group(1)), int(v.group(2) or 0)) if v else (0, 0)
        return (ver, ("preview" not in n and "exp" not in n), n)
    pros = sorted([m for m in ok if "pro" in m], key=key, reverse=True)
    flashes = sorted([m for m in ok if "flash" in m], key=key, reverse=True)
    pref = os.environ.get("GM_TEXT_MODEL")
    return ([pref] if pref else []) + pros[:2] + flashes[:2]


def gen_json(prompt, tag="寫稿", temperature=0.6):
    """丟一段提示，拿回 JSON。pro 模型失敗（例如免費額度用完）就換 flash。"""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    last = None
    for m in text_models():
        for attempt in range(2):
            try:
                r = retry(lambda: _call(f"{m}:generateContent", body), f"{tag}（{m.split('/')[-1]}）", n=4)
                txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
                txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
                out = json.loads(txt)
                print(f"  {tag}：使用 {m.split('/')[-1]}", flush=True)
                return out
            except Exception as e:
                last = e
                print(f"  {tag}：{m.split('/')[-1]} 失敗（{type(e).__name__}），換下一個", flush=True)
                break
    raise RuntimeError(f"{tag} 全部模型都失敗：{last}")


def tts(text, out):
    """只送純文字（Gemini 會把任何語氣指示念出來）。"""
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    r = retry(lambda: _call(f"{TTS_MODEL}:generateContent", body), "配音")
    b = base64.b64decode(r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    if b[:4] == b"RIFF":
        open(out, "wb").write(b)
    else:
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b)


def transcribe(path):
    m = next((x for x in text_models() if "flash" in x), text_models()[-1])
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {"contents": [{"parts": [{"inlineData": {"mimeType": "audio/wav", "data": b64}},
                                    {"text": "逐字聽寫這段音檔。中文用繁體字；聽到英文就照英文拼寫，聽到中文音譯就寫中文字。只輸出聽寫文字。"}]}]}
    r = retry(lambda: _call(f"{m}:generateContent", body, 180), "聽寫")
    return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])


def wav_dur(p):
    with wave.open(p) as w:
        return w.getnframes() / w.getframerate()
