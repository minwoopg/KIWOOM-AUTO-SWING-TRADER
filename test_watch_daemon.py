"""W2 조회 전용 상시 실행 관리자 테스트 (가짜 시계·가짜 키움, 별도 프로세스 포함).

    python test_watch_daemon.py

- 산출물은 임시 폴더에만. 실제 API·계좌·주문 없음. 가짜 키움은 ka10099·ka10081·ka20006·ka10001·ka10003·ka10004·토큰만.
"""
from __future__ import annotations

import ast
import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from domain.research.series import ResearchBar  # noqa: E402,F401
from domain.watchlist.config import dump_document, empty_document  # noqa: E402
from infra.research.collector import ResearchCollector  # noqa: E402
from infra.research.kiwoom_readonly import ReadOnlyResearchClient, ResearchApiError  # noqa: E402
from infra.research.kiwoom_rows import RawBar  # noqa: E402
from infra.research.scan_store import ScanStore  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from infra.research import open_check as A5  # noqa: E402
from infra.watch import daemon as D  # noqa: E402
from infra.watch.manager import load_state  # noqa: E402
from infra.watch.store import WatchStore  # noqa: E402
from tools import watch_daemon as WD  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")


def _fs(p: str):
    q = ROOT / p
    return sorted(str(x.relative_to(ROOT)) for x in q.rglob("*")) if q.exists() else None


FS_BEFORE = {p: _fs(p) for p in ("data", "commands", "reports", "logs")}
CAL = TradingCalendar.load()
TMP = Path(tempfile.mkdtemp(prefix="watch_daemon_"))
SESS = CAL.trading_days_in_range(date(2026, 1, 1), date(2026, 10, 31))


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, sec: float) -> None:
        self.t += timedelta(seconds=sec)

    def mono(self) -> float:
        return self.t.timestamp()


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def stock_bar(d, c, up=0.01, dn=0.01, vol=100_000, tv=5_000):
    c = int(round(c))
    return RawBar(d, c, int(c * (1 + up)), int(c * (1 - dn)), c, vol, tv, "" if vol else "NO_TRADES")


def pass_series(signal_day: date, base=10_000):
    """signal_day에 S1 PASS가 나는 모양(A5 시험과 같은 생성기) + 그 뒤 날은 완만한 움직임."""
    days = [d for d in SESS if d <= signal_day]
    pullback, recover, drift = (0.99, 0.98, 0.97, 0.965), 1.03, 1.003
    n = len(days) - len(pullback) - 2
    closes = [base * drift ** i for i in range(n)]
    out = [stock_bar(days[i], c) for i, c in enumerate(closes)]
    cp = closes[-1] * 1.03
    out.append(stock_bar(days[n], cp))
    for k, f in enumerate(pullback):
        out.append(stock_bar(days[n + 1 + k], cp * f, up=0.005, dn=0.005))
    out.append(stock_bar(days[-1], out[-1].close_raw * recover))
    last = out[-1].close_raw
    for i, d in enumerate([d for d in SESS if d > signal_day]):
        out.append(stock_bar(d, last * (0.999 ** (i + 1))))
    return {b.date: b for b in out}


def flat_series(base=20_000):
    return {d: stock_bar(d, base * 0.997 ** i) for i, d in enumerate(SESS)}


def index_series(base=2500.0):
    return {d: RawBar(d, int(base * 1.001 ** i * 100), int(base * 1.001 ** i * 100.5),
                      int(base * 1.001 ** i * 99.5), int(base * 1.001 ** i * 100), 500_000, 9_000_000, "")
            for i, d in enumerate(SESS)}


def lrow(code, name, mc="0", *, audit="정상"):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": audit, "regDay": "20000101",
            "lastPrice": "00010000", "state": "증거금40%|담보대출|신용가능", "marketCode": mc,
            "marketName": "거래소", "upName": "", "upSizeName": "", "companyClassName": "", "orderWarning": "0",
            "nxtEnable": "N", "kind": "A"}


