# -*- coding: utf-8 -*-
"""M3 B: 개장 전 입력 재확인(INPUT_CHECK) — 가짜 시계·가짜 키움(test_watch_daemon.py와 같은 장치), 임시 폴더만.

    python test_input_check.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_HEAD = (Path(__file__).resolve().parent / "test_watch_daemon.py").read_text(encoding="utf-8").split("# ── 1. 일정")[0]
exec(compile(_HEAD, "test_watch_daemon_head", "exec"))        # Clock·FakeKiwoom·env·daemon·tasks·S_5930 등 같은 장치

from infra.watch.manager import WATCH_AFTER_CLOSE  # noqa: E402

D108 = date(2026, 10, 8)


def prepared(name: str, *, symbols=None):
    """10/7 19:00 마감 준비 완료(005930 PASS·actionable, 개장 전 저장) 상태의 관리자."""
    e = env(name, datetime(2026, 10, 7, 19, 0), symbols or [S_5930])
    d = daemon(e)
    d.dstore.set_meta("since", "2026-10-07")
    d.dstore.conn.execute("DELETE FROM task")
    for _ in range(3):                       # 그날 OPEN_CHECK(후보 없음)가 먼저 돌 수 있음
        r = d.tick()
        if (r["ran"] or "").startswith("CLOSE_PREP|2026-10-07"):
            break
    assert (r["ran"] or "").startswith("CLOSE_PREP|2026-10-07") and r["status"] == "COMPLETE", r
    return e, d


def research_bytes(e) -> bytes:
    return e["paths"].research_db.read_bytes()


# 0. 일정
E0, d0 = prepared("sched")
dt = d0.due_times(D108)
check("0-1) 개장 전 입력 재확인은 개장 20분 전(08:40)부터, 기한 개장 5분 전(08:55) — 특수 개장일(1/2 10:00)은 09:40~09:55. "
      "관리자 기본 완성 기준은 마감 + 300분(20:30)",
      dt[D.INPUT_CHECK] == (datetime(2026, 10, 8, 8, 40), datetime(2026, 10, 8, 8, 55))
      and d0.due_times(date(2026, 1, 2))[D.INPUT_CHECK] == (datetime(2026, 1, 2, 9, 40), datetime(2026, 1, 2, 9, 55))
      and WATCH_AFTER_CLOSE == timedelta(minutes=300))
at(E0, datetime(2026, 10, 8, 8, 39))
check("0-2) 08:39에는 할 일 없음, 다음 예정 08:40", d0.tick()["ran"] is None
      and d0.next_due(datetime(2026, 10, 8, 8, 39)) == datetime(2026, 10, 8, 8, 40))

# 1. 정상 — 다시 받은 값이 스캔 당시와 같음
before = research_bytes(E0)
n0 = len(E0["fake"].calls)
at(E0, datetime(2026, 10, 8, 8, 40))
r1 = d0.tick()
t1 = tasks(E0)[("INPUT_CHECK", "2026-10-08")]
rows1 = d0.dstore.input_checks("2026-10-08")
calls1 = api_codes(E0, n0)
check("1-1) 08:40 입력 재확인: 후보 005930의 종목·지수(KOSPI 001) 입력 창을 다시 받아 비교 → 둘 다 STABLE, 승인 [005930], "
      "조회는 ka10081 005930·ka20006 001뿐",
      r1["ran"] == "INPUT_CHECK|2026-10-08|OPEN-20m" and t1["status"] == "COMPLETE"
      and t1["detail"]["approved"] == ["005930"] and sorted((r["role"], r["status"]) for r in rows1) ==
      [("INDEX", "STABLE"), ("STOCK", "STABLE")] and sorted(set(calls1)) == [("ka10081", "005930"), ("ka20006", "001")]
      and all(r["committed_at"] and r["committed_at"] < "2026-10-08T08:55:00" for r in rows1))
check("1-2) 원본 보존: 다시 받은 값은 비교만 — 연구 DB 파일 바이트 그대로, 창은 스캔과 같은 300세션(신호일까지)",
      research_bytes(E0) == before and all(r["window_end"] == "2026-10-07" and r["scan_bars"] == r["fresh_bars"]
                                           and r["scan_digest"] == r["fresh_digest"] for r in rows1))
check("1-3) 시장 기준은 KRX(10/13 실측: 단축코드 = KRX 단독)로 기록 — 보고서에 재확인 결과",
      all(r["market_basis"].startswith("KRX(10/13 실측") for r in rows1 if r["role"] == "STOCK")
      and "개장 전 입력 재확인" in Path(t1["report_path"]).read_text(encoding="utf-8"))
n14 = len(E0["fake"].calls)
r14 = d0.tick()
check("1-4) 끝난 재확인은 다시 하지 않음(조회 0회)", not (r14["ran"] or "").startswith("INPUT_CHECK")
      and not [c for c in api_codes(E0, n14) if c[0] in ("ka10081", "ka20006")])
d0.stop()

# 2. 신호일 봉이 나중에 바뀜(10/12 재현과 같은 모양 — 종가·저가)
E2, d2 = prepared("changed")
old = E2["fake"].series["005930"][PASS_DAY]
E2["fake"].series["005930"][PASS_DAY] = RawBar(PASS_DAY, old.open_raw, old.high_raw, old.low_raw - 10,
                                                old.close_raw + 10, old.volume + 1234, old.trade_value_raw, "")
at(E2, datetime(2026, 10, 8, 8, 41))
d2.tick()
t2 = tasks(E2)[("INPUT_CHECK", "2026-10-08")]
st2 = {r["role"]: r for r in d2.dstore.input_checks("2026-10-08")}
check("2) 신호일 봉의 종가·저가·거래량이 바뀌면 종목 INPUT_CHANGED(바뀐 날짜·필드 기록), 지수는 STABLE, 승인 없음",
      st2["STOCK"]["status"] == "INPUT_CHANGED" and st2["INDEX"]["status"] == "STABLE"
      and any("2026-10-07 close" in x for x in st2["STOCK"]["diffs"]) and any("low" in x for x in st2["STOCK"]["diffs"])
      and t2["detail"]["approved"] == [] and t2["status"] == "COMPLETE")
d2.stop()

# 3. 조회 실패 → PARTIAL, 기한 안에서 다시(새 행), 성공하면 승인
E3, d3 = prepared("fail")
E3["fake"].fail["005930"] = ["RC"]
at(E3, datetime(2026, 10, 8, 8, 40))
d3.tick()
t3a = tasks(E3)[("INPUT_CHECK", "2026-10-08")]
at(E3, datetime(2026, 10, 8, 8, 46))
d3.tick()
t3b = tasks(E3)[("INPUT_CHECK", "2026-10-08")]
rows3 = d3.dstore.input_checks("2026-10-08")
check("3) 조회 실패 → CHECK_FAILED·PARTIAL(승인 없음), 5분 뒤 다시 시도해 STABLE → 승인. 이전 행은 지우지 않고 새 행 추가",
      t3a["status"] == "PARTIAL" and t3a["detail"]["approved"] == [] and t3b["status"] == "COMPLETE"
      and t3b["detail"]["approved"] == ["005930"] and [r["status"] for r in rows3 if r["role"] == "STOCK"] ==
      ["CHECK_FAILED", "STABLE"])
d3.stop()

# 4. 기한
E4, d4 = prepared("deadline")
E4["fake"].fail["005930"] = ["RC"]
at(E4, datetime(2026, 10, 8, 8, 52))
d4.tick()
at(E4, datetime(2026, 10, 8, 8, 56))
r4 = d4.tick()
t4 = tasks(E4)[("INPUT_CHECK", "2026-10-08")]
check("4-1) 기한(08:55)을 넘기면 다시 시도하지 않고 MISSED — 그날 후보 승인 없음(개장 뒤 재확인 없음)",
      t4["status"] == "MISSED" and "승인되지 않음" in (t4["error"] or "") and not (r4["ran"] or "").startswith("INPUT"))
d4.stop()
E5, d5 = prepared("late_start")
at(E5, datetime(2026, 10, 8, 8, 57))
d5.tick()
check("4-2) 관리자를 기한 뒤에 켜면 그날 재확인 작업을 만들지 않음(지난 시각 확인 없음)",
      ("INPUT_CHECK", "2026-10-08") not in tasks(E5))
d5.stop()

# 5. 후보 없음 → 조회 0회
E6 = env("none", datetime(2026, 10, 7, 19, 0), [{"code": "000660", "interest": {"enabled": True, "s1_analysis": True}}])
d6 = daemon(E6)
d6.dstore.set_meta("since", "2026-10-07")
d6.dstore.conn.execute("DELETE FROM task")
d6.tick()
d6.tick()
n6 = len(E6["fake"].calls)
at(E6, datetime(2026, 10, 8, 8, 40))
d6.tick()
t6 = tasks(E6)[("INPUT_CHECK", "2026-10-08")]
check("5) 후보가 없으면 조회 없이 COMPLETE(NO_CANDIDATES)", t6["status"] == "COMPLETE"
      and t6["detail"]["result"] == "NO_CANDIDATES" and len(E6["fake"].calls) == n6)
d6.stop()

# 6. 스키마 wd3 → wd4
p7 = TMP / "old.sqlite3"
with D.DaemonStore(p7) as ds:
    ds.conn.execute("DROP TABLE input_check")
    ds.set_meta("daemon_schema", "wd3")
with D.DaemonStore(p7) as ds:
    bak = ds.meta("upgrade_backup")
    has = ds.conn.execute("SELECT 1 FROM sqlite_master WHERE name='input_check'").fetchone()
    sch = ds.meta("daemon_schema")
with sqlite3.connect(bak) as cx:
    old_sch = cx.execute("SELECT value FROM meta WHERE key='daemon_schema'").fetchone()[0]
check("6) 관리자 DB wd3 → wd4: 올리기 전 사본(.bak-wd3-) 뒤 input_check 표 추가, 기존 표는 그대로",
      has and sch == "wd4" and ".bak-wd3-" in bak and old_sch == "wd3")

# 9. C1(GPT R3): 작업은 기한 전에 시작했지만 조회가 늦게 끝남 → LATE로 기록, 승인 없음
E9, d9 = prepared("late_finish")
E9["fake"].tick = timedelta(seconds=50)
at(E9, datetime(2026, 10, 8, 8, 54, 30))
d9.tick()
t9 = tasks(E9)[("INPUT_CHECK", "2026-10-08")]
rows9 = d9.dstore.input_checks("2026-10-08")
check("9) 기한(08:55) 전에 시작해도 확인이 기한 뒤에 끝난 행은 LATE:STABLE(승인 근거 아님)로 남기고 승인 없음",
      rows9 and any(r["status"].startswith("LATE:") for r in rows9) and t9["detail"]["approved"] == []
      and any("기한" in x for r in rows9 for x in r["diffs"] if r["status"].startswith("LATE:")))
d9.stop()

# 8. 시장 기준 실측 도구(읽기 전용) — 가짜 응답
from infra.research.kiwoom_readonly import Page  # noqa: E402
from tools import probe_market_basis as PMB  # noqa: E402


class FC:
    def __init__(self):
        self.req = []

    def fetch_page(self, api, payload, key, cont, nk):
        self.req.append((api, payload["stk_cd"]))
        if payload["stk_cd"].endswith("_NX"):
            raise ResearchApiError("return_code=1 종목코드 오류(가짜)")
        close = 251000 if payload["stk_cd"].endswith("_AL") else 250000
        rows = [{"dt": "20261012", "open_pric": "+249000", "high_pric": "+252000", "low_pric": "-249000",
                 "cur_prc": f"+{close}", "trde_qty": "100", "trde_prica": "25"}]
        return Page(rows, "N", "", datetime(2026, 10, 13, 10, 0))


fc, slept = FC(), []
outp = TMP / "basis.json"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc8 = PMB.main(["005930", "--days", "3", "--out", str(outp)], client=fc, today=date(2026, 10, 13),
                   sleep=slept.append)
    rc8b = PMB.main(["005930", "--out", str(outp)], client=FC(), today=date(2026, 10, 13), sleep=slept.append)
o8 = buf.getvalue()
j8 = json.loads(outp.read_text(encoding="utf-8"))
check("8) 시장 기준 도구: ka10081만 단축코드·_NX·_AL 순서로(사이 1.2초), 접미사 오류는 그대로 기록(종료 1), 단축코드 값이 _AL과 "
      "다름을 표시, --out은 새 파일만(있으면 거부 2)",
      [a for a, _ in fc.req] == ["ka10081"] * 3 and [c for _, c in fc.req] == ["005930", "005930_NX", "005930_AL"]
      and slept[:2] == [1.2, 1.2] and rc8 == 1 and "error" in j8["responses"]["_NX"]
      and j8["compare"][0]["same_as_AL"] is False and "다름(종가 251000" in o8 and rc8b == 2)

FS_AFTER = {p: _fs(p) for p in ("data", "commands", "reports", "logs")}
check("7) 저장소 data/·commands/·reports/·logs/에 아무것도 만들지 않음", FS_AFTER == FS_BEFORE)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
