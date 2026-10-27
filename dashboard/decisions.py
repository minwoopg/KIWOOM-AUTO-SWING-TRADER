"""관리 창 UI 1차 (GPT 10/27) — "지금 무엇을 확인하고, 왜 매매하지 않았는지" 자료 계층. **읽기 전용.**

- 저장된 판단만 보여줌: 관리자 스캔 결과(watch_s1 s1_eval — 판정 당시 저장된 조건별 결과·값), 개장 확인 후보 승인
  (`app.candidates.CandidateReader` — 실행기와 같은 읽기 전용 판독), 실행기 판단 기록(strategy_decisions.jsonl),
  장초반 관찰(entry_observation.jsonl), 원장. 화면을 열 때 지표를 다시 계산하지 않음 — 저장되지 않은 근거는 "기록 없음".
- 단계별 건수는 **같은 기준**(오늘 거래일 · 그 신호일(직전 거래일)의 마지막 완료 스캔 · 현재 설정의 주문 허용 종목)으로만
  셈. 다른 날 스캔·관찰을 섞지 않음.
- 세 상태를 구분: 정상 처리(후보 0건 포함) · 아직 처리하지 않음 · 처리 실패·확인 불가.
- SQLite는 mode=ro, 키움 API 호출 없음, 파일 쓰기 없음.
"""
from __future__ import annotations

import json
import zlib
from datetime import date, datetime
from pathlib import Path

from dashboard.data import MISSING, OK, STATE_LABEL, UNREADABLE, Paths, Section, read_jsonl, ro_query

CHECK_LABEL = {
    "HISTORY": "이력(160일)", "C_GT_MA120": "종가 > 120일선", "MA60_RISING": "60일선 상승", "RS60_POS": "60일 상대강도 > 0",
    "PULLBACK": "눌림(2~10일)", "CLOSE_GT_PREV_HIGH": "전일 고가 돌파", "CLOSE_GE_MA20": "종가 ≥ 20일선",
    "ATR_POS": "변동폭(ATR) 있음", "NOT_EXTENDED": "과열 아님", "SECURITY_TYPE": "보통주", "RISK_STATUS": "위험 표시 없음",
    "LIQUIDITY_TV20": "20일 거래대금", "MARKET_REGIME": "시장 조건", "STOP_VALID": "손절 기준 유효",
}
RESULT_LABEL = {"PASS": "충족", "FAIL": "미충족", "UNKNOWN": "판정 불가"}
# 운영 상태 3구분
DONE, NOT_YET, FAILED = "정상 처리", "아직 처리하지 않음", "처리 실패·확인 불가"


def check_label(name: str) -> str:
    return CHECK_LABEL.get(name, name)


# ── 저장된 스캔 ─────────────────────────────────────────────

def scan_run_for(p: Paths, signal_date: str) -> Section:
    """그 신호일의 마지막 완료 스캔(20:30 완성 기준이 보통 마지막)."""
    s = ro_query(p.s1_db, "SELECT run_id,signal_date,scan_at,after_close_min,contract_hash,counts_json FROM scan_run "
                          "WHERE status='COMPLETE' AND signal_date=? ORDER BY scan_at DESC LIMIT 1", (signal_date,))
    if s.state != OK:
        return s
    return Section(OK, s.data[0] if s.data else None, source=s.source, as_of=(s.data[0]["scan_at"] if s.data else ""))


