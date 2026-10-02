"""A5-1: 개장 가격 기록용 시세 원천 확인 프로브 (조회 전용, 2026-10-02).

주문 API는 호출하지 않고, 모의투자 도메인(https://mockapi.kiwoom.com)에서만 실행합니다
(`tools/probe_market_data.py`의 ProbeClient·안전장치를 그대로 씀). 응답은 계좌·토큰 값을 가려 저장합니다.

확인하는 것 (TR 이름·경로는 조사 후보 — 오류 응답도 결과로 기록)
  Q. ka10001(주식기본정보): A5-1이 쓰는 cur_prc(현재가)·base_pric(기준가)와 시가·상한가·하한가·거래량 필드,
     시각처럼 보이는 필드(이름에 tm·time·dt·hms)가 있는지
  T. ka10003(체결정보 후보): 체결 목록의 시각 필드(원천 가격 시각 후보)
  B. ka10004(주식호가 후보): 호가 기준 시각·최우선 매도/매수 호가
  같은 조회를 --repeat번(--gap-sec 간격) 반복해 값·시각 필드가 움직이는지 봅니다(장중 실행 권장).

사용법 (스윙 레포 루트, PowerShell)
    python tools/probe_price_sources.py
    python tools/probe_price_sources.py --symbols 005930,000660,247540 --repeat 2 --gap-sec 5
권장: 장중(09:05 무렵이면 가장 좋음, 오늘이라면 15:20 전). 소요 약 30초.

결과: logs/probes/price_sources_<시각>.jsonl (원시 응답) · logs/probes/price_sources_<시각>_summary.txt (이 파일을 공유)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from tools.probe_market_data import (  # noqa: E402
    ProbeClient, ProbeConfigError, assert_mock_domain, load_env_file, parse_abs_int,
)
from utils.time_utils import now_local  # noqa: E402

TIME_KEY = re.compile(r"(^|_)(tm|time|dt|hms|tim)($|_)|_tm$|tm_", re.IGNORECASE)
PRICE_KEYS = ("cur_prc", "base_pric", "open_pric", "high_pric", "low_pric", "upl_pric", "lst_pric", "trde_qty",
              "sel_fpr_bid", "buy_fpr_bid", "pred_pre", "flu_rt")
A5_REQUIRED = ("cur_prc", "base_pric")


def scalar_fields(d: dict) -> dict:
    return {k: v for k, v in d.items() if not isinstance(v, (list, dict))}


def time_like(d: dict) -> dict:
    return {k: v for k, v in d.items() if not isinstance(v, (list, dict)) and TIME_KEY.search(k)}


def analyze(api_id: str, symbol: str, status, body: Any, requested_at: str, received_at: str) -> dict:
    """한 응답의 필드 요약 (순수 함수 — 테스트 대상)."""
    res: dict[str, Any] = {"api_id": api_id, "symbol": symbol, "requested_at": requested_at,
                           "received_at": received_at, "http_status": status}
    if not isinstance(body, dict):
        res["verdict"] = "응답 본문 없음"
        return res
    res["return_code"] = body.get("return_code")
    res["return_msg"] = body.get("return_msg")
    top = scalar_fields(body)
    res["top_keys"] = sorted(top)
    res["time_like_top"] = time_like(body)
    res["prices_top"] = {k: parse_abs_int(top.get(k)) for k in PRICE_KEYS if k in top}
    lists = {k: v for k, v in body.items() if isinstance(v, list)}
    res["list_keys"] = {k: len(v) for k, v in lists.items()}
    for k, rows in lists.items():
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        res[f"list:{k}:row_keys"] = sorted(first)
        res[f"list:{k}:time_like_first"] = time_like(first)
        res[f"list:{k}:prices_first"] = {p: parse_abs_int(first.get(p)) for p in PRICE_KEYS if p in first}
    if api_id == "ka10001":
        missing = [k for k in A5_REQUIRED if parse_abs_int(top.get(k)) is None]
        res["a5_required_ok"] = not missing
        res["verdict"] = ("A5-1 필수 필드(cur_prc·base_pric) 있음" if not missing
                          else f"A5-1 필수 필드 없음: {missing} — 원시 응답 확인")
    return res


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A5-1 시세 원천 확인 (조회 전용)")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--symbols", default="005930,000660,247540")
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--gap-sec", type=float, default=5.0)
    p.add_argument("--sleep", type=float, default=1.0, help="API 호출 간 대기(초) — 429 방지")
    p.add_argument("--out-dir", default=str(ROOT / "logs" / "probes"))
    return p


def main(argv: list[str] | None = None, *, session=None, now=now_local, sleep=time.sleep) -> int:
    args = build_parser().parse_args(argv)
    assert_mock_domain(args.base_url)
    env = load_env_file(Path(args.env_file))
    app_key, secret = env.get("KIWOOM_APP_KEY", ""), env.get("KIWOOM_SECRET_KEY", "")
    if not app_key or not secret:
        raise ProbeConfigError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 .env에 없음")
    t0 = now()
    stamp = t0.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"price_sources_{stamp}.jsonl"
    summary_path = out_dir / f"price_sources_{stamp}_summary.txt"
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    results = []
    with raw_path.open("w", encoding="utf-8") as raw:
        def sink(rec):
            raw.write(json.dumps(rec, ensure_ascii=False) + "\n")
            raw.flush()

        client = ProbeClient(session or requests.Session(), args.base_url, app_key, secret, args.sleep, sink)
        client.authenticate()
        for rep in range(1, max(1, args.repeat) + 1):
            calls = [("ka10001", s, {"stk_cd": s}) for s in symbols]
            calls += [("ka10003", symbols[0], {"stk_cd": symbols[0]}), ("ka10004", symbols[0], {"stk_cd": symbols[0]})]
            for api_id, sym, payload in calls:
                req = now().isoformat(timespec="milliseconds")
                status, _h, body = client.call(api_id, payload, label=f"{api_id}:{sym}#r{rep}")
                rec = now().isoformat(timespec="milliseconds")
                results.append({"repeat": rep, **analyze(api_id, sym, status, body, req, rec)})
            if rep < args.repeat:
                sleep(args.gap_sec)
    summary = {"run_at": t0.isoformat(timespec="seconds"), "base_url": args.base_url, "symbols": symbols,
               "note": "TR·필드는 조사 후보. A5-1은 ka10001 cur_prc·base_pric을 씀. 원천 시각 필드 후보는 time_like_*.",
               "results": results}
    text = f"# price sources probe {summary['run_at']}\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
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
