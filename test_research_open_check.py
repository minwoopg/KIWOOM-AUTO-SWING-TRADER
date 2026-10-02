# -*- coding: utf-8 -*-
"""A5-1: 다음 거래일 개장 + N분 가격 기록 회귀 테스트 (가짜 데이터·임시 DB, 네트워크·주문 없음).

GPT 재검토 71b78e3 지시: 대상 계약 하나·PASS·final=1·개장 전 확보 후보만 / 후보 목록 확정(signal_id·run_id·계약) /
계약 변경 시 섞지 않음 / 가격 기준 변경이면 상한 비교 보류 / 개장 + 5분(특수 개장일 포함) / 늦으면 실제 조회 시각으로,
정규장 뒤면 누락 / 정상·실패·지연·상한 초과·거래 불가 구분 / 재시작해도 중복 없음 / 관찰 가격 ≠ 체결.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.research.universe import classify_rows, summarize
from infra.research import open_check as A5
from infra.research.kiwoom_readonly import (PRICE_API, RESEARCH_API, Body, ReadOnlyResearchClient, ResearchApiError,
                                            ResearchConfigError)
from infra.research.kiwoom_rows import INDEX_DAILY, STOCK_DAILY, RawBar
from infra.research.research_config import ResearchSettingsError, load_research_settings
from infra.research.s1_scanner import S1Scanner, build_contract
from infra.research.scan_store import ScanStore
from infra.research.store import AdjustmentBasis, FetchedBar, ResearchStore
from utils.trading_calendar import TradingCalendar

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


CAL = TradingCalendar.load()
TMP = Path(tempfile.mkdtemp(prefix="research_a5_"))
T = date(2026, 9, 30)                        # 신호일
D = date(2026, 10, 1)                        # 대상 거래일
SESS = CAL.trading_days_in_range(date(2026, 1, 1), date(2026, 10, 8))
UPTO_T = [d for d in SESS if d <= T]
RECV = datetime(2026, 9, 30, 19, 0)
SCAN = datetime(2026, 9, 30, 19, 30)
TARGET = datetime(2026, 10, 1, 9, 5)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def stock_bar(d, c, up=0.01, dn=0.01, vol=100_000, tv=5_000):
    c = int(round(c))
    return RawBar(d, c, int(c * (1 + up)), int(c * (1 - dn)), c, vol, tv, "" if vol else "NO_TRADES")


def pass_bars(days, *, drift=1.003, pullback=(0.99, 0.98, 0.97, 0.965), recover=1.03, base=10_000):
    n = len(days) - len(pullback) - 2
    closes = [base * drift ** i for i in range(n)]
    out = [stock_bar(days[i], c) for i, c in enumerate(closes)]
    cp = closes[-1] * 1.03
    out.append(stock_bar(days[n], cp))
    for k, f in enumerate(pullback):
        out.append(stock_bar(days[n + 1 + k], cp * f, up=0.005, dn=0.005))
    out.append(stock_bar(days[-1], out[-1].close_raw * recover))
    return out


def down_bars(days):
    return [stock_bar(d, 20_000 * 0.997 ** i) for i, d in enumerate(days)]


def index_bars(days, rate=1.001, base=2500.0):
    return [RawBar(d, int(base * rate ** i * 100), int(base * rate ** i * 100.5), int(base * rate ** i * 99.5),
                   int(base * rate ** i * 100), 500_000, 9_000_000, "") for i, d in enumerate(days)]


def put(st, sid, bars, *, recv=RECV, kind="STOCK"):
    st.init_series(spec=STOCK_DAILY if kind == "STOCK" else INDEX_DAILY, series_id=sid, code=sid.split(":")[-1],
                   bars=[FetchedBar(b, recv) for b in bars],
                   basis=AdjustmentBasis("1" if kind == "STOCK" else None, recv.strftime("%Y%m%d")),
                   activated_at=recv, job_id=None, required_from=None, coverage="OK", coverage_detail="", now=recv)


def lrow(code, name, mc="0"):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": "정상", "regDay": "20000101",
            "lastPrice": "00010000", "state": "증거금40%|담보대출|신용가능", "marketCode": mc, "marketName": "x",
            "upName": "", "upSizeName": "", "companyClassName": "", "orderWarning": "0", "nxtEnable": "N", "kind": "A"}


PASS_CODES = [f"0001{i}0" for i in range(8)]            # 000100 … 000170
CLOSES: dict[str, int] = {}


def build(name: str, *, all_down: bool = False) -> ResearchStore:
    st = ResearchStore(TMP / f"{name}.sqlite3")
    put(st, "INDEX:KOSPI:001", index_bars(UPTO_T), kind="INDEX")
    put(st, "INDEX:KOSDAQ:101", index_bars(UPTO_T, base=850.0), kind="INDEX")
    rows = []
    for i, code in enumerate(PASS_CODES):
        bars = down_bars(UPTO_T) if all_down else pass_bars(UPTO_T, base=10_000 + 2_000 * i)
        put(st, f"STOCK:{code}", bars)
        if not all_down:
            CLOSES[code] = bars[-1].close_raw
        rows.append(lrow(code, f"후보{i}"))
    put(st, "STOCK:000200", down_bars(UPTO_T))
    put(st, "STOCK:000500", pass_bars(UPTO_T, base=30_000)[:-1])      # 9/29까지만 — 스캔 때 STALE
    rows += [lrow("000200", "하락"), lrow("000500", "늦은봉")]
    recs = classify_rows(rows)
    st.save_snapshot(snapshot_date=T, observed_at=datetime(2026, 9, 30, 18, 20),
                     observed_end_at=datetime(2026, 9, 30, 18, 20, 5), market_phase="POST_CLOSE",
                     policy_version="u2:test", source="test", records=recs, raw_pages=[], summary=summarize(recs))
    return st


class FakePrice:
    """시세 가짜 응답(ka10001 기본정보·ka10003 체결·ka10004 호가 — 10/2 실측 응답 모양). 호출마다 시계를 1초 진행.
    spec[symbol] = dict | 'FAIL' | 'PARSE' | 'CRASH'. dict에 '_extra': 'FAIL'이면 체결 조회 실패·호가 필드 없음."""

    def __init__(self, clock: Clock, spec: dict):
        self.clock, self.spec, self.calls = clock, spec, []

    def fetch_body(self, api_id, payload):
        sym = payload["stk_cd"]
        self.calls.append((api_id, sym, self.clock.t))
        req = self.clock.t
        self.clock.t += timedelta(seconds=1)
        v = self.spec[sym]
        if v == "FAIL":
            raise ResearchApiError(f"{api_id} {payload}: 재시도 4회 후에도 실패 — HTTP 429")
        if v == "CRASH":
            raise KeyboardInterrupt
        if v == "PARSE":
            return Body({"return_code": 0, "stk_cd": sym}, req, req + timedelta(milliseconds=300), 1)
        v = dict(v)
        extra = v.pop("_extra", None)
        rcv = req + timedelta(milliseconds=300)
        if api_id == "ka10003":
            if extra == "FAIL":
                raise ResearchApiError("ka10003: HTTP 500")
            t1 = (req - timedelta(seconds=1)).strftime("%H%M%S")
            return Body({"return_code": 0, "cntr_infr": [
                {"tm": t1, "cur_prc": v["cur_prc"], "stex_tp": "KRX", "acc_trde_qty": v["trde_qty"]},
                {"tm": "090000", "cur_prc": v["cur_prc"], "stex_tp": "KRX"}]}, req, rcv, 1)
        if api_id == "ka10004":
            if extra == "FAIL":
                return Body({"return_code": 0}, req, rcv, 1)
            p = int(v["cur_prc"].lstrip("+-"))
            return Body({"return_code": 0, "bid_req_base_tm": req.strftime("%H%M%S"), "sel_fpr_bid": f"+{p + 50}",
                         "buy_fpr_bid": f"-{p}", "sel_fpr_req": "100", "buy_fpr_req": "200", "sel_1th_pre_req_pre": "--33"},
                        req, rcv, 1)
        return Body({"return_code": 0, "stk_cd": sym, "token": "SECRET-TOKEN", **v}, req, rcv,
                    2 if sym == PASS_CODES[0] else 1)


def quote(price, base, *, vol=1000, upl=None):
    return {"cur_prc": f"+{price}", "base_pric": str(base), "open_pric": f"+{price}", "high_pric": f"+{price}",
            "low_pric": f"-{price}", "upl_pric": f"+{upl or int(base * 1.3)}", "lst_pric": f"-{int(base * 0.7)}",
            "trde_qty": str(vol)}


# ── 1. 설정·목표 시각 ────────────────────────────────────────
cfg_dir = TMP / "cfg"
cfg_dir.mkdir()


def cfg(text: str) -> Path:
    p = cfg_dir / f"r{len(list(cfg_dir.iterdir()))}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


bad = 0
for t in ("s1:\n  active_contract: abc\n", "s1:\n  active_contract: current\n  extra: 1\n",
          "a5:\n  open_check:\n    offset_min: 90\n", "a5:\n  open_check:\n    offset_min: true\n", "x: 1\n"):
    try:
        load_research_settings(cfg(t))
    except ResearchSettingsError:
        bad += 1
ok_cfg = load_research_settings(cfg("s1:\n  active_contract: 0123456789ab\n"))
dflt = load_research_settings()
check("1-1) 연구 설정: 계약은 current 또는 12자리 해시, 모르는 키·범위 밖·bool은 오류(fail-closed). 저장소 기본 설정 = current·"
      "개장+5분·허용 120초",
      bad == 5 and ok_cfg.active_contract == "0123456789ab" and dflt.active_contract == "current"
      and (dflt.open_check.offset_min, dflt.open_check.on_time_tolerance_sec) == (5, 120))
st = build("main")
ss = ScanStore(TMP / "main_scans.sqlite3")
os_ = A5.OpenCheckStore(TMP / "main_a5.sqlite3")
sc = S1Scanner(st, ss, CAL)
chk = A5.OpenChecker(os_, ss, st, CAL, contract_hash=sc.contract_hash)
errs = 0
try:
    chk.target_at(date(2026, 10, 5))
except A5.OpenCheckError:
    errs += 1
check("1-2) 목표 시각 = 달력의 개장 + 5분: 일반 거래일 09:05, 특수 개장일(1/2 10:00 개장) 10:05, 휴장일은 오류, "
      "offset 0이면 개장 시각",
      chk.target_at(D) == TARGET and chk.target_at(date(2026, 1, 2)) == datetime(2026, 1, 2, 10, 5) and errs == 1
      and A5.OpenChecker(os_, ss, st, CAL, contract_hash="x", offset_min=0).target_at(D) == datetime(2026, 10, 1, 9, 0))
check("1-3) 대상 계약 'current' = 스캐너 기본 계약과 같은 함수(build_contract)",
      build_contract(CAL)[1] == sc.contract_hash)

# ── 2. 후보 선택·확정 ────────────────────────────────────────
main_run = sc.run(SCAN, now=Clock(datetime(2026, 9, 30, 19, 31)))
other = S1Scanner(st, ss, CAL, after_close=timedelta(minutes=150))          # 다른 계약 — 같은 종목 PASS
other.run(SCAN, now=Clock(datetime(2026, 9, 30, 19, 32)))
legacy = S1Scanner(st, ss, CAL, after_close=timedelta(minutes=155))
leg_run = legacy.run(SCAN, now=Clock(datetime(2026, 9, 30, 19, 33)))
ss.conn.execute("UPDATE scan_run SET contract_hash=NULL WHERE run_id=?", (leg_run["run_id"],))     # 계약 기록 전 흉내
ss.conn.execute("UPDATE s1_observation SET contract_hash=NULL WHERE contract_hash=?", (legacy.contract_hash,))
st.append_forward(series_id="STOCK:000500", bars=[FetchedBar(pass_bars(UPTO_T, base=30_000)[-1],
                                                           datetime(2026, 10, 1, 9, 20))],
                  verified_base_dt="20261001", verified_at=datetime(2026, 10, 1, 9, 20))
post = sc.run(datetime(2026, 10, 1, 9, 30), now=Clock(datetime(2026, 10, 1, 9, 31)))   # 개장 뒤 스캔
obs500 = {o["symbol"]: o for o in ss.observations(T.isoformat(), contract_hash=sc.contract_hash)}["000500"]
draft, cands = A5.select_candidates(ss, st, CAL, D, sc.contract_hash)
cs = {c["symbol"]: c for c in cands}
E = {e["symbol"]: e for e in main_run["evals"]}
check("2-0) 시험 준비: 대상 계약 스캔 PASS 8 · 다른 계약도 같은 8종목 PASS · 개장 뒤 스캔으로 000500이 PASS·확정(actionable=0)",
      sum(e["eligible_signal"] == "PASS" for e in main_run["evals"]) == 8
      and sum(e["eligible_signal"] == "PASS" for e in other.compute(SCAN)["evals"]) == 8
      and obs500["eligible_signal"] == "PASS" and obs500["final"] == 1 and obs500["actionable"] == 0)
check("2-1) [GPT] 후보 = 대상 계약의 직전 거래일 대표 기록 중 PASS·final=1·개장 전 확보만 — 다른 계약·계약 기록 전·FAIL·"
      "개장 뒤 확정(000500)은 제외, 종목당 하나",
      draft["status"] == "OK" and draft["signal_date"] == "2026-09-30" and set(cs) == set(PASS_CODES)
      and len(cands) == 8 and all(c["contract_hash"] == sc.contract_hash for c in cands)
      and draft["source"]["runs"] == [main_run["run_id"]])
sig7 = f"S1|{sc.strategy}|c:{sc.contract_hash}|{PASS_CODES[7]}|2026-09-30"
ss.conn.execute("UPDATE s1_observation SET final=0 WHERE signal_id=?", (sig7,))          # 확정 아님(가정)
_d, cands_nf = A5.select_candidates(ss, st, CAL, D, sc.contract_hash)
ss.conn.execute("UPDATE s1_observation SET final=1 WHERE signal_id=?", (sig7,))
check("2-1b) PASS여도 대표 기록이 확정(final=1)이 아니면 후보 아님",
      {c["symbol"] for c in cands_nf} == set(PASS_CODES[:7]))
c0 = cs[PASS_CODES[0]]
check("2-2) 후보에 signal_id·run_id·계약·입력 해시·진입 상한·참고 손절가·신호일 종가·revision 저장",
      c0["signal_id"] == f"S1|{sc.strategy}|c:{sc.contract_hash}|{PASS_CODES[0]}|2026-09-30"
      and c0["run_id"] == main_run["run_id"] and c0["input_hash"] == E[PASS_CODES[0]]["input_hash"]
      and c0["entry_cap"] == E[PASS_CODES[0]]["result"]["levels"]["entry_cap"]
      and c0["stop_ref"] == E[PASS_CODES[0]]["result"]["levels"]["stop_ref"]
      and c0["signal_close"] == CLOSES[PASS_CODES[0]] and c0["stock_revision"] == 1)

# 가격 시나리오: 후보마다 다른 결과
L = {c: (E[c]["result"]["levels"]["entry_cap"], E[c]["result"]["levels"]["stop_ref"]) for c in PASS_CODES}
spec = {
    PASS_CODES[0]: quote(int(L[PASS_CODES[0]][0]) - 1, CLOSES[PASS_CODES[0]]),                      # 상한 이내
    PASS_CODES[1]: {**quote(int(L[PASS_CODES[1]][0]) + 50, CLOSES[PASS_CODES[1]]), "_extra": "FAIL"},  # 상한 초과
    PASS_CODES[2]: quote(int(L[PASS_CODES[2]][1]) - 10, CLOSES[PASS_CODES[2]]),                     # 손절가 이하
    PASS_CODES[3]: quote(CLOSES[PASS_CODES[3]] // 5, CLOSES[PASS_CODES[3]] // 5),                    # 액면분할(기준 변경)
    PASS_CODES[4]: quote(CLOSES[PASS_CODES[4]], CLOSES[PASS_CODES[4]], vol=0),                       # 거래량 0
    PASS_CODES[5]: quote(int(CLOSES[PASS_CODES[5]] * 1.3), CLOSES[PASS_CODES[5]],
                         upl=int(CLOSES[PASS_CODES[5]] * 1.3)),                                       # 상한가
    PASS_CODES[6]: "FAIL",
    PASS_CODES[7]: "PARSE",
}
clk = Clock(TARGET)
os2 = A5.OpenCheckStore(TMP / "run_a5.sqlite3")
chk2 = A5.OpenChecker(os2, ss, st, CAL, contract_hash=sc.contract_hash, on_time_tolerance_sec=9)
fake = FakePrice(clk, spec)
res = chk2.run(D, client=fake, now=clk)
R = {r["symbol"]: r for r in res["checks"]}
check("2-3) [GPT] 첫 실행이 후보 목록을 확정(D마다 하나, 규칙·원천 스캔 실행 기록) — 그 뒤 실행은 다시 고르지 않음",
      res["set"]["status"] == "OK" and res["set"]["count"] == 8 and res["set"]["contract_hash"] == sc.contract_hash
      and res["set"]["selected_at"] == "2026-10-01T09:05:00" and json.loads(
          os2.conn.execute("SELECT source_json FROM candidate_set").fetchone()[0])["runs"] == [main_run["run_id"]]
      and "PASS" in res["set"]["rule"])

# ── 3. 가격 확인·판정 ────────────────────────────────────────
check("3-1) [GPT] 판정 구분: 상한 이내 / 상한 초과 / 손절가 이하 / 가격 기준 변경(기준가 ≠ 신호일 종가 — 상한 비교 보류) / "
      "거래 불가(거래량 0·상한가) / 조회 실패 / 응답 필드 없음",
      [R[c]["outcome"] for c in PASS_CODES] == ["WITHIN_CAP", "ABOVE_CAP", "BELOW_STOP", "BASIS_CHANGED",
                                                 "NOT_TRADABLE", "NOT_TRADABLE", "FETCH_FAILED", "PARSE_FAILED"]
      and R[PASS_CODES[4]]["outcome_detail"].startswith("ZERO_VOLUME")
      and R[PASS_CODES[5]]["outcome_detail"].startswith("AT_UPPER_LIMIT")
      and R[PASS_CODES[3]]["gap_vs_cap"] is not None and "보류" in R[PASS_CODES[3]]["outcome_detail"]
      and [R[c]["fetch_status"] for c in PASS_CODES[5:]] == ["OK", "FETCH_FAILED", "PARSE_FAILED"])
r0 = R[PASS_CODES[0]]
check("3-2) [GPT] 요청·수신 시각·시도 횟수·지연(초) 기록. 허용(9초) 안은 ON_TIME, 넘으면 LATE — 실제 조회 시각 그대로",
      r0["requested_at"] == "2026-10-01T09:05:00" and r0["received_at"] == "2026-10-01T09:05:00" and r0["attempts"] == 2
      and [R[c]["timing"] for c in PASS_CODES] == ["ON_TIME"] * 4 + ["LATE"] * 4
      and [R[c]["lateness_sec"] for c in PASS_CODES] == [0, 3, 6, 9, 12, 15, 18, 19]
      and R[PASS_CODES[7]]["requested_at"] == "2026-10-01T09:05:19")
x0, x1 = json.loads(r0["extra_json"]), json.loads(R[PASS_CODES[1]]["extra_json"])
check("3-2b) [GPT·10/2 실측] 원천 가격 시각: ka10001 바로 뒤 ka10003 최근 KRX 체결 tm·가격(지연 초), ka10004 호가 기준 시각·"
      "최우선 매도/매수호가 기록. 보조 조회가 실패해도 판정은 ka10001 기준 그대로(상태만 기록), 조회 실패·필드 없음엔 보조 조회 안 함",
      r0["source_time"] == "2026-10-01T09:05:00" and r0["source_price"] == r0["observed_price"]
      and r0["source_exchange"] == "KRX" and r0["source_lag_sec"] == 1
      and r0["best_ask"] == r0["observed_price"] + 50 and r0["best_bid"] == r0["observed_price"]
      and r0["quote_time"] == "2026-10-01T09:05:02" and x0["ka10003"]["status"] == "OK" and x0["ka10004"]["status"] == "OK"
      and R[PASS_CODES[1]]["outcome"] == "ABOVE_CAP" and R[PASS_CODES[1]]["source_time"] is None
      and R[PASS_CODES[1]]["best_ask"] is None and x1["ka10003"]["status"] == "FETCH_FAILED"
      and x1["ka10004"]["status"] == "PARSE_FAILED"
      and all(R[c]["extra_json"] is None for c in PASS_CODES[6:])
      and [a for a, s_, _t in fake.calls if s_ == PASS_CODES[6]] == ["ka10001"])
check("3-3) [GPT] 관찰 가격 ≠ 체결: 가정 체결가격은 상한 이내일 때만 관찰가 + '가정 — 실제 체결 아님' 규칙, 나머지는 비움. "
      "기록·보고서 어디에도 체결(FILLED) 표시 없음",
      r0["assumed_fill_price"] == r0["observed_price"] and "실제 체결 아님" in r0["assumed_fill_rule"]
      and all(R[c]["assumed_fill_price"] is None for c in PASS_CODES[1:])
      and "FILLED" not in json.dumps(res["checks"], ensure_ascii=False))
check("3-4) 응답 본문 보존(가격 원천 필드 확인용) — 토큰처럼 보이는 값은 가림. 선택 필드(시가·상한가·하한가·거래량) 기록",
      json.loads(r0["body_json"])["token"] == "***" and json.loads(r0["body_json"])["cur_prc"].startswith("+")
      and r0["volume"] == 1000 and r0["upper_limit"] and r0["lower_limit"] and r0["open_price"] == r0["observed_price"])
n_calls = len(fake.calls)
res2 = chk2.run(D, client=fake, now=Clock(datetime(2026, 10, 1, 9, 30)))
check("3-5) [GPT] 같은 후보·같은 확인을 다시 실행해도 조회·저장 없음(중복 없음), 실행 기록만 남음",
      len(fake.calls) == n_calls and len(res2["checks"]) == 8
      and os2.conn.execute("SELECT COUNT(*) FROM price_check").fetchone()[0] == 8
      and [r["status"] for r in os2.runs(D)] == ["COMPLETE", "COMPLETE"])

ss.conn.execute("UPDATE s1_observation SET eligible_signal='FAIL' WHERE signal_id=?", (sig7,))   # 확정 뒤 스캔 DB가 바뀌어도
res2b = chk2.run(D, client=fake, now=Clock(datetime(2026, 10, 1, 9, 40)))
ss.conn.execute("UPDATE s1_observation SET eligible_signal='PASS' WHERE signal_id=?", (sig7,))
check("3-5b) [GPT] 확정된 후보 목록은 이후 실행에서 그대로 사용 — 확정 뒤 관찰 기록이 바뀌어도 다시 고르지 않음",
      {c["symbol"] for c in res2b["candidates"]} == set(PASS_CODES) and res2b["set"]["count"] == 8
      and len(fake.calls) == n_calls)

# 계약 변경: 이미 확정된 D는 그대로
res3 = A5.OpenChecker(os2, ss, st, CAL, contract_hash=other.contract_hash).run(D, client=fake, now=Clock(datetime(2026, 10, 1, 10, 0)))
check("3-6) [GPT] 대상 계약을 바꿔도 이미 확정된 D의 후보·계약은 그대로(새 계약으로 다시 고르거나 추가하지 않음, 중복 없음)",
      res3["set"]["contract_hash"] == sc.contract_hash and res3["note"].startswith("CONTRACT_CHANGED")
      and os2.conn.execute("SELECT COUNT(*) FROM candidate_set").fetchone()[0] == 1
      and os2.conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 8 and len(fake.calls) == n_calls)

# 목표 시각 전 / 중단 후 재개 / 정규장 뒤 누락
os3 = A5.OpenCheckStore(TMP / "early_a5.sqlite3")
chk3 = A5.OpenChecker(os3, ss, st, CAL, contract_hash=sc.contract_hash)
try:
    chk3.run(D, client=fake, now=Clock(datetime(2026, 10, 1, 9, 4, 59)))
    early = False
except A5.OpenCheckError:
    early = True
check("3-7) [GPT] 목표 시각 전에는 실행 거부 — 후보 확정·조회·기록 모두 없음",
      early and os3.conn.execute("SELECT COUNT(*) FROM candidate_set").fetchone()[0] == 0
      and os3.conn.execute("SELECT COUNT(*) FROM check_run").fetchone()[0] == 0 and len(fake.calls) == n_calls)
clk4 = Clock(TARGET)
crash_spec = {**{c: quote(int(L[c][0]) - 1, CLOSES[c]) for c in PASS_CODES}, PASS_CODES[2]: "CRASH"}
f4 = FakePrice(clk4, crash_spec)
try:
    chk3.run(D, client=f4, now=clk4)
    crashed = False
except KeyboardInterrupt:
    crashed = True
first_two = {r["symbol"]: r["requested_at"] for r in os3.checks("a5_2026-10-01")}
f4.spec[PASS_CODES[2]] = quote(int(L[PASS_CODES[2]][0]) - 1, CLOSES[PASS_CODES[2]])
clk4.t = datetime(2026, 10, 1, 9, 20)
res4 = chk3.run(D, client=f4, now=clk4)
R4 = {r["symbol"]: r for r in res4["checks"]}
check("3-8) [GPT] 확인 도중 중단 → 기록된 후보는 그대로, 실행은 FAILED. 재시작하면 남은 후보만 조회(늦은 실제 시각, LATE)",
      crashed and set(first_two) == set(PASS_CODES[:2]) and [r["status"] for r in os3.runs(D)] == ["FAILED", "COMPLETE"]
      and all(R4[c]["requested_at"] == first_two[c] and R4[c]["timing"] == "ON_TIME" for c in PASS_CODES[:2])
      and all(R4[c]["timing"] == "LATE" and R4[c]["requested_at"] >= "2026-10-01T09:20:00" for c in PASS_CODES[2:])
      and len(R4) == 8 and len(f4.calls) == 7 + 18)
os5 = A5.OpenCheckStore(TMP / "missed_a5.sqlite3")
f5 = FakePrice(Clock(TARGET), spec)
res5 = A5.OpenChecker(os5, ss, st, CAL, contract_hash=sc.contract_hash).run(D, client=f5, now=Clock(datetime(2026, 10, 2, 8, 0)))
check("3-9) [GPT] 다음 날 실행(또는 정규장 뒤) → 조회하지 않고 모두 MISSED(가격 없음, 일봉으로 채우지 않음). 후보는 같은 규칙으로 확정",
      not f5.calls and res5["set"]["count"] == 8
      and all(r["timing"] == "MISSED" and r["outcome"] == "MISSED" and r["fetch_status"] == "NOT_RUN"
              and r["observed_price"] is None for r in res5["checks"]))
os6 = A5.OpenCheckStore(TMP / "close_a5.sqlite3")
clk6 = Clock(datetime(2026, 10, 1, 15, 29, 52))
f6 = FakePrice(clk6, {c: quote(int(L[c][0]) - 1, CLOSES[c]) for c in PASS_CODES})
res6 = A5.OpenChecker(os6, ss, st, CAL, contract_hash=sc.contract_hash).run(D, client=f6, now=clk6)
check("3-10) 정규장 종료(15:30)를 넘기는 실행: 그 전 조회는 LATE로 기록, 종료 뒤 후보는 조회 없이 MISSED",
      [r["timing"] for r in res6["checks"]] == ["LATE"] * 3 + ["MISSED"] * 5 and len(f6.calls) == 9)

# 후보 없음 구분
st_n = build("none", all_down=True)
ss_n = ScanStore(TMP / "none_scans.sqlite3")
os_n = A5.OpenCheckStore(TMP / "none_a5.sqlite3")
sc_n = S1Scanner(st_n, ss_n, CAL)
no_scan = A5.OpenChecker(os_n, ss_n, st_n, CAL, contract_hash=sc_n.contract_hash).run(D, client=fake, now=Clock(TARGET))
os_n2 = A5.OpenCheckStore(TMP / "none2_a5.sqlite3")
sc_n.run(SCAN, now=Clock(datetime(2026, 9, 30, 19, 31)))
no_cand = A5.OpenChecker(os_n2, ss_n, st_n, CAL, contract_hash=sc_n.contract_hash).run(D, client=fake, now=Clock(TARGET))
check("3-11) 개장 전 대상 계약 스캔이 없으면 NO_SCAN, 스캔은 있었는데 후보가 없으면 NO_CANDIDATES — 둘 다 조회 0회로 확정",
      no_scan["set"]["status"] == "NO_SCAN" and no_cand["set"]["status"] == "NO_CANDIDATES"
      and no_scan["set"]["count"] == no_cand["set"]["count"] == 0 and len(fake.calls) == n_calls)
md = A5.build_markdown(res)
check("3-12) 보고서: 대상일·신호일·계약·후보 상태·시각/조회/판정 집계·종목별 관찰가·기준가·신호일 종가·상한·손절·판정, "
      "'실제 체결이 아님' 고지",
      "대상일 2026-10-01 (신호일 2026-09-30)" in md and f"c:{sc.contract_hash}" in md and "실제 체결이 아니며" in md
      and f"| {PASS_CODES[0]} |" in md and "WITHIN_CAP" in md and "BASIS_CHANGED" in md)

# ── 4. 조회 클라이언트 ───────────────────────────────────────
class Sess:
    def __init__(self, clock, responses):
        self.clock, self.responses, self.posts = clock, list(responses), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, (headers or {}).get("api-id")))
        if url.endswith("/oauth2/token"):
            return Resp(200, {"token": "T", "return_code": 0})
        self.clock.t += timedelta(seconds=1)
        return self.responses.pop(0)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}

    def json(self):
        return self._body


ck = Clock(datetime(2026, 10, 1, 9, 5))
sess = Sess(ck, [Resp(429, {}), Resp(200, {"return_code": 0, "cur_prc": "+100", "base_pric": "100"}),
                 Resp(200, {"cur_prc": "+100"})])
cl = ReadOnlyResearchClient(sess, "https://mockapi.kiwoom.com", "k", "s", now=ck, monotonic=lambda: 0.0,
                            sleep=lambda s: None, retry_backoff_sec=(1,))
b = cl.fetch_body("ka10001", {"stk_cd": "005930"})
try:
    cl.fetch_body("ka10001", {"stk_cd": "005930"})
    no_rc = False
except ResearchApiError:
    no_rc = True
blocked = 0
for api, fn in (("ka10081", "body"), ("kt10000", "body"), ("ka10075", "body"), ("ka10001", "page")):
    try:
        cl.fetch_body(api, {}) if fn == "body" else cl.fetch_page(api, {}, "x")
    except ResearchConfigError:
        blocked += 1
check("4-1) 시세 조회(fetch_body)는 ka10001만, 목록 조회(fetch_page)는 수집 TR 3개만 — 주문·계좌 TR 차단. 429 재시도 후 "
      "시도 횟수·요청/수신 시각, return_code 없으면 오류",
      b.attempts == 2 and b.body["cur_prc"] == "+100" and b.requested_at < b.received_at and no_rc and blocked == 4
      and set(PRICE_API) == {"ka10001", "ka10003", "ka10004"} and set(RESEARCH_API) == {"ka10099", "ka10081", "ka20006"})

# a1 DB(이전 버전 — 보조 조회 열 없음) → a2
a1db = TMP / "old_a1.sqlite3"
old_schema = A5._SCHEMA.replace(" source_price INTEGER,\n  source_exchange TEXT, source_lag_sec INTEGER, best_ask INTEGER,"
                                " best_bid INTEGER, quote_time TEXT, extra_json TEXT,", "")
con = sqlite3.connect(a1db)
con.executescript(old_schema)
con.execute("INSERT INTO meta VALUES('a5_schema','a1')")
con.execute("INSERT INTO price_check(set_id, symbol, check_kind, signal_id, target_at, timing, fetch_status, attempts,"
            " error, outcome, outcome_detail, run_id, recorded_at) VALUES('a5_x','000100','OPEN+5m','s','t','MISSED',"
            "'NOT_RUN',0,'','MISSED','x','r','t')")
con.commit()
con.close()
had = {r[1] for r in sqlite3.connect(a1db).execute("PRAGMA table_info(price_check)")}
with A5.OpenCheckStore(a1db) as oa:
    cols = {r[1] for r in oa.conn.execute("PRAGMA table_info(price_check)")}
    old_row = oa.checks("a5_x")[0]
    ok_a2 = (oa.conn.execute("SELECT value FROM meta WHERE key='a5_schema'").fetchone()[0] == "a2"
             and oa.backup_path is not None and "best_ask" not in had and {"source_price", "best_ask", "extra_json"} <= cols
             and old_row["outcome"] == "MISSED" and old_row["best_ask"] is None)
with A5.OpenCheckStore(a1db) as oa:
    again_none = oa.backup_path is None
check("4-1b) A5 기록 저장소 a1 → a2: 백업 후 보조 조회 열 추가, 기존 기록 그대로(보조 값 비움), 다시 열면 백업 없음",
      ok_a2 and again_none and len(list(TMP.glob("old_a1.sqlite3.bak-a1-*"))) == 1)

# ── 5. CLI ──────────────────────────────────────────────────
from tools import research_collect as rc  # noqa: E402

st.close()
ss.close()
db, sdb = str(TMP / "main.sqlite3"), str(TMP / "main_scans.sqlite3")
rdir = TMP / "a5_reports"
waits = []


def run_cli(args, now, client=None, sleep=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rc.main(["--db", db, "--scan-db", sdb, "open-check", "--a5-db", str(TMP / "cli_a5.sqlite3"),
                        "--a5-report-dir", str(rdir)] + args, calendar=CAL, client=client, now=now,
                       sleep=sleep or waits.append)
    return code, buf.getvalue()


def _no_runs(path: Path) -> bool:
    with A5.OpenCheckStore(path) as s_:
        return s_.runs() == [] and s_.conn.execute("SELECT COUNT(*) FROM candidate_set").fetchone()[0] == 0


code_nw, out_nw = run_cli(["--no-wait"], Clock(datetime(2026, 10, 1, 8, 50)), client=fake)
code_h, out_h = run_cli([], Clock(datetime(2026, 10, 5, 9, 10)), client=fake)
code_far, out_far = run_cli([], Clock(datetime(2026, 10, 1, 7, 0)), client=fake)
check("4-2) CLI: 목표 시각 전 --no-wait·30분보다 멀면 기다리지 않고 기록 없이 0, 휴장일(10/5 대체공휴일)은 다음 거래일 안내",
      code_nw == 0 and "기록 없음" in out_nw and code_far == 0 and "기록 없음" in out_far and code_h == 0
      and "2026-10-06" in out_h and _no_runs(TMP / "cli_a5.sqlite3"))
clk_c = Clock(datetime(2026, 10, 1, 9, 0))
fc = FakePrice(clk_c, {c: quote(int(L[c][0]) - 1, CLOSES[c]) for c in PASS_CODES})


def wait_then_advance(sec):
    waits.append(sec)
    clk_c.t += timedelta(seconds=sec)


code_c, out_c = run_cli([], clk_c, client=fc, sleep=wait_then_advance)
js = json.loads(out_c[out_c.index("{"):])
check("4-3) CLI: 목표 30분 안이면 기다렸다가(300초) 확인·보고서(md·json) — 대상 계약 current = 스캐너 기본 계약",
      code_c == 0 and waits[-1] == 300 and js["counts"]["outcome"] == {"WITHIN_CAP": 8}
      and js["contract_hash"] == js["active_contract"] == sc.contract_hash
      and (rdir / "a5_open_2026-10-01.md").exists() and (rdir / "a5_open_2026-10-01.json").exists()
      and "body_json" not in (rdir / "a5_open_2026-10-01.json").read_text(encoding="utf-8"))


def boom():
    raise ResearchConfigError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 .env에 없음")


rc_make = rc.make_client
rc.make_client = lambda args: boom()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code_lazy = rc.main(["--db", str(TMP / "none.sqlite3"), "--scan-db", str(TMP / "none_scans.sqlite3"), "open-check",
                         "--a5-db", str(TMP / "lazy_a5.sqlite3"), "--a5-report-dir", str(rdir), "--day", "2026-10-02"],
                        calendar=CAL, now=Clock(datetime(2026, 10, 2, 9, 6)), sleep=waits.append)
rc.make_client = rc_make
code_bad, out_bad = run_cli(["--config", str(cfg("s1:\n  active_contract: nope\n"))], Clock(TARGET), client=fake)
check("4-4) 후보가 없으면 API 설정(.env)이 없어도 조회 없이 확정(지연 생성), 설정 파일 오류는 종료 코드 2",
      code_lazy == 0 and code_bad == 2 and "설정 오류" in out_bad)

clk_l = Clock(datetime(2026, 10, 1, 9, 6))
made = []


def fake_factory(args):
    made.append(args.base_url)
    return FakePrice(clk_l, {c: quote(int(L[c][0]) + 99, CLOSES[c]) for c in PASS_CODES})


rc.make_client = fake_factory
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code_real = rc.main(["--db", db, "--scan-db", sdb, "open-check", "--a5-db", str(TMP / "real_a5.sqlite3"),
                         "--a5-report-dir", str(TMP / "real_reports")], calendar=CAL, now=clk_l, sleep=waits.append)
rc.make_client = rc_make
js_r = json.loads(buf.getvalue()[buf.getvalue().index("{"):])
check("4-5) 실제 CLI 경로(_LazyClient → 팩토리만 가짜): 후보가 있으면 클라이언트를 한 번 만들어 ka10001로 조회·기록",
      code_real == 0 and made == ["https://mockapi.kiwoom.com"] and js_r["counts"]["outcome"] == {"ABOVE_CAP": 8}
      and js_r["counts"]["fetch"] == {"OK": 8})

# ── 6. 프로브 분석 ──────────────────────────────────────────
from tools.probe_price_sources import analyze  # noqa: E402

a_ok = analyze("ka10001", "005930", 200, {"return_code": 0, "cur_prc": "+72500", "base_pric": "72000",
                                          "upl_pric": "+93600", "trde_qty": "1234"}, "t0", "t1")
a_no = analyze("ka10001", "005930", 200, {"return_code": 0, "stk_nm": "x"}, "t0", "t1")
a_t = analyze("ka10003", "005930", 200, {"return_code": 0, "cntr_infr": [{"tm": "090501", "cur_prc": "+72500"}]},
              "t0", "t1")
check("6-1) 가격 원천 프로브: ka10001 필수 필드(cur_prc·base_pric) 판정, 목록 행의 시각 필드 후보(tm) 표시",
      a_ok["a5_required_ok"] and a_ok["prices_top"]["cur_prc"] == 72500 and not a_no["a5_required_ok"]
      and a_t["list:cntr_infr:time_like_first"] == {"tm": "090501"})

from tools import probe_price_sources as pps  # noqa: E402


class PSess:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], (headers or {}).get("api-id")))
        if url.endswith("/oauth2/token"):
            return Resp(200, {"token": "TOKEN-XYZ", "return_code": 0})
        if headers["api-id"] == "ka10001":
            return Resp(200, {"return_code": 0, "cur_prc": "+72500", "base_pric": "72000", "token": "LEAK"})
        return Resp(200, {"return_code": 1, "return_msg": "지원하지 않는 TR(가정)"})


envf = TMP / "probe.env"
envf.write_text("KIWOOM_APP_KEY=AK\nKIWOOM_SECRET_KEY=SK\n", encoding="utf-8")
psess = PSess()
pout = TMP / "probe_out"
with contextlib.redirect_stdout(io.StringIO()):
    pcode = pps.main(["--env-file", str(envf), "--symbols", "005930,000660", "--repeat", "2", "--gap-sec", "0",
                      "--sleep", "0", "--out-dir", str(pout)], session=psess, now=Clock(datetime(2026, 10, 2, 11, 0)),
                     sleep=lambda s: None)
summ = list(pout.glob("price_sources_*_summary.txt"))
rawf = list(pout.glob("price_sources_*.jsonl"))
txt = summ[0].read_text(encoding="utf-8") if summ else ""
check("6-2) 프로브 실행 경로: 모의 도메인·조회 TR만(ka10001×2종목·ka10003·ka10004, 2회), 요약·원시 응답 저장, 토큰 값 가림, "
      "오류 응답도 결과로 기록",
      pcode == 0 and [p[1] for p in psess.posts[1:]] == ["ka10001", "ka10001", "ka10003", "ka10004"] * 2
      and len(summ) == 1 and len(rawf) == 1 and "A5-1 필수 필드(cur_prc·base_pric) 있음" in txt
      and "LEAK" not in rawf[0].read_text(encoding="utf-8") and "지원하지 않는 TR" in txt)

# 10/2 11:40 모의 도메인 실측 응답(시세만, 계좌 정보 없음) — 파서가 실제 모양을 그대로 읽는지
REAL_KA10001 = {"stk_cd": "005930", "stk_nm": "삼성전자", "high_pric": "+277000", "open_pric": "-273500",
                "low_pric": "-271500", "upl_pric": "+358500", "lst_pric": "-193500", "base_pric": "276000",
                "exp_cntr_pric": "", "250hgst_pric_dt": "20260619", "cur_prc": "-275750", "pre_sig": "5",
                "pred_pre": "-250", "flu_rt": "-0.09", "trde_qty": "5158006", "return_code": 0,
                "return_msg": "정상적으로 처리되었습니다"}
REAL_KA10003 = {"cntr_infr": [
    {"tm": "114023", "cur_prc": "-275750", "pred_pre": "-250", "pre_rt": "-0.09", "pri_sel_bid_unit": "276000",
     "pri_buy_bid_unit": "-275500", "cntr_trde_qty": "-1", "sign": "5", "acc_trde_qty": "5158437",
     "acc_trde_prica": "1418454985000", "cntr_str": "119.50", "stex_tp": "KRX"},
    {"tm": "114023", "cur_prc": "-275750", "pred_pre": "-250", "pre_rt": "-0.09", "pri_sel_bid_unit": "276000",
     "pri_buy_bid_unit": "-275500", "cntr_trde_qty": "-18", "sign": "5", "acc_trde_qty": "5158436",
     "acc_trde_prica": "1418454709250", "cntr_str": "119.50", "stex_tp": "KRX"}],
    "return_code": 0, "return_msg": "정상적으로 처리되었습니다"}
REAL_KA10004 = {"bid_req_base_tm": "114024", "sel_fpr_bid": "276000", "sel_fpr_req": "21764", "buy_fpr_bid": "-275500",
                "buy_fpr_req": "73689", "sel_1th_pre_req_pre": "--33", "tot_sel_req": "961481", "tot_buy_req": "474580",
                "return_code": 0, "return_msg": "정상적으로 처리되었습니다"}
D2 = date(2026, 10, 2)
qr = A5.parse_quote(REAL_KA10001)
tr = A5.parse_trade(REAL_KA10003, D2)
bk = A5.parse_book(REAL_KA10004, D2)
bad_tr = 0
for body in ({"cntr_infr": []}, {"cntr_infr": [{"tm": "1140", "cur_prc": "1"}]}, {"cntr_infr": [{"tm": "114023",
              "cur_prc": "x", "stex_tp": "KRX"}]}, {"cntr_infr": [{"tm": "114023", "cur_prc": "1", "stex_tp": "NXT"}]},
             {"cntr_infr": [{"tm": "11402", "cur_prc": "1"}]}, {"cntr_infr": [{"tm": "1140230", "cur_prc": "1"}]},
             {"cntr_infr": [{"tm": "254023", "cur_prc": "1"}]}):
    try:
        A5.parse_trade(body, D2)
    except ValueError:
        bad_tr += 1
check("6-3) [10/2 실측 응답] ka10001 현재가 275,750(부호 '-'는 전일 대비)·기준가 276,000(현재가 − 기준가 = pred_pre −250)·"
      "상·하한가·거래량, ka10003 최근 KRX 체결 11:40:23·275,750, ka10004 호가 기준 11:40:24·매도 276,000·매수 275,500. "
      "시각·가격 형식이 틀리거나 KRX 행이 없으면 원천 시각을 만들지 않음",
      (qr.price, qr.base, qr.optional["upper_limit"], qr.optional["lower_limit"], qr.optional["volume"])
      == (275750, 276000, 358500, 193500, 5158006) and qr.base - qr.price == 250
      and tr["source_time"] == datetime(2026, 10, 2, 11, 40, 23) and tr["source_price"] == 275750
      and (bk["quote_time"], bk["best_ask"], bk["best_bid"]) == (datetime(2026, 10, 2, 11, 40, 24), 276000, 275500)
      and bad_tr == 7)

# ── 7. 경계 ────────────────────────────────────────────────
FORBID = ("infra.broker", "app", "domain.service", "domain.position", "domain.risk", "domain.strategy",
          "infra.storage", "infra.notify")
bad_imp = []
for f in ("infra/research/open_check.py", "infra/research/research_config.py", "tools/probe_price_sources.py"):
    for node in ast.walk(ast.parse(Path(f).read_text(encoding="utf-8"))):
        names = [node.module] if isinstance(node, ast.ImportFrom) and node.module else (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [])
        bad_imp += [f"{f}:{n}" for n in names if any(n == p or n.startswith(p + ".") for p in FORBID)]
check("7-1) A5-1은 브로커·주문 실행부·원장·전략 라우터를 import하지 않음(주문 호출 0)", not bad_imp)
for s_ in (os_, os2, os3, os5, os6, os_n, os_n2):
    s_.close()
st_n.close()
ss_n.close()
check("7-2) 테스트 산출물은 임시 폴더에만 — 레포에 data/·reports/·commands/ 생성 없음",
      not Path("data/research/a5_checks.sqlite3").exists() and not Path("reports/research/a5").exists()
      and not Path("commands").exists())

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
sys.exit(0 if failed == 0 else 1)
