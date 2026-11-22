# AI／晶片影片摘要 agent

每天自動找 AI 與晶片主題，整理重點，用繁體中文配音做成幾分鐘的影片。

- **Mac 版（主力）**：在 KY 的 Mac 上跑，有原片畫面（模仿參考頻道：原片段落＋淡化凍結畫面的旁白）。程式在 Mac 的 `~/claude-ai-video-summary`。
- **雲端版（備援）**：本存放區的 `cloud/`。每天由排程任務在 Claude Code 的 Default 雲端環境執行，文字畫面＋Gemini 配音，不需要 Mac。步驟見 `cloud/RUN.md`。

配音：Gemini 語音合成（聲音 Charon）。雲端環境的網路密鑰自動帶 Gemini 金鑰，程式碼裡沒有金鑰。
