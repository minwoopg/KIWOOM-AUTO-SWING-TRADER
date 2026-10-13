# 키움 모의투자 실측 응답 (2026-10-13, M1 limit-cancel `m1-lc-1013`)

`tools/paper_order.py limit-cancel 005930 --command-id m1-lc-1013 --execute`가 저장한 응답 중 본문만 옮김.
계좌번호는 기록기에서 이미 `***`로 가려짐. 인증 응답(토큰)은 넣지 않음. 모의 계좌 금액만 있음.

| 파일 | 내용 |
|---|---|
| limit_order_kt10000.json | 지정가 207,000원 1주 매수 접수 → 주문 0148913 (현재가 230,000) |
| before_cancel_ka10075.json | 취소 전 미체결: ord_stt 접수 · ord_qty 1 · oso_qty 1 · cntr_qty 0 · trde_tp 보통 |
| before_cancel_ka10076.json | 같은 시점 체결 목록(앞서 시장가로 사고판 0112293·0112348만) |
| cancel_kt10003.json | 취소 접수 → 취소 주문 0148928, base_orig_ord_no 0148913, cncl_qty 1 |
| after_cancel_ka10075.json | 취소 뒤 미체결 `oso: []` |
| after_cancel_ka10076.json | 취소 뒤 체결 목록 — 원주문·취소 주문 행 없음 |
| after_cancel_kt00018.json | 취소 뒤 잔고 0주 |

쓰는 곳: `test_m1_measured.py`(판정·취소 확정·체결 근거). 단타 원본 실측(`order_reconciliation/`, 2026-08-14)은
이 레포에 없어서 `test_broker_order_status.py`는 계속 SKIP — 이 자료로 대신하지 않음.
