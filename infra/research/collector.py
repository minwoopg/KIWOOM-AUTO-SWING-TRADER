from __future__ import annotations

"""A2 수집기 — 종목 목록 스냅숏, 재개 가능한 백필, 매일 갱신 (조회 전용, 주문 없음).

수집 대상과 신호 자격 분리 (A2 보완 2)
- 백필·매일 갱신 대상 = 스냅숏의 collect(보통주 전체, 현재 위험 표시 종목 포함) + 지수 2개.
- 현재 위험 표시는 universe_row.eligible_now에만 반영. 과거 위험 상태는 UNKNOWN(현재 값으로 과거 표본을 고르지 않음).

완성 봉만 저장 (A2 보완 6)
- 날짜 d의 봉은 응답 수신 시각이 **d의 정규장 종료 + BAR_COMPLETE_AFTER_CLOSE**(기본 160분 — 15:30 종료면
  18:10, 시간외 단일가 18:00 종료 뒤) 이후일 때만 완성으로 봅니다. 그 전에 받은 당일 봉(장중 스냅숏)은 버립니다.
  d가 수신일보다 이전이면 완성. 달력이 d를 다루지 않으면 완성 여부를 모르므로 버립니다(CALENDAR_UNKNOWN).
  기본값은 보수적 잠정값 — 장 마감 후 시각별 비교 실측으로 줄일 수 있습니다.

종료 조건 (A2 보완 5)
- 페이지 수가 아니라 **필요 시작일(required_from, 기본 2017-01-02)** 이하의 날짜를 받았는지로 종료.
  원천이 먼저 끝나면(cont-yn=N) 상장일로 LISTED_AFTER_START / HISTORY_END를 구분해 기록.
  안전 상한(max_pages, 기본 8)에 걸리면 PAGE_CAP. 부족해도 받은 만큼은 저장하고 사유를 남깁니다.

수정주가 기준 (A2 보완 4, A2-R2) — `infra/research/store.py` 설명 참고.
- 백필 작업은 만들 때 base_dt를 고정하고, 재개해도 같은 base_dt·upd_stkpc_tp=1로 조회합니다.
- 한 종목은 모든 페이지를 한 번에 받아 한 트랜잭션에 저장(중간에 끊기면 그 종목은 저장되지 않고 재개 시 처음부터).
- 기존 봉이 있는 시계열은 후보 검증(비어 있지 않음·저장 범위 포함·변경을 발견한 첫 페이지 값 재현)을 통과해야
  교체합니다. 실패하면 값·조정 메타를 그대로 두고 REBASE_REQUIRED(다음 갱신 때 다시 시도, 그 전엔 새 날짜 추가 안 함).
- 매일 갱신의 겹침 비교는 **첫 페이지 범위 안의 정합 검사**입니다. 그보다 오래된 과거 정정까지 확인한 것은 아닙니다.

시각 (A2-R1): 모든 행에 그 페이지의 수신 시각(received_at)을 붙여 저장합니다. 새 revision의 값은 그 revision의
활성 시각(마지막 페이지 수신 시각) 이후에만 쓸 수 있습니다.

연속조회 계약 (A2-R3): `kiwoom_readonly`가 return_code=0·cont-yn(Y/N)·Y일 때 next-key를 검사하고,
여기서 페이지 진행(과거로)·날짜 순서·base_dt 이후 날짜·다음 키 반복·빈 페이지를 검사합니다.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from domain.research.universe import UniversePolicy, classify_rows, summarize
from infra.research.kiwoom_readonly import Page, ResearchApiError
from infra.research.kiwoom_rows import INDEX_DAILY, STOCK_DAILY, RawBar, RowError, SourceSpec, parse_row
from infra.research.store import (
    COVERAGE_OK, DONE, ERROR, HISTORY_END, INTEGRITY_OK, LISTED_AFTER_START, PAGE_CAP, PENDING, SHORTFALL,
    AdjustmentBasis, FetchedBar, IntegrityError, ResearchStore,
)
from utils.trading_calendar import CalendarCoverageError, TradingCalendar

BAR_COMPLETE_AFTER_CLOSE = timedelta(minutes=160)
DEFAULT_REQUIRED_FROM = date(2017, 1, 2)       # 2019년 평가 + EMA50 300봉·252일 준비 구간 여유
DEFAULT_MAX_PAGES = 8
MAX_LIST_PAGES = 20
INDEX_TARGETS = (("INDEX:KOSPI:001", "001"), ("INDEX:KOSDAQ:101", "101"))
REACHED, SOURCE_END = "REACHED", "SOURCE_END"


class CollectError(RuntimeError):
    """수집 결과를 믿을 수 없음 (그 종목은 저장하지 않음)."""


def stock_series_id(code: str) -> str:
    return f"STOCK:{code}"


def spec_of(kind: str) -> SourceSpec:
    return STOCK_DAILY if kind == "STOCK" else INDEX_DAILY


def payload_of(spec: SourceSpec, code: str, base_dt: str) -> dict:
    if spec.kind == "STOCK":
        return {"stk_cd": code, "base_dt": base_dt, "upd_stkpc_tp": "1"}
    return {"inds_cd": code, "base_dt": base_dt}


def completion(d: date, received_at: datetime, calendar: TradingCalendar,
               after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE) -> str:
    """"" = 완성, 아니면 사유 (INTRADAY / CALENDAR_UNKNOWN / NOT_SESSION / FUTURE_DATE)."""
    if d < received_at.date():
        return ""
    if d > received_at.date():
        return "FUTURE_DATE"
    try:
        st = calendar.session_times(d)
    except CalendarCoverageError:
        return "CALENDAR_UNKNOWN"
    if st is None:
        return "NOT_SESSION"
    return "" if received_at >= datetime.combine(d, st.close) + after_close else "INTRADAY"


@dataclass
class FetchResult:
    bars: list[FetchedBar]                         # 날짜 오름차순, 행마다 그 페이지의 수신 시각
    invalid_raw: dict
    pages: int
    stop: str
    first_received: datetime | None
    last_received: datetime | None
    dropped_incomplete: list[tuple[date, str]] = field(default_factory=list)

    @property
    def first_date(self) -> date | None:
        return self.bars[0].date if self.bars else None

    @property
    def last_date(self) -> date | None:
        return self.bars[-1].date if self.bars else None

    def by_date(self) -> dict[date, RawBar]:
        return {fb.date: fb.raw for fb in self.bars}


def decide_coverage(fr: FetchResult, required_from: date, reg_day: date | None) -> tuple[str, str]:
    first = fr.first_date
    detail = (f"first={first},required_from={required_from},reg_day={reg_day},pages={fr.pages},stop={fr.stop}")
    if first is not None and first <= required_from:
        return COVERAGE_OK, detail
    if fr.stop == PAGE_CAP:
        return PAGE_CAP, detail
    if reg_day is not None and reg_day > required_from:
        return LISTED_AFTER_START, detail
    return HISTORY_END, detail


def _yyyymmdd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


class ResearchCollector:
    def __init__(self, client, store: ResearchStore, calendar: TradingCalendar, *,
                 policy: UniversePolicy | None = None,
                 required_from: date = DEFAULT_REQUIRED_FROM,
                 max_pages: int = DEFAULT_MAX_PAGES,
                 after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE,
                 log: Callable[[str], None] | None = None) -> None:
        if max_pages < 1:
            raise ValueError("max_pages는 1 이상")
        if after_close < timedelta(0):
            raise ValueError("after_close는 0 이상")
        self.client = client
        self.store = store
        self.calendar = calendar
        self.policy = policy or UniversePolicy()
        self.required_from = required_from
        self.max_pages = max_pages
        self.after_close = after_close
        self.log = log or (lambda msg: None)

    # ── 공통: 한 시계열 전체 조회 ────────────────────────────
    def fetch_full(self, spec: SourceSpec, code: str, base_dt: str, required_from: date,
                   max_pages: int | None = None) -> FetchResult:
        """연속조회 계약 검사 (A2-R3): 페이지 안 날짜는 엄격한 내림차순, 다음 페이지는 이전 페이지보다 과거,
        base_dt 뒤 날짜 없음, 다음 키 반복 없음, 이어지는 페이지가 비어 있지 않음, 첫 응답이 비어 있지 않음.
        어기면 CollectError — 그 종목은 저장하지 않고 ERROR로 남습니다."""
        payload = payload_of(spec, code, base_dt)
        base = _yyyymmdd(base_dt)
        bars: dict[date, FetchedBar] = {}
        invalid_raw: dict[date, dict] = {}
        dropped: list[tuple[date, str]] = []
        cont, key, pages, stop = "N", "", 0, PAGE_CAP
        first_received = last_received = None
        prev_oldest: date | None = None
        seen_keys: set[str] = set()
        for _ in range(max_pages or self.max_pages):
            page: Page = self.client.fetch_page(spec.api_id, payload, spec.list_key, cont, key)
            pages += 1
            first_received = first_received or page.received_at
            last_received = page.received_at
            if not page.rows:
                raise CollectError(f"{code}: {'첫 응답' if pages == 1 else f'{pages}번째 페이지'}가 비어 있음"
                                   f"(cont-yn={page.cont_yn})")
            page_dates = []
            for row in page.rows:
                rb = parse_row(row)
                if page_dates and rb.date >= page_dates[-1]:
                    raise CollectError(f"{code}: 페이지 안 날짜가 내림차순이 아님 ({page_dates[-1]} → {rb.date})")
                page_dates.append(rb.date)
                if rb.date > base:
                    raise CollectError(f"{code}: 요청 base_dt({base_dt}) 뒤 날짜의 행 {rb.date}")
                why = completion(rb.date, page.received_at, self.calendar, self.after_close)
                if why == "FUTURE_DATE":
                    raise CollectError(f"{code}: 수신일 이후 날짜의 행 {rb.date}")
                if why:
                    dropped.append((rb.date, why))
                    continue
                bars[rb.date] = FetchedBar(rb, page.received_at)
                if not rb.valid:
                    invalid_raw[rb.date] = row
            if prev_oldest is not None and page_dates[0] >= prev_oldest:
                raise CollectError(f"{code}: 연속조회가 과거로 진행하지 않음 ({prev_oldest} → {page_dates[0]})")
            prev_oldest = page_dates[-1]
            if page_dates[-1] <= required_from:
                stop = REACHED
                break
            if not page.has_more:
                stop = SOURCE_END
                break
            if page.next_key in seen_keys:
                raise CollectError(f"{code}: 같은 next-key 반복 ({page.next_key})")
            seen_keys.add(page.next_key)
            cont, key = "Y", page.next_key
        return FetchResult([bars[d] for d in sorted(bars)], invalid_raw, pages, stop, first_received,
                           last_received, dropped)

    # ── 종목 목록 스냅숏 ─────────────────────────────────────
    def snapshot_universe(self) -> dict:
        rows, raw_pages = [], []
        observed_at = observed_end = None
        for market, mrkt_tp in (("KOSPI", "0"), ("KOSDAQ", "10")):
            cont, key, seen = "N", "", set()
            for i in range(MAX_LIST_PAGES):
                page = self.client.fetch_page("ka10099", {"mrkt_tp": mrkt_tp}, "list", cont, key)
                observed_at = observed_at or page.received_at
                observed_end = page.received_at
                if not page.rows:
                    raise CollectError(f"종목 목록 {market}: {i + 1}번째 페이지가 비어 있음")
                raw_pages.append({"mrkt_tp": mrkt_tp, "page": i + 1, "received_at": page.received_at.isoformat(),
                                  "cont_yn": page.cont_yn, "rows": page.rows})
                rows += page.rows
                if not page.has_more:
                    break
                if page.next_key in seen:
                    raise CollectError(f"종목 목록 {market}: 같은 next-key 반복")
                seen.add(page.next_key)
                cont, key = "Y", page.next_key
            else:
                raise CollectError(f"종목 목록 {market}: {MAX_LIST_PAGES}페이지 넘음")
        return self._save_snapshot(rows, raw_pages, observed_at, observed_end, "ka10099")

    def snapshot_from_rows(self, rows: list[dict], raw_pages: list, observed_at: datetime,
                           observed_end: datetime, source: str) -> dict:
        return self._save_snapshot(rows, raw_pages, observed_at, observed_end, source)

    def _save_snapshot(self, rows, raw_pages, observed_at, observed_end, source) -> dict:
        records = classify_rows(rows, self.policy)
        summary = summarize(records)
        try:
            phase = self.calendar.phase(observed_at).value
        except CalendarCoverageError:
            phase = "UNKNOWN"
        sid = self.store.save_snapshot(snapshot_date=observed_at.date(), observed_at=observed_at,
                                       observed_end_at=observed_end, market_phase=phase,
                                       policy_version=self.policy.policy_version, source=source,
                                       records=records, raw_pages=raw_pages, summary=summary)
        return {"snapshot_id": sid, "observed_at": observed_at, "market_phase": phase, **summary}

    # ── 저장: 새 시계열 / 기존 시계열(검증 후 교체) ────────────
    def _store_full(self, *, spec, series_id, code, fr: FetchResult, basis, job_id, required_from, reg_day,
                    expected=None, forward_after=None, reason, now, in_tx=False) -> dict:
        coverage, detail = decide_coverage(fr, required_from, reg_day)
        if fr.dropped_incomplete:
            detail += f",dropped_incomplete={[f'{d}:{w}' for d, w in fr.dropped_incomplete]}"
        activated = fr.last_received or now()
        has_bars = self.store.conn.execute("SELECT 1 FROM bar WHERE series_id=? LIMIT 1", (series_id,)).fetchone()
        if not has_bars:
            if not fr.bars:
                raise CollectError(f"{code}: 완성 봉 없음 (dropped={fr.dropped_incomplete})")
            res = self.store.init_series(spec=spec, series_id=series_id, code=code, bars=fr.bars, basis=basis,
                                         activated_at=activated, job_id=job_id, required_from=required_from,
                                         coverage=coverage, coverage_detail=detail, invalid_raw=fr.invalid_raw,
                                         reason=reason, now=now(), in_tx=in_tx)
        else:
            res = self.store.replace_series(spec=spec, series_id=series_id, code=code, candidate=fr.bars,
                                            basis=basis, activated_at=activated, job_id=job_id,
                                            required_from=required_from, coverage=coverage, coverage_detail=detail,
                                            expected=expected, forward_after=forward_after,
                                            invalid_raw=fr.invalid_raw, reason=reason, now=now(), in_tx=in_tx)
        return {**res, "coverage": coverage, "detail": detail}

    # ── 백필 작업 ────────────────────────────────────────────
    def create_backfill_job(self, *, now: datetime, snapshot_id: int | None = None,
                            codes: list[str] | None = None, include_index: bool = True,
                            series_ids: list[str] | None = None) -> str:
        snap = (self.store.latest_snapshot() if snapshot_id is None
                else {"snapshot_id": snapshot_id})
        if snap is None:
            raise CollectError("종목 목록 스냅숏이 없음 — 먼저 universe를 실행")
        rows = self.store.snapshot_rows(snap["snapshot_id"], collect_only=True)
        want: set[str] | None = None
        if codes:
            want = set(codes)
        if series_ids is not None:
            want = {s.split(":", 1)[1] for s in series_ids if s.startswith("STOCK:")}
            include_index = include_index and any(s.startswith("INDEX:") for s in series_ids)
        if want is not None:
            rows = [r for r in rows if r["code"] in want]
            missing = want - {r["code"] for r in rows}
            if missing:
                raise CollectError(f"스냅숏의 수집 대상이 아닌 코드: {sorted(missing)}")
        targets = []
        if include_index:
            targets += [{"series_id": sid, "kind": "INDEX", "code": c, "reg_day": None} for sid, c in INDEX_TARGETS
                        if series_ids is None or sid in series_ids]
        targets += [{"series_id": stock_series_id(r["code"]), "kind": "STOCK", "code": r["code"],
                     "reg_day": r["reg_day"]} for r in rows]
        if not targets:
            raise CollectError("작업 대상이 없음")
        base_dt = now.strftime("%Y%m%d")
        job_id = f"backfill_{now.strftime('%Y%m%d_%H%M%S')}"
        self.store.create_job(job_id=job_id, kind="BACKFILL", created_at=now, base_dt=base_dt, upd_stkpc_tp="1",
                              required_from=self.required_from, max_pages=self.max_pages,
                              snapshot_id=snap["snapshot_id"], targets=targets,
                              params={"after_close_min": self.after_close.total_seconds() / 60,
                                      "policy_version": self.policy.policy_version,
                                      "selection": f"snapshot {snap['snapshot_id']}의 collect(보통주) — 현재 상장 종목만",
                                      "survivorship": "과거 상장폐지 종목 없음(생존 편향). 과거 위험 상태 UNKNOWN"})
        return job_id

    def run_backfill(self, job_id: str, *, now: Callable[[], datetime], limit: int | None = None,
                     progress: Callable[[dict], None] | None = None) -> dict:
        job = self.store.get_job(job_id)
        if job is None:
            raise CollectError(f"작업 없음: {job_id}")
        required_from = date.fromisoformat(job["required_from"])
        items = self.store.job_items(job_id, (PENDING, ERROR))
        if limit is not None:
            items = items[:limit]
        done = {DONE: 0, SHORTFALL: 0, ERROR: 0}
        for n, it in enumerate(items, 1):
            spec = spec_of(it["kind"])
            reg_day = date.fromisoformat(it["reg_day"]) if it["reg_day"] else None
            try:
                fr = self.fetch_full(spec, it["code"], job["base_dt"], required_from, job["max_pages"])
                basis = AdjustmentBasis(job["upd_stkpc_tp"] if spec.kind == "STOCK" else None, job["base_dt"])
                with self.store.tx():
                    res = self._store_full(spec=spec, series_id=it["series_id"], code=it["code"], fr=fr, basis=basis,
                                           job_id=job_id, required_from=required_from, reg_day=reg_day,
                                           reason=f"BACKFILL:{job_id}", now=now, in_tx=True)
                    if res["action"] == "REBASE_FAILED":
                        status, reason = ERROR, f"REBASE_FAILED|{';'.join(res['problems'])}"
                    else:
                        status = DONE if res["coverage"] == COVERAGE_OK else SHORTFALL
                        reason = "" if status == DONE else f"{res['coverage']}|{res['detail']}"
                    self.store.set_item(job_id, it["series_id"], status=status, pages=fr.pages,
                                        first_date=fr.first_date, last_date=fr.last_date, reason=reason, now=now(),
                                        in_tx=True)
            except (ResearchApiError, RowError, CollectError, ValueError) as exc:
                status = ERROR
                self.store.set_item(job_id, it["series_id"], status=ERROR, pages=0, first_date=None, last_date=None,
                                    reason=f"{type(exc).__name__}: {exc}"[:500], now=now())
                self.log(f"[BACKFILL] {it['series_id']} ERROR {exc}")
            done[status] += 1
            if progress:
                progress({"n": n, "of": len(items), "series_id": it["series_id"], "status": status})
        finished = self.store.close_job_if_finished(job_id)
        return {"job_id": job_id, "processed": len(items), **done, "job_finished": finished,
                "counts": self.store.job_counts(job_id), "calls": getattr(self.client, "calls", None)}

    # ── 매일 갱신 ────────────────────────────────────────────
    def _open_job_series(self) -> set[str]:
        out = set()
        for j in self.store.open_jobs():
            out |= {it["series_id"] for it in self.store.job_items(j["job_id"], (PENDING, ERROR))}
        return out

    def update_series(self, series_id: str, kind: str, code: str, *, now: Callable[[], datetime],
                      reg_day: date | None = None) -> dict:
        """첫 페이지를 오늘 base_dt로 받아 저장분과 **그 페이지 범위 안에서** 비교(범위 정합 검사).
        같으면 새 날짜만 FORWARD로 추가. 다르거나 재검증 필요 상태면 전체를 다시 받아 검증 후 교체."""
        spec = spec_of(kind)
        meta = self.store.get_series(series_id)
        base_dt = now().strftime("%Y%m%d")
        basis = AdjustmentBasis("1" if kind == "STOCK" else None, base_dt)
        if meta is None or meta.last_date is None:           # 새 상장 등 — 처음부터 전체
            fr = self.fetch_full(spec, code, base_dt, self.required_from)
            res = self._store_full(spec=spec, series_id=series_id, code=code, fr=fr, basis=basis, job_id=None,
                                   required_from=self.required_from, reg_day=reg_day, reason="FORWARD_INIT", now=now)
            return {"series_id": series_id, "action": "INIT", "coverage": res["coverage"], "bars": len(fr.bars)}
        required_from = meta.required_from or self.required_from
        if meta.integrity != INTEGRITY_OK:
            mismatch, expected = f"RETRY_{meta.integrity}", None
        else:
            fr1 = self.fetch_full(spec, code, base_dt, required_from, max_pages=1)
            fresh = fr1.by_date()
            if not fresh:
                return {"series_id": series_id, "action": "NO_DATA", "dropped": fr1.dropped_incomplete}
            stored = {sb.raw.date: sb.raw for sb in self.store.load_bars(series_id)}
            fmin = min(fresh)
            parts = []
            if meta.last_date < fmin:
                parts.append(f"GAP_BEYOND_FIRST_PAGE:last={meta.last_date},page_oldest={fmin}")
            else:
                lo = max(fmin, meta.first_date)
                s_w = {d for d in stored if lo <= d <= meta.last_date}
                f_w = {d for d in fresh if lo <= d <= meta.last_date}
                missing, extra = sorted(s_w - f_w), sorted(f_w - s_w)
                changed = sorted(d for d in s_w & f_w
                                 if stored[d].values() != fresh[d].values() or stored[d].quality != fresh[d].quality)
                older = sorted(d for d in fresh if d < meta.first_date)
                if changed:
                    parts.append(f"CHANGED:{len(changed)}(first={changed[0]})")
                if missing:
                    parts.append(f"MISSING:{len(missing)}(first={missing[0]})")
                if extra:
                    parts.append(f"EXTRA:{len(extra)}(first={extra[0]})")
                if older:
                    parts.append(f"OLDER_THAN_STORED:{len(older)}")
            mismatch, expected = ",".join(parts), fresh
            if not mismatch:
                new = [fb for fb in fr1.bars if fb.date > meta.last_date]
                added = self.store.append_forward(series_id=series_id, bars=new, verified_base_dt=base_dt,
                                                  verified_at=fr1.last_received, now=now())
                return {"series_id": series_id, "action": "APPEND" if added else "UNCHANGED", "added": added,
                        "dropped": fr1.dropped_incomplete}
        # 조정 기준이 바뀌었거나 원천이 고쳐짐 → 새 기준으로 전체 다시 받아 **검증 후** 교체 (A2-R2)
        try:
            fr = self.fetch_full(spec, code, base_dt, required_from)
        except (ResearchApiError, RowError, CollectError) as exc:
            self.store.mark_integrity(series_id, f"{mismatch}|FETCH_FAILED:{exc}"[:500], now())
            raise
        res = self._store_full(spec=spec, series_id=series_id, code=code, fr=fr, basis=basis, job_id=None,
                               required_from=required_from, reg_day=reg_day, expected=expected,
                               forward_after=meta.last_date, reason=f"FORWARD_MISMATCH:{mismatch}", now=now)
        if res["action"] == "REBASE_FAILED":
            self.log(f"[UPDATE] {series_id} 재수집 검증 실패 — {mismatch} / {res['problems']} (기존 값·기준 유지)")
            return {"series_id": series_id, "action": "REBASE_FAILED", "reason": mismatch,
                    "problems": res["problems"], "revision": res["revision"]}
        self.log(f"[UPDATE] {series_id} 전체 재수집 — {mismatch} → {res['action']} rev{res['revision']}")
        return {"series_id": series_id, "action": f"REFETCH_{res['action']}", "reason": mismatch,
                "revision": res["revision"]}

    def run_update(self, *, now: Callable[[], datetime], limit: int | None = None,
                   progress: Callable[[dict], None] | None = None) -> dict:
        snap = self.store.latest_snapshot()
        if snap is None:
            raise CollectError("종목 목록 스냅숏이 없음 — 먼저 universe를 실행")
        blocked = self._open_job_series()
        targets = [(sid, "INDEX", c, None) for sid, c in INDEX_TARGETS]
        for r in self.store.snapshot_rows(snap["snapshot_id"], collect_only=True):
            targets.append((stock_series_id(r["code"]), "STOCK", r["code"],
                            date.fromisoformat(r["reg_day"]) if r["reg_day"] else None))
        targets = [t for t in targets if t[0] not in blocked]
        if limit is not None:
            targets = targets[:limit]
        tally: dict[str, int] = {}
        for n, (sid, kind, code, reg_day) in enumerate(targets, 1):
            try:
                res = self.update_series(sid, kind, code, now=now, reg_day=reg_day)
            except (ResearchApiError, RowError, CollectError, ValueError, IntegrityError) as exc:
                res = {"series_id": sid, "action": "ERROR", "reason": f"{type(exc).__name__}: {exc}"[:300]}
                self.log(f"[UPDATE] {sid} ERROR {exc}")
            tally[res["action"]] = tally.get(res["action"], 0) + 1
            if progress:
                progress({"n": n, "of": len(targets), **res})
        failed = tally.get("ERROR", 0) + tally.get("REBASE_FAILED", 0)
        return {"snapshot_id": snap["snapshot_id"], "targets": len(targets), "skipped_open_job": len(blocked),
                "tally": tally, "failed": failed, "calls": getattr(self.client, "calls", None)}


def load_probe_list(path: Path) -> tuple[list[dict], list, datetime, datetime]:
    """A1 프로브 원시 응답(jsonl)에서 종목 목록을 읽음 (오프라인 확인·첫 스냅숏 용)."""
    import json
    rows, raw_pages, times = [], [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("api_id") != "ka10099":
            continue
        body = rec.get("response_body") or {}
        if rec.get("http_status") != 200 or body.get("return_code") not in (0, None):
            raise CollectError(f"프로브 목록 응답 오류: {rec.get('label')}")
        page_rows = body.get("list") or []
        rows += page_rows
        times.append(datetime.fromisoformat(rec["requested_at"]))
        raw_pages.append({"mrkt_tp": rec.get("request_payload", {}).get("mrkt_tp"), "label": rec.get("label"),
                          "requested_at": rec["requested_at"], "rows": page_rows})
    if not rows:
        raise CollectError(f"프로브 파일에 종목 목록(ka10099)이 없음: {path}")
    return rows, raw_pages, min(times), max(times)
