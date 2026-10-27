"""스윙 자동매매 관리 창 (Tkinter 데스크톱 — 브라우저·서버·추가 설치 없음).

    scripts\\run_dashboard.bat            # 더블클릭
    python -m dashboard.desktop          # 같은 동작(콘솔 창이 남음)

- 읽기 전용: 저장된 기록(DB mode=ro·상태 파일·로그)을 다시 읽어 보여줌. 키움 API 호출·주문 없음.
- 관리자·실행기를 띄우거나 끄지 않음 — 창을 닫아도 그대로 돎.
- 쓰는 것은 신규 매수 중단 요청 파일 하나(없을 때만 생성, 실행기가 다음 폴링에서 반영). 재개는 PowerShell에서 사람이.
- 자료 계층은 `dashboard/data.py`(시험 `test_dashboard.py`).
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, font as tkfont, messagebox, ttk  # noqa: E402

from dashboard import data as D  # noqa: E402

REFRESH_MS = 5000
GREEN, YELLOW, RED, GREY = "#1a7f37", "#9a6700", "#cf222e", "#57606a"


def load_settings_for(root: Path):
    from config.settings import load_settings
    env = root / ".env"                  # 비밀값을 '가리기' 위해서만 읽음 — 화면·저장 파일에 내보내지 않음
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.strip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return load_settings(root / "config" / "settings.yaml")


def now_local() -> datetime:
    from utils.time_utils import now_local as _n
    return _n()


def run_color(label: str) -> str:
    if label.startswith("실행 중"):
        return GREEN
    if label.startswith("응답 없음") or label.startswith("읽기 실패"):
        return RED
    return GREY


STATE_COLOR = {D.OK: GREEN, D.MISSING: GREY, D.UNREADABLE: RED, D.LOCKED: YELLOW, D.NOT_IMPLEMENTED: GREY}


def meta_text(s: D.Section) -> str:
    bits = [f"자료: {D.STATE_LABEL.get(s.state, s.state)}"]
    if s.source:
        bits.append(f"출처 {s.source}")
    if s.as_of:
        bits.append(f"기록 시각 {s.as_of}")
    out = " · ".join(bits)
    if s.error:
        out += f"\n오류: {s.error}"
    for n in s.notes:
        out += f"\n참고: {n}"
    return out


class Table(ttk.Frame):
    """제목 줄 + 표(Treeview) + 스크롤. 행을 두 번 누르면 전체 내용."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.meta = ttk.Label(self, text="", style="Meta.TLabel", justify="left")
        self.meta.pack(fill="x", padx=6, pady=(6, 2))
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree = ttk.Treeview(box, show="headings")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._detail)
        self.empty = ttk.Label(self, text="", style="Empty.TLabel")
        self.cols: list[str] = []

    def show(self, rows: list[dict] | None, meta: str, empty: str) -> None:
        self.meta.configure(text=meta)
        rows = rows or []
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        if cols != self.cols:
            self.tree.configure(columns=cols)
            for c in cols:
                self.tree.heading(c, text=c)
                width = 260 if c in ("내용", "조치", "신규 진입 제외 사유", "사유", "비고", "메모", "상태", "가격 출처", "결과",
                                     "S1 판단", "현재 상태", "종목", "출처", "기준", "데이터") else 120
                self.tree.column(c, width=width, minwidth=60, stretch=True, anchor="w")
            self.cols = cols
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", values=["" if r.get(c) is None else str(r.get(c)) for c in cols])
        if rows:
            self.empty.pack_forget()
        else:
            self.empty.configure(text=empty)
            self.empty.pack(fill="x", padx=12, pady=6)

    def _detail(self, _evt) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        messagebox.showinfo("행 내용", "\n".join(f"{c}: {v}" for c, v in zip(self.cols, vals)), parent=self)


