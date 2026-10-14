from __future__ import annotations

"""전략 신호 키 — 같은 신호로 두 번 진입하지 않기 위한 영속 기록 (M3 B·C2, append-only JSON Lines).

- 키 = "{모드}|{계좌 라벨}|{signal_id}" (signal_id에 전략·계약·종목·신호일이 들어 있음).
- 주문 의도를 실행기에 넘기기 **전에** `claim`(CLAIMED). 이미 있으면 False — 그 신호로는 다시 의도를 만들지 않음.
  지정가가 취소돼 FLAT으로 돌아와도, 안전 한도에 막혀도, 주문 응답이 불명이어도, 재시작해도 같은 신호는 끝.
- 결과는 같은 키로 상태 줄을 덧붙임(`mark`, C2 — GPT 검토 Q5): SHADOW / GUARD_REJECTED:<사유> / SUBMITTED /
  NOT_ACCEPTED / UNKNOWN. 키 상태 = 마지막 줄. CLAIMED만 있고 결과가 없으면 "주문 전 중단"으로 보임(재전송 안 함).
- 그림자(shadow)와 모의 전송(paper)은 파일이 따로 — 그림자 기록이 실제 전송을 막지 않음.
- **손상 처리(C2)**: 읽을 수 없는 줄이 하나라도 있으면 `SignalKeyCorruptError` — 전략은 그날 신규 진입을 하지 않음
  (손상 줄을 조용히 건너뛰어 재진입하지 않게). 보유 청산은 이 파일과 무관하게 계속. 사람 확인 뒤 파일을 고침.
"""

import json
import os
from datetime import datetime
from pathlib import Path

CLAIMED = "CLAIMED"


class SignalKeyCorruptError(RuntimeError):
    pass


def keys_path(state_file: str | Path, mode: str) -> Path:
    return Path(state_file).with_name(f"strategy_signal_keys_{mode}.jsonl")


class SignalKeyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SignalKeyCorruptError(f"{self.path.name} 읽기 실패: {exc}") from exc
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or not str(rec.get("key", "")).strip():
                    raise ValueError("key 없음")
            except ValueError as exc:
                raise SignalKeyCorruptError(f"{self.path.name}:{i} 읽을 수 없는 줄 — 신규 진입 중단, 사람 확인: {exc}") \
                    from exc
            out.append(rec)
        return out

    def keys(self) -> set[str]:
        return {str(r["key"]) for r in self.records()}

    def status(self) -> dict[str, str]:
        """키 → 마지막 상태(CLAIMED / SHADOW / GUARD_REJECTED:… / SUBMITTED / NOT_ACCEPTED / UNKNOWN)."""
        out: dict[str, str] = {}
        for r in self.records():
            out[str(r["key"])] = str(r.get("status") or CLAIMED)
        return out

    def has(self, key: str) -> bool:
        return key in self.keys()

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def claim(self, key: str, record: dict) -> bool:
        if self.has(key):
            return False
        self._append({"key": key, "status": CLAIMED, **record})
        return True

    def mark(self, key: str, status: str, detail: str = "", *, at: datetime | None = None) -> None:
        """결과 상태 줄 추가(키가 없으면 추가하지 않음 — claim 없이 상태만 생기지 않게)."""
        if not self.has(key):
            return
        self._append({"key": key, "status": status, "detail": detail[:300],
                      "at": (at or datetime.now()).isoformat(timespec="seconds")})
