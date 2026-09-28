from __future__ import annotations

"""앱 로그, 거래 로그, 포지션 상태머신 로그를 관리하는 모듈.

2026-09-28 (스윙 분리 1라운드): 단타 레포 `infra/storage/logger.py`(bdde6c2)에서
`build_app_logger`, `TradeCsvLogger`, `_migrate_csv_header_if_needed`,
`PositionLifecycleLogger`만 남겼습니다. signal_log 및 shadow 로거 7종은
단타 판단 관측용이라 제외했습니다.
"""

import csv
import logging
import logging.handlers
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AppLogger = logging.Logger

# 단일 파일 최대 20MB, 최근 10개(최대 200MB)까지만 보관 후 압축 없이 순환.
# 기존엔 FileHandler 하나로 무한정 append만 해서 200MB까지 불어난 상태였음.
APP_LOG_MAX_BYTES = 20 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 10


def build_app_logger(log_file: str, level: str = "INFO") -> AppLogger:
    """파일 기반 앱 로거를 생성합니다.

    프로젝트 전체 로그(app_logger + infra.* / domain.* 모듈 로거)를
    하나의 app.log로 모으기 위해, 파일 핸들러를 루트 로거에 붙입니다.
    그동안 condition_watcher / kiwoom_ws 의 [COND]/[WS] 로그가
    app.log에 안 찍히던 원인을 해결합니다.
    """

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    # 루트 로거에 파일 핸들러를 붙여 모든 하위 로거의 로그를 한곳에 모음
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    target_path = str(Path(log_file).resolve())
    already_has_file = any(
        isinstance(h, (logging.FileHandler, logging.handlers.RotatingFileHandler))
        and getattr(h, "baseFilename", "") == target_path
        for h in root_logger.handlers
    )
    if not already_has_file:
        root_file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=APP_LOG_MAX_BYTES,
            backupCount=APP_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        root_file_handler.setFormatter(formatter)
        root_logger.addHandler(root_file_handler)
        # 콘솔에는 로그를 흘리지 않음 — 전량 출력은 터미널을 뒤덮어 오히려
        # 실행 확인을 방해함. 실행 확인은 main.py의 시작 배너(print)로 대신함.

        # 외부 라이브러리의 과도한 로그는 억제
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    # app_logger는 별도 핸들러 없이 루트 핸들러로 전파(propagate)시켜 중복 방지
    logger = logging.getLogger("swing_auto_trader")
    logger.setLevel(level.upper())
    logger.handlers.clear()      # 기존에 직접 붙인 핸들러 제거 (중복 방지)
    logger.propagate = True

    return logger


# ── trades.csv ───────────────────────────────────────────────────────────────
# 기존 필드 + 매수 당시 판단 근거 컨텍스트 필드

TRADE_FIELDS = [
    # 기본 필드 (단타 레포와 동일 순서 — 앞 8개는 변경 금지)
    "timestamp", "symbol", "side", "quantity", "price", "accepted", "message", "order_id",
    # 컨텍스트 필드 — 단타 전용 분봉 패턴 컬럼(V/PR/VWAP 등)은 제외.
    # 스윙 전용 컬럼은 스윙 로직 라운드에서 추가합니다.
    "entry_strategy",        # 전략명
    "entry_reason",          # 진입 사유 요약
    "exit_reason",           # 매도 사유
    "avg_buy_price",         # 잔고API 기준 평균매입단가 (손익 계산용)
]


class TradeCsvLogger:
    """주문/체결 결과를 CSV 파일로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """거래 로그 한 줄을 CSV 파일에 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            row.setdefault("price", 0)
            # 컨텍스트 필드 기본값 (매도 행이거나 컨텍스트 없는 경우)
            for field in TRADE_FIELDS[8:]:
                row.setdefault(field, "")
            writer.writerow(row)


