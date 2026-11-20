"""
AI 專用 K 線結構濃縮輸出

產出：output/kline_slices_for_ai.md
  A 區：實戰交易案發現場（平倉時追加，FIFO 最近 25 筆）
  B 區：Top 15 活躍妖幣當下結構快照（每 10 分鐘重刷覆寫）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

import config
from fetcher_and_analyzer import load_klines_from_vault

INTERVAL_MS = {"1m": 60_000, "15m": 900_000}

# A 區持久化（重啟後仍保留最近切片）
CRIME_SCENES_JSON: str = "output/kline_crime_scenes.json"


def _now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _out_path() -> Path:
    return Path(getattr(config, "KLINE_SLICES_FOR_AI_MD", "output/kline_slices_for_ai.md"))


def _scenes_path() -> Path:
    return Path(getattr(config, "KLINE_CRIME_SCENES_JSON", CRIME_SCENES_JSON))


def closed_klines(
    df: pd.DataFrame,
    interval: str,
    now_ms: Optional[int] = None,
) -> pd.DataFrame:
    """排除尚未收盤的最後一根。"""
    try:
        if df is None or df.empty:
            return pd.DataFrame()
        now_ms = now_ms or int(time.time() * 1000)
        interval_ms = INTERVAL_MS.get(interval, 60_000)
        mask = df["open_time"].astype(np.int64) + interval_ms <= now_ms
        return df.loc[mask].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def format_compact_bar(
    row: pd.Series | dict[str, Any],
    vol_ma20: float = 0.0,
    tag: str = "",
) -> str:
    """
    Compact Bar Format:
    時間(HH:MM) | 色(R/G) | 收盤價 | 漲跌% | 振幅% | 上影% | 下影% | 量倍數
    """
    try:
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        v = float(row.get("volume", 0.0) or 0.0)
        ot = int(row["open_time"])
        hhmm = datetime.fromtimestamp(ot / 1000.0).strftime("%H:%M")
        color = "R" if c >= o else "G"
        chg = ((c - o) / o * 100.0) if o > 0 else 0.0
        amp = ((h - l) / o * 100.0) if o > 0 else 0.0
        rng = h - l
        if rng > 0:
            up_pct = int(round((h - max(o, c)) / rng * 100.0))
            lo_pct = int(round((min(o, c) - l) / rng * 100.0))
        else:
            up_pct, lo_pct = 0, 0
        vol_x = (v / vol_ma20) if vol_ma20 > 0 else 0.0
        line = (
            f"{hhmm} | {color} | {c:.8g} | {chg:+.2f}% | {amp:.2f}% | "
            f"{up_pct}% | {lo_pct}% | {vol_x:.1f}x"
        )
        if tag:
            line = f"{line}  {tag}"
        return line
    except Exception:
        return "??:?? | ? | - | - | - | - | - | -x"


def _vol_ma_at(volumes: np.ndarray, idx: int, period: int = 20) -> float:
    if idx <= 0:
        return 0.0
    start = max(0, idx - period)
    window = volumes[start:idx]
    if len(window) == 0:
        return 0.0
    m = float(np.mean(window))
    return m if m > 0 else 0.0


def _near_open_time(ot: int, target: Optional[int], tol_ms: int = 60_000) -> bool:
    """時間對齊容差（預設 ±60s），避免 ENTRY 因對齊偏移漏標。"""
    if target is None:
        return False
    try:
        return abs(int(ot) - int(target)) <= int(tol_ms)
    except Exception:
        return False


def dataframe_to_compact_lines(
    df: pd.DataFrame,
    entry_open_time: Optional[int] = None,
    exit_open_time: Optional[int] = None,
) -> list[str]:
    """將 K 線 DataFrame 轉成 compact 行；可標記 [ENTRY]/[EXIT]。"""
    lines: list[str] = []
    try:
        if df is None or df.empty:
            return lines
        vols = df["volume"].to_numpy(dtype=float)
        entry_marked = False
        exit_marked = False
        # 先找最接近的 ENTRY／EXIT index（容差內取最近一根）
        entry_idx = None
        exit_idx = None
        if entry_open_time is not None:
            best_d = None
            for i, ot in enumerate(df["open_time"].astype(np.int64).tolist()):
                d = abs(int(ot) - int(entry_open_time))
                if d <= 60_000 and (best_d is None or d < best_d):
                    best_d, entry_idx = d, i
        if exit_open_time is not None:
            best_d = None
            for i, ot in enumerate(df["open_time"].astype(np.int64).tolist()):
                d = abs(int(ot) - int(exit_open_time))
                if d <= 60_000 and (best_d is None or d < best_d):
                    best_d, exit_idx = d, i

        for i, (_, row) in enumerate(df.iterrows()):
            tag = ""
            if entry_idx is not None and i == entry_idx and not entry_marked:
                tag = "[ENTRY]"
                entry_marked = True
            if exit_idx is not None and i == exit_idx:
                tag = "[EXIT]" if not tag else "[ENTRY][EXIT]"
                exit_marked = True
            vol_ma = _vol_ma_at(vols, i, 20)
            lines.append(format_compact_bar(row, vol_ma20=vol_ma, tag=tag))

        # 後備：EXIT 漏標時標最後一根；ENTRY 漏標交由呼叫端用持倉首根補標
        if exit_open_time is not None and not exit_marked and lines:
            if "[EXIT]" not in lines[-1]:
                lines[-1] = f"{lines[-1]}  [EXIT]"
    except Exception:
        return lines
    return lines


def _trim_hold_bars(hold_df: pd.DataFrame, max_bars: int = 35) -> pd.DataFrame:
    """
    持倉 1m 上限：<=max_bars 全留；超過則取進場後前 15 + 出場前最後 15。
    """
    try:
        if hold_df is None or hold_df.empty:
            return pd.DataFrame()
        n = len(hold_df)
        if n <= max_bars:
            return hold_df.reset_index(drop=True)
        head = hold_df.head(15)
        tail = hold_df.tail(15)
        out = pd.concat([head, tail], ignore_index=True)
        out = out.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        return out
    except Exception:
        return hold_df.tail(max_bars).reset_index(drop=True) if hold_df is not None else pd.DataFrame()


def slice_last_closed(
    df: pd.DataFrame,
    interval: str,
    n: int,
    before_open_time: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> pd.DataFrame:
    """取已收盤最後 n 根；若給 before_open_time 則只取該根之前。"""
    try:
        closed = closed_klines(df, interval, now_ms=now_ms)
        if closed.empty or n <= 0:
            return pd.DataFrame()
        if before_open_time is not None:
            closed = closed[closed["open_time"].astype(np.int64) < int(before_open_time)]
        if closed.empty:
            return pd.DataFrame()
        return closed.tail(n).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def cache_entry_kline_slices(
    df_1m: Optional[pd.DataFrame],
    df_15m: Optional[pd.DataFrame],
    entry_time_sec: float,
) -> dict[str, Any]:
    """
    開倉當下快取：
      - 進場前 8 根已收盤 15m
      - 進場前 10 根已收盤 1m
      - 進場當根 1m open_time（供 [ENTRY] 標記）
    """
    out: dict[str, Any] = {
        "pre_15m": pd.DataFrame(),
        "pre_1m": pd.DataFrame(),
        "entry_1m_open_time": None,
        "entry_1m_bar": None,
    }
    try:
        src_1m = df_1m if (df_1m is not None and not getattr(df_1m, "empty", True)) else None
        src_15m = df_15m if (df_15m is not None and not getattr(df_15m, "empty", True)) else None
        now_ms = int(entry_time_sec * 1000)

        closed_1m = closed_klines(src_1m, "1m", now_ms=now_ms) if src_1m is not None else pd.DataFrame()
        closed_15m = (
            closed_klines(src_15m, "15m", now_ms=now_ms) if src_15m is not None else pd.DataFrame()
        )

        if not closed_1m.empty:
            entry_bar = closed_1m.iloc[-1]
            entry_ot = int(entry_bar["open_time"])
            out["entry_1m_open_time"] = entry_ot
            out["entry_1m_bar"] = entry_bar.to_dict()
            out["pre_1m"] = slice_last_closed(
                closed_1m, "1m", 10, before_open_time=entry_ot, now_ms=now_ms
            )
        out["pre_15m"] = slice_last_closed(closed_15m, "15m", 8, now_ms=now_ms)
    except Exception:
        return out
    return out


def cache_entry_kline_slices_for_symbol(
    symbol: str,
    df_1m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    entry_time_sec: Optional[float] = None,
) -> dict[str, Any]:
    """開倉快取（含 vault 後備）。失敗回傳空結構，不拋例外。"""
    ts = entry_time_sec if entry_time_sec is not None else time.time()
    try:
        if df_1m is None or getattr(df_1m, "empty", True):
            df_1m = load_klines_from_vault(symbol, "1m")
        if df_15m is None or getattr(df_15m, "empty", True):
            df_15m = load_klines_from_vault(symbol, "15m")
        return cache_entry_kline_slices(df_1m, df_15m, ts)
    except Exception:
        return {
            "pre_15m": pd.DataFrame(),
            "pre_1m": pd.DataFrame(),
            "entry_1m_open_time": None,
            "entry_1m_bar": None,
        }


def _df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    try:
        cols = [c for c in ("open_time", "open", "high", "low", "close", "volume") if c in df.columns]
        return df[cols].to_dict(orient="records")
    except Exception:
        return []


def _records_to_df(records: list[dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    try:
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame()


def build_crime_scene(
    *,
    trade_no: int,
    symbol: str,
    school: str,
    side: str,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    net_pnl: float,
    hold_mins: float,
    entry_time_str: str,
    exit_time_str: str,
    entry_detail: str,
    cached: dict[str, Any],
    entry_time_sec: float,
    exit_time_sec: float,
) -> dict[str, Any]:
    """平倉時組裝案發現場切片（含持倉期 1m，最多 35 根；超限取前15+後15）。"""
    scene: dict[str, Any] = {
        "trade_no": trade_no,
        "symbol": symbol,
        "school": school,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "net_pnl_u": round(float(net_pnl), 6),
        "hold_mins": round(float(hold_mins), 2),
        "entry_time": entry_time_str,
        "exit_time": exit_time_str,
        "entry_signal_detail": entry_detail,
        "pre_15m_lines": [],
        "scene_1m_lines": [],
        "recorded_at": _now_str(),
    }
    try:
        max_hold = int(getattr(config, "KLINE_SCENE_HOLD_MAX_BARS", 35))
        pre_15m = cached.get("pre_15m")
        if isinstance(pre_15m, list):
            pre_15m = _records_to_df(pre_15m)
        elif not isinstance(pre_15m, pd.DataFrame):
            pre_15m = pd.DataFrame()
        pre_1m = cached.get("pre_1m")
        if isinstance(pre_1m, list):
            pre_1m = _records_to_df(pre_1m)
        elif not isinstance(pre_1m, pd.DataFrame):
            pre_1m = pd.DataFrame()

        entry_ot = cached.get("entry_1m_open_time")
        entry_bar_rec = cached.get("entry_1m_bar")
        # 後備：用進場 unix 對齊到 1m open_time（向下取整）
        if entry_ot is None:
            try:
                entry_ot = int(entry_time_sec * 1000) // 60_000 * 60_000
            except Exception:
                entry_ot = None

        # 持倉期間 1m：從進場根到平倉當下
        hold_df = pd.DataFrame()
        try:
            vault_1m = load_klines_from_vault(symbol, "1m")
            closed = closed_klines(vault_1m, "1m", now_ms=int(exit_time_sec * 1000))
            if not closed.empty:
                if entry_ot is not None:
                    # 容差：含進場分鐘前後 1 根，再篩 >= entry
                    lo_ot = int(entry_ot) - 60_000
                    hold_df = closed[
                        closed["open_time"].astype(np.int64) >= lo_ot
                    ].copy()
                    # 對齊後取第一根最接近 entry 起算
                    if not hold_df.empty:
                        ots = hold_df["open_time"].astype(np.int64).to_numpy()
                        # 找最接近 entry_ot 的 index 作為起點
                        nearest = int(np.argmin(np.abs(ots - int(entry_ot))))
                        hold_df = hold_df.iloc[nearest:].reset_index(drop=True)
                else:
                    entry_ms = int(entry_time_sec * 1000)
                    hold_df = closed[
                        closed["open_time"].astype(np.int64) >= entry_ms - 60_000
                    ].copy()
                hold_df = _trim_hold_bars(hold_df, max_bars=max_hold)
        except Exception:
            hold_df = pd.DataFrame()

        # 若 vault 尚未含進場根，用快取的 entry bar 補上
        if isinstance(entry_bar_rec, dict) and entry_bar_rec:
            try:
                eb = pd.DataFrame([entry_bar_rec])
                if hold_df is None or hold_df.empty:
                    hold_df = eb
                else:
                    hold_df = pd.concat([eb, hold_df], ignore_index=True)
                hold_df = hold_df.drop_duplicates(subset=["open_time"]).sort_values(
                    "open_time"
                ).reset_index(drop=True)
                hold_df = _trim_hold_bars(hold_df, max_bars=max_hold)
                if entry_ot is None and "open_time" in entry_bar_rec:
                    entry_ot = int(entry_bar_rec["open_time"])
            except Exception:
                pass

        exit_ot = None
        try:
            if not hold_df.empty:
                exit_ot = int(hold_df.iloc[-1]["open_time"])
                # 持倉首根作為 ENTRY 對齊後備
                if entry_ot is None:
                    entry_ot = int(hold_df.iloc[0]["open_time"])
        except Exception:
            exit_ot = None

        # 1m 完整序列：進場前 10 + 持倉（去重）
        try:
            parts = [pre_1m, hold_df]
            parts = [p for p in parts if p is not None and not getattr(p, "empty", True)]
            if parts:
                scene_1m = pd.concat(parts, ignore_index=True)
                scene_1m = (
                    scene_1m.drop_duplicates(subset=["open_time"])
                    .sort_values("open_time")
                    .reset_index(drop=True)
                )
            else:
                scene_1m = pd.DataFrame()
        except Exception:
            scene_1m = hold_df if hold_df is not None else pd.DataFrame()

        scene["pre_15m_lines"] = dataframe_to_compact_lines(pre_15m)
        scene_lines = dataframe_to_compact_lines(
            scene_1m,
            entry_open_time=int(entry_ot) if entry_ot is not None else None,
            exit_open_time=int(exit_ot) if exit_ot is not None else None,
        )
        # ENTRY 仍漏標：在持倉首根對應行強制標記
        if entry_ot is not None and scene_lines and not any("[ENTRY]" in x for x in scene_lines):
            try:
                ots = scene_1m["open_time"].astype(np.int64).tolist() if not scene_1m.empty else []
                best_i, best_d = None, None
                for i, ot in enumerate(ots):
                    d = abs(int(ot) - int(entry_ot))
                    if best_d is None or d < best_d:
                        best_d, best_i = d, i
                if best_i is not None and best_i < len(scene_lines):
                    if "[ENTRY]" not in scene_lines[best_i]:
                        scene_lines[best_i] = f"{scene_lines[best_i]}  [ENTRY]"
            except Exception:
                pass
        scene["scene_1m_lines"] = scene_lines
        scene["_pre_15m_records"] = _df_to_records(pre_15m)
        scene["_scene_1m_meta"] = {
            "entry_open_time": entry_ot,
            "exit_open_time": exit_ot,
        }
    except Exception:
        pass
    return scene


def load_crime_scenes() -> list[dict[str, Any]]:
    path = _scenes_path()
    try:
        if path.exists() and path.stat().st_size > 0:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data[-25:]
    except Exception:
        pass
    return []


def save_crime_scenes(scenes: list[dict[str, Any]]) -> None:
    path = _scenes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = scenes[-25:]
        # 去掉不可 JSON 序列化殘留
        clean: list[dict[str, Any]] = []
        for s in trimmed:
            item = {k: v for k, v in s.items() if not k.startswith("_") or k in ("_pre_15m_records", "_scene_1m_meta")}
            # 確保 lines 存在
            if "pre_15m_lines" not in item:
                item["pre_15m_lines"] = []
            if "scene_1m_lines" not in item:
                item["scene_1m_lines"] = []
            clean.append(item)
        path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def append_crime_scene(scene: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = load_crime_scenes()
    scenes.append(scene)
    scenes = scenes[-25:]
    save_crime_scenes(scenes)
    return scenes


def select_top15_active(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    優先 ROUTED／tradeable，不足 15 再以 |24h漲跌幅| 補 WATCH_ONLY。
    """
    try:
        routed = [
            p
            for p in plans
            if p.get("status") == "ROUTED" or p.get("tradeable") is True
        ]
        # 穩定：可交易先依 |漲跌幅| 大到小
        routed.sort(
            key=lambda p: abs(float(p.get("price_change_pct_24h") or 0.0)),
            reverse=True,
        )
        picked = routed[:15]
        if len(picked) >= 15:
            return picked

        have = {p.get("symbol") for p in picked}
        watch = [
            p
            for p in plans
            if p.get("symbol") not in have
            and (
                p.get("status") in ("WATCH_ONLY", "WATCH")
                or str(p.get("primary_school", "")).upper() == "WATCH_ONLY"
            )
        ]
        watch.sort(
            key=lambda p: abs(float(p.get("price_change_pct_24h") or 0.0)),
            reverse=True,
        )
        for p in watch:
            if len(picked) >= 15:
                break
            picked.append(p)

        # 仍不足：其餘池子依 |漲跌幅| 補齊
        if len(picked) < 15:
            have = {p.get("symbol") for p in picked}
            rest = [p for p in plans if p.get("symbol") not in have]
            rest.sort(
                key=lambda p: abs(float(p.get("price_change_pct_24h") or 0.0)),
                reverse=True,
            )
            for p in rest:
                if len(picked) >= 15:
                    break
                picked.append(p)
        return picked
    except Exception:
        return []


