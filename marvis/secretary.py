"""SECRETARY가 수집한 프로젝트 상태를 읽어 Marvis의 projects 테이블에 반영합니다.

두 도구는 같은 맥미니, 같은 디스크에 있습니다. 그래서 네트워크로 "받아오는" 게
아니라 파일을 여는 겁니다. 옮길 게 없으니 옮기는 타이머도 없습니다.

_STATUS.md의 파서는 SECRETARY/render.py 하나뿐입니다. 여기서 마크다운을 다시
파싱하면 파서가 둘이 되고 반드시 갈라지므로, render.py가 내보낸 기계용 색인
(_index/all_status.json)만 읽습니다.

색인 재생성은 28개 기준 0.1초라 미리 만들어 둘 이유가 없습니다. 필요할 때
그 자리에서 돌리면 항상 최신이고, 감시할 데몬이 하나도 늘지 않습니다.
"""

import json
import logging
import subprocess
import sys
import time

from .db import log_event, new_id, next_seq, transaction
from .settings import SECRETARY_DIR
from .time_utils import now_string

INDEX_FILE = SECRETARY_DIR / "_index" / "all_status.json"
RENDER_SCRIPT = SECRETARY_DIR / "render.py"

SOURCE = "secretary"

# render.py가 멈춰 있어도 브리핑까지 같이 멈추면 안 됩니다.
_RENDER_TIMEOUT_SECONDS = 30

# 프로젝트를 묻는 메시지마다 render.py를 새로 띄우지 않도록 하는 하한입니다.
# _STATUS.md를 고치고 1분 안에 물어보는 경우는 사실상 없고, 있어도 다음
# 질문에서 반영됩니다.
_MIN_REFRESH_INTERVAL_SECONDS = 60
_last_refresh_at: float | None = None

# SECRETARY의 4단계를 Marvis의 2단계로 좁힙니다. 아침 브리핑은 '진행중'만
# 읽어주므로, 관찰중/멈춤/종료는 전부 '중단'으로 두고 원래 상태는
# sub_status에 남겨 물어보면 답할 수 있게 합니다.
_STATUS_MAP = {
    "진행중": ("진행중", None),
    "관찰중": ("중단", "관찰중"),
    "멈춤": ("중단", "멈춤"),
    "종료": ("중단", "종료"),
}

# 동기화가 덮어쓰면 안 되는 것. muted_from_briefing은 SECRETARY가 모르는
# Marvis 쪽 취향이라, 사용자가 "브리핑에서 빼줘"라고 한 걸 매번 되살리면 안 됩니다.
_SYNCED_COLUMNS = ("name", "status", "sub_status", "next_steps", "note")


def available() -> bool:
    return RENDER_SCRIPT.is_file() or INDEX_FILE.is_file()


def refresh_index(force: bool = False) -> bool:
    """render.py를 돌려 색인을 다시 만듭니다. 실패해도 예외를 올리지 않습니다."""
    global _last_refresh_at

    if (not force and _last_refresh_at is not None
            and time.monotonic() - _last_refresh_at < _MIN_REFRESH_INTERVAL_SECONDS):
        return True

    if not RENDER_SCRIPT.is_file():
        logging.warning("SECRETARY render.py를 찾지 못했습니다: %s", RENDER_SCRIPT)
        return False
    try:
        subprocess.run(
            [sys.executable, str(RENDER_SCRIPT)],
            cwd=str(SECRETARY_DIR),
            capture_output=True,
            timeout=_RENDER_TIMEOUT_SECONDS,
            check=True,
        )
        _last_refresh_at = time.monotonic()
        return True
    except subprocess.CalledProcessError as error:
        logging.warning(
            "SECRETARY 색인 재생성 실패 (exit %s): %s",
            error.returncode, (error.stderr or b"").decode("utf-8", "replace")[-500:],
        )
    except subprocess.TimeoutExpired:
        logging.warning("SECRETARY 색인 재생성이 %s초를 넘겨 중단했습니다.",
                        _RENDER_TIMEOUT_SECONDS)
    except OSError as error:
        logging.warning("SECRETARY 색인 재생성을 실행하지 못했습니다: %s", error)
    return False


def load_index(refresh: bool = True, force: bool = False) -> dict | None:
    """색인을 읽습니다. 재생성에 실패하면 마지막으로 성공한 색인이라도 씁니다."""
    if refresh:
        refresh_index(force=force)
    if not INDEX_FILE.is_file():
        logging.warning("SECRETARY 색인이 없습니다: %s", INDEX_FILE)
        return None
    try:
        with INDEX_FILE.open(encoding="utf-8") as file:
            payload = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        logging.warning("SECRETARY 색인을 읽지 못했습니다: %s", error)
        return None
    if not isinstance(payload.get("projects"), list):
        logging.warning("SECRETARY 색인 형식이 예상과 다릅니다: %s", INDEX_FILE)
        return None
    return payload


