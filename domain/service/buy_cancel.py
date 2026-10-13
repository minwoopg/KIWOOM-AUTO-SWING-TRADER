from __future__ import annotations

"""지정가 매수 취소의 확정 판정 (M3 A2) — 순수 함수.

2026-10-13 키움 모의 실측(`paper_order.py limit-cancel`, 원주문 0148913 → 취소 0148928):
- kt10003 응답 `{"ord_no":"0148928","base_orig_ord_no":"0148913","cncl_qty":"000000000001",
  "return_code":0,"return_msg":"모의투자 취소주문완료"}`
- 취소 뒤 ka10075 `oso: []`, ka10076에도 원주문·취소 주문 행이 **없음**(체결 없이 취소된 주문은
  두 목록 어디에도 남지 않음), 잔고 0주.

따라서 "목록에 없음"만으로는 취소와 주문번호 오류·조회 범위 밖을 구분할 수 없습니다. 취소 확정은
다음을 **모두** 만족할 때만입니다.
1. 이 프로그램이 그 원주문번호로 보낸 취소가 접수됨(return_code 0, 응답 불명 아님)
   — 응답의 base_orig_ord_no가 원주문번호와 같고 cncl_qty가 요청 잔량과 같아야 함(누락·0이면 미확정)
2. 원주문 체결조회 결과: 미체결(oso)·체결(cntr) 어디에도 그 주문 행이 없음, 증거 구성 오류 없음
3. 잔고 수량이 주문 직전 수량 그대로
그 밖:
- 체결 행이 전량 체결 signature → FILLED(취소보다 체결이 먼저 — 정상 체결 경로로 처리)
- 미체결 행이 그대로 OPEN → STILL_OPEN(아직 반영 전일 수 있음 — 상태 유지, 다시 확인)
- 나머지(부분 체결·미실측 행·잔고 변화·취소 응답 불명/거절) → UNRESOLVED(사람 확인, 상태 유지)
"""

from dataclasses import dataclass, field
from datetime import datetime

from domain.models import BrokerOrderStatus

CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
FILLED_BEFORE_CANCEL = "FILLED"
STILL_OPEN = "STILL_OPEN"
UNRESOLVED = "UNRESOLVED"


def _norm(order_id) -> str:
    return str(order_id or "").strip().lstrip("0")


@dataclass
class CancelRecord:
    symbol: str
    order_id: str                 # 원 BUY 주문번호
    quantity: int                 # 취소 요청 수량
    base_quantity: int            # 주문 직전 잔고 수량(취소 확정 시 기대 잔고)
    requested_at: datetime
    reason: str = ""
    accepted: bool = False
    ambiguous: bool = False
    cancel_order_id: str = ""
    message: str = ""
    body: dict = field(default_factory=dict)
    attempts: int = 1
    resolved: str = ""            # CANCEL_CONFIRMED / FILLED / UNRESOLVED(사람 확인) — 끝난 기록

    @property
    def body_matches_order(self) -> bool:
        """취소 응답이 이 주문·이 수량의 취소인지(GPT 검토 — 누락·0·불일치는 확인 안 된 것으로).
        실측 응답: base_orig_ord_no="0148913", cncl_qty="000000000001"."""
        return not self.body_mismatch

    @property
    def body_mismatch(self) -> str:
        body = self.body if isinstance(self.body, dict) else {}
        base = _norm(body.get("base_orig_ord_no"))
        if not base:
            return "취소 응답에 원주문번호(base_orig_ord_no) 없음·0"
        if base != _norm(self.order_id):
            return f"취소 응답의 원주문번호 {body.get('base_orig_ord_no')} ≠ {self.order_id}"
        raw = str(body.get("cncl_qty", "") or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            return f"취소 응답의 취소 수량 {raw!r} 해석 불가·0"
        if int(raw) != self.quantity:
            return f"취소 응답의 취소 수량 {int(raw)} ≠ 요청 잔량 {self.quantity}"
        return ""

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "order_id": self.order_id, "quantity": self.quantity,
                "base_quantity": self.base_quantity, "requested_at": self.requested_at.isoformat(),
                "reason": self.reason, "accepted": self.accepted, "ambiguous": self.ambiguous,
                "cancel_order_id": self.cancel_order_id, "message": self.message, "attempts": self.attempts,
                "resolved": self.resolved}


@dataclass(frozen=True)
class CancelAssessment:
    verdict: str
    detail: str


def assess_buy_cancel(record: CancelRecord, evidence, broker_quantity: int) -> CancelAssessment:
    """record: 이 프로그램이 보낸 취소. evidence: `OrderStatusEvidence`(원주문 기준). broker_quantity: 잔고."""
    bo = evidence.broker_order
    if _norm(bo.order_id) != _norm(record.order_id):
        return CancelAssessment(UNRESOLVED, f"조회한 주문번호 {bo.order_id} ≠ 취소 대상 {record.order_id}")
    if bo.status == BrokerOrderStatus.FILLED:
        return CancelAssessment(FILLED_BEFORE_CANCEL, f"취소 전에 전량 체결 {bo.filled_quantity}주 @{bo.filled_price}")
    if bo.status == BrokerOrderStatus.OPEN:
        return CancelAssessment(STILL_OPEN, f"미체결 {bo.open_quantity}주 그대로")
    # UNKNOWN 이하
    if record.ambiguous or not record.accepted:
        return CancelAssessment(UNRESOLVED, "취소 접수가 확인되지 않음(응답 불명·거절) — 목록에 없다는 것만으로 확정 안 함")
    if record.body_mismatch:
        return CancelAssessment(UNRESOLVED, record.body_mismatch)
    if getattr(evidence, "evidence_error", None):
        return CancelAssessment(UNRESOLVED, f"조회 근거 구성 오류: {evidence.evidence_error}")
    if evidence.matched_oso_entries or evidence.matched_cntr_entries:
        return CancelAssessment(
            UNRESOLVED, f"미실측 행 형식(미체결 {len(evidence.matched_oso_entries)}행·체결 "
                        f"{len(evidence.matched_cntr_entries)}행) — 부분 체결일 수 있음")
    if broker_quantity != record.base_quantity:
        return CancelAssessment(UNRESOLVED, f"잔고 {broker_quantity}주 ≠ 주문 직전 {record.base_quantity}주")
    return CancelAssessment(CANCEL_CONFIRMED,
                            f"취소 {record.cancel_order_id or '?'} 접수 + 원주문 미체결·체결 행 없음 + 잔고 "
                            f"{broker_quantity}주 그대로")
