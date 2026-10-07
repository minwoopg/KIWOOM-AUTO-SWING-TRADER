from __future__ import annotations

"""설정 파일 ↔ 감시 DB 적용 경계 (W1d-R1·C1·C3) — 잠금·저널·확정 지점·복구.

파일과 SQLite는 한 트랜잭션이 아니므로 다음 순서와 규칙으로 맞춥니다.

설정 적용 잠금 (C1)
- `<watch DB>.config.lock` OS 파일 잠금(Windows msvcrt / 그 밖 fcntl). CLI 편집·apply·status·prepare의 설정 반영과
  상시 실행 관리자의 설정 반영이 **같은 잠금**을 씀. 잠금은 파일 읽기~DB 확정~복원 동안만 — API 조회 중에는 잡지 않음.
- 대기 상한(기본 30초)을 넘기면 ConfigLockTimeout(아무것도 바꾸지 않음). 같은 프로세스 안에서는 다시 들어갈 수 있음.
- 상시 실행 관리자 전체의 중복 기동 잠금과는 다른 잠금.

CLI가 설정을 바꿀 때 (`commit_config_text`)
1. 잠금 → 남은 저널 복구 → 새 원문 사전 검사(형식·목록·보유 보호) — 거부면 파일 그대로.
2. 저널 `<설정>.apply-journal.json` 기록: 원래 파일 내용·해시, 새 해시, 기준 버전, 명령.
3. 작업별 고유 임시 파일 → os.replace로 설정 파일 교체.
4. DB 트랜잭션 한 번(`manager.sync_config`): 시도 기록·청산 기록(USED)·OPEN 정리·위험 자격 — **COMMIT이 확정 지점**.
5. 확정되면 저널 삭제. 확정 전 실패·거부면 원래 파일로 되돌리고 저널 삭제(DB는 자동으로 이전 그대로).
   되돌리기 실패 → 저널을 남기고 REJECTED("복원 실패")를 기록해 신규 진입 차단. 그 기록마저 실패하면 결과 불확정으로
   보고(성공으로 반환하지 않음) — 남은 저널로 다음 실행이 보수적으로 복구.

남은 저널 복구 (`recover_journal`, 잠금 안에서 모든 설정 반영 전에)
| 지금 파일 | DB 사용 중 설정 | 판단 | 처리 |
|---|---|---|---|
| 새 내용 | 사용 중 설정 원문 = 새 내용 | 확정됨(파일·DB 일치) | 저널 삭제 |
| 새 내용 | 그 밖 | 확정 전 중단 | 원래 파일 복원(실패 시 REJECTED 기록·저널 유지) |
| 원래 내용 | — | 교체 전 중단 | 저널 삭제 |
| 둘 다 아님 | — | 그 뒤 사용자가 고침 | 저널 삭제(이 파일은 일반 적용 규칙·보유 보호로 검사) |

복구 결과 분류 (W2 검토 R5)
- 해결됨: COMMITTED(저널 삭제 실패여도 파일·DB 일치) · ROLLED_BACK · NOT_REPLACED · FILE_CHANGED → 일반 적용 계속.
- 미해결(사용자 확인 필요): JOURNAL_UNREADABLE(읽기·JSON 실패) · JOURNAL_INVALID(키·타입·base64·원래 내용 해시 불일치) ·
  RESTORE_FAILED(원래 파일 복원 실패) → **일반 적용·편집을 하지 않음**: 이전 정상 설정 유지, REJECTED(같은 내용이면 한 번만
  기록)로 신규 진입 차단. status·관리자 순회가 저절로 승인하지 않음.
- 해결은 사람이: `watchlist.py restore [--version N]`(저널을 격리하고 정상 버전 원문으로 되돌려 적용) 또는
  `watchlist.py resolve-journal --keep-file`(저널을 격리하고 지금 파일을 일반 규칙·보유 보호로 적용). 격리한 저널은
  `<저널>.<시각>.quarantined`로 남김.
"""

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from infra.watch.manager import (
    ConfigConflict, WatchState, check_text, file_sha, holding_guard, load_state, read_config_text, sync_config,
)
from infra.watch.store import APPLIED, REJECTED, WatchStore

