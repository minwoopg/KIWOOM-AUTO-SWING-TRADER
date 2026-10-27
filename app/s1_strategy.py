from __future__ import annotations

"""S1 전략 (M3 B 진입 + C2 E1 청산) — 관리자 후보(`app/candidates.py`) → 주문 직전 가격 재확인 → 1주 상한 지정가 매수 의도,
보유는 E1 규칙(손절·10거래일 시간 청산)으로 매도 의도.

E1 청산 (C2 — GPT 최소판 요청 4, B 검토 C3)
- 대상: 원장 보유 > 0 이고 포지션 메타가 이 전략(ORDER·strategy_id 같음)·손절가 있음. 메타가 없거나 다르면 자동 매도하지 않고
  META_MISSING 기록(사람 확인 — 진입은 최대 보유 1이라 어차피 없음).
- 손절: 유효한 현재가(양의 정수) ≤ 손절가 → 원장·잔고 보유 전량 시장가 매도(forced). 가격 조회 실패·0(거래정지 등)은 매도하지 않음.
- 시간 청산: 진입 거래일(원장 첫 체결일)을 0일로 세어 10번째 거래일의 마감 20분 전(일반 15:10)부터 전량 시장가 매도.
  그 시각을 지나쳤으면(실행기가 꺼져 있었음) 다음 정규장 폴링에서 바로. C4부터 forced(09:00부터·15:15 이후 정규장도).
- C4: 손절·시간 청산 모두 유효한 현재가(양의 정수)가 있어야 매도 — 조회 실패·0이면 보류하고 다음 폴링에 다시.
  미접수 매도는 종목별로 60초부터 두 배씩(최대 15분) 기다렸다 다시 냄.
- 중복 매도 방지: 미해결 주문·복구 필요(ERROR·orphan)·장부 불일치 종목은 건너뜀(실행기 안전 한도와 PSM이 한 번 더 막음).
  원장 수량 ≠ 잔고 수량이면 매도하지 않음(QTY_MISMATCH 기록).
- 신규 매수 중단·신호 키 손상·후보 오류는 청산을 막지 않음(청산을 먼저 판단).

규칙 (GPT 최소판 요청 — S1 조건·기준값은 바꾸지 않음)
- 진입 시간: entry_start(09:05) ≤ 지금 < entry_end(session.entry_cancel_at, 09:30), 정규장.
- 최대 보유 1: 원장·잔고에 보유가 있거나 미해결 주문이 있으면 신규 의도 없음. 추가 매수 없음.
- 후보는 승인된 것만(CandidateReader — 관리자 기록 교차 검증). **폴링마다 다시 읽고**, 그 폴링의 결과가
  ERROR·WAITING·거부면 신규 의도 없음(C1 — 한 번 받은 승인을 붙들고 있지 않음).
- 주문 직전 재확인: 현재가 > 참고 손절가 ∧ 현재가 ≤ 진입 상한 ∧ 최우선 매도호가 ≤ 지정가. 하나라도 아니면 그 폴링은 보류
  (진입 시간 안에서 다음 폴링에 다시 확인). 호가를 못 받으면 보류.
- 지정가 = 진입 상한을 KRX 호가 단위로 내림(후보 승인에서 계산), 수량 1.
- 같은 신호는 한 번만: 의도를 넘기기 전에 신호 키를 기록(SignalKeyStore). 취소로 FLAT이 돼도 다시 진입하지 않음.
- 한 폴링에 의도 하나.
- 판단 기록: 후보 상태·보류 사유·의도를 decisions(콜백)로 남김(같은 사유 반복은 한 번).
- H1·H2: 주문 미허용(allowed_symbols 밖) 승인 후보는 `after_tick`(실행기가 그 폴링의 주문 의도를 모두 처리한 뒤 호출)에서
  `EntryObserver`로 가격만 관찰해 따로 기록(의도·신호 키 없음, 보유·미해결 주문이 있어도 계속, 실제 시계·15초 예산).
  관찰 실패는 매매에 영향 없음.
"""

from datetime import date, datetime, time
from typing import Callable

from app.entry_observation import entry_reasons
from domain.strategy.interface import OrderIntent, TickContext
from utils.trading_calendar import MarketPhase

STRATEGY_ID = "s1_pullback_v0.1"


