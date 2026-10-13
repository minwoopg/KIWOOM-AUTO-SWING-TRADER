# -*- coding: utf-8 -*-
"""관리 창(Tkinter) 동작 — 화면이 없는 환경(CI·tkinter 없음)에서는 건너뜀(통과로 세지 않고 SKIP 표시).

창을 숨긴 채 만들고: 그리기, 반복 새로고침 뒤 자료 바이트 동일, 신규 매수 중단 요청(확인 대화상자는 시험용으로 '예') 두 번 →
요청 하나·'요청됨'·버튼 꺼짐. 키움 API·프로세스 실행 없음.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
except Exception as exc:          # noqa: BLE001
    print(f"[SKIP] 관리 창 시험 — 화면/tkinter 없음: {type(exc).__name__}: {exc}")
    print("\n총 0건 중 통과 0건, 실패 0건 (SKIP)")
    sys.exit(0)

from dashboard import desktop as W  # noqa: E402
from dashboard.sample_data import build  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="desk_"))
NOW = datetime.now().replace(microsecond=0)


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


R1 = build(TMP / "normal", "normal", NOW)
before = tree_hash(R1)
app = W.App(R1)
app.win.withdraw()
for _ in range(5):
    app.refresh()
    app.win.update()
st = app.cards["daemon"][1].cget("text")
rows_watch = len(app.tabs["watch"].tree.get_children())
after = tree_hash(R1)
check("1) 창 그리기: 관리자 '실행 중', 감시 종목 3행, 5번 새로고침 뒤 자료 바이트 동일(쓰기 없음)",
      "실행 중" in st and rows_watch == 3 and before == after)
app.win.destroy()

R2 = build(TMP / "empty", "empty", NOW)
app2 = W.App(R2)
app2.win.withdraw()
W.messagebox.askyesno = lambda *a, **k: True
W.messagebox.showinfo = lambda *a, **k: None
app2.reason.insert(0, "창 시험")
app2.request_halt()
app2.request_halt()
app2.win.update()
files = sorted(str(p.relative_to(R2)) for p in R2.rglob("*") if p.is_file())
label = app2.cards["halt"][1].cget("text")
check("2) 신규 매수 중단 요청 두 번 → 요청 파일 하나, '요청됨 — 실행기 반영 전', 버튼 꺼짐",
      files == ["commands/halt_new_buys.json", "config/settings.yaml"] and "반영 전" in label
      and app2.halt_btn.instate(["disabled"]))
W.messagebox.askyesno = lambda *a, **k: False
R3 = build(TMP / "cancel", "empty", NOW)
app3 = W.App(R3)
app3.win.withdraw()
app3.request_halt()
check("3) 확인 대화상자에서 '아니오' → 요청 없음", not (R3 / "commands/halt_new_buys.json").exists())
for a in (app2, app3):
    a.win.destroy()

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
