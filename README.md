# Kirinuki

YouTube 動画から切り抜きクリップを自動生成するツール。

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env  # GROQ_API_KEY を設定
cd remotion && npm install
```

## 起動

```bash
cd web
uvicorn app:app --reload
```

ブラウザで http://localhost:8000 を開く。

## フロー

1. **ダウンロード** — YouTube URL から動画・ライブチャットを取得
2. **文字起こし** — Groq Whisper で音声をテキスト化
3. **切り抜き提案** — Claude が面白い箇所を提案
4. **レンダリング** — Remotion で字幕付きクリップを書き出し