class S1EntryStrategy:
    strategy_id = STRATEGY_ID

    TIME_EXIT_SESSIONS = 10           # 진입일 0 → 10번째 거래일
    TIME_EXIT_BEFORE_CLOSE_MIN = 20   # 그날 마감 20분 전(일반 15:10)
    EXIT_BACKOFF_BASE_SEC = 60        # C4: 청산 매도 미접수 뒤 재전송 간격
    EXIT_BACKOFF_MAX_SEC = 900

    def __init__(self, *, mode: str, scope: str, candidates: Callable[[date, datetime], object],
                 quote: Callable[[str], tuple[int | None, int | None]], keys, decisions: Callable[[dict], None],
                 entry_start: time, entry_end: time, quantity: int = 1, calendar=None,
                 price: Callable[[str], int | None] | None = None, allowed_symbols: tuple[str, ...] = (),
                 observer=None) -> None:
        if quantity != 1:
            raise ValueError("최소판: 수량 1 고정")
        self.mode, self.scope = mode, scope
        self.candidates, self.quote, self.keys, self.decisions = candidates, quote, keys, decisions
        self.entry_start, self.entry_end, self.quantity = entry_start, entry_end, quantity
        self.calendar = calendar
        self.allowed = frozenset(allowed_symbols)       # C3: 비어 있으면 제한 없음(그림자). paper는 기동 점검에서 필수
        self.price = price or (lambda sym: quote(sym)[0])
        self.observer = observer                        # H1: 주문 미허용 후보 가격 관찰(없으면 관찰 안 함)
        self._obs_book = None
        self.book = None
        self._now: datetime | None = None
        # C4: forced 매도가 미접수면 종목별 재전송 간격(60·120·240…초, 최대 900초). 메모리 — 재시작하면 처음부터
        self._exit_wait: dict[str, tuple[int, datetime]] = {}
        self._last_note: dict[str, str] = {}

    # ── 기록 ────────────────────────────────────────────────
    def _note(self, ctx: TickContext, topic: str, event: str, detail: dict | None = None) -> None:
        sig = f"{event}|{sorted((detail or {}).items())}"
        if self._last_note.get(topic) == sig:
            return
        self._last_note[topic] = sig
        self.decisions({"at": ctx.now.isoformat(timespec="seconds"), "mode": self.mode, "strategy": STRATEGY_ID,
                        "topic": topic, "event": event, **(detail or {})})

    def key(self, signal_id: str) -> str:
        return f"{self.mode}|{self.scope}|{signal_id}"

    # ── Strategy ────────────────────────────────────────────
    def on_session_start(self, ctx: TickContext) -> None:
        return None

    def on_session_end(self, ctx: TickContext) -> None:
        if self.book is not None:
            self._note(ctx, "session", "SESSION_END", {"book_state": self.book.state,
                                                       "approved": [a.symbol for a in self.book.approved]})

    def on_intent_result(self, intent: OrderIntent, status: str, detail: str = "") -> None:
        """실행기가 의도를 처리한 결과(C2) — 신호 키에 상태 줄(SHADOW·GUARD_REJECTED·SUBMITTED·NOT_ACCEPTED·UNKNOWN)."""
        if intent.side == "SELL":
            from datetime import timedelta
            if status.startswith("NOT_ACCEPTED"):
                n = self._exit_wait.get(intent.symbol, (0, None))[0] + 1
                at = (self._now or datetime.now()) + timedelta(seconds=min(self.EXIT_BACKOFF_BASE_SEC * 2 ** (n - 1),
                                                                            self.EXIT_BACKOFF_MAX_SEC))
                self._exit_wait[intent.symbol] = (n, at)
            elif status == "SUBMITTED":
                self._exit_wait.pop(intent.symbol, None)
        key = (intent.context or {}).get("signal_key")
        if key:
            try:
                self.keys.mark(key, status, detail, at=self._now)
            except Exception:                          # noqa: BLE001 — 상태 기록 실패가 실행을 멈추지 않음(키는 이미 있음)
                pass
        topic = f"result:{intent.symbol}:{intent.side}:{intent.reason}"
        sig = status                                     # 상세에는 시각이 들어갈 수 있음 — 상태로만 비교
        if self._last_note.get(topic) == sig:          # 주문 가능 시각 전 반복 차단 등 — 같은 결과는 한 번만
            return
        self._last_note[topic] = sig
        self.decisions({"at": (self._now or datetime.now()).isoformat(timespec="seconds"), "mode": self.mode,
                        "strategy": STRATEGY_ID, "topic": intent.symbol, "event": f"RESULT_{intent.side}",
                        "status": status, "detail": detail, "reason": intent.reason})

    # ── E1 청산 ─────────────────────────────────────────────
    def time_exit_at(self, entry_date: date) -> datetime | None:
        if self.calendar is None:
            return None
        d = self.calendar.add_trading_days(entry_date, self.TIME_EXIT_SESSIONS)
        st = self.calendar.session_times(d)
        if st is None:
            return None
        from datetime import timedelta
        return datetime.combine(d, st.close) - timedelta(minutes=self.TIME_EXIT_BEFORE_CLOSE_MIN)

    def _exits(self, ctx: TickContext) -> list[OrderIntent]:
        out: list[OrderIntent] = []
        broker_qty = {p.symbol: p.quantity for p in ctx.balance.positions if p.quantity > 0}
        for sym, pos in sorted(ctx.positions.items()):
            if pos.quantity <= 0:
                continue
            meta = ctx.metas.get(sym)
            if meta is None or meta.origin != "ORDER" or meta.strategy_id != STRATEGY_ID or not meta.stop_price:
                self._note(ctx, f"exit:{sym}", "META_MISSING", {
                    "detail": "보유는 있는데 이 전략의 진입 메타(손절가)가 없음 — 자동 청산 안 함, 사람 확인",
                    "meta": None if meta is None else {"origin": meta.origin, "strategy_id": meta.strategy_id,
                                                       "stop_price": meta.stop_price}})
                continue
            if sym in ctx.in_flight_symbols or sym in ctx.blocked_symbols:
                self._note(ctx, f"exit:{sym}", "EXIT_SKIP_BLOCKED", {"in_flight": sym in ctx.in_flight_symbols})
                continue
            if broker_qty.get(sym, 0) != pos.quantity:
                self._note(ctx, f"exit:{sym}", "QTY_MISMATCH", {"ledger": pos.quantity, "broker": broker_qty.get(sym, 0)})
                continue
            due = self.time_exit_at(pos.first_entry_date)
            time_due = due is not None and ctx.now >= due
            wait = self._exit_wait.get(sym)
            if wait is not None and ctx.now < wait[1]:
                self._note(ctx, f"exit:{sym}", "EXIT_BACKOFF", {"failures": wait[0],
                                                                "next_at": wait[1].isoformat(timespec="seconds")})
                continue
            try:
                px = self.price(sym)
            except Exception as exc:                       # noqa: BLE001
                px = None
                self._note(ctx, f"exit:{sym}", "PRICE_FAILED", {"error": f"{type(exc).__name__}: {exc}",
                                                                "time_due": time_due})
            if not (type(px) is int and px > 0):
                # C4(GPT R2): 손절·시간 청산 모두 유효한 현재가가 있어야 함 — 손절가를 관측가 대신 쓰지 않음.
                # 시간 청산 기한은 그대로 남아 다음 유효 조회에서 실행.
                self._note(ctx, f"exit:{sym}", "NO_PRICE", {"price": px, "time_due": time_due})
                continue
            stop_hit = px <= meta.stop_price
            if not stop_hit and not time_due:
                continue
            reason = "E1_STOP" if stop_hit else "E1_TIME"
            self._note(ctx, f"exit:{sym}", reason, {"price": px, "stop_price": meta.stop_price,
                                                    "entry_date": pos.first_entry_date.isoformat(),
                                                    "time_exit_at": due.isoformat(timespec="minutes") if due else None,
                                                    "quantity": pos.quantity})
            # C4(GPT Q1): 시간 청산도 forced — 기한을 놓친 보유는 다음 날 09:00부터, 15:15 이후 정규장에서도.
            # 복구 필요·장부 불일치·잔고 초과 매도 차단은 안전 한도에 그대로.
            out.append(OrderIntent(sym, "SELL", pos.quantity, px, reason=reason, forced=True,
                                   context={"exit_rule": reason, "stop_price": meta.stop_price, "exit_price_seen": px,
                                            "entry_date": pos.first_entry_date.isoformat(),
                                            "signal_id": (meta.meta or {}).get("signal_id"), "strategy_mode": self.mode}))
        return out

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        now = self._now = ctx.now
        self._obs_book = None
        if ctx.phase != MarketPhase.REGULAR:
            return []
        exits = self._exits(ctx)
        if exits:
            return exits                                   # 청산이 있으면 그 폴링은 신규 진입을 보지 않음
        if not (self.entry_start <= now.time() < self.entry_end):
            return []
        out = self._entry(ctx)
        self._obs_book = self.book                         # 이번 폴링에 읽은 승인 — after_tick 관찰 대상
        return out

    def after_tick(self, ctx: TickContext, clock: Callable[[], datetime] | None = None) -> None:
        """H2(GPT 10/27): 실행기가 그 폴링의 주문 의도를 모두 처리한 뒤 호출 — 주문 미허용 승인 후보 가격 관찰.
        on_tick이 진입 판단을 한 폴링(그때 읽은 승인)만 대상. clock은 실행기 시계(실제 수신 시각 기록·마감 판정)."""
        book, self._obs_book = self._obs_book, None
        if self.observer is None or not self.allowed or book is None or book.state != "READY":
            return
        if ctx.phase != MarketPhase.REGULAR:
            return
        try:
            cands = [c for c in book.approved if c.symbol not in self.allowed]
            if cands:
                self.observer.observe(ctx.trade_date, cands, window_end=datetime.combine(ctx.trade_date, self.entry_end),
                                      clock=clock)
        except Exception as exc:                           # noqa: BLE001 — 관찰 실패가 매매를 멈추지 않음
            self._note(ctx, "observe", "OBSERVE_FAILED", {"error": f"{type(exc).__name__}: {exc}"})

    def _entry(self, ctx: TickContext) -> list[OrderIntent]:
        now = ctx.now
        # C1(GPT 검토 R1): 신규 진입을 검토하는 폴링마다 승인을 새로 읽음 — 이전 READY를 계속 쓰지 않음.
        # 최신 결과가 ERROR·WAITING·거부면 그 폴링은 의도 없음. 이미 쓴 신호 키는 다시 살아나지 않음.
        self.book = self.candidates(ctx.trade_date, now)
        self._note(ctx, "book", f"CANDIDATES_{self.book.state}", {
            "signal_date": self.book.signal_date, "contract": self.book.contract_hash,
            "approved": [a.symbol for a in self.book.approved], "rejected": self.book.rejected,
            "note": self.book.note})
        if self.book.state != "READY" or not self.book.approved:
            return []
        held = ({s for s, p in ctx.positions.items() if p.quantity > 0}
                | {p.symbol for p in ctx.balance.positions if p.quantity > 0})
        if held or ctx.orders_in_flight:
            self._note(ctx, "capacity", "MAX_POSITION", {"held": sorted(held), "in_flight": ctx.orders_in_flight})
            return []
        try:
            used = self.keys.keys()
        except Exception as exc:                           # noqa: BLE001 — 신호 키 파일 손상: 신규 진입 중단
            self._note(ctx, "keys", "SIGNAL_KEYS_UNREADABLE", {"error": f"{type(exc).__name__}: {exc}"})
            return []
        for c in sorted(self.book.approved, key=lambda a: a.symbol):
            k = self.key(c.signal_id)
            if k in used:
                continue
            if self.allowed and c.symbol not in self.allowed:
                self._note(ctx, c.symbol, "NOT_ALLOWED", {"allowed": sorted(self.allowed)})
                continue
            if c.symbol in ctx.blocked_symbols or c.symbol in ctx.in_flight_symbols:
                self._note(ctx, c.symbol, "SKIP_BLOCKED")
                continue
            try:
                price, ask = self.quote(c.symbol)
            except Exception as exc:                       # noqa: BLE001
                price, ask = None, None
                self._note(ctx, c.symbol, "QUOTE_FAILED", {"error": f"{type(exc).__name__}: {exc}"})
            why = entry_reasons(price, ask, c)
            if why:
                self._note(ctx, c.symbol, "WAIT_PRICE", {"reasons": why, "price": price, "ask": ask,
                                                         "limit": c.limit_price, "stop_ref": c.stop_ref,
                                                         "entry_cap": c.entry_cap})
                continue
            rec = {"at": now.isoformat(timespec="seconds"), "symbol": c.symbol, "signal_id": c.signal_id,
                   "signal_date": c.signal_date, "limit_price": c.limit_price, "price": price, "ask": ask}
            try:
                if not self.keys.claim(k, rec):
                    continue
            except Exception as exc:                       # noqa: BLE001
                self._note(ctx, "keys", "SIGNAL_KEYS_UNREADABLE", {"error": f"{type(exc).__name__}: {exc}"})
                return []
            intent = OrderIntent(c.symbol, "BUY", self.quantity, price, reason=f"S1 신호 {c.signal_date}",
                                 order_type="limit", limit_price=c.limit_price,
                                 context={"signal_id": c.signal_id, "signal_date": c.signal_date,
                                          "entry_cap": c.entry_cap, "stop_ref": c.stop_ref,
                                          "contract_hash": c.contract_hash, "scan_run_id": c.scan_run_id,
                                          "strategy_mode": self.mode, "pre_order_price": price, "pre_order_ask": ask,
                                          "signal_key": k})
            self._note(ctx, c.symbol, "INTENT", {**rec, "stop_ref": c.stop_ref, "entry_cap": c.entry_cap})
            return [intent]
        return []