def _migrate_csv_header_if_needed(file_path: Path, target_fields: list[str], log_prefix: str) -> None:
    """기존 CSV의 헤더에 target_fields의 새 필드가 없으면 헤더를 갱신합니다.

    2026-08-05 (GPT 코드리뷰 지적, P0-1): 원래 SignalCsvLogger 안의
    메서드였던 로직을 범용 함수로 추출 — EntryQualityShadowLogger
    (1E.5→1E.6에서 6개 필드 추가)도 같은 마이그레이션이 필요한데,
    이전엔 이 로거가 파일 존재 여부만 확인하고 헤더 스키마는 전혀
    비교하지 않았음. 재현 확인: 1E.5 시절 구형 헤더(32열)에 1E.6
    로거로 행을 추가하면 실제 데이터는 38열이 되어, csv.DictReader
    로 다시 읽을 때 초과된 6개 값이 row[None]으로 밀려나고 
    final_decision 같은 정상 필드가 None으로 파싱됨. 이 로거는
    entry_quality_guard_mode="off"일 때도 빈 헤더 파일을 생성하므로,
    1E.5 코드를 한 번이라도 실행했다면 실서버에 이미 구형 헤더
    파일이 있을 수 있어 — shadow를 켜는 순간 첫날부터 CSV 스키마가
    깨질 위험이 있었음.

    extrasaction='ignore' 때문에, 헤더에 없는 컬럼은 조용히 버려집니다.
    따라서 필드를 추가해도 기존 파일은 그대로면 새 데이터가 영영
    안 들어갑니다. 이 함수가 그 격차를 메웁니다.

    주의: 기존 파일이 utf-8-sig(BOM 포함)로 쓰였을 수 있으므로
    반드시 utf-8-sig로 읽어야 첫 컬럼의 키가 BOM 때문에 깨지지
    않는다. (utf-8로 읽으면 '\ufeff타임스탬프'가 되어 값이 유실됨)
    """
    try:
        with file_path.open("r", newline="", encoding="utf-8-sig") as fp:
            reader = csv.reader(fp)
            existing_header = next(reader, [])
    except (StopIteration, OSError) as exc:
        logger.warning(f"[{log_prefix}] 헤더 확인 실패 — 마이그레이션 건너뜀: {exc}")
        return

    # 헤더가 이미 최신이면 아무것도 안 함
    missing = [f for f in target_fields if f not in existing_header]
    if not missing:
        logger.info(f"[{log_prefix}] 헤더 최신 상태 확인 ({len(existing_header)}개 컬럼) — 마이그레이션 불필요")
        return

    logger.info(
        f"[{log_prefix}] 헤더 마이그레이션 시작 — 누락 컬럼 {len(missing)}개: {missing}"
    )

    # 2026-08-04 (GPT 코드리뷰 지시): 원본 파일을 "w" 모드로 직접
    # 덮어쓰면, 대용량 파일을 재작성하는 도중 프로세스가 죽거나
    # 디스크 문제가 생겼을 때 원본 데이터가 통째로 유실될 위험이
    # 있음(재작성이 절반만 끝난 상태로 파일이 잘리는 경우, 이전
    # 내용도 새 내용도 온전하지 않게 됨). 다음 두 가지로 방어:
    # (1) 재작성 전 원본을 .bak으로 복사(실패해도 원본은 그대로
    #     남아있어 최소한 데이터 유실은 없음).
    # (2) 임시 파일에 전부 쓴 뒤 os.replace()로 원자적 교체 —
    #     os.replace는 같은 파일시스템 안에서 단일 시스템 콜로
    #     완료되므로, 교체 도중에 프로세스가 죽어도 원본 파일이
    #     "절반만 쓰인 상태"로 남는 일이 없음(교체 전이면 원본
    #     그대로, 교체 후면 새 파일 그대로 — 중간 상태가 없음).
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    try:
        shutil.copy2(file_path, backup_path)
        logger.info(f"[{log_prefix}] 마이그레이션 전 백업 생성: {backup_path}")
    except OSError as exc:
        logger.warning(
            f"[{log_prefix}] 백업 생성 실패 — 마이그레이션 중단(원본 보호 우선): {exc}"
        )
        return

    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        # 기존 데이터를 스트리밍으로 한 행씩 읽어 임시 파일에 바로
        # 씀 — 통째로 메모리에 올리지 않아 대용량 파일도 메모리
        # 부담 없이 처리.
        row_count = 0
        with file_path.open("r", newline="", encoding="utf-8-sig") as src, \
                tmp_path.open("w", newline="", encoding="utf-8") as dst:
            reader = csv.DictReader(src)
            writer = csv.DictWriter(dst, fieldnames=target_fields, extrasaction="ignore")
            writer.writeheader()
            for old_row in reader:
                for field in target_fields:
                    old_row.setdefault(field, "")
                writer.writerow(old_row)
                row_count += 1
            # flush + fsync로 OS 캐시가 아니라 실제 디스크에 기록됨을
            # 보장 — os.replace() 자체는 이미 원자적이지만, 그 직전
            # tmp 파일의 내용이 디스크에 아직 안 쓰인 상태에서 정전
            # 등 강한 장애가 나면 replace 후에도 빈 파일이나 일부만
            # 쓰인 파일이 될 위험이 있음.
            dst.flush()
            os.fsync(dst.fileno())

        os.replace(tmp_path, file_path)
        logger.info(
            f"[{log_prefix}] 헤더 마이그레이션 완료 — {row_count:,}행 재작성, "
            f"컬럼 {len(existing_header)}개 → {len(target_fields)}개 "
            f"(백업: {backup_path})"
        )
    except Exception as exc:
        logger.error(
            f"[{log_prefix}] 마이그레이션 중 예외 발생 — 원본 파일은 아직 "
            f"교체 전이라 온전함(임시 파일만 불완전할 수 있음): {exc}"
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ── position_lifecycle.csv ───────────────────────────────────────────────────
# 포지션 5단계 상태머신(shadow, 2026-07-22)의 모든 상태 전이를 기록.
# 정상 전이(FLAT->BUY_PENDING->OPEN->...)와 이상 전이(부분체결/거부/
# 미반영/불변조건위반)를 전부 남겨서, shadow 검증 기간 동안 상태머신이
# 실제로 어떻게 동작하는지 사후 분석할 수 있게 한다.
# (기존엔 POSITION_STATE_MISMATCH 위반 시에만 app.log에 CRITICAL로
#  한 줄 남았고, 정상 전이는 메모리에만 있다가 다음 전이에 덮어써져
#  전혀 추적할 수 없었음)

LIFECYCLE_FIELDS = [
    "timestamp",           # 전이 발생 시각
    "symbol",
    "event",                # BUY_REQUESTED / BUY_RESULT / SELL_REQUESTED /
                             # SELL_RESULT / SYNC / INVARIANT_VIOLATION
    "from_lifecycle",       # 전이 전 상태
    "to_lifecycle",         # 전이 후 상태
    "broker_quantity",      # 이 이벤트 시점의 브로커 잔고 수량(조회한 경우)
    "pending_quantity",     # 진행 중이던 주문의 요청 수량
    "known_quantity",       # 상태머신이 마지막으로 확인한 수량
    "detail",                # PARTIAL_FILL / SELL_REJECTED / BUY_REJECTED 등
                             # last_error 값 또는 불변조건 위반 메시지
]


class PositionLifecycleLogger:
    """포지션 상태머신의 모든 전이를 CSV로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=LIFECYCLE_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """상태 전이 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=LIFECYCLE_FIELDS, extrasaction="ignore")
            for field in LIFECYCLE_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)
