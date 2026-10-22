from __future__ import annotations

"""연구 결과 파일 — 고유 ID·배타적 생성(E3, GPT 10/22). 같은 이름이 있으면 절대 덮어쓰지 않음."""

import secrets
from datetime import datetime
from pathlib import Path


def create_exclusive(out_dir: str | Path, stem: str, exts: tuple[str, ...], *, now: datetime | None = None,
                     tries: int = 5) -> tuple[str, dict[str, Path]]:
    """(실행 ID, 확장자 → 빈 파일 경로). 모든 파일을 'x' 모드로 먼저 만들어 자리를 잡음(동시에 돌려도 충돌 없음)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(tries):
        rid = f"{(now or datetime.now()):%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
        made: dict[str, Path] = {}
        try:
            for ext in exts:
                p = out / f"{stem}_{rid}{ext}"
                with p.open("x", encoding="utf-8"):
                    pass
                made[ext] = p
            return rid, made
        except FileExistsError:
            for p in made.values():
                p.unlink(missing_ok=True)
    raise FileExistsError(f"{out}에 고유한 결과 파일 이름을 만들지 못함")
