"""Timeline_Architect 主流程：A → B → C → D。

執行：
  python main.py             # 讀取 Notion + 抓取 + LLM 萃取 + 寫入 Timeline DB
  python main.py --dry-run   # 同上但跳過寫入，僅印 JSON 到 stdout
"""
from __future__ import annotations
import argparse
import json
import os
import sys

from dotenv import load_dotenv
from notion_client import Client

from modules import notion_reader, scraper, extractor, notion_writer


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Timeline_Architect — Notion 案件時間軸自動生成器"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="執行 A/B/C 但不寫入 Notion，只印 JSON 到 stdout",
    )
    parser.add_argument(
        "--page-id",
        default=None,
        help="覆寫 NOTION_SOURCE_PAGE_ID（測試用）",
    )
    parser.add_argument(
        "--db-id",
        default=None,
        help="覆寫 NOTION_TIMELINE_DB_ID（測試用）",
    )
    args = parser.parse_args()

    load_dotenv()
    notion_token = os.environ.get("NOTION_TOKEN")
    source_page_id = args.page_id or os.environ.get("NOTION_SOURCE_PAGE_ID")
    timeline_db_id = args.db_id or os.environ.get("NOTION_TIMELINE_DB_ID")
    llm_key = os.environ.get("LLM_API_KEY")

    # 環境變數檢查（dry-run 模式不需要 DB id）
    missing: list[str] = []
    if not notion_token:
        missing.append("NOTION_TOKEN")
    if not source_page_id:
        missing.append("NOTION_SOURCE_PAGE_ID")
    if not args.dry_run and not timeline_db_id:
        missing.append("NOTION_TIMELINE_DB_ID")
    if not llm_key:
        missing.append("LLM_API_KEY")
    if missing:
        print(f"❌ 缺少環境變數：{', '.join(missing)}", file=sys.stderr)
        print("   請開啟 .env 確認填寫齊全。", file=sys.stderr)
        sys.exit(1)

    notion = Client(auth=notion_token)

    # === 模組 A：讀取 Notion 頁面 ===
    print(f"📖 [A] 讀取 Notion 頁面 {source_page_id}", file=sys.stderr)
    try:
        urls, text_chunks = notion_reader.read_page(notion, source_page_id)
    except Exception as e:
        print(f"❌ Module A 失敗：{e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"   → 找到 {len(urls)} 個 URL、{len(text_chunks)} 段純文字",
        file=sys.stderr,
    )

    # === 模組 B：抓取 URL 內容 ===
    print(f"🌐 [B] 抓取 {len(urls)} 個 URL", file=sys.stderr)
    scraped_sources: list[tuple[str, str]] = []
    for url in urls:
        body = scraper.scrape(url)
        if body:
            scraped_sources.append((url, body))
    print(
        f"   → 抓取成功 {len(scraped_sources)}/{len(urls)}",
        file=sys.stderr,
    )

    # 純文字流統一掛 "user-pasted text" 作 source_ref
    text_sources: list[tuple[str, str]] = []
    if text_chunks:
        merged_text = "\n\n".join(text_chunks)
        text_sources.append(("user-pasted text", merged_text))

    all_sources = scraped_sources + text_sources
    if not all_sources:
        print("❌ 沒有任何可萃取的內容，終止。", file=sys.stderr)
        sys.exit(1)

    # === 模組 C：LLM 萃取 ===
    print("🧠 [C] 呼叫 LLM 萃取時間軸", file=sys.stderr)
    raw_events = extractor.extract_timeline(all_sources)
    events = extractor.deduplicate(raw_events)
    print(
        f"   → LLM 回 {len(raw_events)} 筆，去重後剩 {len(events)} 筆",
        file=sys.stderr,
    )

    # === Dry Run 分支 ===
    if args.dry_run:
        print("\n📋 --dry-run JSON 輸出：\n", file=sys.stderr)
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return

    # === 模組 D：寫入 Notion ===
    print(f"✍️  [D] 寫入 Notion database {timeline_db_id}", file=sys.stderr)
    success, fail = notion_writer.write_timeline(
        notion, timeline_db_id, events
    )
    print(f"   → 成功 {success}、失敗 {fail}", file=sys.stderr)


if __name__ == "__main__":
    main()
