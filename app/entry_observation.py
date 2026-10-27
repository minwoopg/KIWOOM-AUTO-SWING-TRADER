from __future__ import annotations

"""주문 미허용 승인 후보의 장초반 가격 관찰 (H1 — GPT 10/27 ③).

실행기는 주문 허용 종목(`swing_strategy.allowed_symbols`) 밖의 승인 후보를 NOT_ALLOWED로 건너뛰고 가격을 보지 않아,
관찰을 25종목으로 넓혀도 "그 후보가 09:05~09:30에 실제로 진입 조건을 만족했는지" 자료가 쌓이지 않음. 이 모듈은 그
후보들의 현재가·최우선 매도호가를 진입 시간 동안 **관찰만** 해서 따로 기록함.

- 주문·신호 키와 무관: 의도를 만들지 않고, 신호 키(SignalKeyStore)를 읽거나 쓰지 않음 → 나중에 허용 종목이 바뀌어도
  그 신호가 "이미 쓴 신호"로 막히지 않음.
- 판정 기준은 실제 주문 직전 재확인과 같음(`entry_reasons`): 현재가 > 손절 기준, 현재가 ≤ 진입 상한, 최우선 매도호가 ≤ 지정가.
- 순서(H2 — GPT 10/27): 실행기가 그 폴링의 주문 의도를 **모두 처리(전송)한 뒤** `after_tick`에서 관찰 — 주문 의도
  반환 경로에 끼지 않음. 보유·미해결 주문이 있어도 관찰은 계속.
- 조회 한도: 종목마다 최소 간격(기본 55초 — 폴링 60초마다 한 번), 한 폴링에 최대 5종목(오래 안 본 종목부터),
  한 폴링 전체 15초 예산(U2: monotonic·조회를 작업 스레드에서 남은 예산만큼만 기다림 — 진행 중 조회도 끊음),
  진입 마감이 지나면 조회하지 않음.
- 시각: 종목마다 요청 시작(requested_at)·응답 수신(received_at)을 실제 시계로 기록. 마감 뒤 받은 값은 in_window=false —
  조건을 만족해도 최초 충족(OBS_FIRST_OK)·충족 횟수로 세지 않음.
- 실패해도 매매에 영향 없음: 조회 예외는 QUOTE_FAILED로 기록, 기록 파일 쓰기 실패는 삼킴.

기록(`data/entry_observation.jsonl`, append-only):
- OBS_QUOTE: at(=received_at)·requested_at·received_at·trade_date·symbol·signal_id·signal_date·price·ask·limit·stop_ref·
  entry_cap·ok·in_window·reasons·stop_dist_pct((현재가 − 손절 기준)/현재가)·cap_gap_pct((현재가 − 상한)/상한)·quote_ms
- OBS_FIRST_OK: 그 신호가 처음 조건을 모두 만족한 관찰(재시작하면 다시 남을 수 있음 — 요약은 가장 이른 것)
"""

import json
import threading
import time as _time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

OBS_QUOTE, OBS_FIRST_OK = "OBS_QUOTE", "OBS_FIRST_OK"
REASON_LABEL = {"QUOTE_TIMEOUT": "관찰 예산 초과", "NO_PRICE": "현재가 없음", "PRICE_AT_OR_BELOW_STOP": "현재가 ≤ 손절 기준", "PRICE_ABOVE_CAP": "현재가 > 진입 상한",
                "NO_ASK": "매도호가 없음", "ASK_ABOVE_LIMIT": "매도호가 > 지정가", "QUOTE_FAILED": "조회 실패"}


def entry_reasons(price, ask, c) -> list[str]:
    """주문 직전 재확인(S1EntryStrategy)과 같은 기준 — 빈 목록이면 진입 조건 충족."""
    why = []
    if not price:
        why.append("NO_PRICE")
    else:
        if price <= c.stop_ref:
            why.append("PRICE_AT_OR_BELOW_STOP")
        if price > c.entry_cap:
            why.append("PRICE_ABOVE_CAP")
    if not ask:
        why.append("NO_ASK")
    elif ask > c.limit_price:
        why.append("ASK_ABOVE_LIMIT")
    return why


def observation_path(state_file: str | Path) -> Path:
    return Path(state_file).with_name("entry_observation.jsonl")


