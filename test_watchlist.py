"""사용자 지정 종목 설정·검증·데이터 준비 테스트 (방향 전환 2단계 — 국내 지정 종목).

    python test_watchlist.py

- 모든 산출물은 임시 폴더. 레포에 data/·commands/·config/watchlist.yaml을 만들지 않음.
- 키움은 가짜 세션(ka10099·ka10081·ka20006). 주문·계좌 TR 없음.
"""
from __future__ import annotations

import ast
import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from domain.watchlist.config import (  # noqa: E402
    Listing, dump_document, empty_document, validate,
)
from infra.research.collector import ResearchCollector  # noqa: E402
from infra.research.kiwoom_readonly import ReadOnlyResearchClient  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from infra.watch import manager as M  # noqa: E402
from infra.watch.store import WatchStore  # noqa: E402
from tools import watchlist as W  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")


def _fs(p: str):
    q = ROOT / p
    return sorted(str(x.relative_to(ROOT)) for x in q.rglob("*")) if q.exists() else None


FS_BEFORE = {p: _fs(p) for p in ("data", "commands")}
CFG_EXISTED = (ROOT / "config" / "watchlist.yaml").exists()
CAL = TradingCalendar.load()
TMP = Path(tempfile.mkdtemp(prefix="watchlist_"))
NOW = datetime(2026, 10, 7, 19, 0)            # 10/7(수) 18:10 뒤 — 10/7 봉 완성


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def lrow(code, name, mc="0", *, audit="정상", state="증거금40%|담보대출|신용가능", ow="0", cls="", reg="20000101"):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": audit, "regDay": reg,
            "lastPrice": "00010000", "state": state, "marketCode": mc, "marketName": "거래소", "upName": "",
            "upSizeName": "", "companyClassName": cls, "orderWarning": ow, "nxtEnable": "N", "kind": "A"}


def is_session(d: date) -> bool:
    """달력이 다루는 해(2026)는 달력대로, 그 밖은 평일 — 수집 테스트와 같은 가짜 규칙."""
    if d.year in CAL.covered_years:
        return CAL.is_trading_day(d)
    return d.weekday() < 5


