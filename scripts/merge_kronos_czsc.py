#!/usr/bin/env python3
"""
合并 Kronos 预测报告与 CZSC 缠论分析报告。

用法:
    python scripts/merge_kronos_czsc.py [日期]
    python scripts/merge_kronos_czsc.py 20260604          # 指定日期
    python scripts/merge_kronos_czsc.py                    # 默认今天

输入（自选股）:
    ~/peiking88/Kronos/outputs/kronos-zxg-yyyymmdd.md
    ~/peiking88/czsc/output/czsc-zxg-yyyymmdd.md

输出:
    ~/peiking88/czsc/output/merged-zxg-yyyymmdd.md
"""

import os
import re
import sys
from collections import OrderedDict
from datetime import date

# ── 盘面分析资讯源 ──
# 头条博主（衡山佛曰论股）— 用户主页，无需每日更新
TOUTIAO_USER_URL = "https://www.toutiao.com/c/user/token/MS4wLjABAAAAI86oR8kKzMvj-6geoYfW2ovdpuUUZzDDaxScGnivmtA/"
TOUTIAO_AUTHOR = "衡山佛曰论股"
# 头条文章 ID 缓存文件（由 web_reader 或手动维护）
TOUTIAO_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output", ".toutiao_latest.json")

# ── 盘面分析资讯渠道 ──
# 优先从主流财经网站直接抓取最新资讯页面（不依赖搜索引擎）。
# 注：财联社、雪球等站点为纯 JS 渲染，requests 无法获取实质内容，暂不纳入。
MARKET_NEWS_CHANNELS = [
    {"name": "华尔街见闻", "urls": [
        "https://wallstreetcn.com/news/global",
    ]},
    {"name": "新浪财经", "urls": [
        "https://finance.sina.com.cn/7x24/",
        "https://finance.sina.com.cn/stock/marketresearch/",
    ]},
    {"name": "凤凰财经", "urls": [
        "https://finance.ifeng.com/stock/",
    ]},
    {"name": "第一财经", "urls": [
        "https://www.yicai.com/news/",
    ]},
    {"name": "金融界", "urls": [
        "https://stock.jrj.com.cn/",
    ]},
    {"name": "集思录", "urls": [
        "https://www.jisilu.cn/home/explore/",
    ]},
]

# ── 分析师观点搜索渠道 ──
# 通过 Bing 搜索分析师最新观点，对返回结果做域名过滤（优先指定财经网站）。
MARKET_ANALYST_CHANNELS = [
    {"name": "陈果",     "query": "陈果 A股 最新观点 {month}"},
    {"name": "洪灏",     "query": "洪灏 A股 最新研判 {month}"},
    {"name": "高盛",     "query": "高盛 A股 最新观点 {month}"},
    {"name": "大摩",     "query": "摩根士丹利 A股 最新策略 {month}"},
    {"name": "中信证券", "query": "中信证券 A股 投资策略 {month}"},
]

# 搜索结果域名白名单（优先匹配的取前 2 条，兜底取任意域名前 2 条）
_FINANCE_DOMAINS = [
    # 6 个指定财经网站
    "wallstreetcn.com", "sina.com.cn", "sina.cn",
    "ifeng.com", "yicai.com", "jrj.com.cn", "jisilu.cn",
    # 兜底：其他主流财经网站
    "eastmoney.com", "stcn.com", "caixin.com",
    "cls.cn", "cs.com.cn", "10jqka.com.cn",
    "sohu.com", "163.com", "36kr.com",
]


def _is_finance_domain(url: str) -> bool:
    """检查 URL 是否属于财经网站域名白名单。"""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in _FINANCE_DOMAINS)