class FakeKiwoom:
    """가짜 키움(세션 객체). 요청마다 시계를 tick초 진행. 토큰 만료·401·429·실패 주입."""
    PAGE = 600

    def __init__(self, clock: Clock):
        self.clock = clock
        self.series = {"001": index_series(), "101": index_series(1000.0)}
        self.list_rows = {"0": [], "10": []}
        self.calls: list[tuple[str, str, datetime]] = []
        self.tokens = 0
        self.expires_in: timedelta | None = None
        self.fail: dict[str, list[str]] = {}         # code → ["429", "401", "RC", ...]
        self.tick = timedelta(0)
        self.bodies: list = []

    def post(self, url, headers=None, json=None, timeout=None):
        assert url.startswith("https://mockapi.kiwoom.com"), url
        if url.endswith("/oauth2/token"):
            self.tokens += 1
            body = {"token": f"SECRET-TOKEN-{self.tokens}", "return_code": 0}
            if self.expires_in is not None:
                body["expires_dt"] = (self.clock() + self.expires_in).strftime("%Y%m%d%H%M%S")
            return Resp(200, body)
        api = headers["api-id"]
        code = json.get("stk_cd") or json.get("inds_cd") or f"LIST{json.get('mrkt_tp')}"
        self.calls.append((api, code, self.clock()))
        self.clock.t += self.tick
        acts = self.fail.get(code)
        act = acts.pop(0) if acts else None
        if act == "429":
            return Resp(429, {"return_code": 5})
        if act == "401":
            return Resp(401, {"return_code": 3, "return_msg": "token"})
        if act == "RC":
            return Resp(200, {"return_code": 1, "return_msg": "업무 오류"})
        if api == "ka10099":
            return Resp(200, {"list": self.list_rows[json["mrkt_tp"]], "return_code": 0},
                        {"cont-yn": "N", "next-key": ""})
        if api in ("ka10001", "ka10003", "ka10004"):
            ser = self.series[code]
            last = max(d for d in ser if d < self.clock().date())
            c = ser[last].close_raw
            t = self.clock()
            if api == "ka10001":
                return Resp(200, {"return_code": 0, "stk_cd": code, "cur_prc": f"+{c}", "base_pric": str(c),
                                  "open_pric": f"+{c}", "high_pric": f"+{c}", "low_pric": f"-{c}",
                                  "upl_pric": f"+{int(c * 1.3)}", "lst_pric": f"-{int(c * 0.7)}", "trde_qty": "1000"})
            if api == "ka10003":
                return Resp(200, {"return_code": 0, "cntr_infr": [{"tm": (t - timedelta(seconds=1)).strftime("%H%M%S"),
                                                                    "cur_prc": f"+{c}", "stex_tp": "KRX"}]})
            return Resp(200, {"return_code": 0, "bid_req_base_tm": t.strftime("%H%M%S"), "sel_fpr_bid": f"+{c + 50}",
                              "buy_fpr_bid": f"-{c}"})
        b = json["base_dt"]
        base_dt = date(int(b[:4]), int(b[4:6]), int(b[6:]))
        now = self.clock()
        ser = self.series[code]
        index = code in ("001", "101")
        rows = []
        for d in sorted(ser, reverse=True):
            if d > base_dt or d > now.date():
                continue
            r = ser[d]
            rows.append({"dt": d.strftime("%Y%m%d"), "open_pric": f"+{r.open_raw}", "high_pric": f"+{r.high_raw}",
                         "low_pric": f"-{r.low_raw}", "cur_prc": f"-{r.close_raw}", "trde_qty": str(r.volume),
                         "trde_prica": str(r.trade_value_raw)})
        off = int(headers["next-key"].split(":")[1]) if headers["cont-yn"] == "Y" else 0
        page, more = rows[off:off + self.PAGE], off + self.PAGE < len(rows)
        key = "inds_dt_pole_qry" if index else "stk_dt_pole_chart_qry"
        return Resp(200, {key: page, "return_code": 0},
                    {"cont-yn": "Y" if more else "N", "next-key": f"{code}:{off + self.PAGE}" if more else ""})


PASS_DAY = date(2026, 10, 7)


def env(name: str, start: datetime, symbols: list[dict], *, extra_list=()) -> dict:
    d = TMP / name
    d.mkdir()
    clock = Clock(start)
    fake = FakeKiwoom(clock)
    fake.series.update({"005930": pass_series(PASS_DAY), "000660": flat_series(), "035420": flat_series(30_000)})
    for c in extra_list:
        fake.series[c] = flat_series(15_000)
    fake.list_rows["0"] = [lrow("005930", "삼성전자"), lrow("000660", "SK하이닉스"), lrow("035420", "NAVER"),
                           lrow("069500", "KODEX 200", "8")] + [lrow(c, f"종목{c}") for c in extra_list]
    fake.list_rows["10"] = [lrow("247540", "에코프로비엠", "10")]
    client = ReadOnlyResearchClient(fake, "https://mockapi.kiwoom.com", "APPKEY-XYZ", "SECRET-XYZ", now=clock,
                                    monotonic=clock.mono, sleep=clock.sleep)
    paths = D.DaemonPaths(d / "watchlist.yaml", d / "watch.sqlite3", d / "research.sqlite3", d / "daemon.sqlite3",
                          d / "watch_s1.sqlite3", d / "watch_open.sqlite3", d / "reports", d / "daemon.log")
    # 운영 시작 전 상태: 목록 스냅숏(전날)만 있음
    clock.t, t0 = datetime(2026, 10, 6, 19, 0), clock.t
    with ResearchStore(paths.research_db) as rs:
        ResearchCollector(client, rs, CAL).snapshot_universe()
    clock.t = t0
    fake.calls.clear()
    doc = empty_document()
    doc["symbols"] = symbols
    paths.config.write_text(dump_document(doc), encoding="utf-8")
    return {"dir": d, "clock": clock, "fake": fake, "client": client, "paths": paths}


S_5930 = {"code": "005930", "interest": {"enabled": True, "s1_analysis": True}}
S_0660 = {"code": "000660", "holding": {"quantity": 5, "avg_price": 20000, "stop_price": 18000}}


def daemon(e, **kw) -> D.WatchDaemon:
    logs = e.setdefault("logs", [])
    s = D.DaemonSettings(**{"poll_sec": 60.0, **kw})
    d = D.WatchDaemon(e["paths"], CAL, e["client"], settings=s, now=e["clock"], sleep=e["clock"].sleep,
                      log=logs.append, hook=lambda p: None)
    d.start()
    return d


def at(e, t: datetime) -> None:
    e["clock"].t = t


def tasks(e) -> dict:
    with D.DaemonStore(e["paths"].daemon_db) as ds:
        return {(t["kind"], t["trading_day"]): t for t in ds.tasks()}


def api_codes(e, since: int = 0) -> list:
    return [(a, c) for a, c, _ in e["fake"].calls[since:]]


# ── 1. 일정 ────────────────────────────────────────────────
E0 = env("sched", datetime(2026, 10, 7, 12, 0), [S_5930])
d0 = daemon(E0)
dt_norm, dt_special, dt_hol = d0.due_times(date(2026, 10, 7)), d0.due_times(date(2026, 1, 2)), d0.due_times(date(2026, 10, 9))
check("1-1) 실행 시각은 달력의 실제 세션: 일반일 OPEN_CHECK 09:05·CLOSE_PREP 18:10(정규장 종료 + 160분 잠정), 특수 개장일(1/2 10:00 개장) "
      "10:05, 휴장일(10/9) 작업 없음. CLOSE_PREP 기한 = 다음 거래일 완성 시각(10/7 → 10/8 18:10)",
      dt_norm[D.OPEN_CHECK][0] == datetime(2026, 10, 7, 9, 5) and dt_norm[D.CLOSE_PREP] ==
      (datetime(2026, 10, 7, 18, 10), datetime(2026, 10, 8, 18, 10))
      and dt_special[D.OPEN_CHECK][0] == datetime(2026, 1, 2, 10, 5) and dt_hol == {})