def scan_evals(p: Paths, run_id: str) -> Section:
    """s1_eval 행 + 판정 본문(result_z, 저장 당시 값). 본문을 풀 수 없으면 result=None(요약 열만)."""
    ev = ro_query(p.s1_db, "SELECT symbol,name,market,signal_date,eligible_signal,pattern_pass,eligibility_pass,market_pass,"
                           "stop_valid,data_status,index_status,snapshot_status,no_trades_hold,final,actionable,codec,"
                           "result_z FROM s1_eval WHERE run_id=? ORDER BY symbol", (run_id,))
    if ev.state != OK:
        return ev
    cz = ro_query(p.s1_db, "SELECT name, zdict FROM codec")
    zd = {r["name"]: bytes(r["zdict"]) for r in (cz.data or [])} if cz.state == OK else {}
    out, bad = {}, 0
    for r in ev.data:
        blob, codec = r.pop("result_z"), r.pop("codec")
        res = None
        if blob is not None and codec in zd:
            try:
                do = zlib.decompressobj(zdict=zd[codec])
                res = json.loads((do.decompress(bytes(blob)) + do.flush()).decode("utf-8"))
            except (zlib.error, ValueError, UnicodeDecodeError):
                bad += 1
        r["result"] = res
        out[r["symbol"]] = r
    s = Section(OK, out, source="watch_s1.sqlite3 s1_eval")
    if bad:
        s.notes.append(f"판정 본문을 풀 수 없는 행 {bad}개 — 요약 열만 표시")
    return s


# S1 점수(사용자 요청 10/27): 신호를 가르는 조건 9개의 충족 비율 × 10. 자격 조건(이력·보통주·위험 표시·거래대금·ATR)은
# 점수에 넣지 않고 "자격 미달"로 따로 표시 — 거의 늘 충족이라 점수를 부풀리기 때문. **수익 예측·매수 추천 점수가 아님**:
# S1은 모든 조건을 만족해야(10.0) 신호이고, 9.0이어도 매수하지 않음.
SCORE_CHECKS = ("C_GT_MA120", "MA60_RISING", "RS60_POS", "PULLBACK", "CLOSE_GT_PREV_HIGH", "CLOSE_GE_MA20",
                "NOT_EXTENDED", "MARKET_REGIME", "STOP_VALID")
GATE_CHECKS = ("HISTORY", "ATR_POS", "SECURITY_TYPE", "RISK_STATUS", "LIQUIDITY_TV20")


def s1_score(e: dict | None) -> dict | None:
    """저장된 조건별 결과로 점수 — 판정 본문이 없으면 None(점수 없음). UNKNOWN은 미충족으로 셈."""
    res = (e or {}).get("result") or {}
    checks = {c["name"]: c.get("result") for c in res.get("checks") or []}
    if not checks:
        return None
    met = [n for n in SCORE_CHECKS if checks.get(n) == "PASS"]
    miss = [n for n in SCORE_CHECKS if checks.get(n) != "PASS"]
    gate_bad = [n for n in GATE_CHECKS if n in checks and checks[n] != "PASS"]
    return {"score": round(10 * len(met) / len(SCORE_CHECKS), 1), "met": len(met), "total": len(SCORE_CHECKS),
            "missing": miss, "gate_bad": gate_bad}


def score_text(sc: dict | None) -> str:
    if sc is None:
        return "-"
    t = f"{sc['score']:.1f}/10 ({sc['total']}개 중 {sc['met']})"
    if sc["gate_bad"]:
        t += " · 자격 미달"
    return t


