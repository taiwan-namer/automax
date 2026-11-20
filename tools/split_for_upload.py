#!/usr/bin/env python3
"""把長文字切成多份（每份字元數 ≤ 上限），方便貼到或上傳到有字數限制的聊天網頁。

用法：
  python3 tools/split_for_upload.py 檔案.md               # 切檔，輸出到 檔案_upload_parts/
  python3 tools/split_for_upload.py --clipboard            # 切剪貼簿內容
  python3 tools/split_for_upload.py 檔案.md --copy         # 切完後逐份複製到剪貼簿，按 Enter 換下一份
  選項：--max-chars 15000（每份上限，含標頭）  --max-files 5（超過時警告）  --out 目錄

切點優先順序：章節標題(#/##/###) > 空行 > 換行 > 硬切；不在 ``` 程式碼區塊內切。
原檔不會被修改；輸出目錄已存在時自動加時間戳。
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HEADER_RESERVE = 200


def header(i: int, n: int, src: str, a: int, b: int, total: int) -> str:
    return (f"【第 {i}/{n} 份｜來源：{src}｜原文字元 {a + 1}–{b}／共 {total}】\n"
            f"（共 {n} 份，請全部收到後再一起分析）\n\n")


def boundaries(text: str) -> dict[int, int]:
    """可切位置 -> 優先度（數字越小越好），位置為下一份的起點。"""
    out: dict[int, int] = {}
    in_fence = False
    pos = 0
    prev_blank = False
    for line in text.splitlines(keepends=True):
        if not in_fence and pos > 0:
            if re.match(r"#{1,2} ", line):
                out[pos] = 0
            elif line.startswith("### "):
                out[pos] = 1
            elif prev_blank:
                out[pos] = 2
            else:
                out[pos] = 3
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        prev_blank = line.strip() == ""
        pos += len(line)
    return out


def split_text(text: str, max_chars: int) -> list[tuple[int, int]]:
    body_max = max_chars - HEADER_RESERVE
    total = len(text)
    if total <= body_max:
        return [(0, total)]
    bnd = boundaries(text)
    pts = sorted(bnd)
    n_target = math.ceil(total / body_max)
    spans: list[tuple[int, int]] = []
    start = 0
    while total - start > body_max:
        remaining_parts = max(n_target - len(spans), 2)
        target = start + (total - start) / remaining_parts
        hi = start + body_max
        best = None
        for p in pts:
            if p <= start or p > hi:
                continue
            # 分數：優先度為主，離平均目標近為輔
            score = bnd[p] * body_max + abs(p - target)
            if best is None or score < best[0]:
                best = (score, p)
        cut = best[1] if best else hi
        spans.append((start, cut))
        start = cut
    spans.append((start, total))
    return spans


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--clipboard", action="store_true")
    ap.add_argument("--max-chars", type=int, default=15000)
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--copy", action="store_true")
    a = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    if a.clipboard:
        text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        src_name = f"clipboard_{stamp}"
        out = Path(a.out) if a.out else Path("output/upload_parts") / src_name
    elif a.file:
        src = Path(a.file)
        text = src.read_text(encoding="utf-8")
        src_name = src.name
        out = Path(a.out) if a.out else src.with_name(src.stem + "_upload_parts")
    else:
        ap.error("請給檔案路徑或 --clipboard")
    if not text.strip():
        sys.exit("內容是空的")
    if out.exists():
        out = out.with_name(out.name + "_" + stamp)
    out.mkdir(parents=True)

    spans = split_text(text, a.max_chars)
    n = len(spans)
    parts = []
    for i, (s, e) in enumerate(spans, 1):
        body = header(i, n, src_name, s, e, len(text)) + text[s:e] if n > 1 else text
        p = out / f"part{i}of{n}.md"
        p.write_text(body, encoding="utf-8")
        parts.append((p, body))
        print(f"{p}  {len(body):,} 字元")
    print(f"共 {len(text):,} 字元 → {n} 份（每份上限 {a.max_chars:,}）")
    if n > a.max_files:
        print(f"⚠ 份數 {n} 超過 {a.max_files}，請分批上傳或提高 --max-chars")
    if a.copy:
        for i, (p, body) in enumerate(parts, 1):
            subprocess.run(["pbcopy"], input=body, text=True)
            input(f"已複製第 {i}/{n} 份到剪貼簿，貼上後按 Enter 繼續…" if i < n else f"已複製第 {i}/{n} 份（最後一份），按 Enter 結束")


if __name__ == "__main__":
    main()
