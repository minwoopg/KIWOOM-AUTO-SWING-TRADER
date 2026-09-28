from __future__ import annotations

"""종목별 일봉 로컬 저장소 (스윙 분리 5라운드, 2026-09-28).

파일: `<dir>/<종목코드>.csv` (date,open,high,low,close,volume — 날짜 오름차순)
     `<dir>/<종목코드>.meta.json` (수집 조건·시각·완성 기준일)

- 완성된 봉만 저장합니다(저장 전에 호출부가 거름 — 저장소도 meta의
  completed_through보다 늦은 날짜가 있으면 거부).
- 쓰기는 원자적(tmp → fsync → os.replace). CSV와 meta 중 CSV를 먼저 쓰고
  meta를 나중에 씁니다 — 중간에 끊기면 meta의 row_count/last_date가 CSV와
  달라 다음 로드에서 불일치로 잡힙니다.
- 손상·불일치는 `DailyBarStoreCorruptError` (조용히 빈 데이터로 대체하지 않음).
"""

import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from domain.market_data.daily_bar import BarValidationError, DailyBar, check_series

STORE_SCHEMA_VERSION = 1
CSV_FIELDS = ["date", "open", "high", "low", "close", "volume"]
SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")


class DailyBarStoreCorruptError(RuntimeError):
    """저장된 일봉을 신뢰할 수 없음 — 재수집 필요."""


@dataclass(frozen=True)
class DailyBarMeta:
    symbol: str
    adjusted: bool              # 수정주가 여부 (upd_stkpc_tp=1 → True)
    source: str                 # 예: "kiwoom_ka10081"
    fetched_at: str             # ISO
    completed_through: date     # 이 날짜까지의 봉만 완성된 것으로 저장됨
    row_count: int
    first_date: date | None
    last_date: date | None

    def to_dict(self) -> dict:
        return {
            "schema_version": STORE_SCHEMA_VERSION, "symbol": self.symbol, "adjusted": self.adjusted,
            "source": self.source, "fetched_at": self.fetched_at,
            "completed_through": self.completed_through.isoformat(), "row_count": self.row_count,
            "first_date": self.first_date.isoformat() if self.first_date else None,
            "last_date": self.last_date.isoformat() if self.last_date else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DailyBarMeta":
        if d.get("schema_version") != STORE_SCHEMA_VERSION:
            raise ValueError(f"schema_version 불일치: {d.get('schema_version')!r}")
        return cls(
            symbol=str(d["symbol"]), adjusted=bool(d["adjusted"]), source=str(d["source"]),
            fetched_at=str(d["fetched_at"]), completed_through=date.fromisoformat(d["completed_through"]),
            row_count=int(d["row_count"]),
            first_date=date.fromisoformat(d["first_date"]) if d.get("first_date") else None,
            last_date=date.fromisoformat(d["last_date"]) if d.get("last_date") else None,
        )


def _atomic_write(path: Path, text: str) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


class DailyBarStore:
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        if not SYMBOL_RE.match(symbol):
            raise ValueError(f"종목코드 형식 오류: {symbol!r}")
        return self.dir / f"{symbol}.csv", self.dir / f"{symbol}.meta.json"

    def exists(self, symbol: str) -> bool:
        csv_path, meta_path = self._paths(symbol)
        return csv_path.exists() or meta_path.exists()

    def load(self, symbol: str) -> tuple[list[DailyBar], DailyBarMeta | None]:
        """저장된 봉(오름차순)과 meta. 둘 다 없으면 ([], None)."""
        csv_path, meta_path = self._paths(symbol)
        if not csv_path.exists() and not meta_path.exists():
            return [], None
        if not (csv_path.exists() and meta_path.exists()):
            raise DailyBarStoreCorruptError(f"{symbol}: CSV와 meta 중 하나만 있음")
        try:
            meta = DailyBarMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise DailyBarStoreCorruptError(f"{symbol}: meta 읽기 실패 — {exc}") from exc
        try:
            with csv_path.open(encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames != CSV_FIELDS:
                    raise ValueError(f"헤더 불일치: {reader.fieldnames}")
                bars = check_series(
                    DailyBar(date.fromisoformat(r["date"]), int(r["open"]), int(r["high"]),
                             int(r["low"]), int(r["close"]), int(r["volume"]))
                    for r in reader)
        except (ValueError, BarValidationError, KeyError, TypeError) as exc:
            raise DailyBarStoreCorruptError(f"{symbol}: CSV 읽기 실패 — {exc}") from exc
        if meta.symbol != symbol or meta.row_count != len(bars):
            raise DailyBarStoreCorruptError(
                f"{symbol}: meta(row_count={meta.row_count})와 CSV({len(bars)}행) 불일치 — 쓰기 중단 흔적")
        if bars and (meta.first_date != bars[0].date or meta.last_date != bars[-1].date):
            raise DailyBarStoreCorruptError(f"{symbol}: meta 날짜 범위와 CSV 불일치")
        if bars and bars[-1].date > meta.completed_through:
            raise DailyBarStoreCorruptError(f"{symbol}: 완성 기준일 이후의 봉이 저장돼 있음")
        return bars, meta

    def save(self, symbol: str, bars: list[DailyBar], *, adjusted: bool, source: str,
             fetched_at: str, completed_through: date) -> DailyBarMeta:
        bars = check_series(bars)
        if bars and bars[-1].date > completed_through:
            raise ValueError(f"{symbol}: 완성 기준일({completed_through}) 이후 봉은 저장하지 않음")
        csv_path, meta_path = self._paths(symbol)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(CSV_FIELDS)
        for b in bars:
            w.writerow([b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume])
        meta = DailyBarMeta(symbol, adjusted, source, fetched_at, completed_through, len(bars),
                            bars[0].date if bars else None, bars[-1].date if bars else None)
        _atomic_write(csv_path, buf.getvalue())
        _atomic_write(meta_path, json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
        return meta
