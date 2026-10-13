"""시장 기준 실측 (M3 B — 읽기 전용): 같은 종목 일봉을 단축코드 그대로 / `_NX` / `_AL`로 받아 최근 며칠을 비교.

    python tools/probe_market_basis.py 005930 --days 5
    python tools/probe_market_basis.py 005930 --days 5 --out logs\\market_basis_20261014.json

- 조회 TR은 ka10081(일봉, 수정주가)뿐. 주문·계좌 API 없음, 모의 도메인만(mockapi). 같은 TR이라 호출 사이 1.2초.
- 접미사 코드가 이 TR에서 받아지는지부터 미확인 — 오류면 오류를 그대로 기록(추정해 채우지 않음).
- 결과: 날짜별 종가·저가·고가·거래량·거래대금, 단축코드 값이 `_NX`·`_AL` 중 어느 것과 같은지. 해석은 사람이(GPT 검토).
- `--out`은 새 파일만(있으면 거부). 저장소 data/ 안에는 쓰지 않음.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infra.research.kiwoom_readonly import ResearchApiError, ResearchConfigError  # noqa: E402
from infra.research.kiwoom_rows import parse_row  # noqa: E402

VARIANTS = ("", "_NX", "_AL")
FIELDS = ("open_raw", "high_raw", "low_raw", "close_raw", "volume", "trade_value_raw")


def fetch(client, code: str, base_dt: str) -> dict:
    try:
        page = client.fetch_page("ka10081", {"stk_cd": code, "base_dt": base_dt, "upd_stkpc_tp": "1"},
                                 "stk_dt_pole_chart_qry", "N", "")
    except (ResearchApiError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    rows = {}
    for r in page.rows:
        try:
            b = parse_row(r)
        except Exception as exc:                # noqa: BLE001
            rows.setdefault("_bad", []).append(str(exc)[:100])
            continue
        rows[b.date.isoformat()] = {k: getattr(b, k) for k in FIELDS}
    return {"received_at": page.received_at.isoformat(timespec="seconds"), "rows": rows}


def compare(res: dict, days: int) -> list[dict]:
    base = res[""].get("rows") or {}
    out = []
    for d in sorted((k for k in base if not k.startswith("_")), reverse=True)[:days]:
        row = {"date": d, "bare": base[d]}
        for v in VARIANTS[1:]:
            other = (res[v].get("rows") or {}).get(d)
            row[v] = other
            row[f"same_as{v}"] = None if other is None else other == base[d]
        out.append(row)
    return out


def main(argv=None, *, client=None, today: date | None = None, sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser(description="시장 기준 실측 — 단축코드·_NX·_AL 일봉 비교(읽기 전용)")
    ap.add_argument("code")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--out", help="결과 JSON(새 파일만)")
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args(argv)
    if not (a.code.isdigit() and len(a.code) == 6) or not 1 <= a.days <= 30:
        print("[사용법 오류] 종목코드 6자리, --days 1~30")
        return 2
    out_path = Path(a.out) if a.out else None
    if out_path is not None:
        if (ROOT / "data").resolve() in out_path.resolve().parents:
            print("[거부] data/ 안에는 쓰지 않음")
            return 2
        if out_path.exists():
            print(f"[거부] 이미 있는 파일: {out_path}")
            return 2
    if client is None:
        from tools.research_collect import make_client
        try:
            client = make_client(a)
        except ResearchConfigError as exc:
            print(f"[중단] {exc}")
            return 2
    base_dt = (today or date.today()).strftime("%Y%m%d")
    res = {}
    for i, v in enumerate(VARIANTS):
        if i:
            sleep(1.2)                          # 같은 TR 1초 1회(실측 유량=1)
        res[v] = fetch(client, a.code + v, base_dt)
    table = compare(res, a.days)
    print(f"[시장 기준] {a.code} — 단축코드 / _NX / _AL (ka10081, 수정주가)")
    for v in VARIANTS:
        r = res[v]
        print(f"  {a.code + v:<10} " + (f"오류 {r['error']}" if "error" in r else f"{len(r['rows'])}행 수신 {r['received_at']}"))
    for row in table:
        b = row["bare"]
        print(f"  {row['date']} 종가 {b['close_raw']} 저가 {b['low_raw']} 거래량 {b['volume']} | "
              + " | ".join(f"{v}: " + ("없음" if row[v] is None else
                                       ("같음" if row[f"same_as{v}"] else f"다름(종가 {row[v]['close_raw']} 거래량 {row[v]['volume']})"))
                           for v in VARIANTS[1:]))
    if out_path is not None:
        with open(out_path, "x", encoding="utf-8") as fh:
            json.dump({"code": a.code, "base_dt": base_dt, "responses": res, "compare": table}, fh, ensure_ascii=False,
                      indent=1)
        print(f"  저장: {out_path}")
    return 0 if all("error" not in res[v] for v in VARIANTS) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
