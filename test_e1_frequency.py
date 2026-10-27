# -*- coding: utf-8 -*-
"""E1 (GPT 10/21): 단계별 건수 · S1 신호 빈도 분석(비교 변형) · 관찰 확대(suggest·add-many) · 작은 보완.

    python test_e1_frequency.py

가짜 시계·가짜 키움(test_watch_daemon.py 장치), 임시 폴더만. 실제 API·주문 없음.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_HEAD = (Path(__file__).resolve().parent / "test_watch_daemon.py").read_text(encoding="utf-8").split("# ── 1. 일정")[0]
exec(compile(_HEAD, "test_watch_daemon_head", "exec"))

from domain.research.s1_frequency import VARIANTS  # noqa: E402
from domain.research.s1_funnel import funnel, funnel_line  # noqa: E402
import tools.s1_frequency as SF  # noqa: E402
import tools.watchlist as W  # noqa: E402

# 1. 단계별 건수(순수)
rows = [
    {"data_status": "OK", "pattern_pass": "PASS", "eligibility_pass": "PASS", "market_pass": "FAIL", "stop_valid": "PASS",
     "eligible_signal": "FAIL", "checks": [{"name": "MARKET_REGIME", "result": "FAIL"}]},
    {"data_status": "OK", "pattern_pass": "PASS", "eligibility_pass": "PASS", "market_pass": "PASS", "stop_valid": "PASS",
     "eligible_signal": "PASS", "checks": []},
    {"data_status": "OK", "pattern_pass": "FAIL", "eligibility_pass": "PASS", "market_pass": "PASS", "stop_valid": "UNKNOWN",
     "eligible_signal": "FAIL", "checks": [{"name": "C_GT_MA120", "result": "FAIL"}]},
    {"data_status": "NO_BAR_AT_T", "pattern_pass": "UNKNOWN", "eligibility_pass": "UNKNOWN", "market_pass": "UNKNOWN",
     "stop_valid": "UNKNOWN", "eligible_signal": "UNKNOWN"},
]
f = funnel(rows)
check("1-1) 단계별 건수: 분석 4 → 데이터 준비 3 → 패턴 2 → 자격 2 → 시장 1 → 손절 1 → 최종 1(순차), 시장 조건만으로 제외 1, "
      "독립 시장 PASS 2(패턴 실패 줄 포함) — 두 집계를 이름으로 구분",
      f["analyzed"] == 4 and f["data_ready"] == 3 and f["sequential"] == {"pattern_pass": 2, "eligibility_pass": 2,
                                                                       "market_pass": 1, "stop_valid": 1}
      and f["final"] == 1 and f["blocked_by_market"] == 1 and f["independent"]["market_pass"] == 2
      and f["fail_checks"] == {"C_GT_MA120": 1, "MARKET_REGIME": 1} and "(순차)" in funnel_line(f) and "독립" in funnel_line(f))

from domain.research.s1_frequency import variants_of  # noqa: E402


def mk(pattern_checks, market_state, market_pass):
    checks = [{"name": n_, "result": r_} for n_, r_ in pattern_checks] + [
        {"name": "SECURITY_TYPE", "result": "PASS"}, {"name": "RISK_STATUS", "result": "PASS"},
        {"name": "LIQUIDITY_TV20", "result": "PASS"}, {"name": "MARKET_REGIME", "result": market_pass},
        {"name": "STOP_VALID", "result": "PASS"}]
    pp = "FAIL" if any(r_ == "FAIL" for _, r_ in pattern_checks) else "PASS"
    es = "FAIL" if "FAIL" in (pp, market_pass) else "PASS"
    return {"checks": checks, "pattern_pass": pp, "eligibility_pass": "PASS", "market_pass": market_pass,
            "stop_valid": "PASS", "eligible_signal": es, "market": {"state": market_state}}


va = variants_of(mk([("PULLBACK", "PASS")], "MIXED", "FAIL"), None)
vb = variants_of(mk([("PULLBACK", "PASS")], "RISK_OFF", "FAIL"), None)
vc = variants_of(mk([("C_GT_MA120", "FAIL"), ("PULLBACK", "PASS")], "RISK_ON", "PASS"), None)
vd = variants_of(mk([("PULLBACK", "FAIL")], "RISK_ON", "PASS"), None)
check("1-2) 비교 변형 판정: 시장 MIXED만 막힘 → BASE FAIL·시장 제외 PASS·MIXED 허용 PASS / RISK_OFF → MIXED 허용도 FAIL / "
      "추세(120일선)만 막힘 → 추세 제외 PASS / 눌림 실패 → 추세 제외도 FAIL",
      (va["BASE"], va["NO_MARKET"], va["MARKET_MIXED_OK"]) == ("FAIL", "PASS", "PASS")
      and (vb["NO_MARKET"], vb["MARKET_MIXED_OK"]) == ("PASS", "FAIL")
      and (vc["BASE"], vc["NO_TREND"], vc["NO_MARKET"]) == ("FAIL", "PASS", "FAIL")
      and vd["NO_TREND"] == "FAIL")

# 2. 관리자 마감 준비 → 단계별 건수 기록·보고서·관리 창
E = env("e1", datetime(2026, 10, 7, 19, 0), [S_5930, {"code": "000660", "interest": {"enabled": True, "s1_analysis": True}}])
d = daemon(E)
d.dstore.set_meta("since", "2026-10-07")
d.dstore.conn.execute("DELETE FROM task")
for _ in range(3):
    r = d.tick()
    if (r["ran"] or "").startswith("CLOSE_PREP"):
        break
prep = [t for t in tasks(E).values() if t["kind"] == "CLOSE_PREP"][0]
fz = prep["detail"]["s1"]["funnel"]
rep = Path(prep["report_path"]).read_text(encoding="utf-8")
check("2-1) 마감 준비 작업 기록에 단계별 건수(2종목: 005930 최종 PASS 1), 일일 보고서에 '단계별 건수' 줄과 순차·독립 설명",
      fz["analyzed"] == 2 and fz["final"] == 1 and "단계별 건수: 분석 2" in rep and "순차 = 앞 단계" in rep)
from dashboard.data import Paths as DPaths, today_progress, Section, OK as DOK  # noqa: E402
d.stop()

# 3. 빈도 분석 — 같은 연구 DB, 운영 스캔과 같은 판정
rdb = E["paths"].research_db
h0 = hashlib.sha256(rdb.read_bytes()).hexdigest()
out_dir = TMP / "freq"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = SF.main(["--db", str(rdb), "--codes", "005930,000660,999999", "--days", "5", "--end", "2026-10-07",
                  "--out-dir", str(out_dir)])
md = next(out_dir.glob("s1_frequency_2026-10-07_*.md")).read_text(encoding="utf-8")
csv_text = next(out_dir.glob("s1_frequency_2026-10-07_*.csv")).read_text(encoding="utf-8-sig")
with D.DaemonStore(E["paths"].daemon_db) as ds:
    pass
from infra.research.scan_store import ScanStore  # noqa: E402
with ScanStore(E["paths"].watch_scan_db) as ss:
    run = [x for x in ss.runs("2026-10-07") if x["status"] == "COMPLETE"][-1]
    scan_eval = {e["symbol"]: e["eligible_signal"] for e in ss.evals(run["run_id"])}
base_pass = [ln for ln in csv_text.splitlines()[1:] if ln.startswith("2026-10-07,005930")]
check("3-1) 빈도 분석: 같은 연구 DB에서 10/7 005930 BASE PASS·000660 PASS 아님 — 운영 스캔 판정과 일치. 보고서(단계별 건수·"
      "비교 변형·시장 판정·종목별·분석 못 한 종목 999999)와 CSV 생성",
      rc == 0 and scan_eval == {"005930": "PASS", "000660": "FAIL"} and base_pass and base_pass[0].split(",")[4] == "PASS"
      and "## 2. 비교 변형" in md and "999999" in md and "연구용 근사" in md and "달력 연도 구간 일치" in md)
check("3-2) 연구 DB는 읽기 전용 — 분석 전후 파일 바이트 같음(API 호출 없음)",
      hashlib.sha256(rdb.read_bytes()).hexdigest() == h0)
from infra.research.store import ResearchStore  # noqa: E402
with ResearchStore(rdb, readonly=True) as _rs:
    _stocks, _idx, _, _ = SF.load_inputs(_rs, ["005930", "000660"])
_days = sorted(b.date for b in _idx["INDEX:KOSPI:001"] if b.date <= date(2026, 10, 7))
res = SF.analyze(_days[-20:], lambda t: [x for x in _days if x <= t][-300:], _stocks, _idx)
mono = all(all(r["variants"][v] == "PASS" for v in ("NO_MARKET", "MARKET_MIXED_OK", "NO_TREND"))
           for r in res.rows if r["variants"]["BASE"] == "PASS")
check("3-3) 비교 변형은 BASE보다 넓기만 함(BASE PASS면 시장 제외·MIXED 허용·추세 제외도 PASS), 변형 5종 모두 계산",
      mono and all(set(r["variants"]) == set(VARIANTS) for r in res.rows if r["data_status"] == "OK"))

# 4. 관찰 확대 후보·한 번에 추가
EW = {"cfg": E["paths"].config, "wdb": E["paths"].watch_db, "db": rdb, "client": E["client"], "clock": E["clock"]}


def cli(*args):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        code = W.main(["--config", str(EW["cfg"]), "--watch-db", str(EW["wdb"]), "--db", str(EW["db"]),
                       "--after-close-min", "160", *args], client=EW["client"], now=EW["clock"], calendar=CAL)
    return code, b.getvalue()


cfg_before = EW["cfg"].read_text(encoding="utf-8")
c1, o1 = cli("suggest", "--kospi", "5", "--kosdaq", "5")
check("4-1) suggest: 보통주·위험 없음만(ETF 069500 제외), 이미 있는 005930·000660 제외, 시장별 시가총액 순 → 035420(KOSPI)·"
      "247540(KOSDAQ), add-many 명령 출력. 설정 파일 그대로",
      c1 == 0 and "035420" in o1 and "247540" in o1 and "069500" not in o1 and "| 005930" not in o1
      and 'add-many "035420,247540" --interest --s1' in o1 and EW["cfg"].read_text(encoding="utf-8") == cfg_before)
with WatchStore(EW["wdb"]) as ws:
    v_before = len(ws.history(1000))
E["fake"].series["247540"] = flat_series(50_000)
c2, o2 = cli("add-many", "035420,247540", "--interest", "--s1")
with WatchStore(EW["wdb"]) as ws:
    hist = ws.history(1000)
from domain.watchlist.config import parse_text  # noqa: E402
doc = parse_text(EW["cfg"].read_text(encoding="utf-8"))[0]
codes_now = [s["code"] for s in doc["symbols"]]
check("4-2) add-many: 두 종목을 설정 1버전으로 추가(관심·S1 켜짐), 이력 1건(origin add-many 2), 과거 일봉 준비까지",
      c2 in (0, 1) and len(hist) == v_before + 1 and hist[0]["origin"] == "CLI:add-many 2"
      and {"035420", "247540"} <= set(codes_now) and "add-many 2종목" in o2)
c3, o3 = cli("add-many", "035420,000270", "--interest", "--s1")
with WatchStore(EW["wdb"]) as ws:
    hist3 = ws.history(1000)
check("4-3) 하나라도 이미 있으면(035420) 아무것도 바꾸지 않고 거부 — 이력 그대로",
      c3 == 2 and "[거부]" in o3 and len(hist3) == len(hist))

# 5. 관리자 업그레이드 문구는 한 번만
E5 = env("upgrade_log", datetime(2026, 10, 7, 12, 0), [S_5930])
d5 = daemon(E5)
d5.dstore.set_meta("upgraded_from", "wd3")
d5.stop()
for _ in range(2):
    dx = daemon(E5)
    dx.stop()
n_up = sum("관리자 DB wd3 → wd4" in m for m in E5["logs"])
check("5-1) 관리자 DB 업그레이드 문구는 실제로 올린 뒤 첫 기동에만(재기동 두 번에 1회)", n_up == 1)

# 6. 잔고 조회 복구 로그
from unittest.mock import Mock  # noqa: E402
from app.main import run_session  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402


class Flaky(ScriptedBroker):
    def __init__(self):
        super().__init__()
        self.n = 0

    def get_account_balance(self):
        self.n += 1
        if 2 <= self.n <= 4:
            raise RuntimeError("kiwoom business error [2000](1:network error)")
        return super().get_account_balance()


class Clk:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


s6 = build_minimal_settings(str(TMP / "rec"))
(TMP / "rec").mkdir(exist_ok=True)
ck = Clk(datetime(2026, 10, 21, 10, 24))
lg = Mock()
run_session(s6, Flaky(), lg, clock=ck, sleep=ck.sleep, should_stop=lambda: ck.t >= datetime(2026, 10, 21, 10, 32))
msgs = [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
check("6-1) 잔고 조회 연속 실패 뒤 성공하면 '복구 — 연속 실패 3회 (시작 시각부터)' 한 줄",
      any("잔고 조회 복구 — 연속 실패 3회" in m and "부터" in m for m in msgs)
      and sum("잔고 조회 복구" in m for m in msgs) == 1)

# 8. 번들에 관리자 진단 요약
import zipfile  # noqa: E402
from app.reports import export_bundle  # noqa: E402
broot = TMP / "broot"
(broot / "reports" / "watch" / "daily").mkdir(parents=True)
(broot / "reports" / "watch" / "s1").mkdir(parents=True)
(broot / "reports" / "watch" / "daily" / "watch_2026-10-21.md").write_text("오늘 보고서", encoding="utf-8")
(broot / "reports" / "watch" / "daily" / "watch_2026-10-20.md").write_text("어제 보고서", encoding="utf-8")
(broot / "reports" / "watch" / "s1" / "s1_scan_2026-10-20_r1.md").write_text("탈락 사유 표", encoding="utf-8")
zp = export_bundle(s6, date(2026, 10, 21), root=broot, out_dir=TMP / "bx")
nm = zipfile.ZipFile(zp).namelist()
check("8-1) 운영 번들에 관리자 일일 보고서(그날·전날)와 최근 S1 스캔 보고서 포함 — DB 사본 없이 탈락 사유 확인",
      {"watch/watch_2026-10-21.md", "watch/watch_2026-10-20.md", "watch/s1/s1_scan_2026-10-20_r1.md"} <= set(nm))

repo = Path(__file__).resolve().parent
check("7) 저장소 reports/research·data에 시험 파일 없음",
      not (repo / "reports" / "research" / "s1_frequency_2026-10-07.md").exists())

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
