# -*- coding: utf-8 -*-
"""A2: 연구 데이터 수집 회귀 테스트 (가짜 키움 응답·임시 폴더, 네트워크·주문 없음).

GPT A2 보완 여섯 가지(거래 없는 봉, 위험 종목도 과거 수집, orderWarning 원래 값, 수정주가 기준,
날짜 기준 종료, 장중 스냅숏 구분)와 합의 결정(지수 ÷100, 투자주의·환기 제외, 외국기업 제외)을 확인합니다.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.research.holiday_candidates import holiday_candidates, to_yaml_snippet
from domain.research.s1 import COMMON as S1_COMMON, Eligibility, evaluate_s1
from domain.research.series import SeriesView
from domain.research.universe import (
    COMMON, ETF, ETN, FOREIGN, PREFERRED, REIT, SPAC, UniversePolicy, classify_row, classify_rows, summarize,
)
from domain.research.weekly import CalendarWeekSchedule, weekly_bars, weekly_trend
from infra.research.collector import CollectError, ResearchCollector, completion, load_probe_list, stock_series_id
from infra.research.kiwoom_readonly import RESEARCH_API, ReadOnlyResearchClient, ResearchApiError, ResearchConfigError
from infra.research.kiwoom_rows import INDEX_DAILY, NO_TRADES, STOCK_DAILY, RowError, parse_row, to_research_bar
from infra.research.store import (
    BACKFILL, DONE, ERROR, FORWARD, PENDING, SHORTFALL, FetchedBar, IntegrityError, ResearchStore,
)
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
        self.tick = timedelta(0)
        self.force_off: dict[str, int] = {}

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
        act = acts.pop(0) if acts else None
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
        self.clock.t += self.tick                          # 페이지마다 수신 시각이 달라지게 (A2-R1 시험용)
        if api == "ka10099":
            body = {"list": self.list_rows[json["mrkt_tp"]], "return_code": 0}
            hdr = {"cont-yn": "N", "next-key": ""}
            if act == "Y_NO_KEY":
                hdr = {"cont-yn": "Y", "next-key": ""}
            return Resp(200, body, hdr)
        base_dt = date(int(json["base_dt"][:4]), int(json["base_dt"][4:6]), int(json["base_dt"][6:]))
        rows = self._rows(code, base_dt)
        if headers["cont-yn"] == "Y" and code in self.force_off:
            off = self.force_off.pop(code)                   # 같은 키로 와도 다음 페이지를 줌(키 반복만 시험)
        else:
            off = int(headers["next-key"].split(":")[1]) if headers["cont-yn"] == "Y" else 0
        page = rows[off:off + self.PAGE]
        more = off + self.PAGE < len(rows)
        nkey = f"{code}:{off + self.PAGE}" if more else ""
        key = "stk_dt_pole_chart_qry" if api == "ka10081" else "inds_dt_pole_qry"
        if act == "EMPTY":                                   # 빈 응답
            page, more, nkey = [], False, ""
        elif act == "ONE_ROW":                               # 한 행만 오고 끝
            page, more, nkey = page[:1], False, ""
        elif act == "SAME_KEY" and headers["cont-yn"] == "Y":  # 다음 키 반복 (행은 정상 진행)
            nkey = headers["next-key"]
            self.force_off[code] = off + self.PAGE
        elif act == "NO_PROGRESS":                           # 같은 페이지를 다시 줌
            page, more, nkey = rows[:self.PAGE], True, f"{code}:{off + self.PAGE}"
        elif act == "FUTURE_ROW":                            # base_dt 뒤 날짜
            fut = dict(page[0], dt=(base_dt + timedelta(days=1)).strftime("%Y%m%d"))
            page = [fut] + page
        body = {key: page, "return_code": 0}
        hdr = {"cont-yn": "Y" if more else "N", "next-key": nkey}
        if act == "Y_NO_KEY":
            hdr = {"cont-yn": "Y", "next-key": ""}
        elif act == "NO_RC":
            body.pop("return_code")
        elif act == "BAD_CONT":
            hdr = {"cont-yn": "", "next-key": ""}
        return Resp(200, body, hdr)


def lrow(code, name, mc="0", *, audit="정상", state="증거금40%|담보대출|신용가능", ow="0", cls="", reg="20000101"):
    return {"code": code, "name": name, "listCount": "0000000010000000", "auditInfo": audit, "regDay": reg,
            "lastPrice": "00010000", "state": state, "marketCode": mc, "marketName": "거래소" if mc == "0" else "코스닥",
            "upName": "", "upSizeName": "", "companyClassName": cls, "orderWarning": ow, "nxtEnable": "N", "kind": "A"}


sleeps: list[float] = []


def fill_lists(fake: FakeKiwoom) -> None:
    """시장별 목록이 비면 원천 이상으로 보므로(빈 페이지 오류) 수집 대상이 아닌 행을 하나씩 채움."""
    if not fake.list_rows["0"]:
        fake.list_rows["0"] = [lrow("069500", "KODEX 200", "8")]
    if not fake.list_rows["10"]:
        fake.list_rows["10"] = [lrow("900990", "외국더미", "10", cls="외국기업")]


def make(clock: Clock, fake: FakeKiwoom, db: Path, **kw):
    fill_lists(fake)
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
      UniversePolicy().policy_version.startswith("u2:")
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
      and st.latest_snapshot()["policy_version"].startswith("u2:") and snap["collect"] == 5)
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
act5930 = st.revisions("STOCK:005930")[0]["activated_at"]
check("4-6) [A2-R1] 백필 봉도 실제 수신 시각(received_at) 기록, 사용 가능 시각 = revision 활성 시각, run_type=BACKFILL",
      all(sb.run_type == BACKFILL and sb.received_at == datetime(2026, 9, 30, 19, 0)
          and sb.available_at.isoformat() == act5930 and sb.first_ready_at == sb.received_at for sb in b5930.values()))
rs5930 = st.research_series("STOCK:005930")
rbars = rs5930.bars
v = SeriesView(rbars, [b.date for b in rbars], date(2018, 5, 10), source_id="STOCK:005930")
check("4-7) 연구 봉: 거래대금 원(원천×1e6), 거래 없는 봉이 든 창은 UNKNOWN(NO_TRADES)",
      rbars[-1].trade_value == b5930[rbars[-1].date].raw.trade_value_raw * 1_000_000
      and set(rs5930.available_at) == {b.date for b in rbars}
      and v.window(20)[1] == "NO_TRADES:2018-04-30")
kospi = st.research_series("INDEX:KOSPI:001").bars
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
check("5-2) 18:10 뒤 갱신: 새 봉 FORWARD, 수신·사용 가능·최초 확보 시각 = 그 응답 수신 시각, 앞 봉은 BACKFILL 그대로",
      r["action"] == "APPEND" and last.raw.date == date(2026, 10, 1) and last.run_type == FORWARD
      and last.received_at == last.available_at == last.first_ready_at == datetime(2026, 10, 1, 19, 5)
      and st.load_bars("STOCK:000660")[-2].run_type == BACKFILL)
check("5-3) 장중 값(거래량 절반)은 한 번도 저장되지 않음 — 최종 값만",
      last.raw.volume == int(fk._rows("000660", date(2026, 10, 1))[0]["trde_qty"]))
check("5-4) 겹친 구간이 같으면 확인 기준일(verified_base_dt) 갱신, 조정 기준 revision 유지",
      st.get_series("STOCK:000660").verified_base_dt == "20261001" and st.get_series("STOCK:000660").revision == 1)
ap = [json.loads(r_["detail_json"]) for r_ in st.conn.execute(
    "SELECT detail_json FROM series_event WHERE series_id='STOCK:000660' AND event='APPEND'")]
check("5-4b) [GPT B1] 새 APPEND 기록에는 판(revision)을 적음 — 저장 근거를 판별로 확인",
      len(ap) == 1 and ap[0].get("revision") == 1 and ap[0]["added"] == 1)
r = col.update_series("STOCK:005930", "STOCK", "005930", now=ck)
col.update_series("INDEX:KOSPI:001", "INDEX", "001", now=ck)
fwd_1001 = st.load_bars("STOCK:005930")[-1]
AS_1001 = datetime(2026, 10, 1, 20, 0)
seen_1001 = st.research_series("STOCK:005930", as_of=AS_1001)       # 10/1 밤에 본 그대로 (재현 기준)
idx_1001 = st.research_series("INDEX:KOSPI:001", as_of=AS_1001)


def s1_at(rs_stock, rs_index, t: date) -> dict:
    sess = [b.date for b in rs_index.bars if b.date <= t]
    sv = SeriesView(rs_stock.bars, sess, t, source_id="STOCK:005930")
    iv = SeriesView(rs_index.bars, sess, t, source_id="INDEX:KOSPI:001")
    return evaluate_s1("005930", sv, iv, None, Eligibility(S1_COMMON, "OK", market_index_id="INDEX:KOSPI:001")).to_dict()


s1_orig_1001 = s1_at(seen_1001, idx_1001, date(2026, 10, 1))        # 10/1 밤의 원래 평가
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
check("5-8) [A2-R1] 정정된 10/1 값의 사용 가능 시각 = 새 revision 활성 시각(10/2 19:00) — 10/1 19:05가 아님. "
      "최초 확보 시각(first_ready_at)·run_type은 기록으로만 보존, 새 날짜는 FORWARD",
      b1001.run_type == FORWARD and b1001.raw.close_raw != fwd_1001.raw.close_raw
      and b1001.available_at == datetime(2026, 10, 2, 19, 0) and b1001.received_at == datetime(2026, 10, 2, 19, 0)
      and b1001.first_ready_at == fwd_1001.first_ready_at == datetime(2026, 10, 1, 19, 5)
      and b1002.raw.date == date(2026, 10, 2) and b1002.run_type == FORWARD
      and b1002.available_at == datetime(2026, 10, 2, 19, 0))
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
rs6 = st.research_series("STOCK:000660")
wk = weekly_bars(rs6.bars, CalendarWeekSchedule(CAL), datetime(2026, 10, 10, 12, 0), mode="OBSERVED",
                 data_ready_at=rs6.available_at)
w1005 = [w for w in wk if w.week_start == date(2026, 10, 5)][0]
w0928 = [w for w in wk if w.week_start == date(2026, 9, 28)][0]
check("5-11) FORWARD 봉만 있는 주(10/6~8)는 OBSERVED 완성, available_at = 가장 늦은 확보 시각",
      w1005.complete and w1005.available_at == datetime(2026, 10, 8, 19, 0))
check("5-12) [A2-R1] 백필 봉도 실제 사용 가능 시각이 있으므로 수집 뒤 평가에서는 OBSERVED 완성 "
      "(9/28 주 = 10/2 19:00 재수집 활성 시각)", w0928.complete and w0928.available_at == datetime(2026, 10, 2, 19, 0))
PAST = datetime(2026, 9, 11, 20, 0)                               # 백필(9/30) 전 시점
rsk = st.research_series("INDEX:KOSPI:001")
past = weekly_bars(rsk.bars, CalendarWeekSchedule(CAL), PAST, mode="OBSERVED", data_ready_at=rsk.available_at)
check("5-12b) 수집 전 과거 시점을 OBSERVED로 보면 미확보 → 추세 UNKNOWN, 시점 조회도 빈 값. "
      "과거 재현은 현재 revision + ASSUMED_DELAY(가정 분석)로 따로",
      weekly_trend(past).state == "UNKNOWN" and st.research_series("INDEX:KOSPI:001", as_of=PAST).bars == []
      and weekly_trend(weekly_bars(rsk.bars, CalendarWeekSchedule(CAL), PAST)).state != "UNKNOWN")
fk.series["000660"]["removed"].add(date(2026, 10, 14))       # 수요일 봉이 원천에서 늦게 나타남
for dd in (date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)):
    ck.t = datetime.combine(dd, datetime.min.time()).replace(hour=19, minute=0)
    col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
rs660 = st.research_series("STOCK:000660")
b660, r660 = rs660.bars, rs660.available_at
w_fri = [w for w in weekly_bars(b660, CalendarWeekSchedule(CAL), datetime(2026, 10, 16, 20, 0), mode="OBSERVED",
                                data_ready_at=r660) if w.week_start == date(2026, 10, 12)][0]
check("5-13) 원천에 빠진 날(10/14)은 저장 안 됨 → 그 주 주봉은 금요일 밤에도 불완전",
      date(2026, 10, 14) not in {b.date for b in b660} and not w_fri.complete)
fk.series["000660"]["removed"].discard(date(2026, 10, 14))
ck.t = datetime(2026, 10, 19, 19, 0)
r = col.update_series("STOCK:000660", "STOCK", "000660", now=ck)
b14 = [sb for sb in st.load_bars("STOCK:000660") if sb.raw.date == date(2026, 10, 14)][0]
check("5-14) 누락 봉이 나중에 나타나면 재수집, 그 봉은 FORWARD·사용 가능 시각 = 복구 조회 시각(10/19 19:00)",
      "EXTRA" in r["reason"] and b14.run_type == FORWARD and b14.available_at == datetime(2026, 10, 19, 19, 0))
rs660 = st.research_series("STOCK:000660")
b660, r660 = rs660.bars, rs660.available_at
w_mon = [w for w in weekly_bars(b660, CalendarWeekSchedule(CAL), datetime(2026, 10, 19, 20, 0), mode="OBSERVED",
                                data_ready_at=r660) if w.week_start == date(2026, 10, 12)][0]
check("5-15) 복구 뒤 그 주 주봉 완성, available_at = 복구 조회 시각(주 전체 확보 시각 중 최댓값)",
      w_mon.complete and w_mon.available_at == datetime(2026, 10, 19, 19, 0))

# ── 9. A2-R1: 값 revision과 사용 가능 시각, 시점 조회 ─────────────
again_1001 = st.research_series("STOCK:005930", as_of=AS_1001)
check("9-1) [A2-R1] 10/2 정정 뒤에도 10/1 20:00 시점 조회는 revision 1의 원래 값 그대로(10/1 봉 포함)",
      again_1001.revision == 1 and again_1001.bars == seen_1001.bars and again_1001.bars[-1].date == date(2026, 10, 1)
      and again_1001.bars[-1].close == fwd_1001.raw.close_raw and again_1001.basis.base_dt == "20260930")
check("9-2) 10/1 재평가(S1)가 10/1 당시 평가와 완전히 같음 — 10/2에 정정한 값이 들어가지 않음",
      s1_at(again_1001, st.research_series("INDEX:KOSPI:001", as_of=AS_1001), date(2026, 10, 1)) == s1_orig_1001)
check("9-3) 반대로 현재 revision(정정 값)으로 10/1을 보면 결과가 달라짐 — 시점 조회가 필요한 이유",
      s1_at(st.research_series("STOCK:005930"), st.research_series("INDEX:KOSPI:001"), date(2026, 10, 1)) != s1_orig_1001)
r_before = st.research_series("STOCK:005930", as_of=datetime(2026, 10, 2, 18, 59))
r_after = st.research_series("STOCK:005930", as_of=datetime(2026, 10, 2, 19, 0))
check("9-4) 새 revision 활성 시각 전(10/2 18:59)은 revision 1, 활성 시각(19:00)부터 revision 2(10/2 봉 포함)",
      r_before.revision == 1 and r_before.bars[-1].date == date(2026, 10, 1)
      and r_after.revision == 2 and r_after.bars[-1].date == date(2026, 10, 2))
check("9-5) revision 기록: 조정 기준·활성·대체 시각",
      [(x["revision"], x["base_dt"], x["activated_at"], x["superseded_at"]) for x in st.revisions("STOCK:005930")]
      == [(1, "20260930", "2026-09-30T19:00:00", "2026-10-02T19:00:00"), (2, "20261002", "2026-10-02T19:00:00", None)])
st.close()

# ── 9(계속). 페이지별 수신 시각 ────────────────────────────────
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("005930", date(2016, 6, 1))
fk.list_rows["0"] = [lrow("005930", "삼성전자")]
cl, st2, col2 = make(ck, fk, TMP / "pages.sqlite3")
col2.snapshot_universe()
fk.tick = timedelta(minutes=5)                                       # 호출마다 5분씩 늦게 도착
job = col2.create_backfill_job(now=ck(), include_index=False)
col2.run_backfill(job, now=ck)
b2 = st2.load_bars("STOCK:005930")
newest, p2 = b2[-1], b2[-601]
act2 = st2.revisions("STOCK:005930")[0]["activated_at"]
check("9-6) [A2-R1] 행마다 그 페이지 수신 시각 — 첫 페이지 봉 19:05, 둘째 페이지 봉 19:10 (첫 페이지 시각으로 덮지 않음)",
      newest.received_at == datetime(2026, 9, 30, 19, 5) and p2.received_at == datetime(2026, 9, 30, 19, 10)
      and act2 == max(sb.received_at for sb in b2).isoformat())
check("9-7) 새 시계열은 마지막 페이지 수신 뒤 활성 — 19:07 평가에는 19:10에 받은 봉을 포함해 아무것도 쓰지 않음",
      st2.research_series("STOCK:005930", as_of=datetime(2026, 9, 30, 19, 7)).bars == []
      and len(st2.research_series("STOCK:005930", as_of=datetime.fromisoformat(act2)).bars) == len(b2))
st2.close()

# ── 10. A2-R2: 재수집 후보 검증 ────────────────────────────────
SID = "STOCK:005930"


def split_store(name: str):
    c = Clock(datetime(2026, 9, 30, 19, 0))
    f = FakeKiwoom(c)
    f.add("005930", date(2016, 6, 1), split=(date(2026, 10, 1), 2, date(2026, 10, 1)))   # 10/1부터 과거가 ½
    f.add("001", date(2016, 6, 1), index=True, base=2000)
    f.add("101", date(2016, 6, 1), index=True, base=700)
    f.list_rows["0"] = [lrow("005930", "삼성전자")]
    _, s_, c_ = make(c, f, TMP / name)
    c_.snapshot_universe()
    c_.run_backfill(c_.create_backfill_job(now=c(), include_index=False), now=c)
    c.t = datetime(2026, 10, 1, 19, 0)
    return c, f, s_, c_


def frozen(s_):
    m = s_.get_series(SID)
    return ([(sb.raw.values(), sb.revision, sb.available_at) for sb in s_.load_bars(SID)],
            (m.revision, m.adj_base_dt, m.verified_base_dt, m.first_date, m.last_date))


ck, fk, st3, col3 = split_store("r2_empty.sqlite3")
before = frozen(st3)
fk.fail["005930"] = ["PASS", "EMPTY"]                     # 첫 페이지 정상(변경 발견) → 재수집 응답이 빔
try:
    col3.update_series(SID, "STOCK", "005930", now=ck)
    raised = False
except CollectError:
    raised = True
m3 = st3.get_series(SID)
check("10-1) [A2-R2] 변경 감지 뒤 재수집이 빈 응답 → 값·조정 기준일·확인 기준일·revision 그대로, REBASE_REQUIRED 기록",
      raised and frozen(st3) == before and m3.integrity == "REBASE_REQUIRED"
      and st3.events(SID)[-1]["event"] == "VERIFY_FAILED")
try:
    st3.append_forward(series_id=SID, bars=[FetchedBar(parse_row(fk._rows("005930", date(2026, 10, 1))[0]), ck())],
                       verified_base_dt="20261001", verified_at=ck())
    check("10-2) REBASE_REQUIRED 동안은 새 날짜를 기존 기준 데이터에 붙이지 않음(IntegrityError)", False)
except IntegrityError:
    check("10-2) REBASE_REQUIRED 동안은 새 날짜를 기존 기준 데이터에 붙이지 않음(IntegrityError)",
          frozen(st3) == before)
ck.t = datetime(2026, 10, 2, 19, 0)
r = col3.update_series(SID, "STOCK", "005930", now=ck)
m3 = st3.get_series(SID)
cur3 = st3.load_bars(SID)
check("10-3) 다음 갱신은 첫 페이지 붙이기 없이 바로 재수집 → 검증 통과 뒤에만 revision 2·새 조정 기준 활성, 이전 값 보존",
      r["action"] == "REFETCH_REBASE" and r["reason"] == "RETRY_REBASE_REQUIRED" and m3.revision == 2
      and m3.adj_base_dt == "20261002" and m3.integrity == "OK" and {sb.revision for sb in cur3} == {2}
      and len(st3.load_history(SID)) == len(before[0]))
check("10-4) 재수집으로 들어온 10/1·10/2는 FORWARD, 사용 가능 시각 = 활성 시각",
      [(sb.raw.date, sb.run_type, sb.available_at) for sb in cur3[-2:]]
      == [(date(2026, 10, 1), FORWARD, datetime(2026, 10, 2, 19, 0)), (date(2026, 10, 2), FORWARD, datetime(2026, 10, 2, 19, 0))])
st3.close()

ck, fk, st3, col3 = split_store("r2_one.sqlite3")
before = frozen(st3)
old_0930 = [sb.raw.close_raw for sb in st3.load_bars(SID) if sb.raw.date == date(2026, 9, 30)][0]
fk.fail["005930"] = ["PASS", "ONE_ROW"]                   # 재수집이 10/1 한 행만 주고 끝
r = col3.update_series(SID, "STOCK", "005930", now=ck)
check("10-5) [A2-R2 재현] 재수집이 한 행이면 EXTEND가 아니라 REBASE_FAILED — 9/30 가격·조정 기준일 그대로",
      r["action"] == "REBASE_FAILED" and any(p.startswith("SHORT_HISTORY") for p in r["problems"])
      and any(p.startswith("EXPECTED_MISMATCH") for p in r["problems"]) and frozen(st3) == before
      and [sb.raw.close_raw for sb in st3.load_bars(SID) if sb.raw.date == date(2026, 9, 30)][0] == old_0930
      and st3.get_series(SID).adj_base_dt == "20260930")
ck.t = datetime(2026, 10, 2, 19, 0)                        # 다음 날 정상 재수집으로 복구
r = col3.update_series(SID, "STOCK", "005930", now=ck)
q_fail = st3.research_series(SID, as_of=datetime(2026, 10, 1, 20, 0))
q_before = st3.research_series(SID, as_of=datetime(2026, 9, 30, 20, 0))
q_after = st3.research_series(SID, as_of=datetime(2026, 10, 2, 20, 0))
check("14-1) [2차 #2] 9/30 정상 → 10/1 실패 → 10/2 복구 뒤에도 10/1 20:00 시점 조회는 당시 상태 REBASE_REQUIRED",
      r["action"] == "REFETCH_REBASE" and q_fail.query_mode == "AS_OF" and q_fail.integrity == "REBASE_REQUIRED"
      and "SHORT_HISTORY" in q_fail.integrity_detail)
check("14-2) 실패 전(9/30 20:00)·복구 후(10/2 20:00)는 OK, 현재 조회도 OK",
      q_before.integrity == "OK" and q_after.integrity == "OK" and st3.research_series(SID).integrity == "OK"
      and st3.research_series(SID).query_mode == "CURRENT")
check("14-3) 시점 메타와 현재 메타 구분 — 10/1 조회의 revision_info는 revision 1·기준일 20260930, "
      "current_meta는 revision 2·20261002",
      q_fail.revision == 1 and q_fail.revision_info["base_dt"] == "20260930" and q_fail.basis.base_dt == "20260930"
      and q_fail.current_meta.revision == 2 and q_fail.current_meta.adj_base_dt == "20261002"
      and q_fail.current_meta.integrity == "OK")
check("14-4) 정합성 이력: 실패 시각·복구 시각 기록",
      [(x["at"], x["status"]) for x in st3.integrity_log(SID)]
      == [("2026-10-01T19:00:00", "REBASE_REQUIRED"), ("2026-10-02T19:00:00", "OK")])
st3.close()

ck, fk, st3, col3 = split_store("r2_cap.sqlite3")
before = frozen(st3)
col3.max_pages = 2                                        # 재수집이 페이지 상한으로 2019년까지만
r = col3.update_series(SID, "STOCK", "005930", now=ck)
check("10-6) 재수집이 페이지 상한에 걸려 필요한 시작일에 못 미치면 REBASE_FAILED, 기존 값·메타 유지",
      r["action"] == "REBASE_FAILED" and any(p.startswith("SHORT_HISTORY") for p in r["problems"])
      and frozen(st3) == before and st3.get_series(SID).integrity == "REBASE_REQUIRED")
col3.max_pages = 8
fk.fail["005930"] = ["ONE_ROW"]                           # 재검증 필요 상태 → 바로 재수집 → 또 한 행
res = col3.run_update(now=ck)
check("10-7) 매일 갱신 집계: 검증 실패(REBASE_FAILED)는 정상 갱신이 아니라 failed로 셈(종료 코드 1)",
      res["tally"].get("REBASE_FAILED") == 1 and res["failed"] == 1 and frozen(st3) == before)
st3.close()

ck, fk, st3, col3 = split_store("r2_job.sqlite3")
before = frozen(st3)
job = col3.create_backfill_job(now=ck(), include_index=False, series_ids=[SID])
fk.fail["005930"] = ["ONE_ROW"]
res = col3.run_backfill(job, now=ck)
it = st3.job_items(job)[0]
check("10-8) 기존 시계열을 다시 받는 백필도 같은 검증 — 부족하면 SHORTFALL 저장이 아니라 ERROR(REBASE_FAILED), 값 그대로",
      it["status"] == ERROR and "REBASE_FAILED" in it["reason"] and frozen(st3) == before)
st3.close()

# ── 11. A2-R3: 응답·연속조회 계약 ──────────────────────────────
ck = Clock(datetime(2026, 9, 30, 19, 0))
fk = FakeKiwoom(ck)
fk.add("005930", date(2016, 6, 1))
fk.add("000660", date(2016, 6, 1))
fk.list_rows["0"] = [lrow("005930", "삼성전자"), lrow("000660", "SK하이닉스")]
cl, st4, col4 = make(ck, fk, TMP / "contract.sqlite3")
PAY = {"stk_cd": "005930", "base_dt": "20260930", "upd_stkpc_tp": "1"}
bad = []
for act in ("Y_NO_KEY", "NO_RC", "BAD_CONT"):
    fk.fail["005930"] = [act]
    try:
        cl.fetch_page("ka10081", PAY, "stk_dt_pole_chart_qry")
        bad.append(act)
    except ResearchApiError:
        pass
check("11-1) [A2-R3] cont-yn=Y인데 next-key 없음·return_code 누락·cont-yn 이상 → ResearchApiError", not bad)
bad = []
for acts in (["EMPTY"], ["PASS", "EMPTY"], ["PASS", "SAME_KEY"], ["PASS", "NO_PROGRESS"], ["FUTURE_ROW"]):
    fk.fail["005930"] = list(acts)
    try:
        col4.fetch_full(STOCK_DAILY, "005930", "20260930", date(2017, 1, 2))
        bad.append(acts)
    except CollectError:
        pass
check("11-2) 빈 첫 응답·빈 연속 페이지·다음 키 반복·과거로 진행 안 함·base_dt 뒤 날짜 → CollectError", not bad)
col4.snapshot_universe()
job = col4.create_backfill_job(now=ck(), include_index=False)
fk.fail["005930"] = ["PASS", "Y_NO_KEY"]
col4.run_backfill(job, now=ck)
it = {i["series_id"]: i for i in st4.job_items(job)}
check("11-3) 원천 계약 위반 종목은 작업 ERROR, 시계열을 만들지 않음(이력 끝으로 오인하지 않음), 다른 종목은 정상",
      it["STOCK:005930"]["status"] == ERROR and "next-key" in it["STOCK:005930"]["reason"]
      and st4.get_series("STOCK:005930") is None and it["STOCK:000660"]["status"] == DONE)
col4.run_backfill(job, now=ck)
check("11-4) 다시 실행하면 정상 연속조회로 완료(REACHED)", st4.get_series("STOCK:005930").coverage == "OK"
      and st4.get_job(job)["status"] == "DONE")
n_snap = st4.conn.execute("SELECT COUNT(*) FROM universe_snapshot").fetchone()[0]
fk.fail["LIST0"] = ["Y_NO_KEY"]
try:
    col4.snapshot_universe()
    check("11-5) 목록 조회도 같은 계약 — 모순 응답이면 스냅숏을 저장하지 않음", False)
except ResearchApiError:
    check("11-5) 목록 조회도 같은 계약 — 모순 응답이면 스냅숏을 저장하지 않음",
          st4.conn.execute("SELECT COUNT(*) FROM universe_snapshot").fetchone()[0] == n_snap)
st4.close()

# ── 12. A2-R4: 빈 state·state에만 있는 위험 표시 ─────────────────
cases = {"   ": "STATE_MISSING", "": "STATE_MISSING", "투자주의": "STATE:투자주의",
         "증거금100%|투자주의환기종목": "STATE:투자주의환기종목", "투자경고": "STATE:투자경고",
         "증거금40%|단기과열": "STATE:단기과열", "정리매매": "STATE:정리매매", "증거금40%|새토큰": "STATE_UNRECOGNIZED:새토큰"}
recs = {k: classify_row(lrow("123450", "x", state=k)) for k in cases}
check("12-1) [A2-R4] 빈·공백 state는 보류, state에만 있는 투자주의·환기·경고·단기과열·정리매매도 위험, 모르는 토큰도 보류",
      all(recs[k].risk_flags == (v,) and not recs[k].eligible_now for k, v in cases.items()))
check("12-2) 위험 자격만 막고 수집 대상(collect)은 유지 — 현재 상태가 백필 선택에 개입하지 않음",
      all(r.collect for r in recs.values()))
check("12-3) 정상 토큰(증거금N%·담보대출·신용가능)만 있으면 통과, 정책 u2",
      classify_row(lrow("123450", "x", state="증거금20%|담보대출|신용가능")).eligible_now
      and UniversePolicy().policy_version.startswith("u2:"))

# ── 13. 이전 버전 저장소 이전 (r1 → r2 → r3) ──────────────────────
R1_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE series(series_id TEXT PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL, api_id TEXT NOT NULL,
  price_scale INTEGER NOT NULL, trade_value_unit INTEGER NOT NULL, volume_unit TEXT NOT NULL,
  revision INTEGER NOT NULL, adj_upd_stkpc_tp TEXT, adj_base_dt TEXT NOT NULL, fetched_at TEXT NOT NULL,
  job_id TEXT, verified_base_dt TEXT NOT NULL, verified_at TEXT NOT NULL,
  first_date TEXT, last_date TEXT, required_from TEXT, coverage TEXT NOT NULL, coverage_detail TEXT NOT NULL,
  no_trades_count INTEGER NOT NULL, invalid_count INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE bar(series_id TEXT NOT NULL, date TEXT NOT NULL,
  open_raw INTEGER, high_raw INTEGER, low_raw INTEGER, close_raw INTEGER, volume INTEGER, trade_value_raw INTEGER,
  quality TEXT NOT NULL, run_type TEXT NOT NULL, ready_at TEXT, fetched_at TEXT NOT NULL, revision INTEGER NOT NULL,
  PRIMARY KEY(series_id, date)) WITHOUT ROWID;
CREATE TABLE bar_history(series_id TEXT NOT NULL, revision INTEGER NOT NULL, date TEXT NOT NULL,
  open_raw INTEGER, high_raw INTEGER, low_raw INTEGER, close_raw INTEGER, volume INTEGER, trade_value_raw INTEGER,
  quality TEXT NOT NULL, run_type TEXT NOT NULL, ready_at TEXT, fetched_at TEXT NOT NULL,
  superseded_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(series_id, revision, date)) WITHOUT ROWID;
CREATE TABLE series_event(event_id INTEGER PRIMARY KEY AUTOINCREMENT, series_id TEXT NOT NULL, at TEXT NOT NULL,
  event TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE job(job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, base_dt TEXT NOT NULL,
  upd_stkpc_tp TEXT NOT NULL, required_from TEXT NOT NULL, max_pages INTEGER NOT NULL,
  snapshot_id INTEGER, status TEXT NOT NULL, params_json TEXT NOT NULL);
CREATE TABLE job_item(job_id TEXT NOT NULL, series_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
  code TEXT NOT NULL, reg_day TEXT, status TEXT NOT NULL, pages INTEGER NOT NULL, first_date TEXT, last_date TEXT,
  reason TEXT NOT NULL, attempts INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(job_id, series_id)) WITHOUT ROWID;
"""
MS = "STOCK:005930"