at(E0, datetime(2026, 10, 7, 12, 0))
r12 = d0.tick()
check("1-2) 운영 시작일(since) 이전 작업은 만들지 않고, 시작일 09:05가 지났으면 그날 OPEN_CHECK는 실행(후보 없음 — 관찰 기록이 없어 "
      "NO_SCAN, 가격 조회 0회). 다음 예정 = 그날 18:10",
      r12["ran"] == "OPEN_CHECK|2026-10-07|OPEN+5m" and r12["status"] == "COMPLETE"
      and tasks(E0)[("OPEN_CHECK", "2026-10-07")]["detail"]["candidate_status"] == "NO_SCAN"
      and not [c for c in api_codes(E0) if c[0] == "ka10001"] and d0.tick()["next_due"] == datetime(2026, 10, 7, 18, 10))
d0.stop()

# ── 2. 정상 다일 흐름 ───────────────────────────────────────
E1 = env("multi", datetime(2026, 10, 7, 18, 0), [S_5930, S_0660])
d1 = daemon(E1)
at(E1, datetime(2026, 10, 7, 17, 0))
d1.dstore.set_meta("since", "2026-10-07")
d1.dstore.conn.execute("DELETE FROM task")
r_early = d1.tick()
at(E1, datetime(2026, 10, 7, 19, 0))
n0 = len(E1["fake"].calls)
r_prep = d1.tick()
prep = tasks(E1)[("CLOSE_PREP", "2026-10-07")]
codes_prep = sorted({c for a, c in api_codes(E1, n0)})
check("2-1) 10/7 19:00 CLOSE_PREP: 마감 뒤 목록(ka10099 2회) → 지정 종목·지수만 일봉 준비(전체 시장 수집 없음) → 지정 종목 S1 관찰 "
      "(005930 PASS·actionable) → 보고서. 17:00에는 실행 안 함",
      r_early["ran"] is None or r_early["ran"].startswith("OPEN_CHECK")
      and r_prep["status"] == "COMPLETE" and prep["status"] == "COMPLETE"
      and codes_prep == ["000660", "001", "005930", "101", "LIST0", "LIST10"]
      and prep["detail"]["s1"]["signal_date"] == "2026-10-07" and prep["detail"]["s1"]["counts"]["signals"] == 1
      and prep["detail"]["s1"]["codes"] == ["005930"] and prep["detail"]["s1"]["actionable"] is True
      and prep["config_version"] == 1 and prep["contract_hash"] and prep["report_path"]
      and Path(prep["report_path"]).exists())
with ScanStore(E1["paths"].watch_scan_db) as ss:
    run1 = ss.runs("2026-10-07")[0]
    ctx1 = ss.load_run(run1["run_id"])["context"]
check("2-2) 지정 종목 S1 관찰은 별도 DB·별도 계약(선정 방식 watchlist·대상 코드 포함 — 전체 시장 연구 계약과 해시가 다름), "
      "설정 버전·선정 규칙을 context에 기록, 저장 완료 시각(committed_at) 있음",
      ctx1["contract"]["selection"]["kind"] == "watchlist" and ctx1["contract"]["selection"]["codes"] == ["005930"]
      and ctx1["selection_context"]["watch_config_version"] == 1 and run1["committed_at"]
      and run1["contract_hash"] != __import__("infra.research.s1_scanner", fromlist=["x"]).build_contract(CAL)[1])
n1 = len(E1["fake"].calls)
check("2-3) 끝난 작업은 다시 실행하지 않음 — 같은 날 다시 순회해도 조회 0회",
      d1.tick()["ran"] is None and len(E1["fake"].calls) == n1)
at(E1, datetime(2026, 10, 7, 23, 59, 30))
r_mid1 = d1.tick()
at(E1, datetime(2026, 10, 8, 0, 0, 30))
r_mid2 = d1.tick()
check("2-4) 자정 전환: 23:59·00:00 순회 모두 할 일 없음, 다음 예정은 10/8 09:05",
      r_mid1["ran"] is None and r_mid2["ran"] is None and r_mid2["next_due"] == datetime(2026, 10, 8, 9, 5))
at(E1, datetime(2026, 10, 8, 9, 6))
n2 = len(E1["fake"].calls)
r_open = d1.tick()
oc = tasks(E1)[("OPEN_CHECK", "2026-10-08")]
with A5.OpenCheckStore(E1["paths"].watch_open_db) as os_:
    cset = os_.get_set(date(2026, 10, 8))
    chk = os_.checks(cset["set_id"])
check("2-5) 10/8 09:06 OPEN_CHECK: 전날 개장 전 저장된 지정 종목 관찰의 PASS 후보(005930)만 가격 기록(ka10001·10003·10004), "
      "계약은 그 관찰의 계약, 관찰 가격(체결 아님)",
      r_open["status"] == "COMPLETE" and oc["detail"]["candidate_status"] == "OK" and cset["count"] == 1
      and [c["symbol"] for c in chk] == ["005930"] and chk[0]["fetch_status"] == "OK"
      and oc["contract_hash"] == prep["contract_hash"]
      and sorted(api_codes(E1, n2)) == [("ka10001", "005930"), ("ka10003", "005930"), ("ka10004", "005930")]
      and "FILLED" not in Path(oc["report_path"]).read_text(encoding="utf-8")
      and "FILLED" not in Path(oc["detail"]["report"]).read_text(encoding="utf-8"))
