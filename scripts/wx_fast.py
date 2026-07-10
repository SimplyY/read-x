#!/Users/yuwei/.local/share/uv/tools/wechat-article-to-markdown/bin/python3
"""wx_fast: wechat-article-to-markdown 的轻量入口
- 优先 httpx 直连（~1-3s，零浏览器开销）
- 反爬/验证码时自动回退 Camoufox 浏览器（~9s）
- 两条路径都跳过图片下载
"""
import sys, re, argparse, asyncio
from pathlib import Path

TOOL_SITE = "/Users/yuwei/.local/share/uv/tools/wechat-article-to-markdown/lib/python3.10/site-packages"
sys.path.insert(0, TOOL_SITE)

import httpx
from bs4 import BeautifulSoup
from wechat_article_to_markdown import (
    extract_metadata, process_content, convert_to_markdown, build_markdown
)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _html_to_md(html: str, url: str, label: str = ""):
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_metadata(soup, html)
    if not meta.get("title"):
        return None, "no_title"
    content_html, code_blocks, img_urls = process_content(soup)
    if not content_html or len(content_html) < 200:
        return None, "no_content"
    md = convert_to_markdown(content_html, code_blocks)
    md = re.sub(r'!\[[^\]]*\]\([^)]*\)\n?', '', md)
    meta["source_url"] = url
    result = build_markdown(meta, md)
    print(f"📄 [{label}] 标题: {meta['title']}", file=sys.stderr)
    print(f"👤 [{label}] 作者: {meta.get('author','')}", file=sys.stderr)
    print(f"📅 [{label}] 时间: {meta.get('publish_time','')}", file=sys.stderr)
    print(f"📊 [{label}] Markdown 约 {len(md)} 字符, 跳过 {len(img_urls)} 张图", file=sys.stderr)
    return result, None


def fetch_httpx(url: str):
    print(f"🔄 尝试 httpx 直连...", file=sys.stderr)
    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=15)
    r.raise_for_status()
    return _html_to_md(r.text, url, "httpx")


async def fetch_camoufox(url: str):
    print(f"🦊 httpx 失败，回退 Camoufox 浏览器...", file=sys.stderr)
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("#js_content", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    return _html_to_md(html, url, "camoufox")


def fetch(url: str, output_path: Path | None = None):
    # 1) fast path
    try:
        result, err = fetch_httpx(url)
        if result:
            return result, "httpx"
        print(f"⚠️  httpx 返回异常({err})，尝试浏览器兜底", file=sys.stderr)
    except Exception as e:
        print(f"⚠️  httpx 报错({e})，尝试浏览器兜底", file=sys.stderr)

    # 2) camoufox fallback
    result, err = asyncio.run(fetch_camoufox(url))
    if not result:
        print(f"❌ Camoufox 也失败了({err})，可能文章已删除或需要人工验证", file=sys.stderr)
        sys.exit(1)
    return result, "camoufox"


def main():
    ap = argparse.ArgumentParser(description="Fast WeChat article fetch (httpx first, Camoufox fallback, no images)")
    ap.add_argument("url", help="https://mp.weixin.qq.com/s/...")
    ap.add_argument("-o", "--output", default="-", help="output .md path; default stdout")
    args = ap.parse_args()
    out = Path(args.output) if args.output != "-" else None
    import time
    t0 = time.time()
    md, method = fetch(args.url, out)
    dt = time.time() - t0
    print(f"⏱️  总耗时 {dt:.1f}s (方式: {method})", file=sys.stderr)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"✅ 已保存: {out}", file=sys.stderr)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