def r1_db(path: Path, *, with_events: bool = True, rev2_at: str = "2026-10-01T19:10:00") -> None:
    """r1 코드가 남기던 그대로: 분할 재계산 두 번(REBASE) + FORWARD 추가 1건.
    revision 2는 10/1 19:00 첫 페이지 수신 → 19:10 저장(REBASE 기록), revision 3은 10/2 19:00 → 19:10."""
    con = sqlite3.connect(path)
    con.executescript(R1_DDL)
    con.execute("INSERT INTO meta VALUES('schema_version','r1')")
    con.execute(f"INSERT INTO series VALUES('{MS}','STOCK','005930','ka10081',1,1000000,'SHARES',3,'1','20261002',"
                "'2026-10-02T19:00:00','j1','20261003','2026-10-03T19:00:00','2026-09-28','2026-10-03','2017-01-02','OK',"
                "'',0,0,'2026-10-03T19:00:01')")
    for rev, price, fetched, sup in ((1, 400, "2026-09-30T19:00:00", "2026-10-01T19:10:00"),
                                     (2, 200, "2026-10-01T19:00:00", "2026-10-02T19:10:00")):
        for d in ("2026-09-28", "2026-09-29", "2026-09-30") + (("2026-10-01",) if rev == 2 else ()):
            con.execute(f"INSERT INTO bar_history VALUES('{MS}',?,?,?,?,?,?,1000,1,'','BACKFILL',NULL,?,?,'REBASE')",
                        (rev, d, price, price + 5, price - 5, price, fetched, sup))
    for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"):
        con.execute(f"INSERT INTO bar VALUES('{MS}',?,100,105,95,100,1000,1,'','BACKFILL',NULL,'2026-10-02T19:00:00',3)",
                    (d,))
    con.execute(f"INSERT INTO bar VALUES('{MS}','2026-10-03',101,106,96,101,1000,1,'','FORWARD','2026-10-03T19:00:00',"
                "'2026-10-03T19:00:00',3)")
    if with_events:
        for at, ev, det in (("2026-09-30T19:05:00", "INIT", {"revision": 1}),
                            (rev2_at, "REBASE", {"revision": 2}),
                            ("2026-10-02T19:10:00", "REBASE", {"revision": 3}),
                            ("2026-10-03T19:00:01", "APPEND", {"added": 1})):
            con.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
                        (MS, at, ev, json.dumps(det)))
    con.execute("INSERT INTO job VALUES('j1','BACKFILL','2026-09-30T18:59:00','20260930','1','2017-01-02',8,1,'DONE','{}')")
    con.execute(f"INSERT INTO job_item VALUES('j1','{MS}',0,'STOCK','005930',NULL,'DONE',4,'2026-09-28',"
                "'2026-09-30','',1,'2026-09-30T19:05:00')")
    con.commit()
    con.close()