at(E1, datetime(2026, 10, 8, 19, 0))
r_prep2 = d1.tick()
at(E1, datetime(2026, 10, 9, 12, 0))
r_hol = d1.tick()
at(E1, datetime(2026, 10, 12, 9, 6))
r_open3 = d1.tick()
oc3 = tasks(E1)[("OPEN_CHECK", "2026-10-12")]
check("2-6) 10/8 마감 준비 → 10/9 휴장 할 일 없음 → 10/12(월) 개장 확인은 직전 거래일 10/8의 관찰 사용",
      r_prep2["status"] == "COMPLETE" and r_hol["ran"] is None and r_open3["status"] == "COMPLETE"
      and oc3["detail"]["signal_date"] == "2026-10-08")
d1.stop()
d1b = daemon(E1)
n3 = len(E1["fake"].calls)
check("2-7) 재시작(새 프로세스처럼 새 관리자): 끝난 작업은 그대로, 조회 0회",
      d1b.tick()["ran"] is None and len(E1["fake"].calls) == n3)
d1b.stop()

# ── 3. 놓친 개장 확인·늦은 관찰 ─────────────────────────────
E2 = env("missed", datetime(2026, 10, 7, 19, 0), [S_5930])
d2 = daemon(E2)
d2.dstore.set_meta("since", "2026-10-07")
d2.dstore.conn.execute("DELETE FROM task")
d2.tick()                                                   # 10/7 OPEN_CHECK(이미 마감) 처리
d2.tick()                                                   # 10/7 CLOSE_PREP
at(E2, datetime(2026, 10, 8, 16, 0))                         # 관리자가 장중 내내 꺼져 있었음
n = len(E2["fake"].calls)
r_m = d2.tick()
with A5.OpenCheckStore(E2["paths"].watch_open_db) as os_:
    cs = os_.get_set(date(2026, 10, 8))
    mrows = os_.checks(cs["set_id"])
check("3-1) 개장 확인을 놓침(10/8 16:00 기동) → 당시 가격으로 채우지 않고 MISSED로 기록, 가격 조회 0회",
      r_m["status"] == "COMPLETE" and [r["outcome"] for r in mrows] == ["MISSED"]
      and not [c for c in api_codes(E2, n) if c[0].startswith("ka1000")])
E3 = env("late", datetime(2026, 10, 8, 9, 30), [S_5930])
d3 = daemon(E3)
d3.dstore.set_meta("since", "2026-10-07")
order3 = [d3.tick()["ran"] for _ in range(4)]
t3 = tasks(E3)
check("3-2) 10/7 마감 준비를 놓치고 10/8 09:30 기동: 우선순위대로 개장 확인(10/7·10/8) 먼저 — 개장 전 저장된 관찰이 없어 NO_SCAN. "
      "그 뒤 늦은 CLOSE_PREP(10/7)의 관찰은 개장 뒤 계산이라 actionable=False(그날 후보가 되지 않음)",
      [o.split("|")[0] + "|" + o.split("|")[1] for o in order3[:3]] ==
      ["OPEN_CHECK|2026-10-07", "OPEN_CHECK|2026-10-08", "CLOSE_PREP|2026-10-07"] and order3[3] is None
      and t3[("OPEN_CHECK", "2026-10-08")]["detail"]["candidate_status"] == "NO_SCAN"
      and t3[("CLOSE_PREP", "2026-10-07")]["status"] == "COMPLETE"
      and t3[("CLOSE_PREP", "2026-10-07")]["detail"]["s1"]["actionable"] is False)
at(E3, datetime(2026, 10, 12, 19, 0))
r_l3 = d3.tick()
check("3-3) CLOSE_PREP 기한(다음 거래일 완성)을 넘긴 날(10/8)은 MISSED — 지난 신호를 그 뒤에 만들지 않음, 최근 완성일(10/12)만 실행",
      tasks(E3)[("CLOSE_PREP", "2026-10-08")]["status"] == "MISSED" and r_l3["ran"] is not None)
d2.stop()
d3.stop()

# 개장 뒤(09:01)에 종목을 더해 같은 신호일을 새 계약으로 다시 관찰 → 개장 확인은 개장 전 저장된 관찰의 계약을 써야 함
E3b = env("postopen", datetime(2026, 10, 7, 19, 0), [S_5930])
d3b = daemon(E3b)
d3b.dstore.set_meta("since", "2026-10-07")
d3b.dstore.conn.execute("DELETE FROM task")
d3b.tick()
d3b.tick()
pre_contract = tasks(E3b)[("CLOSE_PREP", "2026-10-07")]["contract_hash"]
doc3b = empty_document()
doc3b["symbols"] = [S_5930, {"code": "035420", "interest": {"enabled": True, "s1_analysis": True}}]
E3b["paths"].config.write_text(dump_document(doc3b), encoding="utf-8")
at(E3b, datetime(2026, 10, 8, 9, 1))
r_po1 = d3b.tick()                                           # 새 범위의 CLOSE_PREP(10/7) — 개장 뒤 저장
at(E3b, datetime(2026, 10, 8, 9, 6))
r_po2 = d3b.tick()
with D.DaemonStore(E3b["paths"].daemon_db) as ds3b:
    post = [t for t in ds3b.tasks() if t["kind"] == "CLOSE_PREP" and t["contract_hash"] != pre_contract]
