#!/usr/bin/env python3
"""
合并 Kronos 预测报告与 CZSC 缠论分析报告。

用法:
    python scripts/merge_kronos_czsc.py [日期]
    python scripts/merge_kronos_czsc.py 20260604          # 指定日期
    python scripts/merge_kronos_czsc.py                    # 默认今天

输入:
    ~/peiking88/Kronos/outputs/kronos_zxg_yyyymmdd.md
    ~/peiking88/czsc/output/czsc_zxg_yyyymmdd.md

输出:
    ~/peiking88/czsc/output/merged_zxg_yyyymmdd.md
"""

import os
import re
import sys
from collections import OrderedDict
from datetime import date


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
    kronos_path = os.path.expanduser(f"~/peiking88/Kronos/outputs/kronos_zxg_{date_str}.md")
    czsc_path = os.path.expanduser(f"~/peiking88/czsc/output/czsc_zxg_{date_str}.md")
    output_path = os.path.expanduser(f"~/peiking88/czsc/output/merged_zxg_{date_str}.md")

    print(f"Kronos: {kronos_path}")
    print(f"CZSC:   {czsc_path}")
    print(f"输出:   {output_path}")
    print()

    kronos_stocks = parse_kronos(kronos_path)
    czsc_stocks = parse_czsc(czsc_path)

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
        f.write(f"# Kronos + 缠论 联合分析报告\n\n")
        f.write(f"**日期**: {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}\n\n")
        f.write(f"**数据来源**: Kronos 价格预测 + CZSC 缠论趋势分析\n\n")
        #        f.write(f"---\n\n")

        # 目录（横向表格排版）
        f.write("## 目录\n\n")
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
                        f'<td style="padding:4px 12px;white-space:nowrap">'
                        f'<a href="#{code}">{name}（{code.upper()}）</a>'
                        f"</td>\n"
                    )
                # 补齐空单元格
                for _ in range(COLS - len(row_codes)):
                    f.write("<td></td>\n")
                f.write("</tr>\n")
            f.write("</table>\n\n")

        #        f.write("---\n\n")

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

                f.write(f'<h3 id="{code}">{stock_count}. {name}（{code.upper()}）</h3>\n\n')

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