mdb = TMP / "r1.sqlite3"
r1_db(mdb)
st5 = ResearchStore(mdb)
revs5 = [(x["revision"], x["activated_at"], x["superseded_at"], x["reason"]) for x in st5.revisions(MS)]
check("13-1) r1 DB를 열면 r5까지 자동 이전 — 봉·작업 보존, 스키마 r5",
      st5.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "r5"
      and len(st5.load_bars(MS)) == 6 and len(st5.load_history(MS)) == 7 and st5.job_counts("j1") == {"DONE": 1})
check("13-2) [2차 #1] 과거 판 활성 시각은 저장 기록(INIT·REBASE) 시각으로 복원 — 첫 페이지 수신 시각 아님, 구간 겹침 없음",
      revs5 == [(1, "2026-09-30T19:05:00", "2026-10-01T19:10:00", "MIGRATED_R1_HISTORY:EVENT"),
                (2, "2026-10-01T19:10:00", "2026-10-02T19:10:00", "MIGRATED_R1_HISTORY:EVENT"),
                (3, "2026-10-02T19:10:00", None, "MIGRATED_R1:EVENT")])
q1905 = st5.research_series(MS, as_of=datetime(2026, 10, 1, 19, 5))
check("13-3) [2차 #1 재현] 10/1 19:05(revision 2 첫 페이지 수신 뒤·저장 전) 조회 → revision 1의 가격(400), revision 2 아님",
      q1905.revision == 1 and {b.close for b in q1905.bars} == {400.0} and q1905.time_proof == "OK")
