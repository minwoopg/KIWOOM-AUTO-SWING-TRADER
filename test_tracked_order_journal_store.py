# -*- coding: utf-8 -*-
"""TrackedOrderJournalStore 단위 회귀 테스트 (스윙 분리 1라운드, 2026-09-28).

단타 레포 `test_tracked_order_journal.py`(bdde6c2)의 1~5절(저장소 자체 검증:
생성 시 방어적 검증, 재시작 후 왕복, 원자적 쓰기, 손상 파일 fail-close,
민감정보 없음)을 **그대로** 옮겼습니다. 6~12절(TradingService 훅 통합)은
주문 실행부(OrderExecutor)를 추출하는 다음 라운드에서 새 구조 기준으로
다시 작성합니다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from infra.storage.tracked_order_journal import (
    SCHEMA_VERSION,
    TrackedOrderJournalCorruptError,
    TrackedOrderJournalStore,
    TrackedOrderRecord,
)

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def _record(symbol="005930", side="BUY", order_id="0012345", base=0, target=10,
            accepted_at=None, lifecycle_kind=None) -> TrackedOrderRecord:
    return TrackedOrderRecord(
        symbol=symbol, side=side, order_id=order_id,
        base_quantity_before_order=base, target_quantity_after_order=target,
        accepted_at=accepted_at or datetime.now(),
        lifecycle_kind=lifecycle_kind or (f"{side}_PENDING"),
    )


# ══════════════════════════════════════════════════════════════
# 1. TrackedOrderRecord — 생성 시점 방어적 검증
# ══════════════════════════════════════════════════════════════
check("1-1) 정상 레코드는 정상 생성됨", _record().order_id == "0012345")
try:
    _record(order_id="")
    check("1-2) 빈 order_id는 생성 자체를 거부함(ValueError)", False)
except ValueError:
    check("1-2) 빈 order_id는 생성 자체를 거부함(ValueError)", True)
try:
    _record(order_id="   ")
    check("1-3) 공백만 있는 order_id도 거부함", False)
except ValueError:
    check("1-3) 공백만 있는 order_id도 거부함", True)
try:
    TrackedOrderRecord(
        symbol="005930", side="HOLD", order_id="123",
        base_quantity_before_order=0, target_quantity_after_order=10,
        accepted_at=datetime.now(), lifecycle_kind="BUY_PENDING",
    )
    check("1-4) side가 BUY/SELL이 아니면 거부함", False)
except ValueError:
    check("1-4) side가 BUY/SELL이 아니면 거부함", True)
try:
    TrackedOrderRecord(
        symbol="005930", side="BUY", order_id="123",
        base_quantity_before_order=0, target_quantity_after_order=10,
        accepted_at=datetime.now(), lifecycle_kind="ORPHAN",
    )
    check("1-5) lifecycle_kind는 BUY_PENDING/SELL_PENDING만 허용함", False)
except ValueError:
    check("1-5) lifecycle_kind는 BUY_PENDING/SELL_PENDING만 허용함", True)
check("1-6) schema_version 기본값이 코드의 SCHEMA_VERSION과 일치",
      _record().schema_version == SCHEMA_VERSION)


# ══════════════════════════════════════════════════════════════
# 2. Store — 쓰기/읽기 왕복, "재시작"(새 객체) 후에도 기록이 살아있음
# ══════════════════════════════════════════════════════════════
root2 = tempfile.mkdtemp()
path2 = f"{root2}/tracked_order_journal.json"
store2a = TrackedOrderJournalStore(path2)
check("2-1) 파일이 없으면 load_all()이 빈 dict", store2a.load_all() == {})
rec2 = _record(symbol="005930", side="BUY", order_id="0011111",
                base=0, target=10, lifecycle_kind="BUY_PENDING")
store2a.upsert(rec2)
check("2-2) upsert 직후 같은 객체에서 조회됨",
      store2a.get("005930") is not None and store2a.get("005930").order_id == "0011111")

# "프로세스 재시작"을 새 TrackedOrderJournalStore 객체로 시뮬레이션 —
# TradingService 자체는 startup에 journal을 자동으로 읽지 않으므로
# (E.1-A 범위 밖) 이 계층에서 직접 검증합니다.
store2b = TrackedOrderJournalStore(path2)
loaded2b = store2b.load_all()
check("2-3) 새 객체(재시작 시뮬레이션)로 같은 경로를 읽어도 레코드가 그대로 로드됨",
      "005930" in loaded2b and loaded2b["005930"].order_id == "0011111")
check("2-4) 로드된 레코드의 필드가 전부 원본과 일치",
      loaded2b["005930"].base_quantity_before_order == 0
      and loaded2b["005930"].target_quantity_after_order == 10
      and loaded2b["005930"].lifecycle_kind == "BUY_PENDING"
      and loaded2b["005930"].first_fill_at is None
      and loaded2b["005930"].orphaned_at is None)

# 두 번째 종목 추가 — 기존 종목 레코드를 덮어쓰지 않는지(다건 저장 정합성)
rec2c = _record(symbol="000660", side="SELL", order_id="0022222",
                 base=50, target=0, lifecycle_kind="SELL_PENDING")
store2b.upsert(rec2c)
loaded2c = TrackedOrderJournalStore(path2).load_all()
check("2-5) 두 번째 종목 추가 후에도 첫 번째 종목 레코드가 그대로 있음",
      "005930" in loaded2c and "000660" in loaded2c)
check("2-6) 두 레코드가 서로 다른 side/order_id를 정확히 유지",
      loaded2c["005930"].side == "BUY" and loaded2c["000660"].side == "SELL")

store2b.remove("005930")
loaded2d = TrackedOrderJournalStore(path2).load_all()
check("2-7) remove() 후 해당 종목만 사라지고 나머지는 유지됨",
      "005930" not in loaded2d and "000660" in loaded2d)
store2b.remove("005930")  # 이미 없는 종목 재삭제 — idempotent해야 함
check("2-8) 존재하지 않는 종목 remove()는 예외 없이 통과(idempotent)", True)

# first_fill_at / orphaned_at 갱신 후 왕복
rec2e = store2b.get("000660")
rec2e.first_fill_at = datetime.now() - timedelta(seconds=30)
rec2e.orphaned_at = datetime.now()
store2b.upsert(rec2e)
loaded2f = TrackedOrderJournalStore(path2).get("000660")
check("2-9) first_fill_at/orphaned_at 갱신 후에도 재로드 시 값이 보존됨",
      loaded2f.first_fill_at is not None and loaded2f.orphaned_at is not None)


# ══════════════════════════════════════════════════════════════
# 3. Store — 원자적 쓰기: fsync 실패 허용, os.replace 실패는 원본 보호
# ══════════════════════════════════════════════════════════════
root3 = tempfile.mkdtemp()
path3 = f"{root3}/tracked_order_journal.json"
store3 = TrackedOrderJournalStore(path3)
store3.upsert(_record(symbol="005930", order_id="0033333"))

# fsync가 실패해도(예: Windows에서 재현된 사례, export_daily_bundle.py
# 1I.5와 동일 판단) 쓰기 자체는 성공해야 함.
_orig_fsync = os.fsync
os.fsync = lambda fd: (_ for _ in ()).throw(OSError(9, "Bad file descriptor"))
try:
    store3.upsert(_record(symbol="000660", order_id="0044444"))
    fsync_survived = True
finally:
    os.fsync = _orig_fsync
check("3-1) fsync 실패해도 upsert()가 예외 없이 완료됨", fsync_survived)
check("3-2) fsync 실패 후에도 두 레코드 모두 정상 조회됨",
      store3.get("005930") is not None and store3.get("000660") is not None)
check("3-3) fsync 실패 후 남은 .tmp 파일이 없음",
      not list(__import__("pathlib").Path(root3).glob("*.tmp")))

# os.replace 자체가 실패하면(디스크 풀 등을 흉내) 원본 파일이 훼손되지
# 않아야 하고, .tmp도 정리돼야 함.
before_bytes = open(path3, "rb").read()
_orig_replace = os.replace
os.replace = lambda *a, **kw: (_ for _ in ()).throw(OSError(28, "No space left on device"))
try:
    try:
        store3.upsert(_record(symbol="233740", order_id="0055555"))
        replace_raised = False
    except OSError:
        replace_raised = True
finally:
    os.replace = _orig_replace
after_bytes = open(path3, "rb").read()
check("3-4) os.replace 실패는 예외로 그대로 전파됨(조용히 삼키지 않음)", replace_raised)
check("3-5) os.replace 실패 후에도 기존 파일 내용이 한 바이트도 안 바뀜(원자성)",
      before_bytes == after_bytes)
check("3-6) os.replace 실패 후 .tmp 파일이 남지 않음(정리됨)",
      not list(__import__("pathlib").Path(root3).glob("*.tmp")))
check("3-7) os.replace 실패 후에도 기존 두 레코드는 그대로 읽힘",
      set(TrackedOrderJournalStore(path3).load_all().keys()) == {"005930", "000660"})


# ══════════════════════════════════════════════════════════════
# 4. Store — 손상된 파일 / schema_version 불일치 → fail-close
# ══════════════════════════════════════════════════════════════
root4 = tempfile.mkdtemp()
path4a = f"{root4}/broken1.json"
open(path4a, "w", encoding="utf-8").write("{ 이건 유효한 JSON이 아님 ][")
store4a = TrackedOrderJournalStore(path4a)
try:
    store4a.load_all()
    check("4-1) JSON 파싱 실패 시 TrackedOrderJournalCorruptError 발생", False)
except TrackedOrderJournalCorruptError:
    check("4-1) JSON 파싱 실패 시 TrackedOrderJournalCorruptError 발생", True)
try:
    store4a.upsert(_record())
    check("4-1b) 손상된 파일 위에 upsert()해도 조용히 덮어쓰지 않고 예외를 냄", False)
except TrackedOrderJournalCorruptError:
    check("4-1b) 손상된 파일 위에 upsert()해도 조용히 덮어쓰지 않고 예외를 냄", True)

path4b = f"{root4}/broken2.json"
json.dump({"schema_version": 999, "records": {}}, open(path4b, "w", encoding="utf-8"))
store4b = TrackedOrderJournalStore(path4b)
try:
    store4b.load_all()
    check("4-2) schema_version 불일치 시 TrackedOrderJournalCorruptError 발생", False)
except TrackedOrderJournalCorruptError:
    check("4-2) schema_version 불일치 시 TrackedOrderJournalCorruptError 발생", True)

path4c = f"{root4}/broken3.json"
json.dump({"schema_version": SCHEMA_VERSION, "records": "이것도 dict가 아님"},
          open(path4c, "w", encoding="utf-8"))
try:
    TrackedOrderJournalStore(path4c).load_all()
    check("4-3) records가 dict가 아니면 fail-close", False)
except TrackedOrderJournalCorruptError:
    check("4-3) records가 dict가 아니면 fail-close", True)

path4d = f"{root4}/broken4.json"
json.dump(
    {"schema_version": SCHEMA_VERSION,
     "records": {"005930": {"symbol": "005930", "side": "BUY"}}},  # 필수 필드 대부분 누락
    open(path4d, "w", encoding="utf-8"),
)
try:
    TrackedOrderJournalStore(path4d).load_all()
    check("4-4) 개별 레코드 필드 누락도 fail-close", False)
except TrackedOrderJournalCorruptError:
    check("4-4) 개별 레코드 필드 누락도 fail-close", True)

check("4-5) 손상 감지 예외들이 전부 이 모듈 전용 타입(범용 Exception을 넓게 잡지 않게)",
      issubclass(TrackedOrderJournalCorruptError, Exception))


# ══════════════════════════════════════════════════════════════
# 5. Store — 민감정보 없음
# ══════════════════════════════════════════════════════════════
root5 = tempfile.mkdtemp()
path5 = f"{root5}/tracked_order_journal.json"
store5 = TrackedOrderJournalStore(path5)
store5.upsert(_record(symbol="005930", order_id="0099999"))
raw5_text = open(path5, encoding="utf-8").read().lower()
_sensitive_markers = (
    "account", "계좌", "token", "appkey", "app_key", "secretkey",
    "secret_key", "password", "passwd", "bearer",
)
check("5-1) 저장된 journal 파일에 계좌/토큰/시크릿 관련 키가 전혀 없음",
      not any(marker in raw5_text for marker in _sensitive_markers))
check("5-2) TrackedOrderRecord 필드 자체에도 그런 이름의 필드가 없음",
      not any(marker in f.lower()
              for f in TrackedOrderRecord.__dataclass_fields__.keys()
              for marker in _sensitive_markers))


print()
print(f"[최종] 총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