class FakeKiwoom:
    """ka10099(목록)·ka10081(종목 일봉)·ka20006(지수 일봉). 일봉은 start부터 거래일마다, 600행 페이지."""
    PAGE = 600

    def __init__(self, clock: Clock):
        self.clock = clock
        self.start: dict[str, date] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self.list_rows = {"0": [], "10": []}

    def _rows(self, code, base_dt: date):
        out, d, i = [], self.start[code], 0
        last = min(base_dt, self.clock().date())
        index = code in ("001", "101")
        while d <= last:
            if is_session(d):
                c = 10_000 + (i % 37) * 10 + i
                o, h, lo = c - 5, c + 20, c - 20
                if index:
                    o, h, lo, c = o * 100, h * 100, lo * 100, c * 100
                out.append({"dt": d.strftime("%Y%m%d"), "open_pric": f"+{o}", "high_pric": f"+{h}",
                            "low_pric": f"-{lo}", "cur_prc": f"-{c}", "trde_qty": str(100_000 + i),
                            "trde_prica": str(max(1, c * (100_000 + i) // 1_000_000))})
                i += 1
            d += timedelta(days=1)
        return list(reversed(out))

    def post(self, url, headers=None, json=None, timeout=None):
        assert url.startswith("https://mockapi.kiwoom.com"), url
        if url.endswith("/oauth2/token"):
            return Resp(200, {"token": "SECRET-TOKEN", "return_code": 0})
        api = headers["api-id"]
        code = json.get("stk_cd") or json.get("inds_cd") or f"LIST{json.get('mrkt_tp')}"
        self.calls.append((api, code))
        if code in self.fail:
            return Resp(200, {"return_code": 1, "return_msg": "업무 오류"})
        if api == "ka10099":
            return Resp(200, {"list": self.list_rows[json["mrkt_tp"]], "return_code": 0},
                        {"cont-yn": "N", "next-key": ""})
        b = json["base_dt"]
        rows = self._rows(code, date(int(b[:4]), int(b[4:6]), int(b[6:])))
        off = int(headers["next-key"].split(":")[1]) if headers["cont-yn"] == "Y" else 0
        page, more = rows[off:off + self.PAGE], off + self.PAGE < len(rows)
        key = "stk_dt_pole_chart_qry" if api == "ka10081" else "inds_dt_pole_qry"
        return Resp(200, {key: page, "return_code": 0},
                    {"cont-yn": "Y" if more else "N", "next-key": f"{code}:{off + self.PAGE}" if more else ""})


def setup(name: str, now: datetime = NOW):
    """연구 DB(목록 스냅숏 + 일부 종목 일봉) · 가짜 키움 · 경로 묶음."""
    d = TMP / name
    d.mkdir()
    clock = Clock(now)
    fake = FakeKiwoom(clock)
    fake.list_rows["0"] = [lrow("005930", "삼성전자"), lrow("000660", "SK하이닉스"), lrow("035420", "NAVER"),
                           lrow("069500", "KODEX 200", "8"), lrow("005935", "삼성전자우"), lrow("011110", "위험종목", audit="투자주의"),
                           lrow("123450", "신규상장", reg="20260301")]
    fake.list_rows["10"] = [lrow("247540", "에코프로비엠", "10"), lrow("900990", "외국더미", "10", cls="외국기업")]
    for c in ("005930", "000660", "035420", "069500", "011110", "247540", "001", "101"):
        fake.start[c] = date(2024, 1, 2)
    fake.start["123450"] = date(2026, 3, 3)
    client = ReadOnlyResearchClient(fake, "https://mockapi.kiwoom.com", "APPKEY-XYZ", "SECRET-XYZ",
                                    now=clock, monotonic=lambda: 0.0, sleep=lambda s: None)
    db = d / "research.sqlite3"
    with ResearchStore(db) as rs:
        ResearchCollector(client, rs, CAL).snapshot_universe()
    fake.calls.clear()
    return {"dir": d, "clock": clock, "fake": fake, "client": client, "db": db, "wdb": d / "watch.sqlite3",
            "cfg": d / "watchlist.yaml"}


def cli(env, *args, clock_t: datetime | None = None) -> tuple[int, str]:
    if clock_t:
        env["clock"].t = clock_t
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = W.main(["--config", str(env["cfg"]), "--watch-db", str(env["wdb"]), "--db", str(env["db"]), *args],
                      client=env["client"], now=env["clock"], calendar=CAL)
    return code, buf.getvalue()


LIST = {c: Listing(c, n, "KOSPI", t, f) for c, n, t, f in (
    ("005930", "삼성전자", "COMMON", ()), ("000660", "SK하이닉스", "COMMON", ()), ("069500", "KODEX 200", "ETF", ()),
    ("011110", "위험종목", "COMMON", ("AUDIT:투자주의",)))}


def doc(*symbols, **top) -> dict:
    d = empty_document()
    d["symbols"] = list(symbols)
    d.update(top)
    return d


# ── 1. 설정 해석·검증 ───────────────────────────────────────
ex = yaml.safe_load((ROOT / "config" / "watchlist.example.yaml").read_text(encoding="utf-8"))
r_ex = validate(ex, LIST)
check("1-1) 예시 설정(config/watchlist.example.yaml)은 정상 — 모두 비활성(관심 꺼짐·보유 없음)이라 감시 대상 종목 0",
      r_ex.ok and len(r_ex.config.symbols) == 2 and r_ex.config.active_symbols == ())

bad_text = """schema: w1
monitor: {interval_sec: 5, foo: 1}
symbols:
  - code: 005700
    interest: {enabled: true}
  - code: 5930
    interest: {enabled: true}
  - code: "00593"
    interest: {enabled: true}
  - code: "000660"
  - code: "005930"
    interest: {enabled: yes_please, price_bands: [{low: 72000, high: 70000}]}
    holding: {quantity: 0, avg_price: 70000, stop_price: 80000, target_price: 75000}
  - code: "005930"
    interest: {enabled: true}
"""
r_bad = validate(yaml.safe_load(bad_text), LIST)
fields = {(e.code, e.field) for e in r_bad.errors}
msgs = " / ".join(str(e) for e in r_bad.errors)
check("1-2) 오류를 종목·필드별로 모두 모음(첫 오류에서 멈추지 않음): 숫자로 읽힌 코드(005700→8진수, 5930)는 따옴표 안내, "
      "6자리 아님, 관심·보유 둘 다 없음, 모르는 키, 범위 밖, bool 아님, 가격대 low>high, 수량 0, 손절 ≥ 목표, 중복 코드",
      r_bad.config is None and "따옴표" in msgs and ("3008", "symbols[0].code") in fields
      and ("5930", "symbols[1].code") in fields and ("00593", "symbols[2].code") in fields
      and ("000660", "symbols[3]") in fields and ("-", "monitor") in fields and ("-", "monitor.interval_sec") in fields
      and ("005930", "symbols[4].interest.enabled") in fields
      and ("005930", "symbols[4].interest.price_bands[0]") in fields
      and ("005930", "symbols[4].holding.quantity") in fields and ("005930", "symbols[4].holding") in fields
      and ("005930", "symbols[5].code") in fields and len(r_bad.errors) >= 11)

both = validate(doc({"code": "005930", "interest": {"enabled": True, "s1_analysis": True,
                                                     "price_bands": [{"low": 60000, "high": 62000}]},
                     "holding": {"quantity": 10, "avg_price": 61000, "stop_price": 57000, "target_price": 70000}}),
                LIST)
s0 = both.config.symbols[0] if both.ok else None
check("1-3) 한 종목이 관심 + 수동 보유를 함께(한 항목 — 가격 조회 공유). 보유 정보는 source=MANUAL(증권사 잔고 아님), "
      "종목 키에 시장 포함(KRX:005930)",
      both.ok and s0.modes == ("INTEREST", "HOLDING") and s0.holding.source == "MANUAL" and s0.key == "KRX:005930"
      and isinstance(s0.code, str) and s0.code == "005930")

lc = validate(doc({"code": "123456", "interest": {"enabled": True}},                       # 목록에 없음 + 관심 → 오류
                  {"code": "654321", "holding": {"quantity": 1, "avg_price": 1000}},       # 목록에 없음 + 보유 → 경고
                  {"code": "069500", "interest": {"enabled": True, "s1_analysis": True}},  # ETF에 S1 → 오류
                  {"code": "011110", "name": "엉뚱", "interest": {"enabled": True}}), LIST)
lw = {(w.code, w.field.split(".")[-1]) for w in lc.warnings}
check("1-4) 종목 목록 대조: 관심 감시 종목이 목록에 없으면 오류, 보유만 있으면 경고(보유 감시 유지), 보통주 아닌 종목의 "
      "S1 분석은 오류, 위험 표시·이름 불일치는 경고(삭제·거부 안 함). 손절가 없는 보유는 경고",
      not lc.ok and {(e.code, e.field) for e in lc.errors} == {("123456", "symbols[0].code"),
                                                               ("069500", "symbols[2].interest.s1_analysis")}
      and ("654321", "code") in lw and ("011110", "symbols[3]") in lw and ("011110", "name") in lw
      and ("654321", "stop_price") in lw)
check("1-5) 종목 목록이 없으면 적용 불가(대조할 수 없음 — fail-closed), 형식 검사만은 가능(check_list=False)",
      not validate(doc({"code": "005930", "interest": {"enabled": True}}), None).ok
      and validate(doc({"code": "005930", "interest": {"enabled": True}}), None, check_list=False).ok)
dumped = dump_document(doc({"code": "005700", "interest": {"enabled": False}},
                           {"code": "000660", "interest": {"enabled": False}}))
check("1-6) CLI가 쓰는 YAML은 숫자처럼 보이는 코드를 항상 따옴표로 — 다시 읽어도 문자열(005700이 8진수로 바뀌지 않음)",
      '"005700"' in dumped and '"000660"' in dumped
      and [s["code"] for s in yaml.safe_load(dumped)["symbols"]] == ["005700", "000660"])

# ── 2. 적용 이력·마지막 정상 설정 ─────────────────────────────
E2 = setup("cfg")
lst, sid = M.listing_from_store(ResearchStore(E2["db"]))
ws = WatchStore(E2["wdb"])
E2["cfg"].write_text("schema: w1\nsymbols:\n  - code: 5930\n    interest: {enabled: true}\n", encoding="utf-8")
st1, a1 = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW)
code_p1, out_p1 = cli(E2, "prepare", "--no-list-refresh")
check("2-1) [GPT] 최초 설정이 잘못되면 REJECTED·감시 시작 안 함(can_monitor=False)·신규 매수 차단, prepare는 종료 코드 2·"
      "조회 0회",
      a1["status"] == "REJECTED" and not st1.can_monitor and st1.entry_blocked
      and st1.block_reason.startswith("NO_VALID_CONFIG") and code_p1 == 2 and "시작 안 함" in out_p1
      and E2["fake"].calls == [])
good = dump_document(doc({"code": "005930", "interest": {"enabled": True, "s1_analysis": True}},
                         {"code": "000660", "holding": {"quantity": 5, "avg_price": 180000, "stop_price": 165000}}))
E2["cfg"].write_text(good, encoding="utf-8")
st2, a2 = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW + timedelta(minutes=1))
E2["cfg"].write_text(good.replace("avg_price: 180000", "avg_price: -1"), encoding="utf-8")
st3, a3 = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW + timedelta(minutes=2))
st3b, a3b = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW + timedelta(minutes=3))
check("2-2) [GPT] 운영 중 잘못된 변경 → REJECTED, 마지막 정상 v2로 감시 유지(can_monitor), 오류·사용 중 버전 표시, "
      "신규 매수 차단. 같은 파일을 다시 읽으면 새 기록 없음",
      a2["status"] == "APPLIED" and st2.active_version == a2["version"] and not st2.entry_blocked
      and a3["status"] == "REJECTED" and st3.can_monitor and st3.active_version == a2["version"]
      and st3.config.symbol("000660").holding.avg_price == 180000 and st3.entry_blocked
      and "CONFIG_ERROR" in st3.block_reason and f"v{a2['version']}" in st3.block_reason
      and st3.config_error[0]["field"] == "symbols[1].holding.avg_price"
      and not a3b["new"] and a3b["version"] == a3["version"])
