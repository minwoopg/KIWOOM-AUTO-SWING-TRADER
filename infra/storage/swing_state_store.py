from __future__ import annotations

"""SwingState 저장소 (스윙 분리 4라운드, 2026-09-28).

단타 `JsonStateStore`와 다른 점
- 손상·형식 불일치를 **조용히 빈 상태로 대체하지 않습니다** (`SwingStateCorruptError`).
  단타 저장소는 필드를 검증 없이 읽었습니다. 이 파일에는 재시작 복구에 쓰이는
  "보냈는지 모르는 주문" 기록이 있으므로, 못 읽으면 주문을 막아야 합니다.
- schema_version을 기록하고 검사합니다. 단타 형식(schema_version 없음) 파일을
  실수로 가리키면 거부합니다.
- 쓰기는 같은 원자적 관용구(tmp → flush → fsync → os.replace)입니다.

`save(state, highest_price=None)`의 두 번째 인자는 `OrderExecutor`가 단타
저장소와 같은 호출 형태로 부르기 때문에 받기만 하고 무시합니다. 스윙의
최고가 같은 값은 `PositionMeta.meta`에 둡니다.
"""

import json
import os
import tempfile
from pathlib import Path

from domain.position.swing_state import SwingState, SwingStateFormatError


class SwingStateCorruptError(RuntimeError):
    """state 파일을 읽을 수 없거나 형식이 맞지 않음 — 복구 전까지 주문 금지."""


class SwingStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> tuple[SwingState, dict[str, int]]:
        """(state, {}) 반환. 파일이 없으면 빈 상태. 두 번째 값은 호환용 빈 dict."""
        if not self.path.exists():
            return SwingState(), {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SwingStateCorruptError(f"{self.path}: 읽기/JSON 파싱 실패 — {exc}") from exc
        try:
            return SwingState.from_dict(raw), {}
        except (SwingStateFormatError, KeyError, TypeError, ValueError) as exc:
            raise SwingStateCorruptError(f"{self.path}: 형식 오류 — {exc}") from exc

    def save(self, state: SwingState, highest_price: dict | None = None) -> None:
        payload = state.to_dict()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