def build_zone_b_snapshot(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """從 vault 讀 Top15 的 16×15m + 20×1m compact 快照。"""
    snaps: list[dict[str, Any]] = []
    for p in select_top15_active(plans):
        sym = str(p.get("symbol") or "")
        if not sym:
            continue
        item: dict[str, Any] = {
            "symbol": sym,
            "status": p.get("status", ""),
            "primary_school": p.get("primary_school", ""),
            "board": p.get("board", ""),
            "price_change_pct_24h": p.get("price_change_pct_24h", 0.0),
            "lines_15m": [],
            "lines_1m": [],
            "error": "",
        }
        try:
            df15 = load_klines_from_vault(sym, "15m")
            df1 = load_klines_from_vault(sym, "1m")
            last15 = slice_last_closed(df15, "15m", 16)
            last1 = slice_last_closed(df1, "1m", 20)
            item["lines_15m"] = dataframe_to_compact_lines(last15)
            item["lines_1m"] = dataframe_to_compact_lines(last1)
            if not item["lines_15m"] and not item["lines_1m"]:
                item["error"] = "K線不足或 vault 無資料"
        except Exception as exc:
            item["error"] = f"讀取失敗：{exc}"
        snaps.append(item)
    return snaps


def _render_zone_a(scenes: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    lines.append("## A 區｜實戰交易案發現場 K 線切片（最近 25 筆 FIFO）")
    lines.append("")
    lines.append(
        "> 格式：`HH:MM | 色(R紅漲/G綠跌) | 收盤 | 漲跌% | 振幅% | 上影% | 下影% | 量倍數`；"
        "`[ENTRY]`=訊號/成交所屬 **1m open_time 區間**（非保證等於實際事件牆鐘秒）；"
        "`[EXIT]`=出場觀測所屬 K 線區間。完整未截斷序列見 `output/kline_archives/`。"
    )
    lines.append("")
    if not scenes:
        lines.append("_尚無已平倉切片（下一筆平倉後會自動寫入）。_")
        lines.append("")
        return lines

    # 新→舊 顯示
    for sc in reversed(scenes[-25:]):
        try:
            pnl = float(sc.get("net_pnl_u") or 0.0)
            sign = "+" if pnl >= 0 else ""
            lines.append(
                f"### #{sc.get('trade_no', '?')} {sc.get('symbol')} "
                f"{sc.get('side')}｜{sc.get('school')}｜{sc.get('exit_reason')}｜"
                f"Net {sign}{pnl:.4f}U｜持倉 {sc.get('hold_mins')}m"
            )
            lines.append(
                f"- 進場 {sc.get('entry_time')} @ {sc.get('entry_price')} → "
                f"出場 {sc.get('exit_time')} @ {sc.get('exit_price')}"
            )
            detail = sc.get("entry_signal_detail") or "-"
            lines.append(f"- 訊號：{detail}")
            lines.append("")
            lines.append("#### 15m 大結構（進場前 ≤8 根）")
            lines.append("```")
            pre15 = sc.get("pre_15m_lines") or []
            lines.extend(pre15 if pre15 else ["(無資料)"])
            lines.append("```")
            lines.append("")
            lines.append("#### 1m 案發現場（進場前 ≤10 + 持倉 ≤35，含 ENTRY/EXIT）")
            lines.append("```")
            sc1 = sc.get("scene_1m_lines") or []
            lines.extend(sc1 if sc1 else ["(無資料)"])
            lines.append("```")
            lines.append("")
        except Exception:
            continue
    return lines


def _render_zone_b(snaps: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    lines.append("## B 區｜Top 15 活躍妖幣當下 K 線結構快照（每 10 分鐘覆寫）")
    lines.append("")
    lines.append(
        "> 每幣：最近 16 根 15m（約 4h）+ 最近 20 根 1m（約 20m）。"
        "優先 ROUTED，不足以 |24h漲跌幅| 補 WATCH_ONLY。"
    )
    lines.append("")
    if not snaps:
        lines.append("_尚無快照（下一輪 10 分鐘重刷後寫入）。_")
        lines.append("")
        return lines

    for i, sn in enumerate(snaps, start=1):
        try:
            chg = float(sn.get("price_change_pct_24h") or 0.0)
            lines.append(
                f"### {i}. {sn.get('symbol')}｜{sn.get('status')}｜"
                f"主門派 {sn.get('primary_school')}｜24h {chg:+.2f}%｜"
                f"{sn.get('board')}"
            )
            if sn.get("error"):
                lines.append(f"- 警告：{sn['error']}")
            lines.append("")
            lines.append("#### 15m × 16")
            lines.append("```")
            lines.extend(sn.get("lines_15m") or ["(無資料)"])
            lines.append("```")
            lines.append("")
            lines.append("#### 1m × 20")
            lines.append("```")
            lines.extend(sn.get("lines_1m") or ["(無資料)"])
            lines.append("```")
            lines.append("")
        except Exception:
            continue
    return lines


def write_kline_slices_md(
    *,
    scenes: Optional[list[dict[str, Any]]] = None,
    zone_b: Optional[list[dict[str, Any]]] = None,
    note: str = "",
) -> Path:
    """重新生成並覆寫 kline_slices_for_ai.md。永不拋例外影響主迴圈。"""
    path = _out_path()
    try:
        if scenes is None:
            scenes = load_crime_scenes()
        if zone_b is None:
            # 嘗試讀上次 B 區快照快取
            zone_b = _load_zone_b_cache()

        lines: list[str] = []
        lines.append("# K 線結構濃縮切片（供 AI 讀取）")
        lines.append("")
        lines.append(f"> 更新時間：{_now_str()}")
        if note:
            lines.append(f"> 觸發：{note}")
        lines.append(
            "> Compact：`時間 | 色(R/G) | 收盤 | 漲跌%((C-O)/O) | 振幅%((H-L)/O) | 上影% | 下影% | 量倍數(量/20MA)`"
        )
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.extend(_render_zone_a(scenes or []))
        lines.append("---")
        lines.append("")
        lines.extend(_render_zone_b(zone_b or []))

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# K 線結構濃縮切片\n\n寫入失敗於 {_now_str()}（主交易迴圈不受影響）。\n",
                encoding="utf-8",
            )
        except Exception:
            pass
    return path


_ZONE_B_CACHE: list[dict[str, Any]] = []
_ZONE_B_JSON = "output/kline_zone_b_cache.json"


def _load_zone_b_cache() -> list[dict[str, Any]]:
    global _ZONE_B_CACHE
    if _ZONE_B_CACHE:
        return _ZONE_B_CACHE
    try:
        p = Path(_ZONE_B_JSON)
        if p.exists() and p.stat().st_size > 0:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _ZONE_B_CACHE = data
                return _ZONE_B_CACHE
    except Exception:
        pass
    return []


def _save_zone_b_cache(snaps: list[dict[str, Any]]) -> None:
    global _ZONE_B_CACHE
    _ZONE_B_CACHE = snaps
    try:
        p = Path(_ZONE_B_JSON)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snaps, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def update_zone_b_and_write(plans: list[dict[str, Any]], note: str = "10分鐘重刷") -> Path:
    """重刷後更新 B 區並覆寫 MD。"""
    try:
        snaps = build_zone_b_snapshot(plans)
        _save_zone_b_cache(snaps)
        return write_kline_slices_md(zone_b=snaps, note=note)
    except Exception:
        return write_kline_slices_md(note=f"{note}（B區更新失敗）")


def record_close_and_write(
    *,
    trade_no: int,
    symbol: str,
    school: str,
    side: str,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    net_pnl: float,
    hold_mins: float,
    entry_time_str: str,
    exit_time_str: str,
    entry_detail: str,
    kline_cache: dict[str, Any],
    entry_time_sec: float,
    exit_time_sec: float,
) -> Path:
    """平倉後寫入 A 區並覆寫 MD。"""
    try:
        cached = dict(kline_cache or {})
        # Position 內可能存 DataFrame；轉 records 以便組裝
        if isinstance(cached.get("pre_15m"), pd.DataFrame):
            cached["pre_15m"] = cached["pre_15m"]
        if isinstance(cached.get("pre_1m"), pd.DataFrame):
            cached["pre_1m"] = cached["pre_1m"]
        scene = build_crime_scene(
            trade_no=trade_no,
            symbol=symbol,
            school=school,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            net_pnl=net_pnl,
            hold_mins=hold_mins,
            entry_time_str=entry_time_str,
            exit_time_str=exit_time_str,
            entry_detail=entry_detail,
            cached=cached,
            entry_time_sec=entry_time_sec,
            exit_time_sec=exit_time_sec,
        )
        scenes = append_crime_scene(scene)
        return write_kline_slices_md(
            scenes=scenes,
            note=f"平倉 #{trade_no} {symbol} {exit_reason}",
        )
    except Exception:
        return write_kline_slices_md(note=f"平倉寫入失敗 {symbol}")
