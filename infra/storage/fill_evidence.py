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


# ── C4(GPT R3): 저장 대기 근거 spool ─────────────────────────────

class EvidenceSpoolCorruptError(RuntimeError):
    pass


def spool_path(fill_ledger_file: str | Path) -> Path:
    return Path(fill_ledger_file).with_name("fill_evidence_spool.jsonl")


class EvidenceSpool:
    """체결조회로 **이미 확인한** 근거가 근거 파일 저장에 실패했을 때의 내구 대기열 (append-only JSON Lines).

    op: PENDING(근거·시도 횟수·다음 시각·오류) / DONE(저장 성공·이미 있음·충돌로 사람 확인) / GAVE_UP(이번 실행 재시도 한도).
    사건의 상태 = 마지막 줄. PENDING·GAVE_UP은 미해결 — 재시작하면 다시 조회하지 않고 저장부터 재개, 날짜가 바뀌어도 버리지 않음.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def put(self, record: FillEvidenceRecord, *, attempts: int, next_at: str, error: str, at: str) -> None:
        self._append({"op": "PENDING", "event_id": record.event_id, "order_id": record.order_id,
                      "record": record.to_dict(), "attempts": attempts, "next_at": next_at, "error": error[:300],
                      "at": at})

    def mark(self, event_id: str, op: str, *, note: str = "", at: str) -> None:
        self._append({"op": op, "event_id": event_id, "note": note[:300], "at": at})

    def unresolved(self) -> dict[str, dict]:
        """event_id → 마지막 PENDING 줄(+ 마지막 op). DONE으로 끝난 사건은 없음."""
        if not self.path.exists():
            return {}
        last: dict[str, dict] = {}
        pending: dict[str, dict] = {}
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get("op") not in ("PENDING", "DONE", "GAVE_UP") \
                        or not rec.get("event_id"):
                    raise ValueError("op·event_id 없음")
                if rec["op"] == "PENDING":
                    FillEvidenceRecord.from_dict(rec["record"])
            except (ValueError, KeyError, TypeError) as exc:
                raise EvidenceSpoolCorruptError(f"{self.path.name}:{i} 읽을 수 없는 줄: {exc}") from exc
            last[rec["event_id"]] = rec
            if rec["op"] == "PENDING":
                pending[rec["event_id"]] = rec
        return {eid: {**pending[eid], "last_op": r["op"]} for eid, r in last.items()
                if r["op"] != "DONE" and eid in pending}
