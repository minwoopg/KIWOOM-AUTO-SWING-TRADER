from __future__ import annotations

"""주문 경로 공용 보호 (M0 커밋 2, GPT 지시서 2026-10-13).

1. 계좌별 단일 주문 실행기 잠금 — `app.main`·M1 수동 주문 도구·원장 정정 도구가 같은 계좌 라벨로 동시에 돌지 않게.
   (같은 데이터 폴더 기준. 다른 폴더에서 같은 계좌를 쓰는 것은 운영 원칙으로 금지 — 폴더 밖 전역 잠금은 두지 않음)
2. 데이터 폴더의 실행 모드 표식(`execution_mode.json`) — 로컬 가짜 브로커(MOCK_LOCAL)로 쌓인 원장·상태를 키움 모의투자
   (KIWOOM_PAPER) 계좌의 것처럼 쓰지 않게. 모드·계좌 라벨이 바뀌면 원장이 비어 있고 미해결 주문이 없을 때만 전환을 기록하고,
   아니면 시작 거부(사람이 데이터 폴더를 정리해야 함 — 자동 이관 없음).
"""

import json
import os
import re
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from infra.storage.process_lock import single_instance_lock

MARKER_NAME = "execution_mode.json"


class DataModeMismatchError(RuntimeError):
    """데이터 폴더가 다른 실행 모드·계좌 라벨에서 쓰였고, 원장·미해결 주문이 남아 있음."""


def _scope(settings) -> str:
    return settings.broker.account_scope_id.strip() or "unscoped"


def _data_dir(settings) -> Path:
    return Path(settings.storage.state_file).resolve().parent


@contextmanager
def order_path_locks(settings):
    """상태 파일 잠금(기존 app.main과 같은 파일) + 계좌 라벨 잠금. 하나라도 잡혀 있으면 RuntimeError."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", _scope(settings))
    with ExitStack() as st:
        st.enter_context(single_instance_lock(Path(settings.storage.state_file).with_suffix(".lock")))
        st.enter_context(single_instance_lock(_data_dir(settings) / f"order_executor_{safe}.lock"))
        yield


def marker_path(settings) -> Path:
    return _data_dir(settings) / MARKER_NAME


def read_marker(settings) -> dict | None:
    p = marker_path(settings)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataModeMismatchError(f"{p}: 읽을 수 없음 — {exc} (사람 확인 필요)") from exc
    if not isinstance(d, dict) or "mode" not in d or "account_scope" not in d:
        raise DataModeMismatchError(f"{p}: 형식 아님(mode·account_scope 없음)")
    return d


def decide_data_mode(settings, mode: str, *, ledger_empty: bool, unresolved: bool) -> str:
    """'CREATE' / 'SAME' / 'SWITCH'. 전환할 수 없으면 DataModeMismatchError. 파일은 쓰지 않음."""
    cur = read_marker(settings)
    if cur is None:
        return "CREATE"
    if cur["mode"] == mode and cur["account_scope"] == _scope(settings):
        return "SAME"
    if ledger_empty and not unresolved:
        return "SWITCH"
    raise DataModeMismatchError(
        f"데이터 폴더는 {cur['mode']}·{cur['account_scope']}에서 쓰였는데 지금은 {mode}·{_scope(settings)} — "
        f"원장 {'비어 있음' if ledger_empty else '기록 있음'}, 미해결 주문 {'있음' if unresolved else '없음'}. "
        f"섞지 않음: 원래 모드로 되돌리거나 데이터 폴더를 사람이 정리한 뒤 다시 시작")


def commit_data_mode(settings, mode: str, action: str, *, now: datetime | None = None) -> Path:
    """decide_data_mode 결과대로 표식 기록(원자적). SAME이면 쓰지 않음."""
    p = marker_path(settings)
    if action == "SAME":
        return p
    now = now or datetime.now()
    prev = read_marker(settings) if action == "SWITCH" else None
    history = list((prev or {}).get("history", []))
    if prev:
        history.append({"mode": prev["mode"], "account_scope": prev["account_scope"],
                        "since": prev.get("since"), "until": now.isoformat(timespec="seconds")})
    d = {"mode": mode, "account_scope": _scope(settings), "since": now.isoformat(timespec="seconds"),
         "history": history}
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return p
