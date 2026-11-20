"""
相容別名：live_simulator.py → live_paper_trader.py

實際模擬盯盤與深度複盤邏輯皆在 live_paper_trader.py。

可直接：
    python3 live_simulator.py --once
    python3 live_simulator.py --loop
或：
    python3 main.py --live

Live 模式重刷週期：每 10 分鐘更新 Top20 股性與策略分派（K 線增量快取）。
"""

from live_paper_trader import main

if __name__ == "__main__":
    main()