E2["cfg"].unlink()
st4, a4 = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW + timedelta(minutes=4))
E2["cfg"].write_text(good.replace("165000", "160000"), encoding="utf-8")
st5, a5 = M.sync_config(ws, E2["cfg"], lst, sid, now=NOW + timedelta(minutes=5))
hist = ws.history()
check("2-3) 파일이 없어져도 REJECTED(파일 없음)·마지막 정상 유지. 고치면 새 버전 APPLIED·차단 해제. 이력에 시각·버전·"
      "성공 여부·원문·대조한 목록 스냅숏",
      a4["status"] == "REJECTED" and a4["errors"][0]["message"] == "설정 파일 없음" and st4.can_monitor
      and a5["status"] == "APPLIED" and not st5.entry_blocked and st5.config.symbol("000660").holding.stop_price == 160000
      and [h["status"] for h in hist] == ["APPLIED", "REJECTED", "REJECTED", "APPLIED", "REJECTED"]
      and [h["version"] for h in hist] == [5, 4, 3, 2, 1] and all(h["attempted_at"] for h in hist)
      and hist[0]["list_snapshot_id"] == sid and hist[1]["raw_text"] is None and "-1" in hist[2]["raw_text"])
ws.close()

# ── 3. CLI 편집 ─────────────────────────────────────────────
E3 = setup("cli")
c_init, _ = cli(E3, "init")
c_add1, o_add1 = cli(E3, "add", "005930", "--interest", "--s1", "--band", "60,000-62,000:눌림", "--no-fetch")
c_add2, o_add2 = cli(E3, "add", "000660", "--holding", "--qty", "5", "--avg", "180000", "--stop", "165000", "--no-fetch")
raw3 = yaml.safe_load(E3["cfg"].read_text(encoding="utf-8"))
check("3-1) init → add(관심·S1·가격대) → add(수동 보유): 파일에 문자열 코드로 기록, 각 변경이 새 버전 APPLIED, --no-fetch면 조회 0",
      c_init == 0 and c_add1 == 0 and c_add2 == 0 and "v1 APPLIED" in o_add1 and "v2 APPLIED" in o_add2
      and [s["code"] for s in raw3["symbols"]] == ["005930", "000660"]
      and raw3["symbols"][0]["interest"]["price_bands"] == [{"low": 60000, "high": 62000, "label": "눌림"}]
      and raw3["symbols"][1]["holding"] == {"quantity": 5, "avg_price": 180000, "stop_price": 165000}
      and E3["fake"].calls == [])