check("13-4) 9/30 19:02(첫 페이지 수신 뒤·최초 저장 전)에는 아직 시계열 없음, 10/2 19:05는 revision 2",
      st5.research_series(MS, as_of=datetime(2026, 9, 30, 19, 2)).integrity == "NO_REVISION"
      and st5.research_series(MS, as_of=datetime(2026, 10, 2, 19, 5)).revision == 2
      and len(st5.research_series(MS, as_of=datetime(2026, 10, 2, 19, 5)).bars) == 4)
mb = {sb.raw.date: sb for sb in st5.load_bars(MS)}
check("13-5) 봉 사용 가능 시각 = max(판 활성 시각, 수신 뒤 첫 저장 기록) — 재수집 봉 10/2 19:10, FORWARD 추가 봉 19:00:01, "
      "수신 시각·최초 확보 시각은 원래 값",
      mb[date(2026, 9, 28)].available_at == datetime(2026, 10, 2, 19, 10)
      and mb[date(2026, 9, 28)].received_at == datetime(2026, 10, 2, 19, 0)
      and mb[date(2026, 10, 3)].available_at == datetime(2026, 10, 3, 19, 0, 1)
      and mb[date(2026, 10, 3)].first_ready_at == datetime(2026, 10, 3, 19, 0)
      and all(sb.time_basis == "MIGRATED" for sb in mb.values()))