def fail_summary(e: dict | None) -> str:
    """S1 판단 한 줄: 통과 / 미충족 조건 / 판정 불가 사유 — 저장된 결과 그대로."""
    if not e:
        return "스캔 기록 없음"
    sig = e["eligible_signal"]
    if sig == "PASS":
        return "통과"
    res = e.get("result") or {}
    checks = res.get("checks") or []
    fails = [check_label(c["name"]) for c in checks if c.get("result") == "FAIL"]
    unk = [check_label(c["name"]) for c in checks if c.get("result") == "UNKNOWN"]
    if e.get("data_status") not in (None, "", "OK"):
        return f"판정 안 함 — 데이터 {e['data_status']}"
    if fails:
        return "탈락: " + ", ".join(fails[:4]) + (f" 외 {len(fails) - 4}" if len(fails) > 4 else "")
    if unk:
        return "판정 불가: " + ", ".join(unk[:3])
    groups = [g for g, k in (("패턴", "pattern_pass"), ("자격", "eligibility_pass"), ("시장", "market_pass"),
                             ("손절 기준", "stop_valid")) if e.get(k) != "PASS"]
    return f"{RESULT_LABEL.get(sig, sig)}: " + (", ".join(groups) or "세부 기록 없음")


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def eval_detail(e: dict | None) -> list[str]:
    if not e:
        return ["S1 판정: 이 신호일 스캔에 기록 없음"]
    res = e.get("result")
    L = [f"S1 판정: {fail_summary(e)} (신호일 {e['signal_date']})",
         f"묶음: 패턴 {e['pattern_pass']} · 자격 {e['eligibility_pass']} · 시장 {e['market_pass']} · 손절 기준 {e['stop_valid']} · "
         f"데이터 {e['data_status']}"]
    if not res:
        L.append("조건별 근거 값: 기록 없음(판정 본문 없음)")
        return L
    L.append("조건별(판정 당시 저장 값):")
    for c in res.get("checks") or []:
        mark = {"PASS": "○", "FAIL": "×", "UNKNOWN": "?"}.get(c.get("result"), "·")
        val = c.get("value")
        L.append(f"  {mark} {check_label(c['name'])}: {RESULT_LABEL.get(c.get('result'), c.get('result'))}"
                 + (f" · 값 {_fmt(val)}" if val not in (None, "") else "")
                 + (f" · {c['detail']}" if c.get("detail") else ""))
    m = res.get("market") or {}
    if m:
        L.append(f"시장 판정: {m.get('state', '-')} ({m.get('index_id') or m.get('source_id') or ''})")
    lv = res.get("levels") or {}
    if lv:
        L.append(f"가격 기준: 손절 기준 {_fmt(lv.get('stop_ref'))} · 진입 상한 {_fmt(lv.get('entry_cap'))} · "
                 f"상한 기준 손절 거리 {_fmt((lv.get('risk_ratio_at_cap') or 0) * 100 if lv.get('risk_ratio_at_cap') else None)}%")
    pb = res.get("pullback") or {}
    if pb.get("pivot_date"):
        L.append(f"눌림: 고점 {pb.get('pivot_date')} {_fmt(pb.get('pivot_high'))} → 저점 {_fmt(pb.get('pullback_low'))} "
                 f"({pb.get('pullback_len')}일)")
    return L


# ── 오늘 기준 ───────────────────────────────────────────────

def _calendar():
    from utils.trading_calendar import TradingCalendar
    return TradingCalendar.load()


def signal_date_for(day: date, calendar=None) -> str | None:
    try:
        cal = calendar or _calendar()
        return cal.previous_trading_day(day).isoformat()
    except Exception:                                      # noqa: BLE001 — 달력 범위 밖
        return None


def candidate_book(p: Paths, day: date, now: datetime, calendar=None):
    """오늘 승인 후보 — 실행기와 같은 읽기 전용 판독(CandidateReader). 실패는 book.state=ERROR로."""
    from app.candidates import CandidateReader
    try:
        return CandidateReader(p.daemon_db.parent, calendar or _calendar()).read(day, now)
    except Exception as exc:                               # noqa: BLE001
        from app.candidates import CandidateBook
        return CandidateBook("ERROR", day.isoformat(), note=f"{type(exc).__name__}: {exc}")


def _day_records(path: Path, day: date, key: str = "at") -> list[dict]:
    s = read_jsonl(path)
    if s.state != OK:
        return []
    d = day.isoformat()
    return [r for r in s.data if isinstance(r, dict) and str(r.get(key, "")).startswith(d)]


def _ledger_today(p: Paths, day: date) -> tuple[dict, list]:
    from dashboard.data import ledger_events
    le = ledger_events(p)
    if le.state != OK:
        return {}, []
    from domain.service.lot_ledger import apply_events
    pos = {s: x for s, x in apply_events(le.data).positions().items() if x.quantity > 0}
    today = [e for e in le.data if e.trade_date == day]
    return pos, today


def _task(p: Paths, kind: str, day: str) -> dict | None:
    s = ro_query(p.daemon_db, "SELECT kind,trading_day,status,attempts,failures,finished_at,error,detail_json FROM task "
                              "WHERE kind=? AND trading_day=? LIMIT 1", (kind, day))
    return s.data[0] if s.state == OK and s.data else None


