# -*- coding: utf-8 -*-
"""관리 창 자료 계층 (M3) — 임시 폴더의 가짜 기록만 읽음. 화면·API 없이 실행(창 동작은 test_dashboard_desktop.py)."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, ".")

from config.settings import load_settings  # noqa: E402
from domain.cost_model import load_cost_model  # noqa: E402
from dashboard import data as D  # noqa: E402
from dashboard.sample_data import build  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="dashboard_"))
NOW = datetime(2026, 10, 14, 10, 30, 0)


def env(name, scenario):
    root = build(TMP / name, scenario, NOW)
    s = load_settings(root / "config/settings.yaml")
    return root, s, D.paths_from_settings(s, root)


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# 1. 정상
R1, S1, P1 = env("normal", "normal")
before = tree_hash(R1)
snap = D.snapshot(P1, S1, NOW)
snap2 = D.snapshot(P1, S1, NOW)
after = tree_hash(R1)
check("1) 화면 자료를 두 번 읽어도 파일·DB 바이트 그대로(만들기·스키마 변경·쓰기 없음)", before == after)
dm, rn = snap["daemon"], snap["runner"]
check("2) 관리자·실행기 실행 중(heartbeat 20초 전), 실행기 모드 KIWOOM_PAPER·코드 버전, 관리자 코드 버전은 로그에서",
      dm.data["label"] == "실행 중" and rn.data["label"] == "실행 중" and rn.data["execution_mode"] == "KIWOOM_PAPER"
      and "HEAD 5c3cac8abcd0" in dm.data["code_version"] and "HEAD" in rn.data["code_version"])
check("3) 자동 전략 미연결(NullStrategy) 표시, 주문 실행 단계도 '미연결'로 — 가짜 정상 없음",
      rn.data["strategy_label"].startswith("자동 전략 미연결")
      and any(r["단계"] == "주문 실행(실행기)" and "미연결" in r["상태"] for r in snap["progress"].data))
prog = {r["단계"]: r for r in snap["progress"].data}
check("4) 후보 0건은 '정상 완료'로, 일봉 입력 재확인 기록이 없으면 '기록 없음 — 그날 후보 승인 없음'으로 구분(M3 B)",
      prog["후보 확정"]["상태"] == "후보 0건(정상 완료)" and prog["일봉 입력 재확인(08:40)"]["상태"].startswith("기록 없음")
      and "승인 없음" in prog["일봉 입력 재확인(08:40)"]["상태"]
      and prog["개장 확인"]["상태"].startswith("COMPLETE"))
w = {r["코드"]: r for r in snap["watch"].data}
check("5) 감시 종목: HOLD 종목과 제외 사유(분석 HOLD·게이트), 가격 UNKNOWN 종목, S1 FAIL/UNKNOWN, 종목명",
      w["010120"]["분석"] == "HOLD" and "NO_TRADES" in w["010120"]["신규 진입 제외 사유"]
      and "ANALYSIS_HOLD" in w["010120"]["신규 진입 제외 사유"] and w["058470"]["가격 데이터"] == "UNKNOWN"
      and w["005930"]["S1"] == "FAIL" and w["005930"]["종목"] == "삼성전자" and w["005930"]["신규 진입 제외 사유"] == "-")
h = {r["종목"]: r for r in snap["holdings"].data}
check("6) 보유: 원장·잔고 수량·평균가·조회 시각, 손절·시간 청산은 '미구현'(가짜 값 없음)",
      h["005380"]["원장 수량"] == 1 and h["005380"]["잔고 수량(실행기)"] == 1 and h["005380"]["잔고 조회 시각"]
      and h["005380"]["손절 기준"].startswith("미구현") and h["005380"]["최근 평가가격"] == "미기록")
o = snap["orders"].data
check("7) 주문: M1 체결·지정가 취소 미확인·실행기 부분 체결(1/3, 미체결 2)·ERROR 종목 사람 확인",
      any(r["결과"] == "FILLED" and r["주문번호"] == "0112293" for r in o)
      and any(r["결과"] == "CANCEL_UNCONFIRMED" and r["주문 유형"] == "지정가" and r["가격"] == 206000 for r in o)
      and any("체결 1/3 · 미체결 2" in r["결과"] for r in o) and any("ERROR·orphan" in r["결과"] for r in o))
c = snap["costs"].data
check("8) 비용·손익: 체결 근거(잔고 평균단가 역산)와 추정(주문 직전 시세) 구분, 실현은 '추정 포함', 비용 모델은 추정 비용",
      any(r["가격 출처"].startswith("추정") for r in c["events"]) and any("브로커 근거" in r["가격 출처"] for r in c["events"])
      and c["realized"][0]["구분"] == "추정 포함" and c["cost_model"]["base"] == load_cost_model(R1 / "config/settings.yaml", use_cache=False).base_roundtrip_pct
      and any("체결 가격 검증이 아님" in n for n in snap["costs"].notes))
errs = snap["errors"].data
blob = json.dumps(errs, ensure_ascii=False)
check("9) 최근 오류: 실행기 429·RECOVERY_REQUIRED·관리자 429, 조치 안내, 토큰·계좌번호 가림",
      any("RECOVERY_REQUIRED" in r["내용"] and "ack" in r["조치"] for r in errs) and any(r["구성 요소"] == "관리자" for r in errs)
      and "SAMPLETOKEN123456" not in blob and "81234567" not in blob)
hv = snap["halt"].data
check("10) 중단 요청은 있으나 실행기 반영 전이면 '요청됨'", hv["state"] == "REQUESTED" and "반영 전" in hv["label"])
red = D.Redactor(S1)
zbytes = D.download_bundle(P1, NOW.date(), red, {k: D.section_dict(v) for k, v in snap.items() if isinstance(v, D.Section)})
z = zipfile.ZipFile(io.BytesIO(zbytes))
allz = "".join(z.read(n).decode("utf-8") for n in z.namelist())
check("11) 다운로드 zip: 요약·로그·주문 기록 포함, .env·DB 원본 없음, 앱키·시크릿·계좌번호·토큰 가림",
      any(n.startswith("logs/app_") for n in z.namelist()) and "summary_2026-10-14.json" in z.namelist()
      and not any(n.endswith((".env", ".sqlite3")) for n in z.namelist())
      and all(x not in allz for x in ("APPKEYSAMPLE123456", "SECRETSAMPLE987654", "81234567", "SAMPLETOKEN123456")))

# 2. 오래된 heartbeat·반영된 중단
R2, S2, P2 = env("stale", "stale")
sn = D.snapshot(P2, S2, NOW)
check("12) heartbeat가 오래되면 '실행 중'이 아니라 '응답 없음'(관리자·실행기 모두), 중단 반영됨",
      sn["daemon"].data["label"].startswith("응답 없음") and sn["runner"].data["label"].startswith("응답 없음")
      and sn["halt"].data["state"] == "APPLIED")

# 3. 파일 없음
R3, S3, P3 = env("empty", "empty")
se = D.snapshot(P3, S3, NOW)
check("13) 자료 없음: 관리자·실행기·감시 종목 '파일 없음', 보유 없음, 후보는 '기록 없음'(정상 완료로 보이지 않음), 아무 파일도 만들지 않음",
      se["daemon"].state == D.MISSING and se["runner"].state == D.MISSING and se["watch"].state == D.MISSING
      and se["holdings"].data == [] and any(r["단계"] == "후보 확정" and "기록 없음" in r["상태"] for r in se["progress"].data)
      and sorted(str(p.relative_to(R3)) for p in R3.rglob("*") if p.is_file()) == ["config/settings.yaml"])

# 4. 읽기 실패·잠금
R4, S4, P4 = env("broken", "normal")
P4.runner_status.write_text("{깨짐", encoding="utf-8")
(R4 / "data/watch/watch.sqlite3").write_bytes(b"not a database")
sb = D.snapshot(P4, S4, NOW)
lock = sqlite3.connect(R4 / "data/watch/daemon.sqlite3", isolation_level=None)
lock.execute("PRAGMA locking_mode=EXCLUSIVE")
lock.execute("BEGIN EXCLUSIVE")
sl = D.daemon_status(P4, NOW)
lock.execute("ROLLBACK")
lock.close()
check("14) 상태 파일 깨짐 → 실행기 '읽기 실패', 감시 DB 손상 → 감시 종목 '읽기 실패', 관리자 DB 잠금 → '잠금·일시 충돌'(재시도 후)",
      sb["runner"].state == D.UNREADABLE and sb["watch"].state == D.UNREADABLE and sl.state == D.LOCKED)

# 5. 중단 요청 — 화면 버튼과 같은 함수, 중복 클릭
from infra.storage.runner_status import request_buy_halt  # noqa: E402
R5, S5, P5 = env("halt", "empty")
r1 = request_buy_halt(P5.commands_dir, reason="화면", by="dashboard", now=NOW)
r2 = request_buy_halt(P5.commands_dir, reason="두 번째 클릭", by="dashboard", now=NOW)
v5 = D.buy_halt_view(P5, NOW).data
check("15) 중단 요청 버튼(같은 함수) 반복 클릭 → 요청 하나, 실행기 없으면 '요청됨'(반영된 것처럼 보이지 않음)",
      r1[0] and not r2[0] and r1[1]["request_id"] == r2[1]["request_id"] and v5["state"] == "REQUESTED")

# 6. 화면 코드 경계: 키움·주문·프로세스 실행을 부르지 않음
src = Path("dashboard/desktop.py").read_text(encoding="utf-8") + Path("dashboard/data.py").read_text(encoding="utf-8")
banned = ("place_order", "KiwoomBroker(", "build_broker", "subprocess", "Popen", "os.system", "run_session",
          "authenticate(", "watch_daemon.py run")
check("16) 화면 코드에 주문·인증·브로커 생성·프로세스 실행 없음(명령은 복사용 문자열로만)",
      not any(b in src.replace('"python -u tools/watch_daemon.py run', "") for b in banned[:-1]))

# 7. 체결 근거(M3 A2) — 추정 매도에 ka10076 근거가 붙으면 체결가·수수료·세금 표시, 읽기만
from domain.service.fill_evidence import FillEvidenceRecord  # noqa: E402
from infra.storage.fill_evidence import FillEvidenceStore  # noqa: E402

R7, S7, P7 = env("evidence", "normal")
FillEvidenceStore(P7.fill_ledger.with_name("fill_evidence.jsonl")).append(FillEvidenceRecord(
    "acct-swing|20261013|SELL|005930|0112348|1", "0112348", "005930", "SELL", 1, 229_500, 800, 457, "ka10076",
    "CONFIRM_TOOL", NOW))
b7 = tree_hash(R7)
c7 = D.costs(P7, S7)
sell7 = next(r for r in c7.data["events"] if r["구분"] == "SELL")
check("17) 비용·체결 근거: 근거가 붙은 매도는 체결가 229,500(브로커 근거)·수수료 800·세금 457, 근거 없는 사건은 '-', "
      "화면 읽기로 파일 변화 없음",
      sell7["가격"] == 229_500 and sell7["가격 출처"].startswith("체결가") and (sell7["수수료"], sell7["세금"]) == (800, 457)
      and all(r["수수료"] == "-" for r in c7.data["events"] if r["구분"] == "BUY") and tree_hash(R7) == b7
      and any("체결 근거가 있는 사건만" in n for n in c7.notes))

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
