"""시나리오: 각 단계는 ("buy", sym, qty) / ("sell", sym, qty, forced) / ("set", sym, qty) /
("sync",) / ("age", sym, seconds) / ("status", "order_seq", STATUS) / ("next", kind) / ("ack_error", sym, qty)
/ ("ack_orphan", sym) / ("restart",)"""
SCENARIOS = {
 "buy_full_fill": [("sync",), ("buy","005930",10), ("sync",), ("set","005930",10), ("sync",), ("sync",)],
 "buy_partial_then_full": [("sync",), ("buy","005930",353), ("set","005930",146), ("sync",), ("sync",), ("set","005930",353), ("sync",)],
 "buy_with_existing": [("set","005930",100), ("sync",), ("buy","005930",50), ("sync",), ("set","005930",150), ("sync",)],
 "buy_reject": [("sync",), ("next","reject"), ("buy","005930",10), ("sync",)],
 "buy_ambiguous_blocks_account": [("sync",), ("next","ambiguous"), ("buy","005930",10), ("sync",), ("buy","000660",5), ("sell","005930",1,True), ("ack_error","005930",0), ("sync",), ("buy","000660",5), ("sync",)],
 "buy_accept_no_id": [("sync",), ("next","accept_noid"), ("buy","005930",10), ("sync",), ("set","005930",10), ("sync",)],
 "sell_partial_then_full": [("set","047040",353), ("sync",), ("sell","047040",353,False), ("set","047040",343), ("sync",), ("sync",), ("set","047040",0), ("sync",), ("sync",)],
 "sell_reject_then_retry": [("set","005930",10), ("sync",), ("next","reject"), ("sell","005930",10,False), ("sync",), ("sell","005930",10,False), ("sell","005930",10,True), ("sync",)],
 "sell_forced_reject_escalation": [("set","005930",10), ("sync",), ("next","reject"), ("sell","005930",10,True), ("next","reject"), ("sell","005930",10,True), ("sync",)],
 "sell_while_buy_pending": [("sync",), ("buy","005930",10), ("sell","005930",10,True), ("sell","005930",10,False), ("sync",)],
 "status_query_age_gate_and_filled": [("sync",), ("buy","005930",10), ("sync",), ("age","005930",31), ("status",1,"FILLED"), ("set","005930",10), ("sync",), ("sync",)],
 "status_open_no_change": [("sync",), ("buy","005930",10), ("age","005930",31), ("status",1,"OPEN"), ("sync",), ("sync",)],
 "status_exception": [("sync",), ("buy","005930",10), ("age","005930",31), ("status",1,"RAISE"), ("sync",)],
 "status_budget_two_symbols": [("set","005930",10), ("set","000660",5), ("sync",), ("sell","005930",10,False), ("sell","000660",5,False), ("age","005930",35), ("age","000660",40), ("sync",), ("sync",)],
 "sell_filled_status_balance_zero": [("set","005930",10), ("sync",), ("sell","005930",10,False), ("age","005930",31), ("status",1,"FILLED"), ("set","005930",0), ("sync",)],
 "restart_with_pending": [("sync",), ("buy","005930",10), ("sync",), ("restart",), ("sync",), ("buy","000660",5), ("sell","005930",1,True)],
 "pending_timeout_orphan": [("sync",), ("buy","005930",10), ("sync",), ("stale","005930"), ("sync",), ("sync",), ("buy","005930",1), ("ack_orphan","005930"), ("sync",), ("buy","005930",1)],
 "sell_timeout_orphan_then_flat": [("set","005930",10), ("sync",), ("sell","005930",10,False), ("stale","005930"), ("sync",), ("set","005930",0), ("sync",), ("sync",)],
}
