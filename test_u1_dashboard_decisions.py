# -*- coding: utf-8 -*-
"""UI 1차 (GPT 10/27): 관리 창 '오늘 요약'·'감시 종목'(종목별 판단 + 선택 상세) 자료 계층 — 읽기 전용, 저장된 판단만.

    python test_u1_dashboard_decisions.py

임시 폴더의 가짜 기록만(dashboard.sample_data). 키움 API·파일 쓰기 없음.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.candidates import ApprovedCandidate, CandidateBook  # noqa: E402
from config.settings import load_settings  # noqa: E402
from dashboard import data as D  # noqa: E402
from dashboard import decisions as X  # noqa: E402
from dashboard.sample_data import build  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="u1_"))
NOW = datetime(2026, 10, 14, 10, 30)          # 신호일 = 2026-10-13
SIG = "2026-10-13"


def env(name, scenario="normal", allowed=()):
    root = build(TMP / name, scenario, NOW)
    s = load_settings(root / "config/settings.yaml")
    if allowed:
        s = dataclasses.replace(s, swing_strategy=dataclasses.replace(s.swing_strategy, allowed_symbols=tuple(allowed)))
    return root, s, D.paths_from_settings(s, root)


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# 1. 정상 — 후보 0건(관리자 후보 판독 READY·후보 없음). 표본 자료엔 개장 확인 DB가 없어 판독을 READY로 바꿔 끼움
ORIG_BOOK = X.candidate_book
X.candidate_book = lambda *a, **k: CandidateBook("READY", "2026-10-14", SIG, "h", [], note="후보 없음 (NO_CANDIDATES)")
R, S, P = env("normal")
h0 = tree_hash(R)
snap = D.snapshot(P, S, NOW)
h1 = tree_hash(R)
sm, sy = snap["summary"], snap["symbols"]
check("1-1) 화면 자료를 읽어도 파일·DB 바이트 그대로(쓰기 없음)", h0 == h1)
steps = {r["단계"]: r for r in sm.data["steps"]}
check("1-2) 오늘 요약: [정상 처리] 전략 결과 'S1 통과 0건'·운영 상태 '정상'(U2 분리), 분석 3·데이터 준비 3, 원장 매수 체결은 따로 표시, "
      "보유 감시 표시, 모든 단계가 같은 신호일 스캔 기준",
      sm.state == D.OK and sm.data["state"] == X.DONE and sm.data["strategy"].startswith("S1 통과 0건")
      and sm.data["ops"] == [] and "운영 상태: 정상" in sm.data["headline"]
      and steps["분석 대상(관심·S1)"]["건수"] == 3 and steps["S1 통과"]["건수"] == 0
      and "매수 체결 1건" in sm.data["headline"] and "보유 005380" in sm.data["headline"]
      and sm.data["basis"]["signal_date"] == SIG and "s1_x" in sm.data["basis"]["scan"])
rows = {r["코드"]: r for r in sy.data["rows"]}
det = "\n".join(sy.data["details"]["005930"])
check("1-3) 종목별: 005930 S1 '탈락: 전일 고가 돌파, 시장 조건'(저장된 조건별 결과), 상세에 판정 당시 값(-1,500)·시장 RISK_OFF·"
      "손절 기준·상한, 010120 '판정 안 함 — 데이터 확인 필요'",
      rows["005930"]["S1 판단"] == "탈락: 전일 고가 돌파, 시장 조건" and "값 -1,500" in det and "RISK_OFF" in det
      and "손절 기준 218,000" in det and rows["010120"]["현재 상태"].startswith("판정 안 함"))
check("1-4) 판정 본문이 없는 행(010120)은 상세에 '기록 없음' — 다시 계산하지 않음",
      any("기록 없음" in ln for ln in sy.data["details"]["010120"]))

# 1-5. S1 점수(10/27 사용자 요청) — 신호를 가르는 조건 9개 충족 비율, 수익 예측 아님
ALL = ("C_GT_MA120", "MA60_RISING", "RS60_POS", "PULLBACK", "CLOSE_GT_PREV_HIGH", "CLOSE_GE_MA20", "NOT_EXTENDED",
       "MARKET_REGIME", "STOP_VALID")
full = {"result": {"checks": [{"name": n, "result": "PASS"} for n in ALL + ("HISTORY", "SECURITY_TYPE")]}}
gate = {"result": {"checks": [{"name": n, "result": "PASS" if i < 7 else "FAIL"} for i, n in enumerate(ALL)]
                   + [{"name": "LIQUIDITY_TV20", "result": "FAIL"}]}}
unk = {"result": {"checks": [{"name": n, "result": "PASS" if i < 7 else "UNKNOWN"} for i, n in enumerate(ALL)]}}
check("1-5) S1 조건 충족도(U2 이름): '마지막 확인' 바로 앞 열, 005930 → 2.2/10(충족 2 · 미충족 2 · 판정 불가 5 — 기록에 없는 조건은 판정 불가), "
      "본문 없음 → '-', 전부 충족 → 10.0, 자격 미달 표시, 판정 불가는 미충족과 따로, 10점이어도 주문 미허용이면 함께 표시, "
      "상세에 '보조 표시 — 수익 가능성·매수 우선순위 아님'",
      list(rows["005930"])[-2:] == ["S1 조건 충족도", "마지막 확인"]
      and rows["005930"]["S1 조건 충족도"] == "2.2/10 (충족 2 · 미충족 2 · 판정 불가 5)"
      and rows["010120"]["S1 조건 충족도"] == "-" and X.s1_score(full)["score"] == 10.0
      and X.score_text(X.s1_score(gate)) == "7.8/10 (충족 7 · 미충족 2) · 자격 미달"
      and X.score_text(X.s1_score(unk)) == "7.8/10 (충족 7 · 미충족 0 · 판정 불가 2)"
      and X.score_text(X.s1_score(full), order_ok=False) == "10.0/10 (충족 9 · 미충족 0) · 주문 미허용"
      and any("수익 가능성·매수 우선순위 아님" in ln for ln in sy.data["details"]["005930"]))

# 2. 승인됐지만 주문 미허용 → 가격만 관찰
R2, S2, P2 = env("approved", allowed=("005930",))
with sqlite3.connect(P2.watch_db) as cx:
    cx.execute("INSERT INTO readiness(target,kind,code,status,reason,detail_json,config_version,run_id,checked_at,"
               "analysis_status,analysis_reason) VALUES('STOCK:000660','STOCK','000660','READY','','{}',6,'prep_x',?,"
               "'READY','')", (f"{SIG}T18:10:21",))
with sqlite3.connect(P2.s1_db) as cx:
    cx.execute("INSERT INTO s1_eval(run_id,symbol,name,market,series_id,signal_date,eligible_signal,pattern_pass,"
               "eligibility_pass,market_pass,stop_valid,data_status,index_status,snapshot_status,no_trades_hold,final,"
               "actionable,input_hash,codec,evidence_z) VALUES('s1_x','000660','SK하이닉스','KOSPI','STOCK:000660',?,'PASS',"
               "'PASS','PASS','PASS','PASS','OK','OK','OK',0,1,1,'h','c',x'00')", (SIG,))
obs = P2.state_file.with_name("entry_observation.jsonl")
obs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (
    {"at": "2026-10-14T09:06:00", "trade_date": "2026-10-14", "event": "OBS_QUOTE", "symbol": "000660",
     "signal_id": "sig-000660", "signal_date": SIG, "price": 199000, "ask": 200500, "limit": 200000, "stop_ref": 180000,
     "ok": False, "in_window": True, "reasons": ["ASK_ABOVE_LIMIT"]},
    {"at": "2026-10-14T09:07:00", "trade_date": "2026-10-14", "event": "OBS_QUOTE", "symbol": "000660",
     "signal_id": "sig-000660", "signal_date": SIG, "price": 198500, "ask": 199500, "limit": 200000, "stop_ref": 180000,
     "ok": True, "in_window": True, "reasons": [], "stop_dist_pct": 9.32})), encoding="utf-8")
BOOK = CandidateBook("READY", "2026-10-14", SIG, "h", [ApprovedCandidate(
    "000660", "SK하이닉스", "sig-000660", SIG, "h", "s1_x", 200400.0, 180000.0, 200000, 199000, "WITHIN_CAP")])
orig = X.candidate_book
X.candidate_book = lambda *a, **k: BOOK
h0 = tree_hash(R2)
s2 = X.today_summary(P2, S2, NOW.date(), NOW, D.runner_status(P2, NOW))
v2 = X.symbol_view(P2, S2, NOW.date(), NOW)
X.candidate_book = orig
r2 = {r["코드"]: r for r in v2.data["rows"]}
d2 = "\n".join(v2.data["details"]["000660"])
st2 = {r["단계"]: r for r in s2.data["steps"]}
check("2-1) 오늘 요약: S1 통과 1 → 승인 1 → 주문 허용 0 → '승인 1종목(000660)이 주문 허용 목록에 없어 가격만 관찰 중'",
      st2["S1 통과"]["건수"] == 1 and st2["개장 확인 승인"]["건수"] == 1 and st2["주문 허용"]["건수"] == 0
      and "가격만 관찰" in s2.data["headline"] and "000660" in s2.data["headline"] and "미허용: 000660" in st2["주문 허용"]["종목"])
check("2-2) 종목별: 000660 주문 자격 '미허용', 현재 상태 '승인 · 주문 미허용 — 가격만 관찰 (관찰 2회, 충족 1)', 상세에 승인 지정가·"
      "장초반 관찰(최초 충족 09:07 198,500)", r2["000660"]["주문 자격"] == "미허용"
      and r2["000660"]["현재 상태"] == "승인 · 주문 미허용 — 가격만 관찰 (관찰 2회, 충족 1)"
      and "지정가 200,000" in d2 and "최초 충족 09:07 198,500" in d2 and h0 == tree_hash(R2))

# 3. 아직 처리하지 않음 / 처리 실패
R3, S3, P3 = env("noscan")
with sqlite3.connect(P3.s1_db) as cx:
    cx.execute("DELETE FROM scan_run")
s3b = X.today_summary(P3, S3, NOW.date(), NOW, D.runner_status(P3, NOW))      # 마감 준비 COMPLETE인데 스캔 없음
with sqlite3.connect(P3.daemon_db) as cx:
    cx.execute("DELETE FROM task WHERE kind='CLOSE_PREP'")
s3 = X.today_summary(P3, S3, NOW.date(), NOW, D.runner_status(P3, NOW))
R4, S4, P4 = env("broken")
P4.s1_db.write_bytes(b"not a sqlite file" * 10)
s4 = X.today_summary(P4, S4, NOW.date(), NOW, D.runner_status(P4, NOW))
R5, S5, P5 = env("empty", "empty")
s5 = X.today_summary(P5, S5, NOW.date(), NOW, D.runner_status(P5, NOW))
check("3-1) 신호일 마감 준비·스캔 없음 → '아직 처리하지 않음', 마감 준비 완료인데 스캔 없음 → '처리 실패', 스캔 DB 손상 → '처리 실패·확인 불가', "
      "자료 없음 → '처리 실패·확인 불가'(정상 완료로 보이지 않음), 빈 폴더에 파일을 만들지 않음",
      s3.data["state"] == X.NOT_YET and "스캔 기록 없음" in s3.data["headline"] and s3b.data["state"] == X.FAILED
      and s4.data["state"] == X.FAILED and s5.data["state"] == X.FAILED
      and not (R5 / "data").exists())

# 3-2. U2(GPT 10/27 ②) — S1 통과 0건이어도 후보 승인 확인 실패는 운영 문제로
X.candidate_book = ORIG_BOOK                       # 표본에 개장 확인 DB 없음 → 실제 판독기 ERROR
s6 = X.today_summary(P, S, NOW.date(), NOW, D.runner_status(P, NOW))
check("3-2) GPT 재현: 후보 판독 ERROR + S1 통과 0건 → 이전 '정상 처리 · 매수 대상 없음' → 이제 [처리 실패·확인 불가], "
      "전략 결과 'S1 통과 0건'과 운영 상태 '후보 승인 확인 실패'를 따로, 승인 단계 '확인 불가'",
      s6.data["state"] == X.FAILED and s6.data["strategy"].startswith("S1 통과 0건")
      and any("후보 승인 확인 실패" in t for _, t in s6.data["ops"])
      and {r["단계"]: r for r in s6.data["steps"]}["개장 확인 승인"]["건수"] == "확인 불가")

# 3-3. U2(GPT 10/27 ①) — 원장 읽기 실패를 체결 0건·보유 없음으로 보이지 않음
X.candidate_book = lambda *a, **k: CandidateBook("READY", "2026-10-14", SIG, "h", [], note="후보 없음")
R7, S7, P7 = env("badledger")
P7.fill_ledger.write_text('{"broken json\n', encoding="utf-8")
s7 = X.today_summary(P7, S7, NOW.date(), NOW, D.runner_status(P7, NOW))
v7 = X.symbol_view(P7, S7, NOW.date(), NOW)
st7 = {r["단계"]: r for r in s7.data["steps"]}
check("3-3) GPT 재현: 원장 손상 → 매수 체결 '확인 불가'(이전 0건), 보유 '확인 불가', 상태 [처리 실패·확인 불가], 운영 상태에 원장 오류, "
      "종목별 참고에 '보유 여부 확인 불가'",
      st7["매수 체결"]["건수"] == "확인 불가" and "보유 확인 불가" in s7.data["strategy"] and s7.data["state"] == X.FAILED
      and any("체결·보유 확인 불가" in t for _, t in s7.data["ops"]) and any("보유 여부 확인 불가" in n for n in v7.notes))
X.candidate_book = ORIG_BOOK

# 4. 다시 계산하지 않음
src = (Path(__file__).resolve().parent / "dashboard" / "decisions.py").read_text(encoding="utf-8")
check("4-1) 화면 자료 코드에 S1 재계산·브로커·주문·API 호출 없음(저장된 판단만)",
      all(x not in src for x in ("evaluate_s1", "KiwoomBroker", "place_order", "get_quote", "requests.", "_post(")))

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