before3 = E3["cfg"].read_text(encoding="utf-8")
c_bad1, o_bad1 = cli(E3, "add", "999999", "--interest", "--no-fetch")             # 목록에 없음
c_bad2, o_bad2 = cli(E3, "add", "005935", "--interest", "--s1", "--no-fetch")     # 우선주 S1
c_bad5, o_bad5 = cli(E3, "add", "069500", "--interest", "--no-fetch")             # ETF — 목록(주식 행)에 없음
c_bad3, o_bad3 = cli(E3, "set", "000660", "--stop", "200000", "--target", "190000")  # 손절 ≥ 목표
c_bad4, _ = cli(E3, "add", "005930", "--interest", "--no-fetch")                  # 이미 있음
with WatchStore(E3["wdb"]) as w3:
    n_ver = len(w3.history())
check("3-2) 검증을 통과하지 못하는 변경(목록에 없는 코드·우선주 S1·ETF(목록은 주식 행만)·손절 ≥ 목표·중복 추가)은 거부 — 파일·이력 그대로",
      (c_bad1, c_bad2, c_bad3, c_bad4, c_bad5) == (2, 2, 2, 2, 2) and "종목 목록에 없음" in o_bad1
      and "보통주만" in o_bad2 and "PREFERRED" in o_bad2 and "종목 목록에 없음" in o_bad5
      and "stop_price" in o_bad3 and E3["cfg"].read_text(encoding="utf-8") == before3 and n_ver == 2)