oc3b = tasks(E3b)[("OPEN_CHECK", "2026-10-08")]
check("3-4) 개장 뒤(09:01)에 같은 신호일을 새 대상·새 계약으로 다시 관찰해도, 개장 확인은 개장 전 저장 완료된 관찰의 계약·후보를 씀"
      "(개장 뒤 관찰은 후보 원천 아님)",
      r_po1["ran"].startswith("CLOSE_PREP|2026-10-07") and len(post) == 1 and r_po2["status"] == "COMPLETE"
      and oc3b["contract_hash"] == pre_contract and oc3b["detail"]["candidate_status"] == "OK"
      and oc3b["detail"]["counts"]["candidates"] == 1)
d3b.stop()

# ── 4. 양보·예산·중지 ─────────────────────────────────────
E4 = env("yield", datetime(2026, 10, 8, 9, 4, 50), [S_5930, S_0660, {"code": "035420", "interest": {"enabled": True}}]
         + [{"code": c, "interest": {"enabled": True}} for c in ("100010", "100020", "100030", "100040")],
         extra_list=("100010", "100020", "100030", "100040"))
d4 = daemon(E4)
d4.dstore.set_meta("since", "2026-10-08")
d4.dstore.set_meta("since", "2026-10-07")
d4.dstore.conn.execute("DELETE FROM task")
E4["fake"].tick = timedelta(seconds=1)
order4 = []
y1 = None
for _ in range(5):
    r = d4.tick()
    order4.append((r["ran"] or "-").split("|scope")[0].split("|OPEN")[0] + ":" + str(r.get("status")))
    if r.get("status") == "YIELDED" and y1 is None:
        y1 = tasks(E4)[("CLOSE_PREP", "2026-10-07")]
check("4-1) 긴 준비가 진행 중 개장 + 5분이 되면 대상 사이에서 양보(YIELDED, 실패로 세지 않음) → 다음 순회에 OPEN_CHECK(10/8) 먼저 → "
      "준비를 이어서 완료",
      order4[:4] == ["OPEN_CHECK|2026-10-07:COMPLETE", "CLOSE_PREP|2026-10-07:YIELDED", "OPEN_CHECK|2026-10-08:COMPLETE",
                     "CLOSE_PREP|2026-10-07:COMPLETE"]
      and y1["detail"]["yield"] == "PRIORITY:OPEN_CHECK" and y1["failures"] == 0)
d4.stop()
E5 = env("budget", datetime(2026, 10, 7, 19, 0), [S_5930, S_0660])
d5 = daemon(E5, daily_call_cap=4)
d5.dstore.set_meta("since", "2026-10-07")
d5.dstore.conn.execute("DELETE FROM task")
d5.tick()
r_b = d5.tick()
b5 = tasks(E5)[("CLOSE_PREP", "2026-10-07")]
with D.DaemonStore(E5["paths"].daemon_db) as ds:
    used5 = ds.calls(date(2026, 10, 7))
check("4-2) 하루 호출 예산(4회)에 닿으면 남은 대상은 다음으로 미루고 YIELDED(CALL_BUDGET) — 모든 작업의 호출을 합산",
      r_b["status"] == "YIELDED" and b5["detail"]["yield"].startswith("CALL_BUDGET") and 4 <= used5 <= 5)
d5.stop()
E6 = env("stop", datetime(2026, 10, 7, 19, 0), [S_5930, S_0660])
d6 = D.WatchDaemon(E6["paths"], CAL, E6["client"], settings=D.DaemonSettings(), now=E6["clock"], sleep=E6["clock"].sleep,
                   log=lambda m: None, hook=lambda p: (d6.dstore.request_stop() if p == "after_list" else None))
d6.start()
d6.dstore.set_meta("since", "2026-10-07")
d6.dstore.conn.execute("DELETE FROM task")
state6 = d6.run_forever(max_ticks=5)
t6 = tasks(E6)
check("4-3) 중지 요청(stop) → 진행 중 준비는 대상 사이에서 YIELDED(STOP_REQUESTED)로 멈추고 루프 종료(STOPPED)",
      state6 == "STOPPED" and t6[("CLOSE_PREP", "2026-10-07")]["status"] == "YIELDED"
      and t6[("CLOSE_PREP", "2026-10-07")]["detail"]["yield"] == "STOP_REQUESTED")
d6.stop()

# ── 5. 설정 변경·오류 ─────────────────────────────────────
E7 = env("config", datetime(2026, 10, 7, 19, 0), [S_5930])
d7 = daemon(E7)
d7.dstore.set_meta("since", "2026-10-07")
d7.dstore.conn.execute("DELETE FROM task")
d7.tick()
d7.tick()
doc7 = empty_document()
doc7["symbols"] = [S_5930, {"code": "035420", "interest": {"enabled": True, "s1_analysis": True}}]
E7["paths"].config.write_text(dump_document(doc7), encoding="utf-8")
at(E7, datetime(2026, 10, 7, 19, 30))
n7 = len(E7["fake"].calls)
r_c = d7.tick()
with D.DaemonStore(E7["paths"].daemon_db) as ds7:
    t7 = [t for t in ds7.tasks() if t["kind"] == "CLOSE_PREP"]
calls7 = api_codes(E7, n7)
check("5-1) 실행 중 종목 추가 → 대상 범위가 바뀌어 같은 날 CLOSE_PREP를 새 범위로 한 번 더(새 종목은 전체 이력, 기존 종목은 1페이지), "
      "S1 관찰 계약도 새 대상 코드로 별도",
      r_c["status"] == "COMPLETE" and len(t7) == 2 and len({t["contract_hash"] for t in t7}) == 2
      and calls7.count(("ka10081", "005930")) == 1 and ("ka10081", "035420") in calls7
      and max(t["config_version"] for t in t7) == 2)