check("13-6) 이전 판 봉도 같은 규칙(revision 2 봉 = 10/1 19:10)",
      {h["available_at"] for h in st5.load_history(MS) if h["revision"] == 2} == {"2026-10-01T19:10:00"})
st5.close()
st5 = ResearchStore(mdb)
check("13-7) 다시 열어도 그대로(이전은 한 번만)", [(x["revision"], x["activated_at"]) for x in st5.revisions(MS)]
      == [(r[0], r[1]) for r in revs5] and len(st5.load_bars(MS)) == 6)
st5.close()

# 이미 r2로 이전된 DB(이전 버전이 만든 잘못된 시각)도 열 때 보정
class R2Only(ResearchStore):
    def _upgrade_r2_to_r3(self) -> None:          # 이전 버전처럼 r2에서 멈춤
        pass

    def _recheck_migrated_times(self, from_version: str) -> None:
        pass


m2 = TMP / "r2_old.sqlite3"
r1_db(m2)
R2Only(m2).close()
con = sqlite3.connect(m2)
old_rev2 = con.execute(f"SELECT activated_at FROM series_revision WHERE series_id='{MS}' AND revision=2").fetchone()[0]
con.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
            (MS, "2026-10-02T19:00:00", "VERIFY_FAILED", json.dumps({"problems": ["SHORT_HISTORY:x"]})))
