from pathlib import Path

fx = Path(__file__).resolve().parent / "fixtures"
fx.mkdir(parents=True, exist_ok=True)

(fx / "arm_fills_synth.jsonl").write_text(
    "\n".join(
        [
            '{"event":"OPEN","trade_id":":BULLAUSDT:727206056c","recorded_at":"2026-11-27T07:29:16.234470Z","qty":100,"price":0.04,"fee_u":0.01,"side":"BUY","strategy_version":"e_top10_1m_down_v1","exchange_trade_key":"ex:1","testnet_client_order_id":"STRAT_E_f9ba63ff","funding_data_gap":true}',
            '{"event":"SETTLEMENT_CLOSE","trade_id":":BULLAUSDT:727206056c","recorded_at":"2026-11-27T07:50:42.717639Z","qty":100,"price":0.039,"fee_u":0.01,"side":"SELL","strategy_version":"e_top10_1m_down_v1","exchange_trade_key":"ex:2","testnet_client_order_id":"STRAT_E_f9ba63ff"}',
            '{"event":"SETTLEMENT_CLOSE","trade_id":":BULLAUSDT:727206056c","recorded_at":"2026-11-27T07:50:42.717639Z","qty":100,"price":0.039,"fee_u":0.01,"side":"SELL","strategy_version":"e_top10_1m_down_v1","exchange_trade_key":"ex:2","testnet_client_order_id":"STRAT_E_f9ba63ff"}',
            '{"event":"FEE_ADJUST","trade_id":":BULLAUSDT:727206056c","recorded_at":"2026-11-27T07:50:42.717639Z","qty":0,"fee_u":0.002,"strategy_version":"e_top10_1m_down_v1","exchange_trade_key":"ex:fee"}',
            '{"event":"OPEN","trade_id":":OTHER:xxx","recorded_at":"2026-11-27T07:29:16.234470Z","qty":1,"price":1,"fee_u":0}',
            "",
        ]
    ),
    encoding="utf-8",
)

(fx / "ledger_fills_synth.jsonl").write_text(
    "\n".join(
        [
            '{"event":"FILL","client_order_id":"STRAT_E_f9ba63ff","exchange_order_id":"366618857","exchange_trade_id":"111","observation_kind":"exchange_trade"}',
            '{"event":"FILL","client_order_id":"OTHER","exchange_order_id":"999","exchange_trade_id":"222","observation_kind":"exchange_trade"}',
            '{"event":"FILL","client_order_id":"STRAT_E_f9ba63ff","exchange_order_id":"366618857","exchange_trade_id":"111","observation_kind":"exchange_trade"}',
            "",
        ]
    ),
    encoding="utf-8",
)

(fx / "ledger_orders_synth.jsonl").write_text(
    '{"event":"ORDER","client_order_id":"STRAT_E_f9ba63ff","exchange_order_id":"366618857"}\n',
    encoding="utf-8",
)
(fx / "bars_empty.json").write_text("[]\n", encoding="utf-8")
(fx / "bars_offwindow.json").write_text(
    '[{"t":1764228600000,"o":1,"h":1,"l":1,"c":1,"v":1}]\n', encoding="utf-8"
)
(fx / "queries.json").write_text(
    '[{"name":"client_order_id","client_order_id":"STRAT_E_f9ba63ff"},'
    '{"name":"exchange_order_id","exchange_order_id":"366618857"},'
    '{"name":"exchange_trade_key","exchange_trade_key":"no_such_key"},'
    '{"name":"tp1_order_id","tp1_order_id":"999999999"}]\n',
    encoding="utf-8",
)
print("ok", (fx / "arm_fills_synth.jsonl").read_bytes()[:4])