E7["paths"].config.write_text("schema: w9\n", encoding="utf-8")
at(E7, datetime(2026, 10, 8, 19, 0))
d7.tick()
r_c2 = d7.tick()
t7b = tasks(E7)
check("5-2) 운영 중 설정 오류 → 관리자는 계속(마지막 정상 v2로 준비), 작업 기록에 신규 진입 차단 표시",
      r_c2["status"] == "COMPLETE" and t7b[("CLOSE_PREP", "2026-10-08")]["config_version"] == 2
      and t7b[("CLOSE_PREP", "2026-10-08")]["detail"]["entry_blocked"] is True)
d7.stop()
E8 = env("noconfig", datetime(2026, 10, 7, 19, 0), [])
E8["paths"].config.write_text("schema: w1\nsymbols:\n  - code: 5930\n", encoding="utf-8")
d8 = daemon(E8)
d8.dstore.set_meta("since", "2026-10-07")
d8.dstore.conn.execute("DELETE FROM task")
r_n = d8.tick()
t8 = tasks(E8)[("OPEN_CHECK", "2026-10-07")]
check("5-3) 정상 설정이 한 번도 없으면 작업을 시작하지 않고 FAILED(NO_VALID_CONFIG) — 조회 0회, 나중에 다시 시도",
      r_n["status"] == "FAILED" and "NO_VALID_CONFIG" in t8["error"] and t8["next_retry_at"] is not None
      and E8["fake"].calls == [])
d8.stop()

# ── 6. 인증·429·DB 실패·보고서 실패 ──────────────────────────
E9 = env("token", datetime(2026, 10, 7, 19, 0), [S_5930, S_0660])
E9["fake"].expires_in = timedelta(minutes=12)
E9["client"]._token = ""                                    # 다음 요청에서 만료 시각이 있는 토큰을 새로 받음
E9["fake"].tick = timedelta(minutes=1)
d9 = daemon(E9)
d9.dstore.set_meta("since", "2026-10-07")
d9.dstore.conn.execute("DELETE FROM task")
d9.tick()
r_t = d9.tick()
check("6-1) 토큰 응답 expires_dt(만료 12분)를 읽어 만료 10분 전부터 요청 전에 새로 발급 — 작업은 끊기지 않고 완료",
      r_t["status"] == "COMPLETE" and E9["fake"].tokens >= 3 and E9["client"].token_expires_at is not None)
E10 = env("auth", datetime(2026, 10, 7, 19, 0), [S_5930, S_0660])
E10["fake"].fail["000660"] = ["401"]
E10["fake"].fail["005930"] = ["429"]
d10 = daemon(E10)
d10.dstore.set_meta("since", "2026-10-07")
d10.dstore.conn.execute("DELETE FROM task")
d10.tick()
r_a = d10.tick()
check("6-2) HTTP 401은 재인증 후 재시도, 429는 대기 후 재시도 — 둘 다 작업 완료(조회 수에 재시도 포함)",
      r_a["status"] == "COMPLETE" and E10["fake"].tokens == 2 and E10["client"].retries >= 1)
E10["fake"].fail["000660"] = ["401"] * 10
cli10 = ReadOnlyResearchClient(E10["fake"], "https://mockapi.kiwoom.com", "K", "S", now=E10["clock"],
                               monotonic=E10["clock"].mono, sleep=E10["clock"].sleep, max_reauth=3)
msgs10 = []
tok0 = E10["fake"].tokens
for _ in range(6):
    try:
        cli10.fetch_page("ka10081", {"stk_cd": "000660", "base_dt": "20261007", "upd_stkpc_tp": "1"},
                         "stk_dt_pole_chart_qry")
    except ResearchApiError as exc:
        msgs10.append(str(exc))
capped = any("재인증이" in m for m in msgs10) and E10["fake"].tokens - tok0 <= 4
other_err = False
E10["fake"].fail["000660"] = ["RC"]
try:
    cli10.fetch_page("ka10081", {"stk_cd": "000660", "base_dt": "20261007", "upd_stkpc_tp": "1"},
                     "stk_dt_pole_chart_qry")
except ResearchApiError as exc:
    other_err = "return_code=1" in str(exc)
check("6-3) 401이 계속되면 창(10분) 안 재인증 3번을 넘기지 않고 오류로 끝냄(무한 재발급 없음). return_code≠0 같은 다른 오류는 인증 "
      "오류로 보지 않음(재발급 없이 그대로 오류)",
      capped and other_err)
d10.stop()
E11 = env("dbfail", datetime(2026, 10, 7, 19, 0), [S_5930])
d11 = daemon(E11)
d11.dstore.set_meta("since", "2026-10-07")
d11.dstore.conn.execute("DELETE FROM task")
d11.tick()
real_prep = D.prepare_data


def locked_prep(*a, **k):
    raise sqlite3.OperationalError("database is locked")


D.prepare_data = locked_prep
try:
    r_db = d11.tick()
finally:
    D.prepare_data = real_prep
t11 = tasks(E11)[("CLOSE_PREP", "2026-10-07")]
r_db_wait = d11.tick()
at(E11, datetime(2026, 10, 7, 19, 6))
r_db2 = d11.tick()
check("6-4) DB 잠김(OperationalError) → 그 작업만 FAILED·사유 기록·5분 뒤 다시 시도(관리자는 계속), 5분 전엔 다시 하지 않음, "
      "다시 시도에서 완료",
      r_db["status"] == "FAILED" and "database is locked" in t11["error"] and t11["failures"] == 1
      and t11["next_retry_at"].startswith("2026-10-07T19:05") and r_db_wait["ran"] is None and r_db2["status"] == "COMPLETE")