def today_summary(p: Paths, settings, day: date, now: datetime, runner: Section, *, calendar=None) -> Section:
    """오늘 매수가 없는(있는) 이유 — 같은 기준의 단계별 건수 + 한 줄 결론 + 상태(정상·아직·실패)."""
    allowed = tuple(getattr(getattr(settings, "swing_strategy", None), "allowed_symbols", ()) or ())
    sig = signal_date_for(day, calendar)
    steps: list[dict] = []
    state, head = DONE, ""
    if sig is None:
        return Section(OK, {"state": FAILED, "headline": f"{day} 직전 거래일을 달력에서 찾지 못함 — 확인 불가", "steps": [],
                            "basis": {}}, source="krx_calendar")
    run = scan_run_for(p, sig)
    prep = _task(p, "CLOSE_PREP", sig)
    open_t = _task(p, "OPEN_CHECK", day.isoformat())
    evs: dict = {}
    if run.state != OK:
        state, head = FAILED, f"관리자 스캔 기록 {STATE_LABEL[run.state]} — 확인 불가"
    elif run.data is None:
        if prep is None:
            state, head = NOT_YET, f"신호일 {sig} 스캔 기록 없음 — 마감 준비(20:30)가 아직 돌지 않음(관리자·PC가 꺼져 있었는지 확인)"
        elif prep["status"] == "COMPLETE":
            state, head = FAILED, f"신호일 {sig} 마감 준비는 완료인데 완료 스캔 기록이 없음 — 확인 불가"
        else:
            state = FAILED if prep["status"] in ("FAILED", "ABORTED", "MISSED") else NOT_YET
            head = f"신호일 {sig} 마감 준비 {prep['status']} — 스캔 미완료"
    else:
        es = scan_evals(p, run.data["run_id"])
        if es.state != OK:
            state, head = FAILED, f"스캔 판정 {STATE_LABEL[es.state]}"
        else:
            evs = es.data
    n_all = len(evs)
    n_ready = sum(1 for e in evs.values() if e["data_status"] == "OK")
    passed = sorted(s for s, e in evs.items() if e["eligible_signal"] == "PASS")
    book = candidate_book(p, day, now, calendar)
    approved = sorted(a.symbol for a in book.approved) if book.state == "READY" else []
    allow_ok = [s for s in approved if not allowed or s in allowed]
    allow_no = [s for s in approved if allowed and s not in allowed]
    dec = _day_records(p.state_file.with_name("strategy_decisions.jsonl"), day)
    pos, fills = _ledger_today(p, day)
    buys = [e for e in fills if e.kind == "BUY"]
    rc = (runner.data or {}).get("counters") or {} if runner.state == OK else {}
    rn_today = runner.state == OK and (runner.data or {}).get("trade_date") == day.isoformat()
    basis = {"trade_date": day.isoformat(), "signal_date": sig,
             "scan": (f"{run.data['run_id']} · {run.data['scan_at']} (마감 {run.data['after_close_min']}분 뒤)"
                      if run.state == OK and run.data else "없음"),
             "allowed": list(allowed) or ["제한 없음"]}
    raw_steps = [
        {"단계": "분석 대상(관심·S1)", "건수": n_all if run.data else "-", "기준": f"신호일 {sig} 스캔", "출처": "watch_s1 s1_eval"},
        {"단계": "데이터 준비", "건수": n_ready if run.data else "-", "기준": "데이터 상태 OK", "출처": "watch_s1 s1_eval"},
        {"단계": "S1 통과", "건수": len(passed) if run.data else "-", "기준": "최종 판정 PASS",
         "종목": ", ".join(passed), "출처": "watch_s1 s1_eval"},
        {"단계": "개장 확인 승인", "건수": len(approved) if book.state == "READY" else "-",
         "기준": f"오늘 {day} 09:05 개장 확인·08:40 입력 재확인", "종목": ", ".join(approved),
         "출처": f"관리자 후보 판독 {book.state}" + (f" — {book.note}" if book.note else "")},
        {"단계": "주문 허용", "건수": len(allow_ok) if book.state == "READY" else "-",
         "기준": "설정 allowed_symbols " + ("/".join(allowed) if allowed else "(제한 없음)"),
         "종목": ", ".join(allow_ok) + (f" (미허용: {', '.join(allow_no)} — 가격만 관찰)" if allow_no else ""),
         "출처": "config/settings.yaml"},
        {"단계": "주문 전송·접수", "건수": (f"{rc.get('orders_sent', 0)}·{rc.get('orders_accepted', 0)}" if rn_today else "-"),
         "기준": "오늘 실행기(매수·매도 합)", "출처": "runner_status.json" if rn_today else "오늘 실행기 기록 없음"},
        {"단계": "매수 체결", "건수": len(buys), "기준": f"원장 {day}", "종목": ", ".join(sorted({e.symbol for e in buys})),
         "출처": "fill_ledger.jsonl"},
    ]
    steps = [{"단계": r["단계"], "건수": r["건수"], "종목": r.get("종목", ""), "기준": r["기준"], "출처": r["출처"]}
             for r in raw_steps]
    if not head:
        oc = None
        if open_t is not None:
            oc = open_t["status"]
        if n_all and not passed:
            head = f"오늘 S1 매수 대상 없음 — 신호일 {sig} S1 통과 종목 없음({n_all}종목 분석, 데이터 준비 {n_ready})"
        elif oc is None and now.time() < datetime.strptime("09:05", "%H:%M").time():
            state, head = NOT_YET, f"S1 통과 {len(passed)}종목 — 개장 확인(09:05) 전"
        elif book.state != "READY":
            state = NOT_YET if book.state == "WAITING" else FAILED
            head = f"S1 통과 {len(passed)}종목 — 후보 승인 {book.state}" + (f": {book.note}" if book.note else "")
        elif not approved:
            head = (f"S1 통과 {len(passed)}종목이 개장 확인에서 승인되지 않음"
                    + (f" — {_rejected_text(book)}" if book.rejected else ""))
        elif not allow_ok:
            head = f"승인 {len(approved)}종목({', '.join(approved)})이 주문 허용 목록에 없어 가격만 관찰 중"
        elif buys:
            head = f"매수 체결 {len(buys)}건 — {', '.join(sorted({e.symbol for e in buys}))}"
        else:
            last = _last_reason(dec, allow_ok)
            head = f"주문 허용 후보 {', '.join(allow_ok)} — " + (last or "실행기 판단 기록 없음(실행기가 켜져 있는지 확인)")
        if buys and "매수 체결" not in head:
            head += f" · 오늘 매수 체결 {len(buys)}건({', '.join(sorted({e.symbol for e in buys}))}, 원장)"
        if pos:
            head += f" · 보유 {', '.join(sorted(pos))}(손절·시간 청산 감시)"
    return Section(OK, {"state": state, "headline": head, "steps": steps, "basis": basis},
                   source="watch_s1 · daemon(후보 판독) · strategy_decisions · fill_ledger · runner_status",
                   as_of=run.data["scan_at"] if run.state == OK and run.data else "")


