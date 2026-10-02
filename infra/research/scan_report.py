from __future__ import annotations

"""A4-A 일일 S1 관찰 보고서 (마크다운 + JSON). 신호는 관찰 기록이며 매매 수익이 아닙니다."""

import json
import os
from pathlib import Path

NOTE = ("신호(PASS)는 S1_BASE 조건을 통과한 **관찰 후보**입니다. 주문·체결 검증이 없으므로 수익으로 해석하지 않습니다. "
        "다음 거래일 가격 확인과 이후 움직임 평가는 A5에서 합니다.")


def _fmt(x, nd=0):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return f"{x:,}" if isinstance(x, int) else str(x)


def _table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def build_markdown(run: dict) -> str:
    c, ctx = run["counts"], run["context"]
    lines = [f"# S1 관찰 보고서 — 신호일 {ctx['signal_date']}", "",
             f"- 실행: `{run['run_id']}` · 스캔 시각 {ctx['scan_at']} · 다음 거래일 개장 {ctx['next_open'] or '달력 밖'}",
             f"- 전략 `{ctx['strategy']}` · 설정 해시 `{ctx['config_hash']}` · 지표 {ctx['feature_version']} · "
             f"시장 {ctx['market_version']} · 분류 정책 {ctx['applied_policy']}",
             f"- 완성 기준: 정규장 종료 + {ctx['after_close_min']}분 — **잠정 기준**(장 마감 후 실측 확인 전)",
             f"- 종목 목록: 스냅숏 {ctx['snapshot']['snapshot_id']} (관측 {ctx['snapshot']['observed_at']}, "
             f"저장 정책 {ctx['snapshot']['stored_policy']}) — 상태 {ctx['snapshot']['status']}",
             f"- 세션: {ctx['sessions']['first']} ~ {ctx['sessions']['last']} ({ctx['sessions']['count']}개, 거래소 달력)",
             "", f"> {NOTE}", "", "## 요약", ""]
    bs = c["by_signal"]
    lines += _table(["대상", "신호(PASS)", "서로 다른 종목", "탈락(FAIL)", "보류(UNKNOWN)", "확정 기록(final)",
                     "데이터 미확보", "거래 없는 봉 보류"],
                    [[c["universe"], c["signals"], c["distinct_symbols"], bs.get("FAIL", 0), bs.get("UNKNOWN", 0),
                      c["final"], c["data_hold_total"], c["no_trades_hold"]]])
    obs = c.get("observations")
    if obs:
        lines += ["", f"대표 기록: 새로 {obs['new']} · 확정 유지 {obs['kept_final']} · 보류 기록 대체 {obs['replaced']}"
                      f" · 더 늦은 기록 유지 {obs['kept_newer']}"]
    lines += ["", "## 시장 환경", ""]
    lines += _table(["지수", "데이터", "판정", "종가", "MA60", "MA120"],
                    [[sid, v["status"], v["regime"]["state"], _fmt(v["regime"]["close"], 2),
                      _fmt(v["regime"]["ma60"], 2), _fmt(v["regime"]["ma120"], 2)]
                     for sid, v in ctx["indexes"].items()])
    lines += ["", "## 시장별", ""]
    lines += _table(["시장", "PASS", "FAIL", "UNKNOWN"],
                    [[m, v.get("PASS", 0), v.get("FAIL", 0), v.get("UNKNOWN", 0)] for m, v in sorted(c["by_market"].items())])
    lines += ["", "## 보류 사유", ""]
    hold_rows = [[f"데이터: {k}", v] for k, v in sorted(c["data_holds"].items())]
    hold_rows += [[f"지수 장애: {k}", v] for k, v in sorted(c["index_holds"].items())]
    if c["snapshot_hold"]:
        hold_rows.append(["종목 목록 스냅숏이 장 마감 전 관측(위험 상태 모름)", c["snapshot_hold"]])
    hold_rows += [[f"조건: {k}", v] for k, v in list(c["unknown_reasons"].items())[:15]]
    lines += _table(["사유", "종목 수"], hold_rows or [["없음", 0]])
    lines += ["", "## 탈락 사유 (조건별, 한 종목이 여러 조건에 걸릴 수 있음)", ""]
    lines += _table(["묶음", "종목 수"], [[k, v] for k, v in sorted(c["fail_groups"].items())] or [["없음", 0]])
    lines += [""] + _table(["조건", "종목 수"], [[k, v] for k, v in c["fail_checks"].items()] or [["없음", 0]])
    lines += ["", "## 후보 (eligible_signal = PASS)", ""]
    cands = [e for e in run["evals"] if e["eligible_signal"] == "PASS"]
    cands.sort(key=lambda e: (-(e["result"]["observations"].get("rs60") or float("-inf")),
                              -(e["result"]["observations"].get("tv20") or float("-inf")), e["symbol"]))
    rows = []
    for e in cands:
        r = e["result"]
        lv, pb, ob = r["levels"], r["pullback"], r["observations"]
        rows.append([e["symbol"], e["name"], e["market"], _fmt(lv.get("stop_ref"), 0), _fmt(lv.get("entry_cap"), 0),
                     _fmt((lv.get("risk_ratio_at_cap") or 0) * 100, 2) + "%" if lv.get("risk_ratio_at_cap") else "-",
                     pb.get("pullback_len", "-"), _fmt((ob.get("rs60") or 0) * 100, 2) + "%" if ob.get("rs60") is not None else "-",
                     _fmt((ob.get("tv20") or 0) / 1e8, 1) + "억" if ob.get("tv20") else "-",
                     "Y" if e["actionable"] else ("N" if e["actionable"] == 0 else "?")])
    lines += _table(["종목", "이름", "시장", "참고 손절가", "진입 상한", "위험 비율(상한 기준)", "눌림 봉 수", "RS60",
                     "20일 거래대금", "다음 개장 전 스캔"], rows) if rows else ["후보 없음"]
    lines += ["", "참고 손절가·진입 상한은 분석용 값입니다(주문 가격 아님)."]
    return "\n".join(lines) + "\n"


def write_report(run: dict, out_dir: str | Path) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"s1_scan_{run['context']['signal_date']}_{run['run_id']}"
    md, js = base.with_suffix(".md"), base.with_suffix(".json")
    payload = {"run_id": run["run_id"], "context": run["context"], "counts": run["counts"],
               "candidates": [{k: e[k] for k in ("symbol", "name", "market", "actionable", "input_hash")}
                              | {"levels": e["result"]["levels"], "pullback": e["result"]["pullback"],
                                 "observations": e["result"]["observations"]}
                              for e in run["evals"] if e["eligible_signal"] == "PASS"],
               "note": NOTE}
    for path, text in ((md, build_markdown(run)),
                       (js, json.dumps(payload, ensure_ascii=False, indent=2, default=str))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return str(md)