d11.stop()
E12 = env("report", datetime(2026, 10, 7, 19, 0), [S_5930])
E12["paths"].report_dir.parent.mkdir(parents=True, exist_ok=True)
E12["paths"].report_dir.write_text("not a dir", encoding="utf-8")       # 보고서 폴더 자리에 파일 → 쓰기 실패
d12 = daemon(E12)
d12.dstore.set_meta("since", "2026-10-07")
d12.dstore.conn.execute("DELETE FROM task")
d12.tick()
r_r = d12.tick()
t12 = tasks(E12)[("CLOSE_PREP", "2026-10-07")]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    WD.cmd_status(WD.build_parser().parse_args(["--daemon-db", str(E12["paths"].daemon_db), "--watch-db",
                                                 str(E12["paths"].watch_db), "status"]),
                  now=E12["clock"], calendar=CAL)
st12 = buf.getvalue()
E12["paths"].report_dir.unlink()
code_rep = WD.main(["--daemon-db", str(E12["paths"].daemon_db), "--report-dir", str(E12["paths"].report_dir),
                    "report", "--day", "2026-10-07"], now=E12["clock"], calendar=CAL)
t12b = tasks(E12)[("CLOSE_PREP", "2026-10-07")]
check("6-5) 보고서 실패: 작업 결과는 저장(COMPLETE)·보고서 실패 표시(status에도), 다시 실행하지 않고 `report --day`로 보고서만 다시 만듦",
      r_r["status"] == "COMPLETE" and t12["report_error"] and "보고서 실패" in st12 and code_rep == 0
      and t12b["report_error"] is None and Path(t12b["report_path"]).exists())
d12.stop()

# 실제 다른 프로세스가 연구 DB 쓰기 잠금을 잠시 잡고 있음 → 기다렸다가 완료(실패로 처리하지 않음)
E16 = env("realock", datetime(2026, 10, 7, 19, 0), [S_5930])
d16 = daemon(E16)
d16.dstore.set_meta("since", "2026-10-07")
d16.dstore.conn.execute("DELETE FROM task")
d16.tick()
holder = subprocess.Popen([sys.executable, "-c",
                           "import sqlite3,sys,time;c=sqlite3.connect(sys.argv[1],isolation_level=None);"
                           "c.execute('BEGIN IMMEDIATE');print('locked',flush=True);time.sleep(2);c.execute('COMMIT')",
                           str(E16["paths"].research_db)], stdout=subprocess.PIPE, text=True)
holder.stdout.readline()
t_lock = time.monotonic()
r_lock = d16.tick()
waited16 = time.monotonic() - t_lock
holder.wait(timeout=30)
check("6-6) 별도 프로세스가 연구 DB 쓰기 잠금을 2초 잡고 있어도 관리자는 기다렸다가(SQLite busy 대기) 완료 — 실패로 바꾸지 않음",
      r_lock["status"] == "COMPLETE" and waited16 >= 1.0)
d16.stop()

