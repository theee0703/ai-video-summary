"""AI 晶片日報（Mac 版）：每天自動挑一支 AI／晶片長影片，照時間軸濃縮成 10 分鐘以上的繁中解說片。

流程（每一步做完會存檔，中斷後重跑會從斷掉的地方接著做）：
  1 挑片   從 config.json 的頻道清單找近幾天的長影片，Gemini 挑最值得看的一支
  2 下載   英文字幕＋720p 影片（KY 自己決定要執行這個下載，只供個人觀看）
  3 逐字稿 字幕轉成帶時間的逐字稿
  4 寫稿   Gemini 讀完整逐字稿，挑 3 個精彩片段放最前面，再照時間軸分段：每段旁白＋原片片段
  5 字幕   原片片段對齊句子，英文整理＋中文翻譯
  6 配音   Gemini 語音（自動聽寫檢查英文名有沒有念成中文）
  7 合成   原片段：彩色＋雙語字幕；旁白段：畫面定格、褪色變暗＋字卡
  8 交付   存到 out/daily/，複製一份到 iCloud 雲碟（手機可看），跳出通知

用法：python run_daily.py            # 今天的影片
      python run_daily.py --video <YouTube 影片 id>   # 指定影片（測試用）
"""
import os, sys, re, json, glob, time, shutil, datetime, subprocess, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gemini as G

ROOT = os.path.expanduser(os.environ.get("VS_ROOT", "~/claude-ai-video-summary"))
D = f"{ROOT}/daily"
CFG = json.load(open(f"{D}/config.json", encoding="utf-8"))
PY = sys.executable
FF = os.environ.get("VS_FFMPEG") or __import__("imageio_ffmpeg").get_ffmpeg_exe()
JS = [] if shutil.which("deno") else (["--js-runtimes", "node"] if shutil.which("node") else [])
TODAY = datetime.date.today()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def jload(p, default=None):
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default


