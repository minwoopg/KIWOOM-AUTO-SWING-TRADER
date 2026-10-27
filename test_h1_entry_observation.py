# -*- coding: utf-8 -*-
"""H1 (GPT 10/27 ③): 주문 미허용 승인 후보의 장초반 가격 관찰 — 주문·신호 키와 분리.

    python test_h1_entry_observation.py

가짜 후보·가짜 호가, 임시 폴더만.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.candidates import ApprovedCandidate, CandidateBook  # noqa: E402
from app.entry_observation import (  # noqa: E402
    OBS_FIRST_OK, OBS_QUOTE, EntryObserver, observation_path, read_day, report_lines, summarize,
)
from app.s1_strategy import S1EntryStrategy  # noqa: E402
from domain.models import AccountBalance  # noqa: E402
from domain.strategy.interface import TickContext  # noqa: E402
from infra.storage.signal_keys import SignalKeyStore  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp())
D = date(2026, 10, 28)


def cand(sym, limit=10_000, stop=9_000.0, cap=10_020.0):
    return ApprovedCandidate(sym, f"N{sym}", f"sig-{sym}-2026-10-27", "2026-10-27", "h", "run", cap, stop, limit, None, "")


def book(*cs, state="READY"):
    return CandidateBook(state, D.isoformat(), "2026-10-27", "h", list(cs))


def ctx(t, *, in_flight=False):
    return TickContext(now=datetime.combine(D, t), phase=MarketPhase.REGULAR, trade_date=D,
                       balance=AccountBalance(10_000_000, 10_000_000, []), positions={}, metas={},
                       blocked_symbols=frozenset(), orders_in_flight=in_flight)


class Quotes:
    def __init__(self, table):
        self.table, self.calls = dict(table), []

    def __call__(self, sym):
        self.calls.append(sym)
        v = self.table[sym]
        if isinstance(v, Exception):
            raise v
        return v


def tick(st, c):
    """실행기 순서: on_tick → (의도 처리) → after_tick(실행기 시계). 시계는 폴링 시각 그대로(시간 경과는 H2 시험)."""
    out = st.on_tick(c)
    st.after_tick(c, lambda: c.now)
    return out


def make(table, books, *, allowed=("005930",), sink=None, name="k"):
    q = Quotes(table)
    obs_rec: list = []
    dec: list = []
    keys = SignalKeyStore(TMP / f"{name}_keys.jsonl")
    it = iter(books) if isinstance(books, list) else None
    obs = EntryObserver(q, sink or obs_rec.append, mode="paper")
    st = S1EntryStrategy(mode="paper", scope="t", candidates=(lambda d, n: next(it)) if it else (lambda d, n: books),
                         quote=q, keys=keys, decisions=dec.append, entry_start=time(9, 5), entry_end=time(9, 30),
                         allowed_symbols=allowed, observer=obs)
    return st, q, obs_rec, dec, keys


# 1. 허용 종목 주문 먼저, 미허용 후보는 관찰만
A, B = cand("005930", 100_000, 95_000.0, 100_200.0), cand("000660", 200_000, 180_000.0, 200_400.0)
st, q, rec, dec, keys = make({"005930": (99_900, 100_000), "000660": (199_000, 200_500)}, book(A, B))
out = tick(st, ctx(time(9, 6)))
r0 = [r for r in rec if r["event"] == OBS_QUOTE]
check("1-1) 허용 005930은 지정가 매수 의도, 미허용 000660은 주문 판단 뒤에 관찰만(조회 순서 005930 → 000660), 관찰 기록 1건 — "
      "매도호가 200,500 > 지정가 200,000 → 미충족(ASK_ABOVE_LIMIT)",
      [i.symbol for i in out] == ["005930"] and q.calls == ["005930", "000660"] and len(r0) == 1
      and r0[0]["symbol"] == "000660" and r0[0]["reasons"] == ["ASK_ABOVE_LIMIT"] and r0[0]["observe_only"]
      and abs(r0[0]["stop_dist_pct"] - (199_000 - 180_000) / 199_000 * 100) < 1e-3)
check("1-2) 신호 키는 주문한 005930만 — 관찰한 000660 신호는 키에 없음(허용 종목이 바뀌어도 '이미 쓴 신호'로 막히지 않음), "
      "판단 기록에 000660 NOT_ALLOWED",
      all("000660" not in k for k in keys.keys()) and any("005930" in k for k in keys.keys())
      and any(d["topic"] == "000660" and d["event"] == "NOT_ALLOWED" for d in dec))

# 2. 보유·미해결 주문이 있어도 관찰 계속, 간격 제한, 최초 충족
st, q, rec, dec, keys = make({"005930": (99_900, 100_000), "000660": (199_000, 199_500)}, book(A, B), name="k2")
tick(st, ctx(time(9, 6), in_flight=True))
tick(st, ctx(time(9, 6, 30), in_flight=True))
tick(st, ctx(time(9, 7, 1), in_flight=True))
q.table["000660"] = (199_000, 200_500)
tick(st, ctx(time(9, 8, 2), in_flight=True))
q.table["000660"] = (199_200, 199_800)
tick(st, ctx(time(9, 9, 3), in_flight=True))
quotes = [r for r in rec if r["event"] == OBS_QUOTE]
firsts = [r for r in rec if r["event"] == OBS_FIRST_OK]
check("2-1) 미해결 주문(최대 보유)이라 주문 의도 없음 — 관찰은 계속, 30초 뒤 폴링은 건너뜀(종목당 55초 간격) → 4회 관찰, 주문 쪽 "
      "호가 조회 없음",
      len(quotes) == 4 and "005930" not in q.calls and not keys.keys()
      and any(d["event"] == "MAX_POSITION" for d in dec))
check("2-2) 최초 조건 충족(09:06)은 한 번만 OBS_FIRST_OK, 이후 미충족·재충족은 관찰 줄로만",
      len(firsts) == 1 and firsts[0]["at"].endswith("09:06:00") and [r["ok"] for r in quotes] == [True, True, False, True])

# 3. 실패해도 매매에 영향 없음
st, q, rec, dec, keys = make({"005930": (99_900, 100_000), "000660": RuntimeError("timeout")}, book(A, B), name="k3")
out = tick(st, ctx(time(9, 6)))
check("3-1) 관찰 조회 예외 → QUOTE_FAILED 기록, 주문 의도는 그대로",
      [i.symbol for i in out] == ["005930"] and rec[0]["reasons"] == ["QUOTE_FAILED"] and "timeout" in rec[0]["error"])


def boom(r):
    raise OSError("disk full")


st, q, rec, dec, keys = make({"005930": (99_900, 100_000), "000660": (199_000, 199_500)}, book(A, B), sink=boom, name="k4")
out = tick(st, ctx(time(9, 6)))
check("3-2) 관찰 기록 파일 쓰기 실패도 삼킴 — 주문 의도 그대로, OBSERVE_FAILED 없음",
      [i.symbol for i in out] == ["005930"] and not any(d["event"] == "OBSERVE_FAILED" for d in dec))

# 4. 관찰하지 않는 경우
st, q, rec, dec, keys = make({"000660": (199_000, 199_500)}, book(B), name="k5")
tick(st, ctx(time(9, 31)))
tick(st, ctx(time(9, 4)))
st2, q2, rec2, _, _ = make({"000660": (199_000, 199_500)}, book(B, state="ERROR"), name="k6")
tick(st2, ctx(time(9, 6)))
st3, q3, rec3, _, _ = make({"000660": (199_000, 199_500)}, book(B), allowed=(), name="k7")
st3.mode = "shadow"
tick(st3, ctx(time(9, 6)))
check("4-1) 진입 시간(09:05~09:30) 밖, 후보 조회 ERROR, 허용 종목 제한 없음(그림자 — 모든 후보를 주문 판단) → 관찰 없음",
      not rec and not rec2 and not [r for r in rec3 if r.get("observe_only")])

# 5. 한 폴링 최대 5종목, 오래 안 본 종목부터
many = [cand(f"00{i:04d}") for i in range(1, 8)]
st, q, rec, dec, keys = make({c.symbol: (9_990, 10_000) for c in many}, book(*many), name="k8")
tick(st, ctx(time(9, 6)))
first = list(q.calls)
tick(st, ctx(time(9, 7)))
check("5-1) 미허용 후보 7개 → 첫 폴링 5개(코드순), 다음 폴링은 아직 안 본 2개부터 채워 5개",
      first == [c.symbol for c in many[:5]] and q.calls[5:7] == [c.symbol for c in many[5:]] and len(q.calls) == 10)

# 6. 요약·보고서·번들
path = TMP / "state" / "entry_observation.jsonl"
path.parent.mkdir(parents=True)
st, q, rec, dec, keys = make({"005930": (99_900, 100_000), "000660": (199_000, 200_500)}, book(A, B), name="k9",
                             sink=lambda r: path.open("a", encoding="utf-8").write(json.dumps(r, ensure_ascii=False) + "\n"))
tick(st, ctx(time(9, 6)))
q.table["000660"] = (198_500, 199_500)
tick(st, ctx(time(9, 7)))
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"at": "2026-10-27T09:06:00", "trade_date": "2026-10-27", "event": OBS_QUOTE, "symbol": "X",
                         "signal_id": "old", "price": 1, "ok": False, "reasons": []}) + "\n")
sm = summarize(read_day(path, D))
lines = report_lines(path, D)
check("6-1) 요약: 그날 기록만(전날 줄 제외), 신호별 관찰 2회·충족 1회·최초 충족 09:07 가격 198,500·가격 범위·미충족 사유",
      len(sm) == 1 and sm[0]["n"] == 2 and sm[0]["ok_n"] == 1 and sm[0]["first_ok"]["price"] == 198_500
      and sm[0]["low"] == 198_500 and sm[0]["high"] == 199_000 and sm[0]["reasons"] == {"ASK_ABOVE_LIMIT": 1}
      and any("최초 조건 충족 09:07" in ln for ln in lines) and "관찰만" in lines[1])
from app.reports import export_bundle  # noqa: E402
from testing_helpers import build_minimal_settings  # noqa: E402
settings = build_minimal_settings(str(TMP / "bs"))
op = observation_path(settings.storage.state_file)
op.parent.mkdir(parents=True, exist_ok=True)
shutil.copy(path, op)
zp = export_bundle(settings, D, root=TMP / "broot", out_dir=TMP / "bx")
zf = zipfile.ZipFile(zp)
nm = next(n for n in zf.namelist() if n.endswith("entry_observation.jsonl"))
body = zf.read(nm).decode("utf-8")
check("6-2) 번들에 entry_observation.jsonl(그날 줄만)", body.count("000660") >= 2 and '"signal_id": "old"' not in body)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
