"""Timeline_Architect — Gradio 網頁介面。

本地測試：python app.py → 開啟 http://127.0.0.1:7860
Hugging Face Spaces 部署：在 Spaces Settings → Secrets 設定以下兩個環境變數：
    NOTION_TOKEN   — 你的 Notion Integration secret
    LLM_API_KEY    — 你的 Anthropic API key
使用者（Blackswan）只需填入她自己的 Source Page ID 和 Timeline DB ID。
"""
from __future__ import annotations
import json
import os
import traceback

import gradio as gr
from dotenv import load_dotenv
from notion_client import Client

from modules import notion_reader, scraper, extractor, notion_writer

load_dotenv()

# ── 後端憑證：從環境變數讀取，不暴露給使用者 ───────────────────────────────
# 本地跑時從 .env 讀取；HF Spaces 上從 Secrets 讀取。
_NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
_LLM_API_KEY  = os.environ.get("LLM_API_KEY", "")
_LLM_MODEL    = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")

# 本地開發用預設值（不會出現在 HF Spaces UI）
_DEFAULT_PAGE = os.environ.get("NOTION_SOURCE_PAGE_ID", "")
_DEFAULT_DB   = os.environ.get("NOTION_TIMELINE_DB_ID", "")


def run_pipeline(
    user_notion_token: str,
    user_llm_api_key: str,
    source_page_id: str,
    timeline_db_id: str,
    dry_run: bool,
) -> tuple[str, str]:
    """
    主流程。回傳 (log_text, json_text)。
    Notion token：優先用使用者自填的；若留空則 fallback 到後端 Secret。
    LLM key：優先用使用者自填的；若留空則 fallback 到後端 Secret。
    """
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)

    # 決定使用哪個 Notion token
    notion_token = user_notion_token.strip() or _NOTION_TOKEN
    if not notion_token:
        return "❌ Please enter your Notion Integration Token (or contact the tool operator).", ""

    # 決定使用哪個 LLM key
    llm_api_key = user_llm_api_key.strip() or _LLM_API_KEY
    if not llm_api_key:
        return "❌ Please enter your Anthropic API Key (or contact the tool operator).", ""

    # 使用者輸入檢查
    missing = []
    if not source_page_id.strip():
        missing.append("Source Page ID")
    if not dry_run and not timeline_db_id.strip():
        missing.append("Timeline Database ID（非 Dry-run 模式必填）")
    if missing:
        return "❌ 缺少必填欄位：\n" + "\n".join(f"  • {m}" for m in missing), ""

    os.environ["LLM_API_KEY"] = llm_api_key

    try:
        notion = Client(auth=notion_token)

        # === Module A ===
        log(f"📖 [A] 讀取 Notion 頁面 {source_page_id.strip()[:8]}...")
        urls, text_chunks = notion_reader.read_page(notion, source_page_id.strip())
        log(f"   → 找到 {len(urls)} 個 URL、{len(text_chunks)} 段純文字")

        # === Module B ===
        log(f"🌐 [B] 抓取 {len(urls)} 個 URL")
        scraped_sources: list[tuple[str, str]] = []
        for url in urls:
            body = scraper.scrape(url)
            if body:
                scraped_sources.append((url, body))
        log(f"   → 抓取成功 {len(scraped_sources)}/{len(urls)}")

        text_sources: list[tuple[str, str]] = []
        if text_chunks:
            text_sources.append(("user-pasted text", "\n\n".join(text_chunks)))

        all_sources = scraped_sources + text_sources
        if not all_sources:
            return "\n".join(logs) + "\n❌ 沒有任何可萃取的內容，終止。", ""

        # === Module C ===
        log("🧠 [C] 呼叫 LLM 萃取時間軸（約 30–60 秒）...")
        raw_events, llm_info = extractor.extract_timeline(all_sources, model=_LLM_MODEL)
        events = extractor.deduplicate(raw_events)
        log(f"   → LLM 回 {len(raw_events)} 筆，去重後剩 {len(events)} 筆")

        # Token 用量與費用估算（Sonnet: $3/1M input, $15/1M output）
        in_tok = llm_info["input_tokens"]
        out_tok = llm_info["output_tokens"]
        cost = in_tok * 3 / 1_000_000 + out_tok * 15 / 1_000_000
        log(f"   → 本次用量：{in_tok:,} input + {out_tok:,} output tokens｜預估花費 ${cost:.3f} USD")
        for w in llm_info.get("truncation_warnings", []):
            log(f"   {w}")

        # === Dry Run ===
        if dry_run:
            json_text = json.dumps(events, ensure_ascii=False, indent=2)
            log("✅ Dry-run 完成，結果顯示在右側 JSON 預覽欄。")
            return "\n".join(logs), json_text

        # === Module D ===
        log(f"✍️  [D] 寫入 Notion database {timeline_db_id.strip()[:8]}...")
        success, fail = notion_writer.write_timeline(
            notion, timeline_db_id.strip(), events
        )
        log(f"   → ✅ 成功 {success} 筆、{'⚠️ 失敗 ' + str(fail) + ' 筆' if fail else '失敗 0 筆'}")
        if success > 0:
            log("\n🎉 Timeline 已寫入 Notion！請到你的 Timeline database 查看。")
        return "\n".join(logs), ""

    except Exception as e:
        tb = traceback.format_exc()
        log(f"\n❌ 發生錯誤：{e}")
        log(f"\n詳細訊息：\n{tb}")
        return "\n".join(logs), ""