con.commit()
con.close()
st6 = ResearchStore(m2)
check("13-8) 이전 버전 r2 DB는 revision 2 활성 시각이 첫 페이지 수신 시각(10/1 19:00)이었음 → 열면 저장 기록 시각(19:10)으로 보정",
      old_rev2 == "2026-10-01T19:00:00" and st6.revisions(MS)[1]["activated_at"] == "2026-10-01T19:10:00"
      and st6.research_series(MS, as_of=datetime(2026, 10, 1, 19, 5)).revision == 1)
check("13-9) 정합성 이력도 기록(VERIFY_FAILED → 다음 REBASE)에서 복원 — 그 사이 시점 조회는 REBASE_REQUIRED",
      st6.research_series(MS, as_of=datetime(2026, 10, 2, 19, 5)).integrity == "REBASE_REQUIRED"
      and st6.research_series(MS, as_of=datetime(2026, 10, 2, 19, 15)).integrity == "OK")
st6.close()

m3 = TMP / "r1_noevent.sqlite3"
r1_db(m3, with_events=False)
st7 = ResearchStore(m3)
q7 = st7.research_series(MS, as_of=datetime(2026, 10, 3, 20, 0))
check("13-10) 저장 기록이 없어 시각을 입증할 수 없는 기존 봉은 UNPROVEN — 시점 조회에서 돌려주지 않고 보류 표시",
      all(sb.time_basis == "UNPROVEN" for sb in st7.load_bars(MS)) and q7.bars == [] and q7.time_proof == "UNPROVEN"
      and q7.unproven_bars == 6 and st7.research_series(MS).time_proof == "UNPROVEN")
st7.close()

m4 = TMP / "r1_overlap.sqlite3"
r1_db(m4, rev2_at="2026-09-30T19:00:00")                     # 기록상 revision 2가 revision 1보다 먼저 — 구간 모순
st8 = ResearchStore(m4)
q8 = st8.research_series(MS, as_of=datetime(2026, 10, 1, 19, 5))
check("13-11) revision 구간이 겹치면(활성 시각이 증가하지 않음) 두 판 모두 UNPROVEN → 그 구간 시점 조회는 보류, "
      "뒤 판(revision 3)은 정상",
      [x["reason"] for x in st8.revisions(MS)] == ["MIGRATED_R1_HISTORY:UNPROVEN", "MIGRATED_R1_HISTORY:UNPROVEN",
                                                   "MIGRATED_R1:EVENT"]
      and q8.bars == [] and q8.time_proof == "UNPROVEN"
      and st8.research_series(MS, as_of=datetime(2026, 10, 3, 20, 0)).time_proof == "OK")
st8.close()

# 같은 초 규칙 (사용자 실측 10/2: 한 페이지짜리 짧은 시계열의 새 봉 199개가 UNPROVEN으로 남음)
m5 = TMP / "r1_samesec.sqlite3"
r1_db(m5)
R2Only(m5).close()                                            # r2 코드 시절
con = sqlite3.connect(m5)
con.execute(f"INSERT INTO bar(series_id, date, open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw, quality,"
            f" run_type, received_at, available_at, first_ready_at, revision) VALUES('{MS}','2026-10-05',101,106,96,101,"
            "1000,1,'','BACKFILL','2026-10-06T08:52:18','2026-10-06T08:52:18','2026-10-06T08:52:18',3)")
