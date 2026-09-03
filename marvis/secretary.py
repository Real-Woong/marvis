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
import os
import re
import subprocess
import sys
import time

from .db import log_event, new_id, next_seq, transaction
from .settings import SECRETARY_DIR
from .time_utils import now_string, today_kst_date

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


# ---------------------------------------------------------------- 쓰기(write-back)


class WriteBackError(Exception):
    """_STATUS.md를 고치지 못했습니다.

    이 예외가 나면 DB도 건드리면 안 됩니다. 원본은 파일이므로 파일에 없는 값이
    DB에 남으면 다음 동기화에서 조용히 되돌아갑니다. 사용자에게는 저장된 것처럼
    보이는데 실제로는 사라지는 것이 가장 나쁩니다.
    """


# 되돌리기가 완전하지 않습니다. Marvis의 '중단'은 SECRETARY의 관찰중/멈춤/종료가
# 합쳐진 것이라 어느 쪽인지 알 수 없습니다. sub_status가 SECRETARY의 상태 이름일
# 때만 그걸 쓰고, 아니면(Marvis 고유 태그거나 비어 있으면) '멈춤'으로 적습니다.
_SECRETARY_STATES = ("진행중", "관찰중", "멈춤", "종료")

# render.py의 프론트매터 경계와 같은 규칙입니다. 여기서는 의미를 읽는 게 아니라
# 고칠 자리를 찾는 것뿐입니다. 파싱은 계속 render.py 하나만 합니다.
_FRONTMATTER = re.compile(r"^(---\n)(.*?\n)(---\n?)(.*)$", re.S)

_SUMMARY_HEADING = "## 한 줄 요약"


def status_file(status_path: str) -> "os.PathLike":
    """status_path(색인의 상대 경로)에 해당하는 _STATUS.md 경로.

    render.py의 ROOT = SCRIPT_DIR.parent와 같은 규칙입니다. 둘이 어긋나면 파일을
    못 찾아 write_back이 실패하므로, 조용히 엉뚱한 파일을 고치지는 않습니다.
    """
    return SECRETARY_DIR.parent / status_path / "_STATUS.md"


def _to_secretary_status(status: str, sub_status: str | None) -> str:
    if status == "진행중":
        return "진행중"
    if sub_status in _SECRETARY_STATES:
        return sub_status
    return "멈춤"


def _set_scalar(frontmatter: str, key: str, value: str) -> str:
    """`key: value` 한 줄을 바꿉니다. 뒤에 붙은 주석은 그대로 둡니다."""
    pattern = re.compile(rf"^({key}:)([^\n#]*)(#[^\n]*)?$", re.M)

    def replace(match: re.Match) -> str:
        old, comment = match.group(2), match.group(3) or ""
        new = f" {value}"
        if comment:
            # 주석 위치가 흔들리면 사람이 보는 diff가 지저분해집니다.
            new = new.ljust(len(old))
        return match.group(1) + new + comment

    if pattern.search(frontmatter):
        return pattern.sub(replace, frontmatter, count=1)
    return frontmatter + f"{key}: {value}\n"


def _set_list(frontmatter: str, key: str, items: list[str]) -> str:
    """`key:` 아래의 `  - ` 항목들을 통째로 갈아끼웁니다."""
    block = f"{key}:\n" + "".join(f"  - {item}\n" for item in items)
    # render.py의 항목 규칙(^\s+-)과 같은 모양만 먹습니다.
    pattern = re.compile(rf"^{key}:[^\n]*\n(?:[ \t]+-[^\n]*\n)*", re.M)
    if pattern.search(frontmatter):
        return pattern.sub(lambda _: block, frontmatter, count=1)
    return frontmatter + block


def _set_summary(body: str, text: str) -> str:
    """본문 `## 한 줄 요약` 아래 문단을 바꿉니다. 다른 절은 건드리지 않습니다."""
    pattern = re.compile(r"^(##\s*한 줄 요약\s*\n)(.*?)(?=\n##|\Z)", re.S | re.M)
    if pattern.search(body):
        return pattern.sub(lambda m: m.group(1) + text + "\n", body, count=1)
    return f"{_SUMMARY_HEADING}\n{text}\n\n{body}"


