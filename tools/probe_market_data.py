"""스윙 설계 전제 확인용 조회 전용 프로브 (스윙 분리 3라운드, 2026-09-28).

주문 API는 호출하지 않습니다. 모의투자 도메인(https://mockapi.kiwoom.com)에서만
실행되며, 우회 옵션은 없습니다 (단타 레포 tools/order_reconciliation_probe.py와
같은 안전 규칙).

확인하는 것
  A. 일봉(ka10081): 장중에 조회하면 당일 미완성 봉이 섞이는가 / 한 페이지에 몇 개
     오는가 / 연속조회(cont-yn/next-key)로 과거를 더 받을 수 있는가
  B. 캘린더 대조: 받은 일봉 날짜와 config/krx_calendar.yaml이 맞는가
  C. 미체결(ka10075)·체결(ka10076) 조회가 당일 주문만 보여주는가
     — 장 시작 전에 실행했을 때 행이 있으면 이전 거래일 주문이 조회된다는 뜻

사용법 (스윙 레포 루트, PowerShell)
    python tools/probe_market_data.py                      # 스윙 계좌 .env
    python tools/probe_market_data.py --env-file ..\\KIWOOM-AUTO-TRADER\\.env --skip-daily
        # C 항목은 주문 이력이 있는 단타 모의계좌로 장 시작 전(08:30 무렵) 실행하면 판정 가능

권장 실행 시점: 장중 1회(A 판정) + 장 마감 후 1회(A 비교) + 장 시작 전 1회(C 판정)

결과: logs/probes/market_data_probe_<시각>.jsonl (원시 응답, 계좌·토큰 값 가림)
      logs/probes/market_data_probe_<시각>_summary.txt (판정 요약 — 이 파일을 공유)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import MarketPhase, TradingCalendar  # noqa: E402

ALLOWED_BASE_URL_HOST = "mockapi.kiwoom.com"
ALLOWED_API = {  # api-id → endpoint (조회 전용 TR만)
    "ka10081": "/api/dostk/chart",    # 주식일봉차트조회
    "ka10001": "/api/dostk/stkinfo",  # 주식기본정보
    "ka10075": "/api/dostk/acnt",     # 미체결
    "ka10076": "/api/dostk/acnt",     # 체결
}
ALLOWED_RESPONSE_HEADER_KEYS = ("api-id", "cont-yn", "next-key")
SENSITIVE_BODY_KEY_MARKERS = (
    "acnt_no", "account_no", "accno", "authorization",
    "access_token", "accesstoken", "appkey", "app_key",
    "secretkey", "secret_key", "app_secret", "token",
)
DATE_RE = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")


class ProbeConfigError(ValueError):
    """실행 조건이 안전 요건을 충족하지 못함."""


# ── 안전 검증 / 가공 (순수 함수) ─────────────────────────────

def assert_mock_domain(base_url: str) -> None:
    parsed = urlparse(base_url or "")
    if ((parsed.scheme or "").lower() != "https"
            or (parsed.hostname or "").lower() != ALLOWED_BASE_URL_HOST
            or parsed.port not in (None, 443)):
        raise ProbeConfigError(
            f"이 프로브는 https://{ALLOWED_BASE_URL_HOST} 에서만 실행합니다 "
            f"(현재 base_url={base_url!r}). 실전 계좌 조회를 막기 위한 안전장치입니다."
        )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(m in str(k).lower() for m in SENSITIVE_BODY_KEY_MARKERS)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def parse_abs_int(value: Any) -> int | None:
    try:
        return abs(int(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def parse_yyyymmdd(value: Any) -> date | None:
    m = DATE_RE.match(str(value or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        raise ProbeConfigError(f".env 파일 없음: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ── 네트워크 ─────────────────────────────────────────────────

class ProbeClient:
    def __init__(self, session, base_url: str, app_key: str, secret_key: str,
                 sleep_sec: float, sink) -> None:
        assert_mock_domain(base_url)
        self.session = session
        self.base_url = base_url
        self.app_key = app_key
        self.secret_key = secret_key
        self.sleep_sec = sleep_sec
        self.sink = sink
        self.token = ""

    def authenticate(self) -> None:
        assert_mock_domain(self.base_url)
        r = self.session.post(f"{self.base_url}/oauth2/token", json={
            "grant_type": "client_credentials", "appkey": self.app_key, "secretkey": self.secret_key,
        }, timeout=10)
        body = r.json()
        token = body.get("token") if isinstance(body, dict) else None
        if r.status_code != 200 or not token:
            raise ProbeConfigError(f"토큰 발급 실패: http={r.status_code} return_msg="
                                   f"{(body or {}).get('return_msg') if isinstance(body, dict) else ''}")
        self.token = token

    def call(self, api_id: str, payload: dict, cont_yn: str = "N", next_key: str = "",
             label: str = "") -> tuple[int | None, dict, Any]:
        assert_mock_domain(self.base_url)
        if api_id not in ALLOWED_API:
            raise ProbeConfigError(f"허용되지 않은 api-id: {api_id}")
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.token}",
            "cont-yn": cont_yn, "next-key": next_key, "api-id": api_id,
        }
        requested_at = datetime.now().isoformat()
        try:
            r = self.session.post(f"{self.base_url}{ALLOWED_API[api_id]}", headers=headers,
                                  json=payload, timeout=10)
            status = r.status_code
            resp_headers = {k: r.headers.get(k, "") for k in ALLOWED_RESPONSE_HEADER_KEYS}
            try:
                body = r.json()
            except ValueError:
                body = {"probe_raw_text_preview": str(getattr(r, "text", ""))[:300]}
        except requests.RequestException as exc:
            status, resp_headers, body = None, {}, {"probe_transport_error": type(exc).__name__}
        body = redact(body)
        self.sink({"label": label, "api_id": api_id, "requested_at": requested_at,
                   "request_payload": payload, "request_cont_yn": cont_yn,
                   "http_status": status, "response_headers": resp_headers, "response_body": body})
        if self.sleep_sec:
            time.sleep(self.sleep_sec)
        return status, resp_headers, body

    def paginate(self, api_id: str, payload: dict, list_key: str, max_pages: int, label: str):
        """(pages[list[rows]], stop_reason). 페이지 캡에 걸리면 stop_reason=CAP."""
        pages: list[list] = []
        cont, key = "N", ""
        for i in range(1, max_pages + 1):
            status, h, body = self.call(api_id, payload, cont, key, label=f"{label}#p{i}")
            if status != 200 or not isinstance(body, dict):
                return pages, f"HTTP_{status}"
            if body.get("return_code") not in (0, None):
                return pages, f"RETURN_CODE_{body.get('return_code')}:{body.get('return_msg', '')}"
            rows = body.get(list_key)
            if not isinstance(rows, list):
                return pages, f"NO_LIST_{list_key}"
            pages.append(rows)
            if h.get("cont-yn") != "Y" or not h.get("next-key"):
                return pages, "END"
            cont, key = "Y", h.get("next-key")
        return pages, "CAP"


# ── 판정 ─────────────────────────────────────────────────────

def analyze_daily(symbol: str, pages: list[list], stop: str, current_price: int | None,
                  now: datetime, cal: TradingCalendar) -> dict:
    rows = [r for p in pages for r in p if isinstance(r, dict)]
    dates = [parse_yyyymmdd(r.get("dt")) for r in rows]
    valid = [d for d in dates if d]
    today = now.date()
    first = rows[0] if rows else {}
    phase = cal.phase(now)
    today_row = next((r for r in rows if parse_yyyymmdd(r.get("dt")) == today), None)
    res = {
        "symbol": symbol,
        "page_sizes": [len(p) for p in pages],
        "stop_reason": stop,
        "total_rows": len(rows),
        "unparseable_dates": len(dates) - len(valid),
        "newest_date": str(max(valid)) if valid else None,
        "oldest_date": str(min(valid)) if valid else None,
        "first_row_date": first.get("dt"),
        "descending_order": valid == sorted(valid, reverse=True),
        "duplicate_dates": len(valid) - len(set(valid)),
        "phase_at_run": phase.value,
        "today_row_present": today_row is not None,
        "today_row_close": parse_abs_int(today_row.get("cur_prc")) if today_row else None,
        "current_price_ka10001": current_price,
    }
    if phase in (MarketPhase.PRE_OPEN, MarketPhase.REGULAR, MarketPhase.CLOSING_AUCTION):
        if today_row is not None:
            same = res["today_row_close"] == current_price and current_price is not None
            res["verdict"] = ("장중 조회에 당일 미완성 봉이 포함됨"
                              + (" (종가 칸 = 현재가)" if same else " (종가 칸 ≠ 현재가 — 원시 응답 확인)"))
        else:
            res["verdict"] = "장중 조회에 당일 봉 없음 — 첫 행이 직전 거래일"
    elif phase == MarketPhase.POST_CLOSE:
        res["verdict"] = ("장 마감 후 당일 봉 포함" if today_row is not None
                          else "장 마감 후인데 당일 봉 없음 — 반영 지연 가능, 원시 응답 확인")
    else:
        res["verdict"] = "휴장일 실행 — 당일 봉 판정 대상 아님"
    return res


def analyze_calendar(bar_dates: list[date], now: datetime, cal: TradingCalendar) -> dict:
    covered_start = date(min(cal.covered_years), 1, 1)
    in_cover = [d for d in bar_dates if d.year in cal.covered_years]
    if not in_cover:
        return {"verdict": "대조할 일봉 없음"}
    start = max(min(in_cover), covered_start)
    end = cal.last_completed_session(now)
    r = cal.compare_with_bar_dates(in_cover, start, end)
    ok = not r["missing_bars"] and not r["unexpected_bars"]
    return {
        "range": f"{start} ~ {end}",
        "missing_bars": [str(d) for d in r["missing_bars"]],
        "unexpected_bars": [str(d) for d in r["unexpected_bars"]],
        "verdict": "캘린더와 일봉 날짜 일치" if ok else "불일치 있음 — krx_calendar.yaml 확인 필요",
    }


def analyze_orders(api_id: str, pages: list[list], stop: str, now: datetime,
                   cal: TradingCalendar) -> dict:
    rows = [r for p in pages for r in p if isinstance(r, dict)]
    keys = sorted({k for r in rows for k in r})
    date_like: dict[str, list[str]] = {}
    past_dates: set[str] = set()
    for r in rows:
        for k, v in r.items():
            d = parse_yyyymmdd(v)
            if d is not None:
                date_like.setdefault(k, [])
                if str(v) not in date_like[k]:
                    date_like[k].append(str(v))
                if d < now.date():
                    past_dates.add(f"{k}={v}")
    phase = cal.phase(now)
    if past_dates:
        verdict = "이전 날짜 주문이 조회됨 (날짜 필드로 확인)"
    elif phase == MarketPhase.PRE_OPEN and rows:
        verdict = "장 시작 전인데 행이 있음 → 이전 거래일 주문도 조회됨"
    elif phase == MarketPhase.PRE_OPEN and not rows:
        verdict = "장 시작 전 0건 → 당일 주문만 조회되거나, 이전 주문 자체가 없음 (주문 이력 있는 계좌로 재확인)"
    elif not rows:
        verdict = "0건 — 주문 이력이 없어 판정 불가"
    else:
        verdict = "장중·장후 실행이라 행의 날짜를 구분할 수 없음 — 장 시작 전에 재실행"
    return {"api_id": api_id, "stop_reason": stop, "rows": len(rows), "row_keys": keys,
            "date_like_fields": {k: v[:5] for k, v in date_like.items()},
            "past_date_values": sorted(past_dates)[:10], "phase_at_run": phase.value,
            "verdict": verdict}


# ── 실행 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="스윙 설계 전제 확인용 조회 전용 프로브")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--symbols", default="005930,000660")
    p.add_argument("--daily-pages", type=int, default=3, help="일봉 연속조회 최대 페이지")
    p.add_argument("--order-pages", type=int, default=5)
    p.add_argument("--sleep", type=float, default=0.5, help="API 호출 간 대기(초)")
    p.add_argument("--skip-daily", action="store_true")
    p.add_argument("--skip-orders", action="store_true")
    p.add_argument("--out-dir", default=str(ROOT / "logs" / "probes"))
    return p


def main(argv: list[str] | None = None, *, session=None, now: datetime | None = None,
         calendar: TradingCalendar | None = None) -> int:
    args = build_parser().parse_args(argv)
    assert_mock_domain(args.base_url)
    env = load_env_file(Path(args.env_file))
    app_key, secret = env.get("KIWOOM_APP_KEY", ""), env.get("KIWOOM_SECRET_KEY", "")
    if not app_key or not secret:
        raise ProbeConfigError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 .env에 없음")
    cal = calendar or TradingCalendar.load()
    now = now or now_local()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"market_data_probe_{stamp}.jsonl"
    summary_path = out_dir / f"market_data_probe_{stamp}_summary.txt"

    with raw_path.open("w", encoding="utf-8") as raw:
        def sink(rec):
            raw.write(json.dumps(rec, ensure_ascii=False) + "\n")
            raw.flush()

        client = ProbeClient(session or requests.Session(), args.base_url, app_key, secret,
                             args.sleep, sink)
        client.authenticate()
        summary: dict[str, Any] = {
            "run_at": now.isoformat(timespec="seconds"),
            "phase_at_run": cal.phase(now).value,
            "last_completed_session": str(cal.last_completed_session(now)),
            "env_file": Path(args.env_file).name,
        }
        today = now.strftime("%Y%m%d")
        if not args.skip_daily:
            summary["daily"] = []
            all_dates: list[date] = []
            for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
                _, _, info = client.call("ka10001", {"stk_cd": sym}, label=f"price:{sym}")
                cur = parse_abs_int(info.get("cur_prc")) if isinstance(info, dict) else None
                pages, stop = client.paginate(
                    "ka10081", {"stk_cd": sym, "base_dt": today, "upd_stkpc_tp": "1"},
                    "stk_dt_pole_chart_qry", args.daily_pages, label=f"daily:{sym}")
                res = analyze_daily(sym, pages, stop, cur, now, cal)
                summary["daily"].append(res)
                if not all_dates:
                    all_dates = [d for p in pages for r in p
                                 if isinstance(r, dict) and (d := parse_yyyymmdd(r.get("dt")))]
            summary["calendar"] = analyze_calendar(all_dates, now, cal)
        if not args.skip_orders:
            summary["orders"] = []
            for api_id, payload, key in (
                ("ka10075", {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"}, "oso"),
                ("ka10076", {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"}, "cntr"),
            ):
                pages, stop = client.paginate(api_id, payload, key, args.order_pages, label=api_id)
                summary["orders"].append(analyze_orders(api_id, pages, stop, now, cal))

    lines = [f"# market data probe {summary['run_at']} ({summary['phase_at_run']})",
             json.dumps(summary, ensure_ascii=False, indent=2)]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n원시 응답: {raw_path}\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    try:  # 콘솔 출력을 파일로 돌려도(cp949) 특수문자 때문에 중단되지 않게
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except ProbeConfigError as exc:
        print(f"[중단] {exc}")
        sys.exit(2)