class EntryObserver:
    MIN_INTERVAL_SEC = 55
    MAX_PER_TICK = 5
    BUDGET_SEC = 15.0          # 한 폴링 관찰 전체 시간 예산 — **실제 최대 대기**(U2: 진행 중인 조회도 이 안에서 끊음)

    def __init__(self, quote: Callable[[str], tuple[int | None, int | None]], sink: Callable[[dict], None], *,
                 mode: str = "", clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = _time.monotonic, budget_sec: float | None = None) -> None:
        self.quote, self.sink, self.mode, self.clock, self.monotonic = quote, sink, mode, clock, monotonic
        self.budget_sec = self.BUDGET_SEC if budget_sec is None else budget_sec
        self._last: dict[str, datetime] = {}          # signal_id → 마지막 조회 요청 시각
        self._first_ok: set[str] = set()
        self._stray: threading.Thread | None = None    # 예산 초과로 기다리지 않고 남겨 둔 조회(끝날 때까지 새 관찰 안 함)

    def _write(self, rec: dict) -> None:
        try:
            self.sink(rec)
        except Exception:                                  # noqa: BLE001 — 기록 실패가 매매를 멈추지 않음
            pass

    def busy(self) -> bool:
        return self._stray is not None and self._stray.is_alive()

    def observe(self, trade_date: date, cands: Iterable, *, window_end: datetime,
                clock: Callable[[], datetime] | None = None) -> int:
        """cands: 관찰할 승인 후보(ApprovedCandidate). window_end: 진입 마감(그 뒤엔 조회하지 않고, 그 뒤 받은 값은
        시간 안 충족으로 세지 않음). clock: 실제 시각(실행기 시계 — 기록용). 반환: 이번에 조회를 끝낸 수.

        시간 예산(U2 — GPT 10/27 ③): 경과는 monotonic으로 재고(시스템 시각 변경 영향 없음), 조회 하나하나를 작업 스레드에서
        돌려 **남은 예산만큼만** 기다림. 예산이 끝나도 안 끝난 조회는 기다리지 않고(QUOTE_TIMEOUT 기록) 그 폴링 관찰을 멈춤 —
        그 조회는 키움 HTTP 시간 제한(호출당 10초) 안에 스스로 끝나며, 끝날 때까지 다음 관찰을 시작하지 않음(동시 조회 1개 이하).
        늦게 온 값은 쓰지 않음."""
        clk = clock or self.clock
        if self.busy():
            return 0                                       # 지난 폴링의 남은 조회가 아직 진행 중 — 이번 관찰은 건너뜀
        self._stray = None
        start = clk()
        deadline = self.monotonic() + self.budget_sec
        due = []
        for c in cands:
            last = self._last.get(c.signal_id)
            if last is None or (start - last).total_seconds() >= self.MIN_INTERVAL_SEC:
                due.append((last or datetime.min, c.symbol, c))
        due.sort(key=lambda x: (x[0], x[1]))
        n = 0
        for _, _, c in due[: self.MAX_PER_TICK]:
            req = clk()
            left = deadline - self.monotonic()
            if req >= window_end or left <= 0:
                break                                      # 마감 지남·예산 소진 — 남은 종목은 다음 폴링(오래 안 본 순)
            self._last[c.signal_id] = req
            box: dict = {}

            def _run(sym=c.symbol, box=box):
                try:
                    box["v"] = self.quote(sym)
                except Exception as exc:                   # noqa: BLE001
                    box["e"] = f"{type(exc).__name__}: {exc}"
            th = threading.Thread(target=_run, name=f"entry-observe-{c.symbol}", daemon=True)
            th.start()
            th.join(timeout=max(0.0, left))
            recv = clk()
            base = {"at": recv.isoformat(timespec="seconds"), "requested_at": req.isoformat(timespec="seconds"),
                    "received_at": recv.isoformat(timespec="seconds"), "trade_date": trade_date.isoformat(),
                    "event": OBS_QUOTE, "mode": self.mode, "observe_only": True, "symbol": c.symbol,
                    "signal_id": c.signal_id, "signal_date": c.signal_date, "limit": c.limit_price,
                    "stop_ref": c.stop_ref, "entry_cap": c.entry_cap, "in_window": recv < window_end}
            if th.is_alive():
                self._stray = th
                self._write({**base, "price": None, "ask": None, "ok": False, "reasons": ["QUOTE_TIMEOUT"],
                             "stop_dist_pct": None, "cap_gap_pct": None, "quote_ms": None,
                             "error": f"관찰 예산 {self.budget_sec:g}초 초과 — 기다리지 않음(늦게 온 값은 버림)"})
                break
            err = box.get("e")
            price, ask = (None, None) if err else box.get("v", (None, None))
            reasons = ["QUOTE_FAILED"] if err else entry_reasons(price, ask, c)
            ok = not reasons
            rec = {**base, "price": price, "ask": ask, "ok": ok, "reasons": reasons,
                   "stop_dist_pct": round((price - c.stop_ref) / price * 100, 3) if price else None,
                   "cap_gap_pct": round((price - c.entry_cap) / c.entry_cap * 100, 3) if price and c.entry_cap else None,
                   "quote_ms": int((recv - req).total_seconds() * 1000)}
            if err:
                rec["error"] = err
            self._write(rec)
            if ok and rec["in_window"] and c.signal_id not in self._first_ok:
                self._first_ok.add(c.signal_id)
                self._write({**rec, "event": OBS_FIRST_OK})
            n += 1
        return n


