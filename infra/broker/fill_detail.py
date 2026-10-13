from __future__ import annotations

"""ka10076 체결 행 → 주문 하나의 체결 근거 (M3 A2) — 순수 함수.

단타 원본에서 그대로 가져온 `kiwoom_order_status.py`는 고치지 않고(출처 기록 unchanged 유지),
그 판정 함수(`derive_broker_order_status`·`find_all_matching`)를 그대로 써서 만듭니다.
"""

from typing import Any, Iterable

from domain.models import BrokerOrderStatus
from domain.service.fill_evidence import FillDetail
from infra.broker.kiwoom_order_status import (
    _parse_optional_qty, derive_broker_order_status, find_all_matching, normalize_order_id,
)


def extract_fill_detail(order_id: Any, symbol: str, cntr_entries: Iterable[dict] | None) -> FillDetail | None:
    """주문 하나의 실제 체결가·수량·수수료·세금.

    2026-10-13 실측(0112293 매수·0112348 매도): 주문마다 한 행, `cntr_pric`=실제 체결가(229,000),
    `tdy_trde_cmsn`/`tdy_trde_tax`=그 주문의 수수료·세금(800 / 매도 457, 매수 0).
    다음이면 None — 값을 지어내지 않음:
    - 주문번호가 비었거나 매칭 행이 없음
    - 매칭 행이 2개 이상(개별 체결/누적 형식 미실측)
    - 전량 체결 signature 불일치(부분 체결 등), 체결가·수량이 0 이하·해석 불가
    - 행의 종목코드가 다름, 매수/매도를 알 수 없음
    """
    target = normalize_order_id(order_id)
    if not target:
        return None
    rows = find_all_matching(list(cntr_entries) if cntr_entries else [], target)
    if len(rows) != 1:
        return None
    row = rows[0]
    row_symbol = str(row.get("stk_cd", "") or "").strip()
    if row_symbol and row_symbol.lstrip("A") != str(symbol).strip():
        return None
    bo = derive_broker_order_status(order_id, symbol, [], [row])
    if bo.status != BrokerOrderStatus.FILLED or bo.side is None:
        return None
    qty, price = bo.filled_quantity, bo.filled_price
    if not qty or not price or qty <= 0 or price <= 0:
        return None
    return FillDetail(
        order_id=str(order_id).strip(), symbol=symbol, side=bo.side.value, quantity=qty, price=price,
        commission=_parse_optional_qty(row.get("tdy_trde_cmsn")),
        tax=_parse_optional_qty(row.get("tdy_trde_tax")), raw=dict(row),
    )