DEFAULT_LOCK_TIMEOUT = 30.0
_HELD: dict[str, list] = {}           # 같은 프로세스 안 재진입: 경로 → [파일 객체, 깊이]


class ConfigLockTimeout(RuntimeError):
    """설정 적용 잠금을 기다리다 시간 초과 — 아무것도 바꾸지 않음."""


def lock_path(wstore: WatchStore) -> Path:
    return Path(wstore.path + ".config.lock")


def _try_lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


@contextmanager
def file_lock(path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT, what: str = "설정 적용",
              sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
    """OS 파일 잠금(프로세스 사이). 같은 프로세스 안에서는 다시 들어감. 프로세스가 죽으면 OS가 풀어 줌."""
    key = str(Path(path).resolve())
    if key in _HELD:
        _HELD[key][1] += 1
        try:
            yield
        finally:
            _HELD[key][1] -= 1
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    if os.name == "nt":
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b"\0")
            f.flush()
    deadline = monotonic() + timeout
    while not _try_lock(f):
        if monotonic() >= deadline:
            f.close()
            raise ConfigLockTimeout(f"{what} 잠금을 {timeout:.0f}초 안에 얻지 못함 — 다른 명령·관리자가 사용 중: {path}")
        sleep(0.1)
    _HELD[key] = [f, 1]
    try:
        yield
    finally:
        del _HELD[key]
        _unlock(f)


def config_lock(wstore: WatchStore, *, timeout: float = DEFAULT_LOCK_TIMEOUT):
    return file_lock(lock_path(wstore), timeout=timeout, what="설정 적용")


# ── 시험용 중단·지연 지점 (환경 변수로만 켜짐 — 운영에서는 아무것도 하지 않음) ─────────
def test_point(name: str) -> None:
    if os.environ.get("WATCH_TEST_CRASH_AT") == name:
        os._exit(97)
    sleep_at = os.environ.get("WATCH_TEST_SLEEP_AT", "")
    if sleep_at.startswith(name + ":"):
        time.sleep(float(sleep_at.split(":", 1)[1]))


# ── 저널 ────────────────────────────────────────────────────
def journal_path(cfg_path: Path) -> Path:
    return Path(cfg_path).with_name(Path(cfg_path).name + ".apply-journal.json")