c_d1, o_d1 = cli(E3, "add", "247540", "--interest", "--holding", "--qty", "3", "--avg", "114000", "--stop", "100000",
                 "--no-fetch")
c_d2, o_d2 = cli(E3, "disable", "247540")
c_d3, o_d3 = cli(E3, "disable", "247540", "--holding")
c_d4, o_d4 = cli(E3, "remove", "247540")
with WatchStore(E3["wdb"]) as w3:
    cfg3 = M.load_state(w3).config
sym = cfg3.symbol("247540")
check("3-3) [GPT] 관심을 꺼도 보유가 남으면 보유 감시 유지(감시 대상), 보유 감시 끄기·보유 있는 항목 삭제는 거부",
      (c_d1, c_d2, c_d3, c_d4) == (0, 0, 2, 2) and not sym.interest_active and sym.holding_active
      and sym.active and sym.modes == ("HOLDING",) and "끌 수 없음" in o_d3 and "삭제 거부" in o_d4
      and "STOCK:247540" in [t[0] for t in M._targets(cfg3)])
c_h1, _ = cli(E3, "holding-close", "247540")
c_h2, _ = cli(E3, "remove", "247540")
with WatchStore(E3["wdb"]) as w3:
    cfg3b = M.load_state(w3).config
    origins = [h["origin"] for h in w3.history()]
