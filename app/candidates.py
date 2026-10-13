from __future__ import annotations

"""실행기용 S1 후보 승인 (M3 B — GPT 최소판 요청 2) — 관리자 DB를 **읽기 전용(mode=ro)**으로만 읽음.

관리자(watch_daemon, 조회 전용)가 만든 기록을 실행기가 교차 검증해, 오늘(D) 신규 진입해도 되는 후보만 돌려줍니다.
관리자 코드는 실행기를 모르고, 이 모듈은 관리자 DB에 쓰지 않습니다(파일·표를 만들지 않음 — 없으면 오류로 보고).

후보 하나가 승인되려면 **모두** 만족 (하나라도 아니면 그 종목은 사유와 함께 제외):
| # | 근거 | 조건 |
|---|---|---|
| 1 | daemon task OPEN_CHECK·D | COMPLETE (아직이면 WAITING — 다시 읽기) |
| 2 | A5 candidate_set·D | status OK, 신호일 = D의 직전 거래일, 계약 C |
| 3 | scan_run(후보의 run_id) | COMPLETE · 계약 C · scan_at·committed_at < 개장 · after_close_min ≥ 하한(기본 300 — 18:10 스캔은 잠정 관찰) |
| 4 | s1_observation(signal_id) | PASS · final=1 · actionable=1 · run_id·input_hash·계약이 후보와 같음 |
| 5 | candidate_gate | **같은 스캔 실행(scan_run_id)**의 마지막 SCAN 게이트 eligible·개장 전 저장 완료 ∧ 마지막 OPEN 게이트 eligible |
| 6 | input_check(D·t·C·종목) | **같은 스캔 실행**의 종목·지수 각각 마지막 결과 STABLE, 확인·저장 시각이 **개장 20분 전 ≤ · < 개장 5분 전**(일반 08:40~08:55) |
| 7 | A5 price_check | OPEN_CHECK 작업의 확인 종류(OPEN+Nm)만, 같은 신호, fetch OK·**ON_TIME**·WITHIN_CAP, 목표 시각 = 달력 개장 + N분, 요청 ≥ 목표 |

C1(GPT 검토 R2~R4): 개장 시각은 DB 값을 믿지 않고 거래일 달력으로 계산해 대조. OPEN_CHECK 작업의 계약 = 후보 목록 계약.
다른 실행의 최신 행으로 누락을 대체하지 않음.
| 8 | 가격 | 진입 상한·참고 손절가 양수, 지정가 = 진입 상한을 KRX 호가로 내림 > 참고 손절가 |

DB를 못 읽거나 형식이 다르면 ERROR — 신규 매수만 보류(보유 청산·주문 복구와 무관). 주문 직전 가격 재확인은 전략이 따로 함.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from domain.market_data.tick_size import floor_to_tick

READY, WAITING, ERROR = "READY", "WAITING", "ERROR"
INPUT_CHECK_LEAD_MIN, INPUT_CHECK_DEADLINE_MIN = 20, 5      # infra/watch/daemon.py와 같은 값(관리자 코드는 import 안 함)


@dataclass(frozen=True)
class ApprovedCandidate:
    symbol: str
    name: str
    signal_id: str
    signal_date: str
    contract_hash: str
    scan_run_id: str
    entry_cap: float
    stop_ref: float
    limit_price: int
    observed_price: int | None
    observed_outcome: str


@dataclass
class CandidateBook:
    state: str                                  # READY / WAITING / ERROR
    day: str
    signal_date: str = ""
    contract_hash: str = ""
    approved: list[ApprovedCandidate] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""
    read_at: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "day": self.day, "signal_date": self.signal_date,
                "contract_hash": self.contract_hash, "approved": [a.__dict__ for a in self.approved],
                "rejected": self.rejected, "note": self.note, "read_at": self.read_at}


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"{path.name} 없음")
    cx = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    cx.row_factory = sqlite3.Row
    return cx


class CandidateReader:
    def __init__(self, watch_dir: str | Path, calendar, *, min_after_close_min: int = 300) -> None:
        d = Path(watch_dir)
        self.daemon_db, self.scan_db, self.open_db = d / "daemon.sqlite3", d / "watch_s1.sqlite3", d / "watch_open.sqlite3"
        self.cal = calendar
        self.min_after_close_min = min_after_close_min

    def read(self, day: date, now: datetime) -> CandidateBook:
        book = CandidateBook(WAITING, day.isoformat(), read_at=now.isoformat(timespec="seconds"))
        try:
            return self._read(day, book)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            book.state, book.note = ERROR, f"관리자 기록 읽기 실패 {type(exc).__name__}: {exc}"
            book.approved = []
            return book

    def _read(self, day: date, book: CandidateBook) -> CandidateBook:
        sig = self.cal.previous_trading_day(day).isoformat()
        book.signal_date = sig
        st = self.cal.session_times(day)
        if st is None:
            book.state, book.note = ERROR, f"{day}는 달력상 거래일이 아님"
            return book
        cal_open = datetime.combine(day, st.open)
        cal_open_s = cal_open.isoformat(timespec="seconds")
        ic_from = (cal_open - timedelta(minutes=INPUT_CHECK_LEAD_MIN)).isoformat(timespec="seconds")
        ic_until = (cal_open - timedelta(minutes=INPUT_CHECK_DEADLINE_MIN)).isoformat(timespec="seconds")
        with closing(_ro(self.daemon_db)) as dx:
            ot = dx.execute("SELECT status, scope, contract_hash FROM task WHERE kind='OPEN_CHECK' AND trading_day=?"
                            " ORDER BY task_key", (day.isoformat(),)).fetchall()
            done = [r for r in ot if r["status"] == "COMPLETE"]
            if not done:
                book.note = f"개장 확인(OPEN_CHECK {day}) 아직 끝나지 않음 — {[r['status'] for r in ot] or '작업 없음'}"
                return book
            if len(done) != 1:
                book.state, book.note = ERROR, f"끝난 개장 확인 작업이 {len(done)}개 — 어느 것인지 정할 수 없음"
                return book
            check_kind, task_contract = done[0]["scope"], done[0]["contract_hash"]
            with closing(_ro(self.open_db)) as ox:
                cs = ox.execute("SELECT * FROM candidate_set WHERE target_day=?", (day.isoformat(),)).fetchone()
                if cs is None:
                    book.state, book.note = ERROR, "개장 확인은 끝났는데 후보 목록(candidate_set)이 없음"
                    return book
                book.contract_hash = cs["contract_hash"]
                if cs["signal_date"] != sig:
                    book.state, book.note = ERROR, f"후보 목록 신호일 {cs['signal_date']} ≠ 직전 거래일 {sig}"
                    return book
                if cs["open_at"] != cal_open_s:
                    book.state, book.note = ERROR, f"후보 목록 개장 시각 {cs['open_at']} ≠ 달력 {cal_open_s}"
                    return book
                if task_contract != cs["contract_hash"]:
                    book.state, book.note = ERROR, (f"개장 확인 작업 계약 {task_contract} ≠ 후보 목록 계약 "
                                                    f"{cs['contract_hash']}")
                    return book
                book.state = READY
                if cs["status"] != "OK":
                    book.note = f"후보 없음 ({cs['status']}: {cs['reason']})"
                    return book
                open_at = cs["open_at"]
                cands = [dict(r) for r in ox.execute("SELECT * FROM candidate WHERE set_id=? ORDER BY symbol",
                                                     (cs["set_id"],))]
                checks = {r["symbol"]: dict(r) for r in ox.execute(
                    "SELECT * FROM price_check WHERE set_id=? AND check_kind=?", (cs["set_id"], check_kind))}
            with closing(_ro(self.scan_db)) as sx:
                runs = {r["run_id"]: dict(r) for r in sx.execute("SELECT * FROM scan_run WHERE signal_date=?", (sig,))}
                obs = {r["signal_id"]: dict(r) for r in sx.execute(
                    "SELECT * FROM s1_observation WHERE signal_date=?", (sig,))}
            gates = [dict(r) for r in dx.execute(
                "SELECT * FROM candidate_gate WHERE signal_date=? AND contract_hash=? ORDER BY gate_id",
                (sig, cs["contract_hash"]))]
            has_ic = dx.execute("SELECT 1 FROM sqlite_master WHERE name='input_check'").fetchone()
            ics = [dict(r) for r in dx.execute(
                "SELECT * FROM input_check WHERE target_day=? AND signal_date=? AND contract_hash=? ORDER BY check_id",
                (day.isoformat(), sig, cs["contract_hash"]))] if has_ic else []
        C = cs["contract_hash"]
        for c in cands:
            sym, why = c["symbol"], []
            run = runs.get(c["run_id"])
            if run is None:
                why.append("SCAN_RUN_MISSING")
            else:
                if run["status"] != "COMPLETE" or run.get("contract_hash") != C:
                    why.append(f"SCAN_RUN:{run['status']}/{run.get('contract_hash')}")
                if not run.get("committed_at") or run["committed_at"] >= open_at or run["scan_at"] >= open_at:
                    why.append("SCAN_NOT_COMMITTED_BEFORE_OPEN")
                if int(run.get("after_close_min") or 0) < self.min_after_close_min:
                    why.append(f"PROVISIONAL_SCAN(after_close_min={run.get('after_close_min')}"
                               f"<{self.min_after_close_min})")
            o = obs.get(c["signal_id"])
            if o is None:
                why.append("OBSERVATION_MISSING")
            elif not (o["eligible_signal"] == "PASS" and o["final"] == 1 and o["actionable"] == 1
                      and o["run_id"] == c["run_id"] and o["input_hash"] == c["input_hash"]
                      and o.get("contract_hash") == C):
                why.append("OBSERVATION_MISMATCH")
            same_run = [g for g in gates if g["symbol"] == sym and g.get("scan_run_id") == c["run_id"]]
            scan_g = [g for g in same_run if g["stage"] == "SCAN" and g["committed_at"]
                      and g["committed_at"] < open_at and g["evaluated_at"] < open_at]
            open_g = [g for g in same_run if g["stage"] == "OPEN" and g["open_at"] == open_at]
            if not scan_g or not scan_g[-1]["eligible"]:
                why.append("SCAN_GATE:" + ("NONE" if not scan_g else ",".join(json.loads(scan_g[-1]["reasons_json"]))))
            if not open_g or not open_g[-1]["eligible"]:
                why.append("OPEN_GATE:" + ("NONE" if not open_g else ",".join(json.loads(open_g[-1]["reasons_json"]))))
            mine = [r for r in ics if r["symbol"] == sym and r["scan_run_id"] == c["run_id"]]
            for role in ("STOCK", "INDEX"):
                last = [r for r in mine if r["role"] == role]
                if not last:
                    why.append(f"INPUT_CHECK_{role}:NONE")
                    continue
                r = last[-1]
                if r["status"] != "STABLE":
                    why.append(f"INPUT_CHECK_{role}:{r['status']}")
                elif not (r["checked_at"] and r["committed_at"] and ic_from <= r["checked_at"] < ic_until
                          and ic_from <= r["committed_at"] < ic_until):
                    why.append(f"INPUT_CHECK_{role}:OUTSIDE_WINDOW(확인 {r['checked_at']}·저장 {r['committed_at']}, "
                               f"허용 {ic_from[11:16]}~{ic_until[11:16]})")
            pc = checks.get(sym)
            if pc is None:
                why.append(f"OPEN_PRICE:NONE({check_kind})")
            else:
                target = _target_at(cal_open, check_kind)
                if pc["signal_id"] != c["signal_id"]:
                    why.append("OPEN_PRICE:SIGNAL_MISMATCH")
                if pc["fetch_status"] != "OK":
                    why.append(f"OPEN_PRICE:FETCH_{pc['fetch_status']}")
                if pc["timing"] != "ON_TIME":
                    why.append(f"OPEN_PRICE:TIMING_{pc['timing']}")
                if target is None or pc["target_at"] != target or not pc.get("requested_at") \
                        or not pc.get("received_at") or pc["requested_at"] < target:
                    why.append(f"OPEN_PRICE:TIME_MISMATCH(목표 {pc['target_at']}·달력 {target}·요청 {pc.get('requested_at')})")
                if pc["outcome"] != "WITHIN_CAP":
                    why.append(f"OPEN_PRICE:{pc['outcome']}")
            cap, stop = c.get("entry_cap"), c.get("stop_ref")
            limit = None
            if not cap or not stop or cap <= 0 or stop <= 0:
                why.append("LEVELS_INVALID")
            else:
                limit = floor_to_tick(cap)
                if limit <= stop:
                    why.append(f"LIMIT_NOT_ABOVE_STOP({limit}≤{stop})")
            if why:
                book.rejected[sym] = why
                continue
            book.approved.append(ApprovedCandidate(
                symbol=sym, name=c.get("name") or "", signal_id=c["signal_id"], signal_date=sig, contract_hash=C,
                scan_run_id=c["run_id"], entry_cap=float(cap), stop_ref=float(stop), limit_price=limit,
                observed_price=pc.get("observed_price"), observed_outcome=pc["outcome"]))
        book.note = f"후보 {len(cands)}건 → 승인 {len(book.approved)}건"
        return book


def _target_at(cal_open: datetime, check_kind: str) -> str | None:
    """'OPEN+5m' → 달력 개장 + 5분(ISO 초). 형식이 다르면 None(승인 안 함)."""
    k = str(check_kind or "")
    if not (k.startswith("OPEN+") and k.endswith("m") and k[5:-1].isdigit()):
        return None
    return (cal_open + timedelta(minutes=int(k[5:-1]))).isoformat(timespec="seconds")
