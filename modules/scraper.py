"""模組 B：抓取 URL 純文字正文。

主路徑：trafilatura
備援：newspaper3k（若匯入失敗則優雅跳過）
"""
from __future__ import annotations
import sys
import trafilatura

# newspaper3k 為選用備援；在 Windows 上某些版本可能裝不起來，所以做 soft import
try:
    from newspaper import Article
    NEWSPAPER_AVAILABLE = True
except ImportError:
    NEWSPAPER_AVAILABLE = False


def fetch_with_trafilatura(url: str) -> str | None:
    """主路徑：用 trafilatura 抓取並萃取正文。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        # 過短內容可能是錯誤頁、登入牆等
        if text and len(text.strip()) > 80:
            return text
        return None
    except Exception as e:
        print(f"  [trafilatura] 失敗 {url}：{e}", file=sys.stderr)
        return None


def fetch_with_newspaper(url: str) -> str | None:
    """備援：用 newspaper3k 抓取。"""
    if not NEWSPAPER_AVAILABLE:
        return None
    try:
        article = Article(url)
        article.download()
        article.parse()
        text = article.text
        if text and len(text.strip()) > 80:
            return text
        return None
    except Exception as e:
        print(f"  [newspaper3k] 失敗 {url}：{e}", file=sys.stderr)
        return None


def scrape(url: str) -> str | None:
    """
    主入口：先試 trafilatura，失敗再試 newspaper3k。
    兩者都失敗回 None（單一 URL 失敗不中斷整體流程）。
    """
    print(f"  抓取 {url}", file=sys.stderr)
    text = fetch_with_trafilatura(url)
    if text:
        return text
    text = fetch_with_newspaper(url)
    if text:
        return text
    print(f"  ⚠ 兩種方法都失敗：{url}", file=sys.stderr)
    return None