def _rejected_text(book) -> str:
    out = []
    for s, rs in sorted(book.rejected.items())[:3]:
        out.append(f"{s}: {', '.join(rs[:2])}")
    return " / ".join(out)


EVENT_LABEL = {"WAIT_PRICE": "가격 조건 대기", "INTENT": "매수 의도", "NOT_ALLOWED": "주문 미허용", "MAX_POSITION": "최대 보유",
               "SKIP_BLOCKED": "차단 종목", "QUOTE_FAILED": "시세 조회 실패", "RESULT_BUY": "매수 처리 결과"}


def _last_reason(dec: list[dict], syms: list[str]) -> str:
    for r in reversed(dec):
        if r.get("topic") in syms or r.get("topic") == "capacity":
            ev = r.get("event", "")
            extra = ""
            if ev == "WAIT_PRICE":
                extra = " (" + ", ".join(r.get("reasons") or []) + ")"
            elif ev == "RESULT_BUY":
                extra = f" ({r.get('status')})"
            return f"{r.get('at', '')[11:16]} {EVENT_LABEL.get(ev, ev)}{extra}"
    return ""


# ── 종목별 판단 ─────────────────────────────────────────────

def _purposes(p: Paths) -> dict[str, list[str]]:
    cfg = p.root / "config" / "watchlist.yaml"
    if not cfg.exists():
        return {}
    try:
        from domain.watchlist.config import parse_text
        raw, err = parse_text(cfg.read_text(encoding="utf-8"))
    except Exception:                                      # noqa: BLE001
        return {}
    if err or not isinstance(raw, dict):
        return {}
    out = {}
    for it in raw.get("symbols") or []:
        it = it or {}
        code = str(it.get("code", "")).strip()
        i = it.get("interest") or {}
        pur = []
        if i.get("enabled"):
            pur.append("신규 진입" if i.get("s1_analysis") else "관심(가격대)")
        if it.get("holding"):
            pur.append("수동 보유 감시")
        out[code] = pur
    return out


