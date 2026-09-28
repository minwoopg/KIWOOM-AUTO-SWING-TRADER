"""동등성 비교용 스크립트 브로커 — tools/equivalence/compare.py 참고."""
from datetime import datetime
from domain.models import (AccountBalance, Position, OrderResult, BrokerOrder, BrokerOrderStatus,
                           OrderStatusEvidence, MarketPrice, OrderSide)
from infra.broker.base import Broker

class ScriptedBroker(Broker):
    def __init__(self):
        self.positions = {}
        self.cash = 100_000_000
        self.next_result = []   # list of ("accept"|"reject"|"ambiguous"|"raise"|"accept_noid")
        self.status = {}        # order_id -> BrokerOrderStatus or Exception
        self.calls = []
        self.seq = 0
    def authenticate(self): pass
    def get_market_price(self, s): return MarketPrice(s, 10000, 10000, 10000, datetime.now())
    def get_account_balance(self):
        return AccountBalance(self.cash, self.cash, [Position(s, q, 10000) for s, q in self.positions.items() if q > 0])
    def place_order(self, order):
        self.calls.append(("place", order.symbol, order.side.value, order.quantity))
        kind = self.next_result.pop(0) if self.next_result else "accept"
        self.seq += 1
        oid = f"{self.seq:07d}"
        if kind == "raise": raise RuntimeError("boom")
        if kind == "ambiguous":
            return OrderResult("", order.symbol, order.side, order.quantity, False, "timeout", datetime.now(), is_ambiguous=True)
        if kind == "reject":
            return OrderResult(oid, order.symbol, order.side, order.quantity, False, "rejected", datetime.now())
        if kind == "accept_noid":
            return OrderResult("", order.symbol, order.side, order.quantity, True, "ok", datetime.now())
        return OrderResult(oid, order.symbol, order.side, order.quantity, True, "ok", datetime.now())
    def get_order_status(self, order_id, symbol):
        self.calls.append(("status", symbol, order_id))
        st = self.status.get(order_id, BrokerOrderStatus.UNKNOWN)
        if isinstance(st, Exception): raise st
        return BrokerOrder(order_id, symbol, st)
    def get_order_status_evidence(self, order_id, symbol):
        return OrderStatusEvidence(broker_order=self.get_order_status(order_id, symbol))
    def get_open_orders(self, s): return []
    def get_daily_prices(self, s, d): return []
    def get_weekly_prices(self, s, w): return []
    def get_minute_bars(self, s, tick_scope=3, count=40): return []

def snapshot(psm, intents, journal_records, pending_buy, pending_sell, calls):
    out = {}
    for sym, st in sorted(psm._states.items()):
        d = {k: v for k, v in vars(st).items() if not isinstance(v, datetime) and k not in ("blocked_count",)}
        d.update({k: (v is not None) for k, v in vars(st).items() if isinstance(v, datetime) or v is None and k.endswith(("_at", "_since"))})
        out[sym] = d
    return {"psm": out, "intents": sorted(intents),
            "journal": {s: (r.side, r.order_id, r.base_quantity_before_order, r.target_quantity_after_order,
                            r.lifecycle_kind, r.first_fill_at is not None, r.orphaned_at is not None)
                        for s, r in sorted(journal_records.items())},
            "pending_buy": sorted(pending_buy), "pending_sell": sorted(pending_sell), "calls": list(calls)}
