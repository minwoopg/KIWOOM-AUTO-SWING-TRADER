"""시작 당시 체크아웃 정보 — `tools/watch_daemon.py code_version()`과 같은 규칙(매매 실행기 상태 파일용, M3).

Git HEAD와 추적 파일 로컬 수정 여부만 읽음. 명령 실패는 '확인 불가'. 미추적 파일은 보지 않음.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def code_version(root: Path = ROOT) -> str:
    def git(*a):
        try:
            r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    head = git("rev-parse", "--short=12", "HEAD")
    if not head:
        return "HEAD 확인 불가"
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty is None:
        return f"HEAD {head} (추적 파일 수정 여부 확인 불가)"
    return f"HEAD {head}" + (" (추적 파일 로컬 수정 있음)" if dirty else " (추적 파일 수정 없음)")