def _to_row(record: dict) -> dict | None:
    """색인 한 건을 projects 테이블의 값으로 옮깁니다."""
    path = (record.get("path") or "").strip()
    name = (record.get("name") or "").strip()
    if not path or not name:
        return None

    status, sub_status = _STATUS_MAP.get(
        (record.get("status") or "").strip(), ("중단", None))

    steps = [str(step).strip() for step in (record.get("next") or []) if str(step).strip()]
    blockers = [str(b).strip() for b in (record.get("blockers") or []) if str(b).strip()]

    # 아침 브리핑은 프로젝트당 한 줄을 소리로 듣는 자리라 둘을 이어 붙이면 너무
    # 깁니다. 막힌 게 있으면 그게 오늘 결정할 일이므로 그것만 보여줍니다.
    if blockers:
        next_steps = f"[막힘] {blockers[0]}"
    else:
        next_steps = steps[0] if steps else None

    return {
        "status_path": path,
        "name": name,
        "status": status,
        "sub_status": sub_status,
        "next_steps": next_steps,
        "note": (record.get("summary") or "").strip() or None,
    }


def sync(refresh: bool = True, force: bool = False) -> dict:
    """색인을 projects 테이블에 반영하고 무엇이 바뀌었는지 돌려줍니다."""
    result = {"added": 0, "updated": 0, "archived": 0, "unchanged": 0, "total": 0}

    index = load_index(refresh=refresh, force=force)
    if index is None:
        result["skipped"] = "색인 없음"
        return result

    rows = [row for row in (_to_row(r) for r in index["projects"]) if row]
    result["total"] = len(rows)
    seen_paths = {row["status_path"] for row in rows}
    now = now_string()

    with transaction() as tx:
        existing = {
            r["status_path"]: dict(r)
            for r in tx.execute(
                "SELECT id, seq, status_path, archived,"
                " name, status, sub_status, next_steps, note"
                " FROM projects WHERE status_path IS NOT NULL"
            )
        }

        for row in rows:
            current = existing.get(row["status_path"])
            if current is None:
                tx.execute(
                    "INSERT INTO projects (id, seq, name, status, sub_status, next_steps,"
                    " note, status_path, source, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_id(), next_seq(tx, "projects"), row["name"], row["status"],
                     row["sub_status"], row["next_steps"], row["note"],
                     row["status_path"], SOURCE, now, now),
                )
                result["added"] += 1
                continue

            changed = any(current[column] != row[column] for column in _SYNCED_COLUMNS)
            if not changed and not current["archived"]:
                result["unchanged"] += 1
                continue

            # muted_from_briefing은 건드리지 않습니다. 사용자가 끈 건 꺼진 채로 둡니다.
            tx.execute(
                "UPDATE projects SET name = ?, status = ?, sub_status = ?, next_steps = ?,"
                " note = ?, source = ?, archived = 0, updated_at = ? WHERE id = ?",
                (row["name"], row["status"], row["sub_status"], row["next_steps"],
                 row["note"], SOURCE, now, current["id"]),
            )
            result["updated"] += 1

        # _STATUS.md가 사라진 프로젝트는 지우지 않고 보관합니다.
        for path, current in existing.items():
            if path not in seen_paths and not current["archived"]:
                tx.execute(
                    "UPDATE projects SET archived = 1, updated_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
                result["archived"] += 1

        # 프로젝트마다 이벤트를 남기면 eval 데이터셋이 동기화 로그로 덮입니다.
        # 실제로 바뀐 게 있을 때만 한 줄로 남깁니다.
        if result["added"] or result["updated"] or result["archived"]:
            log_event(
                tx, "projects.synced", entity="project", source=SOURCE,
                payload={k: v for k, v in result.items() if k != "skipped"},
            )

    return result


def format_sync_result(result: dict) -> str:
    if result.get("skipped"):
        return f"SECRETARY 동기화를 건너뛰었습니다 ({result['skipped']})."
    return (
        f"SECRETARY 프로젝트 {result['total']}개 동기화 — "
        f"추가 {result['added']} · 갱신 {result['updated']} · "
        f"보관 {result['archived']} · 변화없음 {result['unchanged']}"
    )