check("3-4) holding-close(청산)로 보유 정보를 지운 뒤에야 삭제 가능. 이력 origin에 CLI 명령",
      c_h1 == 0 and c_h2 == 0 and cfg3b.symbol("247540") is None
      and origins[:4] == ["CLI:remove 247540", "CLI:holding-close 247540", "CLI:disable 247540", "CLI:add 247540"])

# 등록 즉시 과거 일봉 수집
c_new, o_new = cli(E3, "add", "035420", "--interest")
codes_new = {c for _, c in E3["fake"].calls}
with ResearchStore(E3["db"]) as r3:
    meta_new = r3.get_series("STOCK:035420")
    other_series = r3.get_series("STOCK:000660")
check("3-5) [GPT] 신규 등록 종목은 add 즉시 그 종목 과거 일봉 전체 수집(+ 국내 지수) → 확보·검증 후 READY. 다른 등록·"
      "목록 종목은 조회하지 않음",
      c_new == 0 and "데이터: READY" in o_new and codes_new == {"035420", "001", "101"}
      and meta_new is not None and meta_new.first_date == date(2024, 1, 2) and other_series is None)

# ── 4. 데이터 준비 ─────────────────────────────────────────
E4 = setup("prep")
E4["cfg"].write_text(dump_document(doc(
    {"code": "005930", "interest": {"enabled": True, "s1_analysis": True}},
    {"code": "000660", "holding": {"quantity": 5, "avg_price": 180000, "stop_price": 165000}},
    {"code": "011110", "interest": {"enabled": True}},                       # 위험 표시
    {"code": "123450", "interest": {"enabled": True}},                       # 2026-03 상장 — 이력 부족
    {"code": "035420", "interest": {"enabled": False}})), encoding="utf-8")  # 비활성
c_p, o_p = cli(E4, "prepare", "--no-list-refresh")
codes_p = [c for _, c in E4["fake"].calls]
with WatchStore(E4["wdb"]) as w4:
    rd = w4.readiness()
    st_p = M.load_state(w4)
    runs = w4.prepare_runs()
check("4-1) [GPT] prepare는 등록 종목(관심 켜짐·보유) + 국내 지수 2개만 일봉 수집 — 비활성 종목·목록의 다른 종목은 "
      "조회 안 함(전체 시장 수집 없음)",
      c_p == 0 and set(codes_p) == {"001", "101", "005930", "000660", "011110", "123450"}
      and "035420" not in codes_p and "247540" not in codes_p and "LIST0" not in codes_p
      and all(a in ("ka10081", "ka20006") for a, _ in E4["fake"].calls))
g = {c: M.entry_gate(st_p, st_p.config.symbol(c), rd.get(f"STOCK:{c}")) for c in ("005930", "000660", "011110", "123450")}
check("4-2) [GPT] 준비 상태: 160봉(S1 최소 이력) 확보·검증 → READY, 2026-03 상장(이력 부족) → UNKNOWN(INSUFFICIENT_HISTORY)이며 신규 진입 "
      "관찰 제외. 위험 표시 종목은 데이터 READY여도 진입 관찰 제외(감시는 유지). 보유만 있는 종목은 진입 관찰 대상 아님",
      rd["STOCK:005930"]["status"] == "READY" and rd["INDEX:KOSPI:001"]["status"] == "READY"
      and rd["STOCK:123450"]["status"] == "UNKNOWN" and rd["STOCK:123450"]["reason"] == "INSUFFICIENT_HISTORY" and rd["STOCK:123450"]["detail"]["have"] < 160
      and rd["STOCK:011110"]["status"] == "READY" and g["005930"] == (True, [])
      and g["123450"][0] is False and g["123450"][1] == ["DATA_UNKNOWN:INSUFFICIENT_HISTORY"]
      and g["011110"] == (False, ["RISK_FLAGS"]) and g["000660"] == (False, ["INTEREST_OFF"])
      and "STOCK:035420" not in rd)
