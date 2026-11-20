"""
幣安 USDT 永續合約・當紅小幣股性解剖器 — 全域設定

核心邏輯：抓取當下 24h 漲幅/跌幅各 Top 25（共 50 支純加密小幣），
僅做客觀莊家行為特徵提取，不預設任何交易策略。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# K 線回看區間（分週期鎖定長度）
# ---------------------------------------------------------------------------
# 相容舊參數（部分報表仍引用）
LOOKBACK_DAYS: int = 7

# 15m：波段股性 → 過去 7 天（約 672 根），最多保留 1000 根
LOOKBACK_15M_DAYS: int = 7
LOOKBACK_15M_BARS: int = 672
LOOKBACK_15M_MAX_BARS: int = 1000

# 1m：極短線插針／爆量 → 過去 24 小時（1440 根）
LOOKBACK_1M_HOURS: int = 24
LOOKBACK_1M_BARS: int = 1440

# 股性歷史快照：漲跌幅變動門檻／最小寫入間隔
PROFILE_HISTORY_CHANGE_PCT: float = 5.0  # 24h 漲跌幅變動超過 5% 才記歷史
PROFILE_HISTORY_MIN_HOURS: float = 4.0  # 距離該幣上次寫入超過 4 小時才記歷史

# ---------------------------------------------------------------------------
# 漲跌榜取樣（壓力測試擴編：Top 50）
# ---------------------------------------------------------------------------
TOP_GAINERS: int = 25
TOP_LOSERS: int = 25

# 24h 成交額下限（USDT）：排除極冷門／流動性枯竭幣
MIN_QUOTE_VOLUME_USDT: float = 5_000_000  # 500 萬

# ---------------------------------------------------------------------------
# 純加密貨幣篩選
# ---------------------------------------------------------------------------
# underlyingType 必須為此值（排除 INDEX / PREMARKET 等）
REQUIRED_UNDERLYING_TYPE: str = "COIN"

# underlyingSubType 若包含以下關鍵字 → 非純加密，排除
NON_CRYPTO_SUBTYPE_KEYWORDS: frozenset[str] = frozenset(
    {
        "TradFi",
        "Commodity",
        "Equity",
        "Stock",
        "Forex",
        "Index",
        "Pre-Market",
    }
)

# 美股／TradFi／大宗商品／外匯等 baseAsset 黑名單（精確比對，避免誤殺 FARTCOIN 等）
TRADFI_BASE_BLACKLIST: frozenset[str] = frozenset(
    {
        "TSLA",
        "AAPL",
        "NVDA",
        "MSFT",
        "GOOG",
        "GOOGL",
        "AMZN",
        "META",
        "COIN",
        "MSTR",
        "SPY",
        "QQQ",
        "XAU",
        "XAG",
        "PAXG",
        "XAUT",
        "EUR",
        "GBP",
        "USD",
        "JPY",
        "AUD",
        "NZD",
        "CAD",
        "CHF",
    }
)

# 另以「符號前綴／包含」排除的傳統資產關鍵字（處理 XAUUSDT、EURUSDT 等）
TRADFI_SYMBOL_PREFIXES: tuple[str, ...] = (
    "XAU",
    "XAG",
    "PAXG",
    "XAUT",
    "EUR",
    "GBP",
)

# 主流大幣（正規化後的 base，含 1000BTC → BTC）一律排除
EXCLUDED_MAJOR_BASES: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "BNB",
        "SOL",
        "XRP",
        "DOGE",
        "ADA",
        "TRX",
        "AVAX",
        "LINK",
        "DOT",
        "LTC",
        "BCH",
        "UNI",
        "SUI",
        "TON",
    }
)

# ---------------------------------------------------------------------------
# 特徵計算參數
# ---------------------------------------------------------------------------
VOLUME_MA_PERIOD: int = 20
VOLUME_SPIKE_MULTIPLIER: float = 5.0
SPIKE_HORIZONS_1M: tuple[int, ...] = (3, 5, 15)

# 行情集中度：過去 24h 的 15m K 線根數
PULSE_LOOKBACK_15M_BARS: int = 96  # 24h / 15m
PULSE_TOP_N: int = 5

# 長針：影線 > 實體 × 倍數
LONG_WICK_BODY_MULTIPLIER: float = 2.0

# 量能比：近 2 小時 vs 過去 24 小時（以 1m 均量）
RECENT_VOLUME_BARS_1M: int = 120  # 2h
BASELINE_VOLUME_BARS_1M: int = 1440  # 24h

# 連續順勢統計
STREAK_LENGTHS: tuple[int, ...] = (3, 4)

# ---------------------------------------------------------------------------
# 並行與 API
# ---------------------------------------------------------------------------
CONCURRENT_SYMBOLS: int = 3
BINANCE_FUTURES_BASE_URL: str = "https://fapi.binance.com"
KLINES_LIMIT: int = 1500
REQUEST_DELAY_SEC: float = 0.12

# ---------------------------------------------------------------------------
# 策略分派（Strategy Router）門檻
# ---------------------------------------------------------------------------
SKIP_VOLUME_RATIO_LT: float = 0.35  # 早盤量縮放寬（原 0.65）
SKIP_QUOTE_VOLUME_WAN_LT: float = 500.0  # 500 萬 U（與量能比併用；放寬中後段幣進場）

# 防軋空／強勢幣（FADE 禁空另有更嚴門檻）
ANTI_SQUEEZE_CHG_PCT: float = 50.0
FADE_BAN_SHORT_CHG_PCT: float = 25.0  # change_24h > 25% 且位置>80% 禁 FADE 做空
FADE_BAN_SHORT_POS_PCT: float = 80.0

# 強勢帶量突破 → 強制 MOMO 主 + TREND 副（修正 WATCH_ONLY 誤判）
FORCE_MOMO_VOLUME_RATIO_GE: float = 1.5
FORCE_MOMO_POS_HIGH_GE: float = 70.0
FORCE_MOMO_POS_LOW_LE: float = 30.0

# 門派 1：15m 連四反轉
MEAN_REV_STREAK4_REV_RATE_GE: float = 60.0
MEAN_REV_STREAK4_REV_MOVE_GE: float = 0.8
MEAN_REV_TP_PCT: float = 2.80
MEAN_REV_SL_PCT: float = 1.40
MEAN_REV_MAX_HOLD_MINS: int = 60

# 門派 2：1m 插針反轉（極短線快進快出）
SPIKE_FADE_REV_RATE_15M_GE: float = 65.0
SPIKE_FADE_PULSE_GE: float = 0.30
SPIKE_FADE_REV_RATE_5M_GE: float = 54.0
SPIKE_FADE_TP_DEFAULT: float = 0.95
SPIKE_FADE_TP_MIN: float = 0.85
SPIKE_FADE_TP_MAX: float = 1.10
SPIKE_FADE_SL_DEFAULT: float = 0.70
SPIKE_FADE_SL_MIN: float = 0.65
SPIKE_FADE_SL_MAX: float = 0.80
SPIKE_FADE_WICK_MIN: float = 0.40  # 影線佔比硬門檻
SPIKE_FADE_MAX_HOLD_MINS: int = 12
SPIKE_FADE_VOL_X_MIN: float = 5.0
SPIKE_FADE_VOL_X_MAX: float = 18.0  # 過濾 26x/310x 失真針
# FADE 專屬提早保本
SPIKE_FADE_BE1_MFE: float = 0.55
SPIKE_FADE_BE1_LOCK: float = 0.15
SPIKE_FADE_BE2_MFE: float = 0.80
SPIKE_FADE_BE2_LOCK: float = 0.45

# 門派 3：1m 順勢突破
SPIKE_MOMO_CONT_RATE_5M_GE: float = 56.0  # 5m 延續率
SPIKE_MOMO_REV_RATE_15M_LE: float = 48.0  # 15m 騙炮率上限
SPIKE_MOMO_VOLUME_RATIO_GE: float = 0.8
SPIKE_MOMO_PRICE_POS_LT: float = 92.0
SPIKE_MOMO_VOL_X_MIN: float = 5.0
SPIKE_MOMO_VOL_X_MAX: float = 18.0
SPIKE_MOMO_BODY_MIN: float = 0.65  # 實體佔比
SPIKE_MOMO_TP_PCT: float = 1.80
SPIKE_MOMO_SL_PCT: float = 0.85
SPIKE_MOMO_MAX_HOLD_MINS: int = 20
SPIKE_MOMO_STALL_MINS: float = 4.0
SPIKE_MOMO_STALL_MFE_LT: float = 0.30
# MOMO 專屬保本／移動鎖利
SPIKE_MOMO_BE_MFE: float = 0.90
SPIKE_MOMO_BE_LOCK: float = 0.20
SPIKE_MOMO_TRAIL_MFE: float = 1.50
SPIKE_MOMO_TRAIL_GIVEBACK: float = 0.40

# 門派 4：15m 順勢追擊
TREND_STREAK3_CONT_RATE_GE: float = 55.0
TREND_VOLUME_RATIO_GE: float = 1.2
TREND_TP_PCT: float = 2.80
TREND_SL_PCT: float = 1.40
TREND_MAX_HOLD_MINS: int = 60

# 門派 5：SWING_REV — 暫時封印（不再分派／不開新倉）
SWING_REV_ENABLED: bool = False
SWING_REV_RATE_15M_GE: float = 60.0
SWING_VOLUME_RATIO_GE: float = 1.5
SWING_ATR_15M_GE: float = 0.80
SWING_PULSE_GE: float = 0.15
SWING_CUM_MOVE_PCT: float = 1.8
SWING_TP_PCT: float = 2.40
SWING_SL_PCT: float = 1.30
SWING_MAX_HOLD_MINS: int = 45

# 殭屍盤／流動性枯竭過濾（1m FADE / MOMO）
ZOMBIE_LOOKBACK_1M: int = 15
ZOMBIE_FLAT_AMP_PCT: float = 0.05
ZOMBIE_FLAT_COUNT_GE: int = 4
ZOMBIE_AVG_AMP_LT: float = 0.16

# 兩段式保本鎖利（預設／MEAN・TREND 等）
BREAKEVEN_MFE_GE: float = 0.80
BREAKEVEN_OFFSET_PCT: float = 0.15
TRAIL_ARM_MFE_GE: float = 1.30
TRAIL_GIVEBACK_PCT: float = 0.45

# A 區持倉 1m 擷取上限
KLINE_SCENE_HOLD_MAX_BARS: int = 35

# SL 後同幣冷卻（分鐘）
SL_COOLDOWN_MINUTES: int = 15

# ---------------------------------------------------------------------------
# 模擬盯盤（Paper Trader）— 帳戶與倉位固定參數
# ---------------------------------------------------------------------------
INITIAL_CAPITAL_USDT: float = 500.0  # 壓力測試擴編：初始總本金 500U
LEVERAGE: float = 10.0  # 槓桿 10x
MARGIN_PER_TRADE_USDT: float = 10.0  # 單筆保證金
NOTIONAL_USDT: float = 100.0  # 單筆名目價值 = 10U × 10x
MAX_CONCURRENT_POSITIONS: int = 10  # 最多同時 10 筆（動用 100U 保證金；同幣最多 1 倉）
MAX_OPEN_POSITIONS: int = MAX_CONCURRENT_POSITIONS  # 相容別名
ROUND_TRIP_FEE_RATE: float = 0.001  # 雙邊手續費 0.05%+0.05%=0.1%

# 相容舊名稱（內部仍可引用）
PAPER_INITIAL_BALANCE: float = INITIAL_CAPITAL_USDT
PAPER_MARGIN_USDT: float = MARGIN_PER_TRADE_USDT
PAPER_LEVERAGE: float = LEVERAGE
PAPER_FEE_RATE_ONE_SIDE: float = ROUND_TRIP_FEE_RATE / 2.0

# 榜單／股性／分派重刷間隔（分鐘）— live 模式預設每 10 分鐘
REFRESH_ROUTING_MINUTES: int = 10
SIGNAL_CHECK_SECOND: int = 2  # 每分鐘第 N 秒檢查已收盤 K 線

# K 線增量金庫與 API 限流（防 Rate Limit；Top50 每幣間隔 0.05s）
KLINES_VAULT_DIR: str = "data/kline_vault"  # 正式增量庫（依 symbol+interval 覆寫）
KLINES_CACHE_DIR: str = KLINES_VAULT_DIR  # 相容舊名稱
KLINES_API_CONCURRENCY: int = 2  # 同時打 K 線 API 的上限
KLINES_MIN_INTERVAL_SEC: float = 0.05  # 每次 K 線請求最小間隔
KLINES_PER_SYMBOL_DELAY_SEC: float = 0.05  # 每支幣分析完再間隔，避免打爆限速

# ---------------------------------------------------------------------------
# 輸出路徑
# ---------------------------------------------------------------------------
OUTPUT_DIR: str = "output"
SUMMARY_CSV: str = "output/coin_personality_summary.csv"
AI_REPORT_MD: str = "output/ai_prompt_report.md"
ROUTING_PLAN_MD: str = "output/active_routing_plan.md"
ROUTING_PLAN_JSON: str = "output/active_routing_plan.json"
LIVE_TRADES_CSV: str = "output/live_trades_log.csv"
DAILY_REVIEW_MD: str = "output/daily_review_for_ai.md"
# AI 專用 K 線濃縮切片（案發現場 A 區 + Top15 快照 B 區）
KLINE_SLICES_FOR_AI_MD: str = "output/kline_slices_for_ai.md"
KLINE_CRIME_SCENES_JSON: str = "output/kline_crime_scenes.json"
# 股性資料庫：1 幣 1 行主表 + 智慧去重歷史
COIN_PROFILES_VAULT_CSV: str = "output/coin_profiles_vault.csv"
COIN_PROFILES_VAULT_MD: str = "output/coin_profiles_vault.md"
COIN_PROFILES_HISTORY_CSV: str = "output/coin_profiles_history.csv"
