#!/usr/bin/env python
"""合并缠论趋势评估报告与 Kronos 价格预测报告

用法:
  # 自动检测 output/ 下最新文件
  uv run python scripts/merge_reports.py

  # 手动指定日期
  uv run python scripts/merge_reports.py -d 20260523

  # 手动指定文件
  uv run python scripts/merge_reports.py --czsc output/czsc_zxg_20260522.md --kronos output/kronos_zxg_20260523.md

输出:
  output/zxg_yyyymmdd.md
"""

import argparse
import re
from datetime import date
from pathlib import Path


def parse_czsc(filepath: str) -> dict[str, str]:
    """解析缠论报告，返回 {normalized_code: section_content}

    支持两种标题格式：
      # 名称（600000.SH） 缠论趋势预测
      <h1>★ 名称（600000.SH） 缠论趋势预测</h1>
    """
    text = Path(filepath).read_text(encoding="utf-8")

    # 将 <h1> 格式归一化为 markdown
    text = re.sub(r"<h1>(.+?)</h1>", r"# \1", text)

    sections = {}
    pattern = (
        r"(^# .+?[（(]\d{6}\.(?:SZ|SH)[）)]"
        r".*?(?=^# .+?[（(]\d{6}\.(?:SZ|SH)[）)]|\Z))"
    )
    for m in re.finditer(pattern, text, re.MULTILINE | re.DOTALL):
        content = m.group(1).strip()
        hm = re.match(r"^# .+?[（(](\d{6})\.(SZ|SH)[）)]", content)
        if hm:
            prefix = "sh" if hm.group(2) == "SH" else "sz"
            sections[f"{prefix}{hm.group(1)}"] = content
    return sections


def parse_kronos(filepath: str) -> dict:
    """解析 Kronos 报告，返回 {normalized_code: (name, category, section_content)}"""
    text = Path(filepath).read_text(encoding="utf-8")
    stock_list = []

    # 确定每只标的所属分类
    sec_pattern = r"^(### .+)$"
    sec_matches = list(re.finditer(sec_pattern, text, re.MULTILINE))
    categories = {}
    for i, m in enumerate(sec_matches):
        cat_name = m.group(1).replace("### ", "")
        start = m.end()
        end = sec_matches[i + 1].start() if i + 1 < len(sec_matches) else len(text)
        for sm in re.finditer(r"^#### (.+?) \((s[hz]\d+)\)", text[start:end], re.MULTILINE):
            categories[sm.group(2)] = cat_name

    # 截断尾部段落（预测稳定性告警、错误等），只保留标的预测内容
    tail = re.search(r"\n## 二、|\n## 错误", text)
    body_text = text[: tail.start()] if tail else text

    # 按出现顺序解析
    parts = re.split(r"^(?=#### )", body_text, flags=re.MULTILINE)
    for part in parts[1:]:
        part = part.strip()
        hm = re.match(r"^#### (.+?) \((s[hz]\d+)\)", part)
        if hm:
            stock_list.append((hm.group(2), hm.group(1), categories.get(hm.group(2), ""), part))

    return stock_list


def extract_preamble(filepath: str) -> str:
    """提取 Kronos 报告的第一段（#### 之前的内容）"""
    text = Path(filepath).read_text(encoding="utf-8")
    first = re.search(r"^#### ", text, re.MULTILINE)
    return text[: first.start()].strip() if first else ""


def check_direction_align(czsc_body: str, kronos_body: str) -> bool:
    """判断缠论 1d 方向与 Kronos 3日预测方向是否一致"""
    # 从 CZSC 表格中提取 1d 未完成笔方向
    czsc_dir = None
    ubi_match = re.search(r"<td>未完成笔.*?</td>\s*<td>(.*?)</td>", czsc_body, re.DOTALL)
    if ubi_match:
        cell = ubi_match.group(1)
        if "↑" in cell:
            czsc_dir = "up"
        elif "↓" in cell:
            czsc_dir = "down"

    # 从 Kronos 提取 3日涨跌方向
    kronos_dir = None
    pct_match = re.search(r"3日涨跌:\s*\*{0,2}([+-]?\d+\.?\d*)%", kronos_body)
    if pct_match:
        pct = float(pct_match.group(1))
        if pct > 0:
            kronos_dir = "up"
        elif pct < 0:
            kronos_dir = "down"

    if czsc_dir and kronos_dir and czsc_dir == kronos_dir:
        return True
    return False


