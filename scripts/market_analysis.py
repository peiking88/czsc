#!/usr/bin/env python3
"""
盘面分析：抓取财经网站资讯、分析师观点、头条号博主文章，调用 LLM 生成盘面分析报告。

用法:
    python scripts/market_analysis.py [日期]
    python scripts/market_analysis.py 20260604          # 指定日期（仅影响输出文件名）
    python scripts/market_analysis.py                    # 默认今天

环境变量:
    DEEPSEEK_API_KEY   — API 密钥（未设置则跳过 LLM 生成）
    DEEPSEEK_BASE_URL  — API 基础地址（默认 https://api.deepseek.com/anthropic）
    DEEPSEEK_MODEL     — 模型名称（默认 deepseek-v4-pro）

输出:
    ~/peiking88/czsc/output/market-analysis-yyyymmdd.md
"""

import os
import re
import sys
from datetime import date

# ── 盘面分析资讯渠道 ──
# 优先从主流财经网站直接抓取最新资讯页面（不依赖搜索引擎）。
# 注：财联社、雪球等站点为纯 JS 渲染，requests 无法获取实质内容，暂不纳入。
MARKET_NEWS_CHANNELS: list = []

# ── 分析师观点搜索渠道 ──
# 通过 Bing 搜索分析师最新观点，对返回结果做域名过滤（优先指定财经网站）。
MARKET_ANALYST_CHANNELS = [
    {"name": "洪灏",     "query": "洪灏 A股 最新研判 近一周"},
    {"name": "陈果",     "query": "陈果 A股 最新观点 近一周"},
    {"name": "高盛",     "query": "高盛 A股 最新观点 近一周"},
    {"name": "大摩",     "query": "摩根士丹利 A股 最新策略 近一周"},
    {"name": "中信证券", "query": "中信证券 A股 投资策略 近一周"},
    {"name": "广发宏观", "query": "广发宏观 A股 最新观点 近一周"},
    {"name": "唐海清",   "query": "唐海清 天风证券 A股 最新观点 近两周"},
]

# ── 财经博主盘面分析（头条号） ──
# 通过头条号主页抓取博主最新盘面分析文章。
MARKET_BLOGGER_CHANNELS = [
    {"name": "衡山佛曰论股", "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAAI86oR8kKzMvj-6geoYfW2ovdpuUUZzDDaxScGnivmtA/"},
    {"name": "时间轨迹",     "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAAtLAFP3b8ZrE7gWwJ-2VEBWh0u6ClSyaXb0v93xv-eW0/"},
    {"name": "数据方向",     "url": "https://www.toutiao.com/c/user/token/CicxNMgTotM81WeMJw2M_ltdwxo3jNCOCoeKjJoCDQBnw5Hxp32mE_kaSQo8AAAAAAAAAAAAAFDF9B1VAQjYgD-NiggqAPaOTQDtJEow3BGvtWhBtYzgEdpjnPXafo0QkPRC6bv1DlI3ELyxmQ4Yw8WD6gQiAQMbN1XI/"},
    {"name": "股市刀锋",     "url": "https://www.toutiao.com/c/user/token/MS4wLjABAAAAa4wugAtuUC1SH1uxg-bGNFeAv-G8dk2yPlmnOU8pyBY/"},
    {"name": "徐小明",       "url": "https://www.toutiao.com/c/user/token/CiaOWGxskg8RM0eomSu4fKY0Wr-O1jwkuRENkHJhdXOGPiRcwKGHWhpJCjwAAAAAAAAAAAAAUMa-s4jXx2zbV3949v7bmKrIM0WQpV2mbQK-j230O4NTeUTcDHJN2devOFF-uMQbYZgQmrWZDhjDxYPqBCIBA0F54TE=?/"},
    {"name": "边风炜",       "url": "https://www.toutiao.com/c/user/token/CidknHfCUMgcTGfgy6WOaA8bEaoT9FXszGtw54HPEugG1kJ7U6AGChgaSQo8AAAAAAAAAAAAAFDGvrOI18ds21d_ePb-25iqyDNFkKVdpm0Cvo9t9DuDU3lE3AxyTdnXrzhRfrjEG2GYEIS3mQ4Yw8WD6gQiAQPfno9X/?"},
]

# 搜索结果域名白名单（优先匹配的取前 2 条，兜底取任意域名前 2 条）
_FINANCE_DOMAINS = [
    "eastmoney.com", "stcn.com", "caixin.com",
    "cls.cn", "cs.com.cn", "10jqka.com.cn",
    "sohu.com", "163.com", "36kr.com",
]


