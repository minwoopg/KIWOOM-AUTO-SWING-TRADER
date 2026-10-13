from __future__ import annotations

"""매매 실행기 상태 파일과 신규 매수 중단 명령 (M3 최소판, 대시보드 연결용).

1. `data/runner_status.json` — 실행기(`app.main`)가 몇 초마다 덮어씀: run_id·pid·heartbeat·단계·실행 모드·코드 버전·
   전략 연결 여부·신규 매수 중단 반영 상태·마지막 잔고 시각·보유·추적 주문·요약 카운터. 대시보드는 이 파일을 **읽기만** 함.
   정보용이라 fsync하지 않음(원장·상태 파일과 다름) — 깨지면 대시보드가 '읽기 실패'로 표시.
2. `commands/halt_new_buys.json` — 신규 매수 중단 요청. 누구든(대시보드·CLI) **없을 때만** 만들고(같은 요청 중복 생성 없음),
   실행기가 다음 폴링에서 읽어 반영(`applied_at`을 상태 파일에). 매도·손절·미해결 주문 확인은 계속. 자동 재개 없음 —
   재개는 `python tools/buy_halt.py resume --confirm`(사람)만, 요청 파일을 `commands/processed/`로 옮기고 기록.
"""

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

HALT_FILE = "halt_new_buys.json"
STATUS_FILE = "runner_status.json"


def status_path(settings) -> Path:
    return Path(settings.storage.state_file).resolve().parent / STATUS_FILE


def halt_path(commands_dir: str | Path) -> Path:
    return Path(commands_dir) / HALT_FILE


def _atomic_write(path: Path, data: dict, *, fsync: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_status(path: Path, data: dict) -> None:
    _atomic_write(Path(path), data, fsync=False)


def read_json(path: Path) -> tuple[str, dict | None, str]:
    """('OK'|'MISSING'|'UNREADABLE', 값, 오류)."""
    p = Path(path)
    if not p.exists():
        return "MISSING", None, ""
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(d, dict):
            return "UNREADABLE", None, "JSON 객체가 아님"
        return "OK", d, ""
    except (OSError, ValueError) as exc:
        return "UNREADABLE", None, f"{type(exc).__name__}: {exc}"


def request_buy_halt(commands_dir: str | Path, *, reason: str, by: str, now: datetime) -> tuple[bool, dict]:
    """(새로 만들었는지, 요청 내용). 이미 요청이 있으면 그대로 돌려줌 — 중복 클릭이 새 요청을 만들지 않음."""
    p = halt_path(commands_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    req = {"request_id": f"HALT-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}", "requested_at": now.isoformat(timespec="seconds"),
           "requested_by": by, "reason": (reason or "").strip() or "사유 미기재"}
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL)       # 없을 때만 — 경합해도 하나만 생김
    except FileExistsError:
        st, cur, _ = read_json(p)
        return False, cur if st == "OK" else {"request_id": "?", "unreadable": True}
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(req, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    return True, req


def read_buy_halt(commands_dir: str | Path) -> tuple[str, dict | None]:
    """('NONE'|'ACTIVE'|'UNREADABLE', 요청). 읽을 수 없는 요청 파일도 '중단'으로 취급(안전 쪽)."""
    st, d, err = read_json(halt_path(commands_dir))
    if st == "MISSING":
        return "NONE", None
    if st != "OK" or not d.get("request_id"):
        return "UNREADABLE", {"request_id": "UNREADABLE", "error": err or "request_id 없음"}
    return "ACTIVE", d


def resume_buy_halt(commands_dir: str | Path, *, note: str, now: datetime) -> dict | None:
    """사람이 재개: 요청 파일을 processed/로 옮기며 재개 기록을 붙임. 없으면 None."""
    p = halt_path(commands_dir)
    if not p.exists():
        return None
    st, d, _ = read_json(p)
    rec = dict(d or {"request_id": "UNREADABLE"})
    rec.update({"resumed_at": now.isoformat(timespec="seconds"), "resume_note": note})
    dst = Path(commands_dir) / "processed" / f"halt_new_buys.{now:%Y%m%d_%H%M%S}.json"
    _atomic_write(dst, rec, fsync=True)
    p.unlink()
    return rec
