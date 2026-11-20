"""
相容別名：analyzer.py → strategy_router.py（門派分派邏輯）
"""

from strategy_router import *  # noqa: F401,F403
from strategy_router import run_router, route_coin, route_dataframe

__all__ = ["run_router", "route_coin", "route_dataframe"]