def _is_finance_domain(url: str) -> bool:
    """检查 URL 是否属于财经网站域名白名单。"""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in _FINANCE_DOMAINS)


def _fetch_url_text(url: str, timeout: int = 30) -> str:
    """从 URL 获取页面正文文本（简单去 HTML 标签）

    返回: 纯文本内容（截取前 2000 字符），失败时返回 [获取失败: ...]
    """
    import requests as _req

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"

        raw = resp.text
        # 提取 <title>
        title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        title = title_m.group(1).strip() if title_m else ""

        # 去掉 script / style / noscript
        raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", raw, flags=re.S | re.I)
        # 去掉 HTML 标签
        raw = re.sub(r"<[^>]+>", "\n", raw)
        # 清理空白
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

        content = raw[:2000]
        if title:
            content = f"【{title}】\n{content}"
        return content
    except Exception as e:
        return f"[获取失败: {e}]"


def _extract_date(text: str) -> str:
    """从文本中提取日期，返回 'YYYY-MM-DD' 格式；提取失败返回空字符串。"""
    # 优先匹配 YYYY-MM-DD / YYYY/MM/DD
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 匹配 YYYY年M月（无具体日）
    m = re.search(r"(\d{4})年(\d{1,2})月", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # 匹配 M月D日（无年份，默认当年）
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        y = date.today().year
        return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _baidu_search(query: str, max_results: int = 5, timeout: int = 15) -> list[dict]:
    """使用百度搜索，返回 [{title, snippet, url, date}]。

    百度对中文财经查询返回质量高且稳定，无搜狗反爬、Bing 中文乱码问题。
    """
    from urllib.parse import quote_plus

    import requests as _req

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    results = []
    try:
        url = f"https://www.baidu.com/s?wd={quote_plus(query)}&ie=utf-8"
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text

        if "百度安全验证" in html:
            print("  [百度触发验证码，跳过]")
            return results

        # 提取搜索结果: <h3 class="t"> → <a href="baidu redirect">title</a>
        h3_pattern = re.compile(
            r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>',
            re.S,
        )
        h3_matches = h3_pattern.findall(html)

        # 提取摘要
        snippets: list[str] = []
        for pat in [
            r'<span[^>]*class="[^"]*content-right_[^"]*"[^>]*>(.*?)</span>',
            r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>',
            r'<span[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</span>',
        ]:
            raw = re.findall(pat, html, re.S)
            if raw:
                snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in raw]
                break

        # 解析每条结果，同时把百度跳转链解析为真实 URL
        seen_urls = set()
        for i, (baidu_url, title_html) in enumerate(h3_matches[:max_results]):
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            if not title:
                continue

            # 跟百度 302 跳转拿到真实 URL
            real_url = baidu_url
            try:
                hr = _req.head(baidu_url, headers=headers, timeout=5, allow_redirects=False)
                real_url = hr.headers.get("Location", baidu_url)
            except Exception:
                pass

            if real_url in seen_urls:
                continue
            seen_urls.add(real_url)

            snippet = snippets[i] if i < len(snippets) else ""
            d = _extract_date(title + " " + snippet)

            results.append({
                "title": title,
                "snippet": snippet,
                "url": real_url,
                "date": d,
            })
    except Exception as e:
        print(f"  [百度搜索失败: {e}]")

    return results


