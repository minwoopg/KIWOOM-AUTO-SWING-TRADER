"""OrderExecutor ↔ 단타 TradingService 동작 동등성 비교 (스윙 분리 2라운드).

같은 스크립트 브로커·같은 시나리오를 두 구현에 각각 흘려, 매 단계 뒤의
상태를 비교합니다: PSM 종목별 상태(시각 필드는 존재 여부만), state.json의
주문 의도, 저널 내용, 보류 중인 매수/매도 컨텍스트, 브로커 호출 순서,
첫 체결/완전 청산 훅 호출, 복구 실패 플래그.

사용법 (스윙 레포 루트에서):
    python tools/equivalence/compare.py --orig ..\\KIWOOM-AUTO-TRADER

단타 레포를 수정한 뒤 브로커·상태머신 수정분을 가져올 때, 또는
order_executor.py를 고친 뒤 원본과 어긋나지 않았는지 확인할 때 씁니다.
회귀 테스트 러너(run_regression_tests.py)에는 포함되지 않습니다
(단타 레포가 옆에 있어야 하므로).

원본 쪽 차이 보정(비교 대상이 아닌 부분):
- 원본 _try_buy의 14:50 이후 매수 차단을 피하려고 now_kst를 10:00으로 고정
- 원본 매수 수량은 order_cash_per_trade(1,000,000) // 가격이라, 가격을
  1,000,000 // 수량으로 넘겨 같은 수량이 되게 함
- 원본 첫 체결/청산 부작용(진입시각·손실카운트·알림)은 컨텍스트 pop +
  호출 기록으로 대체 (내용은 단타 규칙이라 비교 대상 아님)
- 원본 강제 매도는 사유 문자열("강제청산 테스트")로, 새 구현은 forced=True로 지정
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scenarios import SCENARIOS  # noqa: E402


def run(mode, repo, name):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), mode, name],
                       cwd=repo, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode:
        return None, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1]), None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", default=os.path.join(HERE, "..", "..", "..", "KIWOOM-AUTO-TRADER"),
                    help="단타 레포 경로")
    ap.add_argument("--new", default=os.path.join(HERE, "..", ".."), help="스윙 레포 경로")
    args = ap.parse_args()
    orig, new = os.path.abspath(args.orig), os.path.abspath(args.new)
    bad = 0
    for name in SCENARIOS:
        o, eo = run("orig", orig, name)
        n, en = run("new", new, name)
        if o is None or n is None:
            print("ERROR", name, eo or en)
            bad += 1
            continue
        diffs = [(i, a["step"], k, a[k], b[k])
                 for i, (a, b) in enumerate(zip(o, n)) for k in a
                 if k != "res" and a[k] != b[k]]
        buy_res = [(a["step"], a["res"], b["res"]) for a, b in zip(o, n)
                   if a["step"][0] == "buy" and a["res"] != b["res"]]
        if diffs or buy_res:
            bad += 1
        print(f"{'SAME' if not (diffs or buy_res) else 'DIFF':5} {name} (steps={len(o)})")
        for d in (diffs + buy_res)[:4]:
            print("      ", str(d)[:400])
    print(f"{len(SCENARIOS) - bad}/{len(SCENARIOS)} identical")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
