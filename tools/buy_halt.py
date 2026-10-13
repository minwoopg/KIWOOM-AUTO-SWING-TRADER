"""신규 매수 중단 — 요청·상태·재개 (M3). 브로커·API 호출 없음.

    python tools/buy_halt.py request --reason "점검"      # 없을 때만 요청 파일 생성(중복 클릭 무해)
    python tools/buy_halt.py status                        # 요청 시각 / 실행기 반영 시각(runner_status.json)
    python tools/buy_halt.py resume --note "확인 완료" --confirm   # 사람만 재개(자동 재개 없음)

실행기(app.main)는 다음 폴링에서 요청을 읽어 신규 매수만 막음(BUY_HALTED). 매도·손절·미해결 주문 확인은 계속.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import load_settings  # noqa: E402
from infra.storage.runner_status import (  # noqa: E402
    read_buy_halt, read_json, request_buy_halt, resume_buy_halt, status_path,
)


def halt_view(settings) -> dict:
    """대시보드·CLI 공용: 요청 상태와 실행기 반영 상태를 따로."""
    st, req = read_buy_halt(settings.storage.commands_dir)
    rs_st, rs, rs_err = read_json(status_path(settings))
    applied = None
    if rs_st == "OK" and req and (rs.get("buy_halt") or {}).get("request_id") == req.get("request_id"):
        applied = rs["buy_halt"].get("applied_at")
    if st == "NONE":
        state = "NONE"
    elif applied:
        state = "APPLIED"
    else:
        state = "REQUESTED"
    return {"state": state, "request": req, "applied_at": applied, "runner_status": rs_st, "runner_error": rs_err,
            "runner_heartbeat": (rs or {}).get("heartbeat_at"), "runner_run_id": (rs or {}).get("run_id")}


LABEL = {"NONE": "중단 없음", "REQUESTED": "요청됨(실행기 반영 전)", "APPLIED": "중단 반영됨"}


def main(argv=None, *, settings=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="신규 매수 중단")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("request")
    r.add_argument("--reason", default="")
    sub.add_parser("status")
    z = sub.add_parser("resume")
    z.add_argument("--note", required=True)
    z.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    if settings is None:
        settings = load_settings(ROOT / "config" / "settings.yaml")
    if now is None:
        from utils.time_utils import now_local
        now = now_local()
    cdir = settings.storage.commands_dir
    if a.cmd == "request":
        created, req = request_buy_halt(cdir, reason=a.reason, by="cli", now=now)
        print(f"[신규 매수 중단] {'요청 생성' if created else '이미 요청돼 있음'} {req.get('request_id')} "
              f"({req.get('requested_at', '?')}) — 실행기가 다음 폴링에서 반영")
        return 0
    if a.cmd == "status":
        v = halt_view(settings)
        req = v["request"] or {}
        print(f"[신규 매수 중단] {LABEL[v['state']]} · 요청 {req.get('request_id', '-')} {req.get('requested_at', '')} · "
              f"반영 {v['applied_at'] or '-'} · 실행기 상태 파일 {v['runner_status']} heartbeat {v['runner_heartbeat'] or '-'}")
        return 0
    if not a.confirm:
        print("[재개] --confirm 없이 바꾸지 않음 — 재개하면 다음 폴링부터 신규 매수 가능")
        return 2
    rec = resume_buy_halt(cdir, note=a.note, now=now)
    print("[재개] 요청 없음" if rec is None else f"[재개] {rec.get('request_id')} 해제 기록(commands/processed/)")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