def _fetch_toutiao_content(url: str, timeout: int = 15) -> str:
    """通过头条移动端 API 获取微头条/文章内容。

    头条页面为纯 CSR（client-side rendering），requests 无法直接抓取。
    使用 m.toutiao.com/i{item_id}/info/ API 可获取 JSON 格式正文。

    返回: 纯文本内容（截取前 2000 字符），失败时返回 [获取失败: ...]
    """
    import requests as _req

    # 从 URL 提取 item_id: /w/1234567890/ 或 /article/1234567890/
    m = re.search(r"/(?:w|article)/(\d+)", url)
    if not m:
        return _fetch_url_text(url, timeout)  # 非标准 URL，回退到普通抓取

    item_id = m.group(1)
    api_url = f"https://m.toutiao.com/i{item_id}/info/"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.toutiao.com/",
        "Cookie": "ttwid=1",
    }
    try:
        resp = _req.get(api_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        thread = data.get("data", {}).get("thread", {})
        base = thread.get("thread_base", {})
        content = base.get("content", "")

        if not content:
            return "[获取失败: 头条API返回空内容]"

        # 提取作者名称: thread_base.user.info.name
        user_info = base.get("user", {}).get("info", {})
        author = user_info.get("name", "")

        # 提取发布日期: create_time 为 Unix 时间戳
        create_ts = base.get("create_time", 0)
        from datetime import datetime as _dt
        date_str = _dt.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""

        result = f"【{author}】\n" if author else ""
        if date_str:
            result += f"日期: {date_str}\n"
        result += content[:2000]
        return result
    except Exception as e:
        return f"[获取失败: {e}]"


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


# DDG 不可达时缓存结果，避免每个渠道重复超时
_ddg_unreachable = None  # None=未检测, True=不可达, False=可达


def _web_search(query: str, max_results: int = 3, timeout: int = 5) -> list[dict]:
    """使用 DuckDuckGo HTML 搜索，返回 [{title, snippet, url, date}]。"""
    global _ddg_unreachable
    if _ddg_unreachable:
        return []

    import requests as _req
    from urllib.parse import quote_plus

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    results = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text

        # DuckDuckGo HTML 结果格式: <a class="result__a" href="...">Title</a>
        # 后跟 <a class="result__snippet">...</a>
        # 每条结果在 <div class="result"> 内
        result_blocks = re.findall(
            r'<div class="result[^"]*">(.*?)</div>\s*(?:<div class="result)',
            html, re.S,
        )
        # 更宽松的匹配：逐个 result__a 提取
        if not result_blocks:
            result_blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                html, re.S,
            )
            for link, title, tail in result_blocks[:max_results]:
                title_clean = re.sub(r"<[^>]+>", "", title).strip()
                snippet_match = re.search(
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', tail, re.S,
                )
                snippet = ""
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                d = _extract_date(title_clean + " " + snippet)
                results.append({
                    "title": title_clean,
                    "snippet": snippet,
                    "url": link,
                    "date": d,
                })
            return results

        # 从 result blocks 提取
        for block in result_blocks[:max_results]:
            link_m = re.search(r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            if not link_m:
                continue
            link = link_m.group(1)
            title_clean = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
            snippet_m = re.search(
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, re.S,
            )
            snippet = ""
            if snippet_m:
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip()
            d = _extract_date(title_clean + " " + snippet)
            results.append({
                "title": title_clean,
                "snippet": snippet,
                "url": link,
                "date": d,
            })
    except Exception as e:
        _ddg_unreachable = True
        print(f"  [DDG 搜索失败: {e}]")

    return results


def _bing_search(query: str, max_results: int = 3, timeout: int = 15) -> list[dict]:
    """使用 Bing 搜索（备选），返回 [{title, snippet, url, date}]。"""
    import requests as _req
    from urllib.parse import quote_plus

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    results = []
    try:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=zh-CN"
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text

        # Bing 结果在 <li class="b_algo"> 内
        blocks = re.findall(r'<li[^>]*class="[^\"]*b_algo[^\"]*"[^>]*>(.*?)</li>', html, re.S)
        for block in blocks[:max_results]:
            # 提取标题和链接
            link_m = re.search(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            if not link_m:
                continue
            link = link_m.group(1)
            title_clean = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
            # 提取摘要
            snippet_m = re.search(
                r'<div class="b_caption"[^>]*>.*?<p>(.*?)</p>', block, re.S,
            )
            snippet = ""
            if snippet_m:
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip()
            # 备选摘要位置
            if not snippet:
                snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
                if snippet_m:
                    snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip()
            d = _extract_date(title_clean + " " + snippet)
            results.append({
                "title": title_clean,
                "snippet": snippet,
                "url": link,
                "date": d,
            })
    except Exception as e:
        print(f"  [Bing 搜索失败: {e}]")

    return results


def _load_toutiao_cache() -> str:
    """从缓存文件加载头条博主最新内容。

    缓存文件 ``output/.toutiao_latest.json`` 由 Claude Code 通过 web_reader
    定期更新，或由用户手动维护。格式::

        {"content": "博主最新微头条正文...", "updated": "2026-06-12T08:30:00"}
    """
    import json as _json

    if not os.path.exists(TOUTIAO_CACHE_FILE):
        return ""

    try:
        with open(TOUTIAO_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = _json.load(f)
        content = cache.get("content", "")
        if content:
            updated = cache.get("updated", "")
            print(f"    头条缓存 (更新于 {updated})")
            return content[:2000]
    except Exception:
        pass
    return ""


def _fetch_toutiao_latest_item_ids(author: str, max_ids: int = 5, timeout: int = 15) -> list[str]:
    """通过 Bing 搜索获取头条博主最新文章 item_id 列表。

    搜索 ``"作者名" site:toutiao.com``，从结果中提取 /w/ 和 /article/ 的 ID，
    去重保序后返回。作为缓存文件的回退方案。
    """
    import requests as _req
    from urllib.parse import quote_plus

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    query = f'"{author}" site:toutiao.com'
    url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=zh-CN"
    try:
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        raw_ids = re.findall(r"toutiao\.com/(?:w|article)/(\d{10,})", resp.text)
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for i in raw_ids:
            if i not in seen:
                seen.add(i)
                unique.append(i)
        if unique:
            print(f"    Bing 搜索: {unique[:3]}")
        return unique[:max_ids]
    except Exception as e:
        print(f"    [Bing 搜索失败: {e}]")

    return []


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
        return ""

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()

    # 过滤空内容和获取失败的
    valid = [c for c in news_contents if c and not c.startswith("[获取失败")]
    if not valid:
        return ""

    combined = "\n\n---\n\n".join(valid)
    today = date.today().strftime("%Y-%m-%d")

    prompt = (
        f"你是一位专业的A股市场分析师。今天是 {today}。\n"
        "请根据以下市场资讯（已按日期从新到旧排列），生成一份简洁的今日盘面分析报告。\n\n"
        "**输出要求：**\n"
        "1. 仅输出一个章节：「综合研判与操作建议」\n"
        "2. 将近两周各机构/分析师的核心观点**自然融合**到综合研判中，作为论据支撑你的判断，不要逐条罗列\n"
        "3. 不要标注任何来源名称、机构名称或具体日期\n"
        "4. 综合研判需涵盖：大盘走势判断、板块轮动方向、风险提示\n"
        "5. 操作建议可结合缠论视角（中枢、买卖点、背驰等）给出具体策略\n"
        "6. 使用 Markdown 格式，语言简洁，重点突出，全文控制在 500 字以内\n\n"
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


# ── 盘面分析缓存（zxg/etf 共用，避免重复抓取） ──
_market_analysis_cache = None


def _fetch_market_analysis() -> str:
    """获取资讯并生成盘面分析（带缓存）"""
    global _market_analysis_cache
    if _market_analysis_cache is not None:
        print("  ✓ 盘面分析（缓存）")
        return _market_analysis_cache

    # ── 第一阶段：头条博主观点（实时搜索优先 → 缓存兜底） ──
    print("获取盘面资讯...")
    contents = []  # list[dict]: {source, date, text}
    if TOUTIAO_USER_URL:
        print(f"  获取头条: {TOUTIAO_AUTHOR}")
        toutiao_text = ""
        # 优先：Bing 搜索 → m.toutiao.com API（实时获取最新内容）
        item_ids = _fetch_toutiao_latest_item_ids(TOUTIAO_AUTHOR)
        for item_id in item_ids:
            toutiao_url = f"https://www.toutiao.com/w/{item_id}/"
            text = _fetch_toutiao_content(toutiao_url)
            if text and not text.startswith("[获取失败"):
                toutiao_text = text
                print(f"    ✓ API 获取成功: {toutiao_url}")
                break
        # 兜底：磁盘缓存（仅搜索失败时使用）
        if not toutiao_text:
            toutiao_text = _load_toutiao_cache()
            if toutiao_text:
                print(f"    ✓ 使用磁盘缓存（实时搜索失败）")
        if toutiao_text:
            d = _extract_date(toutiao_text)
            contents.append({"source": TOUTIAO_AUTHOR, "date": d, "text": toutiao_text})
            print(f"    ✓ 头条内容获取成功")
        else:
            print(f"    - 头条内容获取失败（搜索和缓存均无结果）")

    # ── 第二阶段：财经网站资讯 ──
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

    # ── 第三阶段：分析师观点（Bing 搜索 + 域名过滤） ──
    today = date.today()
    month_str = f"{today.year}年{today.month}月"
    for ch in MARKET_ANALYST_CHANNELS:
        query = ch["query"].replace("{month}", month_str)
        print(f"  搜索分析师: {ch['name']} — {query}")
        results = _bing_search(query, max_results=5)
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

    # ── 按日期降序排列（无日期排末尾） ──
    def _sort_key(item):
        d = item.get("date", "")
        return d if d else "0000-00-00"
    contents.sort(key=_sort_key, reverse=True)

    # ── 第三阶段：交给 LLM 生成分析 ──
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
    else:
        print("  - 跳过盘面分析（未配置 DEEPSEEK_API_KEY 或无有效资讯）")
    _market_analysis_cache = analysis
    return analysis


def demote_headings(content: str, levels: int = 3) -> str:
    """将 markdown 内容中的标题统一降级若干级.

    例如 levels=3 时:  ## 趋势质量评估 → ##### 趋势质量评估
    仅处理 markdown # 标题，不处理 HTML <h1> 等标签。
    """
    extra = "#" * levels

    def _replace(m: re.Match) -> str:
        return extra + m.group(0)

    return re.sub(r"^(#{1,6})\s", _replace, content, flags=re.MULTILINE)


def normalize_code(code: str) -> str:
    """统一股票代码格式为 'sh600123' / 'sz000001' 小写形式."""
    code = code.strip().lower()
    # CZSC 格式: "600549.SH" / "399006.SZ"
    m = re.match(r"^(\d+)\.(sh|sz)$", code)
    if m:
        return f"{m.group(2)}{m.group(1)}"
    # Kronos 格式: "sh600549" / "sz399006" (已是目标格式)
    m = re.match(r"^(sh|sz)(\d+)$", code)
    if m:
        return code
    return code


def parse_czsc(filepath: str) -> OrderedDict:
    """解析 CZSC 缠论报告，返回 {normalized_code: markdown_content}."""
    if not os.path.exists(filepath):
        print(f"[WARN] CZSC 文件不存在: {filepath}")
        return OrderedDict()

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    stocks = OrderedDict()

    # CZSC 有两种标题格式:
    #   格式1: # 名字（CODE） 缠论趋势预测
    #   格式2: <h1><span...>★★</span>  名字（CODE） 缠论趋势预测</h1>
    #   格式3: <h1>名字（CODE） 缠论趋势预测</h1>（无星级）
    # 统一用一个灵活的 pattern 匹配

    # 核心匹配: 名字（CODE.SH） 缠论趋势预测
    inner = r"(.+?)[（(](\d{6})\.(SH|SZ|sh|sz)[）)]\s*缠论趋势预测"

    # 格式1: markdown 标题
    md_pattern = re.compile(r"^#+\s*" + inner + r".*$", re.MULTILINE)
    # 格式2/3: HTML 标题
    html_pattern = re.compile(r"<h1[^>]*>.*?" + inner + r".*?</h1>", re.MULTILINE)

    # 收集所有匹配 (position, name, code_num, market, is_html)
    all_matches = []

    for m in md_pattern.finditer(content):
        # 排除报告总标题行（"缠论趋势预测报告"）
        if "报告" in m.group(1):
            continue
        all_matches.append((m.start(), m.group(1).strip(), m.group(2), m.group(3).lower(), False, m.end()))

    for m in html_pattern.finditer(content):
        all_matches.append((m.start(), m.group(1).strip(), m.group(2), m.group(3).lower(), True, m.end()))

    # 按位置排序
    all_matches.sort(key=lambda x: x[0])

    for i, (pos, name, code_num, market, is_html, end_pos) in enumerate(all_matches):
        code = f"{market}{code_num}"
        ncode = normalize_code(code)

        start = end_pos
        end = all_matches[i + 1][0] if i + 1 < len(all_matches) else len(content)
        section = content[start:end].strip()

        stocks[ncode] = {
            "name": name,
            "code": code,
            "content": section,
        }

    print(f"[CZSC] 解析到 {len(stocks)} 只股票/指数")
    return stocks


def parse_kronos(filepath: str) -> OrderedDict:
    """解析 Kronos 预测报告，返回 {normalized_code: {'name':..., 'category':..., 'content':...}}."""
    if not os.path.exists(filepath):
        print(f"[WARN] Kronos 文件不存在: {filepath}")
        return OrderedDict()

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    stocks = OrderedDict()

    # 匹配所有个股/指数标题: #### 名字 (code) 或 #### 名字 (code) [category]
    header_pattern = re.compile(r"^####\s+(.+?)\s+\(((?:sh|sz|SH|SZ)\d{6})\)(?:\s*\[(.+?)\])?\s*$", re.MULTILINE)

    # 找到所有 ### 区块边界，用于推断 category
    sec_pattern = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    sec_matches = list(sec_pattern.finditer(content))

    # 构建每个位置所属的 category
    def get_category(pos: int) -> str:
        cat = "其他"
        for si, sec_m in enumerate(sec_matches):
            sec_start = sec_m.start()
            sec_end = sec_matches[si + 1].start() if si + 1 < len(sec_matches) else len(content)
            if sec_start <= pos < sec_end:
                cat = sec_m.group(1).strip()
                break
        return cat

    # 找到所有匹配位置
    header_matches = list(header_pattern.finditer(content))

    for i, m in enumerate(header_matches):
        name = m.group(1).strip()
        code = m.group(2).lower()
        explicit_cat = m.group(3)  # 可能为 None
        ncode = normalize_code(code)

        # 确定 category: 优先用 header 中的显式标签，否则根据所在区块推断
        if explicit_cat:
            category = explicit_cat.strip()
        else:
            category = get_category(m.start())

        # 提取该股票的内容：从 header 结束到下一个 #### 或文件结束
        start = m.end()
        end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(content)
        raw_section = content[start:end]

        # 截断到 --- 或 ##  或 ### （下一个大区块）
        cutoff = re.search(r"(\n---\n|\n##\s|\n###\s)", raw_section)
        if cutoff:
            section = raw_section[: cutoff.start()].strip()
        else:
            section = raw_section.strip()

        # 清除其他股票的导航锚点标签及前面关联的空行
        section = re.sub(r'\n\s*<a\s+id="[^"]*"></a>\s*$', "", section).strip()

        stocks[ncode] = {
            "name": name,
            "code": code,
            "category": category,
            "content": section,
        }

    print(f"[Kronos] 解析到 {len(stocks)} 只股票/指数")
    return stocks


def _has_czsc_buy_signal(czsc_content: str) -> bool:
    """判断 CZSC 内容是否包含强买入信号：加仓（三周期共振）或 ≥2 个周期有买点"""
    if not czsc_content:
        return False
    # 加仓 = 三周期共振买点（最强信号）
    if "加仓" in czsc_content:
        return True
    # 统计各周期买卖点中"买"的个数（排除"买卖点：-"和"卖"）
    buy_count = len(re.findall(r"买卖点[：:][^-]*买", czsc_content))
    return buy_count >= 2


def merge(date_str: str):
    """执行合并."""
    kronos_path = os.path.expanduser(f"~/peiking88/Kronos/outputs/kronos-zxg-{date_str}.md")
    czsc_path = os.path.expanduser(f"~/peiking88/czsc/output/czsc-zxg-{date_str}.md")
    output_path = os.path.expanduser(f"~/peiking88/czsc/output/merged-zxg-{date_str}.md")
    report_title = "Kronos + 缠论 联合分析报告"

    print(f"Kronos: {kronos_path}")
    print(f"CZSC:   {czsc_path}")
    print(f"输出:   {output_path}")
    print()

    kronos_stocks = parse_kronos(kronos_path)
    czsc_stocks = parse_czsc(czsc_path)

    # 盘面分析
    market_analysis = _fetch_market_analysis()

    # 收集所有代码（合并去重，保持 Kronos 顺序优先）
    all_codes = list(kronos_stocks.keys())
    for code in czsc_stocks:
        if code not in all_codes:
            all_codes.append(code)

    # 按类别分组（来自 Kronos）
    categories = OrderedDict()
    categories["指数"] = []
    categories["重点关注"] = []  # Kronos看涨/看平 + CZSC有买入/加仓信号
    categories["看涨"] = []
    categories["看平"] = []
    categories["看跌"] = []
    categories["其他"] = []  # 仅在 CZSC 中出现的

    for code in all_codes:
        ks = kronos_stocks.get(code)
        cs = czsc_stocks.get(code)
        cat = ks.get("category", "") if ks else ""

        # 指数直接归入指数组
        if cat == "指数":
            categories["指数"].append(code)
            continue

        # Kronos看涨/看平 + CZSC有买入信号 → 重点关注
        if cat in ("看涨", "看平") and cs and _has_czsc_buy_signal(cs.get("content", "")):
            categories["重点关注"].append(code)
        elif cat in categories:
            categories[cat].append(code)
        elif ks:
            categories["其他"].append(code)
        else:
            categories["其他"].append(code)

    # 写入合并文件
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# {report_title}\n\n")
        f.write(f"**日期**: {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}\n\n")
        f.write(f"**数据来源**: Kronos 价格预测 + CZSC 缠论趋势分析\n\n")
        #        f.write(f"---\n\n")

        # 目录（横向表格排版）
        f.write('<h2 id="目录">目录</h2>\n\n')
        COLS = 5  # 每行股票数
        for cat, codes in categories.items():
            if not codes:
                continue
            f.write(f"### {cat}（{len(codes)} 只）\n\n")
            f.write("<table>\n")
            for row_start in range(0, len(codes), COLS):
                row_codes = codes[row_start : row_start + COLS]
                f.write("<tr>\n")
                for code in row_codes:
                    ks = kronos_stocks.get(code, {})
                    cs = czsc_stocks.get(code, {})
                    name = ks.get("name") or cs.get("name") or code
                    f.write(
                        f'<td style="padding:4px 12px;text-align:center">'
                        f'<a href="#{code}">{name}<br>({code.upper()})</a>'
                        f"</td>\n"
                    )
                # 补齐空单元格
                for _ in range(COLS - len(row_codes)):
                    f.write("<td></td>\n")
                f.write("</tr>\n")
            f.write("</table>\n\n")

        # 盘面分析
        if market_analysis and not market_analysis.startswith("⚠️"):
            f.write("## 📊 盘面分析\n\n")
            f.write(market_analysis)
            f.write("\n\n---\n\n")

        # 逐股票输出
        stock_count = 0
        for cat, codes in categories.items():
            if not codes:
                continue
            f.write(f"## {cat}\n\n")
            for code in codes:
                stock_count += 1
                ks = kronos_stocks.get(code, {})
                cs = czsc_stocks.get(code, {})
                name = ks.get("name") or cs.get("name") or code

                f.write(f'<h3 id="{code}">{stock_count}. {name}（{code.upper()}）'
                        f' <a href="#目录" style="font-size:0.7em;color:#888;">↩ 目录</a></h3>\n\n')

                # Kronos 部分
                if ks:
                    f.write(f"#### 🔮 Kronos 价格预测\n")
                    f.write(demote_headings(ks["content"].rstrip()))
                    f.write("\n\n")
                else:
                    f.write(f"#### 🔮 Kronos 价格预测\n")
                    f.write("> ⚠️ 无 Kronos 预测数据\n\n")

                # CZSC 部分
                if cs:
                    f.write(f"#### 📊 CZSC 缠论趋势分析\n")
                    f.write(demote_headings(cs["content"].rstrip()))
                    f.write("\n\n")
                else:
                    f.write(f"#### 📊 CZSC 缠论趋势分析\n")
                    f.write("> ⚠️ 无 CZSC 缠论分析数据\n\n")

        #                f.write("---\n\n")

        # 附录：统计概览
        f.write("## 附录：统计概览\n\n")
        kronos_only = set(kronos_stocks.keys()) - set(czsc_stocks.keys())
        czsc_only = set(czsc_stocks.keys()) - set(kronos_stocks.keys())
        both = set(kronos_stocks.keys()) & set(czsc_stocks.keys())

        f.write(f"| 指标 | 数量 |\n")
        f.write(f"|---|---|\n")
        f.write(f"| Kronos 覆盖 | {len(kronos_stocks)} |\n")
        f.write(f"| CZSC 覆盖 | {len(czsc_stocks)} |\n")
        f.write(f"| 双覆盖 | {len(both)} |\n")
        f.write(f"| 仅 Kronos | {len(kronos_only)} |\n")
        f.write(f"| 仅 CZSC | {len(czsc_only)} |\n")

        if kronos_only:
            f.write(f"\n**仅 Kronos 覆盖**:\n")
            for code in kronos_only:
                f.write(f"- {kronos_stocks[code]['name']}（{code.upper()}）\n")

        if czsc_only:
            f.write(f"\n**仅 CZSC 覆盖**:\n")
            for code in czsc_only:
                f.write(f"- {czsc_stocks[code]['name']}（{code.upper()}）\n")

    print(f"✅ 合并完成: {output_path}")
    print(f"   双覆盖: {len(both)} | 仅Kronos: {len(kronos_only)} | 仅CZSC: {len(czsc_only)}")
    return output_path


def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = date.today().strftime("%Y%m%d")

    # 校验日期格式
    if not re.match(r"^\d{8}$", date_str):
        print(f"错误: 日期格式不正确，需要 YYYYMMDD，实际: {date_str}")
        sys.exit(1)

    merge(date_str)


if __name__ == "__main__":
    main()
