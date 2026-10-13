from __future__ import annotations

"""전략 신호 키 — 같은 신호로 두 번 진입하지 않기 위한 영속 기록 (M3 B, GPT 검토 — append-only JSON Lines).

- 키 = "{모드}|{계좌 라벨}|{signal_id}" (signal_id에 전략·계약·종목·신호일이 들어 있음).
- 주문 의도를 실행기에 넘기기 **전에** 기록(`claim`). 이미 있으면 False — 그 신호로는 다시 의도를 만들지 않음.
  지정가가 취소돼 FLAT으로 돌아와도, 안전 한도에 막혀도 같은 신호는 끝(재진입 없음).
- 그림자(shadow)와 모의 전송(paper)은 파일이 따로 — 그림자 기록이 실제 전송을 막지 않음.
- 끊긴 마지막 줄은 읽을 때 건너뛰지 않고 키로 인정(보수적 — 쓰다 멈춘 키도 사용한 것으로 봄).
"""

import json
import os
from pathlib import Path


def keys_path(state_file: str | Path, mode: str) -> Path:
    return Path(state_file).with_name(f"strategy_signal_keys_{mode}.jsonl")


class SignalKeyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def keys(self) -> set[str]:
        if not self.path.exists():
            return set()
        out = set()
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.add(str(json.loads(line)["key"]))
            except (ValueError, KeyError, TypeError):
                # 끊긴 줄 — 키 문자열이 보이면 사용한 것으로 봄(다시 진입하지 않는 쪽)
                if '"key": "' in line:
                    out.add(line.split('"key": "', 1)[1].split('"', 1)[0])
        return out

    def has(self, key: str) -> bool:
        return key in self.keys()

    def claim(self, key: str, record: dict) -> bool:
        if self.has(key):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"key": key, **record}, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