def jsave(obj, p):
    json.dump(obj, open(p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def ytdlp(*args, timeout=3600):
    return subprocess.run([PY, "-m", "yt_dlp", *JS, "--no-warnings", *args], capture_output=True, text=True, timeout=timeout)


def mmss(t):
    t = int(t); return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


# ---------------------------------------------------------------- 1 挑片
def meta(vid):
    r = ytdlp("-J", "--skip-download", f"https://www.youtube.com/watch?v={vid}", timeout=300)
    if r.returncode:
        log("  讀不到影片資訊", vid, r.stderr.strip()[-200:]); return None
    return json.loads(r.stdout)


def has_en_subs(m):
    keys = list((m.get("subtitles") or {}).keys()) + list((m.get("automatic_captions") or {}).keys())
    return any(k == "en" or k.startswith("en-") for k in keys)


NEWS_QUERIES = ["AI chips", "Nvidia OR TSMC OR semiconductor", "OpenAI OR Anthropic OR \"Google Gemini\"", "AI data center"]


def news_headlines():
    """Google 新聞 RSS：最近 3 天 AI／晶片的頭條，用來判斷這幾天什麼最重要。"""
    import urllib.request, urllib.parse, xml.etree.ElementTree as ET
    out = []
    for q in CFG.get("news_queries", NEWS_QUERIES):
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
            for it in list(ET.fromstring(xml).iter("item"))[:15]:
                out.append(it.findtext("title", ""))
        except Exception as e:
            log("  新聞讀取失敗：", q, type(e).__name__)
    return list(dict.fromkeys(out))[:60]


def flat_list(url, n):
    r = ytdlp("--flat-playlist", "-J", "--playlist-end", str(n), url, timeout=300)
    return (json.loads(r.stdout).get("entries") or []) if r.returncode == 0 else None


def step_pick(DAY, forced):
    p = f"{DAY}/pick.json"
    if os.path.exists(p):
        return jload(p)
    history = jload(f"{D}/history.json", [])
    hist = {h["id"] for h in history}
    if forced:
        m = meta(forced)
        pick = dict(id=forced, channel=m.get("channel") or m.get("uploader", ""), title=m["title"],
                    minutes=round(m["duration"] / 60), reason="手動指定", desc=(m.get("description") or "")[:1500],
                    channel_url=m.get("channel_url", ""))
        jsave(pick, p); return pick
    # (1) 這幾天的大事
    heads = news_headlines()
    log(f"  讀到 {len(heads)} 則近 3 天新聞標題")
    ev = G.gen_json(f"""下面是最近 3 天 AI 與晶片產業的英文新聞標題。
請整理出最重要的 5 件事（繁體中文，各一句），並寫 4 個英文 YouTube 搜尋關鍵字，用來找「深入討論這些大事的長篇訪談或 podcast」。
只輸出 JSON：{{"events": ["..."], "queries": ["..."]}}
標題：
""" + "\n".join(heads), "整理大事") if heads else {"events": [], "queries": []}
    # (2) 候選：固定頻道＋依大事搜尋
    listed = {c["url"].split("/@")[-1].split("/")[0].lower() for c in CFG["channels"]}
    cands = {}
    for ch in CFG["channels"]:
        es = flat_list(ch["url"], 6)
        if es is None:
            log("  頻道讀取失敗，略過：", ch["name"]); continue
        for e in es:
            cands.setdefault(e.get("id"), dict(e, src="固定頻道"))
    for q in ev.get("queries", [])[:4]:
        for e in flat_list(f"ytsearchdate15:{q}", 15) or []:
            cands.setdefault(e.get("id"), dict(e, src="依大事搜尋"))
    cands = {k: v for k, v in cands.items() if k and k not in hist and (v.get("duration") or 0) >= CFG["min_minutes"] * 60}
    log(f"  符合長度的候選 {len(cands)} 支，讀取詳細資料")
    details = []
    for vid, e in list(cands.items())[:40]:
        m = meta(vid)
        if not m or m.get("live_status") in ("is_upcoming", "is_live") or not m.get("upload_date"):
            continue
        handle = (m.get("uploader_id") or "").lstrip("@").lower()
        is_listed = handle in listed
        followers = m.get("channel_follower_count") or 0
        if not is_listed and followers < CFG.get("min_followers_unlisted", 50000):
            continue  # 不在清單又太小的頻道不考慮，避免農場內容
        age = (TODAY - datetime.datetime.strptime(m["upload_date"], "%Y%m%d").date()).days
        chapters = "；".join(c.get("title", "") for c in (m.get("chapters") or [])[:25])
        details.append(dict(id=vid, channel=m.get("channel", ""), channel_url=m.get("channel_url", ""), handle=handle,
                            listed=is_listed, followers=followers, title=m["title"], minutes=round(m["duration"] / 60),
                            age_days=age, chapters=chapters, desc=(m.get("description") or "")[:1500]))
    pool = []
    for max_age in (CFG["max_age_days"], CFG["fallback_age_days"]):
        pool = [x for x in details if x["age_days"] <= max_age]
        if pool:
            break
    if not pool:
        raise SystemExit("找不到符合條件的新影片（近期都沒有新長片）")
    recent = [h for h in history if (TODAY - datetime.date.fromisoformat(h["date"][:10])).days <= 7]
    listing = "\n".join(
        f"- id={x['id']}｜{x['channel']}（{'常用頻道' if x['listed'] else '搜尋找到，訂閱 ' + str(x['followers'])}）｜{x['title']}｜{x['minutes']} 分鐘｜{x['age_days']} 天前"
        f"\n  章節：{x['chapters'][:500] or '無'}\n  簡介：{x['desc'][:400]}" for x in pool)
    ans = G.gen_json(f"""你在挑今天要濃縮成 12～15 分鐘繁中解說的一支英文長影片。目標：在有限時間內，先聽到最值得聽的內容。
內容偏好（由高到低）：{CFG.get('preferences', '')}
這幾天的產業大事：
{chr(10).join('- ' + e for e in ev.get('events', [])) or '（無資料）'}
最近 7 天已經做過的影片（同主題要扣分）：
{chr(10).join('- ' + h['title'] for h in recent) or '（無）'}

請給每支候選 1～5 分：
- 重要性：是否談到這幾天的大事、或對產業影響大的題目
- 資訊密度：從章節與簡介判斷，有沒有新資訊、數字、具體觀點，而不是閒聊或宣傳
- 來賓份量：公司執行長、創辦人、研究者等第一手說法較高；一般評論較低
- 新鮮度：跟最近 7 天做過的主題重複就低
- 偏好：符合上面內容偏好的程度
total＝重要性×2＋資訊密度×2＋偏好×2＋來賓份量＋新鮮度。
候選：
{listing}
只輸出 JSON：{{"ranking": [{{"id": "...", "scores": {{"重要性": n, "資訊密度": n, "來賓份量": n, "新鮮度": n, "偏好": n}}, "total": n, "reason": "一句繁體中文理由"}}]}}，依 total 由高到低，最多 5 支。""", "挑片打分", temperature=0.3)
    byid = {x["id"]: x for x in pool}
    ranking = [r | {"title": byid[r["id"]]["title"], "channel": byid[r["id"]]["channel"], "minutes": byid[r["id"]]["minutes"]}
               for r in ans.get("ranking", []) if r.get("id") in byid]
    if not ranking:
        ranking = [dict(id=pool[0]["id"], total=0, reason="打分失敗，取最新一支", title=pool[0]["title"],
                        channel=pool[0]["channel"], minutes=pool[0]["minutes"])]
    x = byid[ranking[0]["id"]]
    pick = dict(id=x["id"], channel=x["channel"], channel_url=x["channel_url"], handle=x["handle"], listed=x["listed"],
                title=x["title"], minutes=x["minutes"], reason=ranking[0].get("reason", ""), desc=x["desc"],
                events=ev.get("events", []), ranking=ranking[:3])
    md = ["# 今日選片", "", "## 這幾天的大事"] + [f"- {e}" for e in pick["events"]] + ["", "## 前三名"]
    for i, r in enumerate(ranking[:3], 1):
        md.append(f"{i}. {r['title']}（{r['channel']}，{r['minutes']} 分鐘）總分 {r.get('total')}：{r.get('reason', '')}")
        md.append(f"   https://www.youtube.com/watch?v={r['id']}　加做這支：bash ~/claude-ai-video-summary/daily/daily.sh --extra {r['id']}")
    open(f"{DAY}/選片.md", "w", encoding="utf-8").write("\n".join(md) + "\n")
    jsave(pick, p); return pick


# ---------------------------------------------------------------- 2 下載
def step_fetch(DAY, pick):
    url = f"https://www.youtube.com/watch?v={pick['id']}"
    for k in range(3):
        if os.path.exists(f"{DAY}/src.mp4"):
            return
        r = ytdlp("-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]", "--merge-output-format", "mp4",
                  "--ffmpeg-location", FF, "-o", f"{DAY}/src.%(ext)s", url)
        if not os.path.exists(f"{DAY}/src.mp4"):
            log("  影片下載失敗，2 分鐘後重試：", r.stderr.strip()[-200:]); time.sleep(120)
    if not os.path.exists(f"{DAY}/src.mp4"):
        raise SystemExit("影片下載失敗")


# ---------------------------------------------------------------- 3 逐字稿
def step_transcript(DAY):
    """用 Mac 本機的 Whisper（MLX 版，Apple 晶片加速）聽寫原片，拿到每個字的時間。"""
    p = f"{DAY}/words.json"
    if os.path.exists(p):
        return jload(p)
    wav = f"{DAY}/audio16k.wav"
    if not os.path.exists(wav):
        run([FF, "-y", "-loglevel", "error", "-i", f"{DAY}/src.mp4", "-vn", "-ac", "1", "-ar", "16000", wav + ".tmp.wav"])
        os.replace(wav + ".tmp.wav", wav)
    import mlx_whisper
    r = mlx_whisper.transcribe(wav, path_or_hf_repo=CFG.get("whisper_model", "mlx-community/whisper-large-v3-turbo"),
                               language="en", word_timestamps=True, condition_on_previous_text=False)
    words = []
    for seg in r["segments"]:
        for w in seg.get("words", []):
            t = w["word"].strip()
            if t:
                words.append([round(w["start"], 2), t, round(max(w["end"], w["start"] + 0.05), 2)])
    words.sort(key=lambda x: x[0])
    os.remove(wav)
    jsave(words, p); return words


def transcript_text(words):
    lines, cur, start = [], [], None
    for t, w, e in words:
        if start is None:
            start = t
        cur.append(w)
        if e - start >= 12 or w.endswith((".", "?", "!")) and e - start >= 6:
            lines.append(f"[{int(start)}] {' '.join(cur)}"); cur, start = [], None
    if cur:
        lines.append(f"[{int(start)}] {' '.join(cur)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 4 寫稿
LINE_RULE = """每一句旁白是一個物件 {"show": "畫面上的中文字（不超過 28 字）", "en": "簡短英文字幕", "say": "實際要念的字"}。
旁白規則：
- 台灣用語、繁體中文，口語、像科技節目主持人跟朋友解說；先講結論再補理由。
- say 裡的英文人名、公司名、產品名保留英文字母（例如 Jensen Huang、Blackwell、TPU），不要寫成中文音譯；數字寫成念法（例如「四百五十億美元」「百分之二十七」）；不要加任何語氣或動作指示。
- 提到上市公司時，show 用「代號＋名稱」（例如 NVDA 輝達、2330 台積電），say 只念中文名稱。
- 專有名詞或縮寫第一次出現，用半句白話解釋。
- 用自己的話轉述，不要整段照翻原文。"""


def step_plan(DAY, pick, words):
    p = f"{DAY}/plan.json"
    if os.path.exists(p):
        return jload(p)
    total = words[-1][2]
    prompt = f"""你要把一支英文長影片濃縮成一支 {CFG['target_minutes']} 分鐘的繁體中文解說影片，用來學 AI 與晶片產業知識。
影片：{pick['channel']}｜{pick['title']}｜全長 {mmss(total)}
影片簡介（人名、公司名拼法以這裡為準；逐字稿是機器聽寫，人名可能拼錯）：{pick.get('desc', '')[:1200]}
數字與事實只能來自逐字稿，聽不清楚的數字寧可不提，不要自己補。

成片結構（照順序）：
1. intro：開場旁白 3～5 句（不要提到觀眾是誰、不要說「適合誰看」）。說這是哪個節目、誰在談、這集最重要的結論是什麼、為什麼值得看，最後一句引導「先看三個最精彩的片段，後面完整整理時你會再看到它們的來龍去脈」。
2. highlights：全片最精彩、最有觀點或最有資訊量的 3 個片段（每段 10～25 秒），照原片時間先後排。
3. bridge：1～2 句，引導「接下來照時間順序，完整整理這一集」。
4. chapters：照原片時間順序，把整集分成 7～10 段，涵蓋全片主要內容，不要只挑精華。每段有：
   - title：中文段落標題（不超過 14 字）
   - narration：3～6 句旁白，交代這段在談什麼、背景、關鍵論點與數字（包含沒有剪進來的部分）
   - clips：1～2 個原片片段（每段 25～80 秒），選最能代表這段論點的話，片段要從一句話開頭開始、到一句話講完結束
   所有 chapters 的 clips 加起來約 6～9 分鐘。前面 highlights 的片段可以在對應段落再出現（較長版本）。
5. outro：總結 3～5 句：這集的重點、對產業或投資的意義、一句收尾。

{LINE_RULE}

片段時間用「秒」（數字），對照逐字稿每行開頭的 [秒數]。

只輸出 JSON：
{{"show": "節目中文或英文名", "speakers": ["主要講者英文名"], "title_zh": "這集的中文標題（不超過 24 字）",
 "intro": [句子...], "highlights": [{{"start": 秒, "end": 秒, "why": "一句中文"}}],
 "bridge": [句子...],
 "chapters": [{{"title": "...", "narration": [句子...], "clips": [{{"start": 秒, "end": 秒}}]}}],
 "outro": [句子...]}}

逐字稿（[秒數] 英文，機器字幕沒有標點，可能有聽錯的人名）：
{transcript_text(words)}"""
    plan = G.gen_json(prompt, "寫稿")
    plan = validate_plan(plan, total)
    jsave(plan, p); return plan


def validate_plan(plan, total):
    def fix_clip(c, lo, hi):
        s, e = float(c["start"]), float(c["end"])
        s = max(0, min(s, total - lo)); e = min(total, max(e, s + lo)); e = min(e, s + hi)
        return dict(start=round(s, 1), end=round(e, 1))
    def fix_lines(ls):
        out = []
        for l in ls or []:
            if isinstance(l, str):
                l = dict(show=l, en="", say=l)
            if l.get("say") or l.get("show"):
                out.append(dict(show=l.get("show") or l["say"], en=l.get("en", ""), say=l.get("say") or l["show"]))
        return out
    plan["highlights"] = sorted([fix_clip(h, 8, 30) | {"why": h.get("why", "")} for h in plan.get("highlights", [])][:3], key=lambda c: c["start"])
    chs, last_end = [], -1
    for ch in sorted(plan["chapters"], key=lambda c: min(float(x["start"]) for x in c["clips"]) if c.get("clips") else 1e9):
        clips = []
        for c in sorted(ch.get("clips", []), key=lambda c: float(c["start"])):
            c = fix_clip(c, 15, 95)
            if c["start"] < last_end:  # 跟前一段重疊就丟掉
                continue
            clips.append(c); last_end = c["end"]
        chs.append(dict(title=ch.get("title", "")[:16], narration=fix_lines(ch.get("narration")), clips=clips))
    plan["chapters"] = chs
    for k in ("intro", "bridge", "outro"):
        plan[k] = fix_lines(plan.get(k))
    return plan


# ---------------------------------------------------------------- 5 字幕
def snap(words, s, e):
    """把片段起訖對到講話停頓：起點找附近前面停最久的字，終點找附近後面停最久的字。"""
    def gap_before(i): return words[i][0] - words[i - 1][2] if i > 0 else 9
    def gap_after(i): return words[i + 1][0] - words[i][2] if i + 1 < len(words) else 9
    si = [i for i, w in enumerate(words) if s - 3 <= w[0] <= s + 3]
    ei = [i for i, w in enumerate(words) if e - 3 <= w[2] <= e + 3]
    a = max(si, key=gap_before) if si else min(range(len(words)), key=lambda i: abs(words[i][0] - s))
    b = max(ei, key=gap_after) if ei else min(range(len(words)), key=lambda i: abs(words[i][2] - e))
    if b <= a:
        b = min(len(words) - 1, a + 20)
    return a, b


def chunks(words, a, b):
    out, cur = [], []
    for i in range(a, b + 1):
        cur.append(words[i])
        dur = cur[-1][2] - cur[0][0]
        gap = words[i + 1][0] - words[i][2] if i + 1 <= b else 9
        if len(cur) >= 9 or dur >= 3.4 or gap > 0.7 or (cur[-1][1].endswith((".", "?", "!")) and len(cur) >= 3):
            out.append(dict(s=cur[0][0], e=cur[-1][2], en=" ".join(w[1] for w in cur))); cur = []
    if cur:
        out.append(dict(s=cur[0][0], e=cur[-1][2], en=" ".join(w[1] for w in cur)))
    return out


def step_subs(DAY, plan, words):
    p = f"{DAY}/clips.json"
    if os.path.exists(p):
        return jload(p)
    clips = []
    for i, h in enumerate(plan["highlights"], 1):
        clips.append(dict(id=f"H{i}", **h))
    for ci, ch in enumerate(plan["chapters"], 1):
        for k, c in enumerate(ch["clips"], 1):
            clips.append(dict(id=f"C{ci:02d}_{k}", **c))
    items = []
    for c in clips:
        a, b = snap(words, c["start"], c["end"])
        c["st"], c["en"] = max(0, words[a][0] - 0.15), words[b][2] + 0.25
        c["chunks"] = chunks(words, a, b)
        for j, x in enumerate(c["chunks"], 1):
            x["id"] = f"{c['id']}_{j:02d}"; items.append(dict(id=x["id"], en=x["en"]))
    names = ", ".join(plan.get("speakers", []))
    zh = {}
    for start in range(0, len(items), 120):
        batch = items[start:start + 120]
        ans = G.gen_json(f"""下面是一段英文節目（{plan.get('show', '')}，講者：{names}）的機器聽寫字幕，依時間順序分成小段。
請對每一段：
1. en：整理成正確的英文（補大小寫與標點、修正明顯聽錯的人名與專有名詞），不要改意思、不要合併或拆開段落。
2. zh：翻成自然、簡短的台灣繁體中文字幕。人名、公司名、產品名保留英文。
只輸出 JSON：{{"items": [{{"id": "...", "en": "...", "zh": "..."}}]}}，id 與輸入一一對應。
輸入：
{json.dumps(batch, ensure_ascii=False)}""", "翻譯字幕", temperature=0.3)
        for it in ans.get("items", []):
            zh[it["id"]] = it
    for c in clips:
        for x in c["chunks"]:
            t = zh.get(x["id"], {})
            x["en"] = t.get("en") or x["en"]; x["zh"] = t.get("zh", "")
    jsave(clips, p); return clips


# ---------------------------------------------------------------- 6 配音
def blocks_of(plan):
    bl = [("B00", "開場", plan["intro"]), ("B01", "", plan["bridge"])]
    for ci, ch in enumerate(plan["chapters"], 1):
        bl.append((f"B{ci + 1:02d}", ch["title"], ch["narration"]))
    bl.append(("B99", "總結", plan["outro"]))
    return [b for b in bl if b[2]]


def step_tts(DAY, plan):
    td = f"{DAY}/tts"; os.makedirs(td, exist_ok=True)
    report = jload(f"{td}/report.json", {})
    for bid, _, lines in blocks_of(plan):
        out = f"{td}/{bid}.wav"
        if os.path.exists(out):
            continue
        text = "\n".join(l["say"].rstrip("。") + "。" for l in lines)
        names = sorted({w for w in re.findall(r"[A-Za-z][A-Za-z.\-']+", text) if len(w) >= 2})
        best, best_s, takes = None, -1, 0
        for take in range(3 if names else 1):
            tmp = f"{td}/{bid}.take{take}.wav"
            G.tts(text, tmp); takes += 1; time.sleep(3)
            if not names:
                best = tmp; break
            heard = G.transcribe(tmp).lower()
            s = sum(1 for n in names if n.lower().strip(".") in heard)
            if s > best_s:
                best, best_s = tmp, s
            if s == len(names):
                break
        os.replace(best, out)
        for f in glob.glob(f"{td}/{bid}.take*.wav"):
            os.remove(f)
        report[bid] = dict(takes=takes, names=names, matched=max(best_s, 0) if names else 0)
        jsave(report, f"{td}/report.json")
        log(f"  配音 {bid}：試 {takes} 次" + (f"，英文名念對 {best_s}/{len(names)}" if names else ""))
    return report


# ---------------------------------------------------------------- 7 合成
W, H, FPS = 1280, 720, 30
ENC = ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
FONT = "Noto Sans CJK TC"
FONTDIR = f"{D}/fonts"


def ts(t):
    t = max(0, t); return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


HEAD = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,2,60,60,26,1
Style: Card,{FONT},44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,1.2,2,5,90,90,0,1
Style: Label,{FONT},22,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,0,0,0,0,100,100,0,0,3,6,0,7,24,0,18,1
Style: Head,{FONT},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,2,7,48,0,80,1
Style: Hl,{FONT},26,&H0000D7FF,&H00FFFFFF,&H00000000,&HB0000000,1,0,0,0,100,100,0,0,3,7,0,9,0,24,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ev(s, e, style, text):
    return f"Dialogue: 0,{ts(s)},{ts(e)},{style},,0,0,0,,{text}\n"


def ass_esc(t):
    return t.replace("\n", " ").replace("{", "(").replace("}", ")")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("ffmpeg 失敗：" + r.stderr[-1500:])


def line_times(lines, dur):
    wts = [len(re.findall(r"[一-鿿0-9]", l["say"])) + 2.2 * len(re.findall(r"[A-Za-z]+", l["say"])) + 3 for l in lines]
    tot, t, out = sum(wts), 0.45, []
    for l, w in zip(lines, wts):
        d = (dur) * w / tot
        out.append((t, t + d)); t += d
    return out


def step_render(DAY, pick, plan, clips):
    SRC = f"{DAY}/src.mp4"; TMP = f"{DAY}/render"; os.makedirs(TMP, exist_ok=True)
    show = plan.get("show") or pick["channel"]
    byid = {c["id"]: c for c in clips}
    segs = []
    # 依序排出整支片
    order = [("N", "B00", None)] + [("O", f"H{i}", "hl") for i in range(1, len(plan["highlights"]) + 1)]
    order += [("N", "B01", None)]
    for ci, ch in enumerate(plan["chapters"], 1):
        order.append(("N", f"B{ci + 1:02d}", ci))
        order += [("O", f"C{ci:02d}_{k}", ci) for k in range(1, len(ch["clips"]) + 1)]
    order.append(("N", "B99", None))
    blocks = {b[0]: b for b in blocks_of(plan)}
    prev_end = None
    files = []
    for kind, sid, extra in order:
        out = f"{TMP}/{sid}.mp4"; tmp = out[:-4] + ".tmp.mp4"; assp = f"{TMP}/{sid}.ass"
        if kind == "O":
            c = byid.get(sid)
            if not c:
                continue
            dur = c["en"] - c["st"]
            if extra == "hl":
                n = int(sid[1:]); tag = f"精彩片段 {n}／{len(plan['highlights'])}"
            else:
                tag = f"原片 {mmss(c['st'])}"
            A = HEAD + ev(0, dur, "Label", ass_esc(f"影片來源：{show}")) + ev(0, dur, "Hl", ass_esc(tag))
            for x in c["chunks"]:
                txt = (ass_esc(x["zh"]) + "\\N{\\fs25\\b0}" + ass_esc(x["en"])) if x.get("zh") else ass_esc(x["en"])
                A += ev(x["s"] - c["st"], x["e"] - c["st"] + 0.15, "Sub", txt)
            if not os.path.exists(out):
                open(assp, "w", encoding="utf-8").write(A)
                vf = f"scale={W}:{H},setsar=1,drawbox=x=0:y=ih-118:w=iw:h=104:color=black@0.72:t=fill,ass={assp}:fontsdir={FONTDIR}"
                run([FF, "-y", "-loglevel", "error", "-ss", f"{c['st']:.2f}", "-t", f"{dur:.2f}", "-i", SRC, "-vf", vf,
                     "-af", "afade=t=in:d=0.08,afade=t=out:st=%.2f:d=0.15" % max(0, dur - 0.15)] + ENC + [tmp])
                os.replace(tmp, out)
            prev_end = c["en"]
        else:
            if sid not in blocks:
                continue
            _, title, lines = blocks[sid]
            wav = f"{DAY}/tts/{sid}.wav"; vd = G.wav_dur(wav)
            dur = vd + 0.45 + 0.5
            black = sid == "B99"
            if prev_end is not None:
                frame_t = prev_end - 0.3
            else:  # 開場：用第一個精彩片段的畫面
                frame_t = byid["H1"]["st"] + 1 if "H1" in byid else 60
            if sid.startswith("B") and isinstance(extra, int):  # 段落旁白：第一段沒有前一段時用本段片段開頭
                first = byid.get(f"C{extra:02d}_1")
                if prev_end is None and first:
                    frame_t = first["st"] + 1
            head = {"B00": plan.get("title_zh", ""), "B01": "完整整理", "B99": "總結"}.get(sid, title)
            if isinstance(extra, int):
                first = byid.get(f"C{extra:02d}_1")
                head = f"{extra}．{title}" + (f"\\N{{\\fs22\\b0}}原片 {mmss(first['st'])}" if first else "")
            A = HEAD + ("" if black else ev(0, dur, "Label", ass_esc(f"影片來源：{show}")))
            if head:
                A += ev(0, dur, "Head", head if isinstance(extra, int) else ass_esc(head))
            for (s, e), l in zip(line_times(lines, vd), lines):
                A += ev(s, e + 0.1, "Card", ass_esc(l["show"]) + ("\\N{\\fs26\\b0}" + ass_esc(l["en"]) if l.get("en") else ""))
            if not os.path.exists(out):
                open(assp, "w", encoding="utf-8").write(A)
                aud = ["-i", wav]
                af = f"[1:a]aresample=48000,adelay=450|450,apad,atrim=0:{dur:.2f}[aout]"
                if black:
                    vin = ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", f"color=c=black:s={W}x{H}:r={FPS}"]
                    vf = f"[0:v]ass={assp}:fontsdir={FONTDIR}[vout]"
                else:
                    vin = ["-ss", f"{max(0, frame_t):.2f}", "-t", "1", "-i", SRC]
                    vf = (f"[0:v]scale={W}:{H},setsar=1,trim=end_frame=1,loop=loop=-1:size=1:start=0,setpts=N/{FPS}/TB,"
                          f"trim=duration={dur:.2f},format=yuv420p,hue=s='max(0,1-t/0.5)',split[b0][b1];"
                          f"color=c=black:s={W}x{H}:r={FPS}:d={dur:.2f},format=rgba,colorchannelmixer=aa=0.6,fade=t=in:st=0:d=0.5:alpha=1[blk];"
                          f"[b0][blk]overlay=shortest=1,ass={assp}:fontsdir={FONTDIR}[vout];[b1]nullsink")
                run([FF, "-y", "-loglevel", "error"] + vin + aud + ["-filter_complex", vf + ";" + af,
                     "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.2f}"] + ENC + [tmp])
                os.replace(tmp, out)
        files.append(out)
        log("  合成", sid)
    open(f"{TMP}/list.txt", "w").write("".join(f"file '{f}'\n" for f in files))
    safe = re.sub(r"[\\/:*?\"<>|]", "", plan.get("title_zh", pick["id"]))[:30]
    final = f"{ROOT}/out/daily/{TODAY.isoformat()}_{safe}.mp4"
    os.makedirs(os.path.dirname(final), exist_ok=True)
    run([FF, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", f"{TMP}/list.txt", "-c", "copy",
         "-movflags", "+faststart", final + ".tmp.mp4"])
    os.replace(final + ".tmp.mp4", final)
    return final


# ---------------------------------------------------------------- 8 交付
def step_deliver(DAY, pick, plan, final):
    copied = ""
    dest = os.path.expanduser(CFG.get("copy_to", ""))
    if dest and os.path.isdir(os.path.dirname(dest)):
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(final, dest); copied = "，已複製到 iCloud 雲碟"
    hist = jload(f"{D}/history.json", [])
    hist.append(dict(id=pick["id"], date=TODAY.isoformat(), title=pick["title"], channel=pick["channel"],
                     handle=pick.get("handle", ""), video=os.path.basename(final)))
    jsave(hist, f"{D}/history.json")
    # 頻道清單自動學習：不在清單的頻道，30 天內被選中 2 次就加進常用清單
    h = pick.get("handle")
    if h and not pick.get("listed"):
        n = sum(1 for x in hist if x.get("handle") == h and (TODAY - datetime.date.fromisoformat(x["date"][:10])).days <= 30)
        if n >= 2:
            cfg = jload(f"{D}/config.json")
            cfg["channels"].append({"name": pick["channel"], "url": f"https://www.youtube.com/@{h}/videos", "added": f"{TODAY} 自動加入"})
            jsave(cfg, f"{D}/config.json"); log("  頻道自動加入常用清單：", pick["channel"])
    others = "；".join(f"{i}. {r['title'][:28]}" for i, r in enumerate(pick.get("ranking", [])[1:3], 2))
    msg = f"{plan.get('title_zh', pick['title'])}（{pick['channel']}）{copied}" + (f"。其他候選：{others}" if others else "")
    subprocess.run(["osascript", "-e", f'display notification "{msg[:230].replace(chr(34), "")}" with title "AI 晶片日報已完成"'])
    # 原片只留最近幾天
    for old in sorted(glob.glob(f"{D}/days/*/src.mp4")):
        day = os.path.basename(os.path.dirname(old))
        try:
            if (TODAY - datetime.date.fromisoformat(day)).days > CFG.get("keep_sources_days", 7):
                os.remove(old)
        except Exception:
            pass
    open(f"{DAY}/DONE", "w").write(final)
    log("完成：", final, copied)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--video"); ap.add_argument("--day")
    ap.add_argument("--extra", help="加做指定影片（不影響當天主影片）")
    a = ap.parse_args()
    global TODAY
    if a.day:
        TODAY = datetime.date.fromisoformat(a.day)
    if a.extra:
        a.video = a.extra
    DAY = f"{D}/days/{TODAY.isoformat()}" + (f"_{a.extra}" if a.extra else ""); os.makedirs(DAY, exist_ok=True)
    if os.path.exists(f"{DAY}/DONE"):
        log("今天已經做好了：", open(f"{DAY}/DONE").read()); return
    log("== AI 晶片日報", TODAY)
    log("1 挑片"); pick = step_pick(DAY, a.video); log(f"  {pick['channel']}｜{pick['title']}｜{pick['minutes']} 分鐘｜{pick.get('reason', '')}")
    log("2 下載"); step_fetch(DAY, pick)
    log("3 逐字稿"); words = step_transcript(DAY); log(f"  {len(words)} 字")
    log("4 寫稿"); plan = step_plan(DAY, pick, words)
    clip_sec = sum(c["end"] - c["start"] for ch in plan["chapters"] for c in ch["clips"])
    log(f"  {len(plan['chapters'])} 段，原片約 {clip_sec / 60:.1f} 分鐘")
    log("5 字幕"); clips = step_subs(DAY, plan, words)
    log("6 配音"); step_tts(DAY, plan)
    log("7 合成"); final = step_render(DAY, pick, plan, clips)
    log("8 交付"); step_deliver(DAY, pick, plan, final)


if __name__ == "__main__":
    main()
