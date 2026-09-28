from __future__ import annotations

"""일봉 갱신·조회 (스윙 분리 5라운드, 2026-09-28).

규칙
1. **완성된 봉만** — 실측상 장중 조회에는 당일 미완성 봉이 섞여 오므로,
   `calendar.last_completed_session(now)`보다 늦은 날짜의 행은 받자마자 버립니다.
   조회 API(`completed_bars`)도 같은 기준으로 한 번 더 자릅니다.
2. **증분 갱신** — 로컬에 있으면 1페이지(600행)만 받아 새 날짜를 붙입니다.
3. **수정주가 변경 감지** — 새로 받은 페이지와 로컬이 겹치는 구간에서 값이
   하나라도 다르거나 로컬에 있던 날짜가 빠져 있으면, 과거가 다시 계산된
   것(액면분할·무상증자 등)으로 보고 그 종목을 처음부터 다시 받습니다.
4. **이어 붙일 수 없는 공백** — 1페이지가 로컬 마지막 날짜까지 닿지 않으면
   (600거래일 이상 갱신 안 함) 역시 전체 재수집.
5. 캘린더가 다루는 연도에서 거래일인데 봉이 없는 날은 `missing_sessions`로
   **보고만** 합니다(거래정지 종목일 수 있으므로 오류로 보지 않음).
6. 이상한 행이 하나라도 있으면 그 종목은 저장하지 않고 실패로 보고.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from domain.market_data.daily_bar import BarValidationError, DailyBar, check_series, parse_kiwoom_daily_row
from infra.market_data.daily_bar_source import DataFetchError, PacedFetcher
from infra.market_data.daily_bar_store import DailyBarStore, DailyBarStoreCorruptError
from utils.trading_calendar import TradingCalendar

SOURCE_NAME = "kiwoom_ka10081"

INITIAL = "INITIAL"                # 처음 수집
APPENDED = "APPENDED"              # 새 봉 추가
UNCHANGED = "UNCHANGED"            # 새 봉 없음
FULL_REFETCH = "FULL_REFETCH"      # 수정주가 변경·공백·손상으로 전체 재수집
FAILED = "FAILED"


@dataclass
class UpdateResult:
    symbol: str
    action: str
    completed_through: date | None = None
    last_date: date | None = None
    added: int = 0
    total: int = 0
    pages_fetched: int = 0
    refetch_reason: str = ""
    history_truncated: bool = False
    missing_sessions: list[date] = field(default_factory=list)
    dropped_incomplete: int = 0     # 버린 미완성(미래) 행 수
    error: str = ""

    @property
    def is_current(self) -> bool:
        """마지막 봉이 완성 기준일과 같은가 (아니면 거래정지·지연 가능성)."""
        return self.last_date is not None and self.last_date == self.completed_through

    def line(self) -> str:
        if self.action == FAILED:
            return f"{self.symbol} FAILED | {self.error}"
        extra = []
        if self.refetch_reason:
            extra.append(f"재수집 사유: {self.refetch_reason}")
        if self.history_truncated:
            extra.append("과거 이력 일부만 확보")
        if self.missing_sessions:
            extra.append(f"누락 거래일 {len(self.missing_sessions)}일(최근 {self.missing_sessions[-1]})")
        if not self.is_current:
            extra.append(f"최신 아님(마지막 {self.last_date}, 기준 {self.completed_through})")
        return (f"{self.symbol} {self.action} | +{self.added} / 총 {self.total} | "
                f"~{self.last_date} | 페이지 {self.pages_fetched}" + (" | " + "; ".join(extra) if extra else ""))


class DailyBarRepository:
    def __init__(
        self,
        store: DailyBarStore,
        fetcher: PacedFetcher,
        calendar: TradingCalendar,
        *,
        backfill_pages: int = 3,
        logger=None,
    ) -> None:
        if backfill_pages < 1:
            raise ValueError("backfill_pages는 1 이상")
        self.store = store
        self.fetcher = fetcher
        self.calendar = calendar
        self.backfill_pages = backfill_pages
        self.logger = logger

    # ── 조회 ────────────────────────────────────────────────

    def completed_bars(self, symbol: str, now: datetime, lookback: int | None = None) -> list[DailyBar]:
        """now 시점에 완성된 봉만(오름차순). lookback이면 마지막 N개.

        저장된 봉이 완성 기준일보다 늦을 수 없지만(저장소가 거부), 조회 시점의
        기준일로 한 번 더 잘라 미래 봉이 절대 나가지 않게 합니다.
        """
        cutoff = self.calendar.last_completed_session(now)
        bars, _ = self.store.load(symbol)
        bars = [b for b in bars if b.date <= cutoff]
        return bars[-lookback:] if lookback else bars

    # ── 갱신 ────────────────────────────────────────────────

    def _fetch_pages(self, symbol: str, base_dt: str, max_pages: int, stop_before: date | None):
        """최신→과거로 페이지를 받아 행 목록을 모음. stop_before보다 오래된 행에
        도달하면 멈춤. (rows, pages, reached_end)"""
        rows: list = []
        pages = 0
        cont, key = "N", ""
        while pages < max_pages:
            page = self.fetcher.fetch_page(symbol, base_dt, cont, key)
            pages += 1
            rows.extend(page.rows)
            oldest = None
            for r in reversed(page.rows):
                try:
                    oldest = parse_kiwoom_daily_row(r).date
                    break
                except BarValidationError:
                    continue
            if not page.has_more:
                return rows, pages, True
            if stop_before is not None and oldest is not None and oldest <= stop_before:
                return rows, pages, False
            cont, key = "Y", page.next_key
        return rows, pages, False

    def _parse_completed(self, rows: list, cutoff: date) -> tuple[list[DailyBar], int]:
        parsed = [parse_kiwoom_daily_row(r) for r in rows]
        completed = [b for b in parsed if b.date <= cutoff]
        dropped = len(parsed) - len(completed)
        completed.sort(key=lambda b: b.date)
        return check_series(completed), dropped

    def update(self, symbol: str, now: datetime) -> UpdateResult:
        try:
            return self._update(symbol, now)
        except (DataFetchError, BarValidationError, ValueError) as exc:
            if self.logger is not None:
                self.logger.error(f"[DAILY_BARS] {symbol} 갱신 실패: {type(exc).__name__}: {exc}")
            return UpdateResult(symbol, FAILED, error=f"{type(exc).__name__}: {exc}")

    def _update(self, symbol: str, now: datetime) -> UpdateResult:
        cutoff = self.calendar.last_completed_session(now)
        base_dt = now.strftime("%Y%m%d")   # 실측으로 확인된 요청 형태(오늘 기준) — 결과는 cutoff로 거름
        refetch_reason = ""
        try:
            local, meta = self.store.load(symbol)
        except DailyBarStoreCorruptError as exc:
            local, meta, refetch_reason = [], None, f"로컬 손상({exc})"

        if not local:
            action = INITIAL if not refetch_reason else FULL_REFETCH
            return self._full_fetch(symbol, now, cutoff, base_dt, action, refetch_reason, stop_before=None)

        rows, pages, _ = self._fetch_pages(symbol, base_dt, 1, None)
        fetched, dropped = self._parse_completed(rows, cutoff)
        last_local = local[-1].date

        if not fetched or fetched[0].date > last_local:
            reason = "1페이지가 로컬 마지막 날짜까지 닿지 않음(공백)" if fetched else "응답에 완성 봉 없음"
            if not fetched:
                raise DataFetchError(f"{symbol}: {reason}")
            return self._full_fetch(symbol, now, cutoff, base_dt, FULL_REFETCH, reason,
                                    stop_before=local[0].date, pages_already=pages)

        # 겹치는 구간 비교: fetched의 가장 오래된 날 ~ 로컬 마지막 날
        window_start = fetched[0].date
        local_window = {b.date: b for b in local if b.date >= window_start}
        fetched_window = {b.date: b for b in fetched if b.date <= last_local}
        if local_window != fetched_window:
            changed = sorted(d for d in set(local_window) | set(fetched_window)
                             if local_window.get(d) != fetched_window.get(d))
            reason = f"겹치는 구간 값 변경 {len(changed)}일(예: {changed[0]}) — 수정주가 재계산 가능성"
            return self._full_fetch(symbol, now, cutoff, base_dt, FULL_REFETCH, reason,
                                    stop_before=local[0].date, pages_already=pages)

        new_bars = [b for b in fetched if b.date > last_local]
        merged = local + new_bars
        self.store.save(symbol, merged, adjusted=True, source=SOURCE_NAME,
                        fetched_at=now.isoformat(timespec="seconds"), completed_through=cutoff)
        return self._result(symbol, APPENDED if new_bars else UNCHANGED, merged, cutoff,
                            added=len(new_bars), pages=pages, dropped=dropped)

    def _full_fetch(self, symbol, now, cutoff, base_dt, action, reason, *, stop_before, pages_already=0):
        rows, pages, reached_end = self._fetch_pages(symbol, base_dt, self.backfill_pages, stop_before)
        bars, dropped = self._parse_completed(rows, cutoff)
        if not bars:
            raise DataFetchError(f"{symbol}: 완성 봉이 하나도 없음")
        truncated = (not reached_end) and (stop_before is None or bars[0].date > stop_before)
        self.store.save(symbol, bars, adjusted=True, source=SOURCE_NAME,
                        fetched_at=now.isoformat(timespec="seconds"), completed_through=cutoff)
        if self.logger is not None and action == FULL_REFETCH:
            self.logger.warning(f"[DAILY_BARS] {symbol} 전체 재수집 — {reason}")
        res = self._result(symbol, action, bars, cutoff, added=len(bars),
                           pages=pages + pages_already, dropped=dropped)
        res.refetch_reason = reason
        res.history_truncated = truncated and stop_before is not None
        return res

    def _result(self, symbol, action, bars, cutoff, *, added, pages, dropped) -> UpdateResult:
        res = UpdateResult(symbol, action, completed_through=cutoff,
                           last_date=bars[-1].date if bars else None, added=added, total=len(bars),
                           pages_fetched=pages, dropped_incomplete=dropped)
        covered = [y for y in self.calendar.covered_years]
        start = max(bars[0].date, date(min(covered), 1, 1)) if bars else None
        if start is not None and start <= cutoff:
            res.missing_sessions = self.calendar.compare_with_bar_dates(
                [b.date for b in bars], start, cutoff)["missing_bars"]
        return res
