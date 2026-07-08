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

# ── 盘面分析资讯渠道 ──
# 优先从主流财经网站直接抓取最新资讯页面（不依赖搜索引擎）。
# 注：财联社、雪球等站点为纯 JS 渲染，requests 无法获取实质内容，暂不纳入。
MARKET_NEWS_CHANNELS: list = []

# ── 分析师观点搜索渠道 ──
# 通过 Bing 搜索分析师最新观点，对返回结果做域名过滤（优先指定财经网站）。
MARKET_ANALYST_CHANNELS = [
    {"name": "洪灏",     "query": "洪灏 A股 最新研判 近一周"},
    {"name": "陈果",     "query": "陈果 A股 最新观点 近一周"},
    {"name": "李迅雷",   "query": "李迅雷 A股 最新观点 近一周"},
    {"name": "高盛",     "query": "高盛 A股 最新观点 近一周"},
    {"name": "大摩",     "query": "摩根士丹利 A股 最新策略 近一周"},
    {"name": "中信证券", "query": "中信证券 A股 投资策略 近一周"},
    {"name": "郭磊",     "query": "郭磊 A股 最新观点 近一周"},
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
    import requests as _req
    from urllib.parse import quote_plus

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
            print(f"  [百度触发验证码，跳过]")
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
    import time as _time
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


def _is_code_like(name: str) -> bool:
    """判断名称是否为纯代码格式（如 sh600519 / sz399006 / 600519.SH）。"""
    if not name:
        return True
    return bool(re.match(r"^(sh|sz|SH|SZ)\d{6}$", name)) or \
           bool(re.match(r"^\d{6}\.(SH|SZ|sh|sz)$", name)) or \
           bool(re.match(r"^\d{6}$", name))


def _strip_market_prefix(code: str) -> str:
    """去掉市场前缀，返回纯数字代码（如 'sh600519' → '600519'）。"""
    m = re.match(r"^(?:sh|sz|SH|SZ|bj|BJ)(\d{6})$", code)
    return m.group(1) if m else code


def _market_of(code: str) -> str:
    """提取市场码：sh600519 → sh；无市场前缀返回空串。与 _strip_market_prefix 同源正则。"""
    m = re.match(r"^(sh|sz|bj|SH|SZ|BJ)\d{6}$", code)
    return m.group(1).lower() if m else ""


def _batch_stock_names(codes: list[str]) -> dict[str, str]:
    """从 TDengine stock_name 表批量获取股票名称，返回 {normalized_code: name}。

    tdx-cpp v0.13.7 起 stock_name 含 market 列，两市同 code 分别记录
    （000001: sh=上证指数 / sz=平安银行），按 (code, market) 精确匹配，
    避免同 code 异市名字互相覆盖；无市场前缀的 code 回退到任意一行。
    """
    name_map: dict[str, str] = {}
    try:
        from taosws import connect
    except ImportError:
        print("[WARN] taosws 不可用，跳过股票名称查询")
        return name_map

    # 提取纯数字代码去重
    raw_codes = list({_strip_market_prefix(c) for c in codes})

    try:
        conn = connect()
    except Exception:
        print("[WARN] TDengine 连接失败，跳过股票名称查询")
        return name_map

    try:
        # 批量查询
        placeholders = ",".join(f"'{c}'" for c in raw_codes)
        r = conn.query(
            f"select code, name, market from tdx.stock_name "
            f"where code in ({placeholders})"
        )
        precise: dict[tuple[str, str], str] = {}
        by_code: dict[str, str] = {}
        for row in r:
            code, name, market = row[0], row[1], row[2]
            precise[(code, market)] = name
            by_code.setdefault(code, name)  # 无市场前缀时的回退

        # 回填到原始 normalized_code
        for ncode in codes:
            raw = _strip_market_prefix(ncode)
            name = precise.get((raw, _market_of(ncode))) or by_code.get(raw)
            if name:
                name_map[ncode] = name
    finally:
        conn.close()

    return name_map


def demote_headings(content: str, levels: int = 3) -> str:
    """将 markdown 内容中的标题统一降级若干级.

    例如 levels=3 时:  ## 趋势质量评估 → ##### 趋势质量评估
    仅处理 markdown # 标题，不处理 HTML <h1> 等标签。
    """
    extra = "#" * levels

    def _replace(m: re.Match) -> str:
        return extra + m.group(0)

    return re.sub(r"^(#{1,6})\s", _replace, content, flags=re.MULTILINE)


def _shrink_html_table_font(content: str) -> str:
    """给 CZSC HTML 表格加 font-size，使其与 Kronos markdown 表格字体一致。

    WeChat/Web markdown 渲染器对原生 markdown 表格应用紧凑字号，
    但原始 HTML <table> 沿用浏览器大号默认字体，导致视觉不统一。
    """
    return re.sub(
        r'(<table\s[^>]*style=")',
        r'\1font-size:0.92em;',
        content,
    )


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

    # 找到所有 ##/### 区块边界，用于推断 category
    sec_pattern = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)
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

    # 补齐股票名称：有效展示名若是纯代码则通过 TDX 查询
    need_names: list[str] = []
    for code in set(list(kronos_stocks.keys()) + list(czsc_stocks.keys())):
        ks_name = kronos_stocks.get(code, {}).get("name", "")
        cs_name = czsc_stocks.get(code, {}).get("name", "")
        effective = ks_name or cs_name  # 目录/标题中实际展示的名称（Kronos 优先）
        if _is_code_like(effective):
            need_names.append(code)

    if need_names:
        print(f"查询 {len(need_names)} 只股票名称...")
        name_map = _batch_stock_names(need_names)
        for code, stock in kronos_stocks.items():
            if _is_code_like(stock.get("name", "")) and code in name_map:
                stock["name"] = name_map[code]
        for code, stock in czsc_stocks.items():
            if _is_code_like(stock.get("name", "")) and code in name_map:
                stock["name"] = name_map[code]
        resolved = sum(1 for k in need_names if k in name_map)
        print(f"  已解析 {resolved}/{len(need_names)}")
        if resolved < len(need_names):
            missing = [k for k in need_names if k not in name_map]
            print(f"  未解析: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")

    # 盘面分析
    market_analysis = _fetch_market_analysis()

    # 收集所有代码（合并去重，保持 Kronos 顺序优先）
    all_codes = list(kronos_stocks.keys())
    for code in czsc_stocks:
        if code not in all_codes:
            all_codes.append(code)

    # 已知指数代码（纯数字）
    _KNOWN_INDEX_CODES = frozenset({"999999", "399001", "399005", "399006", "000001", "000300", "000016", "000688", "000852", "399303"})

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

        # 指数直接归入指数组（Kronos 标记为"指数" 或 代码本身是已知指数）
        if cat == "指数" or _strip_market_prefix(code) in _KNOWN_INDEX_CODES:
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
                    f.write(demote_headings(_shrink_html_table_font(cs["content"].rstrip())))
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
