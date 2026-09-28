from __future__ import annotations

"""체결 원장 — append-only JSON Lines (스윙 분리 4라운드, 2026-09-28).

여러 날 보유의 수량·매입단가·진입일의 **단일 원천**입니다. 한 줄 = 체결 사건
하나(`FillEvent`). 수정·삭제는 하지 않고, 정정이 필요하면 사람이 확인한 뒤
반대 방향 사건을 추가합니다.

가격 출처(price_source)를 항상 함께 저장합니다 — 단타 레포에서 "주문가 기준
추정"과 "실제 체결가"가 섞여 손익이 부정확했던 문제(2026-09-17 조사 보고서)를
처음부터 구분하기 위함입니다.

| price_source | 의미 |
|---|---|
| BROKER_FILL | 체결조회(ka10076)의 체결가 |
| BROKER_AVG | 잔고의 매입 평균단가(기존 보유분 인수, 잔고 변화로 역산한 매수 원가) |
| ORDER_ESTIMATE | 주문 직전 시세 — 추정치 |

손상 처리
- 마지막 줄이 개행 없이 끊겨 있고 JSON으로 읽히지 않으면 → 쓰다가 중단된
  줄로 보고 `<파일>.torn-<시각>`으로 옮긴 뒤 계속 (그 사건은 기록되지 않은
  것으로 취급 — 잔고 대조에서 수량 불일치로 드러남)
- 그 외 위치의 손상, 같은 event_id에 다른 내용 → `FillLedgerCorruptError`
  (조용히 건너뛰지 않음)
"""

import json
import os
from datetime import datetime
from pathlib import Path

from domain.position.fill_event import (  # noqa: F401  (재노출)
    FILL_KINDS, LEDGER_SCHEMA_VERSION, PRICE_SOURCES, FillEvent, FillEventError,
)


class FillLedgerCorruptError(RuntimeError):
    """원장 파일을 신뢰할 수 없음 — 복구 전까지 주문 금지."""


class FillLedgerStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.torn_tail_paths: list[Path] = []

    def load(self) -> list[FillEvent]:
        """기록 순서대로 반환. 끊긴 마지막 줄은 격리, 그 외 손상은 예외."""
        if not self.path.exists():
            return []
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise FillLedgerCorruptError(f"{self.path}: 읽기 실패 — {exc}") from exc
        if not data:
            return []
        text = data.decode("utf-8")
        lines = text.split("\n")
        ends_with_newline = text.endswith("\n")
        if ends_with_newline:
            lines = lines[:-1]
        events: list[FillEvent] = []
        seen: dict[str, dict] = {}
        for idx, line in enumerate(lines):
            is_last = idx == len(lines) - 1
            if not line.strip():
                raise FillLedgerCorruptError(f"{self.path}:{idx + 1}: 빈 줄")
            try:
                raw = json.loads(line)
                event = FillEvent.from_dict(raw)
            except Exception as exc:
                if is_last and not ends_with_newline:
                    self._quarantine_torn_tail(line)
                    break
                raise FillLedgerCorruptError(f"{self.path}:{idx + 1}: 읽을 수 없는 줄 — {exc}") from exc
            if event.event_id in seen:
                if seen[event.event_id] != event.to_dict():
                    raise FillLedgerCorruptError(
                        f"{self.path}:{idx + 1}: event_id={event.event_id} 가 다른 내용으로 중복됨")
                continue
            seen[event.event_id] = event.to_dict()
            events.append(event)
        return events

    def _quarantine_torn_tail(self, line: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        torn = self.path.with_name(f"{self.path.name}.torn-{stamp}")
        torn.write_text(line, encoding="utf-8")
        # 원본에서 끊긴 꼬리만 잘라냄 (앞의 완결된 줄은 그대로)
        data = self.path.read_bytes()
        keep = data[: data.rstrip(b"\n").rfind(b"\n") + 1] if b"\n" in data else b""
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(keep)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self.torn_tail_paths.append(torn)

    def append(self, event: FillEvent) -> bool:
        """기록하면 True, 같은 사건이 이미 있으면 False. 같은 id·다른 내용은 예외."""
        for existing in self.load():
            if existing.event_id == event.event_id:
                if existing.to_dict() != event.to_dict():
                    raise FillLedgerCorruptError(
                        f"event_id={event.event_id} 가 이미 다른 내용으로 기록돼 있음")
                return False
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return True
