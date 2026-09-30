# -*- coding: utf-8 -*-
"""A2: 연구 데이터 수집 회귀 테스트 (가짜 키움 응답·임시 폴더, 네트워크·주문 없음).

GPT A2 보완 여섯 가지(거래 없는 봉, 위험 종목도 과거 수집, orderWarning 원래 값, 수정주가 기준,
날짜 기준 종료, 장중 스냅숏 구분)와 합의 결정(지수 ÷100, 투자주의·환기 제외, 외국기업 제외)을 확인합니다.
"""
from __future__ import annotations

import ast
import hashlib
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.research.holiday_candidates import holiday_candidates, to_yaml_snippet
from domain.research.series import SeriesView
from domain.research.universe import (
    COMMON, ETF, ETN, FOREIGN, PREFERRED, REIT, SPAC, UniversePolicy, classify_row, classify_rows, summarize,
)
from domain.research.weekly import CalendarWeekSchedule, weekly_bars
from infra.research.collector import ResearchCollector, completion, load_probe_list, stock_series_id
from infra.research.kiwoom_readonly import RESEARCH_API, ReadOnlyResearchClient, ResearchApiError, ResearchConfigError
from infra.research.kiwoom_rows import INDEX_DAILY, NO_TRADES, STOCK_DAILY, RowError, parse_row, to_research_bar
from infra.research.store import BACKFILL, DONE, ERROR, FORWARD, PENDING, SHORTFALL, ResearchStore
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
CAL_HASH = hashlib.sha256(Path("config/krx_calendar.yaml").read_bytes()).hexdigest()


def _fs_state(p: str):
    """실제 운영 폴더 상태 (사용자가 수집을 돌린 PC에서는 이미 있을 수 있으므로 '변하지 않음'을 확인)."""
    q = Path(p)
    if not q.exists():
        return None
    return sorted((str(x), x.stat().st_mtime_ns) for x in q.rglob("*")) if q.is_dir() else q.stat().st_mtime_ns


FS_BEFORE = {p: _fs_state(p) for p in ("commands", "data/research")}
TMP = Path(tempfile.mkdtemp(prefix="research_a2_"))