def merge(czsc_path: str, kronos_path: str, output_path: str) -> str:
    """合并两份报告，返回输出路径"""
    czsc = parse_czsc(czsc_path)
    kronos = parse_kronos(kronos_path)

    czsc_set = set(czsc)
    common = czsc_set & {k[0] for k in kronos}

    # 先构建标的内容，同时统计方向一致数
    align_count = 0
    align_total = 0
    stock_blocks = []

    current_cat = None
    for kcode, name, cat, ksection in kronos:
        czsc_raw = czsc.get(kcode, "")
        kronos_body = re.sub(r"^#### .+$\n?", "", ksection, count=1, flags=re.MULTILINE).strip()
        kronos_body = re.sub(r"^### .+$\n?", "", kronos_body, flags=re.MULTILINE).strip()

        # 方向一致检测
        aligned = False
        if czsc_raw:
            align_total += 1
            aligned = check_direction_align(czsc_raw, kronos_body)
            if aligned:
                align_count += 1

        # 拼接单只标的 block
        block = []
        if cat and cat != current_cat:
            current_cat = cat
            block.append(f"## {current_cat}")
            block.append("")

        suffix = " ★" if aligned else ""
        block.append(f"### {name}（{kcode}）{suffix}")
        block.append("")

        # Kronos 预测
        block.append(kronos_body)
        block.append("")

        # 缠论分析
        if czsc_raw:
            body = re.sub(r"^# .+$\n?", "", czsc_raw, count=1, flags=re.MULTILINE).strip()
            body = re.sub(r"\n+---\s*$", "", body)
            body = re.sub(r"^## ", "##### ", body, flags=re.MULTILINE)
            body = re.sub(r"^##### 趋势质量评估\n*", "", body, flags=re.MULTILINE)
            block.append(body)
            block.append("")

        block.append("---")
        block.append("")
        stock_blocks.append(block)

    # 组装头部
    pct = f"{align_count / align_total * 100:.0f}%" if align_total else "-"
    lines = []
    lines.append("# 自选股综合分析报告")
    lines.append("")
    lines.append(
        f"**缠论分析**: {Path(czsc_path).stem.replace('czsc_zxg_', '')}"
        f" | **Kronos预测**: {Path(kronos_path).stem.replace('kronos_zxg_', '')}"
    )
    lines.append(f"**标的数**: {len(kronos)} | 含缠论分析: {len(common)} | 仅预测: {len(kronos) - len(common)}")
    lines.append(f"**方向一致** (缠论1d未完成笔 vs Kronos 3日涨跌): {align_count}/{align_total} ({pct})")
    lines.append("**模型**: Kronos-base TDX后复权微调版 + 缠论分型/笔/线段趋势评估")
    lines.append("")
    lines.append("> Kronos 3日价格预测 + 缠论趋势评估（日线/30分钟/5分钟）。")
    lines.append("> ★ 标记表示缠论 1d 未完成笔方向与 Kronos 3日涨跌方向一致。")
    lines.append("> 分类标准：3日累计涨跌幅 >3% 为看涨，<-3% 为看跌，其余为看平。")
    lines.append("")
    lines.append("---")
    lines.append("")

    for block in stock_blocks:
        lines.extend(block)

    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    return output_path


def auto_detect() -> tuple[str, str, str]:
    """自动检测 output/ 下最新的 CZSC 和 Kronos 文件"""
    output_dir = Path("output")
    if not output_dir.exists():
        raise FileNotFoundError("output/ 目录不存在")

    czsc_files = sorted(output_dir.glob("czsc_zxg_*.md"), reverse=True)
    kronos_files = sorted(output_dir.glob("kronos_zxg_*.md"), reverse=True)

    if not czsc_files:
        raise FileNotFoundError("未找到 czsc_zxg_*.md 文件")
    if not kronos_files:
        raise FileNotFoundError("未找到 kronos_zxg_*.md 文件")

    czsc_path = str(czsc_files[0])
    kronos_path = str(kronos_files[0])

    # 从 Kronos 文件名提取日期作为输出日期
    date_match = re.search(r"(\d{8})", kronos_files[0].stem)
    output_date = date_match.group(1) if date_match else date.today().strftime("%Y%m%d")
    output_path = str(output_dir / f"zxg_{output_date}.md")

    return czsc_path, kronos_path, output_path


def main():
    p = argparse.ArgumentParser(description="合并缠论趋势评估与 Kronos 价格预测报告")
    p.add_argument("--czsc", help="CZSC 报告路径（自动检测）")
    p.add_argument("--kronos", help="Kronos 报告路径（自动检测）")
    p.add_argument("-o", "--output", help="输出路径（自动生成）")
    p.add_argument("-d", "--date", help="日期 yyyymmdd，从该日的报告合并")
    args = p.parse_args()

    if args.date:
        czsc_path = f"output/czsc_zxg_{args.date}.md"
        kronos_path = f"output/kronos_zxg_{args.date}.md"
        output_path = f"output/zxg_{args.date}.md"
    elif args.czsc and args.kronos:
        czsc_path = args.czsc
        kronos_path = args.kronos
        output_path = args.output or czsc_path.replace("czsc_zxg_", "zxg_").replace("czsc_", "zxg_")
    else:
        czsc_path, kronos_path, output_path = auto_detect()

    print(f"CZSC : {czsc_path}")
    print(f"Kronos: {kronos_path}")

    out = merge(czsc_path, kronos_path, output_path)
    print(f"合并完成: {out}")


if __name__ == "__main__":
    main()