def _atomic_write(path, text: str) -> None:
    """같은 디렉터리에 임시 파일로 쓴 뒤 바꿔치기합니다.

    중간에 죽어도 반쯤 쓰다 만 _STATUS.md는 남지 않습니다.
    """
    tmp = path.with_name(path.name + ".marvis-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _current_list(status_path: str, key: str) -> list[str]:
    """색인(= render.py가 읽은 결과)에서 지금 목록을 가져옵니다.

    파일에서 직접 읽지 않는 이유는 파서를 둘로 만들지 않기 위해서입니다.
    """
    index = load_index(refresh=True, force=True)
    if index is None:
        return []
    for record in index["projects"]:
        if record.get("path") == status_path:
            return [str(item).strip()
                    for item in (record.get(key) or []) if str(item).strip()]
    return []


def _verify(status_path: str, expected: dict) -> dict:
    """색인을 다시 만들고, 단일 파서가 실제로 무엇을 읽었는지 확인합니다.

    우리가 텍스트를 고친 것과 render.py가 읽는 것이 같다는 보장은 이 확인뿐입니다.
    """
    if not refresh_index(force=True):
        raise WriteBackError("색인을 다시 만들지 못해 반영을 확인할 수 없습니다.")

    index = load_index(refresh=False)
    if index is None:
        raise WriteBackError("색인을 읽지 못해 반영을 확인할 수 없습니다.")

    for record in index["projects"]:
        if record.get("path") == status_path:
            break
    else:
        raise WriteBackError(f"고친 뒤 색인에서 사라졌습니다: {status_path}")

    mismatched = [
        f"{key}: 쓴 값 {value!r} → 읽힌 값 {record.get(key)!r}"
        for key, value in expected.items()
        if record.get(key) != value
    ]
    if mismatched:
        raise WriteBackError("고친 내용이 그대로 읽히지 않았습니다 — " + " · ".join(mismatched))
    return record


def write_back(
    status_path: str,
    *,
    status: str | None = None,
    sub_status: str | None = None,
    next_steps: str | None = None,
    blockers: list[str] | None = None,
    note: str | None = None,
) -> dict:
    """_STATUS.md를 고치고, 다시 읽어 확인한 뒤, 색인 레코드를 돌려줍니다.

    확인에 실패하면 파일을 원래대로 되돌리고 WriteBackError를 올립니다.
    """
    path = status_file(status_path)
    if not path.is_file():
        raise WriteBackError(f"_STATUS.md를 찾지 못했습니다: {path}")

    original = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(original)
    if match is None:
        raise WriteBackError(f"프론트매터가 없어 안전하게 고칠 수 없습니다: {path}")

    opening, frontmatter, closing, body = match.groups()
    expected: dict = {}

    if status is not None:
        secretary_status = _to_secretary_status(status, sub_status)
        frontmatter = _set_scalar(frontmatter, "status", secretary_status)
        expected["status"] = secretary_status
    if next_steps is not None:
        # 목록을 통째로 갈아엎으면 백로그가 조용히 사라집니다. 소곤.zip처럼
        # next가 네 줄인 프로젝트에서 "다음할일은 X" 한마디에 나머지 셋이
        # 없어지면 안 됩니다. 맨 앞에 놓기만 합니다. 브리핑은 next[0]을
        # 읽으므로 들리는 결과는 같고, 나머지는 파일에 남습니다.
        # 빈 문자열은 "다음 할 일 없음"이라는 뜻이므로 그때만 비웁니다.
        head = next_steps.strip()
        if head:
            items = [head] + [item for item in _current_list(status_path, "next")
                              if item != head]
        else:
            items = []
        frontmatter = _set_list(frontmatter, "next", items)
        expected["next"] = items
    if blockers is not None:
        items = [item.strip() for item in blockers if item.strip()]
        frontmatter = _set_list(frontmatter, "blockers", items)
        expected["blockers"] = items
    if note is not None:
        # render.py는 요약을 공백 정리해서 읽으므로 확인도 같은 모양으로 합니다.
        summary = " ".join(note.split())
        body = _set_summary(body, summary)
        expected["summary"] = summary

    if not expected:
        return _verify(status_path, {})

    # 사람이 손으로 고쳤을 때 하는 것과 같습니다.
    frontmatter = _set_scalar(frontmatter, "updated", today_kst_date().isoformat())

    _atomic_write(path, opening + frontmatter + closing + body)
    try:
        record = _verify(status_path, expected)
    except WriteBackError:
        _atomic_write(path, original)
        raise
    logging.info("_STATUS.md 반영 — %s (%s)", status_path, ", ".join(expected))
    return record


# ---------------------------------------------------------------- 텔레그램 메모

NOTE_HEADING = "## [텔레그램 전송]"

# 이 파일을 나중에 여는 사람이나 에이전트(Claude Code, Codex)가 이 절의 성격을
# 오해하지 않게 하는 표지입니다. 마크다운 주석이라 렌더링에는 안 보이고,
# 원문을 읽는 쪽에는 반드시 보입니다.
_NOTE_PREAMBLE = """<!-- 진웅이 텔레그램으로 그때그때 보낸 것.
     코드나 git 이력에서 나온 내용이 아니라 즉흥적으로 떠오른 착상이다.
     다듬어지지 않았고 검증도 안 됐다. 이 중 무엇을 next / blockers 로
     올릴지는 사람이 판단한다. 여기 있다는 이유만으로 합의된 계획이 아니다.
     Marvis(marvis/secretary.py)가 맨 아래에 덧붙이기만 한다. 지우지 않는다. -->"""


def _append_to_note_section(body: str, entry: str) -> str:
    """이미 있는 `## [텔레그램 전송]` 절의 맨 아래에 한 줄 붙입니다."""
    start = body.index(NOTE_HEADING)
    rest = body[start + len(NOTE_HEADING):]
    boundary = re.search(r"^##\s", rest, re.M)
    end = start + len(NOTE_HEADING) + (boundary.start() if boundary else len(rest))

    section = body[start:end].rstrip("\n") + "\n" + entry
    tail = body[end:]
    if tail:
        section += "\n"
    return body[:start] + section + tail


def append_note(status_path: str, text: str) -> str:
    """`## [텔레그램 전송]` 절에 한 줄 덧붙이고, 그 줄을 돌려줍니다.

    프론트매터는 건드리지 않고 `updated:` 도 올리지 않습니다. 떠오른 걸 적어둔
    것은 프로젝트 상태가 갱신된 것과 다릅니다. 여기서 날짜를 올리면 대시보드의
    "며칠 전"이 거짓말이 됩니다. 대신 항목마다 날짜를 답니다.

    render.py 는 이 절을 읽지 않습니다(프론트매터와 `## 한 줄 요약`만 봅니다).
    그래서 검증도 색인이 아니라 파일을 다시 읽어서 합니다.
    """
    content = " ".join(text.split())
    if not content:
        raise WriteBackError("빈 메모는 적지 않습니다.")

    path = status_file(status_path)
    if not path.is_file():
        raise WriteBackError(f"_STATUS.md를 찾지 못했습니다: {path}")

    original = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(original)
    head, body = (original[:match.end(3)], match.group(4)) if match else ("", original)

    entry = f"- {now_string()[:16]} — {content}"
    if NOTE_HEADING in body:
        body = _append_to_note_section(body, entry + "\n")
    else:
        gap = "" if body.endswith("\n\n") or not body else (
            "\n" if body.endswith("\n") else "\n\n")
        body = f"{body}{gap}{NOTE_HEADING}\n{_NOTE_PREAMBLE}\n{entry}\n"

    updated = head + body
    _atomic_write(path, updated)
    if path.read_text(encoding="utf-8") != updated:
        _atomic_write(path, original)
        raise WriteBackError("메모를 쓴 뒤 다시 읽었더니 내용이 다릅니다.")

    logging.info("_STATUS.md 메모 추가 — %s: %s", status_path, content)
    return entry