# ── 가짜 키움 ───────────────────────────────────────────────
class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Resp:
    def __init__(self, status: int, body, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


def is_fake_session(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d.year in CAL.covered_years:
        return CAL.is_trading_day(d)
    return True


class FakeKiwoom:
    """ka10099·ka10081·ka20006을 흉내. base_dt 이하 날짜만 내림차순 600행씩, cont-yn/next-key."""
    PAGE = 600

    def __init__(self, clock: Clock):
        self.clock = clock
        self.series: dict[str, dict] = {}
        self.list_rows = {"0": [], "10": []}
        self.calls: list[dict] = []
        self.fail: dict[str, list[str]] = {}
        self.tokens = 0
        self.token_bodies: list = []

    def add(self, code, start, *, index=False, base=10_000, no_trades=(), removed=(), split=None, end=None):
        self.series[code] = {"start": start, "index": index, "base": base, "no_trades": set(no_trades),
                             "removed": set(removed), "split": split, "end": end}

    def _rows(self, code: str, base_dt: date) -> list[dict]:
        s = self.series[code]
        now = self.clock()
        last = min(base_dt, now.date(), s["end"] or now.date())
        out, d, i, prev_c = [], s["start"], 0, None
        while d <= last:
            if is_fake_session(d) and d not in s["removed"]:
                c = s["base"] + (i % 37) * 10 + i
                o, h, lo, v = c - 5, c + 20, c - 20, 100_000 + i * 3
                if d in s["no_trades"]:
                    o = h = lo = c = prev_c
                    v = 0
                if s["split"] and base_dt >= s["split"][2] and d < s["split"][0]:
                    f = s["split"][1]
                    o, h, lo, c, v = o // f, h // f, lo // f, c // f, v * f
                if d == now.date() and now < datetime.combine(d, datetime.min.time()).replace(hour=18, minute=10):
                    v = v // 2                             # 장중 미완성 값
                tv = 0 if v == 0 else max(1, (c * v) // 1_000_000)
                if s["index"]:
                    o, h, lo, c = o * 100 + 12, h * 100 + 34, lo * 100, c * 100 + 56
                out.append({"dt": d.strftime("%Y%m%d"), "open_pric": f"+{o}", "high_pric": f"+{h}",
                            "low_pric": f"-{lo}", "cur_prc": f"-{c}", "trde_qty": str(v), "trde_prica": str(tv)})
                prev_c = c if not s["index"] else prev_c
                i += 1
            d += timedelta(days=1)
        return list(reversed(out))

    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/oauth2/token"):
            self.tokens += 1
            self.token_bodies.append(json)
            return Resp(200, {"token": f"SECRET-TOKEN-{self.tokens}", "return_code": 0})
        api = headers["api-id"]
        code = json.get("stk_cd") or json.get("inds_cd") or f"LIST{json.get('mrkt_tp')}"
        self.calls.append({"url": url, "api": api, "payload": dict(json), "cont": headers["cont-yn"],
                           "key": headers["next-key"], "auth": headers["authorization"], "code": code})
        acts = self.fail.get(code)
        if acts:
            act = acts.pop(0)
            if act == "429":
                return Resp(429, {"return_code": 5, "return_msg": "too many"})
            if act == "401":
                return Resp(401, {"return_code": 3, "return_msg": "token"})
            if act == "RC":
                return Resp(200, {"return_code": 1, "return_msg": "업무 오류"})
            if act == "INTERRUPT":
                raise KeyboardInterrupt
            if act == "TRANSPORT":
                import requests
                raise requests.ConnectionError("boom")
        if api == "ka10099":
            return Resp(200, {"list": self.list_rows[json["mrkt_tp"]], "return_code": 0}, {"cont-yn": "N", "next-key": ""})
        rows = self._rows(code, date(int(json["base_dt"][:4]), int(json["base_dt"][4:6]), int(json["base_dt"][6:])))
        off = int(headers["next-key"].split(":")[1]) if headers["cont-yn"] == "Y" else 0
        page = rows[off:off + self.PAGE]
        more = off + self.PAGE < len(rows)
        key = "stk_dt_pole_chart_qry" if api == "ka10081" else "inds_dt_pole_qry"
        return Resp(200, {key: page, "return_code": 0},
                    {"cont-yn": "Y" if more else "N", "next-key": f"{code}:{off + self.PAGE}" if more else ""})


def lrow(code, name, mc="0", *, audit="정상", state="증거금40%|담보대출|신용가능", ow="0", cls="", reg="20000101"):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": audit, "regDay": reg,
            "lastPrice": "00010000", "state": state, "marketCode": mc, "marketName": "거래소" if mc == "0" else "코스닥",
            "upName": "", "upSizeName": "", "companyClassName": cls, "orderWarning": ow, "nxtEnable": "N", "kind": "A"}


sleeps: list[float] = []


def make(clock: Clock, fake: FakeKiwoom, db: Path, **kw):
    client = ReadOnlyResearchClient(fake, "https://mockapi.kiwoom.com", "APPKEY-XYZ", "SECRET-XYZ",
                                    now=clock, monotonic=lambda: 0.0, sleep=sleeps.append)
    store = ResearchStore(db)
    return client, store, ResearchCollector(client, store, CAL, **kw)


# ── 1. 행 해석 ──────────────────────────────────────────────
sam = {"cur_prc": "53000", "trde_qty": "0", "trde_prica": "0", "dt": "20180430", "open_pric": "53000",
       "high_pric": "53000", "low_pric": "53000", "pred_pre": "0", "pred_pre_sig": "3", "trde_tern_rt": "0.00"}
rb = parse_row(sam)
check("1-1) [보완1] 삼성전자 2018-04-30 실측 행: OHLC 53,000·거래량 0 → NO_TRADES, 원래 값 보존",
      rb.quality == NO_TRADES and rb.values() == (53000, 53000, 53000, 53000, 0, 0))
r2 = parse_row({"cur_prc": "-269250", "trde_qty": "7197202", "trde_prica": "1960185", "dt": "20260929",
                "open_pric": "+274500", "high_pric": "+276000", "low_pric": "-268500"})
rbar = to_research_bar(r2, STOCK_DAILY)
check("1-2) 부호 뗀 원 단위 가격, 거래대금 백만원 → 원(× 1,000,000)",
      r2.close_raw == 269250 and rbar.close == 269250.0 and rbar.trade_value == 1_960_185_000_000)
ix = to_research_bar(parse_row({"cur_prc": "685362", "trde_qty": "166319", "dt": "20260929", "open_pric": "694347",
                                "high_pric": "696564", "low_pric": "683837", "trde_prica": "9761997"}), INDEX_DAILY)
check("1-3) [결정1] 지수 OHLC 모두 ÷100 (685362 → 6853.62), 거래대금 원 환산",
      abs(ix.close - 6853.62) < 1e-9 and abs(ix.open - 6943.47) < 1e-9 and abs(ix.low - 6838.37) < 1e-9
      and ix.trade_value == 9_761_997_000_000)
bad = [parse_row({**sam, "trde_qty": "12", "cur_prc": "abc"}), parse_row({**sam, "trde_qty": "12", "low_pric": "0"}),
       parse_row({**sam, "trde_qty": "12", "high_pric": "52000"})]
check("1-4) 숫자 아님·0 이하·고저 모순 → INVALID(사유별), 연구 봉으로 바꾸지 않음",
      bad[0].quality == "INVALID:FIELD:cur_prc" and bad[1].quality == "INVALID:NONPOSITIVE_PRICE"
      and bad[2].quality == "INVALID:PRICE_RELATION" and all(to_research_bar(b, STOCK_DAILY) is None for b in bad))
try:
    parse_row({**sam, "dt": "2018043"})
    check("1-5) 날짜를 못 읽는 행은 RowError", False)
except RowError:
    check("1-5) 날짜를 못 읽는 행은 RowError", True)
nt_bar = to_research_bar(rb, STOCK_DAILY)
check("1-6) NO_TRADES 봉은 연구 봉으로 남되 no_trades 표시(계산 정책은 series)", nt_bar.no_trades and nt_bar.volume == 0)

# ── 2. 종목 목록 분류 ───────────────────────────────────────
rows = [lrow("005930", "삼성전자"), lrow("005935", "삼성전자우"), lrow("00088K", "한화3우B"),
        lrow("0120G0", "새보통주", "10"), lrow("400840", "하나스팩", "10", cls="스팩"),
        lrow("900110", "딥커머스", "10", cls="외국기업"), lrow("069500", "KODEX 200", "8"),
        lrow("500001", "ETN A", "60"), lrow("348950", "리츠", "6"),
        lrow("111110", "주의종목", audit="투자주의"), lrow("111120", "환기종목", "10", audit="투자주의환기종목"),
        lrow("111130", "정지", state="증거금100%|거래정지"), lrow("111140", "경고숫자", ow="3"),
        lrow("111150", "관리", "10", audit="관리종목", state="관리종목"),
        {k: v for k, v in lrow("111160", "필드없음").items() if k != "orderWarning"}]
rec = {r.code: r for r in classify_rows(rows)}
check("2-1) 증권 유형: 보통주·우선주(코드 끝 5·K)·새 영숫자 보통주·스팩·외국기업·ETF·ETN·리츠",
      [rec[c].security_type for c in ("005930", "005935", "00088K", "0120G0", "400840", "900110", "069500",
                                      "500001", "348950")]
      == [COMMON, PREFERRED, PREFERRED, COMMON, SPAC, FOREIGN, ETF, ETN, REIT]
      and rec["005935"].type_basis == "CODE_SUFFIX:5")
check("2-2) [결정2] 투자주의·투자주의환기종목 → 위험 표시, 현재 자격 없음",
      rec["111110"].risk_flags == ("AUDIT:투자주의",) and rec["111120"].risk_flags == ("AUDIT:투자주의환기종목",)
      and not rec["111110"].eligible_now and not rec["111120"].eligible_now)
check("2-3) [결정3] 외국기업은 S1·수집 대상 아님(유형 FOREIGN)",
      not rec["900110"].collect and not rec["900110"].eligible_now and "TYPE:FOREIGN" in rec["900110"].exclusions)
check("2-4) [보완3] orderWarning은 원래 숫자 그대로 표시(ORDER_WARNING:3), 번역하지 않음",
      rec["111140"].risk_flags == ("ORDER_WARNING:3",) and rec["111140"].order_warning == "3")
check("2-5) state 토큰 거래정지·관리종목, 세 필드 합집합",
      rec["111130"].risk_flags == ("STATE:거래정지",)
      and set(rec["111150"].risk_flags) == {"AUDIT:관리종목", "STATE:관리종목"})
check("2-6) 필드가 없으면 위험으로(ORDER_WARNING_MISSING, fail-closed)",
      rec["111160"].risk_flags == ("ORDER_WARNING_MISSING",) and not rec["111160"].eligible_now)
check("2-7) [보완2] 현재 위험 표시 보통주도 수집 대상(collect) — 자격만 없음",
      all(rec[c].collect and not rec[c].eligible_now for c in ("111110", "111120", "111130", "111140", "111150"))
      and rec["005930"].collect and rec["005930"].eligible_now)
sm = summarize(list(rec.values()))
check("2-8) 요약: 수집 대상 = 보통주 전체, 현재 자격 = 위험 없는 보통주",
      sm["collect"] == 8 and sm["collect_risk_flagged"] == 6 and sm["eligible_now"] == 2)
check("2-9) 분류 정책 버전·해시 — 정책을 바꾸면 해시가 바뀜",
      UniversePolicy().policy_version.startswith("u1:")
      and UniversePolicy().policy_hash() != UniversePolicy(foreign_class_names=()).policy_hash())
try:
    classify_rows([lrow("005930", "a"), lrow("005930", "b")])
    check("2-10) 같은 코드 두 번 → 오류", False)
except ValueError:
    check("2-10) 같은 코드 두 번 → 오류", True)
check("2-11) 원래 행 보존(raw)", rec["111140"].raw["orderWarning"] == "3" and rec["005930"].raw["state"].startswith("증거금"))
probe = os.environ.get("RESEARCH_PROBE_JSONL")
if probe and Path(probe).exists():
    rs, _, _, _ = load_probe_list(Path(probe))
    s = summarize(classify_rows(rs))
    check("2-12) (실측 파일) 주식 2,740 → 수집 2,544 / 위험 257 / 현재 자격 2,287 / state만 관리 84(전체)",
          (s["stock_rows"], s["collect"], s["collect_risk_flagged"], s["eligible_now"],
           s["state_admin_not_in_audit_all_rows"]) == (2740, 2544, 257, 2287, 84))

# ── 3. 조회 클라이언트 안전장치 ─────────────────────────────
refused = 0
for url in ("https://api.kiwoom.com", "http://mockapi.kiwoom.com", "https://mockapi.kiwoom.com:8443",
            "https://mockapi.kiwoom.com.evil.com"):
    try:
        ReadOnlyResearchClient(object(), url, "k", "s")
    except ResearchConfigError:
        refused += 1
check("3-1) 모의 도메인 외(실전·http·다른 포트·유사 호스트) 거부", refused == 4)
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
cl = ReadOnlyResearchClient(fk, "https://mockapi.kiwoom.com", "APPKEY-XYZ", "SECRET-XYZ", now=ck,
                            monotonic=lambda: 0.0, sleep=sleeps.append)
blocked = 0
for api in ("kt10000", "kt10001", "ka10075", "ka10076", "kt00018"):
    try:
        cl.fetch_page(api, {}, "x")
    except ResearchConfigError:
        blocked += 1
check("3-2) 주문·계좌 TR은 호출 전에 차단, 허용 TR은 조회 3개뿐",
      blocked == 5 and not fk.calls and fk.tokens == 0 and set(RESEARCH_API) == {"ka10099", "ka10081", "ka20006"})
try:
    ReadOnlyResearchClient(fk, "https://mockapi.kiwoom.com", "k", "s", min_interval_sec=0.3)
    check("3-3) 호출 간격 0.5초 미만 거부", False)
except ResearchConfigError:
    check("3-3) 호출 간격 0.5초 미만 거부", True)
fk.add("005930", date(2026, 1, 5))
fk.fail["005930"] = ["429", "TRANSPORT", "401"]
pg = cl.fetch_page("ka10081", {"stk_cd": "005930", "base_dt": "20260930", "upd_stkpc_tp": "1"}, "stk_dt_pole_chart_qry")
check("3-4) 429·전송 실패는 대기 후 재시도, 401은 한 번 재인증 후 성공",
      pg.rows and cl.retries == 2 and fk.tokens == 2 and fk.calls[-1]["auth"] == "Bearer SECRET-TOKEN-2")
fk.fail["005930"] = ["RC"]
n_before = len(fk.calls)
try:
    cl.fetch_page("ka10081", {"stk_cd": "005930", "base_dt": "20260930", "upd_stkpc_tp": "1"}, "stk_dt_pole_chart_qry")
    check("3-5) return_code≠0은 재시도 없이 오류", False)
except ResearchApiError as exc:
    check("3-5) return_code≠0은 재시도 없이 오류, 메시지에 토큰·키 없음",
          len(fk.calls) == n_before + 1 and "SECRET" not in str(exc) and "APPKEY" not in str(exc))
check("3-6) 호출 간격(1초)·재시도 대기(2초·5초) 모두 거침", 1.0 in sleeps and 2.0 in sleeps and 5.0 in sleeps)

# ── 4. 백필 ─────────────────────────────────────────────────
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("001", date(2012, 1, 2), index=True, base=2000)
fk.add("101", date(2012, 1, 2), index=True, base=700)
fk.add("005930", date(2012, 1, 2), no_trades={date(2018, 4, 30), date(2018, 5, 2), date(2018, 5, 3)})
fk.add("000660", date(2012, 1, 2))
fk.add("111110", date(2012, 1, 2))                               # 현재 투자주의 → 수집은 함
fk.add("222220", date(2020, 6, 1))                               # 2020 상장
fk.add("333330", date(2019, 3, 4))                               # 상장일은 오래됐는데 원천 이력이 짧음
fk.list_rows["0"] = [lrow("005930", "삼성전자"), lrow("000660", "SK하이닉스"), lrow("111110", "주의", audit="투자주의"),
                     lrow("005935", "삼성전자우"), lrow("069500", "KODEX 200", "8")]
fk.list_rows["10"] = [lrow("222220", "신규상장", "10", reg="20200601"), lrow("333330", "이력짧음", "10", reg="19990101"),
                      lrow("900110", "외국", "10", cls="외국기업")]
db = TMP / "a.sqlite3"
cl, st, col = make(ck, fk, db)
snap = col.snapshot_universe()
check("4-1) [보완6] 스냅숏에 snapshot_date·observed_at(수신 시각)·장 단계·정책 버전 저장",
      st.latest_snapshot()["observed_at"] == "2026-09-30T19:00:00" and snap["market_phase"] == "POST_CLOSE"
      and st.latest_snapshot()["policy_version"].startswith("u1:") and snap["collect"] == 5)
job = col.create_backfill_job(now=ck())
items = st.job_items(job)
check("4-2) [보완2] 백필 대상 = 지수 2 + 보통주 전체(현재 투자주의 포함), 우선주·ETF·외국기업 제외",
      [i["series_id"] for i in items] == ["INDEX:KOSPI:001", "INDEX:KOSDAQ:101", "STOCK:000660", "STOCK:005930",
                                          "STOCK:111110", "STOCK:222220", "STOCK:333330"])
fk.calls.clear()
res = col.run_backfill(job, now=ck, limit=4)
sam_calls = [c for c in fk.calls if c["code"] == "005930"]
check("4-3) [보완5] 필요 시작일(2017-01-02)을 받으면 종료 — 원천에 더 있어도(cont-yn=Y) 5페이지에서 멈춤",
      len(sam_calls) == 5 and st.get_series("STOCK:005930").first_date <= date(2017, 1, 2)
      and st.get_series("STOCK:005930").coverage == "OK")
check("4-4) [보완4] 모든 페이지 같은 base_dt·upd_stkpc_tp=1, 시계열에 조정 기준·조회 시각 저장",
      {(c["payload"]["base_dt"], c["payload"]["upd_stkpc_tp"]) for c in sam_calls} == {("20260930", "1")}
      and st.get_series("STOCK:005930").adj_base_dt == "20260930"
      and st.get_series("STOCK:005930").adj_upd_stkpc_tp == "1"
      and st.get_series("STOCK:005930").fetched_at == datetime(2026, 9, 30, 19, 0))
b5930 = {sb.raw.date: sb for sb in st.load_bars("STOCK:005930")}
check("4-5) [보완1] 거래 없는 봉 3개가 NO_TRADES로 저장·집계, 원래 값 보존",
      all(b5930[d].raw.quality == NO_TRADES and b5930[d].raw.volume == 0
          for d in (date(2018, 4, 30), date(2018, 5, 2), date(2018, 5, 3)))
      and st.get_series("STOCK:005930").no_trades_count == 3)
check("4-6) 백필 봉은 run_type=BACKFILL, ready_at 없음", all(sb.run_type == BACKFILL and sb.ready_at is None
                                                           for sb in b5930.values()))
rbars, ready, meta = st.research_series("STOCK:005930")
v = SeriesView(rbars, [b.date for b in rbars], date(2018, 5, 10), source_id="STOCK:005930")
check("4-7) 연구 봉: 거래대금 원(원천×1e6), 거래 없는 봉이 든 창은 UNKNOWN(NO_TRADES)",
      rbars[-1].trade_value == b5930[rbars[-1].date].raw.trade_value_raw * 1_000_000 and not ready
      and v.window(20)[1] == "NO_TRADES:2018-04-30")
kospi, _, _ = st.research_series("INDEX:KOSPI:001")
check("4-8) 지수 시계열 ÷100 적용(소수 둘째 자리)", abs(kospi[-1].close * 100 - round(kospi[-1].close * 100)) < 1e-6
      and kospi[-1].close != int(kospi[-1].close))
res2 = col.run_backfill(job, now=ck)
it = {i["series_id"]: i for i in st.job_items(job)}
check("4-9) 2020 상장 → SHORTFALL(LISTED_AFTER_START), 상장일 오래됐는데 이력 짧음 → HISTORY_END, 받은 만큼 저장",
      it["STOCK:222220"]["status"] == SHORTFALL and it["STOCK:222220"]["reason"].startswith("LISTED_AFTER_START")
      and it["STOCK:333330"]["status"] == SHORTFALL and it["STOCK:333330"]["reason"].startswith("HISTORY_END")
      and st.get_series("STOCK:222220").first_date == date(2020, 6, 1))
check("4-10) 현재 투자주의 종목도 과거 일봉 수집 완료(DONE)", it["STOCK:111110"]["status"] == DONE
      and st.get_series("STOCK:111110").coverage == "OK")
check("4-11) 모든 항목 끝나면 작업 DONE", res2["job_finished"] and st.get_job(job)["status"] == "DONE")
st.close()

# 페이지 상한
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("005930", date(2012, 1, 2))
fk.list_rows["0"] = [lrow("005930", "삼성전자")]
cl, st, col = make(ck, fk, TMP / "cap.sqlite3", max_pages=2)
col.snapshot_universe()
job = col.create_backfill_job(now=ck(), include_index=False)
col.run_backfill(job, now=ck)
it = st.job_items(job)[0]
check("4-12) 안전 상한(max_pages)에 걸리면 PAGE_CAP으로 기록(페이지 수로 '이력 끝'이라 하지 않음)",
      it["status"] == SHORTFALL and it["reason"].startswith("PAGE_CAP") and it["pages"] == 2)
st.close()

# 장중 백필 → 당일 봉 제외
ck = Clock(datetime(2026, 9, 30, 12, 31))
fk = FakeKiwoom(ck)
fk.add("005930", date(2025, 1, 2))
fk.list_rows["0"] = [lrow("005930", "삼성전자")]
cl, st, col = make(ck, fk, TMP / "intraday.sqlite3", required_from=date(2025, 1, 2))
col.snapshot_universe()
job = col.create_backfill_job(now=ck(), include_index=False)
col.run_backfill(job, now=ck)
m = st.get_series("STOCK:005930")
check("4-13) [보완6] 장중(12:31) 조회의 당일 봉은 저장하지 않음 — 마지막 = 전 거래일, 사유 기록",
      m.last_date == date(2026, 9, 29) and "2026-09-30:INTRADAY" in m.coverage_detail)
check("4-14) 장중 스냅숏은 장 단계 REGULAR로 기록", st.latest_snapshot()["market_phase"] == "REGULAR")
check("4-15) 완성 기준: 15:30 종료 + 160분 → 18:09 미완성, 18:10 완성, 전날 봉은 완성, 달력 밖 오늘은 모름",
      completion(date(2026, 9, 30), datetime(2026, 9, 30, 18, 9), CAL) == "INTRADAY"
      and completion(date(2026, 9, 30), datetime(2026, 9, 30, 18, 10), CAL) == ""
      and completion(date(2026, 9, 29), datetime(2026, 9, 30, 9, 0), CAL) == ""
      and completion(date(2027, 1, 4), datetime(2027, 1, 4, 20, 0), CAL) == "CALENDAR_UNKNOWN")
st.close()

# 중단·재개 (다음 날 재개해도 같은 base_dt)
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
for c in ("000010", "000020", "000030", "000040"):
    fk.add(c, date(2016, 6, 1))
fk.list_rows["0"] = [lrow(c, c) for c in ("000010", "000020", "000030", "000040")]
cl, st, col = make(ck, fk, TMP / "resume.sqlite3")
col.snapshot_universe()
job = col.create_backfill_job(now=ck(), include_index=False)
fk.fail["000020"] = ["PASS", "PASS", "INTERRUPT"]              # 3번째 페이지에서 Ctrl+C (PASS = 정상 응답)
interrupted = False
try:
    col.run_backfill(job, now=ck)
except KeyboardInterrupt:
    interrupted = True
it = {i["series_id"]: i for i in st.job_items(job)}
check("4-16) 중단: 앞 종목은 DONE, 중단된 종목은 저장 안 됨·PENDING 유지",
      interrupted and it["STOCK:000010"]["status"] == DONE and it["STOCK:000020"]["status"] == PENDING
      and st.get_series("STOCK:000020") is None)
orig_set_item = st.set_item


def boom(*a, **k):
    if k.get("in_tx"):
        raise KeyboardInterrupt
    return orig_set_item(*a, **k)


st.set_item = boom
fk.fail.pop("000020", None)
try:
    col.run_backfill(job, now=ck, limit=1)
except KeyboardInterrupt:
    pass
st.set_item = orig_set_item
check("4-17) 저장 트랜잭션 중간에 끊기면 봉·작업 상태 모두 되돌림(원자성)",
      st.get_series("STOCK:000020") is None and st.load_bars("STOCK:000020") == []
      and st.job_items(job, (PENDING,))[0]["series_id"] == "STOCK:000020")
ck.t = datetime(2026, 10, 2, 19, 0)                               # 이틀 뒤 재개
fk.calls.clear()
col.run_backfill(job, now=ck)
check("4-18) [보완4] 이틀 뒤 재개해도 작업의 base_dt(20260930)로 조회, DONE 종목은 다시 받지 않음",
      {c["payload"]["base_dt"] for c in fk.calls} == {"20260930"}
      and "000010" not in {c["code"] for c in fk.calls}
      and st.get_series("STOCK:000040").last_date == date(2026, 9, 30))
check("4-19) 재개 후 작업 DONE", st.get_job(job)["status"] == "DONE")
st.close()

# 오류 종목은 ERROR로 남고 다음 실행에서 재시도
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
for c in ("000010", "000020"):
    fk.add(c, date(2016, 6, 1))
fk.list_rows["0"] = [lrow(c, c) for c in ("000010", "000020")]
cl, st, col = make(ck, fk, TMP / "err.sqlite3")
col.snapshot_universe()
job = col.create_backfill_job(now=ck(), include_index=False)
fk.fail["000010"] = ["RC"]
r = col.run_backfill(job, now=ck)
check("4-20) 업무 오류 종목은 ERROR(사유 기록), 다른 종목은 계속", r["ERROR"] == 1 and r["DONE"] == 1
      and st.job_items(job, (ERROR,))[0]["reason"].startswith("ResearchApiError") and not r["job_finished"])
r = col.run_backfill(job, now=ck)
check("4-21) 다시 실행하면 ERROR 종목만 재시도해 완료", r["processed"] == 1 and r["DONE"] == 1 and r["job_finished"])
st.close()

# ── 5. 매일 갱신 ────────────────────────────────────────────
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("001", date(2016, 6, 1), index=True, base=2000)
fk.add("101", date(2016, 6, 1), index=True, base=700)
fk.add("005930", date(2016, 6, 1), split=(date(2026, 10, 2), 2, date(2026, 10, 2)))
fk.add("000660", date(2016, 6, 1))
fk.list_rows["0"] = [lrow("005930", "삼성전자"), lrow("000660", "SK하이닉스")]
cl, st, col = make(ck, fk, TMP / "fwd.sqlite3")
col.snapshot_universe()
job = col.create_backfill_job(now=ck())
col.run_backfill(job, now=ck)
ck.t = datetime(2026, 10, 1, 17, 0)                               # 마감 후지만 18:10 전
r = col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
check("5-1) [보완6] 18:10 전 갱신: 당일 봉은 저장 안 함(UNCHANGED, INTRADAY)",
      r["action"] == "UNCHANGED" and st.get_series("STOCK:000660").last_date == date(2026, 9, 30)
      and r["dropped"] == [(date(2026, 10, 1), "INTRADAY")])
ck.t = datetime(2026, 10, 1, 19, 5)
r = col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
last = st.load_bars("STOCK:000660")[-1]
check("5-2) 18:10 뒤 갱신: 새 봉 FORWARD, ready_at = 그 응답 수신 시각, 앞 봉은 BACKFILL 그대로",
      r["action"] == "APPEND" and last.raw.date == date(2026, 10, 1) and last.run_type == FORWARD
      and last.ready_at == datetime(2026, 10, 1, 19, 5) and st.load_bars("STOCK:000660")[-2].run_type == BACKFILL)
check("5-3) 장중 값(거래량 절반)은 한 번도 저장되지 않음 — 최종 값만",
      last.raw.volume == int(fk._rows("000660", date(2026, 10, 1))[0]["trde_qty"]))
check("5-4) 겹친 구간이 같으면 확인 기준일(verified_base_dt) 갱신, 조정 기준 revision 유지",
      st.get_series("STOCK:000660").verified_base_dt == "20261001" and st.get_series("STOCK:000660").revision == 1)
r = col.update_series("STOCK:005930", "STOCK", "005930", now=ck)
fwd_1001 = st.load_bars("STOCK:005930")[-1]
ck.t = datetime(2026, 10, 2, 19, 0)                               # 분할 반영일: 과거 가격이 ½로 다시 계산됨
r = col.update_series("STOCK:005930", "STOCK", "005930", now=ck)
m = st.get_series("STOCK:005930")
cur = st.load_bars("STOCK:005930")
hist = st.load_history("STOCK:005930")
check("5-5) [보완4] 과거 값이 바뀌면(수정주가 재계산) 새 기준으로 전체 재수집·통째 교체(REBASE, revision 2)",
      r["action"] == "REFETCH_REBASE" and r["reason"].startswith("CHANGED") and m.revision == 2
      and m.adj_base_dt == "20261002")
check("5-6) 현재 봉은 전부 새 revision — 서로 다른 조정 기준이 섞이지 않음",
      {sb.revision for sb in cur} == {2} and cur[0].raw.close_raw == int(fk._rows("005930", date(2026, 10, 2))[-1]["cur_prc"][1:]))
check("5-7) 이전 값은 bar_history(revision 1)에 전부 보존, 변경 기록(REBASE) 남음",
      {h["revision"] for h in hist} == {1} and len(hist) == len(cur) - 1
      and st.events("STOCK:005930")[-1]["event"] == "REBASE" and st.events("STOCK:005930")[-1]["detail"]["sample"])
b1001 = [sb for sb in cur if sb.raw.date == date(2026, 10, 1)][0]
b1002 = cur[-1]
check("5-8) 재수집 뒤에도 그 날짜의 처음 완성 확보 시각(ready_at)·run_type 유지, 새 날짜는 FORWARD",
      b1001.run_type == FORWARD and b1001.ready_at == fwd_1001.ready_at and b1001.raw.close_raw != fwd_1001.raw.close_raw
      and b1002.raw.date == date(2026, 10, 2) and b1002.run_type == FORWARD and b1002.ready_at == datetime(2026, 10, 2, 19, 0))
fk.series["000660"]["removed"].add(date(2026, 8, 3))
r = col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
check("5-9) 저장된 날짜가 원천에서 사라져도 재수집(MISSING)", r["action"] == "REFETCH_REBASE" and "MISSING" in r["reason"])

# 매일 갱신 전체 흐름: 열린 백필 작업 종목은 건너뜀, 새 상장은 INIT
fk.add("444440", date(2026, 9, 1))
fk.add("555550", date(2016, 6, 1))
fk.list_rows["10"] = [lrow("444440", "새상장", "10", reg="20260901"), lrow("555550", "백필중", "10")]
ck.t = datetime(2026, 10, 6, 19, 0)
col.snapshot_universe()
job2 = col.create_backfill_job(now=ck(), codes=["555550"], include_index=False)
res = col.run_update(now=ck)
check("5-10) 매일 갱신: 열린 백필 작업에 남은 종목은 건너뜀, 목록에 새로 생긴 종목은 처음부터(INIT)",
      res["skipped_open_job"] == 1 and st.get_series("STOCK:555550") is None
      and st.get_series("STOCK:444440").coverage == "LISTED_AFTER_START" and res["tally"].get("INIT") == 1)

# 주봉 관측 모드와의 연결: FORWARD만 있는 주는 OBSERVED 완성, BACKFILL이 섞인 주는 불완전
for dd in (date(2026, 10, 7), date(2026, 10, 8)):
    ck.t = datetime.combine(dd, datetime.min.time()).replace(hour=19, minute=0)
    col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
bars6, ready6, _ = st.research_series("STOCK:000660")
wk = weekly_bars(bars6, CalendarWeekSchedule(CAL), datetime(2026, 10, 10, 12, 0), mode="OBSERVED",
                 data_ready_at=ready6)
w1005 = [w for w in wk if w.week_start == date(2026, 10, 5)][0]
w0928 = [w for w in wk if w.week_start == date(2026, 9, 28)][0]
check("5-11) FORWARD 봉만 있는 주(10/6~8)는 OBSERVED 완성, available_at = 가장 늦은 확보 시각",
      w1005.complete and w1005.available_at == datetime(2026, 10, 8, 19, 0))
check("5-12) BACKFILL(확보 시각 없음)이 섞인 주는 관측 모드에서 불완전(READY_TIME_UNKNOWN)",
      not w0928.complete and w0928.reason.startswith("READY_TIME_UNKNOWN"))
fk.series["000660"]["removed"].add(date(2026, 10, 14))       # 수요일 봉이 원천에서 늦게 나타남
for dd in (date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)):
    ck.t = datetime.combine(dd, datetime.min.time()).replace(hour=19, minute=0)
    col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
b660, r660, _ = st.research_series("STOCK:000660")
w_fri = [w for w in weekly_bars(b660, CalendarWeekSchedule(CAL), datetime(2026, 10, 16, 20, 0), mode="OBSERVED",
                                data_ready_at=r660) if w.week_start == date(2026, 10, 12)][0]
check("5-13) 원천에 빠진 날(10/14)은 저장 안 됨 → 그 주 주봉은 금요일 밤에도 불완전",
      date(2026, 10, 14) not in {b.date for b in b660} and not w_fri.complete)
fk.series["000660"]["removed"].discard(date(2026, 10, 14))
ck.t = datetime(2026, 10, 19, 19, 0)
r = col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
b14 = [sb for sb in st.load_bars("STOCK:000660") if sb.raw.date == date(2026, 10, 14)][0]
check("5-14) 누락 봉이 나중에 나타나면 재수집, 그 봉은 FORWARD·ready_at = 복구 조회 시각(10/19 19:00)",
      "EXTRA" in r["reason"] and b14.run_type == FORWARD and b14.ready_at == datetime(2026, 10, 19, 19, 0))
b660, r660, _ = st.research_series("STOCK:000660")
w_mon = [w for w in weekly_bars(b660, CalendarWeekSchedule(CAL), datetime(2026, 10, 19, 20, 0), mode="OBSERVED",
                                data_ready_at=r660) if w.week_start == date(2026, 10, 12)][0]
check("5-15) 복구 뒤 그 주 주봉 완성, available_at = 복구 조회 시각(주 전체 확보 시각 중 최댓값)",
      w_mon.complete and w_mon.available_at == datetime(2026, 10, 19, 19, 0))
st.close()

# ── 6. 과거 휴장일 후보 ─────────────────────────────────────
days = [date(2026, 1, 1) + timedelta(days=i) for i in range(273)]
trade = [d for d in days if d.weekday() < 5 and CAL.is_trading_day(d)]
hc = holiday_candidates(trade, date(2026, 1, 1), date(2026, 9, 30), trade)
check("6-1) 지수 날짜에서 뽑은 후보 = 달력의 평일 휴장일(2026 확인분)",
      [c["date"] for c in hc["candidates"]] == sorted(d.isoformat() for d in CAL.holidays if d <= date(2026, 9, 30)))
check("6-2) 고정 공휴일은 이름, 음력·대체·선거일은 '확인 필요'",
      {c["date"]: c["guess"] for c in hc["candidates"]}["2026-05-05"] == "어린이날"
      and {c["date"]: c["guess"] for c in hc["candidates"]}["2026-06-03"] == "확인 필요")
hy = holiday_candidates([d for d in (date(2017, 12, 27), date(2017, 12, 28))], date(2017, 12, 27), date(2017, 12, 29),
                        [date(2017, 12, 27)])
check("6-3) 12/31이 주말이면 마지막 평일을 연말 휴장 추정, 두 지수 날짜 차이 보고",
      hy["candidates"][0]["date"] == "2017-12-29" and hy["candidates"][0]["guess"].startswith("연말 휴장")
      and hy["index_date_mismatch"]["only_first"] == ["2017-12-28"])
check("6-4) YAML 초안은 사람 확인용 표시", "확인 전 초안" in to_yaml_snippet(hy) and "확인 필요" in to_yaml_snippet(hc))

# ── 7. CLI (가짜 클라이언트·임시 DB) ─────────────────────────
from tools import research_collect as rc  # noqa: E402

ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("001", date(2016, 6, 1), index=True, base=2000)
fk.add("101", date(2016, 6, 1), index=True, base=700)
fk.add("005930", date(2016, 6, 1))
fk.list_rows["0"] = [lrow("005930", "삼성전자")]
cli_client = ReadOnlyResearchClient(fk, "https://mockapi.kiwoom.com", "k", "s", now=ck, monotonic=lambda: 0.0,
                                    sleep=lambda s: None)
cdb = str(TMP / "cli.sqlite3")
out_yaml = TMP / "hol.yaml"
codes = [rc.main(["--db", cdb, "universe"], client=cli_client, now=ck, calendar=CAL),
         rc.main(["--db", cdb, "backfill", "--limit", "1"], client=cli_client, now=ck, calendar=CAL),
         rc.main(["--db", cdb, "backfill", "--new"], client=cli_client, now=ck, calendar=CAL),
         rc.main(["--db", cdb, "backfill"], client=cli_client, now=ck, calendar=CAL),
         rc.main(["--db", cdb, "status"], client=cli_client, now=ck, calendar=CAL),
         rc.main(["--db", cdb, "holidays", "--out", str(out_yaml)], client=cli_client, now=ck, calendar=CAL)]
check("7-1) CLI: universe → backfill 일부 → 열린 작업 있으면 --new 거부(2) → 이어서 완료 → status → holidays",
      codes == [0, 0, 2, 0, 0, 0] and out_yaml.exists())

# ── 8. 경계: 연구 수집은 주문·운영 경로와 무관 ─────────────────
FORBID = ("infra.broker", "app", "domain.service", "domain.position", "domain.risk", "domain.strategy",
          "infra.storage", "infra.notify", "tools.probe_market_data", "tools.equivalence")
files = list(Path("domain/research").glob("*.py")) + list(Path("infra/research").glob("*.py")) + [
    Path("tools/research_collect.py")]
bad_imports = []
for f in files:
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        names = [node.module] if isinstance(node, ast.ImportFrom) and node.module else (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [])
        bad_imports += [f"{f}:{n}" for n in names if any(n == p or n.startswith(p + ".") for p in FORBID)]
check("8-1) 연구 계층은 브로커·주문 실행부·원장·서비스·알림을 import하지 않음", not bad_imports)
check("8-2) 테스트 산출물은 임시 폴더에만 — commands/·data/research 생성·변경 없음, 달력 파일 그대로",
      all(_fs_state(p) == FS_BEFORE[p] for p in FS_BEFORE)
      and hashlib.sha256(Path("config/krx_calendar.yaml").read_bytes()).hexdigest() == CAL_HASH)

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
sys.exit(0 if failed == 0 else 1)
