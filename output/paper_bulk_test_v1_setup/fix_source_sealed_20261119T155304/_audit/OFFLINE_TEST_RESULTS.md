# 管理可靠性離線測試結果

前提：fake clock／fake session；**不**代表真實網路延遲上界。

- passed: 22
- failed: 0

## 明細
- PASS budget_due
- PASS budget_book_op
- PASS budget_book_attempts
- PASS budget_book_timeout
- PASS budget_slice
- PASS E1_overdue_after_long_manage
- PASS E1_no_burst_second_skip
- PASS E1_due_advanced_by_period
- PASS E2_429_batch_fail
- PASS E2_429_blocked_all
- PASS E2_418_batch_fail
- PASS E3_timeout_raised
- PASS E3_no_finish_after_cancel
- PASS E3_started
- PASS C_book_initial_stop_without_atr
- PASS M3_atr_none_keeps_stop
- PASS M3_atr_none_noop_or_stop
- PASS D_no_pos_status
- PASS D_no_pos_expected0
- PASS E6_both_checked
- PASS E6_error_count
- PASS E5_partial_then_fail_filled

## 預算基準（離線；部署另批）
- MANAGE_DUE_SEC=5.0
- MANAGE_BOOK_REQUEST_TIMEOUT_SEC=5.0
- MANAGE_BOOK_MAX_ATTEMPTS=2
- MANAGE_BOOK_OP_BUDGET_SEC=10.0
- SCAN_SLICE_MAX_WAIT_SEC=2.0

未合入：N/T 健康禁止開倉政策。
未改：止損／TP／反手／倉位／日損參數。