check("4-3) [GPT] 준비 상태·준비 실행에 적용 설정 버전 연결(readiness.config_version·run_id, prepare_run.config_version)",
      all(r["config_version"] == st_p.active_version and r["run_id"] == runs[0]["run_id"] for r in rd.values())
      and runs[0]["config_version"] == st_p.active_version and runs[0]["status"] == "COMPLETE")
# 운영 중 설정 오류 → 진입 차단, 감시·준비는 마지막 정상 설정으로
E4["cfg"].write_text(E4["cfg"].read_text(encoding="utf-8").replace("schema: w1", "schema: w9"), encoding="utf-8")
E4["fake"].calls.clear()
c_p2, o_p2 = cli(E4, "prepare", "--no-list-refresh", clock_t=datetime(2026, 10, 8, 19, 0))
with WatchStore(E4["wdb"]) as w4:
    st_e = M.load_state(w4)
    rd2 = w4.readiness()
    log_n = len(w4.readiness_log())
check("4-4) [GPT] 운영 중 설정 오류: 마지막 정상 버전으로 준비 계속(새 봉만 추가 — 다시 받지 않음), 결과는 그 버전에 연결, "
      "모든 종목 신규 진입 차단(CONFIG_ERROR). 상태·사유가 그대로인 대상은 이력 행을 늘리지 않음(봉 수는 detail)",
      c_p2 == 0 and "[설정 오류]" in o_p2 and st_e.entry_blocked
      and M.entry_gate(st_e, st_e.config.symbol("005930"), rd2["STOCK:005930"]) == (False, ["CONFIG_ERROR"])
      and all(r["config_version"] == st_e.active_version for r in rd2.values())
      and len(E4["fake"].calls) == 6 and log_n == 6)
# 한 종목 조회 실패 — 다른 종목 영향 없음
E4["cfg"].write_text(E4["cfg"].read_text(encoding="utf-8").replace("schema: w9", "schema: w1")
                     .replace("symbols:\n", 'symbols:\n- code: "247540"\n  interest:\n    enabled: true\n'),
                     encoding="utf-8")
E4["fake"].fail.add("247540")
with ResearchStore(E4["db"]) as r4:
    bars_before = {s: r4.conn.execute("SELECT COUNT(*) FROM bar WHERE series_id=?", (s,)).fetchone()[0]
                   for s in ("STOCK:005930", "STOCK:000660")}
c_p3, o_p3 = cli(E4, "prepare", "--no-list-refresh", clock_t=datetime(2026, 10, 8, 19, 5))
with WatchStore(E4["wdb"]) as w4:
    rd3 = w4.readiness()
    run3 = w4.prepare_runs()[0]
with ResearchStore(E4["db"]) as r4:
    bars_after = {s: r4.conn.execute("SELECT COUNT(*) FROM bar WHERE series_id=?", (s,)).fetchone()[0]
                  for s in ("STOCK:005930", "STOCK:000660")}
check("4-5) 새 종목 조회 실패 → 그 종목만 UNKNOWN(NO_SERIES)·조회 실패 기록, 실행 PARTIAL·종료 코드 1. 다른 종목의 상태·일봉 "
      "그대로",
      c_p3 == 1 and run3["status"] == "PARTIAL" and rd3["STOCK:247540"]["status"] == "UNKNOWN"
      and rd3["STOCK:247540"]["reason"] == "NO_SERIES" and rd3["STOCK:247540"]["detail"]["fetch"]["action"] == "ERROR"
      and rd3["STOCK:005930"]["status"] == "READY" and bars_after == bars_before)