# ── 7. 중단·재시작·중복 기동(실제 별도 프로세스) ───────────────
SUBENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def proc(e, *args, extra=None, wait=True):
    p = e["paths"]
    cmd = [sys.executable, str(ROOT / "tools" / "watch_daemon.py"), "--config", str(p.config), "--watch-db",
           str(p.watch_db), "--db", str(p.research_db), "--daemon-db", str(p.daemon_db), "--watch-scan-db",
           str(p.watch_scan_db), "--watch-open-db", str(p.watch_open_db), "--report-dir", str(p.report_dir),
           "--log-file", str(p.log_file), "--env-file", str(e["dir"] / "no.env"), *args]
    envv = {**SUBENV, **(extra or {})}
    if not wait:
        return subprocess.Popen(cmd, env=envv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8")
    r = subprocess.run(cmd, env=envv, capture_output=True, text=True, encoding="utf-8", timeout=120)
    return r.returncode, r.stdout + r.stderr


E13 = env("kill", datetime(2026, 10, 7, 19, 0), [S_5930])
with D.DaemonStore(E13["paths"].daemon_db) as ds:
    ds.set_meta("since", "2026-10-07")
rc_k, out_k = proc(E13, "run", "--max-ticks", "3",
                   extra={"WATCH_TEST_CRASH_AT": "task_started", "WATCH_TEST_NOW": "2026-10-07T19:00:00"})
t13 = tasks(E13)
running_left = [k for k, t in t13.items() if t["status"] == "RUNNING"]
rc_s, out_s = proc(E13, "status", extra={"WATCH_TEST_NOW": "2026-10-07T19:00:05"})
d13 = daemon(E13)
t13b = tasks(E13)
d13.tick()
r_k = d13.tick()
t13c = tasks(E13)
check("7-1) 실제 프로세스를 작업 시작 직후 강제 종료 → RUNNING이 남고 status는 '비정상 종료' 안내. 다시 기동하면 ABORTED로 바꾸고 "
      "곧바로 다시 시도해 완료(조회 전 종료라 중복 기록 없음)",
      rc_k == 97 and len(running_left) == 1 and "비정상 종료" in out_s
      and t13b[running_left[0]]["status"] == "ABORTED" and r_k["status"] == "COMPLETE"
      and all(t["status"] == "COMPLETE" for t in t13c.values()))
d13.stop()
E14 = env("dup", datetime(2026, 10, 7, 19, 0), [S_5930])
pa = proc(E14, "run", "--max-ticks", "0", extra={"WATCH_TEST_SLEEP_AT": "daemon_started:4",
                                                  "WATCH_TEST_NOW": "2026-10-07T12:00:00"}, wait=False)
deadline = time.monotonic() + 30
while time.monotonic() < deadline and not E14["paths"].daemon_db.exists():
    time.sleep(0.1)
time.sleep(1.0)
rc_dup, out_dup = proc(E14, "run", "--max-ticks", "0", extra={"WATCH_TEST_NOW": "2026-10-07T12:00:00"})
rc_st, out_st = proc(E14, "status", extra={"WATCH_TEST_NOW": "2026-10-07T12:00:01"})
pa_out, _ = pa.communicate(timeout=60)
rc_after, _ = proc(E14, "run", "--max-ticks", "0", extra={"WATCH_TEST_NOW": "2026-10-07T12:00:10"})
check("7-2) 중복 기동: 실행 중인 관리자가 있으면 두 번째 run은 바로 종료 코드 2·안내, status는 '실행 중'. 첫 관리자가 끝나면 다시 기동 가능",
      rc_dup == 2 and "이미 실행 중" in out_dup and "실행 중" in out_st and pa.returncode == 0 and rc_after == 0)
rc_stop, out_stop = proc(E14, "stop")
check("7-3) 실행 중인 관리자가 없을 때 stop은 아무것도 하지 않음(종료 코드 0)", rc_stop == 0 and "없음" in out_stop)
E15 = env("intr", datetime(2026, 10, 7, 19, 0), [S_5930])
d15 = D.WatchDaemon(E15["paths"], CAL, E15["client"], settings=D.DaemonSettings(), now=E15["clock"],
                    sleep=E15["clock"].sleep, log=lambda m: None,
                    hook=lambda p: (_ for _ in ()).throw(KeyboardInterrupt) if p == "after_prepare" else None)
d15.start()
d15.dstore.set_meta("since", "2026-10-07")
d15.dstore.conn.execute("DELETE FROM task")
d15.tick()
state15 = d15.run_forever(max_ticks=3)
t15 = tasks(E15)[("CLOSE_PREP", "2026-10-07")]
check("7-4) 작업 중 Ctrl+C → 그 작업 ABORTED(다시 시도 대상)·관리자는 INTERRUPTED로 끝남", state15 == "INTERRUPTED"
      and t15["status"] == "ABORTED" and t15["next_retry_at"] is not None)
d15.stop("INTERRUPTED")

# ── 8. 상태·로그·경계 ─────────────────────────────────────
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    WD.cmd_status(WD.build_parser().parse_args(["--daemon-db", str(E1["paths"].daemon_db), "--watch-db",
                                                 str(E1["paths"].watch_db), "status"]),
                  now=lambda: datetime(2026, 10, 12, 10, 0), calendar=CAL)
st1 = buf.getvalue()
check("8-1) status: 관리자 상태·run_id·설정 버전·오늘 호출 수·다음 예정·작업별 상태/시도/다음 시도·오류",
      "관리자: 중지됨" in st1 and "run_id wd_" in st1 and "v1 사용 중" in st1 and "다음 예정 2026-10-12T18:10:00" in st1
      and "| CLOSE_PREP | 2026-10-08 | COMPLETE |" in st1)
logs = "\n".join(E1.get("logs", [])) + "\n" + "\n".join(E10.get("logs", []))
check("8-2) 로그에 토큰·앱키 값 없음(가림 함수: token=…·appkey: … → ***)",
      "SECRET-TOKEN" not in logs and "APPKEY-XYZ" not in logs
      and D.redact("token=abc appkey: XYZ authorization=Bearer") == "token=*** appkey: *** authorization=***")
FORBIDDEN = ("infra.broker", "infra.storage", "infra.notify", "domain.strategy", "domain.service", "commands",
             "domain.position", "infra.market_data")
bad = []
for f in (ROOT / "infra/watch/daemon.py", ROOT / "tools/watch_daemon.py"):
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else \
            [a.name for a in node.names] if isinstance(node, ast.Import) else []
        bad += [(f.name, m) for m in mods if m and m.startswith(FORBIDDEN)]
all_apis = {a for e in (E1, E2, E3, E4, E5, E7, E9, E10, E11, E12, E13) for a, _, _ in e["fake"].calls}
check("8-3) 관리자는 브로커·주문 실행부·원장·전략·알림을 import하지 않고, 호출한 TR은 조회 허용 목록뿐(주문·계좌 TR 0)",
      bad == [] and all_apis <= {"ka10099", "ka10081", "ka20006", "ka10001", "ka10003", "ka10004"})
SCHTASKS = ('"HostName","TaskName","Next Run Time","Status","Task To Run"\n'
            '"PC","\\\\swing_update","2026-10-07 18:30","Ready","python tools\\\\research_collect.py update"\n'
            '"PC","\\\\watch_prepare","2026-10-07 18:20","Ready","python tools\\\\watchlist.py prepare"\n'
            '"PC","\\\\other","N/A","Ready","notepad.exe"\n')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_doc = WD.cmd_doctor(WD.build_parser().parse_args(["--daemon-db", str(TMP / "none.sqlite3"), "doctor"]),
                           runner=lambda: SCHTASKS)
    rc_doc0 = WD.cmd_doctor(WD.build_parser().parse_args(["--daemon-db", str(TMP / "none.sqlite3"), "doctor"]),
                            runner=lambda: '"HostName","TaskName","Task To Run"\n"PC","x","notepad.exe"\n')
doc_out = buf.getvalue()
check("8-5) doctor: Windows 작업 스케줄러 목록에서 이 레포의 매일 실행(research_collect·watchlist prepare)을 찾아 안내하고 "
      "바꾸지 않음(읽기만), 겹칠 항목이 없으면 0",
      rc_doc == 1 and "swing_update" in doc_out and "watch_prepare" in doc_out and "notepad" not in doc_out
      and "중지 권장" in doc_out and "바꾸지 않습니다" in doc_out and rc_doc0 == 0)
check("8-4) 테스트 산출물은 임시 폴더에만 — 레포에 data/·commands/·reports/·logs/ 변화 없음",
      {p: _fs(p) for p in ("data", "commands", "reports", "logs")} == FS_BEFORE)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