def _generate_market_analysis(news_contents: list[str]) -> str:
    """调用 DeepSeek Anthropic 兼容 API 生成盘面分析

    环境变量:
      DEEPSEEK_API_KEY  — API 密钥（未设置则跳过）
      DEEPSEEK_BASE_URL — API 基础地址（默认 https://api.deepseek.com/anthropic）
      DEEPSEEK_MODEL    — 模型名称（默认 deepseek-v4-pro）
    """
    import requests as _req

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("  - 跳过盘面分析（未配置环境变量 DEEPSEEK_API_KEY）")
        return ""

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()

    # 过滤空内容和获取失败的
    valid = [c for c in news_contents if c and not c.startswith("[获取失败")]
    if not valid:
        print(f"  - 跳过盘面分析（无有效资讯，{len(news_contents)} 条均为空或获取失败）")
        return ""

    combined = "\n\n---\n\n".join(valid)
    today = date.today().strftime("%Y-%m-%d")

    prompt = (
        f"你是一位专业的A股市场分析师。今天是 {today}。\n"
        "请根据以下市场资讯（已按日期从新到旧排列），生成一份简洁的今日盘面分析报告。\n"
        "资讯来源包括：专业分析师观点、财经博主（头条号）盘面分析。\n\n"
        "**输出要求：**\n"
        "1. 输出两个章节：「各分析师观点」和「综合研判与操作建议」\n"
        "2. 「各分析师观点」章节：按分析师/博主逐一列出其核心观点，**必须标注姓名和日期**，每人一段，格式为「**姓名**（日期）：观点摘要」\n"
        "3. 「综合研判与操作建议」章节：基于以上各分析师观点进行综合研判，涵盖：大盘走势判断、板块轮动方向、风险提示\n"
        "4. 操作建议可结合缠论视角（中枢、买卖点、背驰等）给出具体策略\n"
        "5. 使用 Markdown 格式，语言简洁，重点突出，全文控制在 800 字以内\n\n"
        f"---\n\n{combined}"
    )

    url = f"{base_url}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": 2000,
        "system": "你是一位专业的A股市场分析师，擅长结合缠论技术分析和基本面进行市场研判。",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    try:
        resp = _req.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        # Anthropic Messages API 返回格式: {"content": [{"type": "text", "text": "..."}]}
        content_blocks = data.get("content", [])
        texts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        return "\n".join(texts) if texts else ""
    except Exception as e:
        return f"⚠️ 盘面分析生成失败: {e}"


_playwright_browser = None


def _get_playwright_browser():
    """懒加载 Playwright chromium 实例，跨博主复用。"""
    global _playwright_browser
    if _playwright_browser is None:
        from playwright.sync_api import sync_playwright
        _playwright_browser = sync_playwright().start().chromium.launch(headless=True)
    return _playwright_browser


def _close_playwright_browser():
    """关闭 Playwright 浏览器实例（进程退出时清理）。"""
    global _playwright_browser
    if _playwright_browser is not None:
        try:
            _playwright_browser.close()
        except Exception:
            pass
        _playwright_browser = None


def _fetch_blogger_posts(ch: dict, max_posts: int = 3) -> list[dict]:
    """用 Playwright 抓取头条号博主主页，提取最新文章标题和正文预览。

    头条主页为 JS 渲染，Playwright 加载后从 ``.feed-card-wtt-wrapper`` 卡片
    提取时间（``.time``）和正文预览（``p.content``），每博主最多取 ``max_posts`` 篇。
    """
    name = ch["name"]
    url = ch["url"]
    print(f"  获取博主: {name} — {url}")

    posts: list[dict] = []
    try:
        browser = _get_playwright_browser()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 等待文章卡片渲染（两种主页结构）
            cards = []
            for selector in (".feed-card-wtt-wrapper", ".feed-card-wrapper.feed-card-article-wrapper"):
                try:
                    page.wait_for_selector(selector, timeout=8000)
                    cards = page.query_selector_all(selector)
                    if cards:
                        break
                except Exception:
                    continue
            if not cards:
                print(f"    - {name}: 文章卡片未出现")
                return posts
            page.wait_for_timeout(1500)  # 额外等待懒加载

            for card in cards[:max_posts]:
                # 微头条结构：p.content + .time
                content_el = card.query_selector("p.content")
                time_el = card.query_selector(".time")
                # 文章结构：a.title + .feed-card-footer-time-cmp
                if not content_el:
                    content_el = card.query_selector("a.title")
                if not time_el:
                    time_el = card.query_selector(".feed-card-footer-time-cmp")

                time_text = time_el.inner_text().strip() if time_el else ""
                content_text = content_el.inner_text().strip() if content_el else ""

                if not content_text:
                    continue

                # 解析 "N小时前" / "N分钟前" 为日期
                d = _parse_relative_time(time_text) or _extract_date(content_text[:200])

                posts.append({
                    "source": f"博主-{name}",
                    "date": d,
                    "title": content_text.split("\n", 1)[0][:60],
                    "text": content_text,
                })
        finally:
            page.close()
    except Exception as e:
        print(f"    - {name}: 获取失败 {e}")

    return posts