def read_day(path: Path, day: date) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("trade_date") == day.isoformat():
            out.append(r)
    return out


def summarize(records: Iterable[dict]) -> list[dict]:
    """신호별 요약: 관찰 수·첫/마지막 관찰·최초 조건 충족 시각·그때 가격과 손절 거리·사유별 횟수·가격 범위·조회 실패."""
    by: dict[str, dict] = {}
    for r in sorted((r for r in records if r.get("event") == OBS_QUOTE), key=lambda r: r["at"]):
        s = by.setdefault(r["signal_id"], {"symbol": r["symbol"], "signal_id": r["signal_id"],
                                           "signal_date": r.get("signal_date"), "limit": r.get("limit"),
                                           "stop_ref": r.get("stop_ref"), "entry_cap": r.get("entry_cap"), "n": 0,
                                           "first_at": r["at"], "last_at": r["at"], "first_ok": None, "ok_n": 0,
                                           "reasons": Counter(), "prices": [], "failed": 0})
        s["n"] += 1
        s["last_at"] = r["at"]
        if r.get("price"):
            s["prices"].append(r["price"])
        if r.get("ok") and r.get("in_window", True):
            s["ok_n"] += 1
            if s["first_ok"] is None:
                s["first_ok"] = {"at": r["at"], "price": r.get("price"), "ask": r.get("ask"),
                                 "stop_dist_pct": r.get("stop_dist_pct")}
        for x in r.get("reasons") or []:
            s["reasons"][x] += 1
        s["failed"] += "QUOTE_FAILED" in (r.get("reasons") or [])
        s["late"] = s.get("late", 0) + (not r.get("in_window", True))
    out = []
    for s in sorted(by.values(), key=lambda s: (s["signal_date"] or "", s["symbol"])):
        ps = s.pop("prices")
        out.append({**s, "reasons": dict(s["reasons"].most_common()), "low": min(ps) if ps else None,
                    "high": max(ps) if ps else None})
    return out


def report_lines(path: Path, day: date) -> list[str]:
    rows = summarize(read_day(path, day))
    if not rows:
        return []
    L = ["", "주문 미허용 승인 후보 장초반 관찰(관찰만 — 주문·신호 키 없음):"]
    for s in rows:
        fo = s["first_ok"]
        ok = (f"최초 조건 충족 {fo['at'][11:16]} 현재가 {fo['price']:,}·매도호가 {fo['ask']:,}·손절까지 {fo['stop_dist_pct']:.2f}%"
              if fo else "조건 충족 없음")
        rs = ", ".join(f"{REASON_LABEL.get(k, k)} {v}" for k, v in s["reasons"].items()) or "-"
        rng = f"{s['low']:,}~{s['high']:,}" if s["low"] is not None else "-"
        late = f" · 마감 뒤 수신 {s['late']}회(충족으로 안 셈)" if s.get("late") else ""
        L.append(f"- {s['symbol']} (신호일 {s['signal_date']}, 지정가 {s['limit']:,}·손절 기준 {s['stop_ref']:,.0f}): "
                 f"관찰 {s['n']}회 {s['first_at'][11:16]}~{s['last_at'][11:16]}, 충족 {s['ok_n']}회 · {ok} · "
                 f"가격 {rng} · 미충족 사유 {rs}{late}")
    return L
