from __future__ import annotations

"""로그·기록 파일 민감정보 가리기 (스윙 분리 7라운드, 2026-09-28).

단타 레포 `export_daily_bundle.py`(bdde6c2)의 마스킹 부분(SENSITIVE_KEYS,
정규식, `mask()`, `_mask_json_value()`)을 **그대로** 옮겼습니다. 단타 번들에서
실제 자격증명 누출이 재현돼 여러 차례 보강된 코드라 새로 쓰지 않았습니다.
공개 이름으로 `mask_text()`, `mask_json()`을 추가했습니다.
"""

import re

SENSITIVE_KEYS = (
    "authorization", "bearer",
    # 2026-08-06 (1I.4, GPT 코드리뷰 P0, 재현 확인): 아래 키들이
    # 목록에 없어서 실제 키움 API 자격증명이 전부 누출됐음.
    #   infra/broker/kiwoom_broker.py:90-91 {"appkey":…, "secretkey":…}
    #   infra/broker/kiwoom_broker.py:107   token = body.get("token")
    #   infra/notify/kakao_notifier.py      rest_api_key, client_id
    # 재현: {"token":"SECRET1"} / {"appkey":"SECRET2"} /
    #       {"secretkey":"SECRET3"} 모두 원문 유지.
    "token", "access_token", "refresh_token",
    "appkey", "app_key", "secretkey", "secret_key",
    "api_key", "apikey", "rest_api_key", "client_secret", "client_id",
    "secret", "password", "passwd",
    "account_number", "account_no", "accountno", "account",
    "계좌번호", "계좌",
)
# 긴 키부터 정렬 — "token"이 "access_token"보다 먼저 매칭되면
# 접두사만 남는 부분 매칭이 생기므로.
_KEY_ALT = "|".join(re.escape(k) for k in sorted(SENSITIVE_KEYS, key=len, reverse=True))

# 2026-08-06 (1I.4, GPT 지적): 단일 정규식으로 quoted/unquoted를
# 모두 처리하려다 값 종료 문자에 공백·쉼표·세미콜론이 포함돼
# {"password":"hello world"} / {"secret":"abc,def"} 같은 값이
# 마스킹되지 않았음. **큰따옴표 / 작은따옴표 / 무따옴표 세 패턴으로
# 분리**하는 편이 안정적이라 그렇게 구현함.
#
# quoted 패턴은 닫는 따옴표까지를 값으로 보므로 공백·쉼표·세미콜론이
# 들어가도 전부 가려짐. unquoted 패턴만 구분자에서 값을 끊음.
_DQ_KV_RE = re.compile(
    rf'(?i)(?P<kq>"?)(?P<key>{_KEY_ALT})(?P=kq)(?P<sep>\s*[:=]\s*)"(?P<val>[^"]*)"'
)
_SQ_KV_RE = re.compile(
    rf"(?i)(?P<kq>'?)(?P<key>{_KEY_ALT})(?P=kq)(?P<sep>\s*[:=]\s*)'(?P<val>[^']*)'"
)
_UQ_KV_RE = re.compile(
    rf'(?i)(?P<key>{_KEY_ALT})(?P<sep>\s*[:=]\s*)(?P<val>[^\s,;}}\)\]"\']+)'
)
# "Bearer <token>" 처럼 키 뒤에 공백으로 이어지는 형태
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
# 계좌번호 형태 — 8자리-2자리
_ACCT_DASH_RE = re.compile(r"\b\d{8}-\d{2}\b")
# 10자리 이상 연속 숫자. 종목코드(6자리)·날짜(8자리)·분봉 타임스탬프
# (14자리)를 피하기 위해 10~13자리만, 그리고 앞뒤가 숫자가 아닐 때만.
_ACCT_LONG_RE = re.compile(r"(?<!\d)\d{10,13}(?!\d)")


def _mask_dq(m: "re.Match") -> str:
    return f'{m.group("kq")}{m.group("key")}{m.group("kq")}{m.group("sep")}"***"'


def _mask_sq(m: "re.Match") -> str:
    return f"{m.group('kq')}{m.group('key')}{m.group('kq')}{m.group('sep')}'***'"


def _mask_uq(m: "re.Match") -> str:
    return f'{m.group("key")}{m.group("sep")}***'


def mask(line: str) -> str:
    """민감정보를 가립니다. quoted → Bearer → unquoted → 형태 순서."""
    line = _DQ_KV_RE.sub(_mask_dq, line)
    line = _SQ_KV_RE.sub(_mask_sq, line)
    line = _BEARER_RE.sub("Bearer ***", line)
    line = _UQ_KV_RE.sub(_mask_uq, line)
    line = _ACCT_DASH_RE.sub("***", line)
    line = _ACCT_LONG_RE.sub("***", line)
    return line