con.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
            (MS, "2026-10-06T08:52:17", "EXTEND", json.dumps({"revision": 3})))  # 수신(올림 :18)과 같은 초에 저장(내림 :17)
con.commit()
con.close()
st9 = ResearchStore(m5)
b9 = [sb for sb in st9.load_bars(MS) if sb.raw.date == date(2026, 10, 5)][0]
check("13-12) [실측 재현] 수신(초 올림 08:52:18)과 저장 기록(초 내림 08:52:17)이 같은 초여도 입증 — MIGRATED, "
      "사용 가능 시각 = 수신 시각(저장보다 이르지 않음), 시계열 time_proof OK",
      b9.time_basis == "MIGRATED" and b9.available_at == datetime(2026, 10, 6, 8, 52, 18)
      and st9.research_series(MS).time_proof == "OK")
st9.conn.execute("UPDATE bar SET time_basis='UNPROVEN' WHERE series_id=? AND date='2026-10-05'", (MS,))   # r3 버그 상태
st9.conn.execute(f"INSERT INTO bar(series_id, date, open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw,"
                 f" quality, run_type, received_at, available_at, first_ready_at, time_basis, revision)"
                 f" VALUES('{MS}','2026-10-06',101,106,96,101,1000,1,'','FORWARD','2026-10-06T19:00:00',"
                 "'2026-10-06T19:00:00','2026-10-06T19:00:00','OBSERVED',3)")                  # r3 뒤 새로 저장한 봉
for d, recv in (("2026-10-07", "2026-10-07T08:00:05"), ("2026-10-08", "2026-10-08T08:00:05")):   # 근거 없는 봉
    st9.conn.execute(f"INSERT INTO bar(series_id, date, open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw,"
                     f" quality, run_type, received_at, available_at, first_ready_at, time_basis, revision)"
                     f" VALUES('{MS}',?,101,106,96,101,1000,1,'','BACKFILL',?,?,?,'UNPROVEN',3)", (d, recv, recv, recv))
st9.conn.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
                 (MS, "2026-10-08T08:00:04", "EXTEND", json.dumps({"revision": 99})))   # 다른 revision의 저장 기록
st9.conn.execute("UPDATE meta SET value='r3' WHERE key='schema_version'")
st9.close()
from infra.research.store_inspect import inspect_unproven  # noqa: E402
ins = inspect_unproven(m5)
vv = {r["first"]: r["verdict"] for r in ins["rows"]}
check("13-14) [GPT R2] 읽기 전용 점검: UNPROVEN 봉을 저장 기록과 대조 — 같은 초 저장 근거 있음(PROVABLE_SAME_SECOND) / "
      "기록 없음·다른 revision 기록뿐(NO_EVIDENCE). 점검은 스키마를 올리지 않고 백업도 만들지 않음",
      ins["schema"] == "r3" and ins["unproven_bars"] == 3
      and vv == {"2026-10-05": "PROVABLE_SAME_SECOND", "2026-10-07": "NO_EVIDENCE", "2026-10-08": "NO_EVIDENCE"}
      and ins["verdict_bars"] == {"PROVABLE_SAME_SECOND": 1, "NO_EVIDENCE": 2}
      and not list(TMP.glob("r1_samesec.sqlite3.bak-r3-*")))
st9 = ResearchStore(m5)
bb = {sb.raw.date: sb for sb in st9.load_bars(MS)}
check("13-13) 이미 r3로 바뀐 DB: 열면 r5로 다시 계산 — 근거 있는 같은 초 봉만 MIGRATED, 근거 없는 봉(다른 revision 기록 포함)은 "
      "UNPROVEN 유지(일괄 해제 안 함), r3 뒤 새로 저장한 OBSERVED 봉은 그대로",
      st9.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "r5"
      and bb[date(2026, 10, 5)].time_basis == "MIGRATED" and bb[date(2026, 10, 6)].time_basis == "OBSERVED"
      and bb[date(2026, 10, 6)].available_at == datetime(2026, 10, 6, 19, 0)
      and bb[date(2026, 10, 7)].time_basis == "UNPROVEN" and bb[date(2026, 10, 8)].time_basis == "UNPROVEN"
      and st9.research_series(MS).unproven_bars == 2)
baks = list(TMP.glob("r1_samesec.sqlite3.bak-r3-*"))
with sqlite3.connect(baks[0]) as bk:
    bak_ok = (bk.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "r3"
              and bk.execute("SELECT COUNT(*) FROM bar WHERE time_basis='UNPROVEN'").fetchone()[0] == 3)
check("13-15) [GPT R2] 스키마를 올리기 전에 같은 폴더에 백업(<파일>.bak-r3-<시각>) — 바꾸기 전 상태 그대로",
      len(baks) == 1 and bak_ok and st9.backup_path == str(baks[0]))
st9.close()
st9 = ResearchStore(m5)
check("13-16) 이미 최신이면 다시 백업하지 않음", st9.backup_path is None
      and sorted(p.name.split(".bak-")[1][:2] for p in TMP.glob("r1_samesec.sqlite3.bak-*")) == ["r1", "r2", "r3"])
# ↑ 스키마를 올린 세 번(r1 열기·r2 열기·r3 열기)마다 한 번씩만
st9.close()

# 판 귀속 (GPT B1): 저장 근거는 그 봉과 같은 판의 기록만 — revision 없는 예전 APPEND는 기록 순서·활성 구간으로 귀속
from infra.research.store import attribute_writes, revision_timeline  # noqa: E402

ev_b1 = [("2026-09-30T19:05:00", "INIT", {"revision": 1}),
         ("2026-10-01T19:00:00", "APPEND", {"added": 1}),                 # rev1 구간 안 → rev1
         ("2026-10-02T19:10:00", "APPEND", {"added": 1}),                 # 다음 판 활성과 같은 초(경계) → 모호
         ("2026-10-02T19:10:00", "REBASE", {"revision": 2}),
         ("2026-10-03T19:00:00", "APPEND", {"added": 1}),                 # rev2
         ("2026-10-04T19:00:00", "APPEND", {"revision": 2, "added": 1}),  # 새 기록: revision 명시
         ("2026-10-05T19:00:00", "REBASE", {"bars": 3}),                  # revision 없는 활성 기록 → 이후 모호
         ("2026-10-06T19:00:00", "APPEND", {"added": 1})]
tl_b1 = revision_timeline([{"revision": 1, "reason": "MIGRATED_R1_HISTORY", "activated_at": "2026-09-30T19:00:00"},
                           {"revision": 2, "reason": "FORWARD_MISMATCH", "activated_at": "2026-10-02T19:10:00"}], ev_b1)
aw = [(w.revision, w.source) for w in attribute_writes(ev_b1, tl_b1)]
aw0 = [(w.revision, w.source) for w in attribute_writes([("2026-09-30T19:00:00", "APPEND", {}),
                                                          ("2026-09-30T19:05:00", "INIT", {"revision": 1})], tl_b1)]
