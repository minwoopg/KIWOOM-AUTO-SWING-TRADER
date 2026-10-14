from __future__ import annotations

"""일일 위험 기록 (C3) — `data/daily_risk.jsonl`, append-only.

줄 종류
- DAY_START     : 그 거래일 첫 폴링의 기준(원장 보유 수량·기준가·현금·시작 평가액). 같은 날 첫 줄만 유효 — 재시작해도 다시 쓰지 않음.
- ORDER_FAILURE : 전송한 주문이 미접수·응답 불명(NOT_ACCEPTED / UNKNOWN).
- HALT          : 일일 손실 한도·주문 실패 한도로 신규 매수 중단을 요청한 기록(실제 중단은 commands/halt.json — 사람이 재개).

읽을 수 없는 줄이 있으면 `DailyRiskCorruptError` — 실행기는 그날 신규 매수를 하지 않음(fail-closed).
"""

import json
import os
from pathlib import Path


class DailyRiskCorruptError(RuntimeError):
    pass


def daily_risk_path(state_file: str | Path) -> Path:
    return Path(state_file).with_name("daily_risk.jsonl")


class DailyRiskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise DailyRiskCorruptError(f"{self.path.name} 읽기 실패: {exc}") from exc
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get("type") not in ("DAY_START", "ORDER_FAILURE", "HALT") \
                        or not str(rec.get("trade_date", "")).strip():
                    raise ValueError("type·trade_date 없음")
            except ValueError as exc:
                raise DailyRiskCorruptError(f"{self.path.name}:{i} 읽을 수 없는 줄: {exc}") from exc
            out.append(rec)
        return out

    def day(self, trade_date: str) -> tuple[dict | None, list[dict], list[dict]]:
        """(DAY_START, ORDER_FAILURE 목록, HALT 목록) — 그 거래일."""
        recs = [r for r in self.records() if r["trade_date"] == trade_date]
        start = next((r for r in recs if r["type"] == "DAY_START"), None)
        return (start, [r for r in recs if r["type"] == "ORDER_FAILURE"], [r for r in recs if r["type"] == "HALT"])

    def append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
