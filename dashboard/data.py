"""웹 관리 화면 자료 계층 — **읽기 전용** (M3 대시보드).

모든 SQLite는 `mode=ro`로만 열고(만들기·스키마 올리기·쓰기 없음), 파일은 읽기만 합니다. 키움 API 호출 없음.
쓰기는 딱 하나: 신규 매수 중단 요청(`infra.storage.runner_status.request_buy_halt` — 실행기가 처리하는 명령 계약).

각 구역은 `Section(state, data, error, source, as_of)`로 돌려줍니다. state:
OK · MISSING(파일 없음) · UNREADABLE(읽기 실패) · LOCKED(잠금·일시 충돌) · NOT_IMPLEMENTED(아직 없는 기능).
여러 DB·파일에서 읽은 값은 같은 순간의 상태가 아님 — 구역마다 기록 시각·실행 ID를 함께 표시합니다.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

OK, MISSING, UNREADABLE, LOCKED, NOT_IMPLEMENTED = "OK", "MISSING", "UNREADABLE", "LOCKED", "NOT_IMPLEMENTED"
STATE_LABEL = {OK: "정상", MISSING: "파일 없음", UNREADABLE: "읽기 실패", LOCKED: "잠금·일시 충돌(잠시 뒤 다시)",
               NOT_IMPLEMENTED: "미구현"}
STALE_SEC = 180          # 관리자 poll 60초 × 3, 실행기 폴링 60초 × 3


@dataclass
class Section:
    state: str
    data: object = None
    error: str = ""
    source: str = ""
    as_of: str = ""
    notes: list = field(default_factory=list)


@dataclass(frozen=True)
class Paths:
    root: Path
    daemon_db: Path
    watch_db: Path
    s1_db: Path
    runner_status: Path
    state_file: Path
    fill_ledger: Path
    paper_orders: Path
    journal: Path
    commands_dir: Path
    reports_dir: Path
    app_log: Path
    daemon_log: Path
    trade_log: Path


def paths_from_settings(settings, root: Path) -> Paths:
    def r(p) -> Path:
        p = Path(p)
        return p if p.is_absolute() else root / p
    st = r(settings.storage.state_file)
    return Paths(root=root, daemon_db=root / "data/watch/daemon.sqlite3", watch_db=root / "data/watch/watch.sqlite3",
                 s1_db=root / "data/watch/watch_s1.sqlite3", runner_status=st.parent / "runner_status.json",
                 state_file=st, fill_ledger=r(settings.storage.fill_ledger_file),
                 paper_orders=st.parent / "paper_orders.jsonl", journal=r(settings.storage.tracked_order_journal_file),
                 commands_dir=r(settings.storage.commands_dir), reports_dir=r(settings.storage.reports_dir),
                 app_log=r(settings.storage.app_log_file), daemon_log=root / "logs/watch_daemon.log",
                 trade_log=r(settings.storage.trade_log_file))


# ── 공용 ────────────────────────────────────────────────────

def ro_query(path: Path, sql: str, args=(), *, retries: int = 2) -> Section:
    """읽기 전용 질의. 잠금·일시 충돌은 짧게 다시 시도 후 LOCKED."""
    if not path.exists():
        return Section(MISSING, source=str(path.name))
    last = ""
    for i in range(retries + 1):
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
            try:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(sql, args)]
            finally:
                conn.close()
            return Section(OK, rows, source=path.name)
        except sqlite3.OperationalError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if "locked" in str(exc) or "busy" in str(exc):
                time.sleep(0.2)
                continue
            return Section(UNREADABLE, error=last, source=path.name)
        except sqlite3.DatabaseError as exc:
            return Section(UNREADABLE, error=f"{type(exc).__name__}: {exc}", source=path.name)
    return Section(LOCKED, error=last, source=path.name)


def read_json(path: Path) -> Section:
    if not path.exists():
        return Section(MISSING, source=path.name)
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        return Section(OK, d, source=path.name)
    except (OSError, ValueError) as exc:
        return Section(UNREADABLE, error=f"{type(exc).__name__}: {exc}", source=path.name)


def read_jsonl(path: Path) -> Section:
    if not path.exists():
        return Section(MISSING, source=path.name)
    out, bad = [], 0
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError as exc:
        return Section(UNREADABLE, error=str(exc), source=path.name)
    s = Section(OK, out, source=path.name)
    if bad:
        s.notes.append(f"읽을 수 없는 줄 {bad}개")
    return s


def _age(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


# ── 비밀값 ──────────────────────────────────────────────────

class Redactor:
    def __init__(self, settings=None):
        sec = set()
        if settings is not None:
            b = settings.broker
            for v in (b.app_key, b.secret_key, b.account_number, str(b.account_number or "").replace("-", "")):
                if v and len(str(v)) >= 6:
                    sec.add(str(v))
            k = getattr(settings, "kakao", None)
            for v in (getattr(k, "access_token", ""), getattr(k, "refresh_token", ""), getattr(k, "rest_api_key", ""),
                      getattr(k, "client_secret", "")):
                if v and len(str(v)) >= 6:
                    sec.add(str(v))
        self.secrets = sorted(sec, key=len, reverse=True)

    def text(self, s: str) -> str:
        from infra.reporting.masking import mask
        out = str(s)
        for v in self.secrets:
            out = out.replace(v, "***")
        return mask(out)

    def obj(self, o):
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        return o


# ── 상단 상태 ───────────────────────────────────────────────

def daemon_status(p: Paths, now: datetime) -> Section:
    sec = ro_query(p.daemon_db, "SELECT run_id,pid,started_at,heartbeat_at,stopped_at,state,current_task,last_error,"
                                "progress FROM daemon_run ORDER BY started_at DESC LIMIT 1")
    if sec.state == UNREADABLE and "no such column" in sec.error:
        sec = ro_query(p.daemon_db, "SELECT run_id,pid,started_at,heartbeat_at,stopped_at,state,current_task,last_error "
                                    "FROM daemon_run ORDER BY started_at DESC LIMIT 1")
    if sec.state != OK:
        return sec
    if not sec.data:
        return Section(MISSING, error="실행 기록 없음", source=sec.source)
    r = sec.data[0]
    age = _age(r["heartbeat_at"], now)
    if r["state"] == "RUNNING" and age is not None and age <= STALE_SEC:
        label = "실행 중"
    elif r["state"] == "RUNNING":
        label = f"응답 없음 — heartbeat {age:.0f}초 전(멈춤·비정상 종료 의심)" if age is not None else "응답 없음"
    else:
        label = f"중지됨({r['state']})"
    r = {**r, "label": label, "heartbeat_age_sec": age, "code_version": _daemon_code_version(p),
         "mode": "조회 전용(모의 도메인 시세 TR) — 주문 없음"}
    return Section(OK, r, source=sec.source, as_of=r["heartbeat_at"])


def _daemon_code_version(p: Paths) -> str:
    tail = _tail_lines(p.daemon_log, 4000)
    for line in reversed(tail):
        if "[관리자] 실행 코드" in line:
            m = re.search(r"실행 코드 (.+?) · Python ([\d.]+)", line)
            return f"{m.group(1)} · Python {m.group(2)}" if m else line[:120]
    return "로그에 기록 없음"


def runner_status(p: Paths, now: datetime) -> Section:
    sec = read_json(p.runner_status)
    if sec.state != OK:
        if sec.state == MISSING:
            sec.error = "실행기 상태 파일 없음 — 이 버전으로 실행기가 아직 돈 적 없음"
        return sec
    d = sec.data
    age = _age(d.get("heartbeat_at"), now)
    stage = str(d.get("stage", ""))
    if stage.startswith("FINISHED"):
        label = f"종료됨({stage.split(':', 1)[-1]})"
    elif age is not None and age <= STALE_SEC:
        label = "실행 중"
    else:
        label = (f"응답 없음 — heartbeat {age:.0f}초 전(멈춤·비정상 종료 의심)" if age is not None
                 else "응답 없음")
    d = {**d, "label": label, "heartbeat_age_sec": age,
         "strategy_label": "자동 전략 연결됨" if d.get("strategy_connected") else "자동 전략 미연결(NullStrategy)"}
    return Section(OK, d, source=sec.source, as_of=d.get("heartbeat_at", ""))


def buy_halt_view(p: Paths, now: datetime) -> Section:
    from infra.storage.runner_status import read_buy_halt
    st, req = read_buy_halt(p.commands_dir)
    rs = read_json(p.runner_status)
    applied = None
    if rs.state == OK and req and (rs.data.get("buy_halt") or {}).get("request_id") == req.get("request_id"):
        applied = rs.data["buy_halt"].get("applied_at")
    state = "NONE" if st == "NONE" else ("APPLIED" if applied else "REQUESTED")
    label = {"NONE": "신규 매수 가능(중단 요청 없음)", "REQUESTED": "중단 요청됨 — 실행기 반영 전",
             "APPLIED": "신규 매수 중단 반영됨"}[state]
    return Section(OK, {"state": state, "label": label, "request": req, "applied_at": applied},
                   source="halt_new_buys.json")


# ── 오늘 진행 ───────────────────────────────────────────────

def today_progress(p: Paths, day: date, runner: Section) -> Section:
    sec = ro_query(p.daemon_db, "SELECT task_key,kind,trading_day,status,attempts,failures,started_at,finished_at,"
                                "next_retry_at,error,detail_json,contract_hash,config_version FROM task "
                                "WHERE trading_day<=? ORDER BY trading_day DESC, kind LIMIT 12", (day.isoformat(),))
    rows = []
    if sec.state == OK:
        today_open = next((t for t in sec.data if t["kind"] == "OPEN_CHECK" and t["trading_day"] == day.isoformat()), None)
        sig = None
        if today_open:
            sig = (json.loads(today_open["detail_json"] or "{}") or {}).get("signal_date")
        prep = next((t for t in sec.data if t["kind"] == "CLOSE_PREP" and (t["trading_day"] == sig or
                                                                            (sig is None and t["trading_day"] < day.isoformat()))), None)
        today_prep = next((t for t in sec.data if t["kind"] == "CLOSE_PREP" and t["trading_day"] == day.isoformat()), None)
        for title, t in (("마감 준비(신호일)", prep), ("개장 확인", today_open), ("오늘 마감 준비", today_prep)):
            if t is None:
                rows.append({"단계": title, "상태": "기록 없음", "시각": "", "사유": ""})
                continue
            det = json.loads(t["detail_json"] or "{}")
            why = t["error"] or (det.get("prepare") or {}).get("yield") or ""
            rows.append({"단계": title, "상태": f"{t['status']} (시도 {t['attempts']}/실패 {t['failures']})",
                         "시각": t["finished_at"] or t["started_at"] or "", "사유": why,
                         "작업": t["task_key"], "계약": t["contract_hash"] or ""})
        if today_open:
            det = json.loads(today_open["detail_json"] or "{}")
            cs = det.get("candidate_status", "")
            label = {"NO_CANDIDATES": "후보 0건(정상 완료)", "NO_SCAN": "후보 확인 불가 — 개장 전 스캔 없음",
                     "CANDIDATES": "후보 있음"}.get(cs, cs or "판정 없음")
            if today_open["status"] != "COMPLETE":
                label = f"개장 확인 {today_open['status']} — 후보 미확정"
            rows.append({"단계": "후보 확정", "상태": label, "시각": today_open["finished_at"] or "",
                         "사유": det.get("candidate_reason", ""), "후보 수": (det.get("counts") or {}).get("candidates")})
        else:
            rows.append({"단계": "후보 확정", "상태": "개장 확인 기록 없음", "시각": "", "사유": ""})
    else:
        why = f"관리자 DB {STATE_LABEL[sec.state]}"
        for title in ("마감 준비(신호일)", "개장 확인", "후보 확정"):
            rows.append({"단계": title, "상태": f"기록 없음 — {why}", "시각": "", "사유": sec.error})
    rows.insert(1, {"단계": "일봉 입력 재확인(08:40)", "상태": "미구현(자동 모의매매 최소판 B 단계)", "시각": "", "사유": ""})
    if runner.state == OK:
        d = runner.data
        c = d.get("counters") or {}
        st = (d["strategy_label"] if not d.get("strategy_connected") else
              f"전송 {c.get('orders_sent', 0)} · 접수 {c.get('orders_accepted', 0)} · 차단 {c.get('intents_denied', 0)}")
        rows.append({"단계": "주문 실행(실행기)", "상태": st, "시각": d.get("heartbeat_at", ""),
                     "사유": ", ".join(f"{k} {v}" for k, v in (d.get("denied_codes") or {}).items())})
    else:
        rows.append({"단계": "주문 실행(실행기)", "상태": f"실행기 상태 {STATE_LABEL[runner.state]}", "시각": "",
                     "사유": runner.error})
    out = Section(sec.state if sec.state != OK else OK, rows, error=sec.error, source="daemon.sqlite3 task · runner_status.json")
    return out


# ── 감시 종목 ───────────────────────────────────────────────

def watchlist(p: Paths) -> Section:
    rd = ro_query(p.watch_db, "SELECT target,code,status,reason,analysis_status,analysis_reason,checked_at,run_id,"
                              "config_version FROM readiness WHERE kind='STOCK' OR target LIKE 'STOCK:%' ORDER BY code")
    if rd.state != OK:
        return rd
    risk = ro_query(p.watch_db, "SELECT code,status,flags_json,checked_at FROM symbol_risk")
    risk_map = {r["code"]: r for r in (risk.data or [])} if risk.state == OK else {}
    run = ro_query(p.s1_db, "SELECT run_id,signal_date,scan_at,status,contract_hash FROM scan_run "
                            "WHERE status='COMPLETE' ORDER BY scan_at DESC LIMIT 1")
    evals, run_info = {}, None
    if run.state == OK and run.data:
        run_info = run.data[0]
        ev = ro_query(p.s1_db, "SELECT symbol,name,eligible_signal,pattern_pass,market_pass,eligibility_pass,"
                               "stop_valid,data_status,actionable FROM s1_eval WHERE run_id=?", (run_info["run_id"],))
        evals = {e["symbol"]: e for e in (ev.data or [])} if ev.state == OK else {}
    gate = ro_query(p.daemon_db, "SELECT symbol,stage,signal_date,eligible,reasons_json,committed_at FROM candidate_gate "
                                 "ORDER BY gate_id DESC LIMIT 200")
    last_gate = {}
    for g in (gate.data or []) if gate.state == OK else []:
        last_gate.setdefault(g["symbol"], g)
    rows = []
    for r in rd.data:
        e = evals.get(r["code"], {})
        g = last_gate.get(r["code"])
        excl = []
        if r["status"] != "READY":
            excl.append(f"가격 데이터 {r['status']} {r['reason']}")
        if r["analysis_status"] != "READY":
            excl.append(f"분석 {r['analysis_status']} {r['analysis_reason'] or ''}".strip())
        rk = risk_map.get(r["code"])
        if rk and rk["status"] != "OK":
            excl.append(f"위험 {rk['status']}")
        if g and not g["eligible"]:
            excl.append("게이트: " + ", ".join(json.loads(g["reasons_json"] or "[]")))
        rows.append({"종목": e.get("name", ""), "코드": r["code"], "가격 데이터": r["status"],
                     "분석": r["analysis_status"] or "-", "S1": e.get("eligible_signal", "기록 없음"),
                     "신규 진입 제외 사유": " · ".join(excl) or "-", "판정 시각": r["checked_at"]})
    s = Section(OK, rows, source="watch.sqlite3 readiness · watch_s1 s1_eval · daemon candidate_gate",
                as_of=rd.data[0]["checked_at"] if rd.data else "")
    if run_info:
        s.notes.append(f"S1: 신호일 {run_info['signal_date']} 스캔 {run_info['scan_at']} ({run_info['run_id']})")
    else:
        s.notes.append("S1 스캔 기록 없음")
    return s


# ── 보유·주문·비용 ──────────────────────────────────────────

def ledger_events(p: Paths) -> Section:
    if not p.fill_ledger.exists():
        return Section(MISSING, source=p.fill_ledger.name)
    try:
        ev = _ro_ledger_load(p.fill_ledger)
        return Section(OK, ev, source=p.fill_ledger.name)
    except Exception as exc:          # noqa: BLE001
        return Section(UNREADABLE, error=f"{type(exc).__name__}: {exc}", source=p.fill_ledger.name)


def _ro_ledger_load(path: Path):
    """원장 읽기 — FillLedgerStore.load()는 끊긴 줄을 격리(파일 수정)하므로 쓰지 않고 줄 단위로만 읽음."""
    from domain.position.fill_event import FillEvent
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(FillEvent.from_dict(json.loads(line)))
    seen, uniq = set(), []
    for e in out:
        if e.event_id not in seen:
            seen.add(e.event_id)
            uniq.append(e)
    return uniq


def holdings(p: Paths, runner: Section) -> Section:
    le = ledger_events(p)
    if le.state != OK:
        return le if le.state != MISSING else Section(OK, [], source=le.source, notes=["원장 없음 — 보유 없음"])
    from domain.service.lot_ledger import apply_events
    pos = apply_events(le.data).positions()
    broker = {}
    as_of = ""
    if runner.state == OK:
        broker = {x["symbol"]: x for x in runner.data.get("positions") or []}
        as_of = runner.data.get("last_balance_at") or ""
    rows = []
    for sym in sorted(set(pos) | set(broker)):
        lp, bp = pos.get(sym), broker.get(sym)
        rows.append({"종목": sym, "원장 수량": lp.quantity if lp else 0,
                     "원장 평균가": round(lp.avg_price) if lp else None,
                     "원장 가격 추정 포함": "예" if lp and lp.includes_estimate else "아니오",
                     "잔고 수량(실행기)": bp["quantity"] if bp else 0,
                     "잔고 평균가": bp["average_price"] if bp else None, "잔고 조회 시각": as_of,
                     "최근 평가가격": "미기록", "손절 기준": "미구현(C 단계)", "시간 청산 예정일": "미구현(C 단계)",
                     "첫 진입일": lp.first_entry_date.isoformat() if lp else ""})
    return Section(OK, rows, source="fill_ledger.jsonl · runner_status.json", as_of=as_of)


def orders(p: Paths, runner: Section) -> Section:
    reg = read_jsonl(p.paper_orders)
    rows = []
    if reg.state == OK:
        by = {}
        for r in reg.data:
            by.setdefault(r.get("command_id"), []).append(r)
        for cid, evs in by.items():
            last = evs[-1]
            placed = next((e for e in evs if e.get("event") == "PLACED"), {})
            fin = next((e for e in reversed(evs) if e.get("event") == "FINISHED"), None)
            kind = last.get("kind") or (evs[0].get("kind")) or "m1"
            rows.append({"출처": f"M1 도구({kind})", "명령": cid, "매수/매도": evs[0].get("side", ""),
                         "종목": evs[0].get("symbol", ""), "주문번호": (fin or {}).get("order_id") or placed.get("order_id") or "",
                         "주문 유형": "지정가" if kind == "limit-cancel" else "시장가",
                         "가격": placed.get("limit_price") or (fin or {}).get("limit_price") or (fin or {}).get("reference_price"),
                         "수량": evs[0].get("quantity"), "결과": (fin or {}).get("status") or f"진행 중/불명({last.get('event')})",
                         "시각": last.get("at", ""), "비고": (fin or {}).get("reason", "") or ""})
    if runner.state == OK:
        for o in runner.data.get("tracked_orders") or []:
            rows.append({"출처": "실행기(추적 중)", "명령": runner.data.get("run_id"), "매수/매도": o["side"], "종목": o["symbol"],
                         "주문번호": o["order_id"], "주문 유형": "시장가", "가격": o.get("reference_price"),
                         "수량": o["requested"], "결과": f"체결 {o['filled']}/{o['requested']} · 미체결 {o['requested'] - o['filled']}",
                         "시각": runner.data.get("heartbeat_at", ""), "비고": "추적 중 — 종료 전"})
        for sym in runner.data.get("recovery_symbols") or []:
            rows.append({"출처": "실행기", "명령": "", "매수/매도": "", "종목": sym, "주문번호": "", "주문 유형": "",
                         "가격": None, "수량": None, "결과": "ERROR·orphan — 사람 확인 필요", "시각": runner.data.get("heartbeat_at", ""),
                         "비고": "commands/recovery_required.json의 recovery_id로 ack 명령"})
    st = OK if (reg.state in (OK, MISSING) or runner.state == OK) else reg.state
    s = Section(st, rows, error=reg.error, source="paper_orders.jsonl · runner_status.json")
    s.notes.append("취소 요청·부분 체결의 자동 처리(09:30 취소)는 미구현 — M1 limit-cancel 실측 결과만 표시")
    return s


PRICE_SOURCE_LABEL = {"BROKER_FILL": "체결가(브로커 근거)", "BROKER_AVG": "잔고 평균단가 역산(브로커 근거)",
                      "ORDER_ESTIMATE": "추정(주문 직전 시세)"}


def costs(p: Paths, settings) -> Section:
    le = ledger_events(p)
    if le.state != OK:
        return le
    from domain.service.fill_evidence import apply_fill_evidence
    from domain.service.lot_ledger import apply_events
    from infra.storage.fill_evidence import FillEvidenceStore
    ev_file = p.fill_ledger.with_name("fill_evidence.jsonl")
    ev_note = ""
    try:      # M3 A2: 주문별 체결 근거(ka10076)를 원장 사건에 겹침 — 읽기 전용(파일·폴더 만들지 않음)
        overlay = apply_fill_evidence(le.data, FillEvidenceStore(ev_file, read_only=True).load())
    except Exception as exc:          # noqa: BLE001
        overlay = apply_fill_evidence(le.data, [])
        ev_note = f"체결 근거 기록 읽기 실패 — 원장 가격만 표시: {type(exc).__name__}: {exc}"
    events = overlay.events
    led = apply_events(events)
    ev_rows = []
    for e in events:
        r = overlay.applied.get(e.event_id)
        ev_rows.append({"거래일": e.trade_date.isoformat(), "종목": e.symbol, "구분": e.kind, "수량": e.quantity,
                        "가격": e.price, "가격 출처": PRICE_SOURCE_LABEL.get(e.price_source, e.price_source),
                        "수수료": "-" if r is None or r.commission is None else r.commission,
                        "세금": "-" if r is None or r.tax is None else r.tax,
                        "주문번호": e.order_id, "메모": e.note})
    real = [{"매도일": r.sell_date.isoformat(), "종목": r.symbol, "수량": r.quantity, "매수가": r.buy_price,
             "매도가": r.sell_price, "손익(비용 전)": r.gross_pnl,
             "구분": "추정 포함" if r.is_estimate else "브로커 근거"} for r in led.realized]
    try:
        from domain.cost_model import load_cost_model
        cm = load_cost_model(p.root / "config/settings.yaml", use_cache=False)
        cmd = {"gross": cm.gross_roundtrip_pct, "base": cm.base_roundtrip_pct, "stress": cm.stress_roundtrip_pct}
    except Exception as exc:          # noqa: BLE001
        cmd = {"error": f"{type(exc).__name__}: {exc}"}
    s = Section(OK, {"events": ev_rows, "realized": real, "cost_model": cmd},
                source="fill_ledger.jsonl + fill_evidence.jsonl")
    n_cost = sum(1 for r in overlay.applied.values() if r.commission is not None)
    s.notes.append(f"실제 수수료·세금은 체결 근거가 있는 사건만 표시({n_cost}건) — 손익(비용 전)과 비용 모델은 추정. "
                   "장부 대조 일치는 체결 가격 검증이 아님")
    for m in overlay.mismatched[:5]:
        s.notes.append(f"체결 근거 적용 안 함: {m}")
    if ev_note:
        s.notes.append(ev_note)
    return s


# ── 로그·오류 ───────────────────────────────────────────────

ACTIONS = [
    (r"STARTUP_BLOCK", "시작 거부 — 메시지의 사유(모드·잔고·원장·데이터 폴더) 확인 후 다시 실행"),
    (r"RECOVERY_REQUIRED", "HTS에서 주문·잔고 확인 → commands/recovery_required.json의 recovery_id로 ack 명령"),
    (r"SESSION_HALT", "신규 주문 중단됨 — 원장·잔고 확인, 실행기 재시작 전 원인 해결"),
    (r"ORDER_PLACEMENT_AMBIGUOUS|NOT_ACCEPTED|ambiguous", "주문 접수 여부 불명 — 재전송 금지, HTS 확인"),
    (r"429|허용된 요청 개수", "호출 제한 — 대개 자동 재시도로 복구, 반복되면 실행 시간 분리"),
    (r"잔고 조회 실패", "일시 오류면 다음 폴링에서 복구, 계속되면 네트워크·토큰 확인"),
    (r"원장|LEDGER", "원장 확인 — tools/ledger_review.py status"),
    (r"관리자|CLOSE_PREP|OPEN_CHECK", "관리자 status·로그 확인(python tools/watch_daemon.py status)"),
]
_APP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| (\w+) \| (.*)$")
_DMN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) \[([^\]]+)\] (.*)$")
_ERR_WORDS = ("실패", "오류", "FAILED", "ERROR", "거부", "중단", "응답 없음", "CRASHED", "Traceback", "예외", "HTTP 4",
              "HTTP 5", "http=4", "http=5", "429", "401")


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            data = fh.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def action_for(text: str) -> str:
    for pat, act in ACTIONS:
        if re.search(pat, text):
            return act
    return "로그 앞뒤 확인"


def recent_errors(p: Paths, red: Redactor, limit: int = 40) -> Section:
    rows = []
    notes = []
    for path, comp in ((p.app_log, "실행기"), (p.daemon_log, "관리자")):
        if not path.exists():
            notes.append(f"{comp} 로그 없음({path.name})")
            continue
        for line in _tail_lines(path, 3000):
            m = _APP_RE.match(line)
            if m:
                at, lvl, msg = m.group(1).replace(" ", "T"), m.group(2), m.group(3)
                if lvl not in ("ERROR", "CRITICAL", "WARNING"):
                    continue
            else:
                m = _DMN_RE.match(line)
                if not m:
                    continue
                at, lvl, msg = m.group(1), m.group(2), m.group(3)
                if not any(w in msg for w in _ERR_WORDS):
                    continue
            rows.append({"시각": at, "구성 요소": comp, "수준": lvl, "내용": red.text(msg)[:400], "조치": action_for(msg)})
    rows.sort(key=lambda r: r["시각"], reverse=True)
    if len(notes) == 2:
        return Section(MISSING, [], error="로그 파일 없음 — 오류 여부를 확인할 수 없음",
                       source="logs/app.log · logs/watch_daemon.log", notes=notes)
    return Section(OK, rows[:limit], source="logs/app.log · logs/watch_daemon.log", notes=notes)


def log_lines(path: Path, day: date, red: Redactor) -> list[str]:
    d1, d2 = day.isoformat(), day.isoformat()
    out = []
    for line in _tail_lines(path, 200_000):
        if line.startswith(d1) or line.startswith(d2.replace("-", "-")):
            out.append(red.text(line))
    return out


def download_bundle(p: Paths, day: date, red: Redactor, summary: dict) -> bytes:
    """선택 날짜 로그·운영 요약 zip(비밀값 가림). .env·DB 원본은 넣지 않음."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"summary_{day}.json", json.dumps(red.obj(summary), ensure_ascii=False, indent=1, default=str))
        for path, name in ((p.app_log, "app"), (p.daemon_log, "watch_daemon")):
            z.writestr(f"logs/{name}_{day}.log", "\n".join(log_lines(path, day, red)))
        for f in (p.reports_dir / f"session_status_{day}.json", p.reports_dir / f"daily_report_{day}.md",
                  p.root / "reports/watch/daily" / f"watch_{day}.md"):
            if f.exists() and f.is_file():
                z.writestr(f"reports/{f.name}", red.text(f.read_text(encoding="utf-8", errors="replace")))
        if p.paper_orders.exists():
            z.writestr("paper_orders.jsonl", red.text(p.paper_orders.read_text(encoding="utf-8", errors="replace")))
    return buf.getvalue()


