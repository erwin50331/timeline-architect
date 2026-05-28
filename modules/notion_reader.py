"""模組 A：從 Notion 頁面讀取內容，分流為 URL 流與純文字流。

路徑 A 設計選擇：遇到 child_page 不遞迴下鑽（使用者明確要求保持簡單）。
"""
from __future__ import annotations
import re
from notion_client import Client

# 完整匹配「整個 block 字串等於一個 URL」的情境（用來判斷該 block 應僅作 URL 流處理）
URL_ONLY_REGEX = re.compile(r'^https?://\S+$')

# 支援讀取純文字的 block 類型
TEXT_BEARING_TYPES = {
    "paragraph", "quote", "callout",
    "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item",
    "toggle", "to_do", "code",
}

# 遞迴上限，避免異常巢狀導致無限迴圈
MAX_DEPTH = 5


def fetch_all_blocks(notion: Client, block_id: str) -> list[dict]:
    """分頁拉取所有子 block（處理 Notion API 的 cursor）。"""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        resp = notion.blocks.children.list(
            block_id=block_id,
            start_cursor=cursor,
            page_size=100,
        )
        results.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def rich_text_to_plain_and_urls(rich_text: list[dict]) -> tuple[str, list[str]]:
    """將 rich_text 陣列展平為 (純文字, 內嵌 URL list)。"""
    plain_parts: list[str] = []
    urls: list[str] = []
    for rt in rich_text:
        plain_parts.append(rt.get("plain_text", ""))
        href = rt.get("href")
        if href:
            urls.append(href)
    return "".join(plain_parts), urls


def extract_from_block(
    notion: Client, block: dict, depth: int = 0
) -> tuple[list[str], list[str]]:
    """
    遞迴萃取單一 block 的 URL 與純文字。
    回傳: (urls, text_chunks)
    """
    urls: list[str] = []
    texts: list[str] = []
    btype = block.get("type")

    # 1. 純 URL 型 block
    if btype == "bookmark":
        url = block.get("bookmark", {}).get("url")
        if url:
            urls.append(url)
    elif btype == "embed":
        url = block.get("embed", {}).get("url")
        if url:
            urls.append(url)
    elif btype == "link_preview":
        url = block.get("link_preview", {}).get("url")
        if url:
            urls.append(url)

    # 2. 帶 rich_text 的內容型 block
    elif btype in TEXT_BEARING_TYPES:
        rich_text = block.get(btype, {}).get("rich_text", [])
        plain, embedded_urls = rich_text_to_plain_and_urls(rich_text)
        plain = plain.strip()
        # 整段就是一個 URL → 僅作 URL 流，不重複塞進文字流
        if plain and URL_ONLY_REGEX.match(plain):
            urls.append(plain)
        else:
            urls.extend(embedded_urls)
            if plain:
                texts.append(plain)

    # 3. 遞迴子 block（toggle / list 子項等）
    # 注意：依使用者要求，child_page 不遞迴（保持簡單）
    if (
        block.get("has_children")
        and btype != "child_page"
        and depth < MAX_DEPTH
    ):
        children = fetch_all_blocks(notion, block["id"])
        for child in children:
            c_urls, c_texts = extract_from_block(notion, child, depth + 1)
            urls.extend(c_urls)
            texts.extend(c_texts)

    return urls, texts


def read_page(notion: Client, page_id: str) -> tuple[list[str], list[str]]:
    """
    主入口：讀取整個 Notion page。
    回傳: (unique_urls, text_chunks)
    """
    top_blocks = fetch_all_blocks(notion, page_id)
    if not top_blocks:
        raise RuntimeError(
            f"頁面 {page_id} 沒有任何 block。請確認 page_id 正確、"
            f"且該頁面已透過 Connections 分享給 Integration。"
        )

    all_urls: list[str] = []
    all_texts: list[str] = []
    for block in top_blocks:
        urls, texts = extract_from_block(notion, block)
        all_urls.extend(urls)
        all_texts.extend(texts)

    # URL 去重（保留順序）
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return unique_urls, all_texts