class DetailTable(Table):
    """UI 1차: 표 + 아래 상세(선택한 종목의 저장된 판단 근거). 행을 한 번 누르면 상세가 바뀜."""

    def __init__(self, master, app):
        super().__init__(master, app)
        self.detail = tk.Text(self, height=14, wrap="word", font="TkTextFont")
        self.detail.pack(fill="both", expand=False, padx=6, pady=(0, 6))
        self.detail.configure(state="disabled")
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.details: dict[str, list[str]] = {}
        self._sel_code = None

    def show_with(self, rows, details, meta, empty):
        keep = self._sel_code
        self.details = details or {}
        self.show(rows, meta, empty)
        if keep is not None:                       # 새로고침해도 선택 유지
            for iid in self.tree.get_children():
                vals = self.tree.item(iid, "values")
                if "코드" in self.cols and vals[self.cols.index("코드")] == keep:
                    self.tree.selection_set(iid)
                    break
        self._render_detail()

    def _select(self, _evt=None):
        sel = self.tree.selection()
        if sel and "코드" in self.cols:
            self._sel_code = self.tree.item(sel[0], "values")[self.cols.index("코드")]
        self._render_detail()

    def _render_detail(self):
        lines = self.details.get(self._sel_code) if self._sel_code else None
        txt = "\n".join(lines) if lines else "종목을 누르면 판정 당시 저장된 근거(조건별 값·승인·관찰·실행기 판단)를 여기에 보여줍니다."
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", txt)
        self.detail.configure(state="disabled")