def _atomic_bytes(path: Path, data: bytes) -> None:
    """작업별 고유 임시 파일 → os.replace (C1 — 고정 .tmp 이름 충돌 방지). 실패하면 임시 파일 정리."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_config_file(path: Path, text: str) -> None:
    _atomic_bytes(Path(path), text.encode("utf-8"))


def _restore(cfg_path: Path, old: bytes | None) -> str | None:
    """원래 파일로 되돌림. 반환: 실패 사유(성공이면 None)."""
    try:
        if old is None:
            Path(cfg_path).unlink(missing_ok=True)
        else:
            _atomic_bytes(Path(cfg_path), old)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _record_failure(wstore: WatchStore, cfg_path: Path, *, now: datetime, origin: str, message: str) -> str | None:
    """REJECTED 시도로 남겨 신규 진입 차단. 반환: 기록 실패 사유(성공이면 None) — C3: 삼키지 않고 돌려줌."""
    try:
        wstore.record_attempt(at=now, origin=origin, source_path=str(cfg_path), raw_sha="APPLY_UNCERTAIN",
                              status=REJECTED, config_hash=None,
                              errors=[{"code": "-", "field": "file", "message": message}], warnings=[],
                              raw_text=None, config=None, list_snapshot_id=None)
        return None
    except Exception as exc:                                   # noqa: BLE001 — 사유를 돌려줌
        return f"{type(exc).__name__}: {exc}"


def _remove(path: Path) -> str | None:
    try:
        Path(path).unlink(missing_ok=True)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


RESOLVED_ACTIONS = ("COMMITTED", "ROLLED_BACK", "NOT_REPLACED", "FILE_CHANGED")
_SHA = re.compile(r"^(MISSING|UNREADABLE(:[0-9a-f]{16})?|[0-9a-f]{16})$")


def _content_sha(data: bytes | None) -> str:
    if data is None:
        return "MISSING"
    try:
        return hashlib.sha256(data.decode("utf-8").encode("utf-8")).hexdigest()[:16]
    except UnicodeDecodeError:
        return "UNREADABLE:" + hashlib.sha256(data).hexdigest()[:16]


def _validate_journal(j) -> tuple[bytes | None, str]:
    """(원래 내용, 오류). 오류가 있으면 근거로 쓰지 않음(JOURNAL_INVALID)."""
    if not isinstance(j, dict):
        return None, "JSON 객체가 아님"
    need = {"origin": str, "started_at": str, "pid": int, "old_sha": str, "new_sha": str}
    for k, t in need.items():
        if not isinstance(j.get(k), t) or isinstance(j.get(k), bool):
            return None, f"{k} 없음·형식 오류"
    ev = j.get("expect_version")
    if not (ev is None or (isinstance(ev, int) and not isinstance(ev, bool))):
        return None, "expect_version 형식 오류"
    if "old_b64" not in j or not (j["old_b64"] is None or isinstance(j["old_b64"], str)):
        return None, "old_b64 없음·형식 오류"
    if not _SHA.match(j["old_sha"]) or not _SHA.match(j["new_sha"]):
        return None, "해시 형식 오류"
    try:
        datetime.fromisoformat(j["started_at"])
        old = None if j["old_b64"] is None else base64.b64decode(j["old_b64"], validate=True)
    except (ValueError, TypeError) as exc:
        return None, f"시각·base64 오류({type(exc).__name__})"
    if _content_sha(old) != j["old_sha"]:
        return None, "원래 내용과 old_sha가 다름"
    return old, ""


def quarantine_journal(cfg_path: Path, now: datetime) -> str | None:
    """남은 저널을 옆으로 치움(사람이 해결할 때). 반환: 격리한 경로(없으면 None)."""
    jp = journal_path(cfg_path)
    if not jp.exists():
        return None
    dest = jp.with_name(f"{jp.name}.{now:%Y%m%d_%H%M%S}.quarantined")
    n = 1
    while dest.exists():
        dest, n = jp.with_name(f"{jp.name}.{now:%Y%m%d_%H%M%S}_{n}.quarantined"), n + 1
    os.replace(jp, dest)
    return str(dest)


def _matches(active: dict | None, new_sha: str) -> bool:
    """사용 중(APPLIED) 설정의 원문이 새 파일 내용과 같음 — 파일과 DB가 일치하므로 파일을 되돌리지 않음."""
    return active is not None and active["raw_sha"] == new_sha


def recover_journal(wstore: WatchStore, cfg_path: Path, *, now: datetime) -> dict | None:
    """남은 저널 처리(잠금 안에서). 반환: 처리 결과 또는 None(저널 없음). 모듈 설명의 표 참고."""
    jp = journal_path(cfg_path)
    if not jp.exists():
        return None
    hint = ("정상 버전으로 되돌리려면 `python tools/watchlist.py restore`, 지금 파일을 확인했으면 "
            "`python tools/watchlist.py resolve-journal --keep-file`")
    try:
        j = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"적용 저널을 읽을 수 없음({type(exc).__name__}) — 일반 적용 중단, 이전 정상 설정 유지. {hint}"
        rec = _record_failure(wstore, cfg_path, now=now, origin="RECOVER", message=msg)
        return {"action": "JOURNAL_UNREADABLE", "resolved": False, "message": msg, "record_error": rec}
    old, why = _validate_journal(j)
    if why:
        msg = f"적용 저널 형식이 맞지 않음({why}) — 근거로 쓰지 않음, 일반 적용 중단·이전 정상 설정 유지. {hint}"
        rec = _record_failure(wstore, cfg_path, now=now, origin="RECOVER", message=msg)
        return {"action": "JOURNAL_INVALID", "resolved": False, "message": msg, "record_error": rec}
    new_sha, old_sha = j["new_sha"], j["old_sha"]
    text, rerr = read_config_text(cfg_path)
    cur = file_sha(text, rerr, Path(cfg_path))
    active = wstore.active_version()
    if cur == new_sha:
        if _matches(active, new_sha):
            err = _remove(jp)               # 삭제 실패여도 파일·DB 일치 — 해결됨(다음 실행이 다시 지움)
            return {"action": "COMMITTED", "resolved": True, "journal": j, "remove_error": err}
        rerr2 = _restore(cfg_path, old)
        if rerr2 is None:
            err = _remove(jp)
            return {"action": "ROLLED_BACK", "resolved": True, "journal": j, "remove_error": err,
                    "message": f"확정 전에 중단된 적용({j.get('origin')})을 되돌림 — 원래 설정 파일 복원"}
        msg = (f"중단된 적용({j.get('origin')})의 설정 파일을 되돌리지 못함({rerr2}) — 파일과 사용 중 설정이 다름, "
               f"일반 적용 중단·이전 정상 설정 유지. {hint}")
        rec = _record_failure(wstore, cfg_path, now=now, origin="RECOVER", message=msg)
        return {"action": "RESTORE_FAILED", "resolved": False, "journal": j, "message": msg, "record_error": rec}
    err = _remove(jp)
    return {"action": "NOT_REPLACED" if cur == old_sha else "FILE_CHANGED", "resolved": True, "journal": j,
            "remove_error": err}


def _blocked_by_journal(wstore: WatchStore, rec: dict) -> tuple[WatchState, dict]:
    """미해결 저널 — 일반 적용을 하지 않고 지금 상태(이전 정상 설정 + REJECTED)를 돌려줌."""
    state = load_state(wstore)
    state.journal_block = rec["action"]
    latest = state.latest
    return state, {"version": None if latest is None else latest["version"], "status": REJECTED, "new": False,
                   "errors": [{"code": "-", "field": "journal", "message": rec["message"]}], "warnings": [],
                   "blocked_by_journal": rec["action"], "record_error": rec.get("record_error")}


# ── 일반 설정 반영 (apply·status·prepare·관리자) ─────────────────
def sync_file(wstore: WatchStore, cfg_path: Path, listing, snapshot_id, *, now: Callable[[], datetime],
              origin: str = "FILE", lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> tuple[WatchState, dict, dict | None]:
    """잠금 → 남은 저널 복구 → 파일을 읽어 적용(sync_config). 반환 (상태, 시도, 복구 결과).
    복구가 미해결이면 적용하지 않음(R5) — 이전 정상 설정 유지, 시도는 REJECTED(같은 내용이면 새 행 없음)."""
    with config_lock(wstore, timeout=lock_timeout):
        rec = recover_journal(wstore, Path(cfg_path), now=now())
        if rec is not None and not rec["resolved"]:
            state, att = _blocked_by_journal(wstore, rec)
            return state, att, rec
        state, att = sync_config(wstore, cfg_path, listing, snapshot_id, now=now(), origin=origin)
    return state, att, rec


def resolve_journal_keep_file(wstore: WatchStore, cfg_path: Path, listing, snapshot_id, *,
                              now: Callable[[], datetime], lock_timeout: float = DEFAULT_LOCK_TIMEOUT
                              ) -> tuple[WatchState, dict, str | None]:
    """사람이 지금 파일을 확인했다고 할 때: 저널을 격리하고 파일을 일반 규칙(검증·보유 보호)으로 적용."""
    with config_lock(wstore, timeout=lock_timeout):
        q = quarantine_journal(Path(cfg_path), now())
        state, att = sync_config(wstore, cfg_path, listing, snapshot_id, now=now(), origin="CLI:resolve-journal")
    return state, att, q


# ── CLI 변경 적용 ──────────────────────────────────────────
@dataclass
class CommitResult:
    code: int                         # 0 확정 / 2 거부·실패(이전 설정 유지 또는 결과 불확정)
    outcome: str                      # APPLIED / REJECTED_PRECHECK / CONFLICT / REJECTED_RESTORED / FAILED_RESTORED /
                                      # RESTORE_FAILED / UNCERTAIN
    state: WatchState | None = None
    att: dict | None = None
    errors: list = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    recovered: dict | None = None


def commit_config_text(wstore: WatchStore, cfg_path: Path, text: str, listing, snapshot_id, *,
                       now: Callable[[], datetime], origin: str, close_codes: list[str] | None = None,
                       lock_timeout: float = DEFAULT_LOCK_TIMEOUT, quarantine_unresolved: bool = False,
                       hook: Callable[[str], None] = test_point) -> CommitResult:
    """바꾼 원문을 적용. close_codes = 이 명령이 청산하는 종목(holding-close) — 보유 값은 잠금 안에서 사용 중 설정에서 읽음.
    모듈 설명의 순서·규칙. 확정(DB COMMIT) 전 실패는 모두 이전 설정 유지 + 원래 파일 복원."""
    cfg_path = Path(cfg_path)
    with config_lock(wstore, timeout=lock_timeout):
        rec = recover_journal(wstore, cfg_path, now=now())
        if rec is not None and not rec["resolved"]:
            if not quarantine_unresolved:
                return CommitResult(2, "JOURNAL_UNRESOLVED", recovered=rec,
                                    messages=["미해결 적용 저널이 있어 바꾸지 않았습니다 — restore 또는 "
                                              "resolve-journal --keep-file로 먼저 해결"])
            rec = {**rec, "quarantined": quarantine_journal(cfg_path, now())}   # restore: 사람이 정상 버전을 고름
        before = load_state(wstore)
        held = {} if before.config is None else {s.code: s for s in before.config.symbols if s.holding is not None}
        from dataclasses import asdict
        close_holdings = {c: asdict(held[c].holding) for c in (close_codes or []) if c in held}
        res = check_text(text, listing)
        if not res.ok:
            return CommitResult(2, "REJECTED_PRECHECK", errors=[vars(e) for e in res.errors], recovered=rec,
                                messages=["바꾼 결과가 검증을 통과하지 못해 파일을 바꾸지 않았습니다"])
        guard, _ = holding_guard(before.config, before.active_version, res.config, close_holdings=close_holdings)
        if guard:
            return CommitResult(2, "REJECTED_PRECHECK", errors=[vars(e) for e in guard], recovered=rec,
                                messages=["보유 보호 — 파일을 바꾸지 않았습니다"])
        try:
            old = cfg_path.read_bytes() if cfg_path.is_file() else None
        except OSError as exc:
            return CommitResult(2, "FAILED_RESTORED", recovered=rec,
                                messages=[f"지금 설정 파일을 읽지 못해 바꾸지 않았습니다: {type(exc).__name__}: {exc}"])
        old_text, old_err = read_config_text(cfg_path)
        old_sha = file_sha(old_text, old_err, cfg_path) if old is not None else "MISSING"
        new_sha = file_sha(text, None, cfg_path)
        started = now()
        journal = {"origin": origin, "started_at": started.replace(microsecond=0).isoformat(timespec="seconds"),
                   "pid": os.getpid(), "expect_version": before.active_version, "old_sha": old_sha,
                   "new_sha": new_sha, "old_b64": None if old is None else base64.b64encode(old).decode("ascii")}
        jp = journal_path(cfg_path)
        try:
            _atomic_bytes(jp, json.dumps(journal, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            return CommitResult(2, "FAILED_RESTORED", recovered=rec,
                                messages=[f"적용 저널을 쓰지 못해 바꾸지 않았습니다: {type(exc).__name__}: {exc}"])
        hook("after_journal")
        replaced = False
        try:
            write_config_file(cfg_path, text)
            replaced = True
            hook("after_replace")
            state, att = sync_config(wstore, cfg_path, listing, snapshot_id, now=now(), origin=origin,
                                     close_holdings=close_holdings or None, expect_version=before.active_version,
                                     hook=hook)
        except BaseException as exc:
            r = _after_failure(wstore, cfg_path, old, jp, replaced=replaced, now=now, origin=origin,
                               cause=f"{type(exc).__name__}: {exc}", new_sha=new_sha, started=journal["started_at"],
                               conflict=isinstance(exc, ConfigConflict))
            r.recovered = rec
            if not isinstance(exc, Exception):
                raise
            return r
        hook("after_commit")
        if att["status"] != APPLIED:
            r = _after_failure(wstore, cfg_path, old, jp, replaced=True, now=now, origin=origin,
                               cause=f"v{att['version']} REJECTED", new_sha=new_sha, started=journal["started_at"],
                               rejected=True)
            r.state, r.att, r.errors, r.recovered = state, att, att["errors"], rec
            return r
        msgs = []
        err = _remove(jp)
        if err:
            msgs.append(f"적용은 확정됐지만 저널을 지우지 못함({err}) — 다음 실행이 확정으로 확인하고 지움")
        return CommitResult(0, APPLIED, state, att, messages=msgs, recovered=rec)


def _after_failure(wstore: WatchStore, cfg_path: Path, old: bytes | None, jp: Path, *, replaced: bool, now, origin,
                   cause: str, new_sha: str, started: str, rejected: bool = False,
                   conflict: bool = False) -> CommitResult:
    """확정 전 실패·거부 뒤 정리. 각 단계의 성공/실패를 그대로 보고 (C3)."""
    msgs = []
    # DB가 실제로 확정됐는지 다시 확인(COMMIT 중 예외 등) — 확정됐으면 되돌리지 않음
    try:
        committed = not rejected and replaced and _matches(wstore.active_version(), new_sha)
    except Exception as exc:                                   # noqa: BLE001
        committed = None
        msgs.append(f"DB 확정 여부를 확인하지 못함({type(exc).__name__}: {exc})")
    if committed:
        err = _remove(jp)
        return CommitResult(0, APPLIED, messages=[f"적용 뒤 예외({cause})가 있었지만 DB 확정을 확인함"]
                            + ([f"저널 삭제 실패({err})"] if err else []))
    outcome = "CONFLICT" if conflict else ("REJECTED_RESTORED" if rejected else "FAILED_RESTORED")
    head = "적용 거부" if rejected else ("다른 적용과 충돌" if conflict else "적용 실패")
    msgs.insert(0, f"{head}({cause}) — 이전 사용 중 설정·보유 감시 유지, 청산 기록 없음")
    restore_err = _restore(cfg_path, old) if replaced else None
    if restore_err is None and committed is not None:
        err = _remove(jp)
        msgs.append("원래 설정 파일로 되돌림" if replaced else "설정 파일은 바뀌지 않음")
        if err:
            msgs.append(f"저널 삭제 실패({err}) — 다음 실행이 원래 내용임을 확인하고 지움")
        return CommitResult(2, outcome, messages=msgs)
    if restore_err is None:
        msgs.append("원래 설정 파일로 되돌림 — DB 확정 여부 미확인이라 저널을 남김(다음 실행이 확인)")
        return CommitResult(2, "UNCERTAIN", messages=msgs)
    msg = (f"설정 파일을 원래대로 되돌리지 못함({restore_err}) — 원인 {cause}. 파일과 사용 중 설정이 다름: "
           "파일을 확인한 뒤 apply 또는 restore")
    msgs.append(msg)
    rec_err = _record_failure(wstore, cfg_path, now=now(), origin=f"{origin} (복원 실패)", message=msg)
    if rec_err is None:
        msgs.append("REJECTED(복원 실패)로 기록 — 신규 진입 차단. 저널을 남겨 다음 실행이 다시 복구")
        return CommitResult(2, "RESTORE_FAILED", messages=msgs)
    msgs.append(f"복원 실패 기록도 못 함({rec_err}) — 결과 불확정. 저널을 남겨 다음 실행이 보수적으로 복구")
    return CommitResult(2, "UNCERTAIN", messages=msgs)
