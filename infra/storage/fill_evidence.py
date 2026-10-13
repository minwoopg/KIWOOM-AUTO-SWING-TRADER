from __future__ import annotations

"""체결 근거 기록 — append-only JSON Lines (M3 A2). 규칙은 `domain/service/fill_evidence.py`.

- 한 줄 = `FillEvidenceRecord` 하나. 수정·삭제 없음.
- 같은 event_id·같은 내용 → 무시(False). 같은 event_id·다른 내용 → `FillEvidenceConflictError`
  (덮어쓰지 않음 — 사람 확인).
- 개행 없이 끊긴 마지막 줄은 읽을 때 건너뜀(그 근거는 없는 것으로 — 다시 확인하면 됨),
  다음 기록 전에 그 줄을 `.torn-<시각>`으로 옮김. 그 외 손상은 `FillEvidenceCorruptError`.
- `read_only=True`면 폴더를 만들지 않음(관리 창·보고서용).
"""

import json
import os
from pathlib import Path

from domain.service.fill_evidence import FillEvidenceRecord


class FillEvidenceCorruptError(RuntimeError):
    pass


class FillEvidenceConflictError(RuntimeError):
    pass


def evidence_path(fill_ledger_file: str | Path) -> Path:
    return Path(fill_ledger_file).with_name("fill_evidence.jsonl")


class FillEvidenceStore:
    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[FillEvidenceRecord]:
        if not self.path.exists():
            return []
        text = self.path.read_bytes().decode("utf-8")
        if not text:
            return []
        lines = text.split("\n")
        complete = text.endswith("\n")
        if complete:
            lines = lines[:-1]
        out: list[FillEvidenceRecord] = []
        for idx, line in enumerate(lines):
            try:
                out.append(FillEvidenceRecord.from_dict(json.loads(line)))
            except Exception as exc:
                if idx == len(lines) - 1 and not complete:
                    break
                raise FillEvidenceCorruptError(f"{self.path}:{idx + 1}: 읽을 수 없는 줄 — {exc}") from exc
        return out

    def append(self, record: FillEvidenceRecord) -> bool:
        for existing in self.load():
            if existing.event_id == record.event_id:
                if existing.content_key() != record.content_key():
                    raise FillEvidenceConflictError(
                        f"{record.event_id}: 이미 다른 근거가 있음(기존 {existing.price:,}원 / 새 {record.price:,}원)")
                return False
        prefix = ""
        if self.path.exists():
            data = self.path.read_bytes()
            if data and not data.endswith(b"\n"):
                tail = data[data.rfind(b"\n") + 1:]
                try:   # 개행만 빠진 완결된 기록이면 살리고 줄만 바꿈
                    FillEvidenceRecord.from_dict(json.loads(tail.decode("utf-8")))
                    prefix = "\n"
                except Exception:
                    self._quarantine_torn_tail(data)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(prefix + json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True

    def _quarantine_torn_tail(self, data: bytes) -> None:
        """끊긴 마지막 줄을 `<파일>.torn-<시각>`으로 옮기고 원본에서 잘라냄(앞의 완결된 줄은 그대로)."""
        from datetime import datetime
        cut = data.rfind(b"\n") + 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.path.with_name(f"{self.path.name}.torn-{stamp}").write_bytes(data[cut:])
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(data[:cut])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