class App:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self.settings = load_settings_for(root_dir)
        self.P = D.paths_from_settings(self.settings, root_dir)
        self.red = D.Redactor(self.settings)
        self.win = tk.Tk()
        self.win.title("스윙 자동매매 관리")
        self.win.geometry("1420x900")
        self.win.minsize(1000, 640)
        self._fonts()
        self.auto = tk.BooleanVar(value=True)
        self._job = None
        self._build()
        self.refresh()

    # ── 모양 ──
    def _fonts(self) -> None:
        fams = set(tkfont.families(self.win))
        fam = next((f for f in ("Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "NanumGothic") if f in fams), None)
        base = tkfont.nametofont("TkDefaultFont")
        if fam:
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                tkfont.nametofont(name).configure(family=fam)
        base.configure(size=10)
        fam = fam or base.cget("family")
        self.f_title = (fam, 12, "bold")
        self.f_state = (fam, 11, "bold")
        st = ttk.Style(self.win)
        st.configure("Meta.TLabel", foreground=GREY)
        st.configure("Empty.TLabel", foreground=GREY)
        st.configure("Treeview", rowheight=24)
        st.configure("Treeview.Heading", font=(fam, 10, "bold"))

    def _build(self) -> None:
        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Checkbutton(bar, text="5초마다 자동 갱신", variable=self.auto, command=self._schedule).pack(side="left")
        ttk.Button(bar, text="지금 새로고침", command=self.refresh).pack(side="left", padx=6)
        ttk.Button(bar, text="로그·운영 요약 저장…", command=self.save_bundle).pack(side="left", padx=6)
        ttk.Button(bar, text="실행 명령 보기", command=self.show_commands).pack(side="left", padx=6)
        self.clock = ttk.Label(bar, text="", style="Meta.TLabel")
        self.clock.pack(side="right")

        top = ttk.Frame(self.win)
        top.pack(fill="x", padx=10, pady=6)
        self.cards = {}
        for i, (key, title) in enumerate((("daemon", "관리자 (조회 전용)"), ("runner", "매매 실행기"), ("halt", "신규 매수 중단"))):
            f = ttk.LabelFrame(top, text=title)
            f.grid(row=0, column=i, sticky="nsew", padx=5)
            top.columnconfigure(i, weight=1)
            state = tk.Label(f, text="", font=self.f_state, anchor="w", justify="left")
            state.pack(fill="x", padx=8, pady=(6, 2))
            body = ttk.Label(f, text="", justify="left", wraplength=420)
            body.pack(fill="x", padx=8, pady=(0, 6))
            self.cards[key] = (f, state, body)
        hf = self.cards["halt"][0]
        row = ttk.Frame(hf)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row, text="사유").pack(side="left")
        self.reason = ttk.Entry(row, width=24)
        self.reason.pack(side="left", padx=4)
        self.halt_btn = ttk.Button(row, text="신규 매수 중단 요청…", command=self.request_halt)
        self.halt_btn.pack(side="left", padx=4)

        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.tabs: dict[str, Table] = {}
        # UI 1차: 첫 탭 '오늘 요약'(오늘 거래가 없는/있는 이유), '감시 종목'은 종목별 판단 + 선택 상세
        sf = ttk.Frame(self.nb)
        self.nb.add(sf, text="오늘 요약")
        self.sum_state = tk.Label(sf, text="", font=self.f_title, anchor="w", justify="left", wraplength=1300)
        self.sum_state.pack(fill="x", padx=10, pady=(10, 2))
        self.sum_basis = ttk.Label(sf, text="", style="Meta.TLabel", justify="left", wraplength=1300)
        self.sum_basis.pack(fill="x", padx=10, pady=(0, 4))
        st = Table(sf, self)
        st.pack(fill="both", expand=True)
        self.tabs["summary"] = st
        for key, title in (("watch", "감시 종목"), ("progress", "오늘 진행"), ("holdings", "보유"), ("orders", "주문"),
                           ("errors", "최근 오류"), ("events", "비용·체결 근거"), ("realized", "실현 손익")):
            t = DetailTable(self.nb, self) if key == "watch" else Table(self.nb, self)
            self.nb.add(t, text=title)
            self.tabs[key] = t
        lf = ttk.Frame(self.nb)
        self.nb.add(lf, text="오늘 로그")
        self.log_text = tk.Text(lf, wrap="none", height=10)
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

    # ── 갱신 ──
    def _schedule(self) -> None:
        if self._job is not None:
            self.win.after_cancel(self._job)
            self._job = None
        if self.auto.get():
            self._job = self.win.after(REFRESH_MS, self._tick)

    def _tick(self) -> None:
        self._job = None
        self.refresh()

    def refresh(self) -> None:
        try:
            now = now_local()
            snap = D.snapshot(self.P, self.settings, now)
            self._render(snap, now)
        except Exception as exc:          # 화면 오류가 창을 닫지 않게 — 오류 로그에 남김
            self._error_log(exc)
            self.clock.configure(text=f"화면 갱신 오류: {type(exc).__name__} — logs/dashboard_error.log")
        finally:
            self._schedule()

    def _card(self, key: str, state_text: str, color: str, body: str) -> None:
        _, st, bd = self.cards[key]
        st.configure(text=state_text, fg=color)
        bd.configure(text=body)

    def _render(self, snap: dict, now: datetime) -> None:
        self.clock.configure(text=f"화면 시각 {snap['now']} · 구역마다 기록 시각이 다름(같은 순간의 확정 상태 아님) · "
                                  f"키움 API 호출 없음 · {'자동 갱신 5초' if self.auto.get() else '일시정지'}")
        dm = snap["daemon"]
        if dm.state == D.OK:
            r = dm.data
            age = r.get("heartbeat_age_sec")
            self._card("daemon", f"● {r['label']}", run_color(r["label"]),
                       f"heartbeat {r['heartbeat_at']}" + (f" ({age:.0f}초 전)" if age is not None else "")
                       + f"\n현재 작업: {r.get('current_task') or '-'}" + (f" · {r['progress']}" if r.get("progress") else "")
                       + f"\n실행 ID {r['run_id']} · 코드 {r['code_version']}\n모드: {r['mode']}"
                       + (f"\n마지막 오류: {r['last_error']}" if r.get("last_error") else "") + "\n" + meta_text(dm))
        else:
            self._card("daemon", f"● {D.STATE_LABEL[dm.state]}", STATE_COLOR[dm.state], (dm.error or "") + "\n" + meta_text(dm))
        rn = snap["runner"]
        if rn.state == D.OK:
            r = rn.data
            age = r.get("heartbeat_age_sec")
            rec = r.get("reconcile_ok")
            self._card("runner", f"● {r['label']} · 단계 {r.get('stage')}", run_color(r["label"]),
                       f"heartbeat {r.get('heartbeat_at')}" + (f" ({age:.0f}초 전)" if age is not None else "")
                       + f"\n실행 모드 {r.get('execution_mode', '?')} · 계좌 라벨 {r.get('account_scope', '?')}"
                       + f"\n코드 {r.get('code_version', '?')} · 실행 ID {r.get('run_id')}"
                       + f"\n{r['strategy_label'] if not r.get('strategy_connected') else '전략: ' + str(r.get('strategy_id'))}"
                       + (f"\n신규 주문 중단(원장·잔고 문제): {r['halted_reason']}" if r.get("halted_reason") else "")
                       + f"\n잔고 조회 {r.get('last_balance_at') or '-'} · 장부 대조 "
                       + ("일치" if rec else ("불일치" if rec is False else "미확인")) + "\n" + meta_text(rn))
        else:
            self._card("runner", f"● {D.STATE_LABEL[rn.state]}", STATE_COLOR[rn.state], (rn.error or "") + "\n" + meta_text(rn))
        hv = snap["halt"].data
        color = {"NONE": GREEN, "REQUESTED": YELLOW, "APPLIED": RED}[hv["state"]]
        req = hv.get("request") or {}
        body = (f"요청 {req.get('requested_at', '?')} · {req.get('requested_by', '?')} · 사유 {req.get('reason', '')}\n"
                f"실행기 반영 {hv.get('applied_at') or '아직 반영 전'}\n"
                "재개는 사람이 PowerShell에서: python tools/buy_halt.py resume --note \"...\" --confirm (자동 재개 없음)"
                if req else "요청하면 실행기가 다음 폴링(최대 약 60초)에 반영 — 매도·손절·미해결 주문 확인은 계속")
        self._card("halt", f"● {hv['label']}", color, body)
        self.halt_btn.state(["disabled"] if req else ["!disabled"])

        t = self.tabs
        sm = snap["summary"]
        if sm.state == D.OK:
            d = sm.data
            col = {"정상 처리": GREEN, "아직 처리하지 않음": YELLOW}.get(d["state"], RED)
            self.sum_state.configure(text=f"[{d['state']}] {d['headline']}", fg=col)
            b = d.get("basis") or {}
            self.sum_basis.configure(text=(f"기준: 거래일 {b.get('trade_date', '-')} · 신호일 {b.get('signal_date', '-')} · 스캔 "
                                           f"{b.get('scan', '-')} · 주문 허용 {', '.join(b.get('allowed') or [])} — 모든 건수는 "
                                           "이 한 기준으로만 셈(다른 날 스캔·관찰을 섞지 않음)"))
            t["summary"].show(d["steps"], meta_text(sm), "단계 기록 없음")
        else:
            self.sum_state.configure(text=f"[처리 실패·확인 불가] 오늘 요약 {D.STATE_LABEL[sm.state]}", fg=RED)
            self.sum_basis.configure(text=sm.error)
            t["summary"].show([], meta_text(sm), "요약 자료 없음")
        sy = snap["symbols"]
        if sy.state == D.OK:
            t["watch"].show_with(sy.data["rows"], sy.data["details"], meta_text(sy), "감시 종목 없음")
        else:
            w = snap["watch"]
            t["watch"].show_with(w.data if w.state == D.OK else [], {}, meta_text(sy) + "\n(종목별 판단을 읽지 못해 기본 표)",
                                 f"감시 종목 자료 없음 — {D.STATE_LABEL[w.state]} {w.error}")
        pr = snap["progress"]
        t["progress"].show(pr.data, meta_text(pr), "진행 기록 없음")
        h = snap["holdings"]
        t["holdings"].show(h.data if h.state == D.OK else [], meta_text(h), "보유 없음" if h.state == D.OK
                           else f"보유 확인 불가 — {D.STATE_LABEL[h.state]}")
        o = snap["orders"]
        t["orders"].show(o.data if o.state == D.OK else [], meta_text(o), "주문 기록 없음")
        e = snap["errors"]
        t["errors"].show(e.data or [], meta_text(e), "최근 오류 없음" if e.state == D.OK else f"오류 확인 불가 — {e.error}")
        c = snap["costs"]
        if c.state == D.OK:
            cm = c.data["cost_model"]
            cm_txt = "비용 모델(추정 비용, 왕복 %): " + ", ".join(f"{k} {v}" for k, v in cm.items())
            t["events"].show(c.data["events"], cm_txt + "\n" + meta_text(c), "체결 사건 없음")
            t["realized"].show(c.data["realized"], meta_text(c), "실현 없음")
        else:
            t["events"].show([], meta_text(c), f"원장 {D.STATE_LABEL[c.state]}")
            t["realized"].show([], meta_text(c), f"원장 {D.STATE_LABEL[c.state]}")
        lines = []
        for path, name in ((self.P.app_log, "실행기 app.log"), (self.P.daemon_log, "관리자 watch_daemon.log")):
            got = D.log_lines(path, now.date(), self.red)[-200:]
            lines.append(f"── {name} — {len(got)}줄" + ("" if path.exists() else " (파일 없음)"))
            lines += got
        first = self.log_text.yview()[0]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(lines))
        self.log_text.configure(state="disabled")
        self.log_text.yview_moveto(first)

    # ── 조작 ──
    def request_halt(self) -> None:
        reason = self.reason.get().strip()
        if not messagebox.askyesno("신규 매수 중단",
                                   "신규 매수를 멈춥니다.\n\n보유 종목의 매도·손절과 미해결 주문 확인은 계속됩니다.\n"
                                   "실행기가 다음 폴링(최대 약 60초)에 반영하며, 자동으로 다시 열리지 않습니다.\n\n요청할까요?",
                                   parent=self.win):
            return
        from infra.storage.runner_status import request_buy_halt
        created, r = request_buy_halt(self.P.commands_dir, reason=reason, by="desktop", now=now_local())
        messagebox.showinfo("신규 매수 중단", (f"요청을 만들었습니다: {r['request_id']}" if created
                                         else f"이미 요청돼 있습니다: {r.get('request_id')}") + "\n실행기 반영 전까지 '요청됨'으로 표시됩니다.",
                            parent=self.win)
        self.refresh()

    def save_bundle(self) -> None:
        top = tk.Toplevel(self.win)
        top.title("로그·운영 요약 저장")
        top.transient(self.win)
        ttk.Label(top, text="날짜(YYYY-MM-DD)").pack(padx=12, pady=(12, 4), anchor="w")
        ent = ttk.Entry(top, width=16)
        ent.insert(0, date.today().isoformat())
        ent.pack(padx=12, anchor="w")

        def go():
            try:
                day = date.fromisoformat(ent.get().strip())
            except ValueError:
                messagebox.showerror("날짜", "YYYY-MM-DD 형식", parent=top)
                return
            path = filedialog.asksaveasfilename(parent=top, defaultextension=".zip", initialfile=f"swing_ops_{day}.zip",
                                                filetypes=[("zip", "*.zip")])
            if not path:
                return
            snap = D.snapshot(self.P, self.settings, now_local(), day)
            summary = {k: (D.section_dict(v) if isinstance(v, D.Section) else v) for k, v in snap.items()}
            Path(path).write_bytes(D.download_bundle(self.P, day, self.red, summary))
            messagebox.showinfo("저장", f"저장했습니다(비밀값 가림, .env·DB 원본 제외):\n{path}", parent=top)
            top.destroy()

        ttk.Button(top, text="저장 위치 고르기…", command=go).pack(padx=12, pady=12, anchor="w")

    def show_commands(self) -> None:
        top = tk.Toplevel(self.win)
        top.title("실행 명령 (복사용 — 이 창에서는 실행하지 않음)")
        top.transient(self.win)
        ttk.Label(top, text="PowerShell에 붙여 넣어 실행하세요. 이 창은 명령을 실행하지 않습니다.", style="Meta.TLabel").pack(
            padx=10, pady=(10, 4), anchor="w")
        for title, cmd in D.commands_help(self.P):
            row = ttk.Frame(top)
            row.pack(fill="x", padx=10, pady=2)
            ttk.Label(row, text=title, width=26).pack(side="left")
            e = ttk.Entry(row, width=90)
            e.insert(0, cmd)
            e.configure(state="readonly")
            e.pack(side="left", padx=4)
            ttk.Button(row, text="복사", command=lambda c=cmd: (self.win.clipboard_clear(), self.win.clipboard_append(c))
                       ).pack(side="left")

    def _error_log(self, exc: Exception) -> None:
        try:
            p = self.root_dir / "logs" / "dashboard_error.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {self.red.text(traceback.format_exc())}\n")
        except OSError:
            pass

    def run(self) -> None:
        self.win.mainloop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="스윙 자동매매 관리 창(읽기 전용 + 신규 매수 중단 요청)")
    ap.add_argument("--root", help="자료 폴더(data/·logs/·config/) — 기본은 이 저장소. 검증용 사본을 볼 때만")
    ap.add_argument("--tab", type=int, default=0, help=argparse.SUPPRESS)      # 검증 화면 캡처용
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else CODE_ROOT
    try:
        app = App(root)
        app.nb.select(a.tab)
        app.run()
    except Exception as exc:              # 시작 실패(설정 오류 등)를 창으로 알림
        try:
            messagebox.showerror("관리 창 시작 실패", f"{type(exc).__name__}: {exc}")
        except Exception:                 # noqa: BLE001
            print(f"관리 창 시작 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