# 2026-09-18 (재검토 지적 6번): mask()는 자유 텍스트 로그 줄을 위해
# 만들어진 정규식 기반 함수라 "따옴표 없는 10~13자리 숫자"까지
# 가립니다(_ACCT_LONG_RE). JSON으로 직렬화된 관측 레코드 전체
# 문자열에 그대로 적용하면, 원래 숫자였던 필드 값(예: order_id를
# 정수로 담은 필드, 또는 우연히 10~13자리인 다른 숫자 필드)이
# 따옴표 없는 `***`로 바뀌어 그 줄 전체가 더 이상 유효한 JSON이
# 아니게 됩니다(재현: 합성 원문에 숫자 필드 1234567890을 넣으면
# 결과 줄이 파싱 불가).
#
# 그래서 JSON 레코드는 파싱된 객체 상태에서 이 함수로 재귀적으로
# 처리합니다 — 키 이름이 SENSITIVE_KEYS와 "정확히" 일치할 때만
# (부분 문자열 매칭 아님) 그 값을 "***"로 치환합니다. 숫자·불리언·
# None 값은 (민감 키가 아닌 한) 그대로 두므로 JSON 문법이 깨지지
# 않습니다. 이렇게 하면 "account_scope_id" 같은 필드도 "account"와
# 정확히 일치하지 않으므로 실수로 가려지지 않습니다.
_SENSITIVE_KEYS_LOWER = {k.lower() for k in SENSITIVE_KEYS}

# 2026-09-18 재재검토 반영(지적 4번, 재현된 버그): 이전 구현은 민감
# 키가 아닌 "모든" 문자열 값에도 자유문자열용 mask()를 적용했습니다.
# mask()의 _ACCT_LONG_RE는 문맥과 무관하게 10~13자리 숫자 문자열을
# "***"로 가리므로, requested_order_id="1234567890"이나
# cntr_pric_raw="1234567890"처럼 **의미가 명확한 식별자 필드**(주문
# 번호·가격·수량 원문)까지 뭉개져 서로 다른 주문번호가 전부 같은
# "***"가 되고, 후속 연결·집계 근거가 훼손됐습니다(재현 확인).
#
# 이제 mask()는 아래 화이트리스트에 명시된, 실제로 "자유 텍스트"인
# 필드(예외 메시지 등 — 그 안에 무엇이 섞여 들어올지 스키마로 보장할
# 수 없는 필드)에만 적용합니다. 그 외 필드는 키가 SENSITIVE_KEYS와
# 정확히 일치하지 않는 한 원문 그대로 보존합니다 — `raw` 딕셔너리
# 내부의 원본 API 필드들(response_order_id/ord_qty_raw/cntr_pric_raw
# 등)도 이 모듈의 docstring에 명시된 전제(관측 레코드 자체는
# 계좌번호/토큰을 담지 않는 필드만 씀)에 따라 식별자로 보존됩니다 —
# 그 안에 우연히 SENSITIVE_KEYS와 정확히 일치하는 키가 있으면(예:
# 미래에 원문에 "token" 키가 추가되는 등) 그 값은 여전히 재귀적으로
# 가려집니다.
_FREEFORM_TEXT_FIELDS = {"error_repr"}


def _mask_json_value(value, key: str | None = None):
    """파싱된 JSON 값(dict/list/스칼라)을 재귀적으로 마스킹합니다.

    - dict: 키 이름이 SENSITIVE_KEYS와 정확히 일치(대소문자 무시)하면
      값을 통째로 "***"로 치환(하위 구조까지 있어도 더 내려가지
      않음 — 민감 필드 내부 구조를 부분 노출하지 않기 위함). 그 외
      키는 그 키 이름을 들고 값을 재귀 처리.
    - list: 각 원소를 재귀 처리(원소 자체는 특정 키에 속하지 않으므로
      key를 그대로 전달 — 리스트 안의 문자열 원소에 자유문자열
      마스킹을 적용할지는 리스트를 담고 있던 상위 키로 판단).
    - str: 이 값을 가리키는 키가 `_FREEFORM_TEXT_FIELDS`에 있을 때만
      기존 텍스트용 mask()를 적용합니다. 그 외 문자열(주문번호·가격·
      수량 등 식별자 필드)은 원문 그대로 보존합니다.
    - 그 외(숫자/불리언/None): 그대로 반환 — JSON 구조를 깨뜨리는
      원인이었던 부분이라 여기서는 절대 문자열 치환을 하지 않음.
    """
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if str(k).strip().lower() in _SENSITIVE_KEYS_LOWER:
                result[k] = "***" if v is not None else None
            else:
                result[k] = _mask_json_value(v, key=k)
        return result
    if isinstance(value, list):
        return [_mask_json_value(v, key=key) for v in value]
    if isinstance(value, str) and key in _FREEFORM_TEXT_FIELDS:
        return mask(value)
    return value


def mask_text(line: str) -> str:
    """자유 텍스트 한 줄 (app.log, CSV 행 등)."""
    return mask(line)


def mask_json(value):
    """파싱된 JSON 값 — 민감 키와 정확히 일치하는 키의 값만 가림(구조 보존)."""
    return _mask_json_value(value)
