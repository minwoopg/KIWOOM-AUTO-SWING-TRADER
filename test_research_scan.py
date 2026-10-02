# -*- coding: utf-8 -*-
"""A4-A: S1 관찰 스캔·저장·보고 회귀 테스트 (가짜 데이터·임시 DB, 네트워크·주문 없음).

GPT A4-A 완료 기준: 같은 입력·스캔 시각 재실행 → 같은 판정·중복 없음 / 이후 정정·새 스냅숏에도 기존 기록 유지 /
지수 미확보·오래된 봉·REBASE_REQUIRED·UNPROVEN은 후보 아님 / 저장 도중 중단 → 다음 실행 정상, 실패 실행은 완료 아님.
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.research.universe import classify_rows, summarize
from infra.research.kiwoom_rows import INDEX_DAILY, STOCK_DAILY, RawBar
from infra.research.s1_scanner import S1Scanner, ScanError, expected_session
from infra.research.scan_store import (ABORTED, COMPLETE, FAILED, RUNNING, ScanAbortedError, ScanStore,
                                       inspect_final_reset)
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
TMP = Path(tempfile.mkdtemp(prefix="research_scan_"))
T = date(2026, 9, 30)
SESS = CAL.trading_days_in_range(date(2026, 1, 1), date(2026, 10, 8))
UPTO_T = [d for d in SESS if d <= T]
RECV = datetime(2026, 9, 30, 19, 0)
SCAN = datetime(2026, 9, 30, 19, 30)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def stock_bar(d, c, up=0.01, dn=0.01, vol=100_000, tv=5_000):
    c = int(round(c))
    return RawBar(d, c, int(c * (1 + up)), int(c * (1 - dn)), c, vol, tv, "" if vol else "NO_TRADES")


def pass_bars(days, *, drift=1.003, pullback=(0.99, 0.98, 0.97, 0.965), recover=1.03, base=10_000):
    """완만한 상승 → 고점 → 눌림 → 마지막 날 회복 (S1 PASS 형태)."""
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
    out = []
    for i, d in enumerate(days):
        c = base * rate ** i
        out.append(RawBar(d, int(c * 100), int(c * 100.5), int(c * 99.5), int(c * 100), 500_000, 9_000_000, ""))
    return out


def put(st, sid, bars, *, recv=RECV, kind="STOCK"):
    st.init_series(spec=STOCK_DAILY if kind == "STOCK" else INDEX_DAILY, series_id=sid, code=sid.split(":")[-1],
                   bars=[FetchedBar(b, recv) for b in bars],
                   basis=AdjustmentBasis("1" if kind == "STOCK" else None, recv.strftime("%Y%m%d")),
                   activated_at=recv, job_id=None, required_from=None, coverage="OK", coverage_detail="", now=recv)


def lrow(code, name, mc="0", *, audit="정상", state="증거금40%|담보대출|신용가능", ow="0", cls=""):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": audit, "regDay": "20000101",
            "lastPrice": "00010000", "state": state, "marketCode": mc, "marketName": "x", "upName": "",
            "upSizeName": "", "companyClassName": cls, "orderWarning": ow, "nxtEnable": "N", "kind": "A"}


ROWS = [lrow("000100", "통과종목"), lrow("000200", "하락종목"), lrow("100100", "코스닥통과", "10"),
        lrow("000300", "주의종목", audit="투자주의"), lrow("000400", "시계열없음"), lrow("000500", "오래된봉"),
        lrow("000600", "거래없는봉"), lrow("000700", "재수집필요"), lrow("000800", "시각미입증"),
        lrow("000105", "통과종목우"), lrow("900110", "외국", "10", cls="외국기업")]


def snapshot(st, rows, observed: datetime):
    recs = classify_rows(rows)
    return st.save_snapshot(snapshot_date=observed.date(), observed_at=observed,
                            observed_end_at=observed + timedelta(seconds=5), market_phase="POST_CLOSE",
                            policy_version="u2:test", source="test", records=recs, raw_pages=[],
                            summary=summarize(recs))


def build(name: str, *, snap_at=datetime(2026, 9, 30, 18, 20)) -> ResearchStore:
    st = ResearchStore(TMP / f"{name}.sqlite3")
    put(st, "INDEX:KOSPI:001", index_bars(UPTO_T), kind="INDEX")
    put(st, "INDEX:KOSDAQ:101", index_bars(UPTO_T, base=850.0), kind="INDEX")
    put(st, "STOCK:000100", pass_bars(UPTO_T))
    put(st, "STOCK:000200", down_bars(UPTO_T))
    put(st, "STOCK:100100", pass_bars(UPTO_T, base=5_000))
    put(st, "STOCK:000300", pass_bars(UPTO_T))
    put(st, "STOCK:000500", pass_bars(UPTO_T[:-1]))                       # 9/29까지만
    nt = pass_bars(UPTO_T)
    k = len(nt) - 60
    nt[k] = RawBar(nt[k].date, nt[k - 1].close_raw, nt[k - 1].close_raw, nt[k - 1].close_raw, nt[k - 1].close_raw, 0, 0,
                   "NO_TRADES")
    put(st, "STOCK:000600", nt)
    put(st, "STOCK:000700", pass_bars(UPTO_T))
    put(st, "STOCK:000800", pass_bars(UPTO_T))
    put(st, "STOCK:000105", pass_bars(UPTO_T))
    st.mark_integrity("STOCK:000700", "TEST_REFETCH_FAILED", datetime(2026, 9, 30, 19, 10))
    st.conn.execute("UPDATE bar SET time_basis='UNPROVEN' WHERE series_id='STOCK:000800' AND date='2026-03-03'")
    if snap_at is not None:
        snapshot(st, ROWS, snap_at)
    return st


def scanner(st, name, **kw):
    ss = ScanStore(TMP / f"{name}_scans.sqlite3")
    return S1Scanner(st, ss, CAL, **kw), ss


# ── 1. 신호일 ───────────────────────────────────────────────
check("1-1) 신호일 = 완성된(정규장 종료+160분) 최근 거래일 — 9/30 18:09는 9/29, 18:10은 9/30, 토요일 10/3은 10/2, "
      "개천절 대체공휴일 10/5 밤은 10/2",
      expected_session(CAL, datetime(2026, 9, 30, 18, 9)) == date(2026, 9, 29)
      and expected_session(CAL, datetime(2026, 9, 30, 18, 10)) == T
      and expected_session(CAL, datetime(2026, 10, 3, 12, 0)) == date(2026, 10, 2)
      and expected_session(CAL, datetime(2026, 10, 5, 21, 0)) == date(2026, 10, 2))
try:
    expected_session(CAL, datetime(2027, 1, 4, 20, 0))
    check("1-2) 달력이 다루지 않는 해는 스캔 불가(ScanError)", False)
except ScanError:
    check("1-2) 달력이 다루지 않는 해는 스캔 불가(ScanError)", True)

# ── 2. 기본 스캔 ────────────────────────────────────────────
st = build("base")
sc, ss = scanner(st, "base")
clk = Clock(datetime(2026, 9, 30, 19, 31))
res = sc.run(SCAN, now=clk)
E = {e["symbol"]: e for e in res["evals"]}
check("2-1) 실행 COMPLETE, 신호일 9/30, 대상 = 보통주만(우선주·외국기업 제외)",
      res["status"] == COMPLETE and res["context"]["signal_date"] == "2026-09-30"
      and "000105" not in E and "900110" not in E and len(E) == 9)
check("2-2) 후보(PASS) = 정상 KOSPI·KOSDAQ 형태 2종목, 하락 종목 FAIL",
      {s for s, e in E.items() if e["eligible_signal"] == "PASS"} == {"000100", "100100"}
      and E["000200"]["eligible_signal"] == "FAIL" and E["000200"]["pattern_pass"] == "FAIL")
r300 = E["000300"]["result"]
check("2-3) 현재 위험(투자주의)은 자격 FAIL — 사유 기록, 패턴 결과 보존",
      E["000300"]["eligible_signal"] == "FAIL" and r300["pattern_pass"] == "PASS"
      and [c for c in r300["checks"] if c["name"] == "RISK_STATUS"][0]["detail"] == "AUDIT:투자주의")
check("2-4) [완료 기준] 시계열 없음·오래된 봉·REBASE_REQUIRED·UNPROVEN → 데이터 보류(UNKNOWN), 평가하지 않음",
      E["000400"]["data_status"] == "NO_SERIES" and E["000500"]["data_status"].startswith("STALE:last=2026-09-29")
      and E["000700"]["data_status"] == "INTEGRITY:REBASE_REQUIRED" and E["000800"]["data_status"].startswith("UNPROVEN")
      and all(E[s]["eligible_signal"] == "UNKNOWN" and E[s]["result"] is None and not E[s]["final"]
              for s in ("000400", "000500", "000700", "000800")))
check("2-5) 거래 없는 봉이 160세션 안에 있으면 HISTORY UNKNOWN·no_trades_hold 표시",
      E["000600"]["eligible_signal"] == "UNKNOWN" and E["000600"]["no_trades_hold"] == 1
      and [c for c in E["000600"]["result"]["checks"] if c["name"] == "HISTORY"][0]["detail"].startswith("NO_TRADES")
      and E["000600"]["final"] == 0)
lv = E["000100"]["result"]["levels"]
check("2-6) 후보에 참고 손절가·진입 상한·조건별 결과 저장, 다음 개장 전 스캔(actionable=1), 확정(final=1)",
      lv["stop_ref"] > 0 and lv["entry_cap"] > lv["stop_ref"] and len(E["000100"]["result"]["checks"]) >= 10
      and E["000100"]["actionable"] == 1 and E["000100"]["final"] == 1)
ev = E["000100"]["evidence"]
ctx = res["context"]
check("2-7) 증거 필드: 종목 revision·조정 기준일·지수 revision·스냅숏 ID(종목별), 저장/적용 정책·계산 버전·세션(실행별), "
      "입력 해시(신호 ID와 분리)",
      ev["stock"]["revision"] == 1 and ev["stock"]["base_dt"] == "20260930" and ev["snapshot_id"] == 1
      and ev["index_revision"] == 1 and ctx["applied_policy"].startswith("u2:") and ctx["feature_version"] == "f2"
      and ctx["snapshot"]["stored_policy"] == "u2:test" and ctx["indexes"]["INDEX:KOSPI:001"]["evidence"]["revision"] == 1
      and len(E["000100"]["input_hash"]) == 64
      and ss.observations("2026-09-30")[0]["signal_id"].count("|") == 4
      and E["000100"]["input_hash"] not in ss.observations("2026-09-30")[0]["signal_id"])
c = res["counts"]
check("2-8) 집계: 신호 2·서로 다른 종목 2·데이터 미확보 4·거래 없는 봉 보류 1·시장 RISK_ON·대표 기록 9건 새로",
      c["signals"] == 2 and c["distinct_symbols"] == 2 and c["data_hold_total"] == 4 and c["no_trades_hold"] == 1
      and c["market_regime"] == {"INDEX:KOSPI:001": "RISK_ON", "INDEX:KOSDAQ:101": "RISK_ON"}
      and c["observations"] == {"new": 9, "kept_final": 0, "replaced": 0, "kept_newer": 0})
stored = ss.evals(res["run_id"])
check("2-9) 실행·판정 저장: COMPLETE 1건, 판정 9건(결과 본문 압축 저장 후 그대로 복원)",
      [r["status"] for r in ss.runs()] == [COMPLETE] and len(stored) == 9
      and {e["symbol"]: e["result"] for e in stored}["000100"]["levels"] == lv)

# ── 3. 재실행·재현 ──────────────────────────────────────────
again = sc.run(SCAN, now=clk, verify=True)
check("3-1) [완료 기준] 같은 스캔 시각 재실행 → 건너뜀(중복 저장 없음), 다시 계산하면 판정·입력 해시·결과 동일",
      again["status"] == "SKIPPED_ALREADY_COMPLETE" and again["verify"]["identical"]
      and len(ss.runs()) == 1 and ss.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0] == 9
      and ss.conn.execute("SELECT COUNT(*) FROM s1_observation").fetchone()[0] == 9)
c1, c2 = sc.compute(SCAN), sc.compute(SCAN)
check("3-2) 같은 시각 두 번 계산 결과 완전히 같음(결정적)",
      [(e["symbol"], e["eligible_signal"], e["input_hash"], e["result"]) for e in c1["evals"]]
      == [(e["symbol"], e["eligible_signal"], e["input_hash"], e["result"]) for e in c2["evals"]])

# ── 4. 이후 정정·새 스냅숏·늦게 도착한 봉 ────────────────────────
st.append_forward(series_id="STOCK:000500", bars=[FetchedBar(pass_bars(UPTO_T)[-1], datetime(2026, 10, 1, 9, 30))],
                  verified_base_dt="20261001", verified_at=datetime(2026, 10, 1, 9, 30))
corr = [RawBar(b.date, b.open_raw + 7, b.high_raw + 7, b.low_raw + 7, b.close_raw + 7, b.volume, b.trade_value_raw, "")
        for b in pass_bars(UPTO_T)]
st.replace_series(spec=STOCK_DAILY, series_id="STOCK:000600", code="000600",            # 거래 없는 봉 정정
                  candidate=[FetchedBar(b, datetime(2026, 10, 1, 8, 0)) for b in pass_bars(UPTO_T)],
                  basis=AdjustmentBasis("1", "20261001"), activated_at=datetime(2026, 10, 1, 8, 0), job_id=None,
                  required_from=None, coverage="OK", coverage_detail="", reason="TEST_NO_TRADES_FIX")
rr = st.replace_series(spec=STOCK_DAILY, series_id="STOCK:000100", code="000100",
                       candidate=[FetchedBar(b, datetime(2026, 10, 1, 8, 0)) for b in corr],
                       basis=AdjustmentBasis("1", "20261001"), activated_at=datetime(2026, 10, 1, 8, 0), job_id=None,
                       required_from=None, coverage="OK", coverage_detail="", reason="TEST_CORRECTION")
snapshot(st, [lrow("000100", "통과종목", audit="투자경고")] + ROWS[1:], datetime(2026, 10, 1, 11, 0))
verify_old = sc.verify(res["run_id"], SCAN)
check("4-1) [완료 기준] 10/1 정정(REBASE)·새 스냅숏·늦은 봉 도착 뒤에도 9/30 19:30 재계산 = 저장된 기록과 동일",
      rr["action"] == "REBASE" and verify_old["identical"])
late = sc.run(datetime(2026, 10, 1, 12, 0), now=Clock(datetime(2026, 10, 1, 12, 1)))
L = {e["symbol"]: e for e in late["evals"]}
O = {o["symbol"]: o for o in ss.observations("2026-09-30")}
check("4-2) 10/1 12:00 스캔(신호일 여전히 9/30): 정정 값·새 스냅숏으로 000100은 이번 판정 FAIL(투자경고)이지만 "
      "확정 기록(9/30 19:30 PASS)은 그대로",
      late["context"]["signal_date"] == "2026-09-30" and L["000100"]["eligible_signal"] == "FAIL"
      and L["000100"]["evidence"]["stock"]["revision"] == 2
      and O["000100"]["eligible_signal"] == "PASS" and O["000100"]["run_id"] == res["run_id"])
check("4-3) 보류였던 000500은 늦게 도착한 9/30 봉으로 평가돼 대표 기록 대체(이력 보존), 개장 뒤 스캔이라 actionable=0",
      O["000500"]["run_id"] == late["run_id"] and O["000500"]["data_status"] == "OK" and O["000500"]["replaced_count"] == 1
      and O["000500"]["history"][0]["data_status"].startswith("STALE") and O["000500"]["actionable"] == 0)
check("4-4) [GPT R3] 첫 스캔 NO_TRADES → UNKNOWN(final=0) → 정정 뒤 스캔 PASS가 대표 기록을 대체(이력 보존)",
      O["000600"]["eligible_signal"] == "PASS" and O["000600"]["run_id"] == late["run_id"]
      and O["000600"]["history"][0]["eligible_signal"] == "UNKNOWN" and O["000600"]["final"] == 1)
check("4-4b) 대표 기록 집계: 확정 4건 유지(000100·000200·000300·100100) · 대체 5건(000500·000600 해소 + "
      "000400·000700·000800은 더 늦은 보류로 대체)",
      late["counts"]["observations"]["kept_final"] == 4 and late["counts"]["observations"]["replaced"] == 5)
first_evals = {e["symbol"]: e for e in ss.evals(res["run_id"])}
check("4-5) 첫 실행의 판정 기록 자체도 그대로(append-only)",
      first_evals["000100"]["eligible_signal"] == "PASS" and first_evals["000100"]["evidence"]["stock"]["revision"] == 1)
old_rerun = sc.run(datetime(2026, 9, 30, 20, 0), now=Clock(datetime(2026, 10, 1, 13, 0)))
O2 = {o["symbol"]: o for o in ss.observations("2026-09-30")}
check("4-6) 과거 시각(9/30 20:00) 재현 실행은 더 늦은 보류 기록을 되돌리지 않음(kept_newer)",
      old_rerun["counts"]["observations"]["kept_newer"] >= 1 and O2["000400"]["run_id"] == late["run_id"])
sc.sstore.close()
st.close()

# ── 5. 지수 장애 ────────────────────────────────────────────
st = build("idx")
st.mark_integrity("INDEX:KOSDAQ:101", "TEST_INDEX_FAILED", datetime(2026, 9, 30, 19, 15))
sc, ss = scanner(st, "idx")
r5 = sc.run(SCAN, now=clk)
E5 = {e["symbol"]: e for e in r5["evals"]}
check("5-1) [완료 기준] KOSDAQ 지수 REBASE_REQUIRED → KOSDAQ 종목은 RS·시장 UNKNOWN(후보 아님, 확정 아님), "
      "KOSPI 종목은 그대로 PASS",
      E5["100100"]["index_status"] == "INTEGRITY:REBASE_REQUIRED" and E5["100100"]["market_pass"] == "UNKNOWN"
      and E5["100100"]["eligible_signal"] != "PASS" and E5["100100"]["final"] == 0
      and E5["000100"]["eligible_signal"] == "PASS" and r5["counts"]["index_holds"] == {"INDEX:KOSDAQ:101": 1})
ss.close()
st.close()
st = ResearchStore(TMP / "idx_stale.sqlite3")
put(st, "INDEX:KOSPI:001", index_bars(UPTO_T[:-1]), kind="INDEX")              # 지수 9/29까지만
put(st, "INDEX:KOSDAQ:101", index_bars(UPTO_T, base=850.0), kind="INDEX")
put(st, "STOCK:000100", pass_bars(UPTO_T))
put(st, "STOCK:100100", pass_bars(UPTO_T, base=5_000))
snapshot(st, [ROWS[0], ROWS[2]], datetime(2026, 9, 30, 18, 20))
sc, ss = scanner(st, "idx_stale")
E6 = {e["symbol"]: e for e in sc.run(SCAN, now=clk)["evals"]}
check("5-2) [완료 기준] KOSPI 지수 오래된 봉(9/29까지) → KOSPI 종목 시장 판정 보류, KOSDAQ 종목은 PASS",
      E6["000100"]["index_status"].startswith("STALE") and E6["000100"]["eligible_signal"] != "PASS"
      and E6["100100"]["eligible_signal"] == "PASS")
ss.close()
st.close()

# ── 6. 종목 목록 스냅숏 선택 ─────────────────────────────────
st = build("snap", snap_at=datetime(2026, 9, 30, 14, 12))                   # 장중 스냅숏뿐
snapshot(st, ROWS, datetime(2026, 9, 30, 19, 45))                           # 스캔 시각 뒤 스냅숏
sc, ss = scanner(st, "snap")
r6 = sc.run(SCAN, now=clk)
E7 = {e["symbol"]: e for e in r6["evals"]}
check("6-1) 스캔 시각 뒤 스냅숏은 쓰지 않음(latest_snapshot 아님) — 9/30 14:12 스냅숏 사용",
      r6["context"]["snapshot"]["snapshot_id"] == 1)
check("6-2) 그 스냅숏이 장 마감 전 관측 → 위험 상태 모름(RISK_STATUS UNKNOWN), 후보·확정 아님",
      r6["context"]["snapshot"]["status"].startswith("BEFORE_SESSION_CLOSE")
      and E7["000100"]["eligibility_pass"] == "UNKNOWN" and E7["000100"]["eligible_signal"] == "UNKNOWN"
      and E7["000100"]["final"] == 0 and r6["counts"]["snapshot_hold"] >= 1)
ss.close()
st.close()
st = build("nosnap", snap_at=None)
sc, ss = scanner(st, "nosnap")
try:
    sc.run(SCAN, now=clk)
    check("6-3) 스캔 시각까지 스냅숏이 없으면 스캔 실패 — FAILED로 남고 완료 아님", False)
except ScanError:
    check("6-3) 스캔 시각까지 스냅숏이 없으면 스캔 실패 — FAILED로 남고 완료 아님",
          [r["status"] for r in ss.runs()] == [FAILED] and ss.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0] == 0)
ss.close()
st.close()

# ── 7. 저장 도중 중단·복구 ───────────────────────────────────
st = build("crash")
sc, ss = scanner(st, "crash")


def boom():
    raise KeyboardInterrupt


try:
    sc.run(SCAN, now=clk, before_commit=boom)
    interrupted = False
except KeyboardInterrupt:
    interrupted = True
check("7-1) [완료 기준] 커밋 직전 중단 → 판정·대표 기록 하나도 없음, 실행은 완료가 아님(ABORTED)",
      interrupted and [r["status"] for r in ss.runs()] == [ABORTED]
      and ss.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0] == 0
      and ss.conn.execute("SELECT COUNT(*) FROM s1_observation").fetchone()[0] == 0)
r7 = sc.run(SCAN, now=clk)
check("7-2) 다음 실행은 같은 run_key로 정상 완료(시도 2), 결과 정상",
      r7["status"] == COMPLETE and r7["run_id"].endswith("_a2") and r7["counts"]["signals"] == 2
      and [r["status"] for r in ss.runs()] == [ABORTED, COMPLETE])
rid, _ = ss.begin_run(run_key="k|x", scan_at=datetime(2026, 9, 30, 20, 0), signal_date="2026-09-30", strategy="s",
                      config_hash="c" * 16, universe_policy="u", feature_version="f", market_version="m",
                      after_close_min=160, started_at=datetime(2026, 9, 30, 20, 0))       # 강제 종료로 RUNNING에 남음
r8 = sc.run(datetime(2026, 9, 30, 20, 30), now=Clock(datetime(2026, 9, 30, 20, 31)))
check("7-3) 강제 종료로 RUNNING에 남은 실행은 다음 실행이 ABORTED로 정리",
      r8["aborted_previous"] == [rid] and {r["run_id"]: r["status"] for r in ss.runs()}[rid] == ABORTED)
rid2, _ = ss.begin_run(run_key="k|y", scan_at=datetime(2026, 9, 30, 21, 0), signal_date="2026-09-30", strategy="s",
                       config_hash="c" * 16, universe_policy="u", feature_version="f", market_version="m",
                       after_close_min=160, started_at=datetime(2026, 9, 30, 21, 0))
ss.begin_run(run_key="k|z", scan_at=datetime(2026, 9, 30, 21, 5), signal_date="2026-09-30", strategy="s",
             config_hash="c" * 16, universe_policy="u", feature_version="f", market_version="m",
             after_close_min=160, started_at=datetime(2026, 9, 30, 21, 5))            # 다른 실행이 rid2를 정리
n_eval = ss.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0]
try:
    ss.finish_run(run_id=rid2, evals=r7["evals"], context={}, counts={}, snapshot=None, snapshot_status="OK",
                  scan_at=datetime(2026, 9, 30, 21, 0), now=datetime(2026, 9, 30, 21, 6), strategy="s",
                  config_hash="c" * 16)
    check("7-4) ABORTED로 정리된 실행은 늦게 끝나도 완료로 표시되지 않고 아무것도 쓰지 않음", False)
except ScanAbortedError:
    check("7-4) ABORTED로 정리된 실행은 늦게 끝나도 완료로 표시되지 않고 아무것도 쓰지 않음",
          {r["run_id"]: r["status"] for r in ss.runs()}[rid2] == ABORTED
          and ss.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0] == n_eval)
try:
    ss.conn.execute("UPDATE scan_run SET status='COMPLETE' WHERE run_id=?", (r7["run_id"].replace("_a2", "_a1"),))
    dup = True
except sqlite3.IntegrityError:
    dup = False
check("7-5) 같은 run_key의 COMPLETE는 하나뿐(유일 인덱스)", not dup)
ss.close()
st.close()

# ── 8. 보고서·CLI ───────────────────────────────────────────
from tools import research_collect as rc  # noqa: E402

st = build("cli")
st.close()
db = str(TMP / "cli.sqlite3")
rdir = TMP / "reports"
code = rc.main(["--db", db, "--report-dir", str(rdir), "scan", "--at", "2026-09-30T19:30:00"], calendar=CAL,
               now=Clock(datetime(2026, 9, 30, 19, 31)))
mds = list(rdir.glob("s1_scan_2026-09-30_*.md"))
md = mds[0].read_text(encoding="utf-8") if mds else ""
check("8-1) scan 명령: 기본 관찰 DB(s1_scans.sqlite3)·보고서(md·json) 생성",
      code == 0 and len(mds) == 1 and (TMP / "s1_scans.sqlite3").exists()
      and len(list(rdir.glob("s1_scan_2026-09-30_*.json"))) == 1)
check("8-2) 보고서: 수익 아님 고지·잠정 완성 기준·요약·보류/탈락 사유·후보 표(손절가·진입 상한)",
      "수익으로 해석하지 않습니다" in md and "잠정 기준" in md and "| 000100 | 통과종목 | KOSPI |" in md
      and "데이터: STALE" in md and "데이터: INTEGRITY" in md and "C_GT_MA120" in md and "참고 손절가" in md)
code2 = rc.main(["--db", db, "--report-dir", str(rdir), "scan", "--at", "2026-09-30T19:30:00", "--verify"],
                calendar=CAL, now=Clock(datetime(2026, 9, 30, 19, 40)))
check("8-3) 같은 시각 scan 재실행(--verify): 건너뜀·재계산 일치(0), 보고서·실행 추가 없음",
      code2 == 0 and len(list(rdir.glob("*.md"))) == 1
      and len(ScanStore(TMP / "s1_scans.sqlite3").runs()) == 1)


class DownClient:
    calls = 0

    def fetch_page(self, *a, **k):
        from infra.research.kiwoom_readonly import ResearchApiError
        raise ResearchApiError("모의 장애")


code3 = rc.main(["--db", db, "--report-dir", str(rdir), "update"], calendar=CAL, client=DownClient(),
                now=Clock(datetime(2026, 9, 30, 20, 0)))
runs = ScanStore(TMP / "s1_scans.sqlite3").runs()
check("8-4) update: 갱신이 실패해도(종료 코드 1) 갱신 뒤 스캔은 실행 — 종목별 데이터 상태로 판단",
      code3 == 1 and len(runs) == 2 and runs[-1]["status"] == COMPLETE and runs[-1]["scan_at"] == "2026-09-30T20:00:00")
code4 = rc.main(["--db", db, "--report-dir", str(rdir), "update", "--no-scan"], calendar=CAL, client=DownClient(),
                now=Clock(datetime(2026, 9, 30, 20, 10)))
check("8-5) update --no-scan은 스캔하지 않음", code4 == 1 and len(ScanStore(TMP / "s1_scans.sqlite3").runs()) == 2)

# R4: 보고서 저장 실패 → 같은 시각 재실행이 보고서만 다시 만듦(재계산 없음)
rdb = str(TMP / "rep.sqlite3")
build("rep").close()
rdir2 = TMP / "reports2"
rsdb = str(TMP / "rep_scans_cli.sqlite3")
orig_write = rc.write_report


def broken_write(run, out_dir):
    raise OSError("디스크 가득 참(모의)")


rc.write_report = broken_write
try:
    c1 = rc.main(["--db", rdb, "--scan-db", rsdb, "--report-dir", str(rdir2), "scan", "--at", "2026-09-30T19:30:00"],
                 calendar=CAL,
                 now=Clock(datetime(2026, 9, 30, 19, 31)))
finally:
    rc.write_report = orig_write
srep = ScanStore(rsdb)
n_eval_before = srep.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0]
calls = []
orig_compute = S1Scanner.compute
S1Scanner.compute = lambda self, at: calls.append(at) or orig_compute(self, at)
try:
    c2 = rc.main(["--db", rdb, "--scan-db", rsdb, "--report-dir", str(rdir2), "scan", "--at", "2026-09-30T19:30:00"],
                 calendar=CAL,
                 now=Clock(datetime(2026, 9, 30, 19, 40)))
finally:
    S1Scanner.compute = orig_compute
runs_r = srep.runs()
md2 = list(rdir2.glob("*.md"))
check("8-6) [GPT R4] 보고서 저장 실패 → 종료 코드 1·관찰 기록은 저장 → 같은 시각 재실행은 재계산 없이 저장된 실행으로 보고서만 "
      "다시 만들고 0, 실행·판정 추가 없음",
      c1 == 1 and c2 == 0 and not calls and len(md2) == 1 and len(runs_r) == 1
      and runs_r[0]["report_path"] == str(md2[0])
      and srep.conn.execute("SELECT COUNT(*) FROM s1_eval").fetchone()[0] == n_eval_before)
first_run = srep.load_run(runs_r[0]["run_id"])
direct = TMP / "reports_direct"
from infra.research.scan_report import write_report as wr  # noqa: E402
st_r = ResearchStore(rdb)
fresh = S1Scanner(st_r, ScanStore(TMP / "rep_fresh_scans.sqlite3"), CAL).run(SCAN, now=clk)
pd = Path(wr(fresh, direct))
check("8-7) 저장된 실행으로 다시 만든 보고서 = 같은 입력으로 새로 계산해 만든 보고서(실행 ID만 다름)",
      md2[0].read_text(encoding="utf-8").replace(first_run["run_id"], "RUN")
      == pd.read_text(encoding="utf-8").replace(fresh["run_id"], "RUN"))
st_r.close()
srep.close()

# R5: 분류 정책만 다른 실행 → 실행 ID 충돌 없이 둘 다 완료
from domain.research.universe import UniversePolicy  # noqa: E402
st = build("pol")
ss5 = ScanStore(TMP / "pol_scans.sqlite3")
ra = S1Scanner(st, ss5, CAL).run(SCAN, now=clk)
rb = S1Scanner(st, ss5, CAL, policy=UniversePolicy(foreign_class_names=())).run(SCAN, now=clk)
check("8-8) [GPT R5] 같은 시각·설정에서 분류 정책만 다르면 서로 다른 실행 ID로 둘 다 완료(유일성 충돌 없음)",
      ra["status"] == rb["status"] == COMPLETE and ra["run_id"] != rb["run_id"] and ra["run_key"] != rb["run_key"]
      and [r["status"] for r in ss5.runs()] == [COMPLETE, COMPLETE])
ss5.close()
st.close()

# ── 9. GPT 종합 검토 작은 보완 ─────────────────────────────────
import io  # noqa: E402
import contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc.main(["--db", db, "status"], calendar=CAL)
import json  # noqa: E402
stat = json.loads(buf.getvalue())
check("9-1) status time_basis를 현재 봉(current)과 이전 판(history)으로 나눠 집계",
      set(stat["time_basis"]) == {"current", "history"} and stat["time_basis"]["current"].get("UNPROVEN") == 1)
with ResearchStore(db) as s9:
    s9.create_job(job_id="j_done", kind="BACKFILL", created_at=datetime(2026, 9, 30, 9, 0), base_dt="20260930",
                  upd_stkpc_tp="1", required_from=date(2017, 1, 2), max_pages=8, snapshot_id=1, targets=[], params={})
    s9.close_job_if_finished("j_done")
code9 = rc.main(["--db", db, "--env-file", str(TMP / "없는.env"), "backfill"], calendar=CAL)
check("9-2) 조회가 필요 없는 backfill(끝난 작업만 있음)은 API 설정(.env)이 없어도 실패하지 않음(지연 생성)", code9 == 0)
with ResearchStore(db) as s9:
    n_job = s9.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
try:
    rc.main(["--db", db, "--env-file", str(TMP / "없는.env"), "backfill", "--new", "--limit", "1"], calendar=CAL)
    raised = ""
except Exception as exc:
    raised = str(exc)
with ResearchStore(db) as s9:
    n_job2 = s9.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
check("9-3) 실제 조회가 필요할 때는 작업을 만들기 전에 설정 오류를 그대로 알림(종목 ERROR로 남기지 않음)",
      ".env" in raised and n_job2 == n_job)

# ── 11. 이전 버전 관찰 DB(s1) — UNKNOWN이 고정된 대표 기록 (GPT B2) ──────────
st = build("legacy")
sc, ss = scanner(st, "legacy")
first = sc.run(SCAN, now=Clock(datetime(2026, 9, 30, 19, 31)))
ss.close()
ldb = TMP / "legacy_scans.sqlite3"
con = sqlite3.connect(ldb)                  # 이전 버전(f837185)이 남긴 상태: 입력만 정상이면 UNKNOWN도 final=1, 스키마 s1
con.execute("UPDATE s1_observation SET final=1 WHERE symbol='000600'")
con.execute("UPDATE s1_eval SET final=1 WHERE symbol='000600'")
ctx_old = json.loads(con.execute("SELECT context_json FROM scan_run").fetchone()[0])
ctx_old.pop("final_rule")
con.execute("UPDATE scan_run SET context_json=?", (json.dumps(ctx_old, ensure_ascii=False),))
con.execute("DROP TABLE obs_audit")
con.execute("UPDATE meta SET value='s1' WHERE key='scan_schema'")
con.commit()
con.close()
h_before = hashlib.sha256(ldb.read_bytes()).hexdigest()
pre = inspect_final_reset(ldb)
check("11-1) [GPT B2] 읽기 전용 미리 보기: 이전 버전 대표 기록 중 final을 풀 것 1건(UNKNOWN) — DB 그대로",
      pre["scan_schema"] == "s1" and pre["final_observations"] == 5 and pre["final_to_reset"] == 1
      and pre["by_signal"] == {"UNKNOWN": 1} and hashlib.sha256(ldb.read_bytes()).hexdigest() == h_before)
import contextlib  # noqa: E402
import io  # noqa: E402

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code_i = rc.main(["--db", str(TMP / "legacy.sqlite3"), "--scan-db", str(ldb), "inspect-unproven"], calendar=CAL)
out_i = json.loads(buf.getvalue())
check("11-1b) inspect-unproven(읽기 전용)이 관찰 DB 미리 보기도 함께 출력 — scan_db 풀 기록 1건, 연구 DB(r5)는 재점검 대상 없음"
      "(시험용 UNPROVEN 1봉은 이전된 판이 아니라 보정 대상 아님), 관찰 DB 그대로",
      code_i == 0 and out_i["scan_db"]["final_to_reset"] == 1 and out_i["schema"] == "r5"
      and out_i["recheck_migrated"]["bars"] == 0 and out_i["verdict_bars"] == {"NOT_MIGRATED": 1}
      and hashlib.sha256(ldb.read_bytes()).hexdigest() == h_before)
ss = ScanStore(ldb)
O = {o["symbol"]: o for o in ss.observations("2026-09-30")}
aud = ss.audit()
ev_old = {e["symbol"]: e for e in ss.evals(first["run_id"])}
check("11-2) [GPT B2] 열면 백업 후 s2: UNKNOWN+final=1 대표 기록만 final=0(감사 이력: 바꾸기 전 run·판정·입력 상태), "
      "PASS·FAIL 확정 기록 4건은 그대로, 당시 실행 판정 행(s1_eval)은 보존",
      ss.conn.execute("SELECT value FROM meta WHERE key='scan_schema'").fetchone()[0] == "s2"
      and ss.upgrade_summary["final_reset"] == 1 and ss.upgrade_summary["final_observations"] == 5
      and O["000600"]["final"] == 0 and O["000600"]["eligible_signal"] == "UNKNOWN"
      and all(O[s]["final"] == 1 for s in ("000100", "000200", "000300", "100100"))
      and len(aud) == 1 and aud[0]["action"] == "FINAL_RESET" and aud[0]["before"]["eligible_signal"] == "UNKNOWN"
      and aud[0]["before"]["run_id"] == first["run_id"] and aud[0]["after"] == {"final": 0}
      and ev_old["000600"]["final"] == 1
      and ss.backup_path is not None and len(list(TMP.glob("legacy_scans.sqlite3.bak-s1-*"))) == 1)
st.replace_series(spec=STOCK_DAILY, series_id="STOCK:000600", code="000600",            # 거래 없는 봉 정정
                  candidate=[FetchedBar(b, datetime(2026, 10, 1, 8, 0)) for b in pass_bars(UPTO_T)],
                  basis=AdjustmentBasis("1", "20261001"), activated_at=datetime(2026, 10, 1, 8, 0), job_id=None,
                  required_from=None, coverage="OK", coverage_detail="", reason="TEST_NO_TRADES_FIX")
sc = S1Scanner(st, ss, CAL)
later = sc.run(datetime(2026, 10, 1, 12, 0), now=Clock(datetime(2026, 10, 1, 12, 1)))
O = {o["symbol"]: o for o in ss.observations("2026-09-30")}
check("11-3) [GPT B2 재현] 이전 대표 기록 UNKNOWN → 데이터 정정 뒤 최신 스캔 PASS가 대체(대체 1회, 이력 보존), "
      "확정 기록 4건 유지",
      O["000600"]["eligible_signal"] == "PASS" and O["000600"]["final"] == 1 and O["000600"]["replaced_count"] == 1
      and O["000600"]["history"][0]["eligible_signal"] == "UNKNOWN" and O["000600"]["run_id"] == later["run_id"]
      and later["counts"]["observations"]["kept_final"] == 4 and later["context"]["final_rule"] == "inputs_ok+decided")
vf = sc.verify(first["run_id"], SCAN)
check("11-4) 이전 규칙으로 저장된 실행의 재현 검증: 입력·판정·결과는 일치(identical), final 규칙 차이는 따로 1건",
      vf["identical"] and vf["final_rule_changed"] == 1)
ss.close()
ss = ScanStore(ldb)
check("11-5) 다시 열면 보정·백업 없음(한 번만), 감사 이력 1건 그대로",
      ss.upgrade_summary is None and ss.backup_path is None and len(ss.audit()) == 1
      and inspect_final_reset(ldb)["final_to_reset"] == 0)
ss.close()
st.close()

# ── 10. 경계 ────────────────────────────────────────────────
FORBID = ("infra.broker", "app", "domain.service", "domain.position", "domain.risk", "domain.strategy",
          "infra.storage", "infra.notify")
bad = []
for f in ("infra/research/s1_scanner.py", "infra/research/scan_store.py", "infra/research/scan_report.py"):
    for node in ast.walk(ast.parse(Path(f).read_text(encoding="utf-8"))):
        names = [node.module] if isinstance(node, ast.ImportFrom) and node.module else (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [])
        bad += [f"{f}:{n}" for n in names if any(n == p or n.startswith(p + ".") for p in FORBID)]
check("10-1) 스캐너·관찰 저장소·보고서는 브로커·주문 실행부·원장·전략 라우터를 import하지 않음(주문 호출 0)", not bad)
check("10-2) 테스트 산출물은 임시 폴더에만 — 레포에 data/·reports/·commands/ 생성 없음",
      not Path("data/research/s1_scans.sqlite3").exists() and not Path("reports/research/s1").exists()
      and not Path("commands").exists())

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
sys.exit(0 if failed == 0 else 1)
