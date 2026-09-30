"""A1: 연구 데이터 원천 확인용 조회 전용 프로브 (2026-09-30).

주문 API는 호출하지 않고, 모의투자 도메인(https://mockapi.kiwoom.com)에서만 실행합니다
(`tools/probe_market_data.py`의 ProbeClient·안전장치를 그대로 씀).

**TR 이름·요청 필드는 조사 후보입니다.** 이 프로브는 응답을 그대로 기록해서 필드 이름·단위·
연속조회·지원 여부를 사람이 확인하게 합니다. 오류 응답(return_code·return_msg)도 기록합니다.

확인하는 것
  L. 종목 목록(ka10099 후보, mrkt_tp 0=KOSPI·10=KOSDAQ): 행 수, 필드 이름, 값 종류가 적은 필드의
     값 분포(시장·증권 유형·경고·상태 후보), 종목코드 형태
  D. 종목 일봉(ka10081): 행의 모든 필드 이름, 거래대금 후보 필드(이름에 prica/amt)와
     `후보값 ÷ (종가×거래량)` 중앙값 → 단위 추정(1 ≈ 원, 1e-6 ≈ 백만원), 연속조회로 가장 오래된 날짜
  I. 지수 일봉(ka20006 후보, inds_cd 001=KOSPI·101=KOSDAQ 후보): 필드, 값 원문(소수점·배율 확인용),
     페이지 크기, 날짜 범위

사용법 (스윙 레포 루트, PowerShell)
    python tools/probe_research_sources.py
    python tools/probe_research_sources.py --depth-symbol 005930 --depth-pages 6
권장: 장 마감 후 실행(당일 봉 확정). 소요 약 1~2분.

결과: logs/probes/research_sources_<시각>.jsonl (원시 응답, 계좌·토큰 가림)
      logs/probes/research_sources_<시각>_summary.txt (이 파일을 공유)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from tools.probe_market_data import (  # noqa: E402
    ProbeClient, ProbeConfigError, assert_mock_domain, load_env_file, parse_abs_int, parse_yyyymmdd,
)
from utils.time_utils import now_local  # noqa: E402

MAX_DISTINCT_FOR_DISTRIBUTION = 40
TRADE_VALUE_KEY_MARKERS = ("prica", "amt", "trde_pric")


# ── 순수 분석 ────────────────────────────────────────────────

def first_list_key(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for k, v in body.items():
        if isinstance(v, list):
            return k
    return None


def paginate_auto(client, api_id: str, payload: dict, max_pages: int, label: str):
    """목록 키를 응답에서 찾아 연속조회. (pages, list_key, stop_reason, error_body)."""
    pages, cont, key, list_key = [], "N", "", None
    for i in range(1, max_pages + 1):
        status, h, body = client.call(api_id, payload, cont, key, label=f"{label}#p{i}")
        if status != 200 or not isinstance(body, dict):
            return pages, list_key, f"HTTP_{status}", body
        if body.get("return_code") not in (0, None):
            return pages, list_key, f"RETURN_CODE_{body.get('return_code')}", {
                "return_code": body.get("return_code"), "return_msg": body.get("return_msg")}
        list_key = list_key or first_list_key(body)
        rows = body.get(list_key) if list_key else None
        if not isinstance(rows, list):
            return pages, list_key, "NO_LIST", {"top_level_keys": sorted(body)}
        pages.append(rows)
        if h.get("cont-yn") != "Y" or not h.get("next-key"):
            return pages, list_key, "END", None
        cont, key = "Y", h.get("next-key")
    return pages, list_key, "CAP", None


def field_distributions(rows: list[dict]) -> dict:
    """값 종류가 적은 필드의 분포(상위 20개). 증권 유형·경고·상태 후보를 찾기 위함."""
    out = {}
    keys = sorted({k for r in rows for k in r})
    for k in keys:
        vals = [str(r.get(k, "")) for r in rows]
        c = Counter(vals)
        if len(c) <= MAX_DISTINCT_FOR_DISTRIBUTION:
            out[k] = c.most_common(20)
    return out


def analyze_stock_list(market: str, pages: list[list], list_key, stop: str, err) -> dict:
    rows = [r for p in pages for r in p if isinstance(r, dict)]
    code_key = next((k for k in ("code", "stk_cd", "jmcode") if rows and k in rows[0]), None)
    codes = [str(r.get(code_key, "")) for r in rows] if code_key else []
    return {
        "market": market, "list_key": list_key, "stop_reason": stop, "error": err,
        "page_sizes": [len(p) for p in pages], "rows": len(rows),
        "fields": sorted({k for r in rows for k in r}),
        "samples": rows[:3],
        "low_cardinality_fields": field_distributions(rows),
        "code_field": code_key,
        "code_shapes": {
            "six_digits": sum(1 for c in codes if len(c) == 6 and c.isdigit()),
            "ends_with_0": sum(1 for c in codes if c.endswith("0")),
            "non_digit": sum(1 for c in codes if not c.isdigit()),
            "examples_not_ending_0": [c for c in codes if not c.endswith("0")][:10],
        },
    }


def analyze_daily_fields(symbol: str, pages: list[list], stop: str, err) -> dict:
    rows = [r for p in pages for r in p if isinstance(r, dict)]
    dates = [d for r in rows if (d := parse_yyyymmdd(r.get("dt")))]
    fields = sorted({k for r in rows for k in r})
    candidates = {}
    for k in fields:
        if not any(m in k.lower() for m in TRADE_VALUE_KEY_MARKERS):
            continue
        ratios = []
        for r in rows:
            v, c, q = parse_abs_int(r.get(k)), parse_abs_int(r.get("cur_prc")), parse_abs_int(r.get("trde_qty"))
            if v and c and q:
                ratios.append(v / (c * q))
        med = statistics.median(ratios) if ratios else None
        guess = None
        if med is not None:
            if 0.5 <= med <= 2:
                guess = "원 단위로 보임"
            elif 0.5e-3 <= med <= 2e-3:
                guess = "천원 단위로 보임"
            elif 0.5e-6 <= med <= 2e-6:
                guess = "백만원 단위로 보임"
            else:
                guess = "단위 불명 — 원문 확인"
        candidates[k] = {"rows_used": len(ratios), "median_value_over_close_x_volume": med, "unit_guess": guess,
                         "raw_samples": [r.get(k) for r in rows[:3]]}
    return {
        "symbol": symbol, "stop_reason": stop, "error": err, "page_sizes": [len(p) for p in pages],
        "rows": len(rows), "newest": str(max(dates)) if dates else None, "oldest": str(min(dates)) if dates else None,
        "fields": fields, "samples": rows[:2], "trade_value_candidates": candidates,
        "verdict": ("거래대금 후보 필드 있음 — 단위 추정 확인" if candidates else "거래대금 후보 필드 없음 — 다른 TR 확인 필요"),
    }


def analyze_index(label: str, payload: dict, pages: list[list], list_key, stop: str, err) -> dict:
    rows = [r for p in pages for r in p if isinstance(r, dict)]
    date_key = next((k for k in ("dt", "date", "trd_dt") if rows and k in rows[0]), None)
    dates = [d for r in rows if date_key and (d := parse_yyyymmdd(r.get(date_key)))]
    price_like = {k: [r.get(k) for r in rows[:3]] for k in sorted({k for r in rows for k in r})
                  if any(m in k for m in ("prc", "pric", "idx"))}
    return {
        "label": label, "payload": payload, "list_key": list_key, "stop_reason": stop, "error": err,
        "page_sizes": [len(p) for p in pages], "rows": len(rows),
        "fields": sorted({k for r in rows for k in r}), "samples": rows[:2],
        "price_field_raw_values": price_like,
        "has_decimal_point": any("." in str(v) for vs in price_like.values() for v in vs),
        "newest": str(max(dates)) if dates else None, "oldest": str(min(dates)) if dates else None,
        "descending": dates == sorted(dates, reverse=True) if dates else None,
    }


# ── 실행 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A1 연구 데이터 원천 확인 (조회 전용)")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--list-pages", type=int, default=10, help="종목 목록 연속조회 최대 페이지")
    p.add_argument("--depth-symbol", default="005930")
    p.add_argument("--depth-pages", type=int, default=6, help="일봉 이력 깊이 확인용 최대 페이지")
    p.add_argument("--index-pages", type=int, default=6)
    p.add_argument("--kospi-code", default="001")
    p.add_argument("--kosdaq-code", default="101")
    p.add_argument("--sleep", type=float, default=1.0, help="API 호출 간 대기(초) — 429 방지")
    p.add_argument("--out-dir", default=str(ROOT / "logs" / "probes"))
    return p


def main(argv: list[str] | None = None, *, session=None, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    assert_mock_domain(args.base_url)
    env = load_env_file(Path(args.env_file))
    app_key, secret = env.get("KIWOOM_APP_KEY", ""), env.get("KIWOOM_SECRET_KEY", "")
    if not app_key or not secret:
        raise ProbeConfigError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 .env에 없음")
    now = now or now_local()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"research_sources_{stamp}.jsonl"
    summary_path = out_dir / f"research_sources_{stamp}_summary.txt"
    today = now.strftime("%Y%m%d")

    with raw_path.open("w", encoding="utf-8") as raw:
        def sink(rec):
            raw.write(json.dumps(rec, ensure_ascii=False) + "\n")
            raw.flush()

        client = ProbeClient(session or requests.Session(), args.base_url, app_key, secret, args.sleep, sink)
        client.authenticate()
        summary: dict[str, Any] = {"run_at": now.isoformat(timespec="seconds"), "base_url": args.base_url,
                                   "note": "TR·필드는 조사 후보. 오류도 결과로 기록."}
        summary["stock_list"] = []
        for market, mrkt_tp in (("KOSPI", "0"), ("KOSDAQ", "10")):
            pages, lk, stop, err = paginate_auto(client, "ka10099", {"mrkt_tp": mrkt_tp}, args.list_pages,
                                                 f"list:{market}")
            summary["stock_list"].append(analyze_stock_list(market, pages, lk, stop, err))
        pages, lk, stop, err = paginate_auto(
            client, "ka10081", {"stk_cd": args.depth_symbol, "base_dt": today, "upd_stkpc_tp": "1"},
            args.depth_pages, f"daily:{args.depth_symbol}")
        summary["daily_fields"] = analyze_daily_fields(args.depth_symbol, pages, stop, err)
        summary["index"] = []
        for label, code in (("KOSPI", args.kospi_code), ("KOSDAQ", args.kosdaq_code)):
            payload = {"inds_cd": code, "base_dt": today}
            pages, lk, stop, err = paginate_auto(client, "ka20006", payload, args.index_pages, f"index:{label}")
            summary["index"].append(analyze_index(label, payload, pages, lk, stop, err))

    text = f"# research sources probe {summary['run_at']}\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"원시 응답: {raw_path}\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except ProbeConfigError as exc:
        print(f"[중단] {exc}")
        sys.exit(2)