def _parse_relative_time(text: str) -> str:
    """将 'N小时前' / 'N分钟前' / '昨天' 等相对日期转为 'YYYY-MM-DD'。"""
    from datetime import timedelta

    text = text.strip()
    now = date.today()

    if "分钟前" in text or "小时前" in text or text == "刚刚":
        return now.strftime("%Y-%m-%d")
    if "昨天" in text:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if "前天" in text:
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    # "M月D日" 格式
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{now.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def fetch_market_analysis() -> str:
    """获取资讯并生成盘面分析。

    三阶段抓取：财经网站资讯 → 分析师观点（百度搜索）→ 头条号博主文章，
    然后调用 LLM 生成盘面分析报告。成功返回正文；未配置 API key 或无有效资讯
    时返回空串；生成失败返回以 ``⚠️`` 开头的错误信息。
    """
    import time as _time

    # ── 第一阶段：财经网站资讯 ──
    print("获取盘面资讯...")
    contents = []  # list[dict]: {source, date, title, text}
    for ch in MARKET_NEWS_CHANNELS:
        fetched = 0
        for page_url in ch.get("urls", []):
            print(f"  获取: {ch['name']} — {page_url}")
            page_text = _fetch_url_text(page_url)
            if page_text and not page_text.startswith("[获取失败"):
                d = _extract_date(page_text[:500])
                contents.append({
                    "source": ch["name"],
                    "date": d,
                    "title": f"{ch['name']}最新资讯",
                    "text": page_text,
                })
                fetched += 1
        if fetched:
            print(f"    ✓ {ch['name']}: {fetched} 条")
        else:
            print(f"    - {ch['name']}: 获取失败")

    # ── 第二阶段：分析师观点（百度搜索 + 域名过滤） ──
    for ch in MARKET_ANALYST_CHANNELS:
        query = ch["query"]
        print(f"  搜索分析师: {ch['name']} — {query}")
        results = _baidu_search(query, max_results=5)
        # 域名过滤：优先财经网站
        filtered = [r for r in results if _is_finance_domain(r["url"])]
        # 兜底：无域名匹配时取前 2 条（任意域名）
        if not filtered and results:
            filtered = results[:2]
        fetched = 0
        for sr in filtered[:2]:
            page_text = _fetch_url_text(sr["url"])
            if page_text and not page_text.startswith("[获取失败"):
                d = sr.get("date") or _extract_date(page_text[:500])
                contents.append({
                    "source": ch["name"],
                    "date": d,
                    "title": sr.get("title", ""),
                    "text": page_text,
                })
                fetched += 1
        if fetched:
            print(f"    ✓ {ch['name']}: {fetched} 条")
        else:
            print(f"    - {ch['name']}: 无结果")
        _time.sleep(1.5)  # 百度反爬间隔

    # ── 第三阶段：财经博主盘面分析（头条号） ──
    try:
        for ch in MARKET_BLOGGER_CHANNELS:
            posts = _fetch_blogger_posts(ch)
            contents.extend(posts)
            if posts:
                print(f"    ✓ 博主-{ch['name']}: {len(posts)} 条")
            else:
                print(f"    - 博主-{ch['name']}: 无结果")
            _time.sleep(1.0)
    finally:
        _close_playwright_browser()

    # ── 按日期降序排列（无日期排末尾） ──
    def _sort_key(item):
        d = item.get("date", "")
        return d if d else "0000-00-00"
    contents.sort(key=_sort_key, reverse=True)

    # ── 第四阶段：交给 LLM 生成分析 ──
    # 构造带来源标注的内容
    annotated = []
    for item in contents:
        parts = [f"【来源: {item['source']}】"]
        if item.get("date"):
            parts.append(f"日期: {item['date']}")
        if item.get("title"):
            parts.append(f"标题: {item['title']}")
        parts.append(item["text"])
        annotated.append("\n".join(parts))

    print("生成盘面分析（LLM）...")
    analysis = _generate_market_analysis(annotated)
    if analysis and not analysis.startswith("⚠️"):
        print("  ✓ 盘面分析完成")
    elif analysis:
        print(f"  {analysis}")
    # analysis 为空时，跳过原因已在 _generate_market_analysis 内打印，此处不重复
    return analysis


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    date_str = argv[0] if argv else date.today().strftime("%Y%m%d")
    print(f"盘面分析（{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}）")

    analysis = fetch_market_analysis()
    if not analysis:
        # 跳过原因已在 fetch_market_analysis 内打印
        return
    if analysis.startswith("⚠️"):
        print(analysis)
        return

    output_path = os.path.expanduser(f"~/peiking88/czsc/output/market-analysis-{date_str}.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# 📊 盘面分析（{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}）\n\n")
        f.write(analysis)
        f.write("\n")
    print(f"✅ 盘面分析: {output_path}")


if __name__ == "__main__":
    main()