# ── UI ─────────────────────────────────────────────────────────────────────
CUSTOM_CSS = """
* {
    font-family: Arial, Calibri, 'Helvetica Neue', sans-serif !important;
}
"""

with gr.Blocks(title="Timeline Architect", css=CUSTOM_CSS, theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        """
# 🏛️ Timeline Architect
**Automatically generate a structured case timeline from your Notion research page.**
        """
    )

    # ── 可折疊的 First-time Setup 說明 ──────────────────────────────────────
    with gr.Accordion("📖 First-time setup — click to expand", open=False):
        gr.Markdown(
            """
## First-time Setup (do this once)

### Step 1 — Get your Timeline Database template

You will receive a Notion share link for the **Timeline (Template)** database.
Open the link → click **Duplicate** in the top-right corner → it will be copied into your own workspace.

The database comes pre-configured with 5 required columns:
`Name` · `Date` · `Phase` · `Source` · `Date_Precision`

---

### Step 2 — Create your own Notion Connection (your personal access token)

This token lets Timeline Architect read your source page and write to your Timeline database.
Your data stays entirely in **your own** Notion workspace.

1. Go to [app.notion.com/developers/connections](https://app.notion.com/developers/connections)
2. Click **+ New connection** (or **Create new**)
3. Give it any name (e.g. *Timeline Architect*)
4. Leave all default settings → click **Save**
5. Copy the **Access Token** (starts with `ntn_...`)
6. Paste it into the **Notion Integration Token** field below — keep it safe like a password

---

### Step 3 — Connect your integration to your Notion pages

You need to do this for **both** your source page and your Timeline database:

1. Open the page or database in Notion
2. Click `⋯` (three dots) in the top-right corner
3. Click **Connections**
4. Search for the integration name you just created → click to connect

⚠️ If you skip this step, the tool will return a "page not found" error even if the ID is correct.

---

### Step 4 — Find your Page ID and Database ID

From any Notion page or database URL, copy the **last 32 characters** after the final `/`:

```
https://notion.so/Your-Page-Title-abc123def456ghi789jkl012mno345pq
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                              This is your ID (32 characters)
```

If the URL contains a `?v=...` part, stop before the `?`.
            """
        )

    # ── 主操作區 ────────────────────────────────────────────────────────────
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### 🔑 Credentials")
            notion_token_input = gr.Textbox(
                label="Notion Access Token",
                placeholder="ntn_xxxxxxxxxxxx...",
                type="password",
                value="",
            )
            llm_key_input = gr.Textbox(
                label="Anthropic API Key  (optional — leave blank to use the shared key)",
                placeholder="sk-ant-xxxxxxxxxxxx...",
                type="password",
                value="",
            )
            gr.Markdown(
                """
> Don't have an Anthropic API key? Sign up at
> [console.anthropic.com](https://console.anthropic.com) → add a credit card → create an API key.
> Using your own key means **you control your own costs** (~$0.20–0.50 USD per run).
> If you leave this blank, the operator's shared key will be used instead.
                """
            )
            gr.Markdown("### 📄 Page IDs")
            page_id_input = gr.Textbox(
                label="Source Page ID  (the page where you paste your case materials)",
                placeholder="abc123def456ghi789jkl012mno345pq",
                value=_DEFAULT_PAGE,
            )
            db_id_input = gr.Textbox(
                label="Timeline Database ID  (where the timeline will be written)",
                placeholder="abc123def456ghi789jkl012mno345pq",
                value=_DEFAULT_DB,
            )
            dry_run_toggle = gr.Checkbox(
                label="Dry-run (preview JSON only — do NOT write to Notion)",
                value=False,
            )
            run_btn = gr.Button("🚀 Generate Timeline", variant="primary", size="lg")

        with gr.Column(scale=1):
            gr.Markdown("### 📋 Run Log")
            log_output = gr.Textbox(
                label="",
                lines=16,
                interactive=False,
            )
            gr.Markdown("### 📄 JSON Preview (Dry-run mode only)")
            json_output = gr.Code(
                label="",
                language="json",
                lines=16,
                interactive=False,
            )

    run_btn.click(
        fn=run_pipeline,
        inputs=[notion_token_input, llm_key_input, page_id_input, db_id_input, dry_run_toggle],
        outputs=[log_output, json_output],
    )

    gr.Markdown("---\n*Timeline Architect — 鵝啟田之少女事件簿計畫*")

if __name__ == "__main__":
    demo.launch(share=False)
