from __future__ import annotations

"""키움 모의투자 응답 원문 기록 — 비밀값 제거 (M0 커밋 3, M1 실측 fixture용).

`RecordingPaperKiwoomBroker`는 `PaperKiwoomBroker`와 같고, 응답을 받을 때마다 (api-id, HTTP 상태, 헤더 일부, 본문)을
메모리에 쌓습니다. `save_fixtures()`가 비밀값을 지운 뒤 폴더에 파일로 남깁니다.

지우는 것: 토큰·앱키·시크릿 키 값, 계좌번호로 보이는 키(acnt·account·계좌)의 단일 값, 그리고 설정의 계좌번호·앱키·시크릿·토큰과
같은 문자열(어느 키에 있든). 주문번호·종목·수량·가격·체결 시각은 남김(대조에 필요).
"""

import json
import re
from pathlib import Path

from infra.broker.execution_mode import PaperKiwoomBroker

SECRET_KEY_PATTERN = re.compile(r"(token|appkey|app_key|secret|acnt|account|계좌|authorization)", re.I)
MASK = "***"


def redact(obj, secrets: set[str]):
    if isinstance(obj, dict):
        # 키 이름이 비밀처럼 보이면 **값이 하나일 때만** 가림 — 목록·묶음(예: 잔고 목록 acnt_evlt_remn_indv_tot)은 안으로 들어가 검사
        return {k: (MASK if SECRET_KEY_PATTERN.search(str(k)) and not isinstance(v, (dict, list))
                    else redact(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        out = obj
        for sec in secrets:
            if sec and len(sec) >= 6 and sec in out:
                out = out.replace(sec, MASK)
        return out
    return obj


class RecordingPaperKiwoomBroker(PaperKiwoomBroker):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.recorded: list[dict] = []

    def _to_api_response(self, response):
        api = super()._to_api_response(response)
        try:
            req_api = response.request.headers.get("api-id", "") if response.request is not None else ""
            path = response.request.path_url if response.request is not None else ""
        except Exception:      # noqa: BLE001
            req_api, path = "", ""
        self.recorded.append({"api_id": req_api or api.headers.get("api-id", ""), "path": path,
                              "status": api.status_code, "headers": dict(api.headers), "body": api.body})
        return api

    def secrets(self) -> set[str]:
        c = self.config
        acct = str(c.account_number or "")
        return {str(c.app_key or ""), str(c.secret_key or ""), acct, acct.replace("-", ""),
                str(self.access_token or "")}

    def save_fixtures(self, folder: Path) -> list[Path]:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        secrets = self.secrets()
        for i, rec in enumerate(self.recorded, 1):
            name = re.sub(r"[^A-Za-z0-9_]", "_", rec["api_id"] or "auth")
            p = folder / f"{i:03d}_{name}.json"
            p.write_text(json.dumps(redact(rec, secrets), ensure_ascii=False, indent=2), encoding="utf-8")
            out.append(p)
        return out