# 비활성화·삭제해도 다른 종목 기록·연구 DB 일봉 보존
E4["fake"].fail.clear()
c_rm, _ = cli(E4, "remove", "005930")
with ResearchStore(E4["db"]) as r4:
    kept = r4.conn.execute("SELECT COUNT(*) FROM bar WHERE series_id='STOCK:005930'").fetchone()[0]
with WatchStore(E4["wdb"]) as w4:
    rd4 = w4.readiness()
check("4-6) 종목을 삭제해도 연구 DB의 그 종목 일봉과 다른 종목 준비 기록은 그대로(삭제·재수집 없음)",
      c_rm == 0 and kept == bars_after["STOCK:005930"] and rd4["STOCK:000660"] == rd3["STOCK:000660"])
c_s, o_s = cli(E4, "status")
check("4-7) status: 사용 중 버전·신규 매수 차단 여부, 종목별 관심/S1/가격대/수동 보유(증권사 잔고 아님)/데이터/위험/진입 관찰",
      c_s == 0 and "사용 중" in o_s and "수동 보유(증권사 잔고 아님)" in o_s and "180,000" in o_s
      and "INSUFFICIENT_HISTORY" in o_s and "AUDIT:투자주의" in o_s)
# 장중(다음 날 10:00)엔 전 거래일까지가 기준 — 여전히 READY
E4["fake"].calls.clear()
c_p4, _ = cli(E4, "prepare", "--no-list-refresh", clock_t=datetime(2026, 10, 12, 10, 0))
with WatchStore(E4["wdb"]) as w4:
    rd5 = w4.readiness()
check("4-8) 장중 준비(10/12 10:00): 완성 기준 마지막 거래일은 10/8(10/9 휴장) — 당일 미완성 봉 없이 READY 유지",
      c_p4 == 0 and rd5["STOCK:000660"]["status"] == "READY"
      and rd5["STOCK:000660"]["detail"]["expected_last"] == "2026-10-08")
# 하루 첫 prepare는 목록(ka10099 2회)만 다시 받고 전체 시장 일봉은 받지 않음
E4["fake"].calls.clear()
c_p5, _ = cli(E4, "prepare", clock_t=datetime(2026, 10, 13, 19, 0))
check("4-9) 그날 첫 prepare는 종목 목록(ka10099 2회)만 새로 받아 설정을 다시 대조 — 일봉은 등록 종목·지수만",
      c_p5 == 0 and [c for a, c in E4["fake"].calls if a == "ka10099"] == ["LIST0", "LIST10"]
      and {c for a, c in E4["fake"].calls if a != "ka10099"} == {"001", "101", "000660", "011110", "123450", "247540"})

# ── 5. 경계 ────────────────────────────────────────────────
FORBIDDEN = ("infra.broker", "infra.storage", "infra.notify", "domain.strategy", "domain.service", "commands",
             "domain.position", "infra.market_data")
bad_imports = []
for f in [ROOT / "domain/watchlist/config.py", ROOT / "infra/watch/store.py", ROOT / "infra/watch/manager.py",
          ROOT / "tools/watchlist.py"]:
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        mods = [node.module] if isinstance(node, ast.ImportFrom) and node.module else \
            [a.name for a in node.names] if isinstance(node, ast.Import) else []
        bad_imports += [(f.name, m) for m in mods if m and m.startswith(FORBIDDEN)]
check("5-1) 지정 종목 설정·준비는 브로커·주문 실행부·원장·전략·알림을 import하지 않음(주문 0)", bad_imports == [])
check("5-2) 테스트 산출물은 임시 폴더에만 — 레포에 data/·commands/·config/watchlist.yaml 생성 없음",
      {p: _fs(p) for p in ("data", "commands")} == FS_BEFORE
      and (ROOT / "config" / "watchlist.yaml").exists() == CFG_EXISTED)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
