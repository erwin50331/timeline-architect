"""模組 D：將時間軸事件寫入 Notion Timeline Database。

日期處理：
- YYYY-MM-DD → 直接寫入，Date_Precision = "day"
- YYYY-MM    → 補 -01，Date_Precision = "month"
- YYYY       → 補 -01-01，Date_Precision = "year"
- 其他       → 跳過該筆並警告
"""
from __future__ import annotations
import calendar
import re
import sys
from notion_client import Client

DATE_FULL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_YEAR_RE = re.compile(r"^\d{4}$")


def normalize_date(date_str: str) -> tuple[str | None, str]:
    """將 Date 字串標準化為 Notion Date 欄位可吃的 YYYY-MM-DD，並回傳精度。"""
    date_str = (date_str or "").strip()
    if DATE_FULL_RE.match(date_str):
        return date_str, "day"
    if DATE_MONTH_RE.match(date_str):
        return f"{date_str}-01", "month"
    if DATE_YEAR_RE.match(date_str):
        return f"{date_str}-01-01", "year"
    return None, "unknown"


def normalize_end_date(date_str: str) -> str | None:
    """將 Date_End 標準化為該期間最後一天：YYYY → YYYY-12-31，YYYY-MM → 該月底，YYYY-MM-DD → 原值。"""
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    if DATE_FULL_RE.match(date_str):
        return date_str
    if DATE_MONTH_RE.match(date_str):
        year, month = date_str.split("-")
        last_day = calendar.monthrange(int(year), int(month))[1]
        return f"{date_str}-{last_day:02d}"
    if DATE_YEAR_RE.match(date_str):
        return f"{date_str}-12-31"
    return None


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def write_event(notion: Client, database_id: str, event: dict) -> bool:
    """寫入單一事件至 database，回傳是否成功。"""
    norm_date, precision = normalize_date(event.get("Date", ""))
    if not norm_date:
        print(
            f"  ⚠ 無法解析日期 {event.get('Date')!r}，跳過："
            f"{event.get('Event', '')[:60]}",
            file=sys.stderr,
        )
        return False

    # 處理選用的 Date_End（用於 inferred birth-year 等不確定區間）
    date_value: dict = {"start": norm_date}
    date_end_raw = event.get("Date_End")
    if date_end_raw:
        norm_end = normalize_end_date(date_end_raw)
        if norm_end and norm_end >= norm_date:
            date_value["end"] = norm_end

    properties: dict = {
        "Name": {
            "title": [{"text": {"content": event.get("Event", "")[:2000]}}]
        },
        "Date": {"date": date_value},
        "Phase": {"select": {"name": event["Phase"]}},
        "Date_Precision": {"select": {"name": precision}},
    }

    src_ref = event.get("Source_Ref", "")
    # Source 欄位為 URL 型：只有 URL 才寫入；純文字標記留空
    if is_url(src_ref):
        properties["Source"] = {"url": src_ref}

    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties=properties,
        )
        return True
    except Exception as e:
        print(
            f"  ⚠ 寫入失敗：{event.get('Event', '')[:60]} — {e}",
            file=sys.stderr,
        )
        return False


def write_timeline(
    notion: Client, database_id: str, events: list[dict]
) -> tuple[int, int]:
    """主入口：寫入整批事件，回傳 (success, fail)。"""
    success = 0
    fail = 0
    for evt in events:
        if write_event(notion, database_id, evt):
            success += 1
        else:
            fail += 1
    return success, fail