check("13-17) [GPT B1] 저장 기록의 판 귀속: revision이 적힌 기록은 그대로, 예전 APPEND는 바로 앞 INIT·REBASE의 판(활성 구간 안), "
      "다음 판 활성과 같은 초·앞 활성 기록 없음·revision 없는 활성 기록 뒤는 모호(근거 아님)",
      aw == [(1, "EXPLICIT"), (1, "ORDER"), (None, "UNATTRIBUTED:AT_OR_AFTER_NEXT_REVISION"), (2, "EXPLICIT"),
             (2, "ORDER"), (2, "EXPLICIT"), (None, "UNATTRIBUTED:ACTIVATION_WITHOUT_REVISION"),
             (None, "UNATTRIBUTED:ACTIVATION_WITHOUT_REVISION")]
      and aw0 == [(None, "UNATTRIBUTED:NO_PRIOR_REVISION"), (1, "EXPLICIT")])

# GPT 재현: 끝난 판(revision 2, 10/2 19:10 종료)의 봉이 자기 판 저장 근거 없이 다음 판(revision 3)의 APPEND(10/3 19:00:01)로
# 입증되던 문제. r2 시절 DB → 점검(읽기 전용) → 열기(r5)
m6 = TMP / "r1_b1.sqlite3"
r1_db(m6)
R2Only(m6).close()
con = sqlite3.connect(m6)
con.execute(f"INSERT INTO bar_history(series_id, revision, date, open_raw, high_raw, low_raw, close_raw, volume,"
            f" trade_value_raw, quality, run_type, received_at, available_at, first_ready_at, superseded_at, reason)"
            f" VALUES('{MS}',2,'2026-10-02',200,205,195,200,1000,1,'','FORWARD','2026-10-02T18:00:00',"
            "'2026-10-02T18:00:00','2026-10-02T18:00:00','2026-10-02T19:10:00','REBASE')")
con.commit()
con.close()
st10 = ResearchStore(m6)
h10 = {(h["revision"], h["date"]): h for h in st10.load_history(MS)}
b10 = {sb.raw.date: sb for sb in st10.load_bars(MS)}
check("13-18) [GPT B1 재현] 다음 판의 revision 없는 APPEND로 끝난 판의 봉을 입증하지 않음 — revision 2의 10/2 봉은 UNPROVEN "
      "(이전: MIGRATED·available_at 10/3 19:00:01). 그 APPEND가 속한 revision 3의 봉(10/3)은 그대로 입증",
      h10[(2, "2026-10-02")]["time_basis"] == "UNPROVEN" and h10[(2, "2026-10-02")]["available_at"] != "2026-10-03T19:00:01"
      and b10[date(2026, 10, 3)].time_basis == "MIGRATED"
      and b10[date(2026, 10, 3)].available_at == datetime(2026, 10, 3, 19, 0, 1)
      and st10.research_series(MS, as_of=datetime(2026, 10, 2, 19, 5)).time_proof == "UNPROVEN")
# 이미 r4로(이전 규칙으로) 보정된 DB: 그 봉이 MIGRATED·10/3 19:00:01로 남아 있던 상태
st10.conn.execute("UPDATE bar_history SET time_basis='MIGRATED', available_at='2026-10-03T19:00:01'"
                  " WHERE series_id=? AND revision=2 AND date='2026-10-02'", (MS,))
st10.conn.execute("UPDATE meta SET value='r4' WHERE key='schema_version'")
st10.close()
h_before = hashlib.sha256(m6.read_bytes()).hexdigest()
ins6 = inspect_unproven(m6)
rc6 = ins6["recheck_migrated"]
check("13-19) [GPT B1] 읽기 전용 점검이 r4에서 잘못 입증된 봉을 미리 보여 줌(보정과 같은 판정 함수) — MIGRATED→UNPROVEN 1봉, "
      "나머지 MIGRATED는 그대로, 귀속 불가 기록 0. DB 해시 그대로·백업 없음",
      ins6["schema"] == "r4" and rc6["MIGRATED->UNPROVEN"] == 1 and rc6["available_at_changed"] == 0
      and rc6["unchanged"] == rc6["bars"] - 1 and rc6["rows"][0]["verdict"] == "NO_EVIDENCE"
      and rc6["rows"][0]["available_at"] == "2026-10-03T19:00:01" and ins6["unattributed_writes"] == {}
      and hashlib.sha256(m6.read_bytes()).hexdigest() == h_before and not list(TMP.glob("r1_b1.sqlite3.bak-r4-*")))
st10 = ResearchStore(m6)
h10 = {(h["revision"], h["date"]): h for h in st10.load_history(MS)}
check("13-20) [GPT B1] r4 DB를 열면 백업 후 r5 재점검 — 잘못 입증된 봉만 UNPROVEN, 바뀐 봉 수를 집계(meta·출력)",
      st10.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "r5"
      and h10[(2, "2026-10-02")]["time_basis"] == "UNPROVEN"
      and st10.upgrade_summary["from"] == "r4" and st10.upgrade_summary["MIGRATED->UNPROVEN"] == 1
      and "available_at_changed" not in str(st10.upgrade_summary)
      and json.loads(st10.conn.execute("SELECT value FROM meta WHERE key='recheck_r5'").fetchone()[0])[
          "MIGRATED->UNPROVEN"] == 1
      and len(list(TMP.glob("r1_b1.sqlite3.bak-r4-*"))) == 1)
st10.close()
st10 = ResearchStore(m6)
check("13-21) 다시 열면 재점검·백업 없음", st10.upgrade_summary is None and st10.backup_path is None)
st10.close()

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
fill_lists(fk)
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
with ResearchStore(cdb) as cst:
    n_jobs = cst.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
fk.calls.clear()
again = rc.main(["--db", cdb, "backfill"], client=cli_client, now=ck, calendar=CAL)
with ResearchStore(cdb) as cst:
    n_jobs2 = cst.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
check("7-2) 작업이 끝난 뒤 그냥 backfill을 다시 실행하면 새 전체 작업을 만들지 않음(호출 0회)",
      again == 0 and n_jobs2 == n_jobs and not fk.calls)
ck.t = datetime(2026, 10, 1, 19, 0)
new_job = rc.main(["--db", cdb, "backfill", "--new", "--limit", "1"], client=cli_client, now=ck, calendar=CAL)
with ResearchStore(cdb) as cst:
    n_jobs3 = cst.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
check("7-3) 전체를 다시 받을 때만 --new로 새 작업", new_job == 0 and n_jobs3 == n_jobs + 1)
made = []
orig_make = rc.make_client
rc.make_client = lambda a: made.append(a.env_file) or cli_client       # 팩토리만 가짜 — 실제 _LazyClient 경로
try:
    ldb = str(TMP / "lazy.sqlite3")
    fk.calls.clear()
    lc = [rc.main(["--db", ldb, "universe"], now=ck, calendar=CAL),
          rc.main(["--db", ldb, "backfill", "--limit", "1", "--no-index"], now=ck, calendar=CAL)]
except Exception as exc:                     # 래퍼가 조회를 넘기지 못하면 여기로 (실패로 기록)
    lc = [f"{type(exc).__name__}: {exc}"]
finally:
    rc.make_client = orig_make
check("7-4) [GPT R1] 클라이언트를 넘기지 않는 일반 CLI 경로(_LazyClient)로 universe·backfill 실제 조회 성공",
      lc == [0, 0] and len(made) >= 1 and any(c["api"] == "ka10099" for c in fk.calls)
      and any(c["api"] == "ka10081" for c in fk.calls))

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
