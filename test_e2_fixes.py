# -*- coding: utf-8 -*-
"""E2 (GPT 10/22 검토): suggest 진짜 읽기 전용 · '시장 조건만으로 제외' 정확히 · 시장 판정 분포 독립 계산 ·
번들 S1 보고서는 날짜 기준 · 빈도 분석 결과 덮어쓰지 않음.

    python test_e2_fixes.py

가짜 시계·가짜 키움(test_watch_daemon.py 장치), 임시 폴더만.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import time as _t
import zipfile
from datetime import date, datetime
from pathlib import Path

_HEAD = (Path(__file__).resolve().parent / "test_watch_daemon.py").read_text(encoding="utf-8").split("# ── 1. 일정")[0]
exec(compile(_HEAD, "test_watch_daemon_head", "exec"))

import tools.s1_frequency as SF  # noqa: E402
import tools.watchlist as W  # noqa: E402
from domain.research.s1_funnel import funnel, funnel_line  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402

# 1. suggest 읽기 전용
empty = TMP / "empty"
empty.mkdir()
b = io.StringIO()
with contextlib.redirect_stdout(b):
    c0 = W.main(["--config", str(empty / "w.yaml"), "--watch-db", str(empty / "watch.sqlite3"), "--db",
                 str(empty / "research.sqlite3"), "suggest"])
check("1-1) GPT 재현: 빈 경로에서 suggest → 실패해도 DB를 하나도 만들지 않음(이전 코드: 연구·감시 DB 2개 생성)",
      c0 == 2 and not any(empty.iterdir()))
E = env("ro", datetime(2026, 10, 7, 19, 0), [S_5930])
d = daemon(E)
d.dstore.set_meta("since", "2026-10-07")
d.dstore.conn.execute("DELETE FROM task")
for _ in range(3):
    if (d.tick()["ran"] or "").startswith("CLOSE_PREP"):
        break
d.stop()
rdb = E["paths"].research_db
h0 = hashlib.sha256(rdb.read_bytes()).hexdigest()
wdb_new = TMP / "ro" / "watch_new.sqlite3"
b = io.StringIO()
with contextlib.redirect_stdout(b):
    c1 = W.main(["--config", str(E["paths"].config), "--watch-db", str(wdb_new), "--db", str(rdb), "suggest",
                 "--kospi", "3", "--kosdaq", "1"])
check("1-2) 정상 suggest: 연구 DB 바이트 그대로(mode=ro), 감시 DB는 열지도 만들지도 않음, 결과는 그대로(000660·035420·247540)",
      c1 == 0 and hashlib.sha256(rdb.read_bytes()).hexdigest() == h0 and not wdb_new.exists()
      and all(x in b.getvalue() for x in ("000660", "035420", "247540")))

# 2. 시장 조건만으로 제외
P_, F_, U_ = "PASS", "FAIL", "UNKNOWN"


def row(pat, elig, mk, stop):
    es = F_ if F_ in (pat, elig, mk, stop) else (U_ if U_ in (pat, elig, mk, stop) else P_)
    return {"data_status": "OK", "pattern_pass": pat, "eligibility_pass": elig, "market_pass": mk, "stop_valid": stop,
            "eligible_signal": es, "checks": []}


f2 = funnel([row(P_, P_, F_, F_), row(P_, P_, F_, P_), row(P_, P_, U_, P_), row(P_, P_, P_, P_)])
check("2-1) GPT 재현: 시장·손절 기준 둘 다 실패한 줄은 '시장 조건만으로 제외'에서 뺌(이전 코드: 3) → 시장 FAIL만 1, 판정 불가 1 따로",
      f2["blocked_by_market"] == 1 and f2["market_unknown_only"] == 1 and f2["final"] == 1
      and "판정 불가 1 별도" in funnel_line(f2))

# 3. 시장 판정 분포는 종목 구성과 무관
with ResearchStore(rdb, readonly=True) as rs:
    stocks, idx, _, _ = SF.load_inputs(rs, ["005930"])           # KOSPI 종목만
days = sorted(x.date for x in idx["INDEX:KOSPI:001"] if x.date <= date(2026, 10, 7))
res = SF.analyze(days[-5:], lambda t: [x for x in days if x <= t][-300:], stocks, idx)
check("3-1) GPT 재현: KOSPI 종목만 넣어도 날짜마다 KOSDAQ 지수 판정이 있음(이전 코드: 빠짐)",
      all(set(reg) == {"INDEX:KOSPI:001", "INDEX:KOSDAQ:101"} and all(reg.values()) for reg in res.regimes.values()))
out = TMP / "freq"
for _ in range(2):
    with contextlib.redirect_stdout(io.StringIO()):
        SF.main(["--db", str(rdb), "--codes", "005930", "--days", "3", "--end", "2026-10-07", "--out-dir", str(out)])
    _t.sleep(1.1)
mds = sorted(out.glob("s1_frequency_2026-10-07*.md"))
md_text = mds[0].read_text(encoding="utf-8")
check("3-2) 같은 끝날짜로 두 번 돌려도 덮어쓰지 않음(두 번째는 시각·종목 수 붙인 이름), 보고서 시장 분포에 KOSDAQ 줄 값 있음",
      len(mds) == 2 and "INDEX:KOSDAQ:101 | 0 | 0 | 0 | 0" not in md_text)

# 4. 번들 S1 보고서는 날짜 기준
from app.reports import export_bundle  # noqa: E402
from testing_helpers import build_minimal_settings  # noqa: E402
broot = TMP / "broot"
s1d = broot / "reports" / "watch" / "s1"
s1d.mkdir(parents=True)
(broot / "reports" / "watch" / "daily").mkdir(parents=True)
for nm in ("s1_scan_2026-10-19_a.md", "s1_scan_2026-10-20_b.md", "s1_scan_2026-10-21_c.md"):
    (s1d / nm).write_text(nm, encoding="utf-8")
os.utime(s1d / "s1_scan_2026-10-21_c.md", None)                 # 가장 최근 수정 = 다른 날 자료
(TMP / "bs").mkdir()
zp = export_bundle(build_minimal_settings(str(TMP / "bs")), date(2026, 10, 20), root=broot, out_dir=TMP / "bx")
names = zipfile.ZipFile(zp).namelist()
check("4-1) 과거 날짜(10/20) 번들: 그날·전 거래일 신호일(10/19·10/20) 스캔 보고서만 — 나중에 만든 10/21 보고서는 넣지 않음"
      "(이전 코드: 수정 시각 최근 2개)",
      "watch/s1/s1_scan_2026-10-19_a.md" in names and "watch/s1/s1_scan_2026-10-20_b.md" in names
      and "watch/s1/s1_scan_2026-10-21_c.md" not in names)

# 5. E3 통합 — 그룹·기간 비교, 결과 측정 도구(같은 연구 DB, 읽기 전용)
import tools.s1_outcome as SO  # noqa: E402
gout = TMP / "grp"
with contextlib.redirect_stdout(io.StringIO()):
    gr = SF.main(["--db", str(rdb), "--group", "a=005930", "--group", "b=005930,000660", "--days", "5", "--end",
                  "2026-10-07", "--recent-from", "2026-10-06", "--out-dir", str(gout)])
gmd = next(gout.glob("*.md")).read_text(encoding="utf-8")
check("5-1) 그룹·기간 비교: 그룹 a·b × 전체·최근 4줄, 월별 표(그룹별), 눌림 세부 사유, 중복 탈락 주의 문구, 실행 ID",
      gr == 0 and gmd.count("| a | ") == 2 and gmd.count("| b | ") == 2 and "### a (1종목)" in gmd
      and "눌림(PULLBACK) 실패 세부 사유" in gmd and "중복" in gmd and "실행 ID" in gmd and "4-1. 종목별 일봉 품질" in gmd)
oout = TMP / "out"
b = io.StringIO()
with contextlib.redirect_stdout(b):
    orc = SO.main(["--db", str(rdb), "--codes", "005930,000660", "--days", "5", "--end", "2026-10-07",
                   "--out-dir", str(oout)])
omd = next(oout.glob("*.md")).read_text(encoding="utf-8")
ocsv = next(oout.glob("*.csv")).read_text(encoding="utf-8-sig")
check("5-2) 결과 측정 도구: 10/7 005930 신호 1건 → 진입일 없음(데이터 끝 — 다음 날 봉 없음)으로 분류, 보고서에 '운영 체결 재현이 아님'·"
      "처리 기준, CSV 행, 연구 DB 바이트 그대로",
      orc == 0 and "운영 체결 재현이 아님" in omd and "처리 기준" in omd and "2026-10-07,005930" in ocsv
      and "계좌 기준" in omd and "진입 시 손절 거리" in omd and "계좌 수익률·계좌 최대 낙폭이 아님" not in omd
      and "stop_dist_pct" in ocsv and "quality" in ocsv
      and hashlib.sha256(rdb.read_bytes()).hexdigest() == h0)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