def symbol_view(p: Paths, settings, day: date, now: datetime, *, calendar=None) -> Section:
    """종목별 현재 상태 표 + 선택 종목 상세(저장된 판단). data = {"rows": [...], "details": {code: [줄...]}}."""
    allowed = tuple(getattr(getattr(settings, "swing_strategy", None), "allowed_symbols", ()) or ())
    rd = ro_query(p.watch_db, "SELECT code,status,reason,analysis_status,analysis_reason,checked_at FROM readiness "
                              "WHERE kind='STOCK' OR target LIKE 'STOCK:%' ORDER BY code")
    if rd.state != OK:
        return rd
    sig = signal_date_for(day, calendar)
    run = scan_run_for(p, sig) if sig else Section(MISSING)
    evs = {}
    note = []
    if run.state == OK and run.data:
        es = scan_evals(p, run.data["run_id"])
        evs = es.data if es.state == OK else {}
        note += es.notes
        note.append(f"S1 판정 기준: 신호일 {sig} 스캔 {run.data['scan_at']} ({run.data['run_id']}) — 판정 당시 저장 값")
    else:
        note.append(f"신호일 {sig} 완료 스캔 없음 — S1 판단 '스캔 기록 없음'")
    book = candidate_book(p, day, now, calendar)
    appr = {a.symbol: a for a in book.approved} if book.state == "READY" else {}
    rej = book.rejected if book.state == "READY" else {}
    note.append(f"오늘 후보 판독: {book.state}" + (f" — {book.note}" if book.note else ""))
    dec = _day_records(p.state_file.with_name("strategy_decisions.jsonl"), day)
    by_sym: dict[str, list[dict]] = {}
    for r in dec:
        by_sym.setdefault(str(r.get("topic", "")).replace("exit:", ""), []).append(r)
    from app.entry_observation import observation_path, read_day, summarize
    obs = {s["symbol"]: s for s in summarize(read_day(observation_path(p.state_file), day))}
    pos, _ = _ledger_today(p, day)
    purposes = _purposes(p)
    risk = ro_query(p.watch_db, "SELECT code,status,flags_json FROM symbol_risk")
    risk_map = {r["code"]: r for r in (risk.data or [])} if risk.state == OK else {}
    rows, details = [], {}
    for r in rd.data:
        code = r["code"]
        e = evs.get(code)
        held = code in pos
        pur = list(purposes.get(code, []))
        if held:
            pur.append("보유 관리")
        data_txt = "준비됨" if r["status"] == "READY" and r["analysis_status"] == "READY" else (
            f"분석 보류 {r['analysis_reason'] or ''}".strip() if r["status"] == "READY" else f"가격 {r['status']} {r['reason'] or ''}".strip())
        s1 = fail_summary(e)
        order_ok = "허용" if (not allowed or code in allowed) else "미허용"
        # 현재 상태 — 저장된 기록으로만
        last = (by_sym.get(code) or [None])[-1]
        if held:
            cur = "보유 — 손절·시간 청산 감시"
        elif code in appr:
            if order_ok == "미허용":
                o = obs.get(code)
                cur = "승인 · 주문 미허용 — 가격만 관찰" + (f" (관찰 {o['n']}회, 충족 {o['ok_n']})" if o else " (관찰 기록 없음)")
            else:
                cur = "승인 · 주문 대상 — " + (f"{last['at'][11:16]} {EVENT_LABEL.get(last['event'], last['event'])}"
                                            if last else "실행기 판단 기록 없음")
        elif code in rej:
            cur = "승인 안 됨 — " + ", ".join(rej[code][:2])
        elif r["status"] != "READY" or r["analysis_status"] != "READY":
            cur = "판정 안 함 — 데이터 확인 필요"
        elif e is None:
            cur = "스캔 기록 없음"
        else:
            cur = "다음 스캔 대기"
        rk = risk_map.get(code)
        sc = s1_score(e)
        rows.append({"종목": (e or {}).get("name", ""), "코드": code, "감시 목적": " · ".join(pur) or "-",
                     "데이터": data_txt, "S1 판단": s1, "주문 자격": order_ok, "현재 상태": cur,
                     "S1 점수": score_text(sc), "마지막 확인": r["checked_at"]})
        D = [f"{(e or {}).get('name', '')} {code} — 감시 목적 {' · '.join(pur) or '-'} · 주문 자격 {order_ok}",
             f"데이터: 가격 {r['status']} {r['reason'] or ''} · 분석 {r['analysis_status']} {r['analysis_reason'] or ''}"
             f" (판정 {r['checked_at']})"]
        if rk and rk["status"] != "OK":
            D.append(f"위험 표시: {rk['status']} {rk['flags_json']}")
        if sc is not None:
            D.append(f"S1 점수: {score_text(sc)} — 신호를 가르는 조건 {sc['total']}개 중 충족 비율(수익 예측·매수 추천 아님, "
                     "10.0일 때만 신호)"
                     + (f" · 남은 조건: {', '.join(check_label(n) for n in sc['missing'])}" if sc["missing"] else "")
                     + (f" · 자격 미달: {', '.join(check_label(n) for n in sc['gate_bad'])}" if sc["gate_bad"] else ""))
        else:
            D.append("S1 점수: 기록 없음(판정 본문 없음)")
        D += eval_detail(e)
        if code in appr:
            a = appr[code]
            D.append(f"오늘 승인: 지정가 {a.limit_price:,} · 손절 기준 {a.stop_ref:,.0f} · 진입 상한 {a.entry_cap:,.0f}"
                     f" · 개장 관찰 {a.observed_outcome} {a.observed_price or ''}")
        elif code in rej:
            D.append("오늘 승인 안 됨: " + ", ".join(rej[code]))
        if code in obs:
            o = obs[code]
            fo = o["first_ok"]
            D.append(f"장초반 관찰: {o['n']}회 {o['first_at'][11:16]}~{o['last_at'][11:16]} · 충족 {o['ok_n']} · "
                     + (f"최초 충족 {fo['at'][11:16]} {fo['price']:,}" if fo else "조건 충족 없음")
                     + (f" · 미충족 {', '.join(f'{k} {v}' for k, v in o['reasons'].items())}" if o["reasons"] else ""))
        evs_s = by_sym.get(code) or []
        if evs_s:
            D.append("오늘 실행기 판단(최근 5):")
            for x in evs_s[-5:]:
                extra = x.get("reasons") or x.get("status") or ""
                D.append(f"  {x.get('at', '')[11:19]} {EVENT_LABEL.get(x.get('event'), x.get('event'))} {extra}")
        if held:
            q = pos[code]
            D.append(f"보유: 원장 {q.quantity}주 · 평균 {q.avg_price:,.0f} · 첫 진입 {q.first_entry_date}")
        details[code] = D
    s = Section(OK, {"rows": rows, "details": details},
                source="watch readiness · watch_s1 s1_eval · 관리자 후보 판독 · strategy_decisions · entry_observation · 원장",
                as_of=rd.data[0]["checked_at"] if rd.data else "")
    s.notes += note
    return s