def commands_help(p: Paths) -> list[tuple[str, str]]:
    return [
        ("관리자 상태", "python tools/watch_daemon.py status"),
        ("관리자 실행(전용 창)", "python -u tools/watch_daemon.py run 2>&1 | Tee-Object -FilePath logs\\watch_console.log"),
        ("관리자 중지", "python tools/watch_daemon.py stop"),
        ("실행기 실행(하루 수명주기)", "python -m app.main"),
        ("실행기 점검만", "python -m app.main --check-only"),
        ("작업 스케줄러 실행기 시작/멈춤", "Start-ScheduledTask -TaskName SwingAutoTrader / Stop-ScheduledTask -TaskName SwingAutoTrader"),
        ("신규 매수 중단 상태", "python tools/buy_halt.py status"),
        ("신규 매수 재개(사람 확인 후)", "python tools/buy_halt.py resume --note \"확인 내용\" --confirm"),
        ("원장 상태", "python tools/ledger_review.py status"),
        ("진단(읽기 전용)", "python diag_w2.py 005930 <신호일> --out logs\\diag_<날짜>.txt"),
    ]


def snapshot(p: Paths, settings, now: datetime, day: date | None = None) -> dict:
    """화면 한 번 그릴 자료 전부 — 각 구역의 기록 시각이 다름(같은 순간 아님)."""
    day = day or now.date()
    red = Redactor(settings)
    rn = runner_status(p, now)
    return {"now": now.isoformat(timespec="seconds"), "daemon": daemon_status(p, now), "runner": rn,
            "halt": buy_halt_view(p, now), "progress": today_progress(p, day, rn), "watch": watchlist(p),
            "holdings": holdings(p, rn), "orders": orders(p, rn), "errors": recent_errors(p, red),
            "costs": costs(p, settings)}


def section_dict(s: Section) -> dict:
    return {"state": s.state, "error": s.error, "source": s.source, "as_of": s.as_of, "notes": s.notes, "data": s.data}
